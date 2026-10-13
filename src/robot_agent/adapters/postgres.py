"""Short PostgreSQL transactions; state decisions belong to the application/domain."""

from contextlib import asynccontextmanager
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from datetime import datetime, timezone
import sys

from robot_agent.domain.models import (
    Command, ConflictError, ContextSnapshot, NotFoundError, Plan, PlanStep, Task,
)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      default=lambda item: item.isoformat() if isinstance(item, datetime)
                      else sorted(item) if isinstance(item, (set, frozenset)) else item)


def _snapshot(value):
    return ContextSnapshot(**{**value, "skills": frozenset(value["skills"]),
                              "allowed_actions": frozenset(value["allowed_actions"])})


class PostgresStore:
    def __init__(self, pool, *, deployment_id):
        self.pool = pool
        self.deployment_id = deployment_id

    @asynccontextmanager
    async def unit_of_work(self, context):
        if context is None:
            raise ValueError("customer transactions require TrustedContext")
        async with self.pool.acquire() as connection, connection.transaction():
            yield TaskTransaction(connection, context, self.deployment_id)

    @asynccontextmanager
    async def system_unit_of_work(self):
        # Execution writers use one order: slot, sorted tasks, units/commands/bindings.
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.fetchrow("SELECT * FROM ra_execution_guard WHERE deployment_id=$1 FOR UPDATE",
                                      self.deployment_id)
            yield SystemTaskTransaction(connection, None, self.deployment_id)

    async def migrate(self):
        migrations = Path(__file__).resolve().parents[3] / "db/migrations"
        if not migrations.is_dir():
            migrations = Path(sys.prefix) / "share/ro-agent/db/migrations"
        if not migrations.is_dir():
            raise FileNotFoundError("packaged database migrations are missing")
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(719002601)")
            await connection.execute("""CREATE TABLE IF NOT EXISTS ra_schema_migrations
                                      (version integer PRIMARY KEY, checksum text NOT NULL)""")
            for migration in sorted(migrations.glob("*.sql")):
                version = int(migration.name.split("_", 1)[0])
                sql = migration.read_text(encoding="utf-8")
                checksum = sha256(sql.encode()).hexdigest()
                installed = await connection.fetchval("SELECT checksum FROM ra_schema_migrations WHERE version=$1", version)
                if installed and installed != checksum:
                    raise ConflictError("installed migration checksum differs")
                if not installed:
                    await connection.execute(sql)
                    await connection.execute("INSERT INTO ra_schema_migrations VALUES ($1, $2)", version, checksum)
            await connection.execute("INSERT INTO ra_execution_guard (deployment_id) VALUES ($1) ON CONFLICT DO NOTHING",
                                     self.deployment_id)


class TaskTransaction:
    def __init__(self, connection, context, deployment_id):
        self.connection = connection
        self.context = context
        self.deployment_id = deployment_id
        self.loaded_versions = {}

    def _check_customer(self, customer_id, actor_id=None, actor_kind=None):
        if self.context is None:
            return
        if customer_id != self.context.customer_id or (actor_id is not None and actor_id != self.context.actor_id):
            raise NotFoundError("object not found")
        if actor_kind is not None and actor_kind != self.context.actor_kind:
            raise NotFoundError("object not found")

    async def get_request(self, customer_id, actor_id, key, operation="submit"):
        self._check_customer(customer_id, actor_id)
        digest = sha256(_json([customer_id, actor_id, operation, key]).encode()).digest()
        # Stable cross-process lock closes the absent-row race without holding it during model calls.
        lock_id = int.from_bytes(digest[:8], "big", signed=True)
        await self.connection.execute("SELECT pg_advisory_xact_lock($1)", lock_id)
        row = await self.connection.fetchrow("""SELECT request_digest, result_ref FROM ra_request_keys
            WHERE customer_id=$1 AND actor_id=$2 AND operation=$3 AND key=$4""",
                                             customer_id, actor_id, operation, key)
        return (row["request_digest"], row["result_ref"]) if row else None

    async def put_request(self, customer_id, actor_id, key, digest, task_id, operation="submit"):
        self._check_customer(customer_id, actor_id)
        await self.connection.execute("""INSERT INTO ra_request_keys
            (customer_id, actor_id, operation, key, request_digest, result_ref) VALUES ($1,$2,$3,$4,$5,$6)""",
                                      customer_id, actor_id, operation, key, digest, task_id)

    async def insert_task(self, task):
        self._check_customer(task.customer_id, task.actor_id, task.actor_kind)
        await self.connection.execute("""INSERT INTO ra_tasks
            (customer_id,task_id,actor_kind,actor_id,goal_text,state,reason_code,version,unit_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""", task.customer_id, task.task_id, task.actor_kind,
                                      task.actor_id, task.goal_text, task.state, task.reason_code, task.version, task.unit_id)
        self.loaded_versions[task.task_id] = task.version

    async def load_task(self, task_id, *, for_update=False):
        values = [task_id]
        scope = ""
        if self.context is not None:
            scope = " AND customer_id=$2 AND actor_id=$3 AND actor_kind=$4"
            values += [self.context.customer_id, self.context.actor_id, self.context.actor_kind]
        row = await self.connection.fetchrow("SELECT * FROM ra_tasks WHERE task_id=$1" + scope
                                              + (" FOR UPDATE" if for_update else ""), *values)
        if row is None:
            raise NotFoundError("object not found")
        plan, snapshot = None, None
        if row["active_plan_id"]:
            record = await self.connection.fetchrow("SELECT * FROM ra_plans WHERE customer_id=$1 AND plan_id=$2",
                                                   row["customer_id"], row["active_plan_id"])
            snapshot = _snapshot(json.loads(record["snapshot"]))
            steps = tuple(PlanStep(item["step_id"], item["action"], item["args"], tuple(item["depends_on"]),
                                   tuple(item["source_refs"])) for item in json.loads(record["steps"]))
            plan = Plan(record["plan_id"], record["revision"], steps, snapshot.snapshot_id)
        records = await self.connection.fetch("SELECT * FROM ra_commands WHERE customer_id=$1 AND task_id=$2"
                                               " ORDER BY created_at,command_id", row["customer_id"], task_id)
        commands = tuple(Command(item["command_id"], item["task_id"], item["customer_id"], item["unit_id"],
                                 item["plan_id"], item["step_id"], item["binding_id"], item["wire_skill"],
                                 json.loads(item["wire_args"]), item["dispatch_generation"], item["dispatch_state"],
                                 item["outcome"], item["evidence_conflict"], tuple(item["evidence_ids"]), item["owner_run_id"]) for item in records)
        self.loaded_versions[task_id] = row["version"]
        return Task(task_id, row["customer_id"], row["actor_kind"], row["actor_id"], row["goal_text"], row["state"],
                    row["reason_code"], row["version"], plan, snapshot, row["unit_id"], commands,
                    tuple(row["completed"]), row["clarification"], row["result_disputed"], row["clarification_id"], tuple(row["answers"]))

    async def load_facts(self, task_id, *, for_update):
        return await self.load_task(task_id, for_update=for_update)

    async def save_task(self, task, *, expected_version=None):
        self._check_customer(task.customer_id, task.actor_id, task.actor_kind)
        expected = expected_version if expected_version is not None else self.loaded_versions.get(task.task_id)
        if expected is None or task.version != expected + 1:
            raise ConflictError("expected task version and its successor are required")
        # Nested savepoint also protects callers which catch a domain conflict inside their transaction.
        async with self.connection.transaction():
            if task.plan:
                stored = await self.connection.fetchrow("SELECT * FROM ra_plans WHERE customer_id=$1 AND plan_id=$2",
                                                        task.customer_id, task.plan.plan_id)
                steps_json, snapshot_json = _json([asdict(step) for step in task.plan.steps]), _json(asdict(task.snapshot))
                if stored and (stored["task_id"] != task.task_id or stored["revision"] != task.plan.revision
                               or json.loads(stored["steps"]) != json.loads(steps_json)
                               or json.loads(stored["snapshot"]) != json.loads(snapshot_json)):
                    raise ConflictError("plan version is immutable")
            status = await self.connection.execute("""UPDATE ra_tasks SET state=$1,reason_code=$2,active_plan_id=$3,
                completed=$4,clarification=$5,result_disputed=$6,version=$7,updated_at=clock_timestamp()
                ,clarification_id=$11,answers=$12 WHERE customer_id=$8 AND task_id=$9 AND version=$10""", task.state, task.reason_code,
                task.plan.plan_id if task.plan else None, list(task.completed), task.clarification, task.result_disputed,
                task.version, task.customer_id, task.task_id, expected, task.clarification_id, list(task.answers))
            if status != "UPDATE 1":
                raise ConflictError("task version changed")
            if task.plan:
                if not stored:
                    await self.connection.execute("""INSERT INTO ra_plans
                        (customer_id,plan_id,task_id,revision,steps,snapshot) VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb)""",
                        task.customer_id, task.plan.plan_id, task.task_id, task.plan.revision, steps_json, snapshot_json)
                next_step = next((step.step_id for step in task.plan.steps if step.step_id not in task.completed), None)
                await self.connection.execute("""INSERT INTO ra_units
                    (customer_id,unit_id,task_id,plan_id,binding_id,state,next_step_id,version)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (customer_id,unit_id) DO UPDATE
                    SET state=EXCLUDED.state,next_step_id=EXCLUDED.next_step_id,version=EXCLUDED.version
                    WHERE ra_units.version=$9""", task.customer_id, task.unit_id, task.task_id, task.plan.plan_id,
                    task.snapshot.robot_binding_id, task.state, next_step, task.version, expected)
            for command in task.commands:
                await self._save_command(command)
        self.loaded_versions[task.task_id] = task.version

    async def _save_command(self, command):
        original = await self.connection.fetchrow("SELECT * FROM ra_commands WHERE customer_id=$1 AND command_id=$2",
                                                  command.customer_id, command.command_id)
        wire_json = _json(dict(command.wire_args))
        if original:
            immutable = {"task_id": command.task_id, "unit_id": command.unit_id, "plan_id": command.plan_id,
                         "step_id": command.step_id, "binding_id": command.robot_binding_id,
                         "wire_skill": command.wire_skill}
            if any(original[key] != value for key, value in immutable.items()) or json.loads(original["wire_args"]) != dict(command.wire_args):
                raise ConflictError("command wire content is immutable")
            if (original["dispatch_generation"], original["owner_run_id"]) != (command.dispatch_generation, command.owner_run_id):
                if original["dispatch_state"] != "PREPARED" or command.dispatch_state != "DISPATCHING":
                    raise ConflictError("attempt ownership is immutable")
            await self.connection.execute("""UPDATE ra_commands SET dispatch_state=$1,outcome=$2,evidence_conflict=$3,
                evidence_ids=$4,version=version+1,dispatch_generation=$8,owner_run_id=$9,
                sent_at=CASE WHEN $1='SENT' THEN coalesce(sent_at,clock_timestamp()) ELSE sent_at END,
                finished_at=CASE WHEN $2 IN ('SUCCEEDED','FAILED') THEN coalesce(finished_at,clock_timestamp()) ELSE finished_at END
                WHERE customer_id=$5 AND command_id=$6 AND version=$7""", command.dispatch_state, command.outcome,
                command.evidence_conflict, list(command.evidence_ids), command.customer_id, command.command_id, original["version"],
                command.dispatch_generation, command.owner_run_id)
        else:
            await self.connection.execute("""INSERT INTO ra_commands (customer_id,command_id,task_id,unit_id,plan_id,
                binding_id,step_id,wire_skill,wire_args,dispatch_generation,dispatch_state,outcome,evidence_conflict,evidence_ids,owner_run_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,$13,$14,$15)""", command.customer_id,
                command.command_id, command.task_id, command.unit_id, command.plan_id, command.robot_binding_id,
                command.step_id, command.wire_skill, wire_json, command.dispatch_generation, command.dispatch_state,
                command.outcome, command.evidence_conflict, list(command.evidence_ids), command.owner_run_id)

    async def apply_changes(self, changes):
        await self.save_task(changes.task)
        if changes.event:
            event = changes.event
            await self.connection.execute("""INSERT INTO ra_robot_events (customer_id,event_id,binding_id,
                connection_generation,cmd_id,event_type,received_at,payload) VALUES ($1,$2,$3,$4,$5,'cmd_status',$6,$7::jsonb)
                ON CONFLICT (customer_id,event_id) DO NOTHING""", event.customer_id, event.event_id,
                event.robot_binding_id, event.connection_generation, event.cmd_id, event.received_at,
                _json({"state": event.state, **dict(event.details)}))
        for decision in changes.decisions:
            if decision.kind == "approval":
                detail = decision.detail
                await self.connection.execute("""INSERT INTO ra_decisions (customer_id,decision_id,task_id,plan_id,kind,result,
                    rule_or_policy_version,evidence_refs,detail,plan_revision,scope,approver_id,policy_version,issued_at,expires_at)
                    VALUES ($1,$2,$3,$4,'approval',$5,$6,$7,$8::jsonb,$9,$10,$11,$12,$13,$14)""",
                    changes.task.customer_id, decision.decision_id, changes.task.task_id, changes.task.plan.plan_id,
                    decision.result, decision.rule_or_policy_version, list(decision.evidence_refs), _json(dict(detail)),
                    detail["plan_revision"], list(detail["scope"]), detail["approver_id"], detail["policy_version"],
                    detail["issued_at"], detail["expires_at"])
                continue
            await self.connection.execute("""INSERT INTO ra_decisions (customer_id,decision_id,task_id,plan_id,kind,result,
                rule_or_policy_version,evidence_refs,detail) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)""",
                changes.task.customer_id, decision.decision_id, changes.task.task_id,
                changes.task.plan.plan_id if changes.task.plan else None, decision.kind, decision.result,
                decision.rule_or_policy_version, list(decision.evidence_refs), _json(dict(decision.detail)))

    async def event_seen(self, event):
        row = await self.connection.fetchrow("SELECT * FROM ra_robot_events WHERE customer_id=$1 AND event_id=$2",
                                             event.customer_id, event.event_id)
        if row is None:
            return False
        if (row["cmd_id"], row["binding_id"], row["connection_generation"], json.loads(row["payload"]).get("state")) != (
                event.cmd_id, event.robot_binding_id, event.connection_generation, event.state):
            raise ConflictError("event ID reused with different content")
        return True

    async def append_event(self, task_id, value):
        task = await self.load_task(task_id, for_update=True)
        await self.connection.execute("""INSERT INTO ra_task_events (customer_id,task_id,seq,task_version,event_type,public_payload)
            SELECT $1,$2,coalesce(max(seq),0)+1,$3,$4,$5::jsonb FROM ra_task_events WHERE customer_id=$1 AND task_id=$2""",
            task.customer_id, task_id, task.version, value.event_type, _json(dict(value.public_payload)))

    async def approvals(self, task):
        rows = await self.connection.fetch("""SELECT * FROM ra_decisions WHERE customer_id=$1 AND task_id=$2
            AND plan_id=$3 AND kind='approval' AND result='approve'""", task.customer_id, task.task_id, task.plan.plan_id)
        return [{"customer_id": row["customer_id"], "plan_id": row["plan_id"], "plan_revision": row["plan_revision"],
                 "scope": row["scope"], "policy_version": row["policy_version"], "issued_at": row["issued_at"],
                 "expires_at": row["expires_at"], "revoked_at": row["revoked_at"]} for row in rows]

    async def get_input(self, context, key):
        self._check_customer(context.customer_id, context.actor_id)
        digest = sha256(_json([context.customer_id, context.actor_id, "audio", key]).encode()).digest()
        await self.connection.execute("SELECT pg_advisory_xact_lock($1)", int.from_bytes(digest[:8], "big", signed=True))
        row = await self.connection.fetchrow("SELECT * FROM ra_inputs WHERE customer_id=$1 AND actor_id=$2 AND operation='audio' AND key=$3",
                                             context.customer_id, context.actor_id, key)
        return dict(row) if row else None

    async def insert_input(self, context, key, input_id, digest):
        await self.connection.execute("""INSERT INTO ra_inputs (customer_id,actor_id,operation,key,input_id,request_digest,state)
            VALUES ($1,$2,'audio',$3,$4,$5,'PROCESSING')""", context.customer_id, context.actor_id, key, input_id, digest)

    async def finish_input(self, context, key, text, provider_ref, task_id=None):
        await self.connection.execute("""UPDATE ra_inputs SET normalized_text=$1,provider_ref=$2,result_task_id=$3,
            state=CASE WHEN $3::text IS NULL THEN 'TRANSCRIBED' ELSE 'COMPLETE' END
            WHERE customer_id=$4 AND actor_id=$5 AND operation='audio' AND key=$6""",
            text, provider_ref, task_id, context.customer_id, context.actor_id, key)

    async def fail_input(self, context, key):
        await self.connection.execute("UPDATE ra_inputs SET state='FAILED' WHERE customer_id=$1 AND actor_id=$2 AND operation='audio' AND key=$3",
                                      context.customer_id, context.actor_id, key)

    async def task_events(self, task_id, after, limit):
        task = await self.load_task(task_id)
        rows = await self.connection.fetch("""SELECT seq,task_version,event_type,public_payload FROM ra_task_events
            WHERE customer_id=$1 AND task_id=$2 AND seq>$3 ORDER BY seq LIMIT $4""", task.customer_id, task_id, after, limit)
        return [{"seq": row["seq"], "task_version": row["task_version"], "event_type": row["event_type"],
                 "public_payload": json.loads(row["public_payload"])} for row in rows]


class SystemTaskTransaction(TaskTransaction):
    """Private execution boundary; no HTTP handler accepts this context."""

    async def list_tasks(self):
        task_ids = await self.connection.fetch("SELECT task_id FROM ra_tasks ORDER BY queue_seq")
        return [await self.load_task(row["task_id"]) for row in task_ids]

    async def find_event_task(self, event):
        task_id = await self.connection.fetchval("SELECT task_id FROM ra_commands WHERE customer_id=$1 AND command_id=$2",
                                                  event.customer_id, event.cmd_id)
        return await self.load_task(task_id, for_update=True) if task_id else None

    async def claim_slot(self, deployment_id, task):
        if deployment_id != self.deployment_id:
            raise ConflictError("deployment differs from configured store")
        guard = await self.connection.fetchrow("SELECT * FROM ra_execution_guard WHERE deployment_id=$1", deployment_id)
        if guard["task_id"] not in (None, task.task_id):
            return False
        await self.connection.execute("""UPDATE ra_execution_guard SET customer_id=$1,task_id=$2,unit_id=$3,binding_id=$4,
            state='ACTIVE' WHERE deployment_id=$5""", task.customer_id, task.task_id, task.unit_id,
            task.snapshot.robot_binding_id, deployment_id)
        return True

    async def get_binding(self, customer_id, binding_id):
        row = await self.connection.fetchrow("SELECT * FROM ra_robot_bindings WHERE customer_id=$1 AND binding_id=$2",
                                             customer_id, binding_id)
        return dict(row) if row else None

    async def provision_binding(self, value):
        existing = await self.get_binding(value["customer_id"], value["binding_id"])
        if existing:
            if existing["wire_robot_id"] != value["wire_robot_id"] or set(existing["allowed_capabilities"]) != set(value["allowed_actions"]):
                raise ConflictError("binding changes require explicit maintenance, startup does not overwrite")
            return
        await self.connection.execute("""INSERT INTO ra_robot_bindings
            (customer_id,binding_id,wire_robot_id,credential_ref,allowed_capabilities,enabled)
            VALUES ($1,$2,$3,$4,$5,true)""", value["customer_id"], value["binding_id"], value["wire_robot_id"],
            value["token_env"], list(value["allowed_actions"]))

    async def customer_bindings(self, customer_id):
        rows = await self.connection.fetch("SELECT * FROM ra_robot_bindings WHERE customer_id=$1 AND enabled=true ORDER BY binding_id",
                                           customer_id)
        return [dict(row) for row in rows]

    async def put_resource(self, context, binding_id, kind, entries, source_refs):
        revision = await self.connection.fetchval("SELECT coalesce(max(revision),0)+1 FROM ra_resource_versions"
                                                   " WHERE customer_id=$1 AND resource_id=$2 AND kind=$3",
                                                   context.customer_id, binding_id, kind)
        await self.connection.execute("""INSERT INTO ra_resource_versions
            (customer_id,kind,resource_id,revision,payload,source_refs,created_by) VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7)""",
            context.customer_id, kind, binding_id, revision, _json(entries), list(source_refs), context.actor_id)
        await self.connection.execute("""INSERT INTO ra_resource_heads (customer_id,binding_id,kind,revision)
            VALUES ($1,$2,$3,$4) ON CONFLICT (customer_id,binding_id,kind) DO UPDATE SET revision=EXCLUDED.revision""",
            context.customer_id, binding_id, kind, revision)
        return {"binding_id": binding_id, "kind": kind, "revision": revision, "entries": dict(entries)}

    async def current_resource(self, customer_id, binding_id, kind):
        row = await self.connection.fetchrow("""SELECT v.* FROM ra_resource_versions v JOIN ra_resource_heads h
            ON v.customer_id=h.customer_id AND v.resource_id=h.binding_id AND v.kind=h.kind AND v.revision=h.revision
            WHERE h.customer_id=$1 AND h.binding_id=$2 AND h.kind=$3""", customer_id, binding_id, kind)
        return {"revision": row["revision"], "entries": json.loads(row["payload"])} if row else None

    async def activate_binding(self, binding, frame):
        generation = binding["connection_generation"] + 1
        observations = {key: {"value": value, "received_at": datetime.now(timezone.utc).isoformat(),
                              "connection_generation": generation}
                        for key, value in frame.items() if key not in {"type", "robot_id"}}
        observations["connected"] = {"value": True, "connection_generation": generation,
                                     "received_at": datetime.now(timezone.utc).isoformat()}
        await self.connection.execute("""UPDATE ra_robot_bindings SET connection_generation=$1,observations=$2::jsonb,
            version=version+1 WHERE customer_id=$3 AND binding_id=$4 AND version=$5""", generation, _json(observations),
            binding["customer_id"], binding["binding_id"], binding["version"])
        return generation

    async def update_observations(self, binding, generation, frame):
        stored = await self.get_binding(binding["customer_id"], binding["binding_id"])
        if stored is None or stored["connection_generation"] != generation:
            return
        observations = json.loads(stored["observations"])
        instant = datetime.now(timezone.utc).isoformat()
        for key, value in frame.items():
            if key not in {"type", "robot_id"}:
                observations[key] = {"value": value, "received_at": instant, "connection_generation": generation}
        await self.connection.execute("""UPDATE ra_robot_bindings SET observations=$1::jsonb,version=version+1
            WHERE customer_id=$2 AND binding_id=$3 AND connection_generation=$4""", _json(observations),
            binding["customer_id"], binding["binding_id"], generation)

    async def isolate_binding(self, binding, reason, evidence_id):
        await self.connection.execute("""UPDATE ra_robot_bindings SET isolation_reason=$1,
            isolation_evidence_refs=array_append(isolation_evidence_refs,$2),version=version+1
            WHERE customer_id=$3 AND binding_id=$4""", reason, evidence_id, binding["customer_id"], binding["binding_id"])

    async def tasks_for_binding(self, binding):
        rows = await self.connection.fetch("""SELECT t.task_id FROM ra_tasks t JOIN ra_units u
            ON t.customer_id=u.customer_id AND t.task_id=u.task_id
            WHERE u.customer_id=$1 AND u.binding_id=$2 ORDER BY t.task_id FOR UPDATE OF t""",
            binding["customer_id"], binding["binding_id"])
        return [await self.load_task(row["task_id"]) for row in rows]

    async def record_diagnostic(self, binding, generation, event_id, frame):
        await self.connection.execute("""INSERT INTO ra_robot_events (customer_id,event_id,binding_id,
            connection_generation,event_type,received_at,payload) VALUES ($1,$2,$3,$4,'diagnostic',clock_timestamp(),$5::jsonb)
            ON CONFLICT (customer_id,event_id) DO NOTHING""", binding["customer_id"], event_id, binding["binding_id"],
            generation, _json(frame))

    async def release_slot(self, task_id):
        await self.connection.execute("""UPDATE ra_execution_guard SET customer_id=NULL,task_id=NULL,unit_id=NULL,
            binding_id=NULL,owner_run_id=NULL,state='IDLE' WHERE deployment_id=$1 AND task_id=$2""",
                                      self.deployment_id, task_id)

"""Short PostgreSQL transactions; state decisions belong to the application/domain."""

from contextlib import asynccontextmanager
from dataclasses import asdict
from hashlib import sha256
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
import sys
from uuid import uuid4
import asyncpg

from robot_agent.domain.models import (
    Command, ConflictError, ContextSnapshot, NotFoundError, Plan, PlanStep, RobotAllocation, Task,
)
from robot_agent.domain.errors import make_error


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      default=lambda item: item.isoformat() if isinstance(item, datetime)
                      else sorted(item) if isinstance(item, (set, frozenset)) else item)


def _snapshot(value):
    return ContextSnapshot(**{**value, "skills": frozenset(value["skills"]),
                              "allowed_actions": frozenset(value["allowed_actions"]),
                              "valid_until": datetime.fromisoformat(value["valid_until"]) if value.get("valid_until") else None})


def _record_observations(observations, generation, frame):
    previous_map = observations.get("map", {}).get("value")
    map_name = frame.get("map", previous_map)
    if map_name != previous_map and "waypoint_names" in observations:
        # A map-only change invalidates the old list, even if the robot switches back.
        observations["waypoint_names"]["reported_map"] = None
    instant = datetime.now(timezone.utc).isoformat()
    for key, value in frame.items():
        if key not in {"type", "robot_id"}:
            evidence = {"value": value, "received_at": instant, "connection_generation": generation}
            if key == "waypoint_names":
                evidence["reported_map"] = map_name
            observations[key] = evidence


class PostgresStore:
    def __init__(self, pool, *, deployment_id):
        self.pool = pool
        self.deployment_id = deployment_id
        self.write_available = True

    @asynccontextmanager
    async def unit_of_work(self, context):
        if context is None:
            raise ValueError("customer transactions require TrustedContext")
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                yield TaskTransaction(connection, context, self.deployment_id)
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError, asyncpg.InternalClientError) as exc:
            self._storage_failed(exc)
            raise

    @asynccontextmanager
    async def read_snapshot(self, context):
        """A coherent UI projection without holding locks across multiple tasks."""
        if context is None:
            raise ValueError("customer read snapshots require TrustedContext")
        try:
            async with self.pool.acquire() as connection, connection.transaction(isolation="repeatable_read", readonly=True):
                yield TaskTransaction(connection, context, self.deployment_id, read_only=True)
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError, asyncpg.InternalClientError) as exc:
            self._storage_failed(exc)
            raise

    @asynccontextmanager
    async def system_unit_of_work(self):
        # Serialize only short allocation/state transactions, never model or socket IO.
        # There need not be an allocation row yet; locking a missing row is not a lock.
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(current_schema() || ':ra-allocation', 0))")
                yield SystemTaskTransaction(connection, None, self.deployment_id)
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError, asyncpg.InternalClientError) as exc:
            self._storage_failed(exc)
            raise

    def _storage_failed(self, exc):
        # Authorization errors raised by a business operation inside the context
        # also inherit OSError; they are not evidence of a broken database.
        if isinstance(exc, PermissionError):
            return
        self.write_available = False
        logging.getLogger(__name__).error("storage access failed: type=%s sqlstate=%s",
                                         type(exc).__name__, getattr(exc, "sqlstate", ""))

    async def conversation_messages(self, customer_id, conversation_id):
        """Read the durable chat history for one customer conversation."""
        async with self.pool.acquire() as connection:
            rows = await connection.fetch("""
                SELECT message_id, turn_id, seq, role, message_kind, content, created_at
                FROM ra_task_messages
                WHERE customer_id=$1 AND task_id=$2
                ORDER BY seq
            """, customer_id, conversation_id)
        return [{"message_id": row["message_id"], "turn_id": row["turn_id"],
                 "seq": row["seq"], "role": row["role"],
                 "message_kind": row["message_kind"], "content": row["content"],
                 "created_at": row["created_at"].isoformat()} for row in rows]

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


# Read scope shared by history, home and individual task visibility. Robot
# ownership is the private customer space, independent of its live allocation.
_TASK_READ_SCOPE = """t.customer_id=$1 AND (
    (t.actor_kind=$2 AND t.actor_id=$3)
    OR ($2='user' AND t.actor_kind='robot' AND EXISTS (
        SELECT 1 FROM ra_robot_bindings b
        WHERE b.customer_id=t.customer_id AND b.binding_id=t.actor_id
    ))
)"""


class TaskTransaction:
    def __init__(self, connection, context, deployment_id, *, read_only=False):
        self.connection = connection
        self.context = context
        self.deployment_id = deployment_id
        self.loaded_versions = {}
        self.read_only = read_only

    async def task_allocation_binding(self, task):
        self._check_customer(task.customer_id)
        return await self.connection.fetchval("""SELECT binding_id FROM ra_execution_guard
            WHERE deployment_id=$1 AND customer_id=$2 AND task_id=$3""",
            self.deployment_id, task.customer_id, task.task_id)

    async def task_visible(self, task_id):
        return await self.connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM ra_tasks t WHERE " + _TASK_READ_SCOPE
            + " AND (t.task_id=$4 OR t.conversation_id=$4))",
            self.context.customer_id, self.context.actor_kind, self.context.actor_id, task_id)

    async def conversation_task(self, conversation_id, *, for_update=False, actor_scope=True):
        """Load the newest execution turn behind a public conversation id."""
        if self.read_only and for_update:
            raise ValueError("read snapshots cannot lock a task")
        if for_update:
            if self.context is None:
                await self.connection.execute(
                    "SELECT 1 FROM ra_conversations WHERE conversation_id=$1 FOR UPDATE",
                    conversation_id)
            else:
                await self.connection.execute(
                    "SELECT 1 FROM ra_conversations WHERE customer_id=$1 AND conversation_id=$2 FOR UPDATE",
                    self.context.customer_id, conversation_id)
        values = [conversation_id]
        scope = ""
        if self.context is not None:
            scope = " AND customer_id=$2"
            values.append(self.context.customer_id)
            if actor_scope:
                scope += " AND actor_id=$3 AND actor_kind=$4"
                values.extend([self.context.actor_id, self.context.actor_kind])
        lock = "" if self.read_only else " FOR UPDATE" if for_update else " FOR SHARE"
        row = await self.connection.fetchrow(
            "SELECT task_id FROM ra_tasks WHERE conversation_id=$1" + scope
            + " ORDER BY turn_seq DESC LIMIT 1" + lock, *values)
        if row is None:
            raise NotFoundError("object not found")
        return await self.load_task(row["task_id"], for_update=for_update, actor_scope=actor_scope)

    async def public_task(self, identifier, *, for_update=False, actor_scope=True):
        """Resolve either a public conversation id or an internal turn id."""
        if self.read_only and for_update:
            raise ValueError("read snapshots cannot lock a task")
        values = [identifier]
        scope = ""
        if self.context is not None:
            scope = " AND customer_id=$2"
            values.append(self.context.customer_id)
            if actor_scope:
                scope += " AND actor_id=$3 AND actor_kind=$4"
                values.extend([self.context.actor_id, self.context.actor_kind])
        lock = "" if self.read_only else " FOR UPDATE" if for_update else " FOR SHARE"
        row = await self.connection.fetchrow(
            "SELECT task_id FROM ra_tasks WHERE (task_id=$1 OR conversation_id=$1)" + scope
            + " ORDER BY CASE WHEN task_id=$1 THEN 0 ELSE 1 END, turn_seq DESC LIMIT 1" + lock,
            *values)
        if row is None:
            raise NotFoundError("object not found")
        return await self.load_task(row["task_id"], for_update=for_update, actor_scope=actor_scope)

    async def conversation_messages(self, conversation_id, customer_id=None):
        if self.context is not None:
            self._check_customer(self.context.customer_id)
            customer_id = self.context.customer_id
        elif customer_id is None:
            customer_id = await self.connection.fetchval(
                "SELECT customer_id FROM ra_conversations WHERE conversation_id=$1 LIMIT 1", conversation_id)
            if customer_id is None:
                return []
        rows = await self.connection.fetch("""
            SELECT message_id, turn_id, seq, role, message_kind, content, created_at
            FROM ra_task_messages
            WHERE customer_id=$1 AND task_id=$2
            ORDER BY seq
        """, customer_id, conversation_id)
        return [{"message_id": row["message_id"], "turn_id": row["turn_id"],
                 "seq": row["seq"], "role": row["role"],
                 "message_kind": row["message_kind"], "content": row["content"],
                 "created_at": row["created_at"].isoformat()} for row in rows]

    async def create_conversation(self, customer_id, conversation_id, actor_kind, actor_id):
        await self.connection.execute("""
            INSERT INTO ra_conversations(customer_id,conversation_id,actor_kind,actor_id)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (customer_id,conversation_id) DO NOTHING
        """, customer_id, conversation_id, actor_kind, actor_id)

    async def append_message(self, customer_id, conversation_id, turn_id, role, message_kind, content,
                             request_key="", message_id=None):
        if not content or not content.strip():
            raise ValueError("message content must not be empty")
        message_id = message_id or str(uuid4())
        await self.connection.execute("""
            SELECT 1 FROM ra_conversations
            WHERE customer_id=$1 AND conversation_id=$2
            FOR UPDATE
        """, customer_id, conversation_id)
        seq = await self.connection.fetchval("""
            SELECT coalesce(max(seq),0)+1 FROM ra_task_messages
            WHERE customer_id=$1 AND task_id=$2
        """, customer_id, conversation_id)
        await self.connection.execute("""
            INSERT INTO ra_task_messages(customer_id,task_id,message_id,turn_id,seq,role,message_kind,content,request_key)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """, customer_id, conversation_id, message_id, turn_id, seq, role, message_kind, content, request_key)
        await self.connection.execute("""
            UPDATE ra_conversations SET updated_at=clock_timestamp()
            WHERE customer_id=$1 AND conversation_id=$2
        """, customer_id, conversation_id)
        return message_id

    async def start_turn(self, task, text, *, message_id=None):
        """Create a new execution row for a later message in the same chat."""
        conversation_id = task.conversation_id or task.task_id
        turn_seq = await self.connection.fetchval("""
            SELECT coalesce(max(turn_seq),0)+1 FROM ra_tasks
            WHERE customer_id=$1 AND conversation_id=$2
        """, task.customer_id, conversation_id)
        turn_id = str(uuid4())
        next_task = Task(turn_id, task.customer_id, task.actor_kind, task.actor_id, text,
                         state="RECEIVED", unit_id=str(uuid4()), conversation_id=conversation_id,
                         turn_seq=turn_seq)
        await self.insert_task(next_task)
        await self.append_message(task.customer_id, conversation_id, turn_id, "user", "user_text", text,
                                  message_id=message_id)
        await self.connection.execute("""
            UPDATE ra_conversations SET updated_at=clock_timestamp()
            WHERE customer_id=$1 AND conversation_id=$2
        """, task.customer_id, conversation_id)
        return next_task

    async def owner_history(self, limit, before):
        # Count and page the same visible rows at one PostgreSQL read snapshot.
        result = await self.connection.fetchrow("""
            WITH latest AS NOT MATERIALIZED (
                SELECT DISTINCT ON (t.customer_id, t.conversation_id) t.*
                FROM ra_tasks t
                WHERE """ + _TASK_READ_SCOPE + """
                ORDER BY t.customer_id, t.conversation_id, t.turn_seq DESC
            ), owned AS NOT MATERIALIZED (
                SELECT t.task_id AS turn_id, t.conversation_id AS task_id,
                       t.goal_text, t.state, t.reason_code, t.version, t.created_at, t.updated_at, t.queue_seq,
                       p.snapshot->>'robot_binding_id' AS plan_robot_binding_id,
                       g.binding_id AS allocation_robot_binding_id
                FROM latest t LEFT JOIN ra_plans p ON p.customer_id=t.customer_id
                    AND p.task_id=t.task_id AND p.plan_id=t.active_plan_id
                LEFT JOIN ra_execution_guard g ON g.customer_id=t.customer_id AND g.task_id=t.task_id AND g.deployment_id=$6
            ), page AS (
                SELECT * FROM owned WHERE $4::bigint IS NULL OR queue_seq < $4
                ORDER BY queue_seq DESC LIMIT $5
            ), counts AS (
                SELECT state, count(*) AS n FROM owned GROUP BY state
            )
            SELECT (SELECT COALESCE(json_agg(page ORDER BY queue_seq DESC), '[]') FROM page) AS tasks,
                   (SELECT COALESCE(json_object_agg(state, n), '{}') FROM counts) AS state_counts""",
            self.context.customer_id, self.context.actor_kind, self.context.actor_id, before, limit, self.deployment_id)
        return json.loads(result["tasks"]), json.loads(result["state_counts"])

    async def home_task_rows(self):
        # Active visible rows are independent of the history cursor.
        return await self.connection.fetch("""
            WITH latest AS NOT MATERIALIZED (
                SELECT DISTINCT ON (t.customer_id, t.conversation_id) t.*
                FROM ra_tasks t
                WHERE """ + _TASK_READ_SCOPE + """
                ORDER BY t.customer_id, t.conversation_id, t.turn_seq DESC
            )
            SELECT t.task_id AS turn_id, t.conversation_id AS task_id, t.goal_text, t.state, t.reason_code,
                   t.version, t.queue_seq, t.created_at,
                   t.clarification, t.active_plan_id AS plan_id, t.completed,
                   p.snapshot->>'robot_binding_id' AS binding_id,
                   p.snapshot->'capability_labels' AS labels, p.steps,
                   (SELECT json_agg(json_build_object('step_id', c.step_id, 'outcome', c.outcome,
                              'dispatch_state', c.dispatch_state, 'robot_binding_id', c.binding_id)
                              ORDER BY c.created_at, c.command_id)
                    FROM ra_commands c WHERE c.customer_id=t.customer_id AND c.task_id=t.task_id
                    AND c.plan_id=t.active_plan_id) AS commands
            FROM latest t LEFT JOIN ra_plans p ON p.customer_id=t.customer_id
                AND p.task_id=t.task_id AND p.plan_id=t.active_plan_id
            WHERE (t.state IN ('NEEDS_CLARIFICATION','WAITING_APPROVAL','RUNNING',
                               'CANCEL_REQUESTED','READY','QUEUED','BLOCKED','FAILED')
                   OR t.error IS NOT NULL)
            ORDER BY t.queue_seq""", self.context.customer_id, self.context.actor_kind, self.context.actor_id)

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

    async def find_request(self, customer_id, actor_id, key):
        self._check_customer(customer_id, actor_id)
        row = await self.connection.fetchrow("""SELECT request_digest, result_ref FROM ra_request_keys
            WHERE customer_id=$1 AND actor_id=$2 AND operation='submit' AND key=$3""", customer_id, actor_id, key)
        if row is None:
            # Audio admission uses its durable input_id internally. The original
            # caller only knows its upload key, including when the final input
            # update failed after TaskService.submit had already committed.
            row = await self.connection.fetchrow("""SELECT i.request_digest,
                    coalesce(i.result_task_id,k.result_ref) AS result_ref
                FROM ra_inputs i LEFT JOIN ra_request_keys k
                    ON k.customer_id=i.customer_id AND k.actor_id=i.actor_id
                    AND k.operation='submit' AND k.key=i.input_id
                WHERE i.customer_id=$1 AND i.actor_id=$2 AND i.operation='audio' AND i.key=$3
                    AND coalesce(i.result_task_id,k.result_ref) IS NOT NULL""", customer_id, actor_id, key)
        return (row["request_digest"], row["result_ref"]) if row else None

    async def put_request(self, customer_id, actor_id, key, digest, task_id, operation="submit"):
        self._check_customer(customer_id, actor_id)
        await self.connection.execute("""INSERT INTO ra_request_keys
            (customer_id, actor_id, operation, key, request_digest, result_ref) VALUES ($1,$2,$3,$4,$5,$6)""",
                                      customer_id, actor_id, operation, key, digest, task_id)

    async def insert_task(self, task):
        self._check_customer(task.customer_id, task.actor_id, task.actor_kind)
        conversation_id = task.conversation_id or task.task_id
        turn_seq = task.turn_seq or 1
        await self.create_conversation(task.customer_id, conversation_id, task.actor_kind, task.actor_id)
        await self.connection.execute("""INSERT INTO ra_tasks
            (customer_id,task_id,conversation_id,turn_seq,actor_kind,actor_id,goal_text,state,reason_code,version,unit_id,error,advancement_stopped,cancel_result)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13,$14::jsonb)""", task.customer_id, task.task_id,
                                      conversation_id, turn_seq, task.actor_kind,
                                      task.actor_id, task.goal_text, task.state, task.reason_code, task.version, task.unit_id,
                                      _json(task.error) if task.error else None, task.advancement_stopped,
                                      _json(task.cancel_result) if task.cancel_result else None)
        self.loaded_versions[task.task_id] = task.version

    async def load_task(self, task_id, *, for_update=False, actor_scope=True):
        if self.read_only and for_update:
            raise ValueError("read snapshots cannot lock a task for writing")
        values = [task_id]
        scope = ""
        if self.context is not None:
            scope = " AND customer_id=$2"
            values += [self.context.customer_id]
            if actor_scope:
                scope += " AND actor_id=$3 AND actor_kind=$4"
                values += [self.context.actor_id, self.context.actor_kind]
        lock = "" if self.read_only else " FOR UPDATE" if for_update else " FOR SHARE"
        row = await self.connection.fetchrow("SELECT * FROM ra_tasks WHERE task_id=$1" + scope + lock, *values)
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
        if row["active_plan_id"]:
            records = await self.connection.fetch("SELECT * FROM ra_commands WHERE customer_id=$1 AND task_id=$2 AND plan_id=$3"
                                                   " ORDER BY created_at,command_id", row["customer_id"], task_id,
                                                   row["active_plan_id"])
        else:
            records = ()
        commands = tuple(Command(item["command_id"], item["task_id"], item["customer_id"], item["unit_id"],
                                 item["plan_id"], item["step_id"], item["binding_id"], item["wire_skill"],
                                 json.loads(item["wire_args"]), item["dispatch_generation"], item["dispatch_state"],
                                 item["outcome"], item["evidence_conflict"], tuple(item["evidence_ids"]), item["owner_run_id"]) for item in records)
        self.loaded_versions[task_id] = row["version"]
        return Task(task_id, row["customer_id"], row["actor_kind"], row["actor_id"], row["goal_text"], row["state"],
                    row["reason_code"], row["version"], plan, snapshot, row["unit_id"], commands,
                    tuple(row["completed"]), row["clarification"], row["result_disputed"], row["clarification_id"], tuple(row["answers"]),
                    row["cancel_requested"], row["resume_phase"],
                    self._task_error(row), row["advancement_stopped"],
                    json.loads(row["cancel_result"]) if row["cancel_result"] else None,
                    row["conversation_id"], row["turn_seq"])

    @staticmethod
    def _task_error(row):
        if row["error"]:
            return json.loads(row["error"])
        if row["advancement_stopped"] and row["reason_code"]:
            # Deterministic compatibility projection for historical records. Do
            # not invent a new error id/time each time someone opens the page.
            error = make_error(row["reason_code"])
            return {**error, "error_id": "legacy:" + row["task_id"], "occurred_at": row["updated_at"].isoformat()}
        return None

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
                completed=$4,clarification=$5,result_disputed=$6,cancel_requested=$7,resume_phase=$8,version=$9,updated_at=clock_timestamp(),
                clarification_id=$10,answers=$11,error=$15::jsonb,
                advancement_stopped=advancement_stopped OR $16,cancel_result=$17::jsonb
                WHERE customer_id=$12 AND task_id=$13 AND version=$14
                AND (NOT advancement_stopped OR $16)""", task.state, task.reason_code,
                task.plan.plan_id if task.plan else None, list(task.completed), task.clarification, task.result_disputed,
                task.cancel_requested, task.resume_phase, task.version, task.clarification_id, list(task.answers),
                task.customer_id, task.task_id, expected, _json(task.error) if task.error else None,
                task.advancement_stopped, _json(task.cancel_result) if task.cancel_result else None)
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
                finished_at=CASE WHEN $2 IN ('SUCCEEDED','FAILED') THEN coalesce(finished_at,clock_timestamp()) ELSE finished_at END,
                dispatch_started_at=CASE WHEN $1='DISPATCHING' THEN coalesce(dispatch_started_at,clock_timestamp()) ELSE dispatch_started_at END
                WHERE customer_id=$5 AND command_id=$6 AND version=$7""", command.dispatch_state, command.outcome,
                command.evidence_conflict, list(command.evidence_ids), command.customer_id, command.command_id, original["version"],
                command.dispatch_generation, command.owner_run_id)
        else:
            await self.connection.execute("""INSERT INTO ra_commands (customer_id,command_id,task_id,unit_id,plan_id,
                binding_id,step_id,wire_skill,wire_args,dispatch_generation,dispatch_state,outcome,evidence_conflict,evidence_ids,owner_run_id,dispatch_started_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,$13,$14,$15,CASE WHEN $11='DISPATCHING' THEN clock_timestamp() ELSE NULL END)""", command.customer_id,
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
        task = await self.load_task(task_id, actor_scope=False)
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

    @staticmethod
    def _allocation(row):
        return RobotAllocation(row["customer_id"], row["task_id"], row["binding_id"],
                               _snapshot(json.loads(row["allocation_snapshot"])), row["allocation_id"]) if row else None

    async def allocations(self, customer_id):
        rows = await self.connection.fetch("SELECT * FROM ra_execution_guard WHERE customer_id=$1", customer_id)
        return {row["binding_id"]: self._allocation(row) for row in rows}

    async def task_allocation(self, task):
        row = await self.connection.fetchrow("""SELECT * FROM ra_execution_guard
            WHERE deployment_id=$1 AND customer_id=$2 AND task_id=$3""",
            self.deployment_id, task.customer_id, task.task_id)
        return self._allocation(row)

    async def prior_waiters(self, task):
        rows = await self.connection.fetch("""SELECT task_id FROM ra_tasks WHERE customer_id=$1
            AND state IN ('RECEIVED','QUEUED','READY') AND queue_seq <
            (SELECT queue_seq FROM ra_tasks WHERE customer_id=$1 AND task_id=$2) ORDER BY queue_seq""",
            task.customer_id, task.task_id)
        return [await self.load_task(row["task_id"]) for row in rows]

    async def claim_slot(self, deployment_id, task, snapshot):
        if deployment_id != self.deployment_id or snapshot.customer_id != task.customer_id:
            raise ConflictError("allocation scope differs from task/store")
        allocation_id = str(uuid4())
        status = await self.connection.execute("""INSERT INTO ra_execution_guard
            (customer_id,task_id,unit_id,binding_id,state,allocation_snapshot,deployment_id,allocation_id)
            VALUES ($1,$2,$3,$4,'ACTIVE',$5::jsonb,$6,$7) ON CONFLICT DO NOTHING""",
            task.customer_id, task.task_id, task.unit_id, snapshot.robot_binding_id,
            _json(asdict(snapshot)), deployment_id, allocation_id)
        if status != "INSERT 0 1":
            raise ConflictError("task or robot is already allocated")
        return RobotAllocation(task.customer_id, task.task_id, snapshot.robot_binding_id, snapshot, allocation_id)

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

    async def customer_bindings(self, customer_id, *, include_disabled=False):
        rows = await self.connection.fetch("SELECT * FROM ra_robot_bindings WHERE customer_id=$1 AND ($2 OR enabled=true) ORDER BY binding_id",
                                           customer_id, include_disabled)
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
        await self.connection.execute("UPDATE ra_robot_bindings SET version=version+1 WHERE customer_id=$1 AND binding_id=$2",
                                      context.customer_id, binding_id)
        return {"binding_id": binding_id, "kind": kind, "revision": revision, "entries": dict(entries)}

    async def current_resource(self, customer_id, binding_id, kind):
        row = await self.connection.fetchrow("""SELECT v.* FROM ra_resource_versions v JOIN ra_resource_heads h
            ON v.customer_id=h.customer_id AND v.resource_id=h.binding_id AND v.kind=h.kind AND v.revision=h.revision
            WHERE h.customer_id=$1 AND h.binding_id=$2 AND h.kind=$3""", customer_id, binding_id, kind)
        return {"revision": row["revision"], "entries": json.loads(row["payload"])} if row else None

    async def activate_binding(self, binding, frame):
        generation = binding["connection_generation"] + 1
        observations = {}
        _record_observations(observations, generation, {**frame, "connected": True})
        await self.connection.execute("""UPDATE ra_robot_bindings SET connection_generation=$1,observations=$2::jsonb,
            version=version+1 WHERE customer_id=$3 AND binding_id=$4 AND version=$5""", generation, _json(observations),
            binding["customer_id"], binding["binding_id"], binding["version"])
        return generation

    async def update_observations(self, binding, generation, frame):
        stored = await self.get_binding(binding["customer_id"], binding["binding_id"])
        if stored is None or stored["connection_generation"] != generation:
            return
        observations = json.loads(stored["observations"])
        _record_observations(observations, generation, frame)
        await self.connection.execute("""UPDATE ra_robot_bindings SET observations=$1::jsonb,version=version+1
            WHERE customer_id=$2 AND binding_id=$3 AND connection_generation=$4""", _json(observations),
            binding["customer_id"], binding["binding_id"], generation)

    async def isolate_binding(self, binding, reason, evidence_id):
        await self.connection.execute("""UPDATE ra_robot_bindings SET isolation_reason=$1,
            isolation_evidence_refs=array_append(isolation_evidence_refs,$2),version=version+1
            WHERE customer_id=$3 AND binding_id=$4""", reason, evidence_id, binding["customer_id"], binding["binding_id"])

    async def tasks_for_binding(self, binding):
        rows = await self.connection.fetch("""SELECT t.task_id FROM ra_tasks t WHERE t.customer_id=$1 AND (
            EXISTS (SELECT 1 FROM ra_units u WHERE u.customer_id=t.customer_id AND u.task_id=t.task_id AND u.binding_id=$2)
            OR EXISTS (SELECT 1 FROM ra_execution_guard g WHERE g.customer_id=t.customer_id AND g.task_id=t.task_id AND g.binding_id=$2))
            ORDER BY t.task_id FOR UPDATE OF t""",
            binding["customer_id"], binding["binding_id"])
        return [await self.load_task(row["task_id"]) for row in rows]

    async def record_diagnostic(self, binding, generation, event_id, frame):
        await self.connection.execute("""INSERT INTO ra_robot_events (customer_id,event_id,binding_id,
            connection_generation,event_type,received_at,payload) VALUES ($1,$2,$3,$4,'diagnostic',clock_timestamp(),$5::jsonb)
            ON CONFLICT (customer_id,event_id) DO NOTHING""", binding["customer_id"], event_id, binding["binding_id"],
            generation, _json(frame))

    async def expired_task_ids(self, timeout_seconds):
        rows = await self.connection.fetch("""SELECT DISTINCT task_id FROM ra_commands WHERE dispatch_state IN ('DISPATCHING','SENT')
            AND outcome IN ('NONE','RUNNING') AND dispatch_started_at < clock_timestamp() - $1::double precision * interval '1 second'
            ORDER BY task_id""", timeout_seconds)
        return [row["task_id"] for row in rows]

    async def release_allocation(self, allocation):
        if not allocation.allocation_id:
            raise ValueError("release requires the exact allocation identity")
        await self.connection.execute("""DELETE FROM ra_execution_guard WHERE deployment_id=$1
            AND customer_id=$2 AND task_id=$3 AND binding_id=$4 AND allocation_id=$5""",
            self.deployment_id, allocation.customer_id, allocation.task_id,
            allocation.robot_binding_id, allocation.allocation_id)

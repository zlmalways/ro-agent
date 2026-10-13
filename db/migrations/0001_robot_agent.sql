-- Customer IDs are carried through every business association. Times are UTC instants.
CREATE TABLE IF NOT EXISTS ra_tasks (
    customer_id text NOT NULL,
    task_id text NOT NULL UNIQUE,
    actor_kind text NOT NULL CHECK (actor_kind IN ('user', 'robot')),
    actor_id text NOT NULL,
    goal_text text NOT NULL,
    state text NOT NULL CHECK (state IN ('RECEIVED','PLANNING','NEEDS_CLARIFICATION','WAITING_APPROVAL',
        'READY','QUEUED','RUNNING','BLOCKED','CANCEL_REQUESTED','SUCCEEDED','FAILED','CANCELLED')),
    reason_code text NOT NULL DEFAULT '',
    resume_phase text NOT NULL DEFAULT '',
    active_plan_id text,
    unit_id text NOT NULL,
    clarification text NOT NULL DEFAULT '',
    completed text[] NOT NULL DEFAULT '{}',
    result_disputed boolean NOT NULL DEFAULT false,
    version bigint NOT NULL CHECK (version > 0),
    queue_seq bigserial NOT NULL,
    queued_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (customer_id, task_id)
);
CREATE TABLE IF NOT EXISTS ra_robot_bindings (
    customer_id text NOT NULL,
    binding_id text NOT NULL,
    wire_robot_id text NOT NULL,
    credential_ref text NOT NULL DEFAULT '',
    allowed_capabilities text[] NOT NULL DEFAULT '{}',
    enabled boolean NOT NULL DEFAULT false,
    connection_generation bigint NOT NULL DEFAULT 0,
    isolation_reason text NOT NULL DEFAULT '',
    isolation_evidence_refs text[] NOT NULL DEFAULT '{}',
    observations jsonb NOT NULL DEFAULT '{}',
    version bigint NOT NULL DEFAULT 1,
    PRIMARY KEY (customer_id, binding_id)
);
CREATE TABLE IF NOT EXISTS ra_resource_versions (
    customer_id text NOT NULL,
    kind text NOT NULL,
    resource_id text NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    payload jsonb NOT NULL,
    source_refs text[] NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (customer_id, kind, resource_id, revision)
);
CREATE TABLE IF NOT EXISTS ra_plans (
    customer_id text NOT NULL,
    plan_id text NOT NULL,
    task_id text NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    steps jsonb NOT NULL,
    snapshot jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (customer_id, plan_id),
    UNIQUE (customer_id, task_id, revision),
    UNIQUE (customer_id, task_id, plan_id),
    FOREIGN KEY (customer_id, task_id) REFERENCES ra_tasks (customer_id, task_id)
);
CREATE TABLE IF NOT EXISTS ra_units (
    customer_id text NOT NULL,
    unit_id text NOT NULL,
    task_id text NOT NULL,
    plan_id text NOT NULL,
    binding_id text NOT NULL,
    state text NOT NULL,
    next_step_id text,
    version bigint NOT NULL CHECK (version > 0),
    PRIMARY KEY (customer_id, unit_id),
    UNIQUE (customer_id, unit_id, task_id, binding_id),
    UNIQUE (customer_id, unit_id, task_id, plan_id, binding_id),
    FOREIGN KEY (customer_id, task_id, plan_id) REFERENCES ra_plans (customer_id, task_id, plan_id),
    FOREIGN KEY (customer_id, binding_id) REFERENCES ra_robot_bindings (customer_id, binding_id)
);
CREATE TABLE IF NOT EXISTS ra_commands (
    customer_id text NOT NULL,
    command_id text NOT NULL UNIQUE,
    task_id text NOT NULL,
    unit_id text NOT NULL,
    plan_id text NOT NULL,
    binding_id text NOT NULL,
    step_id text NOT NULL,
    attempt integer NOT NULL DEFAULT 1 CHECK (attempt > 0),
    wire_skill text NOT NULL,
    wire_args jsonb NOT NULL,
    dispatch_generation bigint NOT NULL,
    owner_run_id text NOT NULL DEFAULT '',
    dispatch_state text NOT NULL CHECK (dispatch_state IN ('PREPARED','DISPATCHING','SENT')),
    outcome text NOT NULL CHECK (outcome IN ('NONE','RUNNING','SUCCEEDED','FAILED','UNKNOWN')),
    evidence_conflict boolean NOT NULL DEFAULT false,
    evidence_ids text[] NOT NULL DEFAULT '{}',
    version bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    sent_at timestamptz,
    finished_at timestamptz,
    PRIMARY KEY (customer_id, command_id),
    UNIQUE (customer_id, unit_id, plan_id, step_id, attempt),
    FOREIGN KEY (customer_id, unit_id, task_id, plan_id, binding_id)
        REFERENCES ra_units (customer_id, unit_id, task_id, plan_id, binding_id)
);
CREATE TABLE IF NOT EXISTS ra_decisions (
    customer_id text NOT NULL,
    decision_id text NOT NULL,
    task_id text NOT NULL,
    plan_id text,
    step_id text,
    kind text NOT NULL CHECK (kind IN ('validation','authorization','approval')),
    result text NOT NULL,
    rule_or_policy_version text NOT NULL,
    evidence_refs text[] NOT NULL DEFAULT '{}',
    detail jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (customer_id, decision_id),
    FOREIGN KEY (customer_id, task_id) REFERENCES ra_tasks (customer_id, task_id),
    FOREIGN KEY (customer_id, task_id, plan_id) REFERENCES ra_plans (customer_id, task_id, plan_id)
);
CREATE TABLE IF NOT EXISTS ra_robot_events (
    customer_id text NOT NULL,
    event_id text NOT NULL,
    binding_id text NOT NULL,
    connection_generation bigint NOT NULL,
    cmd_id text,
    event_type text NOT NULL,
    received_at timestamptz NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (customer_id, event_id),
    FOREIGN KEY (customer_id, binding_id) REFERENCES ra_robot_bindings (customer_id, binding_id),
    FOREIGN KEY (customer_id, cmd_id) REFERENCES ra_commands (customer_id, command_id)
);
CREATE TABLE IF NOT EXISTS ra_task_events (
    customer_id text NOT NULL,
    task_id text NOT NULL,
    seq bigint NOT NULL,
    task_version bigint NOT NULL,
    event_type text NOT NULL,
    public_payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (customer_id, task_id, seq),
    FOREIGN KEY (customer_id, task_id) REFERENCES ra_tasks (customer_id, task_id)
);
CREATE TABLE IF NOT EXISTS ra_request_keys (
    customer_id text NOT NULL,
    actor_id text NOT NULL,
    operation text NOT NULL,
    key text NOT NULL,
    request_digest text NOT NULL,
    result_ref text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (customer_id, actor_id, operation, key),
    FOREIGN KEY (customer_id, result_ref) REFERENCES ra_tasks (customer_id, task_id)
);
CREATE TABLE IF NOT EXISTS ra_execution_guard (
    deployment_id text PRIMARY KEY,
    customer_id text,
    task_id text,
    unit_id text,
    binding_id text,
    owner_run_id text,
    state text NOT NULL DEFAULT 'IDLE',
    FOREIGN KEY (customer_id, unit_id, task_id, binding_id)
        REFERENCES ra_units (customer_id, unit_id, task_id, binding_id),
    CHECK ((state = 'IDLE' AND task_id IS NULL AND customer_id IS NULL AND unit_id IS NULL AND binding_id IS NULL)
        OR (state <> 'IDLE' AND task_id IS NOT NULL AND customer_id IS NOT NULL AND unit_id IS NOT NULL
            AND binding_id IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS ra_runtime_owner (
    deployment_id text PRIMARY KEY,
    owner_host_id text NOT NULL,
    owner_run_id text NOT NULL,
    state text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ra_task_queue ON ra_tasks (queue_seq) WHERE state IN ('READY','QUEUED','RUNNING');
CREATE INDEX IF NOT EXISTS ra_command_task ON ra_commands (customer_id, task_id, created_at, command_id);
ALTER TABLE ra_tasks ADD CONSTRAINT ra_active_plan_owner
    FOREIGN KEY (customer_id, task_id, active_plan_id) REFERENCES ra_plans (customer_id, task_id, plan_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE OR REPLACE FUNCTION ra_immutable_record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable record cannot be overwritten' USING ERRCODE = '23514';
END $$;
DROP TRIGGER IF EXISTS ra_immutable_plan ON ra_plans;
CREATE TRIGGER ra_immutable_plan BEFORE UPDATE OR DELETE ON ra_plans
    FOR EACH ROW EXECUTE FUNCTION ra_immutable_record();
DROP TRIGGER IF EXISTS ra_immutable_resource ON ra_resource_versions;
CREATE TRIGGER ra_immutable_resource BEFORE UPDATE OR DELETE ON ra_resource_versions
    FOR EACH ROW EXECUTE FUNCTION ra_immutable_record();
CREATE OR REPLACE FUNCTION ra_command_wire_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.customer_id,NEW.command_id,NEW.task_id,NEW.unit_id,NEW.plan_id,NEW.binding_id,
        NEW.step_id,NEW.attempt,NEW.wire_skill,NEW.wire_args,NEW.dispatch_generation,NEW.owner_run_id)
        IS DISTINCT FROM
        (OLD.customer_id,OLD.command_id,OLD.task_id,OLD.unit_id,OLD.plan_id,OLD.binding_id,
        OLD.step_id,OLD.attempt,OLD.wire_skill,OLD.wire_args,OLD.dispatch_generation,OLD.owner_run_id) THEN
        RAISE EXCEPTION 'command wire content is immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER ra_immutable_command BEFORE UPDATE ON ra_commands
    FOR EACH ROW EXECUTE FUNCTION ra_command_wire_immutable();

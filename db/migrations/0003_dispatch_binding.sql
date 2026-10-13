-- The connection and runtime are bound on the first DISPATCHING attempt.
-- A PREPARED row has no physical effects; ownership can be refreshed once before that attempt.
CREATE OR REPLACE FUNCTION ra_command_wire_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.customer_id,NEW.command_id,NEW.task_id,NEW.unit_id,NEW.plan_id,NEW.binding_id,
        NEW.step_id,NEW.attempt,NEW.wire_skill,NEW.wire_args)
        IS DISTINCT FROM
        (OLD.customer_id,OLD.command_id,OLD.task_id,OLD.unit_id,OLD.plan_id,OLD.binding_id,
        OLD.step_id,OLD.attempt,OLD.wire_skill,OLD.wire_args) THEN
        RAISE EXCEPTION 'command wire content is immutable' USING ERRCODE = '23514';
    END IF;
    IF (NEW.dispatch_generation,NEW.owner_run_id) IS DISTINCT FROM (OLD.dispatch_generation,OLD.owner_run_id)
        AND NOT (OLD.dispatch_state='PREPARED' AND NEW.dispatch_state='DISPATCHING') THEN
        RAISE EXCEPTION 'attempt ownership is immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;

-- Additive task error evidence. Execution facts stay exclusively in ra_commands.
ALTER TABLE ra_tasks ADD COLUMN error jsonb;
ALTER TABLE ra_tasks ADD COLUMN advancement_stopped boolean NOT NULL DEFAULT false;
ALTER TABLE ra_tasks ADD COLUMN cancel_result jsonb;

-- Old failed/blocked tasks must also stay stopped after this deployment. Keep
-- their original reason_code and evidence; the service projects a safe message.
UPDATE ra_tasks SET advancement_stopped=true
WHERE state IN ('BLOCKED','FAILED')
   OR (state IN ('CANCELLED','CANCEL_REQUESTED')
       AND reason_code IN ('RESULT_UNKNOWN','EVIDENCE_CONFLICT','UNTRACKED_EXECUTION',
                           'COMMAND_TIMEOUT','ACTION_FAILED','RESTART_TASK_LOST','ROBOT_DISABLED'));

ALTER TABLE ra_commands DROP CONSTRAINT ra_commands_dispatch_state_check;
ALTER TABLE ra_commands ADD CONSTRAINT ra_commands_dispatch_state_check
    CHECK (dispatch_state IN ('PREPARED','DISPATCHING','SENT','NOT_SENT'));

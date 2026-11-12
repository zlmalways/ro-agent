-- Preserve the plan while stopping its current action. Delivery of a stop
-- request is recorded separately from the robot's outcome in ra_commands.
ALTER TABLE ra_tasks ADD COLUMN pause_step_id text NOT NULL DEFAULT '';
ALTER TABLE ra_tasks ADD COLUMN pause_command_id text NOT NULL DEFAULT '';
ALTER TABLE ra_tasks ADD COLUMN pause_result jsonb;
ALTER TABLE ra_tasks ADD COLUMN skipped_steps text[] NOT NULL DEFAULT '{}';

ALTER TABLE ra_tasks DROP CONSTRAINT ra_tasks_state_check;
ALTER TABLE ra_tasks ADD CONSTRAINT ra_tasks_state_check CHECK (state IN (
    'RECEIVED','PLANNING','NEEDS_CLARIFICATION','WAITING_APPROVAL',
    'READY','QUEUED','RUNNING','BLOCKED','CANCEL_REQUESTED',
    'SUCCEEDED','FAILED','CANCELLED',
    'PAUSE_REQUESTED','PAUSED','COMPLETED_WITH_SKIPS'
));

ALTER TABLE ra_tasks ADD CONSTRAINT ra_tasks_pause_target_check
    CHECK (pause_command_id = '' OR pause_step_id <> '');
ALTER TABLE ra_tasks ADD CONSTRAINT ra_tasks_pause_result_check
    CHECK (pause_result IS NULL OR jsonb_typeof(pause_result) = 'object');
-- A manually skipped action must never be counted as a successful action.
ALTER TABLE ra_tasks ADD CONSTRAINT ra_tasks_skipped_completed_check
    CHECK (NOT (completed && skipped_steps));
ALTER TABLE ra_tasks ADD CONSTRAINT ra_tasks_skipped_terminal_check CHECK (
    (state <> 'SUCCEEDED' OR cardinality(skipped_steps) = 0)
    AND (state <> 'COMPLETED_WITH_SKIPS' OR cardinality(skipped_steps) > 0)
);

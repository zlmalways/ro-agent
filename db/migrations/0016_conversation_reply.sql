-- A conversational reply ends a turn without claiming any robot action succeeded.
ALTER TABLE ra_tasks DROP CONSTRAINT ra_tasks_state_check;
ALTER TABLE ra_tasks ADD CONSTRAINT ra_tasks_state_check CHECK (state IN (
    'RECEIVED','PLANNING','NEEDS_CLARIFICATION','WAITING_APPROVAL',
    'READY','QUEUED','RUNNING','BLOCKED','CANCEL_REQUESTED',
    'SUCCEEDED','FAILED','CANCELLED',
    'PAUSE_REQUESTED','PAUSED','COMPLETED_WITH_SKIPS','REPLIED'
));

-- Keep reply text in the same durable conversation history used by the model.
ALTER TABLE ra_task_messages DROP CONSTRAINT ra_task_messages_check;
ALTER TABLE ra_task_messages ADD CONSTRAINT ra_task_messages_check CHECK (
    (role = 'user' AND message_kind = 'user_text')
    OR (role = 'assistant' AND message_kind IN ('assistant_text', 'clarify', 'unsupported', 'reply'))
);

-- A task id is the user-visible conversation id.  ra_tasks keeps one row per
-- execution turn so the existing planner, allocator, command and feedback
-- facts remain isolated from later turns in the same conversation.
CREATE TABLE IF NOT EXISTS ra_conversations (
    customer_id text NOT NULL,
    conversation_id text NOT NULL,
    actor_kind text NOT NULL CHECK (actor_kind IN ('user', 'robot')),
    actor_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (customer_id, conversation_id)
);

ALTER TABLE ra_tasks ADD COLUMN IF NOT EXISTS conversation_id text;
ALTER TABLE ra_tasks ADD COLUMN IF NOT EXISTS turn_seq bigint;

-- Existing rows were one-task conversations before the conversation model was
-- introduced.  Preserve their public ids and make each one turn 1.
UPDATE ra_tasks
SET conversation_id = task_id,
    turn_seq = 1
WHERE conversation_id IS NULL OR turn_seq IS NULL;

ALTER TABLE ra_tasks ALTER COLUMN conversation_id SET NOT NULL;
ALTER TABLE ra_tasks ALTER COLUMN turn_seq SET NOT NULL;
ALTER TABLE ra_tasks ADD CONSTRAINT ra_tasks_turn_seq_check CHECK (turn_seq > 0);
ALTER TABLE ra_tasks ADD CONSTRAINT ra_tasks_conversation_turn_key
    UNIQUE (customer_id, conversation_id, turn_seq);

INSERT INTO ra_conversations (customer_id, conversation_id, actor_kind, actor_id, created_at, updated_at)
SELECT customer_id, conversation_id, min(actor_kind), min(actor_id), min(created_at), max(updated_at)
FROM ra_tasks
GROUP BY customer_id, conversation_id
ON CONFLICT (customer_id, conversation_id) DO NOTHING;

ALTER TABLE ra_tasks ADD CONSTRAINT ra_tasks_conversation_fk
    FOREIGN KEY (customer_id, conversation_id)
    REFERENCES ra_conversations (customer_id, conversation_id);

CREATE INDEX IF NOT EXISTS ra_tasks_conversation_order
    ON ra_tasks (customer_id, conversation_id, turn_seq DESC);

-- Messages are keyed by conversation, with each message tied to the turn that
-- produced it.  The turn link keeps late execution facts associated with the
-- correct historical turn.
ALTER TABLE ra_task_messages ADD COLUMN IF NOT EXISTS turn_id text;
UPDATE ra_task_messages
SET turn_id = task_id
WHERE turn_id IS NULL;
ALTER TABLE ra_task_messages ALTER COLUMN turn_id SET NOT NULL;

DO $$ DECLARE constraint_name text;
BEGIN
    FOR constraint_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'ra_task_messages'::regclass
          AND confrelid = 'ra_tasks'::regclass
    LOOP
        EXECUTE format('ALTER TABLE ra_task_messages DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END $$;

ALTER TABLE ra_task_messages ADD CONSTRAINT ra_task_messages_conversation_fk
    FOREIGN KEY (customer_id, task_id)
    REFERENCES ra_conversations (customer_id, conversation_id);
ALTER TABLE ra_task_messages ADD CONSTRAINT ra_task_messages_turn_fk
    FOREIGN KEY (customer_id, turn_id)
    REFERENCES ra_tasks (customer_id, task_id);

CREATE INDEX IF NOT EXISTS ra_task_messages_turn
    ON ra_task_messages (customer_id, turn_id, seq);

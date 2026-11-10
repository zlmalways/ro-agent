-- Persist the chatbot conversation for each task.
-- Business state history remains in ra_task_events; audio provenance remains in ra_inputs.
CREATE TABLE IF NOT EXISTS ra_task_messages (
    customer_id text NOT NULL,
    task_id text NOT NULL,
    message_id text NOT NULL,
    seq bigint NOT NULL CHECK (seq > 0),
    role text NOT NULL,
    message_kind text NOT NULL,
    content text NOT NULL CHECK (length(btrim(content)) > 0),
    request_key text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (customer_id, message_id),
    UNIQUE (customer_id, task_id, seq),
    FOREIGN KEY (customer_id, task_id)
        REFERENCES ra_tasks (customer_id, task_id),
    CHECK (
        (role = 'user' AND message_kind = 'user_text')
        OR (role = 'assistant' AND message_kind IN ('assistant_text', 'clarify', 'unsupported'))
    )
);

-- A non-empty request key makes repeated delivery of the same user message idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS ra_task_messages_request_key
    ON ra_task_messages (customer_id, task_id, request_key)
    WHERE request_key <> '';

-- Read a task conversation in its original order.
CREATE INDEX IF NOT EXISTS ra_task_messages_context
    ON ra_task_messages (customer_id, task_id, seq);

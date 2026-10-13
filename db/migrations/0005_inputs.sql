CREATE TABLE ra_inputs (
    customer_id text NOT NULL,
    actor_id text NOT NULL,
    operation text NOT NULL,
    key text NOT NULL,
    input_id text NOT NULL UNIQUE,
    request_digest text NOT NULL,
    state text NOT NULL CHECK (state IN ('PROCESSING','TRANSCRIBED','COMPLETE','FAILED')),
    normalized_text text,
    provider_ref text,
    result_task_id text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (customer_id,actor_id,operation,key),
    FOREIGN KEY (customer_id,result_task_id) REFERENCES ra_tasks(customer_id,task_id)
);

-- Keep one durable audit row for every model call.  The request is the exact
-- JSON sent to the provider; the response fields preserve whatever came back
-- before the planner parses it.
CREATE TABLE IF NOT EXISTS ra_llm_calls (
    customer_id text NOT NULL,
    llm_call_id text NOT NULL,
    turn_id text NOT NULL,
    attempt_no integer NOT NULL DEFAULT 1 CHECK (attempt_no > 0),
    provider text NOT NULL,
    model text NOT NULL,
    endpoint text NOT NULL,
    request_json jsonb NOT NULL,
    response_json jsonb,
    response_text text,
    status text NOT NULL CHECK (status IN ('STARTED','SUCCEEDED','FAILED')),
    http_status integer CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599),
    provider_request_id text,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    PRIMARY KEY (customer_id, llm_call_id),
    UNIQUE (llm_call_id),
    UNIQUE (customer_id, turn_id, attempt_no),
    FOREIGN KEY (customer_id, turn_id)
        REFERENCES ra_tasks (customer_id, task_id),
    CHECK (
        (status = 'STARTED' AND finished_at IS NULL)
        OR
        (status IN ('SUCCEEDED','FAILED') AND finished_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS ra_llm_calls_turn
    ON ra_llm_calls (customer_id, turn_id, started_at DESC);

-- A cancelled local request has ended even when the provider returned no body.
-- Preserve existing audit rows; cancellation is recorded by the live call owner.
ALTER TABLE ra_llm_calls
    DROP CONSTRAINT ra_llm_calls_status_check,
    DROP CONSTRAINT ra_llm_calls_check;

ALTER TABLE ra_llm_calls
    ADD CONSTRAINT ra_llm_calls_status_check
        CHECK (status IN ('STARTED','SUCCEEDED','FAILED','CANCELLED')),
    ADD CONSTRAINT ra_llm_calls_check
        CHECK (
            (status = 'STARTED' AND finished_at IS NULL)
            OR
            (status IN ('SUCCEEDED','FAILED','CANCELLED') AND finished_at IS NOT NULL)
        );

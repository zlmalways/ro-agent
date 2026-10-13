-- Keep the installed v1 migration immutable; align the persisted outcome with the V5 contract.
ALTER TABLE ra_commands DROP CONSTRAINT ra_commands_outcome_check;
UPDATE ra_commands SET outcome='RESULT_UNKNOWN' WHERE outcome='UNKNOWN';
ALTER TABLE ra_commands ADD CONSTRAINT ra_commands_outcome_check
    CHECK (outcome IN ('NONE','RUNNING','SUCCEEDED','FAILED','RESULT_UNKNOWN'));

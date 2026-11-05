-- Keep every active allocation, including unknown execution and pre-plan work.
-- An idle deployment row is not an allocation. Migration is atomic; invalid active
-- data fails the constraints instead of being silently dropped or reassigned.
DELETE FROM ra_execution_guard WHERE state='IDLE';
ALTER TABLE ra_execution_guard DROP CONSTRAINT ra_execution_guard_pkey;
ALTER TABLE ra_execution_guard ADD COLUMN allocation_id text NOT NULL DEFAULT gen_random_uuid()::text;
ALTER TABLE ra_execution_guard ALTER COLUMN customer_id SET NOT NULL;
ALTER TABLE ra_execution_guard ALTER COLUMN task_id SET NOT NULL;
ALTER TABLE ra_execution_guard ALTER COLUMN unit_id SET NOT NULL;
ALTER TABLE ra_execution_guard ALTER COLUMN binding_id SET NOT NULL;
ALTER TABLE ra_execution_guard ALTER COLUMN allocation_snapshot SET NOT NULL;
ALTER TABLE ra_execution_guard ALTER COLUMN state SET DEFAULT 'ACTIVE';
ALTER TABLE ra_execution_guard ADD PRIMARY KEY(customer_id,binding_id);
ALTER TABLE ra_execution_guard ADD CONSTRAINT ra_allocation_unique_task UNIQUE(customer_id,task_id);
ALTER TABLE ra_execution_guard ADD CONSTRAINT ra_allocation_unique_id UNIQUE(allocation_id);
ALTER TABLE ra_execution_guard ADD CONSTRAINT ra_allocation_active CHECK(state <> 'IDLE');

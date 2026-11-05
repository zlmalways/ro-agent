-- Preserve the deployment-wide slot while allowing allocation before a plan exists.
-- Old unit/plan/command history stays untouched. All changes are in migrate()'s transaction.
ALTER TABLE ra_execution_guard ADD COLUMN allocation_snapshot jsonb;
UPDATE ra_execution_guard g SET allocation_snapshot=p.snapshot
FROM ra_units u JOIN ra_plans p ON p.customer_id=u.customer_id AND p.plan_id=u.plan_id
WHERE g.customer_id=u.customer_id AND g.unit_id=u.unit_id AND g.task_id=u.task_id;

DO $$ DECLARE constraint_name text;
BEGIN
    FOR constraint_name IN SELECT conname FROM pg_constraint
        WHERE conrelid='ra_execution_guard'::regclass AND confrelid='ra_units'::regclass AND contype='f'
    LOOP
        EXECUTE format('ALTER TABLE ra_execution_guard DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END $$;

ALTER TABLE ra_tasks ADD CONSTRAINT ra_task_unit_owner UNIQUE(customer_id,task_id,unit_id);
ALTER TABLE ra_execution_guard ADD CONSTRAINT ra_allocation_task_owner
    FOREIGN KEY(customer_id,task_id,unit_id) REFERENCES ra_tasks(customer_id,task_id,unit_id);
ALTER TABLE ra_execution_guard ADD CONSTRAINT ra_allocation_robot_owner
    FOREIGN KEY(customer_id,binding_id) REFERENCES ra_robot_bindings(customer_id,binding_id);
ALTER TABLE ra_execution_guard ADD CONSTRAINT ra_allocation_snapshot_required
    CHECK ((state='IDLE' AND allocation_snapshot IS NULL) OR
           (state<>'IDLE' AND allocation_snapshot IS NOT NULL));

ALTER TABLE ra_decisions ADD COLUMN plan_revision bigint;
ALTER TABLE ra_decisions ADD COLUMN scope text[];
ALTER TABLE ra_decisions ADD COLUMN approver_id text;
ALTER TABLE ra_decisions ADD COLUMN policy_version text;
ALTER TABLE ra_decisions ADD COLUMN issued_at timestamptz;
ALTER TABLE ra_decisions ADD COLUMN expires_at timestamptz;
ALTER TABLE ra_decisions ADD COLUMN revoked_at timestamptz;
ALTER TABLE ra_decisions ADD CONSTRAINT ra_approval_complete CHECK (kind <> 'approval' OR
    (plan_id IS NOT NULL AND plan_revision IS NOT NULL AND scope IS NOT NULL AND approver_id IS NOT NULL
        AND policy_version IS NOT NULL AND issued_at IS NOT NULL AND expires_at > issued_at));
CREATE TABLE ra_resource_heads (
    customer_id text NOT NULL,
    binding_id text NOT NULL,
    kind text NOT NULL,
    revision bigint NOT NULL,
    PRIMARY KEY (customer_id,binding_id,kind),
    FOREIGN KEY (customer_id,binding_id) REFERENCES ra_robot_bindings(customer_id,binding_id),
    FOREIGN KEY (customer_id,kind,binding_id,revision)
        REFERENCES ra_resource_versions(customer_id,kind,resource_id,revision)
);

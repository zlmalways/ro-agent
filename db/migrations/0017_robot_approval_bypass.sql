-- Approval is required by default; this setting belongs to one customer/robot.
ALTER TABLE ra_robot_bindings
    ADD COLUMN approval_bypass boolean NOT NULL DEFAULT false,
    ADD COLUMN approval_bypass_version bigint NOT NULL DEFAULT 1 CHECK (approval_bypass_version > 0),
    ADD COLUMN approval_bypass_updated_by text NOT NULL DEFAULT '',
    ADD COLUMN approval_bypass_updated_at timestamptz;

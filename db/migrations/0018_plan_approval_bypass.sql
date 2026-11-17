-- Existing plans retain their original approval behavior. New plans capture
-- the robot setting atomically when their immutable plan record is created.
ALTER TABLE ra_plans
    ADD COLUMN approval_bypass boolean NOT NULL DEFAULT false,
    ADD COLUMN approval_bypass_version bigint NOT NULL DEFAULT 0 CHECK (approval_bypass_version >= 0),
    ADD CONSTRAINT ra_plan_approval_bypass_recorded CHECK (NOT approval_bypass OR approval_bypass_version > 0);

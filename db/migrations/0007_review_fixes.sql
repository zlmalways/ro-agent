ALTER TABLE ra_tasks ADD COLUMN cancel_requested boolean NOT NULL DEFAULT false;
ALTER TABLE ra_commands ADD COLUMN dispatch_started_at timestamptz;

ALTER TABLE ra_tasks ADD COLUMN clarification_id text NOT NULL DEFAULT '';
ALTER TABLE ra_tasks ADD COLUMN answers text[] NOT NULL DEFAULT '{}';

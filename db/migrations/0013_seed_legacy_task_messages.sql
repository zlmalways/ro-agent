-- The conversation table migration preserved old task rows, but those rows
-- predate durable chat messages.  Seed the original user request once so the
-- first agent call receives the same context as a newly submitted task.
INSERT INTO ra_task_messages (customer_id, task_id, message_id, turn_id, seq, role, message_kind, content, request_key)
SELECT t.customer_id, t.conversation_id, 'legacy:' || t.task_id, t.task_id, 1,
       'user', 'user_text', t.goal_text, ''
FROM ra_tasks t
WHERE length(btrim(t.goal_text)) > 0
  AND NOT EXISTS (
      SELECT 1 FROM ra_task_messages m
      WHERE m.customer_id=t.customer_id AND m.task_id=t.conversation_id
  );

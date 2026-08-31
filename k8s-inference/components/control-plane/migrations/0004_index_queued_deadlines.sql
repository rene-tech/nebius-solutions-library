CREATE INDEX fs2_operations_queued_deadline_idx
    ON fs2_operations (deadline_at,id)
    WHERE status='queued' AND deadline_at IS NOT NULL;

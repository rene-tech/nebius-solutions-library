-- Existing operations retain their admission behavior. Delegated operations
-- carry durable correlation; retention does not depend on a parent FK.
ALTER TABLE fs2_operations
    ADD COLUMN parent_operation_id uuid,
    ADD COLUMN parent_attempt_id uuid,
    ADD CONSTRAINT fs2_operations_parent_pair CHECK (
        (parent_operation_id IS NULL) = (parent_attempt_id IS NULL)
        AND parent_operation_id IS DISTINCT FROM id
    );

CREATE UNIQUE INDEX fs2_operations_one_active_child
    ON fs2_operations(parent_operation_id)
    WHERE parent_operation_id IS NOT NULL AND status IN ('queued','activating','running');

CREATE INDEX fs2_operations_parent_idx ON fs2_operations(parent_operation_id, parent_attempt_id)
    WHERE parent_operation_id IS NOT NULL;

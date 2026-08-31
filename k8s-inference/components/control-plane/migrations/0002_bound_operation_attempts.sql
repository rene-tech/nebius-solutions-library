ALTER TABLE fs2_operations
    ADD CONSTRAINT fs2_operations_attempt_within_max
    CHECK (attempt <= max_attempts);

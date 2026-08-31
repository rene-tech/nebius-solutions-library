ALTER TABLE fs2_operations
    ADD CONSTRAINT fs2_operations_model_id_length
    CHECK (char_length(model_id) BETWEEN 1 AND 128),
    ADD CONSTRAINT fs2_operations_idempotency_key_length
    CHECK (char_length(idempotency_key) BETWEEN 8 AND 200);

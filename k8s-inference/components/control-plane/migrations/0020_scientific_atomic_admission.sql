-- Commit the complete frozen scientific admission beside its public Operation.
-- The worker materializes this outbox row idempotently into
-- fs2_scientific_batches, so a process exit after Operation insertion cannot
-- lose the accepted request or require a client replay.
CREATE TABLE fs2_scientific_admission_outbox (
    operation_id uuid PRIMARY KEY REFERENCES fs2_operations(id) ON DELETE CASCADE,
    payload jsonb NOT NULL CHECK (
        pg_column_size(payload) <= 4194304
        AND jsonb_typeof(payload) = 'object'
        AND payload->>'schema_version' = 'fs2-serve.nebius.ai/scientific-batch-state/v8'
        AND payload->>'operation_id' = operation_id::text
        AND payload->>'status' = 'queued'
        AND (payload->>'revision')::integer = 0
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX fs2_scientific_admission_outbox_created_idx
    ON fs2_scientific_admission_outbox(created_at, operation_id);

-- Terminal publication asks for one exact kind. Keep that lookup independent
-- of the number of earlier lifecycle events in the batch ledger.
CREATE INDEX fs2_scientific_batch_events_kind_idx
    ON fs2_scientific_batch_events(operation_id, kind, sequence);

REVOKE ALL ON fs2_scientific_admission_outbox FROM PUBLIC;

CREATE TABLE fs2_scientific_batches (
    operation_id uuid PRIMARY KEY REFERENCES fs2_operations(id) ON DELETE CASCADE,
    batch_id uuid NOT NULL UNIQUE,
    workload_id uuid NOT NULL UNIQUE,
    tenant_id text NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 120),
    model_id text NOT NULL CHECK (model_id ~ '^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$' AND length(model_id) <= 63),
    variant_id text NOT NULL CHECK (variant_id ~ '^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$' AND length(variant_id) <= 128),
    input_artifact_id uuid NOT NULL REFERENCES fs2_scientific_artifacts(id),
    scheduling_digest char(71) NOT NULL CHECK (scheduling_digest ~ '^sha256:[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
    revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
    cancel_requested boolean NOT NULL DEFAULT false,
    state jsonb NOT NULL CHECK (
        pg_column_size(state) <= 4194304
        AND jsonb_typeof(state) = 'object'
        AND state->>'schema_version' = 'fs2-serve.nebius.ai/scientific-batch-state/v6'
        AND state->>'operation_id' = operation_id::text
        AND state->>'batch_id' = batch_id::text
        AND state->>'workload_id' = workload_id::text
        AND state->>'tenant_id' = tenant_id
        AND state->>'model_id' = model_id
        AND state->>'variant_id' = variant_id
        AND state->>'input_artifact_id' = input_artifact_id::text
        AND state->>'status' = status
        AND (state->>'revision')::integer = revision
        AND (state->>'cancel_requested')::boolean = cancel_requested
    ),
    controller_id text,
    fencing_token bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    lease_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(operation_id, batch_id, workload_id),
    CHECK ((controller_id IS NULL) = (lease_expires_at IS NULL)),
    CHECK (controller_id IS NULL OR length(controller_id) BETWEEN 1 AND 253)
);

CREATE INDEX fs2_scientific_batches_claim_idx
    ON fs2_scientific_batches(status, lease_expires_at, operation_id)
    WHERE status IN ('queued','running','succeeded','failed','cancelled');
CREATE INDEX fs2_scientific_batches_tenant_idx
    ON fs2_scientific_batches(tenant_id, operation_id);

CREATE TABLE fs2_scientific_batch_events (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id char(71) NOT NULL CHECK (event_id ~ '^sha256:[0-9a-f]{64}$'),
    operation_id uuid NOT NULL,
    batch_id uuid NOT NULL,
    workload_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN (
        'lifecycle','attempt_fenced','retry_scheduled','stage_succeeded','stage_failed',
        'batch_succeeded','batch_failed','batch_cancelled'
    )),
    stage_id text,
    shard_id text,
    attempt_id uuid,
    phase text CHECK (phase IS NULL OR phase IN (
        'queued','scheduling','admitted','node_pending','image_loading','artifact_loading',
        'restoring','semantic_warmup','active_compute','allocated_idle','grace_drain',
        'preempted','teardown'
    )),
    code text CHECK (code IS NULL OR length(code) BETWEEN 1 AND 128),
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(operation_id, event_id),
    FOREIGN KEY(operation_id, batch_id, workload_id)
        REFERENCES fs2_scientific_batches(operation_id, batch_id, workload_id) ON DELETE CASCADE,
    CHECK (kind <> 'lifecycle' OR (stage_id IS NOT NULL AND attempt_id IS NOT NULL AND phase IS NOT NULL)),
    CHECK (stage_id IS NULL OR (stage_id ~ '^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$' AND length(stage_id) <= 63)),
    CHECK (shard_id IS NULL OR (shard_id ~ '^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$' AND length(shard_id) <= 63))
);

CREATE INDEX fs2_scientific_batch_events_operation_idx
    ON fs2_scientific_batch_events(operation_id, sequence);

CREATE FUNCTION fs2_scientific_batch_state_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.operation_id <> OLD.operation_id OR NEW.batch_id <> OLD.batch_id
       OR NEW.workload_id <> OLD.workload_id OR NEW.tenant_id <> OLD.tenant_id
       OR NEW.model_id <> OLD.model_id OR NEW.variant_id <> OLD.variant_id
       OR NEW.input_artifact_id <> OLD.input_artifact_id
       OR NEW.scheduling_digest <> OLD.scheduling_digest
       OR NEW.state->'plan' <> OLD.state->'plan' OR NEW.state->'scheduling' <> OLD.state->'scheduling'
       OR NEW.state->'adapter_execution' <> OLD.state->'adapter_execution'
       OR NEW.state->'access_context' <> OLD.state->'access_context'
       OR NEW.state->'input_manifest' <> OLD.state->'input_manifest'
       OR NEW.state->'runtime_artifacts' <> OLD.state->'runtime_artifacts' THEN
        RAISE EXCEPTION 'scientific batch admission is immutable';
    END IF;
    IF NEW.revision < OLD.revision THEN
        RAISE EXCEPTION 'scientific batch revision cannot move backwards';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER fs2_scientific_batch_state_immutable_trigger
BEFORE UPDATE ON fs2_scientific_batches
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_batch_state_immutable();

CREATE FUNCTION fs2_scientific_batch_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'scientific batch ledgers are append-only';
END;
$$;

CREATE TRIGGER fs2_scientific_batch_events_append_only_trigger
BEFORE UPDATE OR DELETE ON fs2_scientific_batch_events
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_batch_append_only();

REVOKE ALL ON fs2_scientific_batches,fs2_scientific_batch_events FROM PUBLIC;
REVOKE ALL ON SEQUENCE fs2_scientific_batch_events_sequence_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_batch_state_immutable(),
    fs2_scientific_batch_append_only() FROM PUBLIC;

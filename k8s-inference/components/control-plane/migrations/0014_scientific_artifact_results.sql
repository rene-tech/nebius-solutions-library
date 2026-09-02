-- Scientific artifact provenance, per-stage manifest commits and terminal results.
--
-- Scope is (operation, stage, shard, attempt). A gang-scheduled stage with no
-- shard identity stores the sentinel '-', which no DNS name can collide with,
-- so every foreign key stays composite and NOT NULL.
--
-- No column in this migration holds object bytes, a presigned URL, a signed
-- header, or a credential. Only content addresses and identities are stored.

CREATE TABLE fs2_scientific_stage_attempts (
    attempt_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL REFERENCES fs2_operations(id),
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    stage_id text NOT NULL CHECK (length(stage_id) BETWEEN 1 AND 63 AND stage_id ~ '^[a-z][a-z0-9-]*$'),
    shard_id text NOT NULL CHECK (
        shard_id='-'
        OR (length(shard_id) BETWEEN 1 AND 253 AND shard_id ~ '^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$')
    ),
    attempt_number integer NOT NULL CHECK (attempt_number BETWEEN 1 AND 10),
    status text NOT NULL CHECK (status IN ('running','succeeded','failed','cancelled','preempted')),
    resolved_pool_id text CHECK (length(resolved_pool_id) BETWEEN 1 AND 128),
    admitted_resource_flavor text CHECK (length(admitted_resource_flavor) BETWEEN 1 AND 253),
    accelerator_resource_name text CHECK (length(accelerator_resource_name) BETWEEN 1 AND 253),
    accelerator_count integer NOT NULL DEFAULT 0 CHECK (accelerator_count BETWEEN 0 AND 1024),
    admitted_at timestamptz,
    kueue_workload_uid text CHECK (length(kueue_workload_uid) BETWEEN 1 AND 128),
    k8s_job_uid text CHECK (length(k8s_job_uid) BETWEEN 1 AND 128),
    pod_uids text[] NOT NULL DEFAULT '{}' CHECK (COALESCE(array_length(pod_uids,1),0) <= 1024),
    node_uids text[] NOT NULL DEFAULT '{}' CHECK (COALESCE(array_length(node_uids,1),0) <= 1024),
    gpu_uuids text[] NOT NULL DEFAULT '{}' CHECK (COALESCE(array_length(gpu_uuids,1),0) <= 1024),
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    retention_expires_at timestamptz NOT NULL,
    UNIQUE (attempt_id,operation_id,tenant_id,stage_id,shard_id),
    UNIQUE (operation_id,stage_id,shard_id,attempt_number),
    CHECK ((status='running') = (completed_at IS NULL)),
    CHECK (completed_at IS NULL OR completed_at >= started_at),
    CHECK (retention_expires_at > started_at),
    CHECK (
        (accelerator_count >= 1 AND admitted_at IS NOT NULL AND resolved_pool_id IS NOT NULL
            AND admitted_resource_flavor IS NOT NULL AND accelerator_resource_name IS NOT NULL)
        OR (accelerator_count = 0 AND resolved_pool_id IS NULL
            AND admitted_resource_flavor IS NULL AND accelerator_resource_name IS NULL)
    ),
    CHECK (status NOT IN ('succeeded','preempted') OR admitted_at IS NOT NULL)
);

CREATE INDEX fs2_scientific_stage_attempts_operation_idx
    ON fs2_scientific_stage_attempts (operation_id,stage_id,shard_id,attempt_number);

CREATE INDEX fs2_scientific_stage_attempts_stage_status_idx
    ON fs2_scientific_stage_attempts (operation_id,tenant_id,stage_id,status);

CREATE TABLE fs2_scientific_artifacts (
    id uuid PRIMARY KEY,
    attempt_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    tenant_id text NOT NULL,
    stage_id text NOT NULL,
    shard_id text NOT NULL,
    direction text NOT NULL CHECK (direction IN ('input','output')),
    digest char(71) NOT NULL CHECK (digest ~ '^sha256:[a-f0-9]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes BETWEEN 0 AND 1099511627776),
    media_type text NOT NULL CHECK (
        length(media_type) BETWEEN 3 AND 128
        AND media_type ~ '^[a-z0-9][a-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+_-]*$'
    ),
    compression text CHECK (compression IN ('gzip','zstd')),
    storage_key text NOT NULL CHECK (length(storage_key) BETWEEN 1 AND 1024),
    access_profile text NOT NULL CHECK (access_profile IN ('public','restricted','academic')),
    access_receipt_digest char(71) CHECK (access_receipt_digest ~ '^sha256:[a-f0-9]{64}$'),
    retention_expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (attempt_id,operation_id,tenant_id,stage_id,shard_id)
        REFERENCES fs2_scientific_stage_attempts(attempt_id,operation_id,tenant_id,stage_id,shard_id),
    UNIQUE (attempt_id,direction,digest),
    UNIQUE (storage_key),
    UNIQUE (id,operation_id,tenant_id,attempt_id),
    CHECK (
        (access_profile='public' AND access_receipt_digest IS NULL)
        OR (access_profile IN ('restricted','academic') AND access_receipt_digest IS NOT NULL)
    ),
    CHECK (
        storage_key='scientific/v1/tenants/' || tenant_id || '/operations/' || operation_id::text
            || '/stages/' || stage_id || '/shards/' || shard_id || '/attempts/' || attempt_id::text
            || '/' || direction || '/sha256/' || substring(digest FROM 8)
    )
);

CREATE INDEX fs2_scientific_artifacts_operation_idx
    ON fs2_scientific_artifacts (operation_id,tenant_id,stage_id,created_at,id);

CREATE TABLE fs2_scientific_uploads (
    id uuid PRIMARY KEY,
    attempt_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    tenant_id text NOT NULL,
    stage_id text NOT NULL,
    shard_id text NOT NULL,
    direction text NOT NULL CHECK (direction IN ('input','output')),
    expected_digest char(71) NOT NULL CHECK (expected_digest ~ '^sha256:[a-f0-9]{64}$'),
    expected_size_bytes bigint NOT NULL CHECK (expected_size_bytes BETWEEN 0 AND 1099511627776),
    media_type text NOT NULL CHECK (
        length(media_type) BETWEEN 3 AND 128
        AND media_type ~ '^[a-z0-9][a-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+_-]*$'
    ),
    compression text CHECK (compression IN ('gzip','zstd')),
    storage_key text NOT NULL CHECK (length(storage_key) BETWEEN 1 AND 1024),
    access_profile text NOT NULL CHECK (access_profile IN ('public','restricted','academic')),
    access_receipt_digest char(71) CHECK (access_receipt_digest ~ '^sha256:[a-f0-9]{64}$'),
    artifact_id uuid,
    begun_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finalized_at timestamptz,
    FOREIGN KEY (attempt_id,operation_id,tenant_id,stage_id,shard_id)
        REFERENCES fs2_scientific_stage_attempts(attempt_id,operation_id,tenant_id,stage_id,shard_id),
    FOREIGN KEY (artifact_id,operation_id,tenant_id,attempt_id)
        REFERENCES fs2_scientific_artifacts(id,operation_id,tenant_id,attempt_id),
    UNIQUE (storage_key),
    CHECK (
        (access_profile='public' AND access_receipt_digest IS NULL)
        OR (access_profile IN ('restricted','academic') AND access_receipt_digest IS NOT NULL)
    ),
    CHECK ((artifact_id IS NULL) = (finalized_at IS NULL)),
    CHECK (
        storage_key='scientific/v1/tenants/' || tenant_id || '/operations/' || operation_id::text
            || '/stages/' || stage_id || '/shards/' || shard_id || '/attempts/' || attempt_id::text
            || '/' || direction || '/sha256/' || substring(expected_digest FROM 8)
    )
);

CREATE INDEX fs2_scientific_uploads_operation_idx
    ON fs2_scientific_uploads (operation_id,tenant_id,begun_at,id);

CREATE TABLE fs2_scientific_stage_commits (
    operation_id uuid NOT NULL REFERENCES fs2_operations(id),
    stage_id text NOT NULL CHECK (length(stage_id) BETWEEN 1 AND 63 AND stage_id ~ '^[a-z][a-z0-9-]*$'),
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    manifest_digest char(71) NOT NULL CHECK (manifest_digest ~ '^sha256:[a-f0-9]{64}$'),
    validation_digest char(71) NOT NULL CHECK (validation_digest ~ '^sha256:[a-f0-9]{64}$'),
    semantic_valid boolean NOT NULL,
    manifest jsonb NOT NULL CHECK (
        jsonb_typeof(manifest)='object'
        AND octet_length(manifest::text) BETWEEN 2 AND 8388608
        AND manifest->>'schema'='fs2-serve.nebius.ai/scientific-artifact-manifest/v1'
        AND manifest->>'manifest_id'=operation_id::text || ':' || stage_id
        AND jsonb_typeof(manifest->'entries')='array'
    ),
    committed_at timestamptz NOT NULL,
    validated_at timestamptz NOT NULL,
    PRIMARY KEY (operation_id,stage_id),
    CHECK (validated_at >= committed_at)
);

CREATE TABLE fs2_scientific_stage_commit_attempts (
    operation_id uuid NOT NULL,
    stage_id text NOT NULL,
    attempt_id uuid NOT NULL REFERENCES fs2_scientific_stage_attempts(attempt_id),
    PRIMARY KEY (operation_id,stage_id,attempt_id),
    FOREIGN KEY (operation_id,stage_id)
        REFERENCES fs2_scientific_stage_commits(operation_id,stage_id)
);

CREATE TABLE fs2_scientific_run_results (
    operation_id uuid PRIMARY KEY REFERENCES fs2_operations(id),
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    result_digest char(71) NOT NULL CHECK (result_digest ~ '^sha256:[a-f0-9]{64}$'),
    terminal_status text NOT NULL CHECK (terminal_status IN ('succeeded','failed','cancelled')),
    semantic_validation_status text NOT NULL
        CHECK (semantic_validation_status IN ('passed','failed','not-run')),
    document jsonb NOT NULL CHECK (
        jsonb_typeof(document)='object'
        AND octet_length(document::text) BETWEEN 2 AND 8388608
        AND document->>'schema'='fs2-serve.nebius.ai/scientific-run-result/v1'
        AND document->>'operation_id'=operation_id::text
        AND document->>'terminal_status'=terminal_status
        AND document->'semantic_validation'->>'status'=semantic_validation_status
        AND jsonb_typeof(document->'attempts')='array'
        AND jsonb_typeof(document->'scheduling_snapshot')='object'
        AND jsonb_typeof(document->'execution_identity')='object'
    ),
    submitted_at timestamptz NOT NULL,
    completed_at timestamptz NOT NULL,
    committed_at timestamptz NOT NULL,
    retention_expires_at timestamptz NOT NULL,
    UNIQUE (operation_id,result_digest),
    CHECK (completed_at >= submitted_at),
    CHECK (committed_at >= completed_at),
    CHECK (retention_expires_at > committed_at)
);

CREATE INDEX fs2_scientific_run_results_retention_idx
    ON fs2_scientific_run_results (retention_expires_at,operation_id);

CREATE TABLE fs2_scientific_artifact_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type text NOT NULL CHECK (
        event_type IN ('attempt_opened','attempt_closed','upload_begun','artifact_finalized',
                       'stage_committed','result_committed')
    ),
    operation_id uuid NOT NULL REFERENCES fs2_operations(id),
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    stage_id text CHECK (length(stage_id) BETWEEN 1 AND 63 AND stage_id ~ '^[a-z][a-z0-9-]*$'),
    attempt_id uuid,
    upload_id uuid,
    artifact_id uuid,
    manifest_digest char(71) CHECK (manifest_digest ~ '^sha256:[a-f0-9]{64}$'),
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (event_type IN ('attempt_opened','attempt_closed') AND stage_id IS NOT NULL
            AND attempt_id IS NOT NULL AND upload_id IS NULL AND artifact_id IS NULL
            AND manifest_digest IS NULL)
        OR (event_type='upload_begun' AND stage_id IS NOT NULL AND attempt_id IS NOT NULL
            AND upload_id IS NOT NULL AND artifact_id IS NULL AND manifest_digest IS NULL)
        OR (event_type='artifact_finalized' AND stage_id IS NOT NULL AND attempt_id IS NOT NULL
            AND upload_id IS NOT NULL AND artifact_id IS NOT NULL AND manifest_digest IS NULL)
        OR (event_type='stage_committed' AND stage_id IS NOT NULL AND attempt_id IS NULL
            AND upload_id IS NULL AND artifact_id IS NULL AND manifest_digest IS NOT NULL)
        OR (event_type='result_committed' AND stage_id IS NULL AND attempt_id IS NULL
            AND upload_id IS NULL AND artifact_id IS NULL AND manifest_digest IS NOT NULL)
    )
);

CREATE INDEX fs2_scientific_artifact_events_operation_idx
    ON fs2_scientific_artifact_events (operation_id,tenant_id,id);

CREATE TABLE fs2_scientific_retention_ledger (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    operation_id uuid NOT NULL UNIQUE,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    purged_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    artifact_count integer NOT NULL CHECK (artifact_count >= 0),
    byte_count bigint NOT NULL CHECK (byte_count >= 0),
    retention_expired_at timestamptz NOT NULL
);

COMMENT ON TABLE fs2_scientific_retention_ledger IS
    'Durable evidence of completed retention deletions; it survives the purge it records';

-- The operation must exist, own the tenant, and not yet be terminal.
CREATE FUNCTION fs2_scientific_assert_writable() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    current_tenant text;
BEGIN
    SELECT tenant_id INTO current_tenant FROM fs2_operations WHERE id=NEW.operation_id FOR SHARE;
    IF NOT FOUND OR current_tenant<>NEW.tenant_id THEN
        RAISE EXCEPTION USING ERRCODE='FS201', MESSAGE='scientific artifact scope is not current';
    END IF;
    IF EXISTS (SELECT 1 FROM fs2_scientific_run_results WHERE operation_id=NEW.operation_id) THEN
        RAISE EXCEPTION USING ERRCODE='FS203', MESSAGE='operation already published a terminal result';
    END IF;
    RETURN NEW;
END
$function$;

-- A superseded attempt may never publish or reserve an artifact.
CREATE FUNCTION fs2_scientific_assert_live_attempt() RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    newest integer;
BEGIN
    SELECT max(attempt_number) INTO newest
    FROM fs2_scientific_stage_attempts
    WHERE operation_id=NEW.operation_id AND stage_id=NEW.stage_id AND shard_id=NEW.shard_id;
    IF newest IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='FS201', MESSAGE='scientific artifact attempt is unknown';
    END IF;
    IF EXISTS (
        SELECT 1 FROM fs2_scientific_stage_attempts
        WHERE attempt_id=NEW.attempt_id AND attempt_number < newest
    ) THEN
        RAISE EXCEPTION USING ERRCODE='FS201', MESSAGE='superseded scientific attempt cannot write';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION fs2_scientific_validate_attempt_transition() RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF OLD.operation_id IS DISTINCT FROM NEW.operation_id
       OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.stage_id IS DISTINCT FROM NEW.stage_id
       OR OLD.shard_id IS DISTINCT FROM NEW.shard_id
       OR OLD.attempt_number IS DISTINCT FROM NEW.attempt_number
       OR OLD.started_at IS DISTINCT FROM NEW.started_at
       OR OLD.retention_expires_at IS DISTINCT FROM NEW.retention_expires_at
       OR OLD.status <> 'running'
       OR NEW.status = 'running'
       OR NEW.completed_at IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='invalid scientific attempt transition';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION fs2_scientific_validate_upload_transition() RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF OLD.attempt_id IS DISTINCT FROM NEW.attempt_id
       OR OLD.operation_id IS DISTINCT FROM NEW.operation_id
       OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.stage_id IS DISTINCT FROM NEW.stage_id
       OR OLD.shard_id IS DISTINCT FROM NEW.shard_id
       OR OLD.direction IS DISTINCT FROM NEW.direction
       OR OLD.expected_digest IS DISTINCT FROM NEW.expected_digest
       OR OLD.expected_size_bytes IS DISTINCT FROM NEW.expected_size_bytes
       OR OLD.media_type IS DISTINCT FROM NEW.media_type
       OR OLD.compression IS DISTINCT FROM NEW.compression
       OR OLD.storage_key IS DISTINCT FROM NEW.storage_key
       OR OLD.access_profile IS DISTINCT FROM NEW.access_profile
       OR OLD.access_receipt_digest IS DISTINCT FROM NEW.access_receipt_digest
       OR OLD.begun_at IS DISTINCT FROM NEW.begun_at
       OR OLD.artifact_id IS NOT NULL
       OR NEW.artifact_id IS NULL
       OR NEW.finalized_at IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='invalid scientific upload transition';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION fs2_scientific_reject_mutation() RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='immutable scientific artifact record';
END
$function$;

-- Deletion is reserved for retention, which announces itself for one transaction.
CREATE FUNCTION fs2_scientific_guard_retention_delete() RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF COALESCE(current_setting('fs2.retention_purge', true),'off') <> 'on' THEN
        RAISE EXCEPTION USING ERRCODE='FS202',
            MESSAGE='scientific artifact rows are deletable only by retention';
    END IF;
    RETURN OLD;
END
$function$;

CREATE TRIGGER fs2_scientific_attempts_writable
BEFORE INSERT ON fs2_scientific_stage_attempts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_assert_writable();

CREATE TRIGGER fs2_scientific_uploads_writable
BEFORE INSERT ON fs2_scientific_uploads
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_assert_writable();

CREATE TRIGGER fs2_scientific_artifacts_writable
BEFORE INSERT ON fs2_scientific_artifacts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_assert_writable();

CREATE TRIGGER fs2_scientific_stage_commits_writable
BEFORE INSERT ON fs2_scientific_stage_commits
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_assert_writable();

CREATE TRIGGER fs2_scientific_uploads_live_attempt
BEFORE INSERT ON fs2_scientific_uploads
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_assert_live_attempt();

CREATE TRIGGER fs2_scientific_artifacts_live_attempt
BEFORE INSERT ON fs2_scientific_artifacts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_assert_live_attempt();

CREATE TRIGGER fs2_scientific_attempts_transition
BEFORE UPDATE ON fs2_scientific_stage_attempts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_validate_attempt_transition();

CREATE TRIGGER fs2_scientific_uploads_transition
BEFORE UPDATE ON fs2_scientific_uploads
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_validate_upload_transition();

CREATE TRIGGER fs2_scientific_artifacts_immutable
BEFORE UPDATE ON fs2_scientific_artifacts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_stage_commits_immutable
BEFORE UPDATE ON fs2_scientific_stage_commits
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_stage_commit_attempts_immutable
BEFORE UPDATE ON fs2_scientific_stage_commit_attempts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_run_results_immutable
BEFORE UPDATE ON fs2_scientific_run_results
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_artifact_events_immutable
BEFORE UPDATE ON fs2_scientific_artifact_events
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_retention_ledger_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_retention_ledger
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_attempts_retention_delete
BEFORE DELETE ON fs2_scientific_stage_attempts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_guard_retention_delete();

CREATE TRIGGER fs2_scientific_uploads_retention_delete
BEFORE DELETE ON fs2_scientific_uploads
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_guard_retention_delete();

CREATE TRIGGER fs2_scientific_artifacts_retention_delete
BEFORE DELETE ON fs2_scientific_artifacts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_guard_retention_delete();

CREATE TRIGGER fs2_scientific_stage_commits_retention_delete
BEFORE DELETE ON fs2_scientific_stage_commits
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_guard_retention_delete();

CREATE TRIGGER fs2_scientific_stage_commit_attempts_retention_delete
BEFORE DELETE ON fs2_scientific_stage_commit_attempts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_guard_retention_delete();

CREATE TRIGGER fs2_scientific_artifact_events_retention_delete
BEFORE DELETE ON fs2_scientific_artifact_events
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_guard_retention_delete();

CREATE TRIGGER fs2_scientific_run_results_retention_delete
BEFORE DELETE ON fs2_scientific_run_results
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_guard_retention_delete();

REVOKE ALL ON fs2_scientific_stage_attempts,fs2_scientific_artifacts,fs2_scientific_uploads,
    fs2_scientific_stage_commits,fs2_scientific_stage_commit_attempts,fs2_scientific_run_results,
    fs2_scientific_artifact_events,fs2_scientific_retention_ledger FROM PUBLIC;
REVOKE ALL ON SEQUENCE fs2_scientific_artifact_events_id_seq FROM PUBLIC;
REVOKE ALL ON SEQUENCE fs2_scientific_retention_ledger_id_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_assert_writable(),fs2_scientific_assert_live_attempt(),
    fs2_scientific_validate_attempt_transition(),fs2_scientific_validate_upload_transition(),
    fs2_scientific_reject_mutation(),fs2_scientific_guard_retention_delete() FROM PUBLIC;

COMMENT ON TABLE fs2_scientific_stage_attempts IS
    'Stage and shard attempts with the exact Kueue admission identity they received';
COMMENT ON TABLE fs2_scientific_artifacts IS
    'Immutable content addresses only; scientific bytes and signed handles stay in object storage';
COMMENT ON TABLE fs2_scientific_uploads IS
    'Attempt-fenced upload expectations; presigned upload handles are never persisted';
COMMENT ON TABLE fs2_scientific_stage_commits IS
    'Exactly one immutable scientific-artifact-manifest/v1 commit per operation stage';
COMMENT ON TABLE fs2_scientific_run_results IS
    'Exactly one immutable canonical scientific-run-result/v1 document per operation';
COMMENT ON TABLE fs2_scientific_artifact_events IS
    'Append-only closed event identities with no free-form detail, credentials, URLs or payloads';

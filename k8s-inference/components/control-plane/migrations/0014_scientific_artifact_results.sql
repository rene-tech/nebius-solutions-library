CREATE TABLE fs2_scientific_artifacts (
    id uuid PRIMARY KEY,
    operation_id uuid NOT NULL REFERENCES fs2_operations(id),
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    attempt integer NOT NULL CHECK (attempt BETWEEN 0 AND 10),
    direction text NOT NULL CHECK (direction IN ('input','output')),
    digest char(71) NOT NULL CHECK (digest ~ '^sha256:[a-f0-9]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes BETWEEN 0 AND 1099511627776),
    media_type text NOT NULL CHECK (
        length(media_type) BETWEEN 3 AND 127
        AND media_type ~ '^[a-z0-9][a-z0-9!#$&^_.+\-]*/[a-z0-9][a-z0-9!#$&^_.+\-]*$'
    ),
    compression text CHECK (compression IN ('gzip','zstd')),
    storage_key text NOT NULL CHECK (length(storage_key) BETWEEN 1 AND 1024),
    access_profile text NOT NULL CHECK (access_profile IN ('public','restricted','academic')),
    access_receipt_digest char(71) CHECK (access_receipt_digest ~ '^sha256:[a-f0-9]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (operation_id,attempt,storage_key),
    UNIQUE (id,operation_id,tenant_id,attempt),
    CHECK (
        (access_profile='public' AND access_receipt_digest IS NULL)
        OR (access_profile IN ('restricted','academic') AND access_receipt_digest IS NOT NULL)
    ),
    CHECK (
        storage_key='scientific/v1/tenants/' || tenant_id || '/operations/' || operation_id::text
            || '/attempts/' || attempt::text || '/' || direction || '/sha256/' || substring(digest FROM 8)
    )
);

CREATE INDEX fs2_scientific_artifacts_operation_idx
    ON fs2_scientific_artifacts (operation_id,attempt,created_at,id);

CREATE TABLE fs2_scientific_uploads (
    id uuid PRIMARY KEY,
    operation_id uuid NOT NULL REFERENCES fs2_operations(id),
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    attempt integer NOT NULL CHECK (attempt BETWEEN 0 AND 10),
    direction text NOT NULL CHECK (direction IN ('input','output')),
    expected_digest char(71) NOT NULL CHECK (expected_digest ~ '^sha256:[a-f0-9]{64}$'),
    expected_size_bytes bigint NOT NULL CHECK (expected_size_bytes BETWEEN 0 AND 1099511627776),
    media_type text NOT NULL CHECK (
        length(media_type) BETWEEN 3 AND 127
        AND media_type ~ '^[a-z0-9][a-z0-9!#$&^_.+\-]*/[a-z0-9][a-z0-9!#$&^_.+\-]*$'
    ),
    compression text CHECK (compression IN ('gzip','zstd')),
    storage_key text NOT NULL CHECK (length(storage_key) BETWEEN 1 AND 1024),
    access_profile text NOT NULL CHECK (access_profile IN ('public','restricted','academic')),
    access_receipt_digest char(71) CHECK (access_receipt_digest ~ '^sha256:[a-f0-9]{64}$'),
    artifact_id uuid,
    begun_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finalized_at timestamptz,
    UNIQUE (id,operation_id,tenant_id,attempt),
    UNIQUE (operation_id,attempt,storage_key),
    FOREIGN KEY (artifact_id,operation_id,tenant_id,attempt)
        REFERENCES fs2_scientific_artifacts(id,operation_id,tenant_id,attempt),
    CHECK (
        (access_profile='public' AND access_receipt_digest IS NULL)
        OR (access_profile IN ('restricted','academic') AND access_receipt_digest IS NOT NULL)
    ),
    CHECK (
        storage_key='scientific/v1/tenants/' || tenant_id || '/operations/' || operation_id::text
            || '/attempts/' || attempt::text || '/' || direction || '/sha256/'
            || substring(expected_digest FROM 8)
    ),
    CHECK ((artifact_id IS NULL AND finalized_at IS NULL) OR (artifact_id IS NOT NULL AND finalized_at IS NOT NULL))
);

CREATE INDEX fs2_scientific_uploads_operation_idx
    ON fs2_scientific_uploads (operation_id,attempt,begun_at,id);

CREATE TABLE fs2_scientific_result_manifests (
    operation_id uuid PRIMARY KEY REFERENCES fs2_operations(id),
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    attempt integer NOT NULL CHECK (attempt BETWEEN 0 AND 10),
    manifest_digest char(71) NOT NULL CHECK (manifest_digest ~ '^sha256:[a-f0-9]{64}$'),
    status text NOT NULL CHECK (status IN ('succeeded','failed','cancelled','preempted','expired')),
    semantic_validation_status text NOT NULL CHECK (semantic_validation_status IN ('passed','failed','not_run')),
    manifest jsonb NOT NULL CHECK (
        jsonb_typeof(manifest)='object'
        AND octet_length(manifest::text) BETWEEN 2 AND 1048576
        AND manifest->>'schema_version'='fs2-serve.nebius.ai/scientific-result-record/v1'
        AND manifest->>'operation_id'=operation_id::text
        AND manifest->>'tenant_id'=tenant_id
        AND (manifest->>'attempt')::integer=attempt
        AND manifest->>'manifest_digest'=manifest_digest
        AND manifest->>'status'=status
        AND manifest->'validation'->>'status'=semantic_validation_status
    ),
    completed_at timestamptz NOT NULL,
    committed_at timestamptz NOT NULL,
    UNIQUE (operation_id,manifest_digest),
    CHECK (committed_at >= completed_at)
);

CREATE TABLE fs2_scientific_artifact_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type text NOT NULL CHECK (event_type IN ('upload_begun','artifact_finalized','result_committed')),
    operation_id uuid NOT NULL REFERENCES fs2_operations(id),
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    attempt integer NOT NULL CHECK (attempt BETWEEN 0 AND 10),
    upload_id uuid,
    artifact_id uuid,
    manifest_digest char(71),
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (upload_id,operation_id,tenant_id,attempt)
        REFERENCES fs2_scientific_uploads(id,operation_id,tenant_id,attempt),
    FOREIGN KEY (artifact_id,operation_id,tenant_id,attempt)
        REFERENCES fs2_scientific_artifacts(id,operation_id,tenant_id,attempt),
    FOREIGN KEY (operation_id,manifest_digest)
        REFERENCES fs2_scientific_result_manifests(operation_id,manifest_digest),
    CHECK (
        (event_type='upload_begun' AND upload_id IS NOT NULL
            AND artifact_id IS NULL AND manifest_digest IS NULL)
        OR (event_type='artifact_finalized' AND upload_id IS NOT NULL
            AND artifact_id IS NOT NULL AND manifest_digest IS NULL)
        OR (event_type='result_committed' AND upload_id IS NULL
            AND artifact_id IS NULL AND manifest_digest IS NOT NULL)
    )
);

CREATE INDEX fs2_scientific_artifact_events_operation_idx
    ON fs2_scientific_artifact_events (operation_id,occurred_at,id);

CREATE FUNCTION fs2_scientific_assert_current_attempt() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    current_tenant text;
    current_attempt integer;
BEGIN
    SELECT tenant_id,attempt INTO current_tenant,current_attempt
    FROM fs2_operations
    WHERE id=NEW.operation_id
    FOR SHARE;
    IF NOT FOUND OR current_tenant<>NEW.tenant_id OR current_attempt<>NEW.attempt THEN
        RAISE EXCEPTION USING
            ERRCODE='FS201',
            MESSAGE='stale scientific artifact attempt';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION fs2_scientific_validate_upload_transition() RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF OLD.operation_id IS DISTINCT FROM NEW.operation_id
       OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.attempt IS DISTINCT FROM NEW.attempt
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
        RAISE EXCEPTION USING
            ERRCODE='FS202',
            MESSAGE='invalid scientific upload transition';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION fs2_scientific_reject_mutation() RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE='FS202',
        MESSAGE='immutable scientific artifact record';
END
$function$;

CREATE TRIGGER fs2_scientific_uploads_attempt_fence
BEFORE INSERT OR UPDATE ON fs2_scientific_uploads
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_assert_current_attempt();

CREATE TRIGGER fs2_scientific_artifacts_attempt_fence
BEFORE INSERT ON fs2_scientific_artifacts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_assert_current_attempt();

CREATE TRIGGER fs2_scientific_results_attempt_fence
BEFORE INSERT ON fs2_scientific_result_manifests
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_assert_current_attempt();

CREATE TRIGGER fs2_scientific_events_attempt_fence
BEFORE INSERT ON fs2_scientific_artifact_events
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_assert_current_attempt();

CREATE TRIGGER fs2_scientific_upload_transition
BEFORE UPDATE ON fs2_scientific_uploads
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_validate_upload_transition();

CREATE TRIGGER fs2_scientific_artifacts_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_artifacts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_result_manifests_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_result_manifests
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_artifact_events_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_artifact_events
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

REVOKE ALL ON fs2_scientific_artifacts,fs2_scientific_uploads,
    fs2_scientific_result_manifests,fs2_scientific_artifact_events FROM PUBLIC;
REVOKE ALL ON SEQUENCE fs2_scientific_artifact_events_id_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_assert_current_attempt(),
    fs2_scientific_validate_upload_transition(),fs2_scientific_reject_mutation() FROM PUBLIC;

COMMENT ON TABLE fs2_scientific_artifacts IS
    'Immutable content addresses only; scientific bytes and signed handles remain in object storage';
COMMENT ON TABLE fs2_scientific_uploads IS
    'Attempt-fenced upload expectations and finalized references; signed upload handles are never persisted';
COMMENT ON TABLE fs2_scientific_result_manifests IS
    'Exactly one immutable payload-free terminal scientific result manifest per operation';
COMMENT ON TABLE fs2_scientific_artifact_events IS
    'Append-only closed event identities with no free-form detail, credentials, URLs, or biological payloads';

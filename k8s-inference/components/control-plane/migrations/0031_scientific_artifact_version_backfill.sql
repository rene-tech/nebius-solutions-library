-- One-way, evidence-backed transition for artifacts created before migration
-- 0030 captured immutable provider object versions.
--
-- Backfill is deliberately not an ordinary UPDATE privilege.  The maintenance
-- role can invoke the security-definer function only; the function accepts a
-- version after every immutable artifact field has been independently proved,
-- writes an append-only receipt, and opens the immutable trigger for that one
-- row/transaction.  A partially backfilled operation remains purge-fenced by
-- the runtime until every artifact has an exact version.

-- Online executor reads are admitted from a customer request before the
-- worker lease exists. Persist the schema-selected artifact membership in the
-- same transaction as the Operation so the isolated issuer can derive read
-- authority without decrypting a payload or trusting a caller-supplied ID.
-- The Operation cascade bounds retention; the artifact reference deliberately
-- delays producer-artifact purge until every consuming Operation is gone.
CREATE TABLE fs2_operation_admission_authorities (
    operation_id uuid PRIMARY KEY REFERENCES fs2_operations(id) ON DELETE CASCADE,
    token_id uuid NOT NULL REFERENCES fs2_tokens(id),
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    model_id text NOT NULL CHECK (length(model_id) BETWEEN 1 AND 128),
    protocol text NOT NULL CHECK (length(protocol) BETWEEN 1 AND 64),
    operation text NOT NULL CHECK (length(operation) BETWEEN 1 AND 128),
    required_scope text NOT NULL CHECK (required_scope IN ('inference.invoke','mcp.invoke')),
    request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    authority_token text NOT NULL CHECK (
        length(authority_token) BETWEEN 1 AND 16384
        AND authority_token LIKE 'fs2_operation_admission.%'
    ),
    admitted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

REVOKE ALL ON fs2_operation_admission_authorities FROM PUBLIC;

COMMENT ON TABLE fs2_operation_admission_authorities IS
    'Issuer-signed durable PAT root for controller, executor and workload artifact authority';

-- Keep enrollment open across the rolling application deployment.  The old
-- gateway can continue admitting requests while this migration and the new
-- issuer are installed; database-owner triggers capture those requests before
-- commit.  A distinct cutover workload closes the bridge only after the new
-- gateway rollout has drained.  Once closed, a deferred constraint rejects
-- every operation that has neither an issuer signature nor a captured legacy
-- enrollment, so an old gateway cannot silently reappear after cutover.
CREATE TABLE fs2_artifact_authority_legacy_cutovers (
    singleton smallint PRIMARY KEY CHECK (singleton=1),
    opened_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    closed_at timestamptz,
    enrolled_operation_count bigint CHECK (
        enrolled_operation_count IS NULL OR enrolled_operation_count >= 0
    ),
    enrolled_input_count bigint CHECK (
        enrolled_input_count IS NULL OR enrolled_input_count >= 0
    ),
    CHECK (
        (closed_at IS NULL AND enrolled_operation_count IS NULL AND enrolled_input_count IS NULL)
        OR (closed_at IS NOT NULL AND closed_at >= opened_at
            AND enrolled_operation_count IS NOT NULL AND enrolled_input_count IS NOT NULL)
    )
);

INSERT INTO fs2_artifact_authority_legacy_cutovers(singleton) VALUES (1);

REVOKE ALL ON fs2_artifact_authority_legacy_cutovers FROM PUBLIC;

COMMENT ON TABLE fs2_artifact_authority_legacy_cutovers IS
    'One-way independently authenticated overlap gate for the SAI-19 authority rollout';

-- Snapshot the exact already-admitted rows before the new issuer contract is
-- enforced.  The runtime role receives no privilege on this table and cannot
-- add or alter candidates.  This is a bounded compatibility bridge only for
-- operations admitted before the independently authenticated bridge closure;
-- all later operations require an issuer signature in
-- fs2_operation_admission_authorities.
CREATE TABLE fs2_operation_admission_legacy_candidates (
    operation_id uuid PRIMARY KEY REFERENCES fs2_operations(id) ON DELETE CASCADE,
    cutover_id smallint NOT NULL DEFAULT 1 REFERENCES fs2_artifact_authority_legacy_cutovers(singleton),
    token_id uuid NOT NULL REFERENCES fs2_tokens(id),
    tenant_id text NOT NULL,
    model_id text NOT NULL,
    protocol text NOT NULL,
    operation text NOT NULL,
    authorized_invoke_scopes text[] NOT NULL CHECK (
        cardinality(authorized_invoke_scopes) BETWEEN 1 AND 2
        AND authorized_invoke_scopes <@ ARRAY['inference.invoke','mcp.invoke']::text[]
    ),
    request_hmac_key_id text NOT NULL,
    request_hmac char(64) NOT NULL CHECK (request_hmac ~ '^[a-f0-9]{64}$'),
    accepted_at timestamptz NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO fs2_operation_admission_legacy_candidates (
    operation_id,token_id,tenant_id,model_id,protocol,operation,authorized_invoke_scopes,
    request_hmac_key_id,request_hmac,accepted_at
)
SELECT operation.id,operation.token_id,operation.tenant_id,operation.model_id,
       operation.protocol,operation.operation,scope.authorized_invoke_scopes,
       operation.request_hmac_key_id,operation.request_hmac,operation.accepted_at
FROM fs2_operations operation
JOIN fs2_tokens token ON token.id=operation.token_id
CROSS JOIN LATERAL (
    SELECT array_agg(value ORDER BY value) AS authorized_invoke_scopes
    FROM unnest(token.scopes) value
    WHERE value IN ('inference.invoke','mcp.invoke')
) scope
WHERE (
      operation.status IN ('queued','activating','running')
      OR EXISTS (
          SELECT 1 FROM fs2_scientific_batches batch
          WHERE batch.operation_id=operation.id
      )
  )
  AND cardinality(scope.authorized_invoke_scopes) >= 1
  AND ('*'=ANY(token.models) OR operation.model_id=ANY(token.models));

REVOKE ALL ON fs2_operation_admission_legacy_candidates FROM PUBLIC;

COMMENT ON TABLE fs2_operation_admission_legacy_candidates IS
    'Database-owner-captured immutable enrollment for operations admitted before SAI-19 authority cutover';

-- Freeze the exact scientific input set for every enrolled operation.  This
-- table is populated only by database-owner migration/trigger code and is not
-- writable by the gateway role.  It therefore preserves already accepted
-- queued/running batches without allowing a compromised gateway to invent a
-- same-tenant artifact binding.
CREATE TABLE fs2_operation_admission_legacy_inputs (
    operation_id uuid NOT NULL REFERENCES fs2_operation_admission_legacy_candidates(operation_id)
        ON DELETE CASCADE,
    tenant_id text NOT NULL,
    artifact_id uuid NOT NULL REFERENCES fs2_scientific_artifacts(id),
    producer_operation_id uuid NOT NULL REFERENCES fs2_operations(id),
    source_kind text NOT NULL CHECK (source_kind IN ('manifest','entry')),
    logical_artifact_id text,
    object_version_id text NOT NULL CHECK (
        length(object_version_id) BETWEEN 1 AND 1024
        AND object_version_id ~ '^[A-Za-z0-9._~+=/-]+$'
        AND object_version_id NOT IN ('null','unpersisted')
    ),
    digest char(71) NOT NULL CHECK (digest ~ '^sha256:[a-f0-9]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes BETWEEN 0 AND 1099511627776),
    media_type text NOT NULL CHECK (length(media_type) BETWEEN 3 AND 128),
    compression text CHECK (compression IN ('gzip','zstd')),
    captured_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    PRIMARY KEY (operation_id,artifact_id),
    CHECK (
        (source_kind='manifest' AND logical_artifact_id IS NULL)
        OR (source_kind='entry' AND logical_artifact_id IS NOT NULL)
    )
);

REVOKE ALL ON fs2_operation_admission_legacy_inputs FROM PUBLIC;

COMMENT ON TABLE fs2_operation_admission_legacy_inputs IS
    'Database-owner snapshot of exact immutable artifact inputs for pre-cutover scientific operations';

CREATE FUNCTION fs2_capture_artifact_authority_legacy_inputs(requested_operation_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    -- The manifest artifact is independently fixed by the immutable batch
    -- input_artifact_id even for old state codecs that did not carry a parsed
    -- input_manifest projection.
    WITH admission AS (
        SELECT batch.operation_id,batch.tenant_id,batch.input_artifact_id,batch.state
        FROM fs2_scientific_batches batch
        WHERE batch.operation_id=requested_operation_id
        UNION ALL
        SELECT outbox.operation_id,operation.tenant_id,
               (outbox.payload->>'input_artifact_id')::uuid,outbox.payload
        FROM fs2_scientific_admission_outbox outbox
        JOIN fs2_operations operation ON operation.id=outbox.operation_id
        WHERE outbox.operation_id=requested_operation_id
          AND NOT EXISTS (
              SELECT 1 FROM fs2_scientific_batches batch
              WHERE batch.operation_id=outbox.operation_id
          )
    )
    INSERT INTO fs2_operation_admission_legacy_inputs (
        operation_id,tenant_id,artifact_id,producer_operation_id,source_kind,
        logical_artifact_id,object_version_id,digest,size_bytes,media_type,compression
    )
    SELECT candidate.operation_id,admission.tenant_id,artifact.id,artifact.operation_id,
           'manifest',NULL,artifact.object_version_id,artifact.digest,
           artifact.size_bytes,artifact.media_type,artifact.compression
    FROM fs2_operation_admission_legacy_candidates candidate
    JOIN admission ON admission.operation_id=candidate.operation_id
    JOIN fs2_scientific_artifacts artifact ON artifact.id=admission.input_artifact_id
    WHERE candidate.operation_id=requested_operation_id
      AND artifact.tenant_id=admission.tenant_id
      AND artifact.object_version_id IS NOT NULL
      AND (
          admission.state->'input_manifest' IS NULL
          OR artifact.id::text=admission.state#>>'{input_manifest,manifest_artifact_id}'
      )
      AND (
          admission.state->'input_manifest' IS NULL
          OR artifact.digest=admission.state#>>'{input_manifest,manifest_digest}'
      )
    ON CONFLICT (operation_id,artifact_id) DO NOTHING;

    -- Every parsed entry must match the immutable provider version and all
    -- metadata retained in the frozen batch state.  A missing/mismatched row
    -- is left unresolved so the close function below refuses cutover.
    WITH admission AS (
        SELECT batch.operation_id,batch.tenant_id,batch.state
        FROM fs2_scientific_batches batch
        WHERE batch.operation_id=requested_operation_id
        UNION ALL
        SELECT outbox.operation_id,operation.tenant_id,outbox.payload
        FROM fs2_scientific_admission_outbox outbox
        JOIN fs2_operations operation ON operation.id=outbox.operation_id
        WHERE outbox.operation_id=requested_operation_id
          AND NOT EXISTS (
              SELECT 1 FROM fs2_scientific_batches batch
              WHERE batch.operation_id=outbox.operation_id
          )
    )
    INSERT INTO fs2_operation_admission_legacy_inputs (
        operation_id,tenant_id,artifact_id,producer_operation_id,source_kind,
        logical_artifact_id,object_version_id,digest,size_bytes,media_type,compression
    )
    SELECT candidate.operation_id,admission.tenant_id,artifact.id,artifact.operation_id,
           'entry',entry.value->>'logical_artifact_id',artifact.object_version_id,
           artifact.digest,artifact.size_bytes,artifact.media_type,artifact.compression
    FROM fs2_operation_admission_legacy_candidates candidate
    JOIN admission ON admission.operation_id=candidate.operation_id
    CROSS JOIN LATERAL jsonb_array_elements(
        COALESCE(admission.state#>'{input_manifest,entries}','[]'::jsonb)
    ) entry(value)
    JOIN fs2_scientific_artifacts artifact
      ON artifact.id=(entry.value->>'artifact_id')::uuid
     AND artifact.tenant_id=admission.tenant_id
     AND artifact.object_version_id IS NOT NULL
     AND artifact.digest=entry.value->>'digest'
     AND artifact.size_bytes=(entry.value->>'size_bytes')::bigint
     AND artifact.media_type=entry.value->>'media_type'
     AND artifact.compression IS NOT DISTINCT FROM NULLIF(entry.value->>'compression','none')
    WHERE candidate.operation_id=requested_operation_id
    ON CONFLICT (operation_id,artifact_id) DO NOTHING;
END
$function$;

REVOKE ALL ON FUNCTION fs2_capture_artifact_authority_legacy_inputs(uuid) FROM PUBLIC;

SELECT fs2_capture_artifact_authority_legacy_inputs(operation_id)
FROM fs2_operation_admission_legacy_candidates;

CREATE FUNCTION fs2_capture_artifact_authority_legacy_operation() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    enrollment_open boolean;
    invoke_scopes text[];
BEGIN
    SELECT closed_at IS NULL INTO enrollment_open
    FROM fs2_artifact_authority_legacy_cutovers
    WHERE singleton=1
    FOR SHARE;
    IF enrollment_open THEN
        SELECT array_agg(value ORDER BY value) INTO invoke_scopes
        FROM fs2_tokens token
        CROSS JOIN LATERAL unnest(token.scopes) value
        WHERE token.id=NEW.token_id
          AND value IN ('inference.invoke','mcp.invoke')
          AND ('*'=ANY(token.models) OR NEW.model_id=ANY(token.models));
        IF cardinality(invoke_scopes) IS NULL OR cardinality(invoke_scopes) < 1 THEN
            RETURN NEW;
        END IF;
        INSERT INTO fs2_operation_admission_legacy_candidates (
            operation_id,token_id,tenant_id,model_id,protocol,operation,authorized_invoke_scopes,
            request_hmac_key_id,request_hmac,accepted_at
        ) VALUES (
            NEW.id,NEW.token_id,NEW.tenant_id,NEW.model_id,NEW.protocol,NEW.operation,
            invoke_scopes,
            NEW.request_hmac_key_id,NEW.request_hmac,NEW.accepted_at
        ) ON CONFLICT (operation_id) DO NOTHING;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_capture_artifact_authority_legacy_operation_trigger
AFTER INSERT ON fs2_operations
FOR EACH ROW EXECUTE FUNCTION fs2_capture_artifact_authority_legacy_operation();

CREATE FUNCTION fs2_capture_artifact_authority_legacy_batch() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    PERFORM fs2_capture_artifact_authority_legacy_inputs(NEW.operation_id);
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_capture_artifact_authority_legacy_batch_trigger
AFTER INSERT ON fs2_scientific_batches
FOR EACH ROW EXECUTE FUNCTION fs2_capture_artifact_authority_legacy_batch();

CREATE TRIGGER fs2_capture_artifact_authority_legacy_outbox_trigger
AFTER INSERT ON fs2_scientific_admission_outbox
FOR EACH ROW EXECUTE FUNCTION fs2_capture_artifact_authority_legacy_batch();

REVOKE ALL ON FUNCTION fs2_capture_artifact_authority_legacy_operation(),
    fs2_capture_artifact_authority_legacy_batch() FROM PUBLIC;

CREATE FUNCTION fs2_enforce_artifact_authority_cutover() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF EXISTS (
        SELECT 1 FROM fs2_artifact_authority_legacy_cutovers
        WHERE singleton=1 AND closed_at IS NOT NULL
    ) AND NOT EXISTS (
        SELECT 1 FROM fs2_operation_admission_authorities root
        WHERE root.operation_id=NEW.id
    ) AND NOT EXISTS (
        SELECT 1 FROM fs2_operation_admission_legacy_candidates candidate
        WHERE candidate.operation_id=NEW.id
    ) THEN
        RAISE EXCEPTION USING ERRCODE='FS205',
            MESSAGE='operation has no independently custodied artifact admission authority';
    END IF;
    RETURN NEW;
END
$function$;

CREATE CONSTRAINT TRIGGER fs2_operations_artifact_authority_cutover
AFTER INSERT ON fs2_operations
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION fs2_enforce_artifact_authority_cutover();

REVOKE ALL ON FUNCTION fs2_enforce_artifact_authority_cutover() FROM PUBLIC;

CREATE FUNCTION fs2_close_artifact_authority_legacy_enrollment()
RETURNS TABLE (
    cutover_closed_at timestamptz,
    enrolled_operation_count bigint,
    enrolled_input_count bigint
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    gate fs2_artifact_authority_legacy_cutovers%ROWTYPE;
    operation_total bigint;
    input_total bigint;
BEGIN
    SELECT * INTO gate
    FROM fs2_artifact_authority_legacy_cutovers
    WHERE singleton=1
    FOR UPDATE;
    IF gate.closed_at IS NOT NULL THEN
        RETURN QUERY SELECT gate.closed_at,gate.enrolled_operation_count,gate.enrolled_input_count;
        RETURN;
    END IF;

    -- This is the serialized second snapshot. Operation INSERT triggers take
    -- a shared lock on the same gate row, so no old admission can cross this
    -- watermark without either being enrolled here or being rejected by the
    -- deferred constraint after closure.
    INSERT INTO fs2_operation_admission_legacy_candidates (
        operation_id,token_id,tenant_id,model_id,protocol,operation,
        authorized_invoke_scopes,request_hmac_key_id,request_hmac,accepted_at
    )
    SELECT operation.id,operation.token_id,operation.tenant_id,operation.model_id,
           operation.protocol,operation.operation,scope.authorized_invoke_scopes,
           operation.request_hmac_key_id,operation.request_hmac,operation.accepted_at
    FROM fs2_operations operation
    JOIN fs2_tokens token ON token.id=operation.token_id
    CROSS JOIN LATERAL (
        SELECT array_agg(value ORDER BY value) AS authorized_invoke_scopes
        FROM unnest(token.scopes) value
        WHERE value IN ('inference.invoke','mcp.invoke')
    ) scope
    LEFT JOIN fs2_operation_admission_authorities root ON root.operation_id=operation.id
    LEFT JOIN fs2_operation_admission_legacy_candidates candidate ON candidate.operation_id=operation.id
    WHERE root.operation_id IS NULL
      AND candidate.operation_id IS NULL
      AND (
          operation.accepted_at >= gate.opened_at
          OR operation.status IN ('queued','activating','running')
          OR EXISTS (
              SELECT 1 FROM fs2_scientific_batches batch
              WHERE batch.operation_id=operation.id
          )
          OR EXISTS (
              SELECT 1 FROM fs2_scientific_admission_outbox outbox
              WHERE outbox.operation_id=operation.id
          )
      )
      AND cardinality(scope.authorized_invoke_scopes) >= 1
      AND ('*'=ANY(token.models) OR operation.model_id=ANY(token.models));

    PERFORM fs2_capture_artifact_authority_legacy_inputs(operation_id)
    FROM fs2_operation_admission_legacy_candidates;

    IF EXISTS (
        SELECT 1
        FROM fs2_operations operation
        LEFT JOIN fs2_operation_admission_authorities root ON root.operation_id=operation.id
        LEFT JOIN fs2_operation_admission_legacy_candidates candidate ON candidate.operation_id=operation.id
        WHERE root.operation_id IS NULL
          AND candidate.operation_id IS NULL
          AND (
              operation.accepted_at >= gate.opened_at
              OR operation.status IN ('queued','activating','running')
              OR EXISTS (
                  SELECT 1 FROM fs2_scientific_batches batch
                  WHERE batch.operation_id=operation.id
              )
              OR EXISTS (
                  SELECT 1 FROM fs2_scientific_admission_outbox outbox
                  WHERE outbox.operation_id=operation.id
              )
          )
    ) THEN
        RAISE EXCEPTION USING ERRCODE='FS205',
            MESSAGE='legacy artifact authority enrollment is incomplete';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM fs2_operation_admission_legacy_candidates candidate
        JOIN fs2_operations operation ON operation.id=candidate.operation_id
        JOIN fs2_tokens token ON token.id=candidate.token_id
        WHERE ROW(
            operation.token_id,operation.tenant_id,operation.model_id,operation.protocol,
            operation.operation,operation.request_hmac_key_id,operation.request_hmac,
            operation.accepted_at
        ) IS DISTINCT FROM ROW(
            candidate.token_id,candidate.tenant_id,candidate.model_id,candidate.protocol,
            candidate.operation,candidate.request_hmac_key_id,candidate.request_hmac,
            candidate.accepted_at
        )
           OR NOT candidate.authorized_invoke_scopes <@ token.scopes
           OR NOT ('*'=ANY(token.models) OR candidate.model_id=ANY(token.models))
    ) THEN
        RAISE EXCEPTION USING ERRCODE='FS205',
            MESSAGE='legacy artifact authority enrollment differs from durable authorization';
    END IF;

    -- Every pre-cutover scientific admission must have a captured manifest
    -- and every frozen manifest entry must have an exact metadata/version row.
    IF EXISTS (
        WITH admission AS (
            SELECT batch.operation_id,batch.tenant_id,batch.input_artifact_id,batch.state
            FROM fs2_scientific_batches batch
            UNION ALL
            SELECT outbox.operation_id,operation.tenant_id,
                   (outbox.payload->>'input_artifact_id')::uuid,outbox.payload
            FROM fs2_scientific_admission_outbox outbox
            JOIN fs2_operations operation ON operation.id=outbox.operation_id
            WHERE NOT EXISTS (
                SELECT 1 FROM fs2_scientific_batches batch
                WHERE batch.operation_id=outbox.operation_id
            )
        )
        SELECT 1
        FROM admission
        JOIN fs2_operation_admission_legacy_candidates candidate
          ON candidate.operation_id=admission.operation_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM fs2_operation_admission_legacy_inputs legacy
            JOIN fs2_scientific_artifacts artifact ON artifact.id=legacy.artifact_id
            WHERE legacy.operation_id=admission.operation_id
              AND legacy.tenant_id=admission.tenant_id
              AND legacy.artifact_id=admission.input_artifact_id
              AND legacy.source_kind='manifest'
              AND artifact.tenant_id=legacy.tenant_id
              AND artifact.operation_id=legacy.producer_operation_id
              AND artifact.object_version_id=legacy.object_version_id
              AND artifact.digest=legacy.digest
              AND artifact.size_bytes=legacy.size_bytes
              AND artifact.media_type=legacy.media_type
              AND artifact.compression IS NOT DISTINCT FROM legacy.compression
        ) OR EXISTS (
            SELECT 1
            FROM jsonb_array_elements(
                COALESCE(admission.state#>'{input_manifest,entries}','[]'::jsonb)
            ) entry(value)
            WHERE NOT EXISTS (
                SELECT 1
                FROM fs2_operation_admission_legacy_inputs legacy
                JOIN fs2_scientific_artifacts artifact ON artifact.id=legacy.artifact_id
                WHERE legacy.operation_id=admission.operation_id
                  AND legacy.tenant_id=admission.tenant_id
                  AND legacy.artifact_id=(entry.value->>'artifact_id')::uuid
                  AND legacy.source_kind='entry'
                  AND legacy.logical_artifact_id=entry.value->>'logical_artifact_id'
                  AND artifact.tenant_id=legacy.tenant_id
                  AND artifact.operation_id=legacy.producer_operation_id
                  AND artifact.object_version_id=legacy.object_version_id
                  AND artifact.digest=legacy.digest
                  AND artifact.digest=entry.value->>'digest'
                  AND artifact.size_bytes=legacy.size_bytes
                  AND artifact.size_bytes=(entry.value->>'size_bytes')::bigint
                  AND artifact.media_type=legacy.media_type
                  AND artifact.media_type=entry.value->>'media_type'
                  AND artifact.compression IS NOT DISTINCT FROM legacy.compression
                  AND artifact.compression IS NOT DISTINCT FROM NULLIF(entry.value->>'compression','none')
            )
        )
    ) THEN
        RAISE EXCEPTION USING ERRCODE='FS205',
            MESSAGE='legacy scientific artifact input enrollment is incomplete';
    END IF;

    SELECT count(*) INTO operation_total
    FROM fs2_operation_admission_legacy_candidates;
    SELECT count(*) INTO input_total
    FROM fs2_operation_admission_legacy_inputs;
    UPDATE fs2_artifact_authority_legacy_cutovers
    SET closed_at=clock_timestamp(),
        enrolled_operation_count=operation_total,
        enrolled_input_count=input_total
    WHERE singleton=1 AND closed_at IS NULL
    RETURNING closed_at INTO gate.closed_at;
    IF gate.closed_at IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='FS205', MESSAGE='artifact authority cutover raced';
    END IF;
    RETURN QUERY SELECT gate.closed_at,operation_total,input_total;
END
$function$;

REVOKE ALL ON FUNCTION fs2_close_artifact_authority_legacy_enrollment() FROM PUBLIC;

COMMENT ON FUNCTION fs2_close_artifact_authority_legacy_enrollment() IS
    'Independently invoked one-way watermark that snapshots overlap admissions and fences unsigned successors';

CREATE TABLE fs2_operation_artifact_inputs (
    operation_id uuid NOT NULL REFERENCES fs2_operations(id) ON DELETE CASCADE,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    artifact_id uuid NOT NULL REFERENCES fs2_scientific_artifacts(id),
    authority_token text NOT NULL CHECK (
        length(authority_token) BETWEEN 1 AND 16384
        AND authority_token LIKE 'fs2_artifact_admission.%'
    ),
    bound_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (operation_id,artifact_id)
);

REVOKE ALL ON fs2_operation_artifact_inputs FROM PUBLIC;

CREATE FUNCTION fs2_validate_operation_artifact_input() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM fs2_operations operation
        JOIN fs2_scientific_artifacts artifact ON artifact.id=NEW.artifact_id
        WHERE operation.id=NEW.operation_id
          AND operation.tenant_id=NEW.tenant_id
          AND artifact.tenant_id=NEW.tenant_id
          AND artifact.object_version_id IS NOT NULL
    ) THEN
        RAISE EXCEPTION USING ERRCODE='FS201',
            MESSAGE='operation artifact input is not a finalized tenant artifact';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_operation_artifact_inputs_scope
BEFORE INSERT ON fs2_operation_artifact_inputs
FOR EACH ROW EXECUTE FUNCTION fs2_validate_operation_artifact_input();

COMMENT ON TABLE fs2_operation_artifact_inputs IS
    'Immutable admission-time mapping carrying an issuer-signed PAT and exact-version artifact attestation';

CREATE VIEW fs2_required_artifact_authority_keys
WITH (security_barrier=true)
AS
SELECT DISTINCT split_part(authority_token,'.',2) AS key_id
FROM fs2_operation_admission_authorities
WHERE split_part(authority_token,'.',2) ~ '^[a-f0-9]{16}$'
UNION
SELECT DISTINCT split_part(authority_token,'.',2) AS key_id
FROM fs2_operation_artifact_inputs
WHERE split_part(authority_token,'.',2) ~ '^[a-f0-9]{16}$';

REVOKE ALL ON fs2_required_artifact_authority_keys FROM PUBLIC;

COMMENT ON VIEW fs2_required_artifact_authority_keys IS
    'Public-key IDs still required by durable operation roots/input receipts; key retirement is blocked while listed';

ALTER TABLE fs2_scientific_uploads
    ADD CONSTRAINT fs2_scientific_uploads_version_scope_unique
    UNIQUE (id,operation_id,tenant_id);

CREATE TABLE fs2_scientific_upload_object_versions (
    -- This is retained provider-custody evidence, not mutable upload state.
    -- It deliberately has no FK to fs2_scientific_uploads: operation retention
    -- may remove the upload row after its policy window, while exact-version
    -- and orphan-quarantine evidence must remain append-only.
    upload_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    storage_key text NOT NULL CHECK (length(storage_key) BETWEEN 1 AND 1024),
    object_version_id text NOT NULL CHECK (
        length(object_version_id) BETWEEN 1 AND 1024
        AND object_version_id ~ '^[A-Za-z0-9._~+=/-]+$'
        AND object_version_id NOT IN ('null','unpersisted')
    ),
    expected_digest char(71) NOT NULL CHECK (expected_digest ~ '^sha256:[a-f0-9]{64}$'),
    expected_size_bytes bigint NOT NULL CHECK (expected_size_bytes BETWEEN 0 AND 1099511627776),
    expected_media_type text NOT NULL CHECK (length(expected_media_type) BETWEEN 3 AND 128),
    expected_compression text CHECK (expected_compression IN ('gzip','zstd')),
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    UNIQUE (storage_key,object_version_id),
    CHECK (observed_at < expires_at)
);

CREATE INDEX fs2_scientific_upload_object_versions_expiry_idx
    ON fs2_scientific_upload_object_versions (expires_at,operation_id,tenant_id);

REVOKE ALL ON fs2_scientific_upload_object_versions FROM PUBLIC;

CREATE FUNCTION fs2_validate_scientific_upload_object_version() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM fs2_scientific_uploads upload
        WHERE upload.id=NEW.upload_id
          AND upload.operation_id=NEW.operation_id
          AND upload.tenant_id=NEW.tenant_id
          AND upload.storage_key=NEW.storage_key
          AND upload.expected_digest=NEW.expected_digest
          AND upload.expected_size_bytes=NEW.expected_size_bytes
          AND upload.media_type=NEW.expected_media_type
          AND upload.compression IS NOT DISTINCT FROM NEW.expected_compression
    ) THEN
        RAISE EXCEPTION USING ERRCODE='FS201',
            MESSAGE='upload object version is not bound to an exact live reservation';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_scientific_upload_object_versions_scope
BEFORE INSERT ON fs2_scientific_upload_object_versions
FOR EACH ROW EXECUTE FUNCTION fs2_validate_scientific_upload_object_version();

CREATE TRIGGER fs2_scientific_upload_object_versions_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_upload_object_versions
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_guard_retention_delete();

COMMENT ON TABLE fs2_scientific_upload_object_versions IS
    'Detached durable exact-version orphan ledger inserted only after provider inspection or provider-authenticated inline PUT and retained through operation purge';

CREATE TABLE fs2_scientific_artifact_version_backfills (
    artifact_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    storage_key text NOT NULL CHECK (length(storage_key) BETWEEN 1 AND 1024),
    object_version_id text NOT NULL CHECK (
        length(object_version_id) BETWEEN 1 AND 1024
        AND object_version_id ~ '^[A-Za-z0-9._~+=/-]+$'
        AND object_version_id NOT IN ('null','unpersisted')
    ),
    verified_digest char(71) NOT NULL CHECK (verified_digest ~ '^sha256:[a-f0-9]{64}$'),
    verified_size_bytes bigint NOT NULL CHECK (verified_size_bytes BETWEEN 0 AND 1099511627776),
    verified_media_type text NOT NULL CHECK (length(verified_media_type) BETWEEN 3 AND 128),
    verified_compression text CHECK (verified_compression IN ('gzip','zstd')),
    provider_receipt_digest char(71) NOT NULL CHECK (
        provider_receipt_digest ~ '^sha256:[a-f0-9]{64}$'
    ),
    provider_receipt text NOT NULL CHECK (
        length(provider_receipt) BETWEEN 1 AND 16384
        AND provider_receipt LIKE 'fs2_provider_observation.%'
    ),
    observed_at timestamptz NOT NULL,
    backfilled_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    backfilled_by name NOT NULL DEFAULT current_user,
    UNIQUE (storage_key,object_version_id),
    CHECK (observed_at <= backfilled_at)
);

REVOKE ALL ON fs2_scientific_artifact_version_backfills FROM PUBLIC;

CREATE OR REPLACE FUNCTION fs2_scientific_reject_mutation() RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_TABLE_NAME='fs2_scientific_artifacts'
       AND COALESCE(current_setting('fs2.object_version_backfill', true),'off')='on'
       AND OLD.object_version_id IS NULL
       AND NEW.object_version_id IS NOT NULL
       AND ROW(
           OLD.id,OLD.attempt_id,OLD.operation_id,OLD.tenant_id,OLD.stage_id,OLD.shard_id,
           OLD.direction,OLD.digest,OLD.size_bytes,OLD.media_type,OLD.compression,
           OLD.storage_key,OLD.access_profile,OLD.access_receipt_digest,
           OLD.retention_expires_at,OLD.created_at
       ) IS NOT DISTINCT FROM ROW(
           NEW.id,NEW.attempt_id,NEW.operation_id,NEW.tenant_id,NEW.stage_id,NEW.shard_id,
           NEW.direction,NEW.digest,NEW.size_bytes,NEW.media_type,NEW.compression,
           NEW.storage_key,NEW.access_profile,NEW.access_receipt_digest,
           NEW.retention_expires_at,NEW.created_at
       )
       AND EXISTS (
           SELECT 1
           FROM fs2_scientific_artifact_version_backfills b
           WHERE b.artifact_id=NEW.id
             AND b.operation_id=NEW.operation_id
             AND b.tenant_id=NEW.tenant_id
             AND b.storage_key=NEW.storage_key
             AND b.object_version_id=NEW.object_version_id
             AND b.verified_digest=NEW.digest
             AND b.verified_size_bytes=NEW.size_bytes
             AND b.verified_media_type=NEW.media_type
             AND b.verified_compression IS NOT DISTINCT FROM NEW.compression
       ) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='immutable scientific artifact record';
END
$function$;

CREATE FUNCTION fs2_scientific_backfill_object_version(
    requested_artifact_id uuid,
    requested_storage_key text,
    requested_object_version_id text,
    requested_digest char(71),
    requested_size_bytes bigint,
    requested_media_type text,
    requested_compression text,
    requested_provider_receipt text,
    requested_provider_receipt_digest char(71),
    requested_observed_at timestamptz
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    artifact fs2_scientific_artifacts%ROWTYPE;
BEGIN
    SELECT * INTO artifact
    FROM fs2_scientific_artifacts
    WHERE id=requested_artifact_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='FS201', MESSAGE='scientific artifact is unknown';
    END IF;
    IF requested_object_version_id IS NULL
       OR requested_object_version_id IN ('null','unpersisted')
       OR length(requested_object_version_id) NOT BETWEEN 1 AND 1024
       OR requested_object_version_id !~ '^[A-Za-z0-9._~+=/-]+$'
       OR length(requested_provider_receipt) NOT BETWEEN 1 AND 16384
       OR requested_provider_receipt NOT LIKE 'fs2_provider_observation.%'
       OR requested_provider_receipt_digest !~ '^sha256:[a-f0-9]{64}$'
       OR requested_observed_at IS NULL
       OR requested_observed_at > clock_timestamp()
       OR artifact.storage_key IS DISTINCT FROM requested_storage_key
       OR artifact.digest IS DISTINCT FROM requested_digest
       OR artifact.size_bytes IS DISTINCT FROM requested_size_bytes
       OR artifact.media_type IS DISTINCT FROM requested_media_type
       OR artifact.compression IS DISTINCT FROM requested_compression THEN
        RAISE EXCEPTION USING ERRCODE='FS204', MESSAGE='scientific artifact version proof differs';
    END IF;
    IF artifact.object_version_id IS NOT NULL THEN
        IF artifact.object_version_id=requested_object_version_id
           AND EXISTS (
               SELECT 1 FROM fs2_scientific_artifact_version_backfills b
               WHERE b.artifact_id=artifact.id
                 AND b.object_version_id=requested_object_version_id
                 AND b.provider_receipt=requested_provider_receipt
                 AND b.provider_receipt_digest=requested_provider_receipt_digest
           ) THEN
            RETURN;
        END IF;
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='scientific artifact version is already immutable';
    END IF;

    INSERT INTO fs2_scientific_artifact_version_backfills (
        artifact_id,operation_id,tenant_id,storage_key,object_version_id,
        verified_digest,verified_size_bytes,verified_media_type,verified_compression,
        provider_receipt,provider_receipt_digest,observed_at
    ) VALUES (
        artifact.id,artifact.operation_id,artifact.tenant_id,artifact.storage_key,
        requested_object_version_id,requested_digest,requested_size_bytes,
        requested_media_type,requested_compression,requested_provider_receipt,
        requested_provider_receipt_digest,
        requested_observed_at
    );
    PERFORM set_config('fs2.object_version_backfill','on',true);
    UPDATE fs2_scientific_artifacts
    SET object_version_id=requested_object_version_id
    WHERE id=artifact.id AND object_version_id IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='scientific artifact version changed concurrently';
    END IF;
END
$function$;

REVOKE ALL ON FUNCTION fs2_scientific_backfill_object_version(
    uuid,text,text,char(71),bigint,text,text,text,char(71),timestamptz
) FROM PUBLIC;

CREATE TRIGGER fs2_scientific_artifact_version_backfills_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_artifact_version_backfills
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

COMMENT ON TABLE fs2_scientific_artifact_version_backfills IS
    'Append-only proof for the one permitted NULL-to-exact-version artifact transition';
COMMENT ON FUNCTION fs2_scientific_backfill_object_version(
    uuid,text,text,char(71),bigint,text,text,text,char(71),timestamptz
) IS
    'Issuer-only exact-version backfill; metadata and retained signed provider observation must match the immutable artifact row';

-- A public signed PUT can outlive the gateway request that issued it. These
-- standalone, append-only records survive operation-row retention so every
-- abandoned upload has a durable claim, exact-version discovery (when an
-- object exists), and terminal cleanup receipt. They deliberately have no
-- cascading foreign key to mutable operational metadata.
CREATE TABLE fs2_scientific_abandoned_upload_claims (
    upload_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    storage_key text NOT NULL CHECK (length(storage_key) BETWEEN 1 AND 1024),
    eligible_at timestamptz NOT NULL,
    claimed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (storage_key),
    CHECK (eligible_at <= claimed_at)
);

CREATE TABLE fs2_scientific_abandoned_upload_versions (
    upload_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL,
    tenant_id text NOT NULL,
    storage_key text NOT NULL,
    object_version_id text NOT NULL CHECK (
        length(object_version_id) BETWEEN 1 AND 1024
        AND object_version_id ~ '^[A-Za-z0-9._~+=/-]+$'
        AND object_version_id NOT IN ('null','unpersisted')
    ),
    observed_size_bytes bigint NOT NULL CHECK (observed_size_bytes BETWEEN 0 AND 1099511627776),
    observed_media_type text NOT NULL CHECK (length(observed_media_type) BETWEEN 3 AND 128),
    observed_compression text CHECK (observed_compression IN ('gzip','zstd')),
    provider_observed_at timestamptz NOT NULL,
    provider_receipt_digest char(71) NOT NULL CHECK (
        provider_receipt_digest ~ '^sha256:[a-f0-9]{64}$'
    ),
    provider_receipt text NOT NULL CHECK (
        length(provider_receipt) BETWEEN 1 AND 16384
        AND provider_receipt LIKE 'fs2_provider_observation.%'
    ),
    discovered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (storage_key,object_version_id),
    CHECK (provider_observed_at <= discovered_at)
);

CREATE TABLE fs2_scientific_abandoned_upload_attempts (
    id bigserial PRIMARY KEY,
    upload_id uuid NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX fs2_scientific_abandoned_upload_attempts_fairness_idx
    ON fs2_scientific_abandoned_upload_attempts (upload_id,attempted_at DESC);

CREATE TABLE fs2_scientific_abandoned_upload_receipts (
    upload_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL,
    tenant_id text NOT NULL,
    storage_key text NOT NULL,
    outcome text NOT NULL CHECK (
        outcome IN ('exact-version-quarantined','provider-write-fenced-absence')
    ),
    object_version_id text CHECK (
        length(object_version_id) BETWEEN 1 AND 1024
        AND object_version_id ~ '^[A-Za-z0-9._~+=/-]+$'
        AND object_version_id NOT IN ('null','unpersisted')
    ),
    receipt_digest char(71) NOT NULL CHECK (receipt_digest ~ '^sha256:[a-f0-9]{64}$'),
    provider_receipt text NOT NULL CHECK (
        length(provider_receipt) BETWEEN 1 AND 16384
        AND provider_receipt LIKE 'fs2_provider_observation.%'
    ),
    completed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (storage_key),
    CHECK (
        (outcome='exact-version-quarantined' AND object_version_id IS NOT NULL)
        OR (outcome='provider-write-fenced-absence' AND object_version_id IS NOT NULL)
    )
);

CREATE VIEW fs2_scientific_abandoned_upload_dead_letters AS
SELECT claim.upload_id,claim.operation_id,claim.tenant_id,claim.storage_key,
       count(attempt.id)::integer AS attempt_count,
       max(attempt.attempted_at) AS exhausted_at
FROM fs2_scientific_abandoned_upload_claims claim
JOIN fs2_scientific_abandoned_upload_attempts attempt ON attempt.upload_id=claim.upload_id
LEFT JOIN fs2_scientific_abandoned_upload_receipts receipt ON receipt.upload_id=claim.upload_id
WHERE receipt.upload_id IS NULL
GROUP BY claim.upload_id,claim.operation_id,claim.tenant_id,claim.storage_key
HAVING count(attempt.id) >= 16;

REVOKE ALL ON fs2_scientific_abandoned_upload_dead_letters FROM PUBLIC;

REVOKE ALL ON fs2_scientific_abandoned_upload_claims FROM PUBLIC;
REVOKE ALL ON fs2_scientific_abandoned_upload_versions FROM PUBLIC;
REVOKE ALL ON fs2_scientific_abandoned_upload_attempts FROM PUBLIC;
REVOKE ALL ON SEQUENCE fs2_scientific_abandoned_upload_attempts_id_seq FROM PUBLIC;
REVOKE ALL ON fs2_scientific_abandoned_upload_receipts FROM PUBLIC;

CREATE TRIGGER fs2_scientific_abandoned_upload_claims_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_abandoned_upload_claims
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_abandoned_upload_versions_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_abandoned_upload_versions
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_abandoned_upload_attempts_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_abandoned_upload_attempts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_abandoned_upload_receipts_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_abandoned_upload_receipts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE FUNCTION fs2_scientific_claim_abandoned_uploads(
    requested_cutoff timestamptz,
    requested_limit integer
) RETURNS TABLE (
    upload_id uuid,
    operation_id uuid,
    tenant_id text,
    storage_key text,
    expected_digest char(71),
    expected_size_bytes bigint,
    expected_media_type text,
    expected_compression text,
    begun_at timestamptz,
    retention_expires_at timestamptz,
    object_version_id text,
    observed_size_bytes bigint,
    observed_media_type text,
    observed_compression text,
    provider_observed_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF requested_cutoff IS NULL
       OR requested_cutoff > clock_timestamp()-interval '1 hour'
       OR requested_limit NOT BETWEEN 1 AND 500 THEN
        RAISE EXCEPTION USING ERRCODE='FS204', MESSAGE='abandoned upload claim window is invalid';
    END IF;

    WITH candidates AS (
        SELECT upload.id,upload.operation_id,upload.tenant_id,upload.storage_key,upload.begun_at
        FROM fs2_scientific_uploads upload
        LEFT JOIN fs2_scientific_abandoned_upload_claims claim ON claim.upload_id=upload.id
        LEFT JOIN fs2_scientific_abandoned_upload_receipts receipt ON receipt.upload_id=upload.id
        WHERE upload.artifact_id IS NULL
          AND upload.begun_at <= requested_cutoff
          AND claim.upload_id IS NULL
          AND receipt.upload_id IS NULL
        ORDER BY upload.begun_at,upload.id
        FOR UPDATE OF upload SKIP LOCKED
        LIMIT requested_limit
    )
    INSERT INTO fs2_scientific_abandoned_upload_claims (
        upload_id,operation_id,tenant_id,storage_key,eligible_at
    )
    SELECT id,operation_id,tenant_id,storage_key,begun_at+interval '1 hour'
    FROM candidates
    ON CONFLICT ON CONSTRAINT fs2_scientific_abandoned_upload_claims_pkey DO NOTHING;

    RETURN QUERY
    WITH unresolved AS MATERIALIZED (
        SELECT claim.upload_id,claim.operation_id,claim.tenant_id,claim.storage_key,
               upload.expected_digest,upload.expected_size_bytes,upload.media_type,
               upload.compression,upload.begun_at,attempt.retention_expires_at,
               version.object_version_id,discovery.observed_size_bytes,
               discovery.observed_media_type,discovery.observed_compression,
               discovery.provider_observed_at
        FROM fs2_scientific_abandoned_upload_claims claim
        JOIN fs2_scientific_uploads upload ON upload.id=claim.upload_id
        JOIN fs2_scientific_stage_attempts attempt ON attempt.attempt_id=upload.attempt_id
        LEFT JOIN fs2_scientific_upload_object_versions version ON version.upload_id=claim.upload_id
        LEFT JOIN fs2_scientific_abandoned_upload_versions discovery ON discovery.upload_id=claim.upload_id
        LEFT JOIN fs2_scientific_abandoned_upload_receipts receipt ON receipt.upload_id=claim.upload_id
        WHERE receipt.upload_id IS NULL
          AND (
              SELECT count(*) FROM fs2_scientific_abandoned_upload_attempts prior
              WHERE prior.upload_id=claim.upload_id
          ) < 16
        ORDER BY COALESCE((
            SELECT max(retry.attempted_at)
            FROM fs2_scientific_abandoned_upload_attempts retry
            WHERE retry.upload_id=claim.upload_id
        ),claim.claimed_at),claim.upload_id
        LIMIT requested_limit
    ), attempted AS (
        INSERT INTO fs2_scientific_abandoned_upload_attempts (upload_id)
        SELECT unresolved.upload_id FROM unresolved
        WHERE (
            SELECT count(*) FROM fs2_scientific_abandoned_upload_attempts prior
            WHERE prior.upload_id=unresolved.upload_id
        ) < 16
        RETURNING fs2_scientific_abandoned_upload_attempts.upload_id
    )
    SELECT unresolved.*
    FROM unresolved
    ORDER BY unresolved.begun_at,unresolved.upload_id;
END
$function$;

CREATE FUNCTION fs2_scientific_register_abandoned_upload_version(
    requested_upload_id uuid,
    requested_object_version_id text,
    requested_size_bytes bigint,
    requested_media_type text,
    requested_compression text,
    requested_provider_observed_at timestamptz,
    requested_provider_receipt text,
    requested_provider_receipt_digest char(71)
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    upload fs2_scientific_uploads%ROWTYPE;
    expiry timestamptz;
    existing_version text;
BEGIN
    SELECT item,attempt.retention_expires_at INTO upload,expiry
    FROM fs2_scientific_uploads item
    JOIN fs2_scientific_stage_attempts attempt ON attempt.attempt_id=item.attempt_id
    WHERE item.id=requested_upload_id
    FOR UPDATE OF item;
    IF NOT FOUND OR upload.artifact_id IS NOT NULL OR NOT EXISTS (
        SELECT 1 FROM fs2_scientific_abandoned_upload_claims claim
        WHERE claim.upload_id=requested_upload_id
    ) THEN
        RAISE EXCEPTION USING ERRCODE='FS201', MESSAGE='abandoned upload claim is unavailable';
    END IF;
    IF requested_object_version_id IS NULL
       OR requested_object_version_id IN ('null','unpersisted')
       OR length(requested_object_version_id) NOT BETWEEN 1 AND 1024
       OR requested_object_version_id !~ '^[A-Za-z0-9._~+=/-]+$'
       OR requested_size_bytes NOT BETWEEN 0 AND 1099511627776
       OR length(requested_media_type) NOT BETWEEN 3 AND 128
       OR (requested_compression IS NOT NULL AND requested_compression NOT IN ('gzip','zstd'))
       OR requested_provider_observed_at IS NULL
       OR requested_provider_observed_at > clock_timestamp()
       OR length(requested_provider_receipt) NOT BETWEEN 1 AND 16384
       OR requested_provider_receipt NOT LIKE 'fs2_provider_observation.%'
       OR requested_provider_receipt_digest !~ '^sha256:[a-f0-9]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE='FS204', MESSAGE='abandoned upload provider proof is invalid';
    END IF;

    INSERT INTO fs2_scientific_abandoned_upload_versions (
        upload_id,operation_id,tenant_id,storage_key,object_version_id,
        observed_size_bytes,observed_media_type,observed_compression,provider_observed_at,
        provider_receipt,provider_receipt_digest
    ) VALUES (
        upload.id,upload.operation_id,upload.tenant_id,upload.storage_key,
        requested_object_version_id,requested_size_bytes,requested_media_type,
        requested_compression,requested_provider_observed_at,requested_provider_receipt,
        requested_provider_receipt_digest
    ) ON CONFLICT (upload_id) DO NOTHING;

    SELECT object_version_id INTO existing_version
    FROM fs2_scientific_abandoned_upload_versions
    WHERE upload_id=upload.id;
    IF existing_version IS DISTINCT FROM requested_object_version_id THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='abandoned upload version proof conflicts';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM fs2_scientific_abandoned_upload_versions proof
        WHERE proof.upload_id=upload.id
          AND proof.object_version_id=requested_object_version_id
          AND proof.observed_size_bytes=requested_size_bytes
          AND proof.observed_media_type=requested_media_type
          AND proof.observed_compression IS NOT DISTINCT FROM requested_compression
          AND proof.provider_observed_at=requested_provider_observed_at
          AND proof.provider_receipt=requested_provider_receipt
          AND proof.provider_receipt_digest=requested_provider_receipt_digest
    ) THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='abandoned upload provider proof conflicts';
    END IF;

    INSERT INTO fs2_scientific_upload_object_versions (
        upload_id,operation_id,tenant_id,storage_key,object_version_id,
        expected_digest,expected_size_bytes,expected_media_type,expected_compression,
        observed_at,expires_at
    ) VALUES (
        upload.id,upload.operation_id,upload.tenant_id,upload.storage_key,
        requested_object_version_id,upload.expected_digest,upload.expected_size_bytes,
        upload.media_type,upload.compression,clock_timestamp(),
        GREATEST(expiry,clock_timestamp()+interval '1 day')
    ) ON CONFLICT (upload_id) DO NOTHING;

    SELECT object_version_id INTO existing_version
    FROM fs2_scientific_upload_object_versions
    WHERE upload_id=upload.id;
    IF existing_version IS DISTINCT FROM requested_object_version_id THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='abandoned upload ledger conflicts';
    END IF;
END
$function$;

CREATE FUNCTION fs2_scientific_record_abandoned_upload_cleanup(
    requested_upload_id uuid,
    requested_outcome text,
    requested_object_version_id text,
    requested_provider_receipt text,
    requested_receipt_digest char(71)
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    claim fs2_scientific_abandoned_upload_claims%ROWTYPE;
    receipt fs2_scientific_abandoned_upload_receipts%ROWTYPE;
BEGIN
    SELECT * INTO claim
    FROM fs2_scientific_abandoned_upload_claims
    WHERE upload_id=requested_upload_id
    FOR SHARE;
    IF NOT FOUND
       OR length(requested_provider_receipt) NOT BETWEEN 1 AND 16384
       OR requested_provider_receipt NOT LIKE 'fs2_provider_observation.%'
       OR requested_receipt_digest !~ '^sha256:[a-f0-9]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE='FS201', MESSAGE='abandoned upload cleanup claim is unavailable';
    END IF;
    IF requested_outcome='exact-version-quarantined' THEN
        IF requested_object_version_id IS NULL OR NOT EXISTS (
            SELECT 1 FROM fs2_scientific_upload_object_versions version
            WHERE version.upload_id=requested_upload_id
              AND version.object_version_id=requested_object_version_id
        ) THEN
            RAISE EXCEPTION USING ERRCODE='FS204', MESSAGE='abandoned upload quarantine proof conflicts';
        END IF;
    ELSIF requested_outcome='provider-write-fenced-absence' THEN
        IF requested_object_version_id IS NULL
           OR EXISTS (
               SELECT 1 FROM fs2_scientific_upload_object_versions version
               WHERE version.upload_id=requested_upload_id
           )
           OR clock_timestamp() < claim.eligible_at
           OR clock_timestamp() < claim.claimed_at THEN
            RAISE EXCEPTION USING ERRCODE='FS204', MESSAGE='abandoned upload absence proof conflicts';
        END IF;
    ELSE
        RAISE EXCEPTION USING ERRCODE='FS204', MESSAGE='abandoned upload cleanup outcome is invalid';
    END IF;

    INSERT INTO fs2_scientific_abandoned_upload_receipts (
        upload_id,operation_id,tenant_id,storage_key,outcome,object_version_id,
        provider_receipt,receipt_digest
    ) VALUES (
        claim.upload_id,claim.operation_id,claim.tenant_id,claim.storage_key,
        requested_outcome,requested_object_version_id,requested_provider_receipt,
        requested_receipt_digest
    ) ON CONFLICT (upload_id) DO NOTHING;

    SELECT * INTO receipt
    FROM fs2_scientific_abandoned_upload_receipts
    WHERE upload_id=requested_upload_id;
    IF receipt.outcome IS DISTINCT FROM requested_outcome
       OR receipt.object_version_id IS DISTINCT FROM requested_object_version_id
       OR receipt.provider_receipt IS DISTINCT FROM requested_provider_receipt
       OR receipt.receipt_digest IS DISTINCT FROM requested_receipt_digest THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='abandoned upload cleanup receipt conflicts';
    END IF;
END
$function$;

REVOKE ALL ON FUNCTION fs2_scientific_claim_abandoned_uploads(timestamptz,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_register_abandoned_upload_version(
    uuid,text,bigint,text,text,timestamptz,text,char(71)
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_record_abandoned_upload_cleanup(
    uuid,text,text,text,char(71)
) FROM PUBLIC;

COMMENT ON TABLE fs2_scientific_abandoned_upload_claims IS
    'Append-only finalization fence for signed uploads abandoned beyond the provider-handle lifetime';
COMMENT ON TABLE fs2_scientific_abandoned_upload_versions IS
    'Append-only exact provider version and metadata discovered before abandoned-upload deletion';
COMMENT ON TABLE fs2_scientific_abandoned_upload_attempts IS
    'Append-only, per-upload capped scheduling evidence used to rotate unresolved cleanup claims fairly';
COMMENT ON VIEW fs2_scientific_abandoned_upload_dead_letters IS
    'Non-destructive terminal scheduling view for unresolved claims after sixteen bounded attempts; claims and provider bytes remain fenced and retained';
COMMENT ON TABLE fs2_scientific_abandoned_upload_receipts IS
    'Append-only issuer-signed exact-version quarantine or retained provider write-fence proof retained independently of operation rows';

CREATE OR REPLACE VIEW fs2_required_artifact_authority_keys AS
SELECT DISTINCT split_part(authority_token,'.',2) AS key_id
FROM fs2_operation_admission_authorities
WHERE split_part(authority_token,'.',2) ~ '^[a-f0-9]{16}$'
UNION
SELECT DISTINCT split_part(authority_token,'.',2) AS key_id
FROM fs2_operation_artifact_inputs
WHERE split_part(authority_token,'.',2) ~ '^[a-f0-9]{16}$'
UNION
SELECT DISTINCT split_part(provider_receipt,'.',2) AS key_id
FROM fs2_scientific_artifact_version_backfills
WHERE split_part(provider_receipt,'.',2) ~ '^[a-f0-9]{16}$'
UNION
SELECT DISTINCT split_part(provider_receipt,'.',2) AS key_id
FROM fs2_scientific_abandoned_upload_versions
WHERE split_part(provider_receipt,'.',2) ~ '^[a-f0-9]{16}$'
UNION
SELECT DISTINCT split_part(provider_receipt,'.',2) AS key_id
FROM fs2_scientific_abandoned_upload_receipts
WHERE split_part(provider_receipt,'.',2) ~ '^[a-f0-9]{16}$';

COMMENT ON VIEW fs2_required_artifact_authority_keys IS
    'Every Ed25519 key ID still required by operation roots, artifact inputs, provider backfills, quarantined versions, or retained cleanup receipts';

-- SAI-21 successor: fence late upload completion and make cleanup claims fair.
--
-- Migration 0030 is an immutable rejected ancestor. This forward migration
-- retains its rows and ledgers while replacing its callable cleanup path with
-- generation-bound v2 routines. Quota remains charged until the remover has
-- completed, the provider quiet interval has elapsed, and a distinct verifier
-- records two stable exact-version snapshots around an absent HEAD.

CREATE TABLE fs2_schema_rollout_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    migration_version text NOT NULL CHECK (migration_version='0031_scientific_quota_fencing.sql'),
    phase text NOT NULL CHECK (phase IN ('expanded','contracted')),
    expanded_at timestamptz NOT NULL,
    contracted_at timestamptz,
    bridge_image_ref text CHECK (
        bridge_image_ref IS NULL OR bridge_image_ref ~ '^[^@[:space:]]+@sha256:[a-f0-9]{64}$'
    ),
    bridge_release_revision bigint CHECK (bridge_release_revision IS NULL OR bridge_release_revision>=1),
    predecessor_image_ref text CHECK (
        predecessor_image_ref IS NULL OR predecessor_image_ref ~ '^[^@[:space:]]+@sha256:[a-f0-9]{64}$'
    ),
    bridge_registered_at timestamptz,
    CHECK (
        (bridge_image_ref IS NULL)=(bridge_release_revision IS NULL)
        AND (bridge_image_ref IS NULL)=(predecessor_image_ref IS NULL)
        AND (bridge_image_ref IS NULL)=(bridge_registered_at IS NULL)
    ),
    CHECK ((phase='contracted')=(contracted_at IS NOT NULL))
);

INSERT INTO fs2_schema_rollout_state(
    singleton,migration_version,phase,expanded_at,contracted_at
) VALUES(true,'0031_scientific_quota_fencing.sql','expanded',clock_timestamp(),NULL);

CREATE TABLE fs2_schema_bridge_ready_receipts (
    migration_version text NOT NULL CHECK (
        migration_version='0031_scientific_quota_fencing.sql'
    ),
    bridge_image_ref text NOT NULL CHECK (
        bridge_image_ref ~ '^[^@[:space:]]+@sha256:[a-f0-9]{64}$'
    ),
    bridge_release_revision bigint NOT NULL CHECK (bridge_release_revision>=1),
    predecessor_image_ref text NOT NULL CHECK (
        predecessor_image_ref ~ '^[^@[:space:]]+@sha256:[a-f0-9]{64}$'
    ),
    deployment_namespace text NOT NULL CHECK (
        deployment_namespace ~ '^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$'
    ),
    deployment_name text NOT NULL CHECK (
        deployment_name ~ '^[a-z0-9](?:[-a-z0-9]{0,251}[a-z0-9])?$'
    ),
    deployment_uid text NOT NULL CHECK (
        deployment_uid ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
    ),
    deployment_generation bigint NOT NULL CHECK (deployment_generation>=1),
    deployment_observed_generation bigint NOT NULL CHECK (
        deployment_observed_generation=deployment_generation
    ),
    deployment_desired_replicas integer NOT NULL CHECK (deployment_desired_replicas>=1),
    deployment_updated_replicas integer NOT NULL,
    deployment_ready_replicas integer NOT NULL,
    deployment_available_replicas integer NOT NULL,
    runtime_pod_count integer NOT NULL CHECK (runtime_pod_count>=1),
    runtime_pod_set_digest char(64) NOT NULL CHECK (
        runtime_pod_set_digest ~ '^[a-f0-9]{64}$'
    ),
    kubernetes_audit_id text NOT NULL CHECK (length(kubernetes_audit_id) BETWEEN 1 AND 200),
    predecessor_drained_at timestamptz NOT NULL,
    cleanup_not_before timestamptz NOT NULL,
    bound_legacy_artifacts bigint NOT NULL CHECK (bound_legacy_artifacts>=0),
    pending_legacy_artifacts bigint NOT NULL CHECK (pending_legacy_artifacts=0),
    unresolved_legacy_artifacts bigint NOT NULL CHECK (unresolved_legacy_artifacts=0),
    missing_unfinished_upload_sessions bigint NOT NULL CHECK (
        missing_unfinished_upload_sessions=0
    ),
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (migration_version,bridge_release_revision),
    CHECK (deployment_updated_replicas=deployment_desired_replicas),
    CHECK (deployment_ready_replicas=deployment_desired_replicas),
    CHECK (deployment_available_replicas=deployment_desired_replicas),
    CHECK (runtime_pod_count=deployment_desired_replicas),
    CHECK (cleanup_not_before>predecessor_drained_at),
    CHECK (recorded_at>=predecessor_drained_at)
);

CREATE TABLE fs2_schema_bridge_rollout_attempts (
    bridge_image_ref text NOT NULL CHECK (
        bridge_image_ref ~ '^[^@[:space:]]+@sha256:[a-f0-9]{64}$'
    ),
    predecessor_image_ref text NOT NULL CHECK (
        predecessor_image_ref ~ '^[^@[:space:]]+@sha256:[a-f0-9]{64}$'
    ),
    bridge_release_revision bigint NOT NULL CHECK (bridge_release_revision>=1),
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (bridge_image_ref,predecessor_image_ref,bridge_release_revision)
);

ALTER TABLE fs2_scientific_artifact_quota_reservations
    ADD COLUMN latest_upload_capability_expires_at timestamptz NOT NULL
        DEFAULT (CURRENT_TIMESTAMP+interval '15 minutes'),
    ADD COLUMN upload_completion_grace_seconds integer NOT NULL DEFAULT 900,
    ADD COLUMN provider_stability_grace_seconds integer NOT NULL DEFAULT 300;

-- Close every pre-0031 destructive entry point in the first short expansion
-- transaction. Old CronJobs may still be scheduled while later resumable
-- steps run, but their retained EXECUTE grants can only reach these fail-closed
-- bodies. The expanded application compatibility window never includes v1
-- cleanup authority.
CREATE OR REPLACE FUNCTION fs2_scientific_claim_artifact_removals(
    p_limit integer,p_operation_id uuid DEFAULT NULL,p_tenant_id text DEFAULT NULL
) RETURNS TABLE(
    upload_id uuid,operation_id uuid,attempt_id uuid,tenant_id text,
    storage_key text,eligible_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='FS202',
        MESSAGE='legacy artifact cleanup is disabled after quota fencing expansion';
END
$function$;

CREATE OR REPLACE FUNCTION fs2_scientific_claim_artifact_verifications(p_limit integer)
RETURNS TABLE(
    upload_id uuid,operation_id uuid,attempt_id uuid,tenant_id text,
    storage_key text,eligible_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='FS202',
        MESSAGE='legacy artifact cleanup is disabled after quota fencing expansion';
END
$function$;

CREATE OR REPLACE FUNCTION fs2_scientific_record_artifact_removal(
    p_upload_id uuid,p_operation_id uuid,p_attempt_id uuid,p_tenant_id text,
    p_storage_key text,p_evidence_kind text,p_provider_request_id text,
    p_removed_version_count integer,p_observed_at timestamptz
) RETURNS fs2_scientific_artifact_quota_reservations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='FS202',
        MESSAGE='legacy artifact cleanup evidence is disabled after quota fencing expansion';
END
$function$;

REVOKE ALL ON FUNCTION fs2_scientific_claim_artifact_removals(integer,uuid,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_claim_artifact_verifications(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_record_artifact_removal(
    uuid,uuid,uuid,text,text,text,text,integer,timestamptz
) FROM PUBLIC;

-- fs2-migration-transaction-boundary

-- Existing handles cannot be reconstructed.  The DDL default above gives
-- every retained row a fresh maximum-capability fence without issuing an
-- UPDATE that 0030's quota-state transition trigger would correctly reject
-- for already-removing or released rows. Provider completion and stability
-- grace are added by the claim routines below.

ALTER TABLE fs2_scientific_artifact_quota_reservations
    ALTER COLUMN latest_upload_capability_expires_at
        SET DEFAULT (statement_timestamp()+interval '15 minutes'),
    ADD CONSTRAINT fs2_scientific_artifact_quota_upload_grace_bound
        CHECK (upload_completion_grace_seconds BETWEEN 60 AND 3600) NOT VALID,
    ADD CONSTRAINT fs2_scientific_artifact_quota_stability_grace_bound
        CHECK (provider_stability_grace_seconds BETWEEN 30 AND 3600) NOT VALID,
    ADD CONSTRAINT fs2_scientific_artifact_quota_capability_time_order
        CHECK (latest_upload_capability_expires_at>=reserved_at) NOT VALID;

ALTER TABLE fs2_scientific_uploads
    ADD COLUMN provider_version_id text,
    ADD CONSTRAINT fs2_scientific_uploads_provider_version_id_bound
        CHECK (provider_version_id IS NULL OR length(provider_version_id) BETWEEN 1 AND 1024) NOT VALID;

ALTER TABLE fs2_scientific_artifacts
    ADD COLUMN provider_version_id text,
    ADD CONSTRAINT fs2_scientific_artifacts_provider_version_id_bound
        CHECK (provider_version_id IS NULL OR length(provider_version_id) BETWEEN 1 AND 1024) NOT VALID;

-- fs2-migration-transaction-boundary

-- Provider-version completeness is phase-aware rather than a NOT VALID CHECK.
-- PostgreSQL applies a NOT VALID check to new rows, which would reject the
-- exact predecessor's publication transition during the bounded expand
-- window.  The triggers below admit that legacy transition only while the
-- one-way rollout phase is expanded, retain a background binding claim, and
-- require provider versions for every post-contract publication.

CREATE TABLE fs2_scientific_artifact_upload_sessions (
    -- Retained provider evidence deliberately has no FK to the 90-day
    -- application metadata.  Normal retention may delete the upload row
    -- without deleting or being blocked by this custody record.
    upload_id uuid PRIMARY KEY,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    storage_key text NOT NULL CHECK (length(storage_key) BETWEEN 1 AND 1024),
    provider_upload_id text NOT NULL UNIQUE CHECK (length(provider_upload_id) BETWEEN 1 AND 1024),
    session_generation integer NOT NULL DEFAULT 1 CHECK (session_generation BETWEEN 1 AND 1000000),
    part_size_bytes bigint NOT NULL CHECK (part_size_bytes BETWEEN 5242880 AND 5368709120),
    part_count integer NOT NULL CHECK (part_count BETWEEN 1 AND 10000),
    provider_stability_grace_seconds integer NOT NULL CHECK (
        provider_stability_grace_seconds BETWEEN 30 AND 3600
    ),
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','legacy','completed','aborted')),
    provider_version_id text CHECK (length(provider_version_id) BETWEEN 1 AND 1024),
    initiated_at timestamptz NOT NULL,
    terminal_at timestamptz,
    terminal_provider_request_id text CHECK (
        terminal_provider_request_id IS NULL OR length(terminal_provider_request_id) BETWEEN 1 AND 512
    ),
    CHECK ((state='completed')=(provider_version_id IS NOT NULL)),
    CHECK ((state IN ('active','legacy'))=(terminal_at IS NULL)),
    CHECK ((state IN ('active','legacy'))=(terminal_provider_request_id IS NULL))
);

-- Preserve every unfinished pre-0031 PutObject capability.  Its opaque
-- provider session identity did not exist, so it receives a retained legacy
-- marker and can only finalize by proving one unambiguous provider VersionId.
-- Cleanup remains delayed by the migration's conservative capability fence.
INSERT INTO fs2_scientific_artifact_upload_sessions(
    upload_id,tenant_id,storage_key,provider_upload_id,session_generation,
    part_size_bytes,part_count,provider_stability_grace_seconds,state,
    provider_version_id,initiated_at,terminal_at,terminal_provider_request_id
)
SELECT upload.id,upload.tenant_id,upload.storage_key,
       'legacy-single-put-v1:'||upload.id::text,1,
       GREATEST(5242880,LEAST(upload.expected_size_bytes,5368709120)),1,
       reservation.provider_stability_grace_seconds,'legacy',NULL,
       upload.begun_at,NULL,NULL
FROM fs2_scientific_uploads upload
JOIN fs2_scientific_artifact_quota_reservations reservation
  ON reservation.upload_id=upload.id
WHERE upload.artifact_id IS NULL;

-- fs2-migration-transaction-boundary

CREATE FUNCTION fs2_scientific_queue_legacy_upload_session_v2()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    upload record;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.fs2_schema_rollout_state
        WHERE singleton AND phase='expanded'
    ) THEN
        RETURN NEW;
    END IF;
    SELECT * INTO upload
    FROM public.fs2_scientific_uploads
    WHERE id=NEW.upload_id AND tenant_id=NEW.tenant_id;
    IF FOUND AND upload.artifact_id IS NULL THEN
        INSERT INTO public.fs2_scientific_artifact_upload_sessions(
            upload_id,tenant_id,storage_key,provider_upload_id,session_generation,
            part_size_bytes,part_count,provider_stability_grace_seconds,state,
            provider_version_id,initiated_at,terminal_at,terminal_provider_request_id
        ) VALUES(
            upload.id,upload.tenant_id,upload.storage_key,
            'legacy-single-put-v1:'||upload.id::text,1,
            GREATEST(5242880,LEAST(upload.expected_size_bytes,5368709120)),1,
            NEW.provider_stability_grace_seconds,'legacy',NULL,
            upload.begun_at,NULL,NULL
        ) ON CONFLICT (upload_id) DO NOTHING;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_scientific_quota_queue_legacy_upload_session
AFTER INSERT ON fs2_scientific_artifact_quota_reservations
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_queue_legacy_upload_session_v2();

-- fs2-migration-transaction-boundary

-- CREATE TRIGGER waits for predecessor writers that began before the DDL lock,
-- but the original snapshot above cannot see those transactions.  Repeat the
-- idempotent synthesis after the trigger exists so a commit on either side of
-- the lock is covered by exactly one retained legacy session.
INSERT INTO fs2_scientific_artifact_upload_sessions(
    upload_id,tenant_id,storage_key,provider_upload_id,session_generation,
    part_size_bytes,part_count,provider_stability_grace_seconds,state,
    provider_version_id,initiated_at,terminal_at,terminal_provider_request_id
)
SELECT upload.id,upload.tenant_id,upload.storage_key,
       'legacy-single-put-v1:'||upload.id::text,1,
       GREATEST(5242880,LEAST(upload.expected_size_bytes,5368709120)),1,
       reservation.provider_stability_grace_seconds,'legacy',NULL,
       upload.begun_at,NULL,NULL
FROM fs2_scientific_uploads upload
JOIN fs2_scientific_artifact_quota_reservations reservation
  ON reservation.upload_id=upload.id
WHERE upload.artifact_id IS NULL
ON CONFLICT (upload_id) DO NOTHING;

-- fs2-migration-transaction-boundary

CREATE TABLE fs2_scientific_artifact_upload_session_creation_claims (
    -- This retained claim precedes the external CreateMultipartUpload call.
    -- It has no FK so normal 90-day application retention cannot erase the
    -- reconciliation ledger or block application metadata purge.
    upload_id uuid PRIMARY KEY,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    storage_key text NOT NULL CHECK (length(storage_key) BETWEEN 1 AND 1024),
    claim_id uuid NOT NULL UNIQUE,
    claim_generation integer NOT NULL CHECK (claim_generation BETWEEN 1 AND 1000000),
    part_size_bytes bigint NOT NULL CHECK (part_size_bytes BETWEEN 5242880 AND 5368709120),
    part_count integer NOT NULL CHECK (part_count BETWEEN 1 AND 10000),
    provider_stability_grace_seconds integer NOT NULL DEFAULT 300 CHECK (
        provider_stability_grace_seconds BETWEEN 30 AND 3600
    ),
    state text NOT NULL CHECK (state IN ('creating','bound','reconciling','reconciled')),
    claimed_at timestamptz NOT NULL,
    reconcile_after timestamptz NOT NULL,
    reconciliation_claimed_at timestamptz,
    empty_observed_at timestamptz,
    provider_upload_id text CHECK (length(provider_upload_id) BETWEEN 1 AND 1024),
    reconciled_at timestamptz,
    CHECK (reconcile_after>claimed_at),
    CHECK ((state='bound')=(provider_upload_id IS NOT NULL)),
    CHECK ((state='reconciled')=(reconciled_at IS NOT NULL)),
    CHECK ((state IN ('reconciling','reconciled'))=(reconciliation_claimed_at IS NOT NULL))
);

CREATE TABLE fs2_scientific_artifact_upload_session_reconciliation_events (
    upload_id uuid NOT NULL,
    claim_id uuid NOT NULL,
    claim_generation integer NOT NULL CHECK (claim_generation BETWEEN 1 AND 1000000),
    tenant_id text NOT NULL,
    storage_key text NOT NULL CHECK (length(storage_key) BETWEEN 1 AND 1024),
    provider_request_id text NOT NULL CHECK (length(provider_request_id) BETWEEN 1 AND 512),
    aborted_upload_count integer NOT NULL CHECK (aborted_upload_count BETWEEN 0 AND 10000),
    multipart_session_set_digest text NOT NULL CHECK (
        multipart_session_set_digest ~ '^sha256:[a-f0-9]{64}$'
    ),
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (upload_id,claim_generation)
);

CREATE TABLE fs2_scientific_artifact_finalization_leases (
    upload_id uuid PRIMARY KEY,
    lease_id uuid NOT NULL UNIQUE,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    lease_generation integer NOT NULL CHECK (lease_generation BETWEEN 1 AND 1000000),
    session_generation integer NOT NULL CHECK (session_generation BETWEEN 1 AND 1000000),
    state text NOT NULL CHECK (state IN ('active','completed','failed')),
    acquired_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    completed_at timestamptz,
    failed_at timestamptz,
    failure_code text CHECK (
        failure_code IS NULL OR failure_code IN ('content_verification_failed','artifact_policy_failed')
    ),
    provider_version_id text CHECK (length(provider_version_id) BETWEEN 1 AND 1024),
    CHECK ((state='completed')=(completed_at IS NOT NULL)),
    CHECK ((state='failed')=(failed_at IS NOT NULL)),
    CHECK ((state='failed')=(failure_code IS NOT NULL)),
    CHECK ((state IN ('completed','failed'))=(provider_version_id IS NOT NULL)),
    CHECK (expires_at>acquired_at)
);

CREATE TABLE fs2_scientific_artifact_finalization_failures (
    upload_id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    lease_id uuid NOT NULL,
    lease_generation integer NOT NULL CHECK (lease_generation BETWEEN 1 AND 1000000),
    session_generation integer NOT NULL CHECK (session_generation BETWEEN 1 AND 1000000),
    provider_upload_id text NOT NULL CHECK (length(provider_upload_id) BETWEEN 1 AND 1024),
    provider_version_id text NOT NULL CHECK (length(provider_version_id) BETWEEN 1 AND 1024),
    provider_request_id text NOT NULL CHECK (length(provider_request_id) BETWEEN 1 AND 512),
    failure_code text NOT NULL CHECK (
        failure_code IN ('content_verification_failed','artifact_policy_failed')
    ),
    observed_digest text NOT NULL CHECK (observed_digest ~ '^sha256:[a-f0-9]{64}$'),
    observed_size_bytes bigint NOT NULL CHECK (observed_size_bytes BETWEEN 0 AND 1099511627776),
    observed_media_type text NOT NULL CHECK (length(observed_media_type) BETWEEN 3 AND 255),
    observed_compression text,
    observed_at timestamptz NOT NULL
);

CREATE TABLE fs2_scientific_artifact_legacy_version_claims (
    artifact_id uuid PRIMARY KEY,
    claim_generation integer NOT NULL DEFAULT 0 CHECK (claim_generation BETWEEN 0 AND 1000000),
    claimed_at timestamptz,
    list_key_marker text CHECK (list_key_marker IS NULL OR length(list_key_marker) BETWEEN 1 AND 1024),
    list_version_id_marker text CHECK (
        list_version_id_marker IS NULL OR length(list_version_id_marker) BETWEEN 1 AND 1024
    ),
    scan_completed_at timestamptz,
    retry_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    resolution text NOT NULL DEFAULT 'pending' CHECK (resolution IN ('pending','bound','unresolved')),
    resolution_reason text CHECK (resolution_reason IS NULL OR length(resolution_reason) BETWEEN 1 AND 120),
    CHECK ((list_key_marker IS NULL)=(list_version_id_marker IS NULL)),
    CHECK ((resolution='pending')=(resolution_reason IS NULL))
);

CREATE TABLE fs2_scientific_artifact_legacy_version_scan_events (
    artifact_id uuid NOT NULL,
    claim_generation integer NOT NULL CHECK (claim_generation BETWEEN 1 AND 1000000),
    claimed_at timestamptz NOT NULL,
    prior_key_marker text,
    prior_version_id_marker text,
    next_key_marker text,
    next_version_id_marker text,
    provider_request_id text NOT NULL CHECK (length(provider_request_id) BETWEEN 1 AND 512),
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (artifact_id,claim_generation),
    CHECK ((prior_key_marker IS NULL)=(prior_version_id_marker IS NULL)),
    CHECK ((next_key_marker IS NULL)=(next_version_id_marker IS NULL)),
    CHECK (observed_at>=claimed_at)
);

CREATE TABLE fs2_scientific_artifact_legacy_version_bindings (
    artifact_id uuid PRIMARY KEY,
    upload_id uuid NOT NULL,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    storage_key text NOT NULL CHECK (length(storage_key) BETWEEN 1 AND 1024),
    provider_version_id text NOT NULL CHECK (length(provider_version_id) BETWEEN 1 AND 1024),
    provider_request_id text NOT NULL CHECK (length(provider_request_id) BETWEEN 1 AND 512),
    observed_digest text NOT NULL CHECK (observed_digest ~ '^sha256:[a-f0-9]{64}$'),
    observed_size_bytes bigint NOT NULL CHECK (observed_size_bytes BETWEEN 0 AND 1099511627776),
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (storage_key,provider_version_id)
);

INSERT INTO fs2_scientific_artifact_legacy_version_claims(artifact_id)
SELECT id FROM fs2_scientific_artifacts WHERE provider_version_id IS NULL
ON CONFLICT (artifact_id) DO NOTHING;

CREATE VIEW fs2_scientific_artifacts_versioned
WITH (security_invoker=true)
AS
SELECT artifact.id,artifact.attempt_id,artifact.operation_id,artifact.tenant_id,
       artifact.stage_id,artifact.shard_id,artifact.direction,artifact.digest,
       artifact.size_bytes,artifact.media_type,artifact.compression,artifact.storage_key,
       COALESCE(artifact.provider_version_id,binding.provider_version_id) AS provider_version_id,
       artifact.access_profile,artifact.access_receipt_digest,
       artifact.retention_expires_at,artifact.created_at
FROM fs2_scientific_artifacts artifact
LEFT JOIN fs2_scientific_artifact_legacy_version_bindings binding
  ON binding.artifact_id=artifact.id;

CREATE TABLE fs2_scientific_artifact_upload_capabilities (
    capability_id uuid PRIMARY KEY,
    upload_id uuid NOT NULL,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    session_generation integer NOT NULL CHECK (session_generation BETWEEN 1 AND 1000000),
    part_number integer NOT NULL CHECK (part_number BETWEEN 1 AND 10000),
    size_bytes bigint NOT NULL CHECK (size_bytes BETWEEN 0 AND 5368709120),
    checksum text NOT NULL CHECK (checksum ~ '^sha256:[a-f0-9]{64}$'),
    media_type text NOT NULL CHECK (media_type ~ '^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$'),
    compression text CHECK (compression IN ('gzip','zstd')),
    expires_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (upload_id,capability_id),
    CHECK (expires_at>=recorded_at-interval '1 minute')
);

CREATE INDEX fs2_scientific_artifact_upload_capabilities_upload_idx
    ON fs2_scientific_artifact_upload_capabilities (upload_id,expires_at DESC,capability_id);

CREATE INDEX fs2_scientific_artifact_upload_capabilities_part_idx
    ON fs2_scientific_artifact_upload_capabilities
       (upload_id,session_generation,part_number,recorded_at,capability_id);

CREATE TABLE fs2_scientific_artifact_deletion_evidence_v2 (
    upload_id uuid NOT NULL,
    removal_generation integer NOT NULL CHECK (removal_generation BETWEEN 1 AND 1000000),
    operation_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    storage_key text NOT NULL CHECK (length(storage_key) BETWEEN 1 AND 1024),
    evidence_kind text NOT NULL CHECK (
        evidence_kind IN ('absence_confirmed','all_versions_removed')
    ),
    provider_request_id text NOT NULL CHECK (length(provider_request_id) BETWEEN 1 AND 512),
    removed_version_count bigint NOT NULL CHECK (removed_version_count>=0),
    aborted_upload_count bigint NOT NULL CHECK (aborted_upload_count>=0),
    multipart_list_request_id text NOT NULL CHECK (length(multipart_list_request_id) BETWEEN 1 AND 512),
    multipart_session_set_digest text NOT NULL CHECK (
        multipart_session_set_digest ~ '^sha256:[a-f0-9]{64}$'
    ),
    removal_claimed_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (upload_id,removal_generation),
    CHECK (
        (evidence_kind='absence_confirmed' AND removed_version_count=0)
        OR (evidence_kind='all_versions_removed' AND removed_version_count>=1)
    ),
    CHECK (observed_at>=removal_claimed_at)
);

CREATE TABLE fs2_scientific_artifact_verification_failures_v2 (
    upload_id uuid NOT NULL,
    removal_generation integer NOT NULL CHECK (removal_generation BETWEEN 1 AND 1000000),
    verification_generation integer NOT NULL CHECK (verification_generation BETWEEN 1 AND 1000000),
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    verification_claimed_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (upload_id,removal_generation,verification_generation),
    CHECK (recorded_at>=verification_claimed_at)
);

CREATE TABLE fs2_scientific_artifact_removal_evidence_v2 (
    upload_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    storage_key text NOT NULL UNIQUE CHECK (length(storage_key) BETWEEN 1 AND 1024),
    evidence_kind text NOT NULL CHECK (evidence_kind='absence_confirmed'),
    provider_request_id text NOT NULL CHECK (length(provider_request_id) BETWEEN 1 AND 512),
    removed_version_count integer NOT NULL CHECK (removed_version_count=0),
    observed_at timestamptz NOT NULL,
    latest_upload_capability_expires_at timestamptz NOT NULL,
    removal_generation integer NOT NULL CHECK (removal_generation BETWEEN 1 AND 1000000),
    verification_generation integer NOT NULL CHECK (verification_generation BETWEEN 1 AND 1000000),
    first_list_request_id text NOT NULL CHECK (length(first_list_request_id) BETWEEN 1 AND 512),
    head_request_id text NOT NULL CHECK (length(head_request_id) BETWEEN 1 AND 512),
    second_list_request_id text NOT NULL CHECK (length(second_list_request_id) BETWEEN 1 AND 512),
    first_version_set_digest text NOT NULL CHECK (first_version_set_digest ~ '^sha256:[a-f0-9]{64}$'),
    second_version_set_digest text NOT NULL CHECK (second_version_set_digest ~ '^sha256:[a-f0-9]{64}$'),
    first_multipart_list_request_id text NOT NULL CHECK (
        length(first_multipart_list_request_id) BETWEEN 1 AND 512
    ),
    second_multipart_list_request_id text NOT NULL CHECK (
        length(second_multipart_list_request_id) BETWEEN 1 AND 512
    ),
    first_multipart_session_set_digest text NOT NULL CHECK (
        first_multipart_session_set_digest ~ '^sha256:[a-f0-9]{64}$'
    ),
    second_multipart_session_set_digest text NOT NULL CHECK (
        second_multipart_session_set_digest ~ '^sha256:[a-f0-9]{64}$'
    ),
    claim_digest text NOT NULL CHECK (claim_digest ~ '^sha256:[a-f0-9]{64}$'),
    CHECK (first_version_set_digest=second_version_set_digest),
    CHECK (first_multipart_session_set_digest=second_multipart_session_set_digest),
    CHECK (observed_at>=latest_upload_capability_expires_at)
);

CREATE TABLE fs2_scientific_artifact_janitor_tenant_cursors (
    tenant_id text PRIMARY KEY CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    last_removal_claimed_at timestamptz,
    last_verification_claimed_at timestamptz
);

INSERT INTO fs2_scientific_artifact_janitor_tenant_cursors(tenant_id)
SELECT DISTINCT tenant_id FROM fs2_scientific_artifact_quota_reservations
ON CONFLICT (tenant_id) DO NOTHING;

-- fs2-migration-transaction-boundary

CREATE FUNCTION fs2_scientific_ensure_artifact_janitor_tenant_cursor() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    INSERT INTO public.fs2_scientific_artifact_janitor_tenant_cursors(tenant_id)
    VALUES(NEW.tenant_id)
    ON CONFLICT (tenant_id) DO NOTHING;
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_scientific_artifact_quota_tenant_cursor
AFTER INSERT ON fs2_scientific_artifact_quota_reservations
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_ensure_artifact_janitor_tenant_cursor();

-- fs2-migration-transaction-boundary

INSERT INTO fs2_scientific_artifact_janitor_tenant_cursors(tenant_id)
SELECT DISTINCT tenant_id FROM fs2_scientific_artifact_quota_reservations
ON CONFLICT (tenant_id) DO NOTHING;

-- fs2-migration-transaction-boundary

CREATE FUNCTION fs2_scientific_validate_upload_session_transition() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF OLD.upload_id IS DISTINCT FROM NEW.upload_id
       OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.storage_key IS DISTINCT FROM NEW.storage_key
       OR OLD.provider_upload_id IS DISTINCT FROM NEW.provider_upload_id
       OR OLD.session_generation IS DISTINCT FROM NEW.session_generation
       OR OLD.part_size_bytes IS DISTINCT FROM NEW.part_size_bytes
       OR OLD.part_count IS DISTINCT FROM NEW.part_count
       OR OLD.initiated_at IS DISTINCT FROM NEW.initiated_at
       OR OLD.state NOT IN ('active','legacy')
       OR NEW.state NOT IN ('completed','aborted')
       OR NEW.terminal_at IS NULL
       OR NEW.terminal_provider_request_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='invalid artifact upload-session transition';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_scientific_artifact_upload_sessions_transition
BEFORE UPDATE ON fs2_scientific_artifact_upload_sessions
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_validate_upload_session_transition();

CREATE TRIGGER fs2_scientific_artifact_upload_sessions_no_delete
BEFORE DELETE ON fs2_scientific_artifact_upload_sessions
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_artifact_upload_session_claims_no_delete
BEFORE DELETE ON fs2_scientific_artifact_upload_session_creation_claims
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_artifact_upload_session_reconciliation_events_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_artifact_upload_session_reconciliation_events
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE FUNCTION fs2_scientific_validate_finalization_lease_transition() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF OLD.upload_id IS DISTINCT FROM NEW.upload_id
       OR OLD.lease_id IS DISTINCT FROM NEW.lease_id
       OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.session_generation IS DISTINCT FROM NEW.session_generation
       OR OLD.state<>'active' THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='invalid artifact finalization-lease transition';
    END IF;
    IF NEW.state='active' THEN
        IF NEW.lease_generation<>OLD.lease_generation+1
           OR NEW.acquired_at<=OLD.acquired_at OR NEW.expires_at<=NEW.acquired_at
           OR NEW.completed_at IS NOT NULL OR NEW.failed_at IS NOT NULL
           OR NEW.failure_code IS NOT NULL OR NEW.provider_version_id IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='invalid artifact finalization-lease renewal';
        END IF;
    ELSIF NEW.state='completed' THEN
        IF NEW.lease_generation<>OLD.lease_generation
           OR NEW.acquired_at<>OLD.acquired_at OR NEW.expires_at<>OLD.expires_at
           OR NEW.completed_at IS NULL OR NEW.failed_at IS NOT NULL
           OR NEW.failure_code IS NOT NULL OR NEW.provider_version_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='invalid artifact finalization-lease completion';
        END IF;
    ELSIF NEW.state='failed' THEN
        IF NEW.lease_generation<>OLD.lease_generation
           OR NEW.acquired_at<>OLD.acquired_at OR NEW.expires_at<>OLD.expires_at
           OR NEW.completed_at IS NOT NULL OR NEW.failed_at IS NULL
           OR NEW.failure_code IS NULL OR NEW.provider_version_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='invalid artifact finalization-lease failure';
        END IF;
    ELSE
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='invalid artifact finalization-lease state';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_scientific_artifact_finalization_leases_transition
BEFORE UPDATE ON fs2_scientific_artifact_finalization_leases
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_validate_finalization_lease_transition();

CREATE TRIGGER fs2_scientific_artifact_finalization_leases_no_delete
BEFORE DELETE ON fs2_scientific_artifact_finalization_leases
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_artifact_finalization_failures_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_artifact_finalization_failures
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_artifact_legacy_version_bindings_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_artifact_legacy_version_bindings
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_artifact_legacy_version_scan_events_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_artifact_legacy_version_scan_events
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_schema_bridge_ready_receipts_immutable
BEFORE UPDATE OR DELETE ON fs2_schema_bridge_ready_receipts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_schema_bridge_rollout_attempts_immutable
BEFORE UPDATE OR DELETE ON fs2_schema_bridge_rollout_attempts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

-- fs2-migration-transaction-boundary

CREATE FUNCTION fs2_scientific_validate_upload_provider_version_v2() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
DECLARE
    artifact_version text;
BEGIN
    -- The expand release deliberately leaves the N-1 runtime's original
    -- artifact publication transition available while that exact image is
    -- still serving.  The contract release removes its column privileges and
    -- flips this one-way fence only after the 0031-aware runtime is Ready.
    IF EXISTS (
        SELECT 1 FROM public.fs2_schema_rollout_state
        WHERE singleton AND phase='expanded'
    ) THEN
        RETURN NEW;
    END IF;
    IF NEW.artifact_id IS NOT NULL AND NEW.provider_version_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='FS202',
            MESSAGE='contracted scientific upload requires a provider version';
    END IF;
    IF OLD.provider_version_id IS NOT DISTINCT FROM NEW.provider_version_id THEN
        RETURN NEW;
    END IF;
    SELECT provider_version_id INTO artifact_version
    FROM public.fs2_scientific_artifacts
    WHERE id=NEW.artifact_id AND operation_id=NEW.operation_id
      AND tenant_id=NEW.tenant_id AND attempt_id=NEW.attempt_id;
    IF OLD.provider_version_id IS NOT NULL OR NEW.provider_version_id IS NULL
       OR OLD.artifact_id IS NOT NULL OR NEW.artifact_id IS NULL
       OR artifact_version IS DISTINCT FROM NEW.provider_version_id THEN
        RAISE EXCEPTION USING ERRCODE='FS202',
            MESSAGE='invalid scientific upload provider-version transition';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_scientific_uploads_provider_version_transition
BEFORE UPDATE ON fs2_scientific_uploads
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_validate_upload_provider_version_v2();

-- fs2-migration-transaction-boundary

CREATE FUNCTION fs2_scientific_validate_artifact_provider_version_v2() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF NEW.provider_version_id IS NULL AND NOT EXISTS (
        SELECT 1 FROM public.fs2_schema_rollout_state
        WHERE singleton AND phase='expanded'
    ) THEN
        RAISE EXCEPTION USING ERRCODE='FS202',
            MESSAGE='contracted scientific artifact requires a provider version';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_scientific_artifacts_provider_version_insert
BEFORE INSERT ON fs2_scientific_artifacts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_validate_artifact_provider_version_v2();

CREATE FUNCTION fs2_scientific_queue_legacy_artifact_version_v2() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF NEW.provider_version_id IS NULL THEN
        INSERT INTO public.fs2_scientific_artifact_legacy_version_claims(artifact_id)
        VALUES(NEW.id)
        ON CONFLICT (artifact_id) DO NOTHING;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_scientific_artifacts_queue_legacy_version
AFTER INSERT ON fs2_scientific_artifacts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_queue_legacy_artifact_version_v2();

-- fs2-migration-transaction-boundary

-- Close the analogous predecessor INSERT window for finalized artifacts.  A
-- row committed before the trigger lock is visible here; a later row fires
-- the trigger.  Both paths converge on the same append-only claim identity.
INSERT INTO fs2_scientific_artifact_legacy_version_claims(artifact_id)
SELECT id FROM fs2_scientific_artifacts WHERE provider_version_id IS NULL
ON CONFLICT (artifact_id) DO NOTHING;

-- fs2-migration-transaction-boundary

CREATE TRIGGER fs2_scientific_artifact_upload_capabilities_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_artifact_upload_capabilities
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_artifact_deletion_evidence_v2_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_artifact_deletion_evidence_v2
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_artifact_verification_failures_v2_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_artifact_verification_failures_v2
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE TRIGGER fs2_scientific_artifact_removal_evidence_v2_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_artifact_removal_evidence_v2
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

COMMENT ON TABLE fs2_scientific_artifact_upload_capabilities IS
    'Append-only exact-session part capabilities bound to part number, length and checksum';
COMMENT ON TABLE fs2_scientific_artifact_upload_sessions IS
    'One server-completed provider generation per upload; clients never receive completion authority';
COMMENT ON TABLE fs2_scientific_artifact_finalization_leases IS
    'Renewable provider-mutation fence; expiry schedules reconciliation and never authorizes cleanup';
COMMENT ON TABLE fs2_scientific_artifact_finalization_failures IS
    'Append-only failed publication evidence bound to the exact provider VersionId and finalization generation';
COMMENT ON TABLE fs2_schema_rollout_state IS
    'One-way 0031 expand/contract fence; expanded preserves N-1 runtime transitions until the candidate is Ready';
COMMENT ON TABLE fs2_scientific_artifact_legacy_version_bindings IS
    'Verifier-owned immutable VersionId pins for retained artifacts created before migration 0031';
COMMENT ON TABLE fs2_scientific_artifact_legacy_version_scan_events IS
    'Append-only bounded provider inventory pages for pre-0031 exact-key VersionId backfill';
COMMENT ON TABLE fs2_scientific_artifact_deletion_evidence_v2 IS
    'Append-only remover receipts that never release quota';
COMMENT ON TABLE fs2_scientific_artifact_removal_evidence_v2 IS
    'Verifier-only stable double-snapshot evidence bound to exact claim generations';
COMMENT ON TABLE fs2_scientific_artifact_janitor_tenant_cursors IS
    'Nonblocking round-robin cursors; one locked tenant is skipped instead of blocking a cleanup pass';

CREATE FUNCTION fs2_scientific_claim_upload_session_creation_v2(
    p_upload_id uuid,
    p_tenant_id text,
    p_storage_key text,
    p_claim_id uuid,
    p_part_size_bytes bigint,
    p_part_count integer
) RETURNS fs2_scientific_artifact_upload_session_creation_claims
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    upload record;
    reservation record;
    claim record;
    now_at timestamptz := clock_timestamp();
BEGIN
    IF p_part_size_bytes NOT BETWEEN 5242880 AND 5368709120
       OR p_part_count NOT BETWEEN 1 AND 10000 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='artifact upload-session shape is invalid';
    END IF;
    SELECT * INTO upload FROM public.fs2_scientific_uploads
    WHERE id=p_upload_id AND tenant_id=p_tenant_id FOR UPDATE;
    IF NOT FOUND OR upload.storage_key<>p_storage_key OR upload.artifact_id IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='upload-session creation fence has closed';
    END IF;
    SELECT * INTO reservation FROM public.fs2_scientific_artifact_quota_reservations
    WHERE upload_id=p_upload_id AND tenant_id=p_tenant_id FOR UPDATE;
    IF NOT FOUND OR reservation.state<>'active' OR reservation.expires_at<=now_at THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='upload-session creation fence has closed';
    END IF;
    SELECT * INTO claim FROM public.fs2_scientific_artifact_upload_session_creation_claims
    WHERE upload_id=p_upload_id FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO public.fs2_scientific_artifact_upload_session_creation_claims(
            upload_id,tenant_id,storage_key,claim_id,claim_generation,
            part_size_bytes,part_count,provider_stability_grace_seconds,
            state,claimed_at,reconcile_after
        ) VALUES(
            p_upload_id,p_tenant_id,p_storage_key,p_claim_id,1,
            p_part_size_bytes,p_part_count,reservation.provider_stability_grace_seconds,
            'creating',now_at,
            now_at+make_interval(secs=>reservation.upload_completion_grace_seconds)
        ) RETURNING * INTO claim;
    ELSIF claim.storage_key<>p_storage_key OR claim.tenant_id<>p_tenant_id
       OR claim.part_size_bytes<>p_part_size_bytes OR claim.part_count<>p_part_count THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='upload-session creation already differs';
    ELSIF claim.state='reconciled' THEN
        UPDATE public.fs2_scientific_artifact_upload_session_creation_claims
        SET claim_id=p_claim_id,claim_generation=claim_generation+1,state='creating',
            claimed_at=now_at,
            reconcile_after=now_at+make_interval(secs=>reservation.upload_completion_grace_seconds),
            reconciliation_claimed_at=NULL,empty_observed_at=NULL,
            provider_upload_id=NULL,reconciled_at=NULL
        WHERE upload_id=p_upload_id AND state='reconciled'
        RETURNING * INTO claim;
    END IF;
    RETURN claim;
END
$function$;

CREATE FUNCTION fs2_scientific_bind_upload_session_v2(
    p_upload_id uuid,
    p_tenant_id text,
    p_storage_key text,
    p_claim_id uuid,
    p_provider_upload_id text,
    p_part_size_bytes bigint,
    p_part_count integer,
    p_initiated_at timestamptz
) RETURNS fs2_scientific_artifact_upload_sessions
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    upload record;
    session record;
    claim record;
BEGIN
    IF length(p_provider_upload_id) NOT BETWEEN 1 AND 1024
       OR p_part_size_bytes NOT BETWEEN 5242880 AND 5368709120
       OR p_part_count NOT BETWEEN 1 AND 10000
       OR p_initiated_at>clock_timestamp()+interval '5 minutes' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='artifact upload-session identity is invalid';
    END IF;
    SELECT * INTO upload
    FROM public.fs2_scientific_uploads
    WHERE id=p_upload_id AND tenant_id=p_tenant_id
    FOR UPDATE;
    IF NOT FOUND OR upload.storage_key<>p_storage_key OR upload.artifact_id IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact upload cannot bind a provider session';
    END IF;
    SELECT * INTO claim
    FROM public.fs2_scientific_artifact_upload_session_creation_claims
    WHERE upload_id=p_upload_id
    FOR UPDATE;
    IF NOT FOUND OR claim.claim_id<>p_claim_id OR claim.state<>'creating'
       OR claim.tenant_id<>p_tenant_id OR claim.storage_key<>p_storage_key
       OR claim.part_size_bytes<>p_part_size_bytes OR claim.part_count<>p_part_count THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact upload-session owner is stale';
    END IF;
    INSERT INTO public.fs2_scientific_artifact_upload_sessions(
        upload_id,tenant_id,storage_key,provider_upload_id,part_size_bytes,part_count,
        provider_stability_grace_seconds,initiated_at
    ) VALUES(
        p_upload_id,p_tenant_id,p_storage_key,p_provider_upload_id,p_part_size_bytes,p_part_count,
        claim.provider_stability_grace_seconds,p_initiated_at
    ) ON CONFLICT (upload_id) DO NOTHING;
    SELECT * INTO session
    FROM public.fs2_scientific_artifact_upload_sessions
    WHERE upload_id=p_upload_id;
    IF session.tenant_id<>p_tenant_id OR session.storage_key<>p_storage_key
       OR session.provider_upload_id<>p_provider_upload_id
       OR session.part_size_bytes<>p_part_size_bytes OR session.part_count<>p_part_count
       OR session.provider_stability_grace_seconds<>claim.provider_stability_grace_seconds THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact upload session already differs';
    END IF;
    UPDATE public.fs2_scientific_artifact_upload_session_creation_claims
    SET state='bound',provider_upload_id=p_provider_upload_id
    WHERE upload_id=p_upload_id AND claim_id=p_claim_id AND state='creating';
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact upload-session owner changed';
    END IF;
    RETURN session;
END
$function$;

CREATE FUNCTION fs2_scientific_claim_stale_upload_session_creations_v2(p_limit integer)
RETURNS TABLE(
    upload_id uuid,tenant_id text,storage_key text,claim_id uuid,
    claim_generation integer,claimed_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    candidate record;
    claim_time timestamptz := clock_timestamp();
BEGIN
    IF p_limit NOT BETWEEN 1 AND 500 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='upload-session reconcile limit is invalid';
    END IF;
    FOR candidate IN
        SELECT claim.*
        FROM public.fs2_scientific_artifact_upload_session_creation_claims claim
        WHERE claim.state IN ('creating','reconciling') AND claim.reconcile_after<=claim_time
        ORDER BY claim.reconcile_after,claim.tenant_id,claim.upload_id
        FOR UPDATE SKIP LOCKED
        LIMIT p_limit
    LOOP
        UPDATE public.fs2_scientific_artifact_upload_session_creation_claims claim
        SET state='reconciling',claim_generation=claim.claim_generation+1,
            reconciliation_claimed_at=claim_time,reconcile_after=claim_time+interval '5 minutes'
        WHERE claim.upload_id=candidate.upload_id
          AND claim.claim_generation=candidate.claim_generation
          AND claim.state IN ('creating','reconciling')
        RETURNING claim.upload_id,claim.tenant_id,claim.storage_key,claim.claim_id,
                  claim.claim_generation,claim.reconciliation_claimed_at
        INTO upload_id,tenant_id,storage_key,claim_id,claim_generation,claimed_at;
        IF FOUND THEN RETURN NEXT; END IF;
    END LOOP;
END
$function$;

CREATE FUNCTION fs2_scientific_record_upload_session_creation_reconciled_v2(
    p_upload_id uuid,p_tenant_id text,p_storage_key text,p_claim_id uuid,
    p_claim_generation integer,p_claimed_at timestamptz,p_provider_request_id text,
    p_aborted_upload_count integer,p_multipart_session_set_digest text,p_observed_at timestamptz
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    claim record;
BEGIN
    IF length(p_provider_request_id) NOT BETWEEN 1 AND 512
       OR p_aborted_upload_count NOT BETWEEN 0 AND 10000
       OR p_multipart_session_set_digest<>'sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945'
       OR p_observed_at<p_claimed_at OR p_observed_at>clock_timestamp()+interval '5 minutes' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='upload-session reconciliation evidence is invalid';
    END IF;
    SELECT * INTO claim FROM public.fs2_scientific_artifact_upload_session_creation_claims
    WHERE upload_id=p_upload_id FOR UPDATE;
    IF NOT FOUND OR claim.state<>'reconciling' OR claim.tenant_id<>p_tenant_id
       OR claim.storage_key<>p_storage_key OR claim.claim_id<>p_claim_id
       OR claim.claim_generation<>p_claim_generation
       OR claim.reconciliation_claimed_at<>p_claimed_at THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='upload-session reconciliation claim is stale';
    END IF;
    INSERT INTO public.fs2_scientific_artifact_upload_session_reconciliation_events(
        upload_id,claim_id,claim_generation,tenant_id,storage_key,provider_request_id,
        aborted_upload_count,multipart_session_set_digest,observed_at
    ) VALUES(
        p_upload_id,p_claim_id,p_claim_generation,p_tenant_id,p_storage_key,p_provider_request_id,
        p_aborted_upload_count,p_multipart_session_set_digest,p_observed_at
    ) ON CONFLICT (upload_id,claim_generation) DO NOTHING;
    IF p_aborted_upload_count>0 OR claim.empty_observed_at IS NULL THEN
        UPDATE public.fs2_scientific_artifact_upload_session_creation_claims
        SET state='reconciling',empty_observed_at=p_observed_at,
            reconcile_after=p_observed_at
                +make_interval(secs=>provider_stability_grace_seconds)
        WHERE upload_id=p_upload_id AND state='reconciling'
          AND claim_id=p_claim_id AND claim_generation=p_claim_generation
          AND reconciliation_claimed_at=p_claimed_at;
    ELSIF p_observed_at<claim.empty_observed_at
            +make_interval(secs=>claim.provider_stability_grace_seconds) THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='upload-session provider quiet interval is incomplete';
    ELSE
        UPDATE public.fs2_scientific_artifact_upload_session_creation_claims
        SET state='reconciled',reconciled_at=p_observed_at
        WHERE upload_id=p_upload_id AND state='reconciling'
          AND claim_id=p_claim_id AND claim_generation=p_claim_generation
          AND reconciliation_claimed_at=p_claimed_at;
    END IF;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='upload-session reconciliation compare-and-set failed';
    END IF;
END
$function$;

CREATE FUNCTION fs2_scientific_record_upload_session_aborted_v2(
    p_upload_id uuid,
    p_tenant_id text,
    p_session_generation integer,
    p_provider_upload_id text,
    p_provider_request_id text,
    p_observed_at timestamptz
) RETURNS fs2_scientific_artifact_upload_sessions
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    session record;
BEGIN
    IF length(p_provider_request_id) NOT BETWEEN 1 AND 512
       OR p_observed_at>clock_timestamp()+interval '5 minutes' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='artifact upload-session abort receipt is invalid';
    END IF;
    SELECT * INTO session
    FROM public.fs2_scientific_artifact_upload_sessions
    WHERE upload_id=p_upload_id AND tenant_id=p_tenant_id
    FOR UPDATE;
    IF NOT FOUND OR session.session_generation<>p_session_generation
       OR session.provider_upload_id<>p_provider_upload_id THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact upload-session abort is stale';
    END IF;
    IF session.state IN ('active','legacy') THEN
        UPDATE public.fs2_scientific_artifact_upload_sessions
        SET state='aborted',terminal_at=p_observed_at,
            terminal_provider_request_id=p_provider_request_id
        WHERE upload_id=p_upload_id AND state IN ('active','legacy');
    ELSIF session.state<>'aborted' THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='completed upload session cannot be aborted';
    END IF;
    SELECT * INTO session
    FROM public.fs2_scientific_artifact_upload_sessions
    WHERE upload_id=p_upload_id;
    RETURN session;
END
$function$;

CREATE FUNCTION fs2_scientific_mark_upload_session_completed_v2(
    p_upload_id uuid,
    p_tenant_id text,
    p_session_generation integer,
    p_provider_upload_id text,
    p_provider_version_id text,
    p_provider_request_id text,
    p_observed_at timestamptz
) RETURNS fs2_scientific_artifact_upload_sessions
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    session record;
BEGIN
    IF length(p_provider_version_id) NOT BETWEEN 1 AND 1024
       OR length(p_provider_request_id) NOT BETWEEN 1 AND 512
       OR p_observed_at>clock_timestamp()+interval '5 minutes' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='artifact upload-session completion is invalid';
    END IF;
    SELECT * INTO session
    FROM public.fs2_scientific_artifact_upload_sessions
    WHERE upload_id=p_upload_id AND tenant_id=p_tenant_id
    FOR UPDATE;
    IF NOT FOUND OR session.session_generation<>p_session_generation
       OR session.provider_upload_id<>p_provider_upload_id
       OR session.state NOT IN ('active','legacy','completed') THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact upload-session completion is stale';
    END IF;
    IF session.state IN ('active','legacy') THEN
        UPDATE public.fs2_scientific_artifact_upload_sessions
        SET state='completed',provider_version_id=p_provider_version_id,
            terminal_at=p_observed_at,terminal_provider_request_id=p_provider_request_id
        WHERE upload_id=p_upload_id AND state IN ('active','legacy');
    ELSIF session.provider_version_id<>p_provider_version_id THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact upload-session version already differs';
    END IF;
    SELECT * INTO session
    FROM public.fs2_scientific_artifact_upload_sessions
    WHERE upload_id=p_upload_id;
    RETURN session;
END
$function$;

CREATE FUNCTION fs2_scientific_acquire_artifact_finalization_lease_v2(
    p_upload_id uuid,
    p_operation_id uuid,
    p_tenant_id text,
    p_session_generation integer,
    p_lease_id uuid
) RETURNS fs2_scientific_artifact_finalization_leases
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    reservation record;
    session record;
    lease record;
BEGIN
    SELECT quota.*,upload.artifact_id INTO reservation
    FROM public.fs2_scientific_artifact_quota_reservations quota
    JOIN public.fs2_scientific_uploads upload ON upload.id=quota.upload_id
    WHERE quota.upload_id=p_upload_id AND quota.operation_id=p_operation_id
      AND quota.tenant_id=p_tenant_id
    FOR UPDATE OF quota,upload;
    IF NOT FOUND OR reservation.state<>'active' OR reservation.artifact_id IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact finalization fence has closed';
    END IF;
    SELECT * INTO lease
    FROM public.fs2_scientific_artifact_finalization_leases
    WHERE upload_id=p_upload_id
    FOR UPDATE;
    IF FOUND THEN
        IF lease.session_generation<>p_session_generation OR lease.state<>'active'
           OR lease.expires_at<=clock_timestamp() THEN
            RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact finalization lease already differs';
        END IF;
        RETURN lease;
    END IF;
    IF GREATEST(reservation.expires_at,reservation.latest_upload_capability_expires_at)
       +make_interval(secs=>reservation.upload_completion_grace_seconds)<=clock_timestamp() THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact finalization fence has closed';
    END IF;
    SELECT * INTO session
    FROM public.fs2_scientific_artifact_upload_sessions
    WHERE upload_id=p_upload_id AND tenant_id=p_tenant_id
    FOR UPDATE;
    IF NOT FOUND OR session.session_generation<>p_session_generation
       OR session.state NOT IN ('active','legacy','completed') THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact upload session cannot finalize';
    END IF;
    INSERT INTO public.fs2_scientific_artifact_finalization_leases(
        upload_id,lease_id,tenant_id,lease_generation,session_generation,state,acquired_at,expires_at
    ) VALUES(
        p_upload_id,p_lease_id,p_tenant_id,1,p_session_generation,'active',clock_timestamp(),
        clock_timestamp()+make_interval(secs=>reservation.upload_completion_grace_seconds)
    ) RETURNING * INTO lease;
    RETURN lease;
END
$function$;

CREATE FUNCTION fs2_scientific_claim_expired_finalization_leases_v2(p_limit integer)
RETURNS TABLE(
    upload_id uuid,operation_id uuid,tenant_id text,storage_key text,
    provider_upload_id text,session_generation integer,part_size_bytes bigint,
    part_count integer,provider_stability_grace_seconds integer,
    initiated_at timestamptz,session_state text,
    session_provider_version_id text,lease_id uuid,lease_generation integer,
    acquired_at timestamptz,expires_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    candidate record;
    claim_time timestamptz := clock_timestamp();
BEGIN
    IF p_limit NOT BETWEEN 1 AND 500 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='finalization recovery limit is invalid';
    END IF;
    FOR candidate IN
        SELECT lease.upload_id,upload.operation_id,upload.tenant_id,upload.storage_key,
               session.provider_upload_id,session.session_generation,session.part_size_bytes,
               session.part_count,session.initiated_at,session.state AS session_state,
               session.provider_version_id AS session_provider_version_id,
               lease.lease_id,lease.lease_generation,
               reservation.upload_completion_grace_seconds,
               reservation.provider_stability_grace_seconds
        FROM public.fs2_scientific_artifact_finalization_leases lease
        JOIN public.fs2_scientific_uploads upload ON upload.id=lease.upload_id
        JOIN public.fs2_scientific_artifact_upload_sessions session ON session.upload_id=lease.upload_id
        JOIN public.fs2_scientific_artifact_quota_reservations reservation
          ON reservation.upload_id=lease.upload_id
        WHERE lease.state='active' AND lease.expires_at<=claim_time
          AND upload.artifact_id IS NULL AND reservation.state='active'
          AND session.state IN ('active','legacy','completed')
        ORDER BY lease.expires_at,lease.upload_id
        FOR UPDATE OF lease SKIP LOCKED
        LIMIT p_limit
    LOOP
        UPDATE public.fs2_scientific_artifact_finalization_leases lease
        SET lease_generation=lease.lease_generation+1,acquired_at=claim_time,
            expires_at=claim_time+make_interval(secs=>candidate.upload_completion_grace_seconds)
        WHERE lease.upload_id=candidate.upload_id AND lease.state='active'
          AND lease.lease_generation=candidate.lease_generation AND lease.expires_at<=claim_time
        RETURNING lease.lease_generation,lease.acquired_at,lease.expires_at
        INTO lease_generation,acquired_at,expires_at;
        IF NOT FOUND THEN CONTINUE; END IF;
        upload_id := candidate.upload_id;
        operation_id := candidate.operation_id;
        tenant_id := candidate.tenant_id;
        storage_key := candidate.storage_key;
        provider_upload_id := candidate.provider_upload_id;
        session_generation := candidate.session_generation;
        part_size_bytes := candidate.part_size_bytes;
        part_count := candidate.part_count;
        provider_stability_grace_seconds := candidate.provider_stability_grace_seconds;
        initiated_at := candidate.initiated_at;
        session_state := candidate.session_state;
        session_provider_version_id := candidate.session_provider_version_id;
        lease_id := candidate.lease_id;
        RETURN NEXT;
    END LOOP;
END
$function$;

CREATE FUNCTION fs2_scientific_record_finalization_failure_v2(
    p_upload_id uuid,p_operation_id uuid,p_tenant_id text,p_lease_id uuid,
    p_lease_generation integer,p_session_generation integer,p_provider_upload_id text,
    p_provider_version_id text,p_provider_request_id text,p_failure_code text,
    p_observed_digest text,p_observed_size_bytes bigint,p_observed_media_type text,
    p_observed_compression text,p_observed_at timestamptz
) RETURNS fs2_scientific_artifact_finalization_failures
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    upload record;
    session record;
    lease record;
    evidence record;
BEGIN
    IF p_failure_code NOT IN ('content_verification_failed','artifact_policy_failed')
       OR length(p_provider_version_id) NOT BETWEEN 1 AND 1024
       OR length(p_provider_request_id) NOT BETWEEN 1 AND 512
       OR p_observed_digest !~ '^sha256:[a-f0-9]{64}$'
       OR p_observed_size_bytes NOT BETWEEN 0 AND 1099511627776
       OR length(p_observed_media_type) NOT BETWEEN 3 AND 128
       OR (p_observed_compression IS NOT NULL
           AND length(p_observed_compression) NOT BETWEEN 1 AND 32)
       OR p_observed_at>clock_timestamp()+interval '5 minutes' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='artifact finalization failure evidence is invalid';
    END IF;
    SELECT * INTO upload FROM public.fs2_scientific_uploads
    WHERE id=p_upload_id AND operation_id=p_operation_id AND tenant_id=p_tenant_id
      AND artifact_id IS NULL FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact finalization failure upload fence is stale';
    END IF;
    SELECT * INTO session FROM public.fs2_scientific_artifact_upload_sessions
    WHERE upload_id=p_upload_id AND tenant_id=p_tenant_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact finalization failure session fence is stale';
    END IF;
    SELECT * INTO lease FROM public.fs2_scientific_artifact_finalization_leases
    WHERE upload_id=p_upload_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact finalization failure lease fence is stale';
    END IF;
    IF lease.state<>'active' OR lease.lease_id<>p_lease_id
       OR lease.lease_generation<>p_lease_generation
       OR lease.session_generation<>p_session_generation
       OR session.state NOT IN ('active','legacy') OR session.session_generation<>p_session_generation
       OR session.provider_upload_id<>p_provider_upload_id THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact finalization failure fence is stale';
    END IF;
    INSERT INTO public.fs2_scientific_artifact_finalization_failures(
        upload_id,tenant_id,lease_id,lease_generation,session_generation,
        provider_upload_id,provider_version_id,provider_request_id,failure_code,
        observed_digest,observed_size_bytes,observed_media_type,observed_compression,observed_at
    ) VALUES(
        p_upload_id,p_tenant_id,p_lease_id,p_lease_generation,p_session_generation,
        p_provider_upload_id,p_provider_version_id,p_provider_request_id,p_failure_code,
        p_observed_digest,p_observed_size_bytes,p_observed_media_type,p_observed_compression,p_observed_at
    ) ON CONFLICT (upload_id) DO NOTHING;
    SELECT * INTO evidence FROM public.fs2_scientific_artifact_finalization_failures
    WHERE upload_id=p_upload_id;
    IF evidence.tenant_id<>p_tenant_id
       OR evidence.lease_id<>p_lease_id OR evidence.lease_generation<>p_lease_generation
       OR evidence.session_generation<>p_session_generation
       OR evidence.provider_upload_id<>p_provider_upload_id
       OR evidence.provider_version_id<>p_provider_version_id
       OR evidence.provider_request_id<>p_provider_request_id
       OR evidence.failure_code<>p_failure_code OR evidence.observed_digest<>p_observed_digest
       OR evidence.observed_size_bytes<>p_observed_size_bytes
       OR evidence.observed_media_type<>p_observed_media_type
       OR evidence.observed_compression IS DISTINCT FROM p_observed_compression
       OR evidence.observed_at<>p_observed_at THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact finalization failure already differs';
    END IF;
    UPDATE public.fs2_scientific_artifact_upload_sessions
    SET state='completed',provider_version_id=p_provider_version_id,
        terminal_at=p_observed_at,terminal_provider_request_id=p_provider_request_id
    WHERE upload_id=p_upload_id AND state IN ('active','legacy')
      AND session_generation=p_session_generation;
    UPDATE public.fs2_scientific_artifact_finalization_leases
    SET state='failed',failed_at=p_observed_at,failure_code=p_failure_code,
        provider_version_id=p_provider_version_id
    WHERE upload_id=p_upload_id AND state='active' AND lease_id=p_lease_id
      AND lease_generation=p_lease_generation;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact finalization failure compare-and-set failed';
    END IF;
    RETURN evidence;
END
$function$;

CREATE FUNCTION fs2_scientific_record_upload_capability_v2(
    p_upload_id uuid,
    p_tenant_id text,
    p_capability_id uuid,
    p_session_generation integer,
    p_part_number integer,
    p_size_bytes bigint,
    p_checksum text,
    p_media_type text,
    p_compression text,
    p_expires_at timestamptz
) RETURNS fs2_scientific_artifact_quota_reservations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    reservation record;
    session record;
    upload record;
    stored record;
BEGIN
    IF p_expires_at<clock_timestamp()-interval '1 minute'
       OR p_expires_at>clock_timestamp()+interval '16 minutes'
       OR p_part_number NOT BETWEEN 1 AND 10000
       OR p_size_bytes NOT BETWEEN 0 AND 5368709120
       OR p_checksum !~ '^sha256:[a-f0-9]{64}$'
       OR p_media_type !~ '^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$'
       OR (p_compression IS NOT NULL AND p_compression NOT IN ('gzip','zstd')) THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='upload capability expiry is outside the signed bound';
    END IF;
    SELECT * INTO reservation
    FROM public.fs2_scientific_artifact_quota_reservations
    WHERE upload_id=p_upload_id AND tenant_id=p_tenant_id
    FOR UPDATE;
    IF NOT FOUND OR reservation.state<>'active'
       OR reservation.expires_at<=clock_timestamp() THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='upload capability cannot extend a fenced reservation';
    END IF;
    SELECT * INTO session
    FROM public.fs2_scientific_artifact_upload_sessions
    WHERE upload_id=p_upload_id AND tenant_id=p_tenant_id
    FOR UPDATE;
    SELECT * INTO upload
    FROM public.fs2_scientific_uploads
    WHERE id=p_upload_id AND tenant_id=p_tenant_id;
    IF session.upload_id IS NULL OR upload.id IS NULL
       OR session.state NOT IN ('active','legacy')
       OR session.session_generation<>p_session_generation
       OR (session.state='legacy' AND (p_part_number<>1 OR session.part_count<>1))
       OR upload.media_type<>p_media_type
       OR upload.compression IS DISTINCT FROM p_compression
       OR (session.state='legacy' AND (
            upload.expected_size_bytes<>p_size_bytes OR upload.expected_digest<>p_checksum
          ))
       OR p_part_number>session.part_count
       OR p_size_bytes<>CASE
            WHEN p_part_number<session.part_count THEN session.part_size_bytes
            ELSE reservation.reserved_bytes-session.part_size_bytes*(session.part_count-1)
          END
       OR EXISTS (
            SELECT 1 FROM public.fs2_scientific_artifact_upload_capabilities prior
            WHERE prior.upload_id=p_upload_id
              AND prior.session_generation=p_session_generation
              AND prior.part_number=p_part_number
              AND (
                    prior.size_bytes<>p_size_bytes OR prior.checksum<>p_checksum
                    OR prior.media_type<>p_media_type
                    OR prior.compression IS DISTINCT FROM p_compression
                  )
       ) THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='upload part capability differs from its session fence';
    END IF;
    INSERT INTO public.fs2_scientific_artifact_upload_capabilities(
        capability_id,upload_id,tenant_id,session_generation,part_number,size_bytes,checksum,
        media_type,compression,expires_at
    ) VALUES(
        p_capability_id,p_upload_id,p_tenant_id,p_session_generation,p_part_number,
        p_size_bytes,p_checksum,p_media_type,p_compression,p_expires_at
    )
    ON CONFLICT (capability_id) DO NOTHING;
    SELECT * INTO stored FROM public.fs2_scientific_artifact_upload_capabilities
    WHERE capability_id=p_capability_id;
    IF stored.upload_id<>p_upload_id OR stored.tenant_id<>p_tenant_id
       OR stored.session_generation<>p_session_generation
       OR stored.part_number<>p_part_number OR stored.size_bytes<>p_size_bytes
       OR stored.checksum<>p_checksum
       OR stored.media_type<>p_media_type
       OR stored.compression IS DISTINCT FROM p_compression
       OR stored.expires_at<>p_expires_at THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='upload capability identity already differs';
    END IF;
    UPDATE public.fs2_scientific_artifact_quota_reservations
    SET latest_upload_capability_expires_at=GREATEST(
        latest_upload_capability_expires_at,p_expires_at
    )
    WHERE upload_id=p_upload_id AND tenant_id=p_tenant_id AND state='active';
    SELECT * INTO reservation
    FROM public.fs2_scientific_artifact_quota_reservations
    WHERE upload_id=p_upload_id;
    RETURN reservation;
END
$function$;

CREATE FUNCTION fs2_scientific_publish_artifact_v2(
    p_upload_id uuid,
    p_operation_id uuid,
    p_tenant_id text,
    p_artifact_id uuid,
    p_lease_id uuid,
    p_lease_generation integer,
    p_session_generation integer,
    p_provider_upload_id text,
    p_provider_version_id text,
    p_provider_request_id text,
    p_observed_digest text,
    p_observed_size_bytes bigint,
    p_observed_media_type text,
    p_observed_compression text,
    p_observed_at timestamptz
) RETURNS fs2_scientific_artifacts
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    upload record;
    attempt record;
    reservation record;
    session record;
    lease record;
    artifact public.fs2_scientific_artifacts%ROWTYPE;
    published_at timestamptz := clock_timestamp();
BEGIN
    IF length(p_provider_version_id) NOT BETWEEN 1 AND 1024
       OR length(p_provider_request_id) NOT BETWEEN 1 AND 512
       OR p_observed_digest !~ '^sha256:[a-f0-9]{64}$'
       OR p_observed_size_bytes NOT BETWEEN 0 AND 1099511627776
       OR length(p_observed_media_type) NOT BETWEEN 3 AND 128
       OR p_observed_compression IS NOT NULL
          AND p_observed_compression NOT IN ('gzip','zstd')
       OR p_observed_at>published_at+interval '5 minutes' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='artifact publication evidence is invalid';
    END IF;
    SELECT * INTO upload
    FROM public.fs2_scientific_uploads
    WHERE id=p_upload_id AND operation_id=p_operation_id AND tenant_id=p_tenant_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact upload identity is stale';
    END IF;
    IF upload.artifact_id IS NOT NULL THEN
        SELECT * INTO artifact FROM public.fs2_scientific_artifacts
        WHERE id=upload.artifact_id AND provider_version_id=p_provider_version_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact upload is already bound differently';
        END IF;
        RETURN artifact;
    END IF;
    IF upload.expected_digest<>p_observed_digest
       OR upload.expected_size_bytes<>p_observed_size_bytes
       OR upload.media_type<>p_observed_media_type
       OR upload.compression IS DISTINCT FROM p_observed_compression THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='provider object differs from upload intent';
    END IF;
    SELECT * INTO reservation
    FROM public.fs2_scientific_artifact_quota_reservations
    WHERE upload_id=p_upload_id AND operation_id=p_operation_id
      AND tenant_id=p_tenant_id AND state='active'
    FOR UPDATE;
    SELECT * INTO lease
    FROM public.fs2_scientific_artifact_finalization_leases
    WHERE upload_id=p_upload_id
    FOR UPDATE;
    SELECT * INTO session
    FROM public.fs2_scientific_artifact_upload_sessions
    WHERE upload_id=p_upload_id AND tenant_id=p_tenant_id
    FOR UPDATE;
    IF reservation IS NULL OR lease IS NULL OR session IS NULL
       OR lease.lease_id<>p_lease_id OR lease.lease_generation<>p_lease_generation
       OR lease.session_generation<>p_session_generation OR lease.state<>'active'
       OR session.session_generation<>p_session_generation
       OR session.provider_upload_id<>p_provider_upload_id
       OR session.state NOT IN ('active','legacy','completed')
       OR session.state='completed' AND session.provider_version_id<>p_provider_version_id THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact publication fence is stale';
    END IF;
    SELECT * INTO attempt
    FROM public.fs2_scientific_stage_attempts
    WHERE attempt_id=upload.attempt_id AND operation_id=p_operation_id
      AND tenant_id=p_tenant_id
    FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact attempt identity is stale';
    END IF;
    IF session.state IN ('active','legacy') THEN
        UPDATE public.fs2_scientific_artifact_upload_sessions
        SET state='completed',provider_version_id=p_provider_version_id,
            terminal_at=p_observed_at,terminal_provider_request_id=p_provider_request_id
        WHERE upload_id=p_upload_id AND state IN ('active','legacy');
    END IF;
    INSERT INTO public.fs2_scientific_artifacts(
        id,attempt_id,operation_id,tenant_id,stage_id,shard_id,direction,digest,size_bytes,
        media_type,compression,storage_key,provider_version_id,access_profile,
        access_receipt_digest,retention_expires_at,created_at
    ) VALUES(
        p_artifact_id,upload.attempt_id,upload.operation_id,upload.tenant_id,
        upload.stage_id,upload.shard_id,upload.direction,upload.expected_digest,
        upload.expected_size_bytes,upload.media_type,upload.compression,upload.storage_key,
        p_provider_version_id,upload.access_profile,upload.access_receipt_digest,
        published_at+(attempt.retention_expires_at-attempt.started_at),published_at
    ) RETURNING * INTO artifact;
    UPDATE public.fs2_scientific_uploads
    SET artifact_id=p_artifact_id,provider_version_id=p_provider_version_id,finalized_at=published_at
    WHERE id=p_upload_id AND artifact_id IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact upload publication compare-and-set failed';
    END IF;
    UPDATE public.fs2_scientific_artifact_finalization_leases
    SET state='completed',completed_at=published_at,provider_version_id=p_provider_version_id
    WHERE upload_id=p_upload_id AND lease_id=p_lease_id
      AND lease_generation=p_lease_generation AND state='active';
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact finalization lease compare-and-set failed';
    END IF;
    UPDATE public.fs2_scientific_artifact_quota_reservations
    SET expires_at=artifact.retention_expires_at
    WHERE upload_id=p_upload_id AND state='active';
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact quota reservation changed';
    END IF;
    INSERT INTO public.fs2_scientific_artifact_quota_events(
        upload_id,operation_id,attempt_id,tenant_id,event_type,reserved_bytes,
        reserved_objects,expires_at,occurred_at
    ) VALUES(
        reservation.upload_id,reservation.operation_id,reservation.attempt_id,
        reservation.tenant_id,'retention_extended',reservation.reserved_bytes,
        reservation.reserved_objects,artifact.retention_expires_at,published_at
    );
    INSERT INTO public.fs2_scientific_artifact_events(
        event_type,operation_id,tenant_id,stage_id,attempt_id,upload_id,artifact_id,occurred_at
    ) VALUES(
        'artifact_finalized',upload.operation_id,upload.tenant_id,upload.stage_id,
        upload.attempt_id,p_upload_id,p_artifact_id,published_at
    );
    RETURN artifact;
END
$function$;

CREATE FUNCTION fs2_scientific_claim_legacy_artifact_versions_v2(p_limit integer)
RETURNS TABLE(
    artifact_id uuid,
    upload_id uuid,
    tenant_id text,
    storage_key text,
    expected_digest text,
    expected_size_bytes bigint,
    expected_media_type text,
    expected_compression text,
    claim_generation integer,
    claimed_at timestamptz,
    eligible_at timestamptz,
    list_key_marker text,
    list_version_id_marker text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    candidate record;
    claim_time timestamptz := clock_timestamp();
BEGIN
    IF p_limit NOT BETWEEN 1 AND 500 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='legacy artifact claim limit is invalid';
    END IF;
    FOR candidate IN
        SELECT claim.artifact_id,upload.id AS upload_id,artifact.tenant_id,
               artifact.storage_key,artifact.digest,artifact.size_bytes,
               artifact.media_type,artifact.compression,claim.claim_generation,
               claim.list_key_marker,claim.list_version_id_marker,
               artifact.created_at AS eligible_at
        FROM public.fs2_scientific_artifact_legacy_version_claims claim
        JOIN public.fs2_scientific_artifacts artifact ON artifact.id=claim.artifact_id
        JOIN public.fs2_scientific_uploads upload ON upload.artifact_id=artifact.id
        LEFT JOIN public.fs2_scientific_artifact_legacy_version_bindings binding
          ON binding.artifact_id=artifact.id
        WHERE artifact.provider_version_id IS NULL AND binding.artifact_id IS NULL
          AND claim.scan_completed_at IS NULL
          AND claim.retry_at<=claim_time
        ORDER BY claim.retry_at,artifact.created_at,claim.artifact_id
        FOR UPDATE OF claim SKIP LOCKED
        LIMIT p_limit
    LOOP
        UPDATE public.fs2_scientific_artifact_legacy_version_claims claim
        SET claim_generation=claim.claim_generation+1,claimed_at=claim_time,
            retry_at=claim_time+make_interval(
                secs=>LEAST(3600.0,30.0*power(2.0,LEAST(claim.claim_generation,6)))
            )
        WHERE claim.artifact_id=candidate.artifact_id
          AND claim.claim_generation=candidate.claim_generation;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;
        artifact_id := candidate.artifact_id;
        upload_id := candidate.upload_id;
        tenant_id := candidate.tenant_id;
        storage_key := candidate.storage_key;
        expected_digest := candidate.digest;
        expected_size_bytes := candidate.size_bytes;
        expected_media_type := candidate.media_type;
        expected_compression := candidate.compression;
        claim_generation := candidate.claim_generation+1;
        claimed_at := claim_time;
        eligible_at := candidate.eligible_at;
        list_key_marker := candidate.list_key_marker;
        list_version_id_marker := candidate.list_version_id_marker;
        RETURN NEXT;
    END LOOP;
END
$function$;

CREATE FUNCTION fs2_scientific_record_legacy_artifact_version_scan_v2(
    p_artifact_id uuid,
    p_claim_generation integer,
    p_claimed_at timestamptz,
    p_prior_key_marker text,
    p_prior_version_id_marker text,
    p_next_key_marker text,
    p_next_version_id_marker text,
    p_provider_request_id text,
    p_observed_at timestamptz
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    claim record;
    stored record;
BEGIN
    IF (p_prior_key_marker IS NULL)<>(p_prior_version_id_marker IS NULL)
       OR (p_next_key_marker IS NULL)<>(p_next_version_id_marker IS NULL)
       OR length(p_provider_request_id) NOT BETWEEN 1 AND 512
       OR p_observed_at<p_claimed_at
       OR p_observed_at>clock_timestamp()+interval '5 minutes' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='legacy artifact version scan evidence is invalid';
    END IF;
    SELECT * INTO claim
    FROM public.fs2_scientific_artifact_legacy_version_claims
    WHERE artifact_id=p_artifact_id
    FOR UPDATE;
    IF NOT FOUND OR claim.claim_generation<>p_claim_generation
       OR claim.claimed_at<>p_claimed_at OR claim.scan_completed_at IS NOT NULL
       OR claim.list_key_marker IS DISTINCT FROM p_prior_key_marker
       OR claim.list_version_id_marker IS DISTINCT FROM p_prior_version_id_marker
       OR EXISTS (
           SELECT 1 FROM public.fs2_scientific_artifact_legacy_version_bindings binding
           WHERE binding.artifact_id=p_artifact_id
       ) THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='legacy artifact version scan claim is stale';
    END IF;
    INSERT INTO public.fs2_scientific_artifact_legacy_version_scan_events(
        artifact_id,claim_generation,claimed_at,prior_key_marker,prior_version_id_marker,
        next_key_marker,next_version_id_marker,provider_request_id,observed_at
    ) VALUES(
        p_artifact_id,p_claim_generation,p_claimed_at,p_prior_key_marker,p_prior_version_id_marker,
        p_next_key_marker,p_next_version_id_marker,p_provider_request_id,p_observed_at
    ) ON CONFLICT (artifact_id,claim_generation) DO NOTHING;
    SELECT * INTO stored
    FROM public.fs2_scientific_artifact_legacy_version_scan_events
    WHERE artifact_id=p_artifact_id AND claim_generation=p_claim_generation;
    IF stored.claimed_at<>p_claimed_at
       OR stored.prior_key_marker IS DISTINCT FROM p_prior_key_marker
       OR stored.prior_version_id_marker IS DISTINCT FROM p_prior_version_id_marker
       OR stored.next_key_marker IS DISTINCT FROM p_next_key_marker
       OR stored.next_version_id_marker IS DISTINCT FROM p_next_version_id_marker
       OR stored.provider_request_id<>p_provider_request_id
       OR stored.observed_at<>p_observed_at THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='legacy artifact version scan evidence already differs';
    END IF;
    UPDATE public.fs2_scientific_artifact_legacy_version_claims
    SET list_key_marker=p_next_key_marker,
        list_version_id_marker=p_next_version_id_marker,
        scan_completed_at=CASE WHEN p_next_key_marker IS NULL THEN p_observed_at ELSE NULL END,
        resolution=CASE WHEN p_next_key_marker IS NULL THEN 'unresolved' ELSE 'pending' END,
        resolution_reason=CASE WHEN p_next_key_marker IS NULL
            THEN 'provider_version_not_found' ELSE NULL END,
        retry_at=CASE WHEN p_next_key_marker IS NULL
            THEN 'infinity'::timestamptz ELSE clock_timestamp() END
    WHERE artifact_id=p_artifact_id AND claim_generation=p_claim_generation
      AND claimed_at=p_claimed_at AND scan_completed_at IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='legacy artifact version scan compare-and-set failed';
    END IF;
END
$function$;

CREATE FUNCTION fs2_scientific_record_legacy_artifact_version_v2(
    p_artifact_id uuid,
    p_upload_id uuid,
    p_tenant_id text,
    p_storage_key text,
    p_claim_generation integer,
    p_claimed_at timestamptz,
    p_provider_version_id text,
    p_provider_request_id text,
    p_observed_digest text,
    p_observed_size_bytes bigint,
    p_observed_media_type text,
    p_observed_compression text,
    p_observed_at timestamptz
) RETURNS fs2_scientific_artifact_legacy_version_bindings
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    claim record;
    artifact record;
    upload record;
    binding record;
BEGIN
    IF length(p_provider_version_id) NOT BETWEEN 1 AND 1024
       OR length(p_provider_request_id) NOT BETWEEN 1 AND 512
       OR p_observed_digest !~ '^sha256:[a-f0-9]{64}$'
       OR p_observed_size_bytes NOT BETWEEN 0 AND 1099511627776
       OR p_observed_at>clock_timestamp()+interval '5 minutes' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='legacy artifact version evidence is invalid';
    END IF;
    SELECT * INTO claim
    FROM public.fs2_scientific_artifact_legacy_version_claims
    WHERE artifact_id=p_artifact_id
    FOR UPDATE;
    SELECT * INTO artifact
    FROM public.fs2_scientific_artifacts
    WHERE id=p_artifact_id AND tenant_id=p_tenant_id
    FOR SHARE;
    SELECT * INTO upload
    FROM public.fs2_scientific_uploads
    WHERE id=p_upload_id AND artifact_id=p_artifact_id AND tenant_id=p_tenant_id
    FOR SHARE;
    IF claim IS NULL OR artifact IS NULL OR upload IS NULL
       OR claim.claim_generation<>p_claim_generation OR claim.claimed_at<>p_claimed_at
       OR artifact.provider_version_id IS NOT NULL OR artifact.storage_key<>p_storage_key
       OR artifact.digest<>p_observed_digest OR artifact.size_bytes<>p_observed_size_bytes
       OR artifact.media_type<>p_observed_media_type
       OR artifact.compression IS DISTINCT FROM p_observed_compression THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='legacy artifact version claim is stale';
    END IF;
    INSERT INTO public.fs2_scientific_artifact_legacy_version_bindings(
        artifact_id,upload_id,tenant_id,storage_key,provider_version_id,
        provider_request_id,observed_digest,observed_size_bytes,observed_at
    ) VALUES(
        p_artifact_id,p_upload_id,p_tenant_id,p_storage_key,p_provider_version_id,
        p_provider_request_id,p_observed_digest,p_observed_size_bytes,p_observed_at
    ) ON CONFLICT (artifact_id) DO NOTHING;
    SELECT * INTO binding
    FROM public.fs2_scientific_artifact_legacy_version_bindings
    WHERE artifact_id=p_artifact_id;
    IF binding.upload_id<>p_upload_id OR binding.tenant_id<>p_tenant_id
       OR binding.storage_key<>p_storage_key
       OR binding.provider_version_id<>p_provider_version_id
       OR binding.provider_request_id<>p_provider_request_id
       OR binding.observed_digest<>p_observed_digest
       OR binding.observed_size_bytes<>p_observed_size_bytes
       OR binding.observed_at<>p_observed_at THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='legacy artifact version is already bound differently';
    END IF;
    UPDATE public.fs2_scientific_artifact_legacy_version_claims
    SET resolution='bound',resolution_reason='provider_version_bound',
        scan_completed_at=p_observed_at,retry_at='infinity'::timestamptz
    WHERE artifact_id=p_artifact_id AND claim_generation=p_claim_generation
      AND claimed_at=p_claimed_at AND resolution='pending';
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='legacy artifact version resolution changed';
    END IF;
    RETURN binding;
END
$function$;

CREATE FUNCTION fs2_scientific_legacy_version_rollout_status_v2()
RETURNS TABLE(
    pending bigint,bound bigint,unresolved bigint,unbound_artifacts bigint,
    missing_unfinished_upload_sessions bigint
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    SELECT
        count(*) FILTER (WHERE claim.resolution='pending')::bigint,
        count(*) FILTER (WHERE claim.resolution='bound')::bigint,
        count(*) FILTER (WHERE claim.resolution='unresolved')::bigint,
        (
            SELECT count(*)::bigint
            FROM public.fs2_scientific_artifacts artifact
            LEFT JOIN public.fs2_scientific_artifact_legacy_version_bindings binding
              ON binding.artifact_id=artifact.id
            WHERE artifact.provider_version_id IS NULL AND binding.artifact_id IS NULL
        ),
        (
            SELECT count(*)::bigint
            FROM public.fs2_scientific_uploads upload
            LEFT JOIN public.fs2_scientific_artifact_upload_sessions session
              ON session.upload_id=upload.id
            WHERE upload.artifact_id IS NULL AND session.upload_id IS NULL
        )
    FROM public.fs2_scientific_artifact_legacy_version_claims claim
$function$;

CREATE FUNCTION fs2_scientific_mark_schema_bridge_ready_v2(
    p_bridge_image_ref text,
    p_bridge_release_revision bigint,
    p_predecessor_image_ref text,
    p_deployment_namespace text,
    p_deployment_name text,
    p_deployment_uid text,
    p_deployment_generation bigint,
    p_deployment_observed_generation bigint,
    p_deployment_desired_replicas integer,
    p_deployment_updated_replicas integer,
    p_deployment_ready_replicas integer,
    p_deployment_available_replicas integer,
    p_runtime_pod_count integer,
    p_runtime_pod_set_digest text,
    p_kubernetes_audit_id text,
    p_kubernetes_observed_at timestamptz
) RETURNS fs2_schema_bridge_ready_receipts
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    rollout record;
    status record;
    receipt record;
    cleanup_fence timestamptz;
    recorded_at timestamptz := clock_timestamp();
BEGIN
    IF p_bridge_image_ref !~ '^[^@[:space:]]+@sha256:[a-f0-9]{64}$'
       OR p_predecessor_image_ref !~ '^[^@[:space:]]+@sha256:[a-f0-9]{64}$'
       OR p_bridge_release_revision<1
       OR p_deployment_namespace !~ '^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$'
       OR p_deployment_name !~ '^[a-z0-9](?:[-a-z0-9]{0,251}[a-z0-9])?$'
       OR p_deployment_uid !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
       OR p_deployment_generation<1
       OR p_deployment_observed_generation<>p_deployment_generation
       OR p_deployment_desired_replicas<1
       OR p_deployment_updated_replicas<>p_deployment_desired_replicas
       OR p_deployment_ready_replicas<>p_deployment_desired_replicas
       OR p_deployment_available_replicas<>p_deployment_desired_replicas
       OR p_runtime_pod_count<>p_deployment_desired_replicas
       OR p_runtime_pod_set_digest !~ '^[a-f0-9]{64}$'
       OR length(p_kubernetes_audit_id) NOT BETWEEN 1 AND 200
       OR p_kubernetes_observed_at>recorded_at+interval '5 minutes'
       OR p_kubernetes_observed_at<recorded_at-interval '15 minutes' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='schema bridge receipt is invalid';
    END IF;
    SELECT * INTO rollout
    FROM public.fs2_schema_rollout_state
    WHERE singleton
    FOR UPDATE;
    IF rollout.phase NOT IN ('expanded','contracted')
       OR rollout.bridge_image_ref<>p_bridge_image_ref
       OR rollout.predecessor_image_ref<>p_predecessor_image_ref
       OR p_kubernetes_observed_at+interval '1 second'<rollout.bridge_registered_at THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='schema bridge registration differs';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.fs2_schema_bridge_rollout_attempts attempt
        WHERE attempt.bridge_image_ref=p_bridge_image_ref
          AND attempt.predecessor_image_ref=p_predecessor_image_ref
          AND attempt.bridge_release_revision=p_bridge_release_revision
    ) THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='schema bridge attempt is not registered';
    END IF;
    SELECT * INTO receipt FROM public.fs2_schema_bridge_ready_receipts
    WHERE migration_version='0031_scientific_quota_fencing.sql'
      AND bridge_release_revision=p_bridge_release_revision;
    IF FOUND THEN
        IF receipt.bridge_image_ref<>p_bridge_image_ref
           OR receipt.predecessor_image_ref<>p_predecessor_image_ref THEN
            RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='schema bridge receipt already differs';
        END IF;
        RETURN receipt;
    END IF;
    SELECT * INTO status FROM public.fs2_scientific_legacy_version_rollout_status_v2();
    IF status.pending<>0 OR status.unresolved<>0 OR status.unbound_artifacts<>0
       OR status.missing_unfinished_upload_sessions<>0 THEN
        RAISE EXCEPTION USING ERRCODE='FS202',
            MESSAGE='schema bridge cannot route with unresolved legacy artifacts';
    END IF;
    SELECT p_kubernetes_observed_at+interval '15 minutes'
           +make_interval(secs=>COALESCE(max(upload_completion_grace_seconds),3600))
           +make_interval(secs=>COALESCE(max(provider_stability_grace_seconds),3600))
    INTO cleanup_fence
    FROM public.fs2_scientific_artifact_quota_reservations;
    INSERT INTO public.fs2_schema_bridge_ready_receipts(
        migration_version,bridge_image_ref,bridge_release_revision,predecessor_image_ref,
        deployment_namespace,deployment_name,deployment_uid,deployment_generation,
        deployment_observed_generation,deployment_desired_replicas,
        deployment_updated_replicas,deployment_ready_replicas,
        deployment_available_replicas,runtime_pod_count,runtime_pod_set_digest,
        kubernetes_audit_id,
        predecessor_drained_at,cleanup_not_before,bound_legacy_artifacts,
        pending_legacy_artifacts,unresolved_legacy_artifacts,
        missing_unfinished_upload_sessions,recorded_at
    ) VALUES(
        '0031_scientific_quota_fencing.sql',p_bridge_image_ref,p_bridge_release_revision,
        p_predecessor_image_ref,p_deployment_namespace,p_deployment_name,p_deployment_uid,
        p_deployment_generation,p_deployment_observed_generation,p_deployment_desired_replicas,
        p_deployment_updated_replicas,p_deployment_ready_replicas,
        p_deployment_available_replicas,p_runtime_pod_count,p_runtime_pod_set_digest,
        p_kubernetes_audit_id,p_kubernetes_observed_at,cleanup_fence,status.bound,0,0,0,recorded_at
    ) ON CONFLICT (migration_version,bridge_release_revision) DO NOTHING;
    SELECT * INTO receipt FROM public.fs2_schema_bridge_ready_receipts
    WHERE migration_version='0031_scientific_quota_fencing.sql'
      AND bridge_release_revision=p_bridge_release_revision;
    IF receipt.bridge_image_ref<>p_bridge_image_ref
       OR receipt.bridge_release_revision<>p_bridge_release_revision
       OR receipt.predecessor_image_ref<>p_predecessor_image_ref
       OR receipt.deployment_namespace<>p_deployment_namespace
       OR receipt.deployment_name<>p_deployment_name
       OR receipt.deployment_uid<>p_deployment_uid
       OR receipt.deployment_generation<>p_deployment_generation
       OR receipt.runtime_pod_set_digest<>p_runtime_pod_set_digest
       OR receipt.kubernetes_audit_id<>p_kubernetes_audit_id
       OR receipt.missing_unfinished_upload_sessions<>0
       OR receipt.predecessor_drained_at<>p_kubernetes_observed_at THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='schema bridge receipt already differs';
    END IF;
    RETURN receipt;
END
$function$;

CREATE FUNCTION fs2_scientific_claim_artifact_removals_v2(
    p_limit integer,
    p_operation_id uuid DEFAULT NULL,
    p_tenant_id text DEFAULT NULL
)
RETURNS TABLE(
    upload_id uuid,
    operation_id uuid,
    attempt_id uuid,
    tenant_id text,
    storage_key text,
    provider_upload_id text,
    upload_session_generation integer,
    latest_upload_capability_expires_at timestamptz,
    removal_generation integer,
    removal_claimed_at timestamptz,
    eligible_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    tenant_candidate record;
    candidate record;
    bridge_receipt record;
    rollout record;
    global_cleanup_not_before timestamptz := '-infinity'::timestamptz;
    claimed_at timestamptz := clock_timestamp();
    claimed integer := 0;
    progress boolean;
BEGIN
    IF p_limit NOT BETWEEN 1 AND 500
       OR (p_operation_id IS NULL)<>(p_tenant_id IS NULL) THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='artifact removal claim scope is invalid';
    END IF;
    SELECT * INTO rollout FROM public.fs2_schema_rollout_state WHERE singleton;
    IF NOT FOUND OR rollout.phase<>'contracted' THEN
        RETURN;
    END IF;
    IF rollout.predecessor_image_ref IS NOT NULL THEN
        SELECT * INTO bridge_receipt
        FROM public.fs2_schema_bridge_ready_receipts receipt
        WHERE receipt.migration_version='0031_scientific_quota_fencing.sql'
          AND receipt.bridge_image_ref=rollout.bridge_image_ref
          AND receipt.predecessor_image_ref=rollout.predecessor_image_ref
        ORDER BY receipt.cleanup_not_before DESC,receipt.bridge_release_revision DESC
        LIMIT 1;
        IF NOT FOUND OR bridge_receipt.cleanup_not_before>claimed_at THEN
            RETURN;
        END IF;
        global_cleanup_not_before := bridge_receipt.cleanup_not_before;
    END IF;
    WHILE claimed<p_limit LOOP
      progress := false;
      FOR tenant_candidate IN
        SELECT janitor_cursor.tenant_id
        FROM public.fs2_scientific_artifact_janitor_tenant_cursors janitor_cursor
        WHERE (p_tenant_id IS NULL OR janitor_cursor.tenant_id=p_tenant_id)
          AND EXISTS (
              SELECT 1
              FROM public.fs2_scientific_artifact_quota_reservations reservation
              WHERE reservation.tenant_id=janitor_cursor.tenant_id
                AND (p_operation_id IS NULL OR reservation.operation_id=p_operation_id)
                AND (
                    (
                        reservation.state='active'
                        AND NOT EXISTS (
                            SELECT 1
                            FROM public.fs2_scientific_artifact_finalization_leases lease
                            WHERE lease.upload_id=reservation.upload_id AND lease.state='active'
                        )
                        AND GREATEST(
                            reservation.expires_at,
                            reservation.latest_upload_capability_expires_at
                        )+make_interval(secs=>reservation.upload_completion_grace_seconds)<=claimed_at
                    ) OR (
                        reservation.state='removing'
                        AND reservation.removal_retry_at<=claimed_at
                        AND (
                            NOT EXISTS (
                                SELECT 1
                                FROM public.fs2_scientific_artifact_deletion_evidence_v2 deletion
                                WHERE deletion.upload_id=reservation.upload_id
                                  AND deletion.removal_generation=reservation.removal_attempts
                            ) OR EXISTS (
                                SELECT 1
                                FROM public.fs2_scientific_artifact_verification_failures_v2 failure
                                WHERE failure.upload_id=reservation.upload_id
                                  AND failure.removal_generation=reservation.removal_attempts
                                  AND failure.verification_generation=reservation.verification_attempts
                            )
                        )
                    )
                )
          )
        ORDER BY janitor_cursor.last_removal_claimed_at NULLS FIRST,janitor_cursor.tenant_id
        FOR UPDATE OF janitor_cursor SKIP LOCKED
        LIMIT p_limit
    LOOP
        SELECT reservation.*,upload.storage_key,session.provider_upload_id,
               COALESCE(session.session_generation,0) AS upload_session_generation,
               GREATEST(
                   global_cleanup_not_before,
                   GREATEST(
                       reservation.expires_at,
                       reservation.latest_upload_capability_expires_at
                   )+make_interval(secs=>reservation.upload_completion_grace_seconds)
               ) AS write_fenced_at
        INTO candidate
        FROM public.fs2_scientific_artifact_quota_reservations reservation
        JOIN public.fs2_scientific_uploads upload ON upload.id=reservation.upload_id
        LEFT JOIN public.fs2_scientific_artifact_upload_sessions session
          ON session.upload_id=reservation.upload_id
        WHERE reservation.tenant_id=tenant_candidate.tenant_id
          AND (p_operation_id IS NULL OR reservation.operation_id=p_operation_id)
          AND (
              (
                  reservation.state='active'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.fs2_scientific_artifact_finalization_leases lease
                      WHERE lease.upload_id=reservation.upload_id AND lease.state='active'
                  )
                  AND GREATEST(
                      reservation.expires_at,
                      reservation.latest_upload_capability_expires_at
                  )+make_interval(secs=>reservation.upload_completion_grace_seconds)<=claimed_at
              ) OR (
                  reservation.state='removing'
                  AND reservation.removal_retry_at<=claimed_at
                  AND (
                      NOT EXISTS (
                          SELECT 1
                          FROM public.fs2_scientific_artifact_deletion_evidence_v2 deletion
                          WHERE deletion.upload_id=reservation.upload_id
                            AND deletion.removal_generation=reservation.removal_attempts
                      ) OR EXISTS (
                          SELECT 1
                          FROM public.fs2_scientific_artifact_verification_failures_v2 failure
                          WHERE failure.upload_id=reservation.upload_id
                            AND failure.removal_generation=reservation.removal_attempts
                            AND failure.verification_generation=reservation.verification_attempts
                      )
                  )
              )
          )
        ORDER BY write_fenced_at,reservation.upload_id
        FOR UPDATE OF reservation SKIP LOCKED
        LIMIT 1;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;
        UPDATE public.fs2_scientific_artifact_quota_reservations reservation
        SET state='removing',removal_attempts=reservation.removal_attempts+1,
            removal_claimed_at=claimed_at,
            removal_retry_at=claimed_at+make_interval(
                secs=>LEAST(3600.0,30.0*power(2.0,LEAST(reservation.removal_attempts,6)))
            ),
            verification_retry_at=COALESCE(reservation.verification_retry_at,claimed_at)
        WHERE reservation.upload_id=candidate.upload_id
          AND reservation.state=candidate.state
          AND reservation.removal_attempts=candidate.removal_attempts;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;
        UPDATE public.fs2_scientific_artifact_janitor_tenant_cursors
        SET last_removal_claimed_at=claimed_at
        WHERE fs2_scientific_artifact_janitor_tenant_cursors.tenant_id=tenant_candidate.tenant_id;
        INSERT INTO public.fs2_scientific_artifact_quota_events(
            upload_id,operation_id,attempt_id,tenant_id,event_type,reserved_bytes,
            reserved_objects,expires_at,occurred_at
        ) VALUES(
            candidate.upload_id,candidate.operation_id,candidate.attempt_id,
            candidate.tenant_id,'removal_claimed',candidate.reserved_bytes,
            candidate.reserved_objects,candidate.expires_at,claimed_at
        );
        upload_id := candidate.upload_id;
        operation_id := candidate.operation_id;
        attempt_id := candidate.attempt_id;
        tenant_id := candidate.tenant_id;
        storage_key := candidate.storage_key;
        provider_upload_id := candidate.provider_upload_id;
        upload_session_generation := candidate.upload_session_generation;
        latest_upload_capability_expires_at := candidate.latest_upload_capability_expires_at;
        removal_generation := candidate.removal_attempts+1;
        removal_claimed_at := claimed_at;
        eligible_at := candidate.write_fenced_at;
        claimed := claimed+1;
        progress := true;
        RETURN NEXT;
        EXIT WHEN claimed>=p_limit;
      END LOOP;
      EXIT WHEN NOT progress;
    END LOOP;
END
$function$;

CREATE FUNCTION fs2_scientific_record_artifact_removal_completion_v2(
    p_upload_id uuid,
    p_operation_id uuid,
    p_attempt_id uuid,
    p_tenant_id text,
    p_storage_key text,
    p_removal_generation integer,
    p_removal_claimed_at timestamptz,
    p_evidence_kind text,
    p_provider_request_id text,
    p_removed_version_count bigint,
    p_aborted_upload_count bigint,
    p_multipart_list_request_id text,
    p_multipart_session_set_digest text,
    p_observed_at timestamptz
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    reservation record;
    stored record;
BEGIN
    IF p_evidence_kind NOT IN ('absence_confirmed','all_versions_removed')
       OR length(p_provider_request_id) NOT BETWEEN 1 AND 512
       OR length(p_multipart_list_request_id) NOT BETWEEN 1 AND 512
       OR p_multipart_session_set_digest<>'sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945'
       OR p_aborted_upload_count<0
       OR p_removal_generation NOT BETWEEN 1 AND 1000000
       OR p_observed_at< p_removal_claimed_at
       OR p_observed_at>clock_timestamp()+interval '5 minutes'
       OR NOT (
           (p_evidence_kind='absence_confirmed' AND p_removed_version_count=0)
           OR (p_evidence_kind='all_versions_removed' AND p_removed_version_count>=1)
       ) THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='artifact deletion result is invalid';
    END IF;
    SELECT quota.*,upload.storage_key INTO reservation
    FROM public.fs2_scientific_artifact_quota_reservations quota
    JOIN public.fs2_scientific_uploads upload ON upload.id=quota.upload_id
    WHERE quota.upload_id=p_upload_id AND quota.operation_id=p_operation_id
      AND quota.attempt_id=p_attempt_id AND quota.tenant_id=p_tenant_id
    FOR UPDATE OF quota;
    IF NOT FOUND OR reservation.state<>'removing'
       OR reservation.storage_key<>p_storage_key
       OR reservation.removal_attempts<>p_removal_generation
       OR reservation.removal_claimed_at<>p_removal_claimed_at THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact deletion result is stale';
    END IF;
    UPDATE public.fs2_scientific_artifact_upload_sessions
    SET state='aborted',terminal_at=p_observed_at,
        terminal_provider_request_id=p_multipart_list_request_id
    WHERE upload_id=p_upload_id AND state='active';
    INSERT INTO public.fs2_scientific_artifact_deletion_evidence_v2(
        upload_id,removal_generation,operation_id,attempt_id,tenant_id,storage_key,
        evidence_kind,provider_request_id,removed_version_count,aborted_upload_count,
        multipart_list_request_id,multipart_session_set_digest,removal_claimed_at,observed_at
    ) VALUES(
        p_upload_id,p_removal_generation,p_operation_id,p_attempt_id,p_tenant_id,p_storage_key,
        p_evidence_kind,p_provider_request_id,p_removed_version_count,p_aborted_upload_count,
        p_multipart_list_request_id,p_multipart_session_set_digest,p_removal_claimed_at,p_observed_at
    ) ON CONFLICT (upload_id,removal_generation) DO NOTHING;
    SELECT * INTO stored
    FROM public.fs2_scientific_artifact_deletion_evidence_v2
    WHERE upload_id=p_upload_id AND removal_generation=p_removal_generation;
    IF stored.operation_id<>p_operation_id OR stored.attempt_id<>p_attempt_id
       OR stored.tenant_id<>p_tenant_id OR stored.storage_key<>p_storage_key
       OR stored.evidence_kind<>p_evidence_kind
       OR stored.provider_request_id<>p_provider_request_id
       OR stored.removed_version_count<>p_removed_version_count
       OR stored.aborted_upload_count<>p_aborted_upload_count
       OR stored.multipart_list_request_id<>p_multipart_list_request_id
       OR stored.multipart_session_set_digest<>p_multipart_session_set_digest
       OR stored.removal_claimed_at<>p_removal_claimed_at
       OR stored.observed_at<>p_observed_at THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact deletion result already differs';
    END IF;
END
$function$;

CREATE FUNCTION fs2_scientific_claim_artifact_verifications_v2(p_limit integer)
RETURNS TABLE(
    upload_id uuid,
    operation_id uuid,
    attempt_id uuid,
    tenant_id text,
    storage_key text,
    provider_upload_id text,
    upload_session_generation integer,
    latest_upload_capability_expires_at timestamptz,
    removal_generation integer,
    verification_generation integer,
    removal_claimed_at timestamptz,
    verification_claimed_at timestamptz,
    eligible_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    tenant_candidate record;
    candidate record;
    claimed_at timestamptz := clock_timestamp();
    claimed integer := 0;
    progress boolean;
BEGIN
    IF p_limit NOT BETWEEN 1 AND 500 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='artifact verification claim limit is invalid';
    END IF;
    WHILE claimed<p_limit LOOP
      progress := false;
      FOR tenant_candidate IN
        SELECT janitor_cursor.tenant_id
        FROM public.fs2_scientific_artifact_janitor_tenant_cursors janitor_cursor
        WHERE EXISTS (
            SELECT 1
            FROM public.fs2_scientific_artifact_quota_reservations reservation
            JOIN public.fs2_scientific_artifact_deletion_evidence_v2 deletion
              ON deletion.upload_id=reservation.upload_id
             AND deletion.removal_generation=reservation.removal_attempts
            WHERE reservation.tenant_id=janitor_cursor.tenant_id
              AND reservation.state='removing'
              AND reservation.verification_retry_at<=claimed_at
              AND deletion.observed_at+make_interval(
                  secs=>reservation.provider_stability_grace_seconds
              )<=claimed_at
              AND NOT EXISTS (
                  SELECT 1 FROM public.fs2_scientific_artifact_removal_evidence_v2 evidence
                  WHERE evidence.upload_id=reservation.upload_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM public.fs2_scientific_artifact_verification_failures_v2 failure
                  WHERE failure.upload_id=reservation.upload_id
                    AND failure.removal_generation=reservation.removal_attempts
              )
        )
        ORDER BY janitor_cursor.last_verification_claimed_at NULLS FIRST,janitor_cursor.tenant_id
        FOR UPDATE OF janitor_cursor SKIP LOCKED
        LIMIT p_limit
    LOOP
        SELECT reservation.*,upload.storage_key,session.provider_upload_id,
               COALESCE(session.session_generation,0) AS upload_session_generation,
               deletion.observed_at AS deletion_observed_at,
               GREATEST(
                   reservation.expires_at,
                   reservation.latest_upload_capability_expires_at
               )+make_interval(secs=>reservation.upload_completion_grace_seconds) AS write_fenced_at
        INTO candidate
        FROM public.fs2_scientific_artifact_quota_reservations reservation
        JOIN public.fs2_scientific_uploads upload ON upload.id=reservation.upload_id
        LEFT JOIN public.fs2_scientific_artifact_upload_sessions session
          ON session.upload_id=reservation.upload_id
        JOIN public.fs2_scientific_artifact_deletion_evidence_v2 deletion
          ON deletion.upload_id=reservation.upload_id
         AND deletion.removal_generation=reservation.removal_attempts
        WHERE reservation.tenant_id=tenant_candidate.tenant_id
          AND reservation.state='removing'
          AND reservation.verification_retry_at<=claimed_at
          AND deletion.observed_at+make_interval(
              secs=>reservation.provider_stability_grace_seconds
          )<=claimed_at
          AND NOT EXISTS (
              SELECT 1 FROM public.fs2_scientific_artifact_removal_evidence_v2 evidence
              WHERE evidence.upload_id=reservation.upload_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM public.fs2_scientific_artifact_verification_failures_v2 failure
              WHERE failure.upload_id=reservation.upload_id
                AND failure.removal_generation=reservation.removal_attempts
          )
        ORDER BY deletion.observed_at,reservation.upload_id
        FOR UPDATE OF reservation SKIP LOCKED
        LIMIT 1;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;
        UPDATE public.fs2_scientific_artifact_quota_reservations reservation
        SET verification_attempts=reservation.verification_attempts+1,
            verification_claimed_at=claimed_at,
            verification_retry_at=claimed_at+make_interval(
                secs=>LEAST(3600.0,30.0*power(2.0,LEAST(reservation.verification_attempts,6)))
            )
        WHERE reservation.upload_id=candidate.upload_id
          AND reservation.state='removing'
          AND reservation.removal_attempts=candidate.removal_attempts
          AND reservation.verification_attempts=candidate.verification_attempts;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;
        UPDATE public.fs2_scientific_artifact_janitor_tenant_cursors
        SET last_verification_claimed_at=claimed_at
        WHERE fs2_scientific_artifact_janitor_tenant_cursors.tenant_id=tenant_candidate.tenant_id;
        upload_id := candidate.upload_id;
        operation_id := candidate.operation_id;
        attempt_id := candidate.attempt_id;
        tenant_id := candidate.tenant_id;
        storage_key := candidate.storage_key;
        provider_upload_id := candidate.provider_upload_id;
        upload_session_generation := candidate.upload_session_generation;
        latest_upload_capability_expires_at := candidate.latest_upload_capability_expires_at;
        removal_generation := candidate.removal_attempts;
        verification_generation := candidate.verification_attempts+1;
        removal_claimed_at := candidate.removal_claimed_at;
        verification_claimed_at := claimed_at;
        eligible_at := candidate.write_fenced_at;
        claimed := claimed+1;
        progress := true;
        RETURN NEXT;
        EXIT WHEN claimed>=p_limit;
      END LOOP;
      EXIT WHEN NOT progress;
    END LOOP;
END
$function$;

CREATE FUNCTION fs2_scientific_record_artifact_verification_failure_v2(
    p_upload_id uuid,
    p_tenant_id text,
    p_removal_generation integer,
    p_verification_generation integer,
    p_verification_claimed_at timestamptz
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    reservation record;
    stored record;
BEGIN
    SELECT * INTO reservation
    FROM public.fs2_scientific_artifact_quota_reservations
    WHERE upload_id=p_upload_id AND tenant_id=p_tenant_id
    FOR UPDATE;
    IF NOT FOUND OR reservation.state<>'removing'
       OR reservation.removal_attempts<>p_removal_generation
       OR reservation.verification_attempts<>p_verification_generation
       OR reservation.verification_claimed_at<>p_verification_claimed_at THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact verification failure is stale';
    END IF;
    INSERT INTO public.fs2_scientific_artifact_verification_failures_v2(
        upload_id,removal_generation,verification_generation,tenant_id,verification_claimed_at
    ) VALUES(
        p_upload_id,p_removal_generation,p_verification_generation,p_tenant_id,p_verification_claimed_at
    ) ON CONFLICT (upload_id,removal_generation,verification_generation) DO NOTHING;
    SELECT * INTO stored
    FROM public.fs2_scientific_artifact_verification_failures_v2
    WHERE upload_id=p_upload_id AND removal_generation=p_removal_generation
      AND verification_generation=p_verification_generation;
    IF stored.tenant_id<>p_tenant_id
       OR stored.verification_claimed_at<>p_verification_claimed_at THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact verification failure already differs';
    END IF;
END
$function$;

CREATE FUNCTION fs2_scientific_record_artifact_removal_v2(
    p_upload_id uuid,
    p_operation_id uuid,
    p_attempt_id uuid,
    p_tenant_id text,
    p_storage_key text,
    p_evidence_kind text,
    p_provider_request_id text,
    p_removed_version_count bigint,
    p_observed_at timestamptz,
    p_latest_upload_capability_expires_at timestamptz,
    p_removal_generation integer,
    p_verification_generation integer,
    p_first_list_request_id text,
    p_head_request_id text,
    p_second_list_request_id text,
    p_first_version_set_digest text,
    p_second_version_set_digest text,
    p_first_multipart_list_request_id text,
    p_second_multipart_list_request_id text,
    p_first_multipart_session_set_digest text,
    p_second_multipart_session_set_digest text,
    p_claim_digest text
) RETURNS fs2_scientific_artifact_quota_reservations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    reservation record;
    deletion record;
    stored record;
    legacy record;
BEGIN
    IF p_evidence_kind<>'absence_confirmed' OR p_removed_version_count<>0
       OR length(p_provider_request_id) NOT BETWEEN 1 AND 512
       OR length(p_first_list_request_id) NOT BETWEEN 1 AND 512
       OR length(p_head_request_id) NOT BETWEEN 1 AND 512
       OR length(p_second_list_request_id) NOT BETWEEN 1 AND 512
       OR length(p_first_multipart_list_request_id) NOT BETWEEN 1 AND 512
       OR length(p_second_multipart_list_request_id) NOT BETWEEN 1 AND 512
       OR p_provider_request_id<>p_second_list_request_id
       OR p_first_version_set_digest !~ '^sha256:[a-f0-9]{64}$'
       OR p_second_version_set_digest<>p_first_version_set_digest
       OR p_first_version_set_digest<>'sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945'
       OR p_second_multipart_session_set_digest<>p_first_multipart_session_set_digest
       OR p_first_multipart_session_set_digest<>'sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945'
       OR p_claim_digest !~ '^sha256:[a-f0-9]{64}$'
       OR p_observed_at>clock_timestamp()+interval '5 minutes'
       OR p_observed_at<p_latest_upload_capability_expires_at THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='artifact stable-absence evidence is invalid';
    END IF;
    SELECT quota.*,upload.storage_key INTO reservation
    FROM public.fs2_scientific_artifact_quota_reservations quota
    JOIN public.fs2_scientific_uploads upload ON upload.id=quota.upload_id
    WHERE quota.upload_id=p_upload_id AND quota.operation_id=p_operation_id
      AND quota.attempt_id=p_attempt_id AND quota.tenant_id=p_tenant_id
    FOR UPDATE OF quota;
    IF NOT FOUND OR reservation.storage_key<>p_storage_key THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact removal target identity differs';
    END IF;
    SELECT * INTO stored FROM public.fs2_scientific_artifact_removal_evidence_v2
    WHERE upload_id=p_upload_id;
    IF FOUND THEN
        IF reservation.state<>'released'
           OR stored.operation_id<>p_operation_id OR stored.attempt_id<>p_attempt_id
           OR stored.tenant_id<>p_tenant_id OR stored.storage_key<>p_storage_key
           OR stored.provider_request_id<>p_provider_request_id
           OR stored.observed_at<>p_observed_at
           OR stored.latest_upload_capability_expires_at<>p_latest_upload_capability_expires_at
           OR stored.removal_generation<>p_removal_generation
           OR stored.verification_generation<>p_verification_generation
           OR stored.first_list_request_id<>p_first_list_request_id
           OR stored.head_request_id<>p_head_request_id
           OR stored.second_list_request_id<>p_second_list_request_id
           OR stored.first_version_set_digest<>p_first_version_set_digest
           OR stored.second_version_set_digest<>p_second_version_set_digest
           OR stored.first_multipart_list_request_id<>p_first_multipart_list_request_id
           OR stored.second_multipart_list_request_id<>p_second_multipart_list_request_id
           OR stored.first_multipart_session_set_digest<>p_first_multipart_session_set_digest
           OR stored.second_multipart_session_set_digest<>p_second_multipart_session_set_digest
           OR stored.claim_digest<>p_claim_digest THEN
            RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact stable-absence evidence already differs';
        END IF;
        SELECT * INTO reservation
        FROM public.fs2_scientific_artifact_quota_reservations
        WHERE upload_id=p_upload_id;
        RETURN reservation;
    END IF;
    IF reservation.state<>'removing'
       OR reservation.latest_upload_capability_expires_at<>p_latest_upload_capability_expires_at
       OR reservation.removal_attempts<>p_removal_generation
       OR reservation.verification_attempts<>p_verification_generation
       OR reservation.verification_claimed_at IS NULL
       OR p_observed_at<reservation.verification_claimed_at
       OR EXISTS (
           SELECT 1 FROM public.fs2_scientific_artifact_verification_failures_v2 failure
           WHERE failure.upload_id=p_upload_id
             AND failure.removal_generation=p_removal_generation
             AND failure.verification_generation=p_verification_generation
       ) THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact stable-absence claim is stale';
    END IF;
    SELECT * INTO deletion
    FROM public.fs2_scientific_artifact_deletion_evidence_v2
    WHERE upload_id=p_upload_id AND removal_generation=p_removal_generation;
    IF NOT FOUND
       OR reservation.verification_claimed_at<deletion.observed_at
          +make_interval(secs=>reservation.provider_stability_grace_seconds) THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact provider quiet interval is incomplete';
    END IF;
    INSERT INTO public.fs2_scientific_artifact_removal_evidence_v2(
        upload_id,operation_id,attempt_id,tenant_id,storage_key,evidence_kind,
        provider_request_id,removed_version_count,observed_at,
        latest_upload_capability_expires_at,removal_generation,verification_generation,
        first_list_request_id,head_request_id,second_list_request_id,
        first_version_set_digest,second_version_set_digest,
        first_multipart_list_request_id,second_multipart_list_request_id,
        first_multipart_session_set_digest,second_multipart_session_set_digest,claim_digest
    ) VALUES(
        p_upload_id,p_operation_id,p_attempt_id,p_tenant_id,p_storage_key,p_evidence_kind,
        p_provider_request_id,p_removed_version_count,p_observed_at,
        p_latest_upload_capability_expires_at,p_removal_generation,p_verification_generation,
        p_first_list_request_id,p_head_request_id,p_second_list_request_id,
        p_first_version_set_digest,p_second_version_set_digest,
        p_first_multipart_list_request_id,p_second_multipart_list_request_id,
        p_first_multipart_session_set_digest,p_second_multipart_session_set_digest,p_claim_digest
    );
    INSERT INTO public.fs2_scientific_artifact_removal_evidence(
        upload_id,operation_id,attempt_id,tenant_id,storage_key,evidence_kind,
        provider_request_id,removed_version_count,observed_at
    ) VALUES(
        p_upload_id,p_operation_id,p_attempt_id,p_tenant_id,p_storage_key,p_evidence_kind,
        p_provider_request_id,p_removed_version_count,p_observed_at
    ) ON CONFLICT (upload_id) DO NOTHING;
    SELECT * INTO legacy FROM public.fs2_scientific_artifact_removal_evidence
    WHERE upload_id=p_upload_id;
    IF legacy.operation_id<>p_operation_id OR legacy.attempt_id<>p_attempt_id
       OR legacy.tenant_id<>p_tenant_id OR legacy.storage_key<>p_storage_key
       OR legacy.evidence_kind<>p_evidence_kind
       OR legacy.provider_request_id<>p_provider_request_id
       OR legacy.removed_version_count<>p_removed_version_count
       OR legacy.observed_at<>p_observed_at THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='legacy artifact evidence conflicts with v2';
    END IF;
    UPDATE public.fs2_scientific_artifact_quota_reservations
    SET state='released',released_at=p_observed_at,release_reason='provider_removed'
    WHERE upload_id=p_upload_id AND state='removing'
      AND removal_attempts=p_removal_generation
      AND verification_attempts=p_verification_generation;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact release compare-and-set failed';
    END IF;
    INSERT INTO public.fs2_scientific_artifact_quota_events(
        upload_id,operation_id,attempt_id,tenant_id,event_type,reserved_bytes,
        reserved_objects,expires_at,release_reason,occurred_at
    ) VALUES(
        reservation.upload_id,reservation.operation_id,reservation.attempt_id,
        reservation.tenant_id,'released',reservation.reserved_bytes,
        reservation.reserved_objects,reservation.expires_at,'provider_removed',p_observed_at
    );
    SELECT * INTO reservation
    FROM public.fs2_scientific_artifact_quota_reservations
    WHERE upload_id=p_upload_id;
    RETURN reservation;
END
$function$;

-- fs2-migration-transaction-boundary

-- Validation uses PostgreSQL's lower-impact constraint-validation lock after
-- the short ACCESS EXCLUSIVE expansion transactions have committed. New rows
-- were already checked by the NOT VALID constraints from their creation.
ALTER TABLE fs2_scientific_artifact_quota_reservations
    VALIDATE CONSTRAINT fs2_scientific_artifact_quota_upload_grace_bound,
    VALIDATE CONSTRAINT fs2_scientific_artifact_quota_stability_grace_bound,
    VALIDATE CONSTRAINT fs2_scientific_artifact_quota_capability_time_order;
ALTER TABLE fs2_scientific_uploads
    VALIDATE CONSTRAINT fs2_scientific_uploads_provider_version_id_bound;
ALTER TABLE fs2_scientific_artifacts
    VALIDATE CONSTRAINT fs2_scientific_artifacts_provider_version_id_bound;

-- fs2-migration-transaction-boundary

-- The pre-0031 janitor entry points cannot express the capability, completion,
-- stability, generation, or double-snapshot evidence required by this
-- migration.  Replace them in place with fail-closed bodies before any old
-- CronJob can run against the expanded schema.  Expansion preserves only the
-- predecessor's publication transition; cleanup authority never crosses the
-- migration boundary.
CREATE OR REPLACE FUNCTION fs2_scientific_claim_artifact_removals(
    p_limit integer,p_operation_id uuid DEFAULT NULL,p_tenant_id text DEFAULT NULL
) RETURNS TABLE(
    upload_id uuid,operation_id uuid,attempt_id uuid,tenant_id text,
    storage_key text,eligible_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='FS202',
        MESSAGE='legacy artifact cleanup is disabled after quota fencing expansion';
END
$function$;

CREATE OR REPLACE FUNCTION fs2_scientific_claim_artifact_verifications(p_limit integer)
RETURNS TABLE(
    upload_id uuid,operation_id uuid,attempt_id uuid,tenant_id text,
    storage_key text,eligible_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='FS202',
        MESSAGE='legacy artifact cleanup is disabled after quota fencing expansion';
END
$function$;

CREATE OR REPLACE FUNCTION fs2_scientific_record_artifact_removal(
    p_upload_id uuid,p_operation_id uuid,p_attempt_id uuid,p_tenant_id text,
    p_storage_key text,p_evidence_kind text,p_provider_request_id text,
    p_removed_version_count integer,p_observed_at timestamptz
) RETURNS fs2_scientific_artifact_quota_reservations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='FS202',
        MESSAGE='legacy artifact cleanup evidence is disabled after quota fencing expansion';
END
$function$;

REVOKE ALL ON fs2_scientific_artifact_upload_sessions,
    fs2_schema_rollout_state,
    fs2_schema_bridge_ready_receipts,
    fs2_schema_bridge_rollout_attempts,
    fs2_scientific_artifact_upload_session_creation_claims,
    fs2_scientific_artifact_upload_session_reconciliation_events,
    fs2_scientific_artifact_finalization_leases,
    fs2_scientific_artifact_finalization_failures,
    fs2_scientific_artifact_legacy_version_claims,
    fs2_scientific_artifact_legacy_version_scan_events,
    fs2_scientific_artifact_legacy_version_bindings,
    fs2_scientific_artifacts_versioned,
    fs2_scientific_artifact_upload_capabilities,
    fs2_scientific_artifact_deletion_evidence_v2,
    fs2_scientific_artifact_verification_failures_v2,
    fs2_scientific_artifact_removal_evidence_v2,
    fs2_scientific_artifact_janitor_tenant_cursors FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_claim_artifact_removals(integer,uuid,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_claim_artifact_verifications(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_record_artifact_removal(
    uuid,uuid,uuid,text,text,text,text,integer,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_claim_upload_session_creation_v2(
    uuid,text,text,uuid,bigint,integer
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_bind_upload_session_v2(
    uuid,text,text,uuid,text,bigint,integer,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_claim_stale_upload_session_creations_v2(
    integer
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_record_upload_session_creation_reconciled_v2(
    uuid,text,text,uuid,integer,timestamptz,text,integer,text,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_record_upload_session_aborted_v2(
    uuid,text,integer,text,text,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_mark_upload_session_completed_v2(
    uuid,text,integer,text,text,text,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_acquire_artifact_finalization_lease_v2(
    uuid,uuid,text,integer,uuid
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_claim_expired_finalization_leases_v2(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_record_finalization_failure_v2(
    uuid,uuid,text,uuid,integer,integer,text,text,text,text,text,bigint,text,text,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_record_upload_capability_v2(
    uuid,text,uuid,integer,integer,bigint,text,text,text,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_publish_artifact_v2(
    uuid,uuid,text,uuid,uuid,integer,integer,text,text,text,text,bigint,text,text,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_claim_legacy_artifact_versions_v2(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_record_legacy_artifact_version_scan_v2(
    uuid,integer,timestamptz,text,text,text,text,text,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_record_legacy_artifact_version_v2(
    uuid,uuid,text,text,integer,timestamptz,text,text,text,bigint,text,text,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_legacy_version_rollout_status_v2() FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_mark_schema_bridge_ready_v2(
    text,bigint,text,text,text,text,bigint,bigint,integer,integer,integer,integer,
    integer,text,text,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_ensure_artifact_janitor_tenant_cursor() FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_validate_artifact_provider_version_v2() FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_queue_legacy_artifact_version_v2() FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_queue_legacy_upload_session_v2() FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_claim_artifact_removals_v2(integer,uuid,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_record_artifact_removal_completion_v2(
    uuid,uuid,uuid,text,text,integer,timestamptz,text,text,bigint,bigint,text,text,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_claim_artifact_verifications_v2(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_record_artifact_verification_failure_v2(
    uuid,text,integer,integer,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_record_artifact_removal_v2(
    uuid,uuid,uuid,text,text,text,text,bigint,timestamptz,timestamptz,integer,integer,
    text,text,text,text,text,text,text,text,text,text
) FROM PUBLIC;

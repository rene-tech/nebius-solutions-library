-- SAI-21: retained artifact quota reservations and scientific GPU settlement.
--
-- Upload intents remain immutable provenance. Quota state lives beside them so
-- an abandoned intent can release capacity without deleting or rewriting its
-- record. The append-only event ledger survives artifact retention purges.

CREATE TABLE fs2_scientific_artifact_quota_reservations (
    upload_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    reserved_bytes bigint NOT NULL CHECK (reserved_bytes BETWEEN 0 AND 1099511627776),
    reserved_objects integer NOT NULL DEFAULT 1 CHECK (reserved_objects = 1),
    state text NOT NULL CHECK (state IN ('active','released')),
    reserved_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    released_at timestamptz,
    release_reason text CHECK (release_reason IN ('expired','attempt_closed','retention_purged')),
    CHECK (expires_at > reserved_at),
    CHECK (
        (state='active' AND released_at IS NULL AND release_reason IS NULL)
        OR (state='released' AND released_at IS NOT NULL AND released_at>=reserved_at
            AND release_reason IS NOT NULL)
    )
);

CREATE INDEX fs2_scientific_artifact_quota_reservations_active_idx
    ON fs2_scientific_artifact_quota_reservations (tenant_id,expires_at,upload_id)
    WHERE state='active';

CREATE TABLE fs2_scientific_artifact_quota_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    upload_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    event_type text NOT NULL CHECK (event_type IN ('reserved','retention_extended','released')),
    reserved_bytes bigint NOT NULL CHECK (reserved_bytes BETWEEN 0 AND 1099511627776),
    reserved_objects integer NOT NULL CHECK (reserved_objects = 1),
    expires_at timestamptz NOT NULL,
    release_reason text CHECK (release_reason IN ('expired','attempt_closed','retention_purged')),
    occurred_at timestamptz NOT NULL,
    CHECK (
        (event_type='released' AND release_reason IS NOT NULL)
        OR (event_type<>'released' AND release_reason IS NULL)
    )
);

CREATE INDEX fs2_scientific_artifact_quota_events_tenant_idx
    ON fs2_scientific_artifact_quota_events (tenant_id,id);

COMMENT ON TABLE fs2_scientific_artifact_quota_reservations IS
    'Mutable active/released quota state; upload provenance remains immutable and retained';
COMMENT ON TABLE fs2_scientific_artifact_quota_events IS
    'Append-only byte and object reservation ledger retained across artifact purges';

CREATE FUNCTION fs2_scientific_validate_artifact_quota_transition() RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF OLD.upload_id IS DISTINCT FROM NEW.upload_id
       OR OLD.operation_id IS DISTINCT FROM NEW.operation_id
       OR OLD.attempt_id IS DISTINCT FROM NEW.attempt_id
       OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.reserved_bytes IS DISTINCT FROM NEW.reserved_bytes
       OR OLD.reserved_objects IS DISTINCT FROM NEW.reserved_objects THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact quota reservation identity is immutable';
    END IF;
    IF OLD.state='active' AND NEW.state='active' THEN
        IF NEW.reserved_at IS DISTINCT FROM OLD.reserved_at OR NEW.expires_at < OLD.expires_at
           OR NEW.released_at IS NOT NULL OR NEW.release_reason IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='invalid artifact quota retention extension';
        END IF;
    ELSIF OLD.state='active' AND NEW.state='released' THEN
        IF NEW.reserved_at IS DISTINCT FROM OLD.reserved_at
           OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
           OR NEW.released_at IS NULL OR NEW.released_at<NEW.reserved_at
           OR NEW.release_reason IS NULL THEN
            RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='invalid artifact quota release';
        END IF;
    ELSIF OLD.state='released' AND NEW.state='active' THEN
        IF NEW.reserved_at < OLD.reserved_at OR NEW.expires_at <= NEW.reserved_at
           OR NEW.released_at IS NOT NULL OR NEW.release_reason IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='invalid artifact quota reactivation';
        END IF;
    ELSE
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='invalid artifact quota reservation transition';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_scientific_artifact_quota_reservations_transition
BEFORE UPDATE ON fs2_scientific_artifact_quota_reservations
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_validate_artifact_quota_transition();

CREATE TRIGGER fs2_scientific_artifact_quota_events_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_artifact_quota_events
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

-- Existing finalized objects retain quota until their recorded artifact
-- retention deadline. Existing unfinished intents receive the same bounded
-- 24-hour reservation window as new admissions. Any already-expired record is
-- preserved as released evidence rather than deleted; this also releases a
-- standalone finalized input even when no run-result purge was ever created.
WITH migration_clock AS (
    SELECT clock_timestamp() AS migrated_at
), source AS (
    SELECT upload.id AS upload_id,upload.operation_id,upload.attempt_id,upload.tenant_id,
           upload.expected_size_bytes AS reserved_bytes,upload.begun_at AS reserved_at,
           upload.artifact_id,
           COALESCE(artifact.retention_expires_at,upload.begun_at+interval '24 hours') AS expires_at,
           migration_clock.migrated_at
    FROM fs2_scientific_uploads upload
    LEFT JOIN fs2_scientific_artifacts artifact ON artifact.id=upload.artifact_id
    CROSS JOIN migration_clock
)
INSERT INTO fs2_scientific_artifact_quota_reservations(
    upload_id,operation_id,attempt_id,tenant_id,reserved_bytes,reserved_objects,
    state,reserved_at,expires_at,released_at,release_reason
)
SELECT upload_id,operation_id,attempt_id,tenant_id,reserved_bytes,1,
       CASE WHEN expires_at>migrated_at THEN 'active' ELSE 'released' END,
       reserved_at,expires_at,
       CASE WHEN expires_at<=migrated_at THEN migrated_at END,
       CASE WHEN expires_at<=migrated_at THEN 'expired' END
FROM source;

INSERT INTO fs2_scientific_artifact_quota_events(
    upload_id,operation_id,attempt_id,tenant_id,event_type,reserved_bytes,
    reserved_objects,expires_at,release_reason,occurred_at
)
SELECT upload_id,operation_id,attempt_id,tenant_id,'reserved',reserved_bytes,
       reserved_objects,expires_at,NULL,reserved_at
FROM fs2_scientific_artifact_quota_reservations;

INSERT INTO fs2_scientific_artifact_quota_events(
    upload_id,operation_id,attempt_id,tenant_id,event_type,reserved_bytes,
    reserved_objects,expires_at,release_reason,occurred_at
)
SELECT upload_id,operation_id,attempt_id,tenant_id,'released',reserved_bytes,
       reserved_objects,expires_at,release_reason,released_at
FROM fs2_scientific_artifact_quota_reservations
WHERE state='released';

-- One immutable settlement makes the reservation-to-usage transition
-- auditable and idempotent. The charged value can never exceed the admitted
-- reservation; incomplete lifecycle evidence retains that bounded maximum.
CREATE TABLE fs2_scientific_gpu_settlements (
    -- Deliberately no foreign keys: settlement evidence must survive ordinary
    -- operation and token retention without blocking those existing lifecycles.
    operation_id uuid PRIMARY KEY,
    token_id uuid NOT NULL,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    reserved_gpu_seconds double precision NOT NULL CHECK (
        reserved_gpu_seconds >= 0 AND reserved_gpu_seconds < 'Infinity'::double precision
    ),
    charged_gpu_seconds double precision NOT NULL CHECK (
        charged_gpu_seconds >= 0
        AND charged_gpu_seconds <= reserved_gpu_seconds
        AND charged_gpu_seconds < 'Infinity'::double precision
    ),
    released_gpu_seconds double precision NOT NULL CHECK (
        released_gpu_seconds >= 0
        AND released_gpu_seconds <= reserved_gpu_seconds
        AND released_gpu_seconds < 'Infinity'::double precision
    ),
    evidence_complete boolean NOT NULL,
    reason text NOT NULL CHECK (reason IN ('observed_execution','no_gpu_execution','missing_evidence_fail_safe')),
    settled_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (abs((charged_gpu_seconds+released_gpu_seconds)-reserved_gpu_seconds) < 0.000001),
    CHECK (evidence_complete = (reason<>'missing_evidence_fail_safe'))
);

CREATE TRIGGER fs2_scientific_gpu_settlements_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_gpu_settlements
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

COMMENT ON TABLE fs2_scientific_gpu_settlements IS
    'Exactly-once reservation settlement; observed execution is charged and unused capacity released';

REVOKE ALL ON fs2_scientific_artifact_quota_reservations,
    fs2_scientific_artifact_quota_events,fs2_scientific_gpu_settlements FROM PUBLIC;
REVOKE ALL ON SEQUENCE fs2_scientific_artifact_quota_events_id_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_validate_artifact_quota_transition() FROM PUBLIC;

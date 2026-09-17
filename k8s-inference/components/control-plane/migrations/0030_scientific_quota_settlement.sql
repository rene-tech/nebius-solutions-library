-- SAI-21: retained artifact quota reservations and scientific GPU settlement.
--
-- Upload intents remain immutable provenance. Quota state lives beside them;
-- bytes and object count remain charged until a separate verifier re-fetches
-- exact-key provider absence after removal. The append-only event ledger and
-- absence receipt survive artifact retention purges.

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
    state text NOT NULL CHECK (state IN ('active','removing','released')),
    reserved_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    removal_attempts integer NOT NULL DEFAULT 0 CHECK (removal_attempts BETWEEN 0 AND 1000000),
    removal_claimed_at timestamptz,
    removal_retry_at timestamptz,
    verification_attempts integer NOT NULL DEFAULT 0 CHECK (verification_attempts BETWEEN 0 AND 1000000),
    verification_claimed_at timestamptz,
    verification_retry_at timestamptz,
    released_at timestamptz,
    release_reason text CHECK (release_reason IN ('provider_removed')),
    CHECK (expires_at > reserved_at),
    CHECK (
        (state='active' AND removal_attempts=0 AND removal_claimed_at IS NULL
            AND removal_retry_at IS NULL AND verification_attempts=0
            AND verification_claimed_at IS NULL AND verification_retry_at IS NULL
            AND released_at IS NULL AND release_reason IS NULL)
        OR (state='removing' AND removal_attempts>=1 AND removal_claimed_at IS NOT NULL
            AND removal_retry_at IS NOT NULL AND released_at IS NULL AND release_reason IS NULL)
        OR (state='released' AND released_at IS NOT NULL AND released_at>=reserved_at
            AND release_reason IS NOT NULL)
    )
);

CREATE INDEX fs2_scientific_artifact_quota_reservations_active_idx
    ON fs2_scientific_artifact_quota_reservations (tenant_id,expires_at,upload_id)
    WHERE state<>'released';

CREATE TABLE fs2_scientific_artifact_quota_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    upload_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    event_type text NOT NULL CHECK (event_type IN ('reserved','retention_extended','removal_claimed','released')),
    reserved_bytes bigint NOT NULL CHECK (reserved_bytes BETWEEN 0 AND 1099511627776),
    reserved_objects integer NOT NULL CHECK (reserved_objects = 1),
    expires_at timestamptz NOT NULL,
    release_reason text CHECK (release_reason IN ('provider_removed')),
    occurred_at timestamptz NOT NULL,
    CHECK (
        (event_type='released' AND release_reason IS NOT NULL)
        OR (event_type<>'released' AND release_reason IS NULL)
    )
);

CREATE INDEX fs2_scientific_artifact_quota_events_tenant_idx
    ON fs2_scientific_artifact_quota_events (tenant_id,id);

CREATE TABLE fs2_scientific_artifact_removal_evidence (
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
    CHECK (evidence_kind='absence_confirmed' AND removed_version_count=0)
);

CREATE INDEX fs2_scientific_artifact_removal_evidence_tenant_idx
    ON fs2_scientific_artifact_removal_evidence (tenant_id,observed_at,upload_id);

COMMENT ON TABLE fs2_scientific_artifact_quota_reservations IS
    'Mutable active/removing/released quota state; upload provenance remains immutable and retained';
COMMENT ON TABLE fs2_scientific_artifact_quota_events IS
    'Append-only byte and object reservation ledger retained across artifact purges';
COMMENT ON TABLE fs2_scientific_artifact_removal_evidence IS
    'Append-only provider proof required before any retained byte or object leaves tenant quota';

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
           OR NEW.removal_attempts IS DISTINCT FROM OLD.removal_attempts
           OR NEW.removal_claimed_at IS DISTINCT FROM OLD.removal_claimed_at
           OR NEW.removal_retry_at IS DISTINCT FROM OLD.removal_retry_at
           OR NEW.verification_attempts IS DISTINCT FROM OLD.verification_attempts
           OR NEW.verification_claimed_at IS DISTINCT FROM OLD.verification_claimed_at
           OR NEW.verification_retry_at IS DISTINCT FROM OLD.verification_retry_at
           OR NEW.released_at IS NOT NULL OR NEW.release_reason IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='invalid artifact quota retention extension';
        END IF;
    ELSIF OLD.state='active' AND NEW.state='removing' THEN
        IF NEW.reserved_at IS DISTINCT FROM OLD.reserved_at
           OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
           OR NEW.removal_attempts<>1 OR NEW.removal_claimed_at IS NULL
           OR NEW.removal_retry_at<=NEW.removal_claimed_at
           OR NEW.verification_attempts<>0
           OR NEW.verification_claimed_at IS NOT NULL
           OR NEW.verification_retry_at IS NULL
           OR NEW.released_at IS NOT NULL OR NEW.release_reason IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='invalid artifact quota removal claim';
        END IF;
    ELSIF OLD.state='removing' AND NEW.state='removing' THEN
        IF NEW.reserved_at IS DISTINCT FROM OLD.reserved_at
           OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
           OR NEW.released_at IS NOT NULL OR NEW.release_reason IS NOT NULL
           OR NOT (
               (
                   NEW.removal_attempts=OLD.removal_attempts+1
                   AND NEW.removal_claimed_at>=OLD.removal_claimed_at
                   AND NEW.removal_retry_at>NEW.removal_claimed_at
                   AND NEW.verification_attempts=OLD.verification_attempts
                   AND NEW.verification_claimed_at IS NOT DISTINCT FROM OLD.verification_claimed_at
                   AND NEW.verification_retry_at IS NOT DISTINCT FROM OLD.verification_retry_at
               ) OR (
                   NEW.verification_attempts=OLD.verification_attempts+1
                   AND NEW.verification_claimed_at IS NOT NULL
                   AND NEW.verification_retry_at>NEW.verification_claimed_at
                   AND NEW.removal_attempts=OLD.removal_attempts
                   AND NEW.removal_claimed_at IS NOT DISTINCT FROM OLD.removal_claimed_at
                   AND NEW.removal_retry_at IS NOT DISTINCT FROM OLD.removal_retry_at
               )
           ) THEN
            RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='invalid artifact removal retry claim';
        END IF;
    ELSIF OLD.state='removing' AND NEW.state='released' THEN
        IF NEW.reserved_at IS DISTINCT FROM OLD.reserved_at
           OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
           OR NEW.removal_attempts IS DISTINCT FROM OLD.removal_attempts
           OR NEW.removal_claimed_at IS DISTINCT FROM OLD.removal_claimed_at
           OR NEW.removal_retry_at IS DISTINCT FROM OLD.removal_retry_at
           OR NEW.verification_attempts IS DISTINCT FROM OLD.verification_attempts
           OR NEW.verification_claimed_at IS DISTINCT FROM OLD.verification_claimed_at
           OR NEW.verification_retry_at IS DISTINCT FROM OLD.verification_retry_at
           OR NEW.released_at IS NULL OR NEW.released_at<NEW.reserved_at
           OR NEW.release_reason<>'provider_removed'
           OR NOT EXISTS (
               SELECT 1 FROM fs2_scientific_artifact_removal_evidence evidence
               WHERE evidence.upload_id=NEW.upload_id AND evidence.operation_id=NEW.operation_id
                 AND evidence.attempt_id=NEW.attempt_id AND evidence.tenant_id=NEW.tenant_id
                 AND evidence.observed_at=NEW.released_at
           ) THEN
            RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact quota release lacks exact provider evidence';
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

CREATE TRIGGER fs2_scientific_artifact_removal_evidence_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_artifact_removal_evidence
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

CREATE FUNCTION fs2_scientific_claim_artifact_removals(
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
    eligible_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    candidate record;
    claimed_at timestamptz := clock_timestamp();
BEGIN
    IF p_limit NOT BETWEEN 1 AND 500
       OR (p_operation_id IS NULL)<>(p_tenant_id IS NULL) THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='artifact removal claim scope is invalid';
    END IF;
    FOR candidate IN
        WITH eligible AS (
            SELECT reservation.upload_id,reservation.operation_id,reservation.attempt_id,
                   reservation.tenant_id,reservation.reserved_bytes,reservation.reserved_objects,
                   reservation.expires_at,reservation.state,reservation.removal_attempts,
                   upload.storage_key,
                   row_number() OVER (
                       PARTITION BY reservation.tenant_id
                       ORDER BY reservation.expires_at,reservation.upload_id
                   ) AS tenant_rank
            FROM public.fs2_scientific_artifact_quota_reservations reservation
            JOIN public.fs2_scientific_uploads upload ON upload.id=reservation.upload_id
            WHERE (
                    (reservation.state='active' AND reservation.expires_at<=claimed_at)
                    OR (reservation.state='removing' AND reservation.removal_retry_at<=claimed_at)
                  )
              AND (
                    p_operation_id IS NULL
                    OR (
                        reservation.operation_id=p_operation_id
                        AND reservation.tenant_id=p_tenant_id
                        AND reservation.expires_at<=claimed_at
                    )
                  )
        )
        SELECT * FROM eligible
        ORDER BY tenant_rank,expires_at,tenant_id,upload_id
        LIMIT p_limit
    LOOP
        PERFORM pg_advisory_xact_lock(hashtextextended(candidate.tenant_id,7221));
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
        IF FOUND THEN
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
            eligible_at := candidate.expires_at;
            RETURN NEXT;
        END IF;
    END LOOP;
END
$function$;

CREATE FUNCTION fs2_scientific_claim_artifact_verifications(p_limit integer)
RETURNS TABLE(
    upload_id uuid,
    operation_id uuid,
    attempt_id uuid,
    tenant_id text,
    storage_key text,
    eligible_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    candidate record;
    claimed_at timestamptz := clock_timestamp();
BEGIN
    IF p_limit NOT BETWEEN 1 AND 500 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='artifact verification claim limit is invalid';
    END IF;
    FOR candidate IN
        WITH eligible AS (
            SELECT reservation.upload_id,reservation.operation_id,reservation.attempt_id,
                   reservation.tenant_id,reservation.reserved_bytes,reservation.reserved_objects,
                   reservation.expires_at,reservation.verification_attempts,upload.storage_key,
                   row_number() OVER (
                       PARTITION BY reservation.tenant_id
                       ORDER BY reservation.expires_at,reservation.upload_id
                   ) AS tenant_rank
            FROM public.fs2_scientific_artifact_quota_reservations reservation
            JOIN public.fs2_scientific_uploads upload ON upload.id=reservation.upload_id
            WHERE reservation.state='removing'
              AND reservation.verification_retry_at<=claimed_at
        )
        SELECT * FROM eligible
        ORDER BY tenant_rank,expires_at,tenant_id,upload_id
        LIMIT p_limit
    LOOP
        PERFORM pg_advisory_xact_lock(hashtextextended(candidate.tenant_id,7221));
        UPDATE public.fs2_scientific_artifact_quota_reservations reservation
        SET verification_attempts=reservation.verification_attempts+1,
            verification_claimed_at=claimed_at,
            verification_retry_at=claimed_at+make_interval(
                secs=>LEAST(3600.0,30.0*power(2.0,LEAST(reservation.verification_attempts,6)))
            )
        WHERE reservation.upload_id=candidate.upload_id
          AND reservation.state='removing'
          AND reservation.verification_attempts=candidate.verification_attempts;
        IF FOUND THEN
            upload_id := candidate.upload_id;
            operation_id := candidate.operation_id;
            attempt_id := candidate.attempt_id;
            tenant_id := candidate.tenant_id;
            storage_key := candidate.storage_key;
            eligible_at := candidate.expires_at;
            RETURN NEXT;
        END IF;
    END LOOP;
END
$function$;

CREATE FUNCTION fs2_scientific_record_artifact_removal(
    p_upload_id uuid,
    p_operation_id uuid,
    p_attempt_id uuid,
    p_tenant_id text,
    p_storage_key text,
    p_evidence_kind text,
    p_provider_request_id text,
    p_removed_version_count integer,
    p_observed_at timestamptz
) RETURNS fs2_scientific_artifact_quota_reservations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    reservation record;
    stored record;
BEGIN
    IF p_evidence_kind<>'absence_confirmed'
       OR length(p_provider_request_id) NOT BETWEEN 1 AND 512
       OR p_observed_at>clock_timestamp()+interval '5 minutes'
       OR p_observed_at<clock_timestamp()-interval '5 minutes'
       OR p_removed_version_count<>0 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='artifact provider evidence is invalid';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(p_tenant_id,7221));
    SELECT quota.*,upload.storage_key INTO reservation
    FROM public.fs2_scientific_artifact_quota_reservations quota
    JOIN public.fs2_scientific_uploads upload ON upload.id=quota.upload_id
    WHERE quota.upload_id=p_upload_id AND quota.operation_id=p_operation_id
      AND quota.attempt_id=p_attempt_id AND quota.tenant_id=p_tenant_id
    FOR UPDATE OF quota;
    IF NOT FOUND OR reservation.storage_key<>p_storage_key
       OR reservation.state NOT IN ('removing','released')
       OR reservation.verification_attempts<1
       OR reservation.verification_claimed_at IS NULL
       OR p_observed_at<reservation.verification_claimed_at THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact removal target is not durably fenced';
    END IF;
    INSERT INTO public.fs2_scientific_artifact_removal_evidence(
        upload_id,operation_id,attempt_id,tenant_id,storage_key,evidence_kind,
        provider_request_id,removed_version_count,observed_at
    ) VALUES(
        p_upload_id,p_operation_id,p_attempt_id,p_tenant_id,p_storage_key,p_evidence_kind,
        p_provider_request_id,p_removed_version_count,p_observed_at
    ) ON CONFLICT (upload_id) DO NOTHING;
    SELECT * INTO stored FROM public.fs2_scientific_artifact_removal_evidence
    WHERE upload_id=p_upload_id;
    IF stored.operation_id<>p_operation_id OR stored.attempt_id<>p_attempt_id
       OR stored.tenant_id<>p_tenant_id OR stored.storage_key<>p_storage_key
       OR stored.evidence_kind<>p_evidence_kind
       OR stored.provider_request_id<>p_provider_request_id
       OR stored.removed_version_count<>p_removed_version_count
       OR stored.observed_at<>p_observed_at THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='artifact provider evidence already differs';
    END IF;
    IF reservation.state='released' THEN
        SELECT * INTO reservation
        FROM public.fs2_scientific_artifact_quota_reservations
        WHERE upload_id=p_upload_id;
        RETURN reservation;
    END IF;
    UPDATE public.fs2_scientific_artifact_quota_reservations
    SET state='released',released_at=p_observed_at,release_reason='provider_removed'
    WHERE upload_id=p_upload_id AND state='removing';
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

-- Existing finalized objects retain quota until their recorded artifact
-- retention deadline. Existing unfinished intents receive the same bounded
-- 24-hour cleanup-eligibility window as new admissions. Elapsed wall time is
-- never removal evidence, so every migrated reservation remains quota-counted
-- until a provider-confirmed absence is appended above.
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
       'active',reserved_at,expires_at,NULL,NULL
FROM source;

INSERT INTO fs2_scientific_artifact_quota_events(
    upload_id,operation_id,attempt_id,tenant_id,event_type,reserved_bytes,
    reserved_objects,expires_at,release_reason,occurred_at
)
SELECT upload_id,operation_id,attempt_id,tenant_id,'reserved',reserved_bytes,
       reserved_objects,expires_at,NULL,reserved_at
FROM fs2_scientific_artifact_quota_reservations;

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

CREATE FUNCTION fs2_scientific_settle_terminal_operation(p_operation_id uuid) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    operation_record record;
    batch_record record;
    token_record record;
    plan_stage jsonb;
    stage_record jsonb;
    attempt_record jsonb;
    admission jsonb;
    resource_class text;
    started_at timestamptz;
    completed_at timestamptz;
    accelerator_count integer;
    gpu_stage_count integer := 0;
    observed_gpu_stage_count integer := 0;
    observed_gpu_seconds double precision := 0;
    missing_evidence boolean := false;
    charged_gpu_seconds double precision;
    released_gpu_seconds double precision;
    evidence_complete boolean;
    settlement_reason text;
    operation_status text;
    semantic_outcome text;
    response_status integer;
BEGIN
    SELECT id,token_id,tenant_id,protocol,status,reserved_gpu_seconds,attempt
      INTO operation_record
      FROM public.fs2_operations WHERE id=p_operation_id;
    IF NOT FOUND OR operation_record.protocol<>'scientific-batch-v1' THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='scientific settlement operation is absent';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('fs2-scientific-token' || chr(31) || operation_record.token_id::text,0)
    );
    SELECT id,token_id,tenant_id,protocol,status,reserved_gpu_seconds,attempt
      INTO operation_record
      FROM public.fs2_operations WHERE id=p_operation_id FOR UPDATE;
    SELECT status,state
      INTO batch_record
      FROM public.fs2_scientific_batches WHERE operation_id=p_operation_id FOR UPDATE;
    SELECT id,gpu_seconds_reserved
      INTO token_record
      FROM public.fs2_tokens WHERE id=operation_record.token_id FOR UPDATE;
    IF batch_record IS NULL OR token_record IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='scientific settlement identity is incomplete';
    END IF;
    IF operation_record.status IN ('succeeded','failed','cancelled','preempted','expired') THEN
        IF NOT EXISTS (
            SELECT 1 FROM public.fs2_scientific_gpu_settlements
            WHERE operation_id=p_operation_id AND token_id=operation_record.token_id
              AND tenant_id=operation_record.tenant_id
              AND charged_gpu_seconds=(
                  SELECT estimated_gpu_seconds FROM public.fs2_operations WHERE id=p_operation_id
              )
              AND operation_record.reserved_gpu_seconds=0
        ) THEN
            RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='terminal scientific operation lacks settlement';
        END IF;
        RETURN;
    END IF;
    IF operation_record.status NOT IN ('queued','running')
       OR batch_record.status NOT IN ('succeeded','failed','cancelled')
       OR batch_record.state->>'status' IS DISTINCT FROM batch_record.status THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='scientific batch is not ready for terminal settlement';
    END IF;
    IF operation_record.reserved_gpu_seconds<0
       OR token_record.gpu_seconds_reserved+0.000001<operation_record.reserved_gpu_seconds THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='scientific GPU reservation is inconsistent';
    END IF;

    FOR plan_stage IN SELECT value FROM jsonb_array_elements(batch_record.state->'plan'->'stages') LOOP
        IF plan_stage->>'resource_class'='gpu' THEN
            gpu_stage_count := gpu_stage_count+1;
        END IF;
    END LOOP;
    FOR stage_record IN SELECT value FROM jsonb_array_elements(batch_record.state->'stages') LOOP
        SELECT value->>'resource_class' INTO resource_class
        FROM jsonb_array_elements(batch_record.state->'plan'->'stages')
        WHERE value->>'stage_id'=stage_record->>'stage_id';
        IF resource_class IS NULL THEN
            RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='scientific GPU stage evidence is incomplete';
        END IF;
        IF resource_class='gpu' AND EXISTS (
            SELECT 1 FROM jsonb_array_elements(stage_record->'attempts') attempt
            WHERE attempt->'scheduling_admission' IS NOT NULL
              AND jsonb_typeof(attempt->'scheduling_admission')<>'null'
              AND (attempt->'scheduling_admission'->>'accelerator_count')::integer>0
        ) THEN
            observed_gpu_stage_count := observed_gpu_stage_count+1;
        END IF;
        FOR attempt_record IN SELECT value FROM jsonb_array_elements(stage_record->'attempts') LOOP
            admission := attempt_record->'scheduling_admission';
            IF resource_class='gpu'
               AND (admission IS NULL OR jsonb_typeof(admission)='null') THEN
                -- Attempt identity is persisted before external apply. Once
                -- it exists, missing scheduling evidence is fail-safe charge.
                missing_evidence := true;
                CONTINUE;
            END IF;
            IF resource_class='gpu'
               AND admission IS NOT NULL AND jsonb_typeof(admission)<>'null'
               AND (admission->>'accelerator_count')::integer=0 THEN
                missing_evidence := true;
            END IF;
            IF admission IS NULL OR jsonb_typeof(admission)='null'
               OR (admission->>'accelerator_count')::integer=0 THEN
                CONTINUE;
            END IF;
            accelerator_count := (admission->>'accelerator_count')::integer;
            IF resource_class<>'gpu' THEN
                missing_evidence := true;
                CONTINUE;
            END IF;
            started_at := COALESCE(
                (admission->>'quota_reserved_at')::timestamptz,
                (admission->>'admitted_at')::timestamptz
            );
            completed_at := (attempt_record->>'completed_at')::timestamptz;
            IF started_at IS NULL OR completed_at IS NULL OR completed_at<started_at
               OR attempt_record->>'resource_released'<>'true' THEN
                missing_evidence := true;
            ELSE
                observed_gpu_seconds := observed_gpu_seconds
                    + accelerator_count*extract(epoch FROM completed_at-started_at);
            END IF;
        END LOOP;
    END LOOP;
    IF gpu_stage_count=0 AND operation_record.reserved_gpu_seconds>0 THEN
        missing_evidence := true;
    END IF;
    IF batch_record.status='succeeded' AND observed_gpu_stage_count<gpu_stage_count THEN
        missing_evidence := true;
    END IF;
    IF missing_evidence OR observed_gpu_seconds>operation_record.reserved_gpu_seconds THEN
        charged_gpu_seconds := operation_record.reserved_gpu_seconds;
        evidence_complete := false;
        settlement_reason := 'missing_evidence_fail_safe';
    ELSIF observed_gpu_seconds>0 THEN
        charged_gpu_seconds := observed_gpu_seconds;
        evidence_complete := true;
        settlement_reason := 'observed_execution';
    ELSE
        charged_gpu_seconds := 0;
        evidence_complete := true;
        settlement_reason := 'no_gpu_execution';
    END IF;
    released_gpu_seconds := operation_record.reserved_gpu_seconds-charged_gpu_seconds;
    INSERT INTO public.fs2_scientific_gpu_settlements(
        operation_id,token_id,tenant_id,reserved_gpu_seconds,charged_gpu_seconds,
        released_gpu_seconds,evidence_complete,reason
    ) VALUES(
        operation_record.id,operation_record.token_id,operation_record.tenant_id,
        operation_record.reserved_gpu_seconds,charged_gpu_seconds,released_gpu_seconds,
        evidence_complete,settlement_reason
    );
    UPDATE public.fs2_tokens
    SET gpu_seconds_reserved=gpu_seconds_reserved-operation_record.reserved_gpu_seconds,
        gpu_seconds_used=gpu_seconds_used+charged_gpu_seconds
    WHERE id=operation_record.token_id;

    operation_status := batch_record.status;
    semantic_outcome := CASE WHEN batch_record.status='succeeded' THEN 'passed' ELSE 'failed' END;
    response_status := CASE
        WHEN batch_record.status='succeeded' THEN 200
        WHEN batch_record.status='cancelled' THEN 409
        ELSE 422
    END;
    PERFORM set_config('fs2.scientific_settlement_operation',operation_record.id::text,true);
    UPDATE public.fs2_operations
    SET status=operation_status::fs2_operation_status,completed_at=clock_timestamp(),
        outcome=operation_status,semantic_outcome=semantic_outcome,http_status=response_status,
        error_code=batch_record.state->>'failure_code',error_detail=NULL,worker_id=NULL,
        heartbeat_at=NULL,lease_expires_at=NULL,reserved_gpu_seconds=0,
        estimated_gpu_seconds=charged_gpu_seconds
    WHERE id=operation_record.id;
    INSERT INTO public.fs2_operation_events(operation_id,event,status,attempt)
    VALUES(
        operation_record.id,'scientific_batch_' || operation_status,
        operation_status::fs2_operation_status,operation_record.attempt
    );
END
$function$;

-- Maintenance can discover expired ciphertext without receiving scientific
-- runtime authority. It appends a request; the runtime/controller owns the
-- settlement, cancellation and payload purge under its normal token locks.
CREATE TABLE fs2_scientific_interruption_requests (
    operation_id uuid PRIMARY KEY,
    cause text NOT NULL CHECK (cause IN ('payload_expired')),
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TRIGGER fs2_scientific_interruption_requests_immutable
BEFORE UPDATE OR DELETE ON fs2_scientific_interruption_requests
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_reject_mutation();

COMMENT ON TABLE fs2_scientific_interruption_requests IS
    'Append-only maintenance-to-runtime handoff; contains identities and causes, never payloads';

CREATE FUNCTION fs2_maintenance_stage_payload_expiry(batch_limit integer)
RETURNS TABLE(operation_id uuid,token_id uuid)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF batch_limit NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='payload-expiry batch limit is outside the bound';
    END IF;
    INSERT INTO public.fs2_scientific_interruption_requests(operation_id,cause)
    SELECT operation.id,'payload_expired'
    FROM public.fs2_operations operation
    WHERE operation.protocol='scientific-batch-v1'
      AND operation.status IN ('queued','running')
      AND operation.payload_expires_at<=clock_timestamp()
      AND operation.payload_purged_at IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM public.fs2_scientific_interruption_requests request
          WHERE request.operation_id=operation.id
      )
    ORDER BY operation.payload_expires_at,operation.id
    LIMIT batch_limit
    ON CONFLICT (operation_id) DO NOTHING;

    RETURN QUERY
    SELECT operation.id,operation.token_id
    FROM public.fs2_operations operation
    WHERE operation.protocol<>'scientific-batch-v1'
      AND operation.payload_expires_at<=clock_timestamp()
      AND operation.payload_purged_at IS NULL
    ORDER BY operation.payload_expires_at,operation.id
    LIMIT batch_limit;
END
$function$;

CREATE FUNCTION fs2_maintenance_purge_expired_payloads(batch_limit integer)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    candidate record;
    operation_record record;
    purged integer := 0;
BEGIN
    IF batch_limit NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='payload-expiry batch limit is outside the bound';
    END IF;
    -- The staging routine appends scientific requests only. It never exposes
    -- scientific rows or mutates their operation/token accounting.
    PERFORM * FROM public.fs2_maintenance_stage_payload_expiry(batch_limit);
    FOR candidate IN
        SELECT operation.id,operation.token_id
        FROM public.fs2_operations operation
        WHERE operation.protocol<>'scientific-batch-v1'
          AND operation.payload_expires_at<=clock_timestamp()
          AND operation.payload_purged_at IS NULL
        ORDER BY operation.payload_expires_at,operation.id
        LIMIT batch_limit
    LOOP
        PERFORM pg_advisory_xact_lock(
            hashtextextended('fs2-scientific-token' || chr(31) || candidate.token_id::text,0)
        );
        PERFORM 1 FROM public.fs2_tokens WHERE id=candidate.token_id FOR UPDATE;
        SELECT id,token_id,status,reserved_gpu_seconds INTO operation_record
        FROM public.fs2_operations
        WHERE id=candidate.id AND protocol<>'scientific-batch-v1'
          AND payload_expires_at<=clock_timestamp() AND payload_purged_at IS NULL
        FOR UPDATE SKIP LOCKED;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;
        IF operation_record.status IN ('queued','activating','running')
           AND operation_record.reserved_gpu_seconds>0 THEN
            UPDATE public.fs2_tokens
            SET gpu_seconds_reserved=GREATEST(
                0,gpu_seconds_reserved-operation_record.reserved_gpu_seconds
            )
            WHERE id=operation_record.token_id;
        END IF;
        UPDATE public.fs2_operations
        SET request_key_id=NULL,request_nonce=NULL,request_ciphertext=NULL,
            response_key_id=NULL,response_nonce=NULL,response_ciphertext=NULL,
            payload_purged_at=clock_timestamp(),
            status=CASE WHEN status IN ('queued','activating','running')
                THEN 'expired'::public.fs2_operation_status ELSE status END,
            completed_at=CASE WHEN status IN ('queued','activating','running')
                THEN clock_timestamp() ELSE completed_at END,
            outcome=CASE WHEN status IN ('queued','activating','running') THEN 'expired' ELSE outcome END,
            error_code=CASE WHEN status IN ('queued','activating','running')
                THEN 'payload_expired' ELSE error_code END,
            error_detail=NULL,worker_id=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
            fencing_token=fencing_token+1,
            reserved_gpu_seconds=CASE WHEN status IN ('queued','activating','running')
                THEN 0 ELSE reserved_gpu_seconds END
        WHERE id=operation_record.id AND protocol<>'scientific-batch-v1';
        purged := purged+1;
    END LOOP;
    RETURN purged;
END
$function$;

CREATE FUNCTION fs2_maintenance_delete_expired_rows(
    operation_retention_seconds integer,
    token_retention_seconds integer,
    audit_retention_seconds integer,
    usage_retention_seconds integer,
    batch_limit integer DEFAULT 100
)
RETURNS TABLE(operations integer,tokens integer,audit integer,usage integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    deleted_tokens integer := 0;
BEGIN
    IF operation_retention_seconds<1 OR token_retention_seconds<1
       OR audit_retention_seconds<1 OR usage_retention_seconds<1
       OR batch_limit NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='maintenance retention bound is invalid';
    END IF;
    WITH candidates AS (
        SELECT id FROM public.fs2_operations
        WHERE status IN ('succeeded','failed','cancelled','preempted','expired')
          AND completed_at<clock_timestamp()-make_interval(secs=>operation_retention_seconds)
        ORDER BY completed_at,id FOR UPDATE SKIP LOCKED LIMIT batch_limit
    ), removed AS (
        DELETE FROM public.fs2_operations operation USING candidates
        WHERE operation.id=candidates.id RETURNING operation.id
    ) SELECT count(*)::integer INTO operations FROM removed;

    WITH candidates AS (
        SELECT token.id FROM public.fs2_tokens token
        WHERE (
            (token.revoked_at IS NOT NULL AND token.revoked_at<clock_timestamp()-make_interval(secs=>token_retention_seconds))
            OR (token.expires_at IS NOT NULL AND token.expires_at<clock_timestamp()-make_interval(secs=>token_retention_seconds))
        ) AND NOT EXISTS (
            SELECT 1 FROM public.fs2_operations operation WHERE operation.token_id=token.id
        )
        ORDER BY COALESCE(token.revoked_at,token.expires_at),token.id
        FOR UPDATE SKIP LOCKED LIMIT batch_limit
    ), removed AS (
        DELETE FROM public.fs2_tokens token USING candidates
        WHERE token.id=candidates.id AND NOT EXISTS (
            SELECT 1 FROM public.fs2_operations operation WHERE operation.token_id=token.id
        ) RETURNING token.id
    ) SELECT count(*)::integer INTO deleted_tokens FROM removed;
    tokens := deleted_tokens;

    WITH candidates AS (
        SELECT id FROM public.fs2_audit_events
        WHERE occurred_at<clock_timestamp()-make_interval(secs=>audit_retention_seconds)
        ORDER BY occurred_at,id LIMIT batch_limit
    ), removed AS (
        DELETE FROM public.fs2_audit_events event USING candidates
        WHERE event.id=candidates.id RETURNING event.id
    ) SELECT count(*)::integer INTO audit FROM removed;

    WITH candidates AS (
        SELECT operation_id FROM public.fs2_usage_facts
        WHERE occurred_at<clock_timestamp()-make_interval(secs=>usage_retention_seconds)
        ORDER BY occurred_at,operation_id LIMIT batch_limit
    ), removed AS (
        DELETE FROM public.fs2_usage_facts fact USING candidates
        WHERE fact.operation_id=candidates.operation_id RETURNING fact.operation_id
    ) SELECT count(*)::integer INTO usage FROM removed;
    RETURN NEXT;
END
$function$;

CREATE FUNCTION fs2_scientific_guard_operation_settlement() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF OLD.protocol='scientific-batch-v1'
       AND OLD.status IN ('queued','activating','running')
       AND (
           NEW.status IN ('succeeded','failed','cancelled','preempted','expired')
           OR NEW.reserved_gpu_seconds < OLD.reserved_gpu_seconds
       )
       AND current_setting('fs2.scientific_settlement_operation',true)
           IS DISTINCT FROM OLD.id::text THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='scientific accounting can change only in settlement routine';
    END IF;
    IF OLD.protocol='scientific-batch-v1'
       AND OLD.status IN ('succeeded','failed','cancelled','preempted','expired')
       AND (
           NEW.status IS DISTINCT FROM OLD.status
           OR NEW.token_id IS DISTINCT FROM OLD.token_id
           OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.protocol IS DISTINCT FROM OLD.protocol
           OR NEW.reserved_gpu_seconds IS DISTINCT FROM OLD.reserved_gpu_seconds
           OR NEW.estimated_gpu_seconds IS DISTINCT FROM OLD.estimated_gpu_seconds
       ) THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='terminal scientific operation is immutable';
    END IF;
    IF OLD.protocol='scientific-batch-v1'
       AND OLD.status IN ('queued','activating','running')
       AND (
           NEW.status IN ('succeeded','failed','cancelled','preempted','expired')
           OR NEW.reserved_gpu_seconds < OLD.reserved_gpu_seconds
       )
       AND NOT EXISTS (
           SELECT 1 FROM public.fs2_scientific_gpu_settlements settlement
           WHERE settlement.operation_id=OLD.id AND settlement.token_id=OLD.token_id
             AND settlement.tenant_id=OLD.tenant_id
             AND settlement.reserved_gpu_seconds=OLD.reserved_gpu_seconds
             AND settlement.charged_gpu_seconds=NEW.estimated_gpu_seconds
             AND NEW.reserved_gpu_seconds=0
       ) THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='scientific operation transition lacks GPU settlement';
    END IF;
    IF OLD.protocol='scientific-batch-v1'
       AND NEW.status IN ('succeeded','failed','cancelled','preempted','expired')
       AND NOT EXISTS (
           SELECT 1 FROM public.fs2_scientific_batches batch
           WHERE batch.operation_id=OLD.id
             AND batch.status=CASE
                 WHEN NEW.status='succeeded' THEN 'succeeded'
                 WHEN NEW.status='cancelled' THEN 'cancelled'
                 ELSE 'failed'
             END
       ) THEN
        RAISE EXCEPTION USING ERRCODE='FS202', MESSAGE='scientific operation terminal state is not synchronized';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_scientific_operations_settlement_guard
BEFORE UPDATE ON fs2_operations
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_guard_operation_settlement();

REVOKE ALL ON fs2_scientific_artifact_quota_reservations,
    fs2_scientific_artifact_quota_events,fs2_scientific_artifact_removal_evidence,
    fs2_scientific_gpu_settlements,fs2_scientific_interruption_requests FROM PUBLIC;
REVOKE ALL ON SEQUENCE fs2_scientific_artifact_quota_events_id_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_validate_artifact_quota_transition() FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_claim_artifact_removals(integer,uuid,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_claim_artifact_verifications(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_record_artifact_removal(
    uuid,uuid,uuid,text,text,text,text,integer,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_settle_terminal_operation(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_maintenance_stage_payload_expiry(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_maintenance_purge_expired_payloads(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_maintenance_delete_expired_rows(integer,integer,integer,integer,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_guard_operation_settlement() FROM PUBLIC;

-- Keep the expanded outbox schema writable by the immediately preceding
-- runtime without relaxing the digest contract. Migration application is one
-- transaction, so predecessor pods observe either the pre-0035 schema or this
-- final shape. Their exact INSERT(operation_id,payload) is completed by this
-- trigger before the 0036 NOT NULL constraint is checked.
CREATE FUNCTION fs2_scientific_bind_admission_digest() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
DECLARE
    payload_scheduling_digest text;
BEGIN
    payload_scheduling_digest := NEW.payload #>> '{scheduling,digest}';
    IF payload_scheduling_digest IS NULL
       OR payload_scheduling_digest !~ '^sha256:[0-9a-f]{64}$'
       OR (NEW.scheduling_digest IS NOT NULL
           AND NEW.scheduling_digest::text IS DISTINCT FROM payload_scheduling_digest) THEN
        RAISE EXCEPTION USING ERRCODE='FS204',
            MESSAGE='scientific admission digest differs from its frozen payload';
    END IF;
    NEW.scheduling_digest := payload_scheduling_digest;
    RETURN NEW;
END
$function$;

REVOKE ALL ON FUNCTION fs2_scientific_bind_admission_digest() FROM PUBLIC;

CREATE TRIGGER fs2_scientific_bind_admission_digest_trigger
BEFORE INSERT ON fs2_scientific_admission_outbox
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_bind_admission_digest();

-- Fail closed unless the inserted batch consumes exactly one locked admission
-- handoff. The authoritative value remains independently present in the full
-- frozen payload and the bound NOT NULL digest column.
CREATE OR REPLACE FUNCTION fs2_scientific_consume_admission_outbox() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    frozen_payload jsonb;
    stored_scheduling_digest char(71);
    frozen_scheduling_digest text;
    operation_matches boolean;
    artifact_matches boolean;
    deleted_rows integer;
BEGIN
    SELECT outbox.payload,
           outbox.scheduling_digest,
           outbox.payload #>> '{scheduling,digest}',
           EXISTS (
               SELECT 1
               FROM public.fs2_operations AS operation
               WHERE operation.id=NEW.operation_id
                 AND operation.tenant_id=NEW.tenant_id
                 AND operation.model_id=NEW.model_id
                 AND operation.protocol='scientific-batch-v1'
           ),
           EXISTS (
               SELECT 1
               FROM public.fs2_scientific_artifacts AS artifact
               WHERE artifact.id=NEW.input_artifact_id
                 AND artifact.operation_id=NEW.operation_id
                 AND artifact.tenant_id=NEW.tenant_id
           )
    INTO frozen_payload,
         stored_scheduling_digest,
         frozen_scheduling_digest,
         operation_matches,
         artifact_matches
    FROM public.fs2_scientific_admission_outbox AS outbox
    WHERE outbox.operation_id=NEW.operation_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='FS204',
            MESSAGE='scientific admission handoff is missing';
    END IF;

    IF frozen_scheduling_digest IS NULL
       OR frozen_scheduling_digest !~ '^sha256:[0-9a-f]{64}$'
       OR (stored_scheduling_digest IS NOT NULL
           AND stored_scheduling_digest::text IS DISTINCT FROM frozen_scheduling_digest)
       OR NEW.state IS DISTINCT FROM frozen_payload
       OR NEW.scheduling_digest::text IS DISTINCT FROM frozen_scheduling_digest
       OR NEW.status <> 'queued'
       OR NEW.revision <> 0
       OR NEW.cancel_requested
       OR NEW.controller_id IS NOT NULL
       OR NEW.fencing_token <> 0
       OR NEW.lease_expires_at IS NOT NULL
       OR NOT operation_matches
       OR NOT artifact_matches THEN
        RAISE EXCEPTION USING ERRCODE='FS204',
            MESSAGE='scientific batch differs from its frozen admission';
    END IF;

    DELETE FROM public.fs2_scientific_admission_outbox
    WHERE operation_id=NEW.operation_id
      AND payload=frozen_payload
      AND scheduling_digest IS NOT DISTINCT FROM stored_scheduling_digest;
    GET DIAGNOSTICS deleted_rows = ROW_COUNT;
    IF deleted_rows <> 1 THEN
        RAISE EXCEPTION USING ERRCODE='FS204',
            MESSAGE='scientific admission handoff was not consumed exactly once';
    END IF;
    RETURN NEW;
END
$function$;

REVOKE ALL ON FUNCTION fs2_scientific_consume_admission_outbox() FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_bind_admission_digest() FROM PUBLIC;

DO $grant$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fs2_serve_runtime') THEN
        REVOKE ALL ON FUNCTION fs2_scientific_consume_admission_outbox() FROM fs2_serve_runtime;
        REVOKE ALL ON FUNCTION fs2_scientific_bind_admission_digest() FROM fs2_serve_runtime;
    END IF;
END
$grant$;

COMMENT ON FUNCTION fs2_scientific_consume_admission_outbox() IS
    'Fail-closed exact-row completion with predecessor insertion compatibility';

COMMENT ON COLUMN fs2_scientific_admission_outbox.scheduling_digest IS
    'Exact digest bound from the immutable scheduling snapshot, including predecessor two-column inserts';

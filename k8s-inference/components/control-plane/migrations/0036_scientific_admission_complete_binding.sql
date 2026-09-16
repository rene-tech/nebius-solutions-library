-- Migration 0035 is backfilled by the trusted schema migrator in the same
-- transaction. Refuse to expose a partially bound handoff after that step.
ALTER TABLE fs2_scientific_admission_outbox
    ALTER COLUMN scheduling_digest SET NOT NULL;

-- Consume an outbox row only when the inserted batch is the complete frozen
-- initial state, including its independently stored scheduling digest. A
-- mismatched insertion is rejected so it cannot occupy the operation key and
-- strand the protected handoff.
CREATE OR REPLACE FUNCTION fs2_scientific_consume_admission_outbox() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    frozen_payload jsonb;
    frozen_scheduling_digest char(71);
    operation_matches boolean;
    artifact_matches boolean;
    deleted_rows integer;
BEGIN
    SELECT outbox.payload,outbox.scheduling_digest,
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
    INTO frozen_payload,frozen_scheduling_digest,operation_matches,artifact_matches
    FROM public.fs2_scientific_admission_outbox AS outbox
    WHERE outbox.operation_id=NEW.operation_id
    FOR UPDATE;

    IF FOUND THEN
        IF NEW.state IS DISTINCT FROM frozen_payload
           OR NEW.scheduling_digest IS DISTINCT FROM frozen_scheduling_digest
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
          AND scheduling_digest=frozen_scheduling_digest;
        GET DIAGNOSTICS deleted_rows = ROW_COUNT;
        IF deleted_rows <> 1 THEN
            RAISE EXCEPTION USING ERRCODE='FS204',
                MESSAGE='scientific admission handoff was not consumed exactly once';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

REVOKE ALL ON FUNCTION fs2_scientific_consume_admission_outbox() FROM PUBLIC;

-- A predecessor might have committed an exact initial batch before its
-- application-level completion. Reject a mismatched pair; delete only the
-- fully bound historical pair.
DO $existing$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM fs2_scientific_admission_outbox AS outbox
        JOIN fs2_scientific_batches AS batch USING (operation_id)
        WHERE batch.state IS DISTINCT FROM outbox.payload
           OR batch.scheduling_digest IS DISTINCT FROM outbox.scheduling_digest
           OR batch.status <> 'queued'
           OR batch.revision <> 0
           OR batch.cancel_requested
           OR batch.controller_id IS NOT NULL
           OR batch.fencing_token <> 0
           OR batch.lease_expires_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION USING ERRCODE='FS204',
            MESSAGE='existing scientific batch differs from its frozen admission';
    END IF;
END
$existing$;

DELETE FROM fs2_scientific_admission_outbox AS outbox
USING fs2_scientific_batches AS batch,fs2_operations AS operation
WHERE outbox.operation_id=batch.operation_id
  AND batch.state=outbox.payload
  AND batch.scheduling_digest=outbox.scheduling_digest
  AND batch.status='queued'
  AND batch.revision=0
  AND NOT batch.cancel_requested
  AND batch.controller_id IS NULL
  AND batch.fencing_token=0
  AND batch.lease_expires_at IS NULL
  AND operation.id=batch.operation_id
  AND operation.tenant_id=batch.tenant_id
  AND operation.model_id=batch.model_id
  AND operation.protocol='scientific-batch-v1'
  AND EXISTS (
      SELECT 1
      FROM fs2_scientific_artifacts AS artifact
      WHERE artifact.id=batch.input_artifact_id
        AND artifact.operation_id=batch.operation_id
        AND artifact.tenant_id=batch.tenant_id
  );

DO $grant$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fs2_serve_runtime') THEN
        REVOKE UPDATE,DELETE ON TABLE fs2_scientific_admission_outbox FROM fs2_serve_runtime;
        REVOKE ALL ON FUNCTION fs2_scientific_consume_admission_outbox() FROM fs2_serve_runtime;
    END IF;
END
$grant$;

COMMENT ON FUNCTION fs2_scientific_consume_admission_outbox() IS
    'Non-callable exact-row completion bound to the full initial state and scheduling digest';

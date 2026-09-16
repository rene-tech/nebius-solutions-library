-- Consume the crash-recovery admission row only as a side effect of creating
-- its exact durable scientific batch. Runtime never receives DELETE on the
-- outbox and cannot invoke this trigger function directly.
CREATE FUNCTION fs2_scientific_consume_admission_outbox() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    DELETE FROM public.fs2_scientific_admission_outbox AS outbox
    USING public.fs2_operations AS operation
    WHERE outbox.operation_id=NEW.operation_id
      AND operation.id=NEW.operation_id
      AND operation.tenant_id=NEW.tenant_id
      AND operation.model_id=NEW.model_id
      AND operation.protocol='scientific-batch-v1'
      AND outbox.payload->>'operation_id'=NEW.operation_id::text
      AND outbox.payload->>'batch_id'=NEW.batch_id::text
      AND outbox.payload->>'workload_id'=NEW.workload_id::text
      AND outbox.payload->>'tenant_id'=NEW.tenant_id
      AND outbox.payload->>'model_id'=NEW.model_id
      AND outbox.payload->>'variant_id'=NEW.variant_id
      AND outbox.payload->>'input_artifact_id'=NEW.input_artifact_id::text
      AND outbox.payload->'plan'=NEW.state->'plan'
      AND outbox.payload->'scheduling'=NEW.state->'scheduling'
      AND outbox.payload->'adapter_execution'=NEW.state->'adapter_execution'
      AND outbox.payload->'access_context'=NEW.state->'access_context'
      AND outbox.payload->'input_manifest'=NEW.state->'input_manifest'
      AND outbox.payload->'runtime_artifacts'=NEW.state->'runtime_artifacts'
      AND EXISTS (
          SELECT 1
          FROM public.fs2_scientific_artifacts AS artifact
          WHERE artifact.id=NEW.input_artifact_id
            AND artifact.operation_id=NEW.operation_id
            AND artifact.tenant_id=NEW.tenant_id
      );
    RETURN NEW;
END
$function$;

REVOKE ALL ON FUNCTION fs2_scientific_consume_admission_outbox() FROM PUBLIC;

CREATE TRIGGER fs2_scientific_consume_admission_outbox_trigger
AFTER INSERT ON fs2_scientific_batches
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_consume_admission_outbox();

-- A predecessor could stop after the durable batch commit but before its
-- application-level outbox delete. Consume only rows whose complete immutable
-- admission binding agrees with an already materialized batch.
DELETE FROM fs2_scientific_admission_outbox AS outbox
USING fs2_scientific_batches AS batch, fs2_operations AS operation
WHERE outbox.operation_id=batch.operation_id
  AND operation.id=batch.operation_id
  AND operation.tenant_id=batch.tenant_id
  AND operation.model_id=batch.model_id
  AND operation.protocol='scientific-batch-v1'
  AND outbox.payload->>'operation_id'=batch.operation_id::text
  AND outbox.payload->>'batch_id'=batch.batch_id::text
  AND outbox.payload->>'workload_id'=batch.workload_id::text
  AND outbox.payload->>'tenant_id'=batch.tenant_id
  AND outbox.payload->>'model_id'=batch.model_id
  AND outbox.payload->>'variant_id'=batch.variant_id
  AND outbox.payload->>'input_artifact_id'=batch.input_artifact_id::text
  AND outbox.payload->'plan'=batch.state->'plan'
  AND outbox.payload->'scheduling'=batch.state->'scheduling'
  AND outbox.payload->'adapter_execution'=batch.state->'adapter_execution'
  AND outbox.payload->'access_context'=batch.state->'access_context'
  AND outbox.payload->'input_manifest'=batch.state->'input_manifest'
  AND outbox.payload->'runtime_artifacts'=batch.state->'runtime_artifacts'
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
    'Non-callable exact-row outbox consumption bound to durable scientific batch insertion';

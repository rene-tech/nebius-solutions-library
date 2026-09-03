-- Academic/non-public runtime authorization is deployment-bound. Scientific
-- input and output artifacts therefore never require a caller-supplied license
-- receipt. A receipt may still record the exact reviewed deployment handoff;
-- public artifacts remain forbidden from carrying one.

DO $migration$
DECLARE
    constraint_row record;
BEGIN
    FOR constraint_row IN
        SELECT conrelid::regclass AS table_name, conname
        FROM pg_constraint
        WHERE contype = 'c'
          AND conrelid IN (
              'fs2_scientific_artifacts'::regclass,
              'fs2_scientific_uploads'::regclass
          )
          AND pg_get_constraintdef(oid) LIKE '%access_profile%access_receipt_digest%'
    LOOP
        EXECUTE format(
            'ALTER TABLE %s DROP CONSTRAINT %I',
            constraint_row.table_name,
            constraint_row.conname
        );
    END LOOP;
END
$migration$;

ALTER TABLE fs2_scientific_artifacts
    ADD CONSTRAINT fs2_scientific_artifacts_access_is_deployment_bound CHECK (
        access_profile <> 'public' OR access_receipt_digest IS NULL
    );

ALTER TABLE fs2_scientific_uploads
    ADD CONSTRAINT fs2_scientific_uploads_access_is_deployment_bound CHECK (
        access_profile <> 'public' OR access_receipt_digest IS NULL
    );

-- Kubernetes qualified extended-resource names may contain a DNS prefix up to
-- 253 characters plus '/', followed by a 63-character name (317 total).
DO $migration$
DECLARE
    constraint_row record;
BEGIN
    FOR constraint_row IN
        SELECT conname
        FROM pg_constraint
        WHERE contype = 'c'
          AND conrelid = 'fs2_scientific_stage_attempts'::regclass
          AND pg_get_constraintdef(oid) LIKE '%accelerator_resource_name%253%'
    LOOP
        EXECUTE format(
            'ALTER TABLE fs2_scientific_stage_attempts DROP CONSTRAINT %I',
            constraint_row.conname
        );
    END LOOP;
END
$migration$;

ALTER TABLE fs2_scientific_stage_attempts
    ADD CONSTRAINT fs2_scientific_stage_attempts_accelerator_resource_name_bound CHECK (
        accelerator_resource_name IS NULL
        OR length(accelerator_resource_name) BETWEEN 1 AND 317
    );

ALTER TABLE fs2_operations
    ADD COLUMN dispatch_snapshot jsonb;

ALTER TABLE fs2_operations
    ADD CONSTRAINT fs2_operations_dispatch_snapshot_check CHECK (
        dispatch_snapshot IS NULL
        OR (
            jsonb_typeof(dispatch_snapshot)='object'
            AND dispatch_snapshot->>'schema_version'='fs2-serve.nebius.ai/dynamic-dispatch-snapshot/v1'
            AND octet_length(dispatch_snapshot::text) BETWEEN 2 AND 262144
        )
    );

ALTER TABLE fs2_model_deployment_status_events
    ADD COLUMN source_uid text,
    ADD COLUMN source_resource_version text;

UPDATE fs2_model_deployment_status_events
SET source_uid='legacy:' || observation_id::text,
    source_resource_version='legacy:' || id::text
WHERE source_uid IS NULL OR source_resource_version IS NULL;

ALTER TABLE fs2_model_deployment_status_events
    ALTER COLUMN source_uid SET NOT NULL,
    ALTER COLUMN source_resource_version SET NOT NULL,
    ADD CONSTRAINT fs2_model_deployment_status_source_uid_check
        CHECK (length(source_uid) BETWEEN 1 AND 128),
    ADD CONSTRAINT fs2_model_deployment_status_source_rv_check
        CHECK (length(source_resource_version) BETWEEN 1 AND 128);

COMMENT ON COLUMN fs2_operations.dispatch_snapshot IS
    'Exact non-secret dynamic route material retained only for already-admitted operation dispatch';
COMMENT ON COLUMN fs2_model_deployment_status_events.source_uid IS
    'Observed Kubernetes ModelDeployment UID; CR recreation establishes a new ordering epoch';
COMMENT ON COLUMN fs2_model_deployment_status_events.source_resource_version IS
    'Opaque Kubernetes resourceVersion retained for observation identity and audit';

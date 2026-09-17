-- Bind every newly finalized scientific artifact to one immutable provider
-- object version. Historical rows remain readable as metadata but byte-release
-- paths fail closed until an authorized, evidence-backed backfill records their
-- exact versions and validates this constraint.

ALTER TABLE fs2_scientific_artifacts
    ADD COLUMN object_version_id text;

ALTER TABLE fs2_scientific_artifacts
    ADD CONSTRAINT fs2_scientific_artifacts_object_version_required CHECK (
        object_version_id IS NOT NULL
        AND length(object_version_id) BETWEEN 1 AND 1024
        AND object_version_id ~ '^[A-Za-z0-9._~+=/-]+$'
    ) NOT VALID;

COMMENT ON COLUMN fs2_scientific_artifacts.object_version_id IS
    'Immutable provider object version captured at finalize and required for every byte release';

COMMENT ON CONSTRAINT fs2_scientific_artifacts_object_version_required
    ON fs2_scientific_artifacts IS
    'Enforced for new rows; validate only after exact-version backfill of historical artifacts';

-- Add the exact application-derived scheduling digest to the durable
-- admission handoff. The trusted migrator backfills pre-existing rows by
-- reopening their closed state codec before the successor makes this column
-- mandatory. Runtime receives no UPDATE privilege on this table.
ALTER TABLE fs2_scientific_admission_outbox
    ADD COLUMN scheduling_digest char(71) CHECK (
        scheduling_digest IS NULL OR scheduling_digest ~ '^sha256:[0-9a-f]{64}$'
    );

COMMENT ON COLUMN fs2_scientific_admission_outbox.scheduling_digest IS
    'Exact digest derived from the immutable scheduling snapshot by the trusted admission codec';

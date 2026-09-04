-- Migration 0020 created the crash-safe scientific admission outbox, while
-- its runtime privilege lived only in the migrator's unversioned grant pass.
-- Repair databases that recorded 0020 successfully without retaining that
-- privilege. The canonical role is normally provisioned before this job; the
-- guard keeps source-tree/custom-role migrations valid, with the migrator's
-- post-migration grant pass covering the configured runtime role.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime') THEN
        GRANT SELECT, INSERT, DELETE
            ON TABLE fs2_scientific_admission_outbox
            TO fs2_serve_runtime;
    END IF;
END;
$$;

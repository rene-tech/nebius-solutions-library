-- The crash-safe admission path reads the frozen outbox row with
-- SELECT ... FOR SHARE before committing the public Operation. PostgreSQL
-- requires UPDATE privilege for that row-locking read even though the runtime
-- never updates the outbox. Add the missing privilege without changing the
-- already-applied 0021 migration.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime') THEN
        GRANT UPDATE
            ON TABLE fs2_scientific_admission_outbox
            TO fs2_serve_runtime;
    END IF;
END;
$$;

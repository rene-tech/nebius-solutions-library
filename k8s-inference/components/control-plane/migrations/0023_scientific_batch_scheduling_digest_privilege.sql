-- The scientific controller writes the complete frozen state envelope and
-- re-emits its immutable scheduling digest on every fenced transition. The
-- runtime role already has UPDATE on every other column in that statement;
-- add the omitted digest column without broadening it to table-level UPDATE.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime') THEN
        GRANT UPDATE (scheduling_digest)
            ON TABLE fs2_scientific_batches
            TO fs2_serve_runtime;
    END IF;
END;
$$;

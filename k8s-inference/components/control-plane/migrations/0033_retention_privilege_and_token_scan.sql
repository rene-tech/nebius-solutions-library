-- Each bounded token-retention candidate needs one FK eligibility probe. The
-- original active-only token index cannot answer whether any historical
-- operation remains, so provide a complete lookup without scanning the
-- operation backlog.
CREATE INDEX fs2_operations_token_retention_idx
    ON fs2_operations (token_id);

-- PostgreSQL factory defaults include PUBLIC execute on functions, and an
-- operator can add defaults for tables, sequences or service group roles.
-- Make the schema owner's global defaults explicitly private. The migrator
-- repeats this reconciliation for deployment-configured role names.
ALTER DEFAULT PRIVILEGES REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES REVOKE ALL ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM PUBLIC;

DO $defaults$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'fs2_serve_reporting',
        'fs2_serve_runtime',
        'fs2_serve_maintenance',
        'fs2_serve_activation'
    ] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=role_name) THEN
            EXECUTE format('ALTER DEFAULT PRIVILEGES REVOKE ALL ON TABLES FROM %I',role_name);
            EXECUTE format('ALTER DEFAULT PRIVILEGES REVOKE ALL ON SEQUENCES FROM %I',role_name);
            EXECUTE format('ALTER DEFAULT PRIVILEGES REVOKE ALL ON FUNCTIONS FROM %I',role_name);
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %I',role_name
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %I',role_name
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM %I',role_name
            );
        END IF;
    END LOOP;
END
$defaults$;

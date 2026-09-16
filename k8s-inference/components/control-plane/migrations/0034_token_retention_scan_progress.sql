-- Keep each token-retention pass index/batch bounded without permanently
-- re-reading an operation-referenced head window. Only the maintenance role
-- may advance these two non-payload cursors; the migrator grants the configured
-- maintenance group after applying the immutable migration set.
CREATE TABLE fs2_retention_scan_cursors (
    stream text PRIMARY KEY CHECK (stream IN ('tokens_revoked','tokens_expiry')),
    position_at timestamptz,
    position_id uuid,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((position_at IS NULL) = (position_id IS NULL))
);

INSERT INTO fs2_retention_scan_cursors(stream)
VALUES ('tokens_revoked'),('tokens_expiry');

REVOKE ALL ON TABLE fs2_retention_scan_cursors FROM PUBLIC;

DO $roles$
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
            EXECUTE format('REVOKE ALL ON TABLE fs2_retention_scan_cursors FROM %I',role_name);
        END IF;
    END LOOP;
END
$roles$;

COMMENT ON TABLE fs2_retention_scan_cursors IS
    'Maintenance-only bounded keyset progress; contains no token or request payload';

-- App identity is independent of qualified model identity. Default app rows
-- preserve historical route IDs; a cloned app owns a new public route.
CREATE TABLE IF NOT EXISTS fs2_apps (
    app_id uuid PRIMARY KEY,
    display_name text NOT NULL,
    model_ref text NOT NULL,
    public_model_id text NOT NULL UNIQUE,
    execution_mode text NOT NULL CHECK (execution_mode IN ('serving', 'scientific')),
    namespace text NOT NULL,
    deployment_name text,
    academic_required boolean NOT NULL DEFAULT false,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE(namespace, deployment_name)
);
CREATE INDEX IF NOT EXISTS fs2_apps_model_ref ON fs2_apps(model_ref, app_id);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime') THEN
        GRANT SELECT, INSERT, UPDATE ON fs2_apps TO fs2_serve_runtime;
    END IF;
END;
$$;

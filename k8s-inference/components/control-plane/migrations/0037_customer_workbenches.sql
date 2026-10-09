-- Customer bindings are independent of replaceable Serverless runtimes.
-- No cascade deletes: retiring an instance never deletes a user or bucket.
CREATE TABLE fs2_customer_profiles (
    tenant_id text PRIMARY KEY,
    display_name text NOT NULL,
    purpose text NOT NULL DEFAULT 'customer',
    archived boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE fs2_workbenches (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    principal_ids text[] NOT NULL,
    name text NOT NULL,
    management text NOT NULL CHECK (management IN ('managed','customer')),
    endpoint_id text NOT NULL UNIQUE,
    project_id text NOT NULL,
    bucket_name text,
    state_filesystem_id text,
    protected boolean NOT NULL DEFAULT false,
    protection_reason text NOT NULL DEFAULT '',
    desired_release text,
    revision bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX fs2_workbenches_tenant ON fs2_workbenches(tenant_id);

CREATE TABLE fs2_workbench_observations (
    resource_id text PRIMARY KEY,
    project_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('endpoint','bucket')),
    observation jsonb NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX fs2_workbench_observations_project ON fs2_workbench_observations(project_id, kind);

CREATE TABLE fs2_workbench_operations (
    id uuid PRIMARY KEY,
    workbench_id uuid NOT NULL REFERENCES fs2_workbenches(id),
    idempotency_key text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('backup','upgrade','restore','retire')),
    state text NOT NULL DEFAULT 'queued',
    requested_by text NOT NULL,
    specification jsonb NOT NULL,
    progress jsonb NOT NULL DEFAULT '{}',
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(workbench_id,idempotency_key)
);
CREATE UNIQUE INDEX fs2_workbench_one_active_operation ON fs2_workbench_operations(workbench_id)
    WHERE state IN ('queued','running','awaiting_confirmation');

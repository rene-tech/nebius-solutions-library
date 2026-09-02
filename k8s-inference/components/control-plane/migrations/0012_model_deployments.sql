CREATE TABLE fs2_model_deployment_revisions (
    namespace text NOT NULL CHECK (
        length(namespace) BETWEEN 1 AND 63
        AND namespace ~ '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$'
    ),
    name text NOT NULL CHECK (
        length(name) BETWEEN 1 AND 253
        AND name ~ '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*$'
    ),
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    revision bigint NOT NULL CHECK (revision >= 1),
    etag char(71) NOT NULL CHECK (etag ~ '^sha256:[a-f0-9]{64}$'),
    spec jsonb NOT NULL CHECK (
        jsonb_typeof(spec)='object'
        AND octet_length(spec::text) BETWEEN 2 AND 8388608
        AND spec->>'tenantId'=tenant_id
    ),
    action text NOT NULL CHECK (action IN ('create','update','rollback')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 200),
    previous_revision bigint,
    PRIMARY KEY (namespace,name,revision),
    UNIQUE (namespace,name,revision,tenant_id,etag),
    FOREIGN KEY (namespace,name,previous_revision)
        REFERENCES fs2_model_deployment_revisions(namespace,name,revision),
    CHECK (
        (revision=1 AND previous_revision IS NULL AND action='create')
        OR (revision>1 AND previous_revision=revision-1 AND action IN ('update','rollback'))
    )
);

CREATE INDEX fs2_model_deployment_revisions_history_idx
    ON fs2_model_deployment_revisions (namespace,name,revision DESC);

CREATE TABLE fs2_model_deployments (
    namespace text NOT NULL,
    name text NOT NULL,
    tenant_id text NOT NULL,
    current_revision bigint NOT NULL,
    current_etag char(71) NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_by text NOT NULL CHECK (length(updated_by) BETWEEN 1 AND 200),
    PRIMARY KEY (namespace,name),
    UNIQUE (namespace,tenant_id,name),
    FOREIGN KEY (namespace,name,current_revision,tenant_id,current_etag)
        REFERENCES fs2_model_deployment_revisions(namespace,name,revision,tenant_id,etag)
);

CREATE INDEX fs2_model_deployments_tenant_list_idx
    ON fs2_model_deployments (namespace,tenant_id,name);

CREATE TABLE fs2_model_deployment_idempotency (
    actor_id uuid NOT NULL,
    hmac_key_id text NOT NULL CHECK (length(hmac_key_id) BETWEEN 1 AND 64),
    key_hmac char(64) NOT NULL CHECK (key_hmac ~ '^[a-f0-9]{64}$'),
    request_hmac char(64) NOT NULL CHECK (request_hmac ~ '^[a-f0-9]{64}$'),
    namespace text NOT NULL,
    name text NOT NULL,
    revision bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (actor_id,hmac_key_id,key_hmac),
    FOREIGN KEY (namespace,name,revision)
        REFERENCES fs2_model_deployment_revisions(namespace,name,revision)
);

CREATE INDEX fs2_model_deployment_idempotency_revision_idx
    ON fs2_model_deployment_idempotency (namespace,name,revision);

CREATE TABLE fs2_model_deployment_status_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_id uuid NOT NULL UNIQUE,
    namespace text NOT NULL,
    name text NOT NULL,
    tenant_id text NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 1),
    spec_etag char(71) NOT NULL CHECK (spec_etag ~ '^sha256:[a-f0-9]{64}$'),
    status jsonb NOT NULL CHECK (
        jsonb_typeof(status)='object'
        AND octet_length(status::text) BETWEEN 2 AND 8388608
        AND status->>'spec_digest'=spec_etag
    ),
    observed_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (namespace,name,revision,tenant_id,spec_etag)
        REFERENCES fs2_model_deployment_revisions(namespace,name,revision,tenant_id,etag)
);

CREATE INDEX fs2_model_deployment_status_latest_idx
    ON fs2_model_deployment_status_events (namespace,name,id DESC);

COMMENT ON TABLE fs2_model_deployment_revisions IS
    'Append-only ModelDeployment desired-state history; rows do not assert Kubernetes apply or readiness';
COMMENT ON TABLE fs2_model_deployments IS
    'Current ModelDeployment revision projection; Kubernetes ownership and status remain separate';
COMMENT ON TABLE fs2_model_deployment_idempotency IS
    'HMAC-only replay receipts for internal revision writes; raw idempotency keys are never persisted';
COMMENT ON TABLE fs2_model_deployment_status_events IS
    'Append-only bounded controller observations; latest event may be stale relative to desired state';

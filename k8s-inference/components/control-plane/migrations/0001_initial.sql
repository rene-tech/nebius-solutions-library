CREATE TABLE IF NOT EXISTS fs2_tokens (
    id uuid PRIMARY KEY,
    prefix text NOT NULL UNIQUE,
    pepper_key_id text NOT NULL,
    digest text NOT NULL,
    principal_id text NOT NULL,
    tenant_id text NOT NULL,
    scopes text[] NOT NULL,
    models text[] NOT NULL,
    expires_at timestamptz,
    request_budget bigint CHECK (request_budget IS NULL OR request_budget > 0),
    requests_used bigint NOT NULL DEFAULT 0 CHECK (requests_used >= 0),
    gpu_seconds_budget double precision CHECK (gpu_seconds_budget IS NULL OR gpu_seconds_budget > 0),
    gpu_seconds_used double precision NOT NULL DEFAULT 0 CHECK (gpu_seconds_used >= 0),
    gpu_seconds_reserved double precision NOT NULL DEFAULT 0 CHECK (gpu_seconds_reserved >= 0),
    max_concurrency integer NOT NULL CHECK (max_concurrency > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by text NOT NULL,
    revoked_at timestamptz
);

CREATE INDEX IF NOT EXISTS fs2_tokens_tenant_idx ON fs2_tokens (tenant_id, created_at DESC);

DO $migration$
BEGIN
    CREATE TYPE fs2_operation_status AS ENUM (
        'queued', 'activating', 'running', 'succeeded', 'failed', 'cancelled', 'preempted', 'expired'
    );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$migration$;

CREATE TABLE IF NOT EXISTS fs2_operations (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    principal_id text NOT NULL,
    token_id uuid NOT NULL REFERENCES fs2_tokens(id),
    model_id text NOT NULL,
    model_revision text NOT NULL,
    protocol text NOT NULL,
    operation text NOT NULL,
    idempotency_key text NOT NULL,
    request_hmac_key_id text NOT NULL,
    request_hmac char(64) NOT NULL,
    request_key_id text,
    request_nonce bytea,
    request_ciphertext bytea,
    request_content_type text NOT NULL,
    traceparent text,
    status fs2_operation_status NOT NULL DEFAULT 'queued',
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    deadline_at timestamptz,
    activation_started_at timestamptz,
    ready_at timestamptz,
    started_at timestamptz,
    completed_at timestamptz,
    outcome text,
    semantic_outcome text,
    http_status integer,
    response_hmac_key_id text,
    response_hmac char(64),
    response_key_id text,
    response_nonce bytea,
    response_ciphertext bytea,
    response_content_type text,
    payload_expires_at timestamptz NOT NULL,
    error_code text,
    error_detail text,
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts > 0 AND max_attempts <= 10),
    worker_id text,
    fencing_token bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    heartbeat_at timestamptz,
    lease_expires_at timestamptz,
    pod_uid text,
    node_uid text,
    gpu_uuids text[] NOT NULL DEFAULT '{}',
    gpu_count integer NOT NULL DEFAULT 0 CHECK (gpu_count >= 0),
    preemptible boolean,
    estimated_gpu_seconds double precision NOT NULL DEFAULT 0 CHECK (estimated_gpu_seconds >= 0),
    reserved_gpu_seconds double precision NOT NULL DEFAULT 0 CHECK (reserved_gpu_seconds >= 0),
    cold_start_seconds double precision CHECK (cold_start_seconds IS NULL OR cold_start_seconds >= 0),
    UNIQUE (tenant_id, principal_id, token_id, idempotency_key)
    ,CHECK (
        (request_key_id IS NULL AND request_nonce IS NULL AND request_ciphertext IS NULL)
        OR (request_key_id IS NOT NULL AND request_nonce IS NOT NULL
            AND request_ciphertext IS NOT NULL AND octet_length(request_nonce)=12
            AND octet_length(request_ciphertext)>=16)
    )
    ,CHECK (
        (response_key_id IS NULL AND response_nonce IS NULL AND response_ciphertext IS NULL)
        OR (response_key_id IS NOT NULL AND response_nonce IS NOT NULL
            AND response_ciphertext IS NOT NULL AND octet_length(response_nonce)=12
            AND octet_length(response_ciphertext)>=16)
    )
);

CREATE INDEX IF NOT EXISTS fs2_operations_queue_idx
    ON fs2_operations (available_at, accepted_at, id)
    WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS fs2_operations_tenant_idx
    ON fs2_operations (tenant_id, accepted_at DESC);
CREATE INDEX IF NOT EXISTS fs2_operations_token_active_idx
    ON fs2_operations (token_id, status)
    WHERE status IN ('queued', 'activating', 'running');
CREATE INDEX IF NOT EXISTS fs2_operations_payload_expiry_idx
    ON fs2_operations (payload_expires_at)
    WHERE request_ciphertext IS NOT NULL OR response_ciphertext IS NOT NULL;
CREATE INDEX IF NOT EXISTS fs2_operations_lease_expiry_idx
    ON fs2_operations (lease_expires_at)
    WHERE status IN ('activating', 'running');

CREATE TABLE IF NOT EXISTS fs2_operation_events (
    id bigserial PRIMARY KEY,
    operation_id uuid NOT NULL REFERENCES fs2_operations(id) ON DELETE CASCADE,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    event text NOT NULL,
    status fs2_operation_status NOT NULL,
    attempt integer NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS fs2_operation_events_operation_idx
    ON fs2_operation_events (operation_id, occurred_at, id);

CREATE TABLE IF NOT EXISTS fs2_audit_events (
    id bigserial PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    actor text NOT NULL,
    tenant_id text,
    token_id uuid,
    action text NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    outcome text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS fs2_audit_tenant_idx ON fs2_audit_events (tenant_id, occurred_at DESC, id DESC);

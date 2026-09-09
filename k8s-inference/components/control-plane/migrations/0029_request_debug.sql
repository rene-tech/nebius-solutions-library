-- Opt-in preproduction debug exchanges. Clear columns contain only list/filter
-- metadata; headers, query, complete bodies and exception detail use the existing
-- AES-GCM payload key ring. No operation FK: pre-admission rejection is evidence.
CREATE TABLE fs2_request_debug (
    id uuid PRIMARY KEY,
    source text NOT NULL CHECK (source IN ('public', 'upstream')),
    request_id uuid,
    operation_id uuid,
    operation_attempt integer CHECK (operation_attempt >= 0),
    upstream_attempt integer CHECK (upstream_attempt >= 1),
    started_at timestamptz NOT NULL,
    completed_at timestamptz NOT NULL,
    tenant_id text,
    principal_id text,
    token_id uuid,
    model_id text,
    mcp_tool text,
    endpoint text NOT NULL,
    method text NOT NULL,
    http_status integer CHECK (http_status BETWEEN 100 AND 599),
    error_type text,
    disconnected boolean NOT NULL,
    request_observed_bytes bigint NOT NULL CHECK (request_observed_bytes >= 0),
    response_observed_bytes bigint NOT NULL CHECK (response_observed_bytes >= 0),
    request_complete boolean NOT NULL,
    response_complete boolean NOT NULL,
    request_redacted boolean NOT NULL,
    response_redacted boolean NOT NULL,
    key_id text NOT NULL,
    nonce bytea NOT NULL CHECK (octet_length(nonce) = 12),
    ciphertext bytea NOT NULL
);
CREATE INDEX fs2_request_debug_time ON fs2_request_debug (started_at DESC, id DESC);
CREATE INDEX fs2_request_debug_model_time ON fs2_request_debug (model_id, started_at DESC, id DESC);
CREATE INDEX fs2_request_debug_tenant_time ON fs2_request_debug (tenant_id, started_at DESC, id DESC);
CREATE INDEX fs2_request_debug_operation ON fs2_request_debug (operation_id, started_at DESC, id DESC)
    WHERE operation_id IS NOT NULL;
CREATE INDEX fs2_request_debug_request ON fs2_request_debug (request_id) WHERE request_id IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime') THEN
        GRANT SELECT, INSERT ON fs2_request_debug TO fs2_serve_runtime;
    END IF;
END;
$$;

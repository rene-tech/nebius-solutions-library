-- Actual public HTTP exchanges, not logical runs. No payloads, authorization
-- headers or query strings. Nullable bytes mean an incomplete/unobserved body.
CREATE TABLE fs2_request_telemetry (
    request_id uuid PRIMARY KEY,
    started_at timestamptz NOT NULL,
    completed_at timestamptz NOT NULL,
    endpoint text NOT NULL,
    method text NOT NULL,
    transport text NOT NULL CHECK (transport IN ('http', 'mcp')),
    http_status integer CHECK (http_status BETWEEN 100 AND 599),
    response_duration_seconds double precision NOT NULL CHECK (response_duration_seconds >= 0),
    request_bytes bigint CHECK (request_bytes >= 0),
    response_bytes bigint CHECK (response_bytes >= 0),
    request_bytes_observed bigint NOT NULL CHECK (request_bytes_observed >= 0),
    response_bytes_observed bigint NOT NULL CHECK (response_bytes_observed >= 0),
    request_complete boolean NOT NULL,
    response_complete boolean NOT NULL,
    disconnected boolean NOT NULL,
    error_type text,
    tenant_id text,
    principal_id text,
    token_id uuid,
    model_id text,
    operation_id uuid,
    mcp_tool text,
    mcp_is_error boolean,
    CHECK (request_complete = (request_bytes IS NOT NULL)),
    CHECK (response_complete = (response_bytes IS NOT NULL)),
    CHECK ((request_complete AND request_bytes = request_bytes_observed)
        OR (NOT request_complete AND request_bytes IS NULL)),
    CHECK ((response_complete AND response_bytes = response_bytes_observed)
        OR (NOT response_complete AND response_bytes IS NULL))
);
CREATE INDEX fs2_request_telemetry_model_time ON fs2_request_telemetry (model_id, started_at);
CREATE INDEX fs2_request_telemetry_owner_time ON fs2_request_telemetry (tenant_id, principal_id, started_at);
CREATE INDEX fs2_request_telemetry_operation ON fs2_request_telemetry (operation_id, started_at)
    WHERE operation_id IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime') THEN
        GRANT SELECT, INSERT ON fs2_request_telemetry TO fs2_serve_runtime;
    END IF;
END;
$$;

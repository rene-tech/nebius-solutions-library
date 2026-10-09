-- Independent additive workshop schema; no changes to existing serving tables.
CREATE SCHEMA IF NOT EXISTS fs2_workshop;
CREATE TABLE IF NOT EXISTS fs2_workshop.batches (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    principal_id text NOT NULL,
    idempotency_key text NOT NULL,
    request_sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, principal_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS fs2_workshop.runs (
    id uuid PRIMARY KEY,
    batch_id uuid NOT NULL REFERENCES fs2_workshop.batches(id),
    tenant_id text NOT NULL,
    principal_id text NOT NULL,
    token_id text NOT NULL,
    credential_ciphertext bytea,
    status text NOT NULL CHECK (status IN ('queued','running','paused','takeover','interrupted','completed','failed','aborted')),
    state jsonb NOT NULL,
    version bigint NOT NULL DEFAULT 0,
    lease_owner text,
    lease_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workshop_queue ON fs2_workshop.runs(status, updated_at);
CREATE INDEX IF NOT EXISTS workshop_owner ON fs2_workshop.runs(tenant_id, principal_id, created_at DESC);
CREATE TABLE IF NOT EXISTS fs2_workshop.events (
    id bigserial PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES fs2_workshop.runs(id),
    kind text NOT NULL,
    data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workshop_run_events ON fs2_workshop.events(run_id,id);
CREATE TABLE IF NOT EXISTS fs2_workshop.audio (
    run_id uuid NOT NULL REFERENCES fs2_workshop.runs(id),
    turn_index integer NOT NULL,
    wav bytea NOT NULL,
    metadata jsonb NOT NULL,
    PRIMARY KEY(run_id,turn_index)
);
-- Bounded speech artifacts: attempts are distinct so interrupted retries never
-- overwrite an earlier recording or masquerade as the committed turn.
CREATE TABLE IF NOT EXISTS fs2_workshop.audio_segments (
    run_id uuid NOT NULL REFERENCES fs2_workshop.runs(id),
    turn_index integer NOT NULL,
    attempt_id uuid NOT NULL,
    segment_index integer NOT NULL,
    wav bytea NOT NULL,
    metadata jsonb NOT NULL,
    PRIMARY KEY(run_id,turn_index,attempt_id,segment_index)
);
GRANT USAGE ON SCHEMA fs2_workshop TO fs2_serve_runtime, fs2_serve_reporting;
GRANT SELECT, INSERT, UPDATE ON fs2_workshop.batches, fs2_workshop.runs TO fs2_serve_runtime;
GRANT SELECT, INSERT ON fs2_workshop.events, fs2_workshop.audio, fs2_workshop.audio_segments TO fs2_serve_runtime;
GRANT USAGE ON SEQUENCE fs2_workshop.events_id_seq TO fs2_serve_runtime;
GRANT SELECT ON ALL TABLES IN SCHEMA fs2_workshop TO fs2_serve_reporting;

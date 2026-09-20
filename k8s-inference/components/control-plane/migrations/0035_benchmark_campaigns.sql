-- Durable benchmark intent/results. Kueue and the existing serving admission
-- remain the owners of actual GPU work; this queue only coordinates experiments.
CREATE TABLE fs2_benchmark_campaigns (
    id uuid PRIMARY KEY,
    name text NOT NULL UNIQUE,
    spec jsonb NOT NULL,
    spec_sha256 text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    max_parallel integer NOT NULL CHECK (max_parallel BETWEEN 1 AND 16)
);
CREATE TABLE fs2_benchmark_trials (
    id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES fs2_benchmark_campaigns(id),
    case_id text NOT NULL,
    model_id text NOT NULL,
    workload_class text NOT NULL,
    repetition integer NOT NULL CHECK (repetition > 0),
    case_spec jsonb NOT NULL,
    status text NOT NULL CHECK (status IN
        ('queued','running','succeeded','failed','capacity-unavailable','unsupported')),
    fence bigint NOT NULL DEFAULT 0,
    worker text,
    lease_until timestamptz,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(campaign_id,case_id,repetition)
);
CREATE INDEX fs2_benchmark_trials_claim ON fs2_benchmark_trials(campaign_id,status,lease_until,created_at);
CREATE INDEX fs2_benchmark_trials_model ON fs2_benchmark_trials(model_id,updated_at DESC);
CREATE TABLE fs2_benchmark_attempts (
    trial_id uuid NOT NULL REFERENCES fs2_benchmark_trials(id),
    fence bigint NOT NULL,
    worker text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    outcome text,
    PRIMARY KEY(trial_id,fence)
);

DO $migration$
BEGIN
    CREATE TYPE fs2_activation_action AS ENUM ('activate', 'deactivate');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$migration$;

DO $migration$
BEGIN
    CREATE TYPE fs2_activation_status AS ENUM ('queued', 'claimed', 'ready', 'failed', 'expired');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$migration$;

CREATE TABLE IF NOT EXISTS fs2_activation_intents (
    id uuid PRIMARY KEY,
    operation_id uuid UNIQUE REFERENCES fs2_operations(id) ON DELETE CASCADE,
    operation_attempt integer NOT NULL CHECK (operation_attempt >= 0 AND operation_attempt <= 10),
    model_id text NOT NULL,
    model_revision text NOT NULL,
    binding_digest char(64) NOT NULL CHECK (binding_digest ~ '^[0-9a-f]{64}$'),
    action fs2_activation_action NOT NULL,
    status fs2_activation_status NOT NULL DEFAULT 'queued',
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    deadline_at timestamptz,
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0 AND attempt <= 10),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0 AND max_attempts <= 10),
    controller_id text,
    fencing_token bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    heartbeat_at timestamptz,
    lease_expires_at timestamptz,
    scale_contract_digest char(64) CHECK (
        scale_contract_digest IS NULL OR scale_contract_digest ~ '^[0-9a-f]{64}$'
    ),
    target_uid text,
    target_resource_version text,
    target_observed_generation bigint,
    target_template_digest char(64),
    target_active boolean,
    target_observed_at timestamptz,
    error_code text,
    CHECK (
        (action='activate' AND operation_id IS NOT NULL)
        OR (action='deactivate' AND operation_id IS NULL)
    ),
    CHECK (
        (target_uid IS NULL AND target_resource_version IS NULL
            AND target_observed_generation IS NULL
            AND target_template_digest IS NULL AND target_active IS NULL
            AND target_observed_at IS NULL)
        OR (target_uid IS NOT NULL AND target_resource_version IS NOT NULL
            AND target_resource_version ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
            AND target_observed_generation IS NOT NULL AND target_observed_generation > 0
            AND target_template_digest ~ '^[0-9a-f]{64}$'
            AND target_active IS NOT NULL AND target_observed_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS fs2_activation_intents_queue_idx
    ON fs2_activation_intents (available_at, requested_at, id)
    WHERE status='queued';

CREATE INDEX IF NOT EXISTS fs2_activation_intents_lease_idx
    ON fs2_activation_intents (lease_expires_at)
    WHERE status='claimed';

CREATE UNIQUE INDEX IF NOT EXISTS fs2_activation_one_deactivate_idx
    ON fs2_activation_intents (model_id, action)
    WHERE action='deactivate' AND status IN ('queued','claimed');

CREATE TABLE IF NOT EXISTS fs2_activation_target_state (
    model_id text PRIMARY KEY,
    target_uid text NOT NULL,
    resource_version text NOT NULL CHECK (resource_version ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    observed_generation bigint NOT NULL CHECK (observed_generation > 0),
    template_digest char(64) NOT NULL CHECK (template_digest ~ '^[0-9a-f]{64}$'),
    active boolean NOT NULL,
    observed_at timestamptz NOT NULL,
    controller_fencing_token bigint NOT NULL CHECK (controller_fencing_token > 0)
);

CREATE TABLE IF NOT EXISTS fs2_activation_events (
    id bigserial PRIMARY KEY,
    intent_id uuid NOT NULL REFERENCES fs2_activation_intents(id) ON DELETE CASCADE,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    event text NOT NULL,
    status fs2_activation_status NOT NULL,
    attempt integer NOT NULL,
    fencing_token bigint NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS fs2_activation_events_intent_idx
    ON fs2_activation_events (intent_id, occurred_at, id);

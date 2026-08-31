CREATE TABLE IF NOT EXISTS fs2_activation_controller_status (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    controller_id text NOT NULL CHECK (char_length(controller_id) BETWEEN 1 AND 253),
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    activation_set_digest char(64) NOT NULL CHECK (activation_set_digest ~ '^[0-9a-f]{64}$'),
    heartbeat_at timestamptz NOT NULL,
    lease_expires_at timestamptz NOT NULL CHECK (lease_expires_at > heartbeat_at)
);

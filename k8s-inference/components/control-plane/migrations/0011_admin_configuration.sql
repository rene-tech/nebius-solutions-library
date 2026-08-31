CREATE TABLE fs2_configuration_revisions (
    revision bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    etag char(64) NOT NULL CHECK (etag ~ '^[a-f0-9]{64}$'),
    desired jsonb NOT NULL CHECK (
        jsonb_typeof(desired)='object' AND octet_length(desired::text) BETWEEN 2 AND 8388608
    ),
    effective jsonb NOT NULL CHECK (
        jsonb_typeof(effective)='object' AND octet_length(effective::text) BETWEEN 2 AND 8388608
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 200),
    previous_revision bigint UNIQUE REFERENCES fs2_configuration_revisions(revision),
    reconciliation_id uuid UNIQUE,
    CHECK (previous_revision IS NULL OR previous_revision < revision)
);

CREATE INDEX fs2_configuration_revisions_etag_idx
    ON fs2_configuration_revisions (etag,revision DESC);

CREATE TABLE fs2_configuration_plans (
    id uuid PRIMARY KEY,
    base_revision bigint NOT NULL REFERENCES fs2_configuration_revisions(revision),
    base_etag char(64) NOT NULL CHECK (base_etag ~ '^[a-f0-9]{64}$'),
    proposed_etag char(64) NOT NULL CHECK (proposed_etag ~ '^[a-f0-9]{64}$'),
    state text NOT NULL CHECK (state IN ('valid','rejected','superseded')),
    payload jsonb NOT NULL CHECK (
        jsonb_typeof(payload)='object' AND octet_length(payload::text) BETWEEN 2 AND 16777216
    ),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    created_by text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 200),
    CHECK (expires_at > created_at)
);

CREATE INDEX fs2_configuration_plans_expiry_idx
    ON fs2_configuration_plans (expires_at,id);

CREATE TABLE fs2_configuration_reconciliation_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reconciliation_id uuid NOT NULL,
    plan_id uuid NOT NULL REFERENCES fs2_configuration_plans(id),
    phase text NOT NULL CHECK (phase IN (
        'pending','awaiting-terraform-plan-apply','rendering','applying','verifying',
        'succeeded','failed','rolled-back'
    )),
    payload jsonb NOT NULL CHECK (
        jsonb_typeof(payload)='object' AND octet_length(payload::text) BETWEEN 2 AND 16777216
    ),
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (reconciliation_id,phase)
);

CREATE INDEX fs2_configuration_reconciliation_latest_idx
    ON fs2_configuration_reconciliation_events (reconciliation_id,id DESC);

COMMENT ON TABLE fs2_configuration_revisions IS
    'Append-only desired/effective admin configuration revisions; browser requests never mutate cloud state directly';
COMMENT ON TABLE fs2_configuration_plans IS
    'Immutable validated plans with redacted Terraform handoffs and optimistic-concurrency identity';
COMMENT ON TABLE fs2_configuration_reconciliation_events IS
    'Append-only reconciliation status transitions; the highest event ID is current state';

CREATE TABLE fs2_release_identity_receipts (
    assertion_id uuid PRIMARY KEY,
    session_id text NOT NULL CHECK (length(session_id) BETWEEN 16 AND 200),
    issuer text NOT NULL CHECK (length(issuer) BETWEEN 1 AND 200),
    subject text NOT NULL CHECK (length(subject) BETWEEN 1 AND 200),
    purpose text NOT NULL CHECK (purpose IN ('operator-enrollment','admin-automation')),
    capability text NOT NULL CHECK (
        capability IN (
            'operator.enroll','tokens.issue','tokens.list','tokens.revoke','audit.read','models.bootstrap'
        )
    ),
    assertion_fingerprint char(64) NOT NULL UNIQUE CHECK (assertion_fingerprint ~ '^[a-f0-9]{64}$'),
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > issued_at),
    consumed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    consumed_by text NOT NULL CHECK (length(consumed_by) BETWEEN 1 AND 208)
);

COMMENT ON TABLE fs2_release_identity_receipts IS
    'Payload-free single-use receipts for provider-attested non-human release assertions';

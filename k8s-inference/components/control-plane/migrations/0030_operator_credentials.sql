CREATE TABLE fs2_operator_credentials (
    principal_id uuid PRIMARY KEY REFERENCES fs2_operator_principals(id),
    pepper_key_id text NOT NULL CHECK (length(pepper_key_id) BETWEEN 1 AND 64),
    digest text NOT NULL CHECK (length(digest) BETWEEN 32 AND 512),
    fingerprint char(64) NOT NULL UNIQUE CHECK (fingerprint ~ '^[a-f0-9]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 200)
);

COMMENT ON TABLE fs2_operator_credentials IS
    'Per-human Argon2id operator credential verifiers; raw credentials are disclosed once and never persisted';

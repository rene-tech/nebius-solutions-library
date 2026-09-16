-- Append-only customer-storage credential custody.  Historical generations are
-- retained for rollback and old-key reads; there is intentionally no mutable
-- current pointer and no delete/retirement path in this migration.
CREATE TABLE fs2_customer_storage_credential_generations (
    tenant_id text NOT NULL,
    principal_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    cipher_key_id text NOT NULL,
    cipher_nonce bytea NOT NULL CHECK (octet_length(cipher_nonce) = 12),
    cipher_value bytea NOT NULL,
    name_key_id text NOT NULL,
    name_digest text NOT NULL CHECK (name_digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    migration_from_generation bigint,
    PRIMARY KEY (tenant_id, principal_id, generation),
    CHECK (
        migration_from_generation IS NULL
        OR migration_from_generation < generation
    )
);

CREATE INDEX fs2_customer_storage_cipher_key_usage
    ON fs2_customer_storage_credential_generations (cipher_key_id);

CREATE INDEX fs2_customer_storage_name_key_usage
    ON fs2_customer_storage_credential_generations (name_key_id);

REVOKE ALL ON fs2_customer_storage_credential_generations FROM PUBLIC;

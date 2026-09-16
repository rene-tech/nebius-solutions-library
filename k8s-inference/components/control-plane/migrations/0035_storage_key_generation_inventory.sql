-- Persist the opaque-name key generation beside every bucket so retiring a
-- customer-storage keyring is gated by authoritative database references.
-- Historical deterministic names predate a keyed naming generation and are
-- deliberately marked as non-retirable legacy identities.
ALTER TABLE fs2_storage_buckets
    ADD COLUMN name_key_id text NOT NULL DEFAULT 'legacy-unkeyed-v0';

ALTER TABLE fs2_storage_buckets
    ALTER COLUMN name_key_id DROP DEFAULT,
    ADD CONSTRAINT fs2_storage_bucket_name_key_id CHECK (
        name_key_id = 'legacy-unkeyed-v0'
        OR name_key_id ~ '^storage-name-v[1-9][0-9]*$'
    );

CREATE INDEX fs2_storage_bucket_name_key_idx
    ON fs2_storage_buckets (name_key_id, tenant_id, owner_key);

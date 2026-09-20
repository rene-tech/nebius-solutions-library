-- Preserve historical token/operation foreign keys without rediscovering a
-- deleted event's accounts. This is not a customer-data erasure mechanism.
CREATE TABLE fs2_retired_tenants (
    tenant_id text PRIMARY KEY CHECK (length(tenant_id) BETWEEN 1 AND 120),
    retired_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    retired_by text NOT NULL,
    archive_sha256 char(64) NOT NULL CHECK (archive_sha256 ~ '^[a-f0-9]{64}$'),
    user_count integer NOT NULL CHECK (user_count >= 0),
    key_count integer NOT NULL CHECK (key_count >= 0)
);

-- Customer data belongs to a tenant (default) or an individual inference owner.
-- Buckets are retained independently of the lifetime of a Kubernetes cluster.
CREATE TABLE fs2_storage_policies (
    tenant_id text PRIMARY KEY,
    mode text NOT NULL DEFAULT 'tenant' CHECK (mode IN ('tenant', 'user', 'disabled')),
    quota_bytes bigint NOT NULL DEFAULT 5000000000 CHECK (quota_bytes > 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE fs2_storage_buckets (
    tenant_id text NOT NULL,
    owner_key text NOT NULL, -- empty for tenant mode, principal_id for user mode
    bucket_id text NOT NULL,
    bucket_name text NOT NULL UNIQUE,
    group_id text NOT NULL,
    endpoint text NOT NULL,
    region text NOT NULL,
    quota_bytes bigint NOT NULL CHECK (quota_bytes > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, owner_key)
);

CREATE TABLE fs2_user_storage (
    tenant_id text NOT NULL,
    principal_id text NOT NULL,
    owner_key text NOT NULL,
    service_account_id text NOT NULL,
    access_key_resource_id text NOT NULL,
    access_key_id text NOT NULL,
    secret_key_id text NOT NULL,
    secret_nonce bytea NOT NULL,
    secret_ciphertext bytea NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, principal_id),
    FOREIGN KEY (tenant_id, owner_key) REFERENCES fs2_storage_buckets (tenant_id, owner_key)
);

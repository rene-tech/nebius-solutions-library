-- A completion receipt is outside the customer's writable bucket: deleting an
-- example (or the bucket manifest) must not cause automatic restoration.
CREATE TABLE fs2_customer_starter_packs (
    bucket_id text NOT NULL,
    pack_version text NOT NULL CHECK (pack_version ~ '^v[1-9][0-9]*$'),
    manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[a-f0-9]{64}$'),
    state text NOT NULL CHECK (state IN ('pending','partial','complete','failed')),
    object_count integer NOT NULL DEFAULT 0 CHECK (object_count >= 0),
    total_bytes bigint NOT NULL DEFAULT 0 CHECK (total_bytes >= 0),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    error_code text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    PRIMARY KEY (bucket_id, pack_version),
    CHECK ((state = 'complete') = (completed_at IS NOT NULL))
);

-- Separate immutable storage layout from the emergency enabled switch, make
-- credential disclosure consumable, and persist every cloud-key transition.
ALTER TABLE fs2_storage_policies
    ADD COLUMN layout_mode text CHECK (layout_mode IN ('tenant', 'user')),
    ADD COLUMN enabled boolean,
    ADD COLUMN migration_state text NOT NULL DEFAULT 'ready'
        CHECK (migration_state IN ('ready', 'inventory_required', 'migrating')),
    ADD COLUMN migration_principal_count integer NOT NULL DEFAULT 0
        CHECK (migration_principal_count >= 0);

UPDATE fs2_storage_policies
SET layout_mode = CASE WHEN mode = 'tenant' THEN 'tenant' ELSE 'user' END,
    enabled = mode <> 'disabled';

ALTER TABLE fs2_storage_policies
    ALTER COLUMN layout_mode SET NOT NULL,
    ALTER COLUMN layout_mode SET DEFAULT 'user',
    ALTER COLUMN enabled SET NOT NULL,
    ALTER COLUMN enabled SET DEFAULT true,
    DROP COLUMN mode;

-- A shared tenant bucket cannot be made per-principal merely by changing the
-- new default. Inventory every historical multi-principal layout and fail it
-- closed until an operator has mapped and migrated its objects. This also
-- prevents exact-membership reconciliation from successively replacing one
-- shared principal with another.
INSERT INTO fs2_storage_policies (
    tenant_id, layout_mode, enabled, quota_bytes, migration_state,
    migration_principal_count
)
SELECT credentials.tenant_id,
       'tenant',
       false,
       COALESCE(buckets.quota_bytes, 5000000000),
       'inventory_required',
       count(*)::integer
FROM fs2_user_storage AS credentials
LEFT JOIN fs2_storage_buckets AS buckets
  ON buckets.tenant_id = credentials.tenant_id
 AND buckets.owner_key = ''
WHERE credentials.owner_key = ''
GROUP BY credentials.tenant_id, buckets.quota_bytes
HAVING count(*) > 1
ON CONFLICT (tenant_id) DO UPDATE
SET enabled = false,
    migration_state = 'inventory_required',
    migration_principal_count = EXCLUDED.migration_principal_count,
    updated_at = now();

ALTER TABLE fs2_user_storage
    ADD COLUMN expires_at timestamptz,
    ADD COLUMN desired_enabled boolean NOT NULL DEFAULT true,
    ADD COLUMN revoked_at timestamptz,
    ADD COLUMN requested_action text CHECK (requested_action IN ('enable', 'rotate', 'revoke', 'disable', 'suspend')),
    ADD COLUMN requested_at timestamptz,
    ADD COLUMN replacement_service_account_id text,
    ADD COLUMN replacement_access_key_resource_id text,
    ADD COLUMN replacement_access_key_id text,
    ADD COLUMN replacement_secret_key_id text,
    ADD COLUMN replacement_secret_nonce bytea,
    ADD COLUMN replacement_secret_ciphertext bytea,
    ADD COLUMN replacement_expires_at timestamptz,
    ADD COLUMN previous_access_key_resource_id text,
    ADD COLUMN rotation_started_at timestamptz,
    ADD COLUMN disclosure_consumed_at timestamptz,
    ADD COLUMN version bigint NOT NULL DEFAULT 0,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();

-- Historical provider keys had no expiry. This grace timestamp is only a
-- database deadline: the durable rotate action below is what replaces and
-- deactivates the provider key. A successful successor has its own bounded
-- provider expires_at and the predecessor ID is retained until deactivation.
UPDATE fs2_user_storage
SET expires_at = now() + interval '7 days',
    desired_enabled = enabled,
    requested_action = CASE WHEN enabled THEN 'rotate' ELSE 'revoke' END,
    requested_at = now(),
    rotation_started_at = CASE WHEN enabled THEN now() ELSE NULL END,
    version = version + 1
WHERE expires_at IS NULL;

ALTER TABLE fs2_user_storage ALTER COLUMN expires_at SET NOT NULL;

ALTER TABLE fs2_user_storage ADD CONSTRAINT fs2_user_storage_replacement_complete CHECK (
    (replacement_access_key_resource_id IS NULL
     AND replacement_service_account_id IS NULL
     AND replacement_access_key_id IS NULL
     AND replacement_secret_key_id IS NULL
     AND replacement_secret_nonce IS NULL
     AND replacement_secret_ciphertext IS NULL
     AND replacement_expires_at IS NULL)
    OR
    (replacement_access_key_resource_id IS NOT NULL
     AND replacement_service_account_id IS NOT NULL
     AND replacement_access_key_id IS NOT NULL
     AND replacement_secret_key_id IS NOT NULL
     AND replacement_secret_nonce IS NOT NULL
     AND replacement_secret_ciphertext IS NOT NULL
     AND replacement_expires_at IS NOT NULL
     AND requested_action = 'rotate'
     AND rotation_started_at IS NOT NULL)
);

ALTER TABLE fs2_user_storage ADD CONSTRAINT fs2_user_storage_rotation_predecessor CHECK (
    previous_access_key_resource_id IS NULL OR requested_action = 'rotate'
);

CREATE INDEX fs2_user_storage_requested_action_idx
    ON fs2_user_storage (requested_at, tenant_id, principal_id)
    WHERE requested_action IS NOT NULL OR desired_enabled <> enabled;

CREATE INDEX fs2_user_storage_expiry_idx
    ON fs2_user_storage (expires_at, tenant_id, principal_id)
    WHERE enabled AND revoked_at IS NULL;

-- The public gateway cannot select encrypted storage rows directly. This
-- owner-controlled function atomically consumes one disclosure for one exact
-- composite identity and returns only that identity's current envelope.
CREATE FUNCTION fs2_consume_user_storage_disclosure(
    p_tenant_id text,
    p_principal_id text,
    p_actor text,
    p_token_id uuid
)
RETURNS TABLE (
    owner_key text,
    access_key_id text,
    secret_key_id text,
    secret_nonce bytea,
    secret_ciphertext bytea,
    expires_at timestamptz,
    bucket_name text,
    endpoint text,
    region text
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    WITH consumed AS (
        UPDATE public.fs2_user_storage
        SET disclosure_consumed_at = clock_timestamp(),
            version = version + 1,
            updated_at = clock_timestamp()
        WHERE tenant_id = p_tenant_id
          AND principal_id = p_principal_id
          AND enabled
          AND desired_enabled
          AND revoked_at IS NULL
          AND requested_action IS NULL
          AND disclosure_consumed_at IS NULL
          AND expires_at > clock_timestamp()
          AND octet_length(p_actor) BETWEEN 1 AND 200
          AND NOT EXISTS (
              SELECT 1
              FROM public.fs2_storage_policies AS policies
              WHERE policies.tenant_id = p_tenant_id
                AND NOT policies.enabled
          )
          AND EXISTS (
              SELECT 1
              FROM public.fs2_storage_buckets AS available_bucket
              WHERE available_bucket.tenant_id = p_tenant_id
                AND available_bucket.owner_key = fs2_user_storage.owner_key
          )
        RETURNING fs2_user_storage.owner_key,
                  fs2_user_storage.access_key_id,
                  fs2_user_storage.secret_key_id,
                  fs2_user_storage.secret_nonce,
                  fs2_user_storage.secret_ciphertext,
                  fs2_user_storage.expires_at
    ), audited AS (
        INSERT INTO public.fs2_audit_events (
            actor, tenant_id, token_id, action, target_type, target_id, outcome
        )
        SELECT
            p_actor,
            p_tenant_id,
            p_token_id,
            CASE WHEN EXISTS (SELECT 1 FROM consumed)
                 THEN 'storage.credentials.disclose'
                 ELSE 'storage.credentials.disclose.replay_denied' END,
            'user_storage',
            p_principal_id,
            CASE WHEN EXISTS (SELECT 1 FROM consumed) THEN 'succeeded' ELSE 'denied' END
        WHERE octet_length(p_actor) BETWEEN 1 AND 200
        RETURNING id
    )
    SELECT consumed.owner_key,
           consumed.access_key_id,
           consumed.secret_key_id,
           consumed.secret_nonce,
           consumed.secret_ciphertext,
           consumed.expires_at,
           buckets.bucket_name,
           buckets.endpoint,
           buckets.region
    FROM consumed
    CROSS JOIN audited
    JOIN public.fs2_storage_buckets AS buckets
      ON buckets.tenant_id = p_tenant_id
     AND buckets.owner_key = consumed.owner_key;
$$;

REVOKE ALL ON FUNCTION fs2_consume_user_storage_disclosure(text, text, text, uuid) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime') THEN
        GRANT EXECUTE ON FUNCTION fs2_consume_user_storage_disclosure(text, text, text, uuid)
            TO fs2_serve_runtime;
    END IF;
END;
$$;

-- Bind tenant-layout storage to one immutable principal and make provider-key
-- ownership proof a database disclosure prerequisite.
ALTER TABLE fs2_storage_policies
    ADD COLUMN singleton_principal_id text;

-- Older default-mode provisioning could create buckets without materializing
-- a policy row. Adopt only an unambiguous layout; mixed layouts fail closed.
INSERT INTO fs2_storage_policies (
    tenant_id, layout_mode, enabled, quota_bytes, migration_state,
    migration_principal_count, singleton_principal_id
)
SELECT buckets.tenant_id,
       CASE WHEN bool_or(buckets.owner_key = '') THEN 'tenant' ELSE 'user' END,
       NOT (bool_or(buckets.owner_key = '') AND bool_or(buckets.owner_key <> '')),
       max(buckets.quota_bytes),
       CASE WHEN bool_or(buckets.owner_key = '') AND bool_or(buckets.owner_key <> '')
            THEN 'inventory_required' ELSE 'ready' END,
       count(DISTINCT credentials.principal_id)::integer,
       NULL
FROM fs2_storage_buckets AS buckets
LEFT JOIN fs2_user_storage AS credentials
  ON credentials.tenant_id = buckets.tenant_id
GROUP BY buckets.tenant_id
ON CONFLICT (tenant_id) DO NOTHING;

WITH identities AS (
    SELECT tenant_id, principal_id FROM fs2_inference_users
    UNION
    SELECT tenant_id, principal_id FROM fs2_user_storage
), tenant_counts AS (
    SELECT policy.tenant_id,
           count(DISTINCT identities.principal_id)::integer AS principal_count,
           min(identities.principal_id) AS singleton_principal_id
    FROM fs2_storage_policies AS policy
    LEFT JOIN identities ON identities.tenant_id = policy.tenant_id
    WHERE policy.layout_mode = 'tenant'
    GROUP BY policy.tenant_id
)
UPDATE fs2_storage_policies AS policy
SET singleton_principal_id = CASE WHEN counts.principal_count = 1
                                 THEN counts.singleton_principal_id ELSE NULL END,
    enabled = policy.enabled AND counts.principal_count = 1,
    migration_state = CASE WHEN policy.migration_state = 'ready' AND counts.principal_count = 1
                           THEN 'ready' ELSE 'inventory_required' END,
    migration_principal_count = counts.principal_count,
    updated_at = clock_timestamp()
FROM tenant_counts AS counts
WHERE policy.tenant_id = counts.tenant_id;

UPDATE fs2_storage_policies
SET singleton_principal_id = NULL
WHERE layout_mode = 'user';

ALTER TABLE fs2_storage_policies
    ADD CONSTRAINT fs2_storage_policy_singleton_layout CHECK (
        (layout_mode = 'user' AND singleton_principal_id IS NULL)
        OR
        (layout_mode = 'tenant' AND (NOT enabled OR singleton_principal_id IS NOT NULL))
    );

ALTER TABLE fs2_user_storage
    ADD COLUMN provider_ownership_verified boolean NOT NULL DEFAULT false,
    ADD COLUMN replacement_provider_ownership_verified boolean;

ALTER TABLE fs2_user_storage
    ADD CONSTRAINT fs2_user_storage_replacement_provider_owned CHECK (
        (replacement_access_key_resource_id IS NULL
         AND replacement_provider_ownership_verified IS NULL)
        OR
        (replacement_access_key_resource_id IS NOT NULL
         AND replacement_provider_ownership_verified IS TRUE)
    );

CREATE FUNCTION fs2_storage_singleton_user_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
DECLARE
    policy record;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        PERFORM pg_advisory_xact_lock(hashtextextended(OLD.tenant_id, 32));
        SELECT layout_mode, enabled, singleton_principal_id INTO policy
        FROM public.fs2_storage_policies
        WHERE tenant_id = OLD.tenant_id;
        IF FOUND AND policy.layout_mode = 'tenant' AND policy.enabled
           AND policy.singleton_principal_id = OLD.principal_id
           AND (TG_OP = 'DELETE'
                OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
                OR OLD.principal_id IS DISTINCT FROM NEW.principal_id) THEN
            RAISE EXCEPTION 'disable tenant storage before changing its singleton principal'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.tenant_id, 32));
    SELECT layout_mode, enabled, singleton_principal_id INTO policy
    FROM public.fs2_storage_policies
    WHERE tenant_id = NEW.tenant_id;

    IF FOUND AND policy.layout_mode = 'tenant' AND policy.enabled THEN
        IF policy.singleton_principal_id IS DISTINCT FROM NEW.principal_id THEN
            RAISE EXCEPTION 'tenant storage mode is bound to another principal'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_storage_singleton_user_guard
BEFORE INSERT OR UPDATE OR DELETE ON fs2_inference_users
FOR EACH ROW EXECUTE FUNCTION fs2_storage_singleton_user_guard();

CREATE FUNCTION fs2_storage_layout_owner_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
DECLARE
    policy record;
BEGIN
    SELECT layout_mode, singleton_principal_id INTO policy
    FROM public.fs2_storage_policies
    WHERE tenant_id = NEW.tenant_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'storage layout policy must exist before storage resources'
            USING ERRCODE = '23514';
    END IF;
    IF TG_TABLE_NAME = 'fs2_storage_buckets' THEN
        IF (policy.layout_mode = 'tenant' AND NEW.owner_key <> '')
           OR (policy.layout_mode = 'user' AND NEW.owner_key = '') THEN
            RAISE EXCEPTION 'bucket owner does not match the bound storage layout'
                USING ERRCODE = '23514';
        END IF;
    ELSE
        IF (policy.layout_mode = 'tenant'
            AND (NEW.owner_key <> '' OR policy.singleton_principal_id IS DISTINCT FROM NEW.principal_id))
           OR (policy.layout_mode = 'user'
               AND (NEW.owner_key = '' OR NEW.owner_key IS DISTINCT FROM NEW.principal_id)) THEN
            RAISE EXCEPTION 'credential owner does not match the bound storage layout'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_storage_bucket_layout_owner_guard
BEFORE INSERT OR UPDATE OF tenant_id, owner_key ON fs2_storage_buckets
FOR EACH ROW EXECUTE FUNCTION fs2_storage_layout_owner_guard();

CREATE TRIGGER fs2_user_storage_layout_owner_guard
BEFORE INSERT OR UPDATE OF tenant_id, principal_id, owner_key ON fs2_user_storage
FOR EACH ROW EXECUTE FUNCTION fs2_storage_layout_owner_guard();

-- The security-definer disclosure function updates disclosure_consumed_at.
-- Returning NULL skips that row without throwing, so its existing denial audit
-- still commits while no encrypted envelope is returned.
CREATE FUNCTION fs2_storage_verified_provider_disclosure_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF OLD.disclosure_consumed_at IS NULL
       AND NEW.disclosure_consumed_at IS NOT NULL
       AND NOT OLD.provider_ownership_verified THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_storage_verified_provider_disclosure_guard
BEFORE UPDATE OF disclosure_consumed_at ON fs2_user_storage
FOR EACH ROW EXECUTE FUNCTION fs2_storage_verified_provider_disclosure_guard();

REVOKE ALL ON FUNCTION fs2_storage_singleton_user_guard() FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_storage_layout_owner_guard() FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_storage_verified_provider_disclosure_guard() FROM PUBLIC;

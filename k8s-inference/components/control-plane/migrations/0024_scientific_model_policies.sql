-- Operator-owned dispatch policy for scientific batch models.
--
-- One row limits when the controller may start new work for a model, either
-- for every tenant (tenant_id NULL) or for one tenant. The controller consults
-- it only at the queued -> running transition, so an accepted batch stays
-- durable and queued while held, running work drains to its own terminal
-- state, and terminal result publication is unaffected. A policy never changes
-- an admitted batch, Kueue quota, node-pool bounds, or per-run resource
-- requests: it can only hold dispatch below the Terraform-owned ceilings.
CREATE TABLE fs2_scientific_model_policies (
    model_id text NOT NULL CHECK (model_id ~ '^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$' AND length(model_id) <= 63),
    tenant_id text CHECK (tenant_id IS NULL OR length(tenant_id) BETWEEN 1 AND 120),
    revision integer NOT NULL CHECK (revision >= 1),
    paused boolean NOT NULL DEFAULT false,
    max_active_runs integer CHECK (max_active_runs IS NULL OR max_active_runs BETWEEN 1 AND 64),
    reason text CHECK (reason IS NULL OR length(reason) BETWEEN 1 AND 300),
    updated_by text NOT NULL CHECK (length(updated_by) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- NULL tenant_id is the all-tenants scope; a unique expression index makes it
-- one row per (model, scope) on every supported PostgreSQL major.
CREATE UNIQUE INDEX fs2_scientific_model_policies_scope_idx
    ON fs2_scientific_model_policies(model_id, COALESCE(tenant_id, ''));

-- Active-run counting and the admin queued/running projection group by model.
CREATE INDEX fs2_scientific_batches_model_activity_idx
    ON fs2_scientific_batches(model_id, status, tenant_id)
    WHERE status IN ('queued', 'running');

CREATE FUNCTION fs2_scientific_model_policy_forward()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.model_id <> OLD.model_id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id THEN
        RAISE EXCEPTION 'scientific model policy scope is immutable';
    END IF;
    IF NEW.revision <> OLD.revision + 1 THEN
        RAISE EXCEPTION 'scientific model policy revision must advance by exactly one';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER fs2_scientific_model_policy_forward_trigger
BEFORE UPDATE ON fs2_scientific_model_policies
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_model_policy_forward();

-- The one dispatch predicate shared by the claim query, the fenced
-- queued -> running transition, and the admin projection. NULL means the
-- controller may dispatch; 'paused' and 'concurrency' name the hold. A tenant
-- row and the all-tenants row both apply; the all-tenants limit counts active
-- runs across tenants while a tenant limit counts only that tenant's runs.
CREATE FUNCTION fs2_scientific_dispatch_hold(p_model_id text, p_tenant_id text)
RETURNS text
LANGUAGE sql
STABLE
STRICT
SET search_path = pg_catalog, public
AS $function$
    SELECT CASE
        WHEN bool_or(policy.paused) THEN 'paused'
        WHEN bool_or(
            policy.max_active_runs IS NOT NULL
            AND policy.max_active_runs <= (
                SELECT count(*)
                FROM fs2_scientific_batches active
                WHERE active.model_id = p_model_id
                  AND active.status = 'running'
                  AND (policy.tenant_id IS NULL OR active.tenant_id = p_tenant_id)
            )
        ) THEN 'concurrency'
    END
    FROM fs2_scientific_model_policies policy
    WHERE policy.model_id = p_model_id
      AND (policy.tenant_id IS NULL OR policy.tenant_id = p_tenant_id)
$function$;

REVOKE ALL ON fs2_scientific_model_policies FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_model_policy_forward(),
    fs2_scientific_dispatch_hold(text, text) FROM PUBLIC;

-- The migrator's grant pass covers the configured runtime role; this versioned
-- grant keeps a database that recorded 0024 consistent on its own.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime') THEN
        GRANT SELECT, INSERT, UPDATE
            ON TABLE fs2_scientific_model_policies
            TO fs2_serve_runtime;
        GRANT EXECUTE
            ON FUNCTION fs2_scientific_dispatch_hold(text, text)
            TO fs2_serve_runtime;
    END IF;
END;
$$;

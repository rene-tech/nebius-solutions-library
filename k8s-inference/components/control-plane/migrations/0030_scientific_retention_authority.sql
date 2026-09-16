-- Retention claims fence late scientific writers while object-store deletion
-- runs outside the metadata transaction. A failed worker's lease is bounded so
-- a later maintenance pass can resume the idempotent deletion.
CREATE TABLE fs2_scientific_retention_claims (
    operation_id uuid PRIMARY KEY REFERENCES fs2_operations(id) ON DELETE CASCADE,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    claim_token uuid NOT NULL,
    retention_expired_at timestamptz NOT NULL,
    claimed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    claim_expires_at timestamptz NOT NULL,
    CHECK (claim_expires_at > claimed_at)
);

CREATE INDEX fs2_scientific_retention_claims_lease_idx
    ON fs2_scientific_retention_claims (claim_expires_at,operation_id);

-- A migration cannot know a deployment's configured maintenance role. Keep
-- deletion fail-closed until the migrator installs the exact role-bound body.
CREATE OR REPLACE FUNCTION fs2_scientific_guard_retention_delete() RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='FS202',
        MESSAGE='scientific artifact rows are deletable only by retention';
END
$function$;

-- A retention claim fences new provenance without changing the existing
-- terminal-publication and legacy-row compatibility contracts.
CREATE OR REPLACE FUNCTION fs2_scientific_assert_writable() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    current_tenant text;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.operation_id::text,727201920030));
    SELECT tenant_id INTO current_tenant
    FROM fs2_operations WHERE id=NEW.operation_id FOR SHARE;
    IF NOT FOUND OR current_tenant<>NEW.tenant_id THEN
        RAISE EXCEPTION USING ERRCODE='FS201', MESSAGE='scientific artifact scope is not current';
    END IF;
    IF EXISTS (
           SELECT 1 FROM fs2_scientific_retention_claims WHERE operation_id=NEW.operation_id
       )
       OR EXISTS (
           SELECT 1 FROM fs2_scientific_run_results WHERE operation_id=NEW.operation_id
       ) THEN
        RAISE EXCEPTION USING ERRCODE='FS203', MESSAGE='operation is terminal for scientific writes';
    END IF;
    RETURN NEW;
END
$function$;

-- Every remaining scientific writer is fenced once maintenance owns a claim.
-- Unlike fs2_scientific_assert_writable this permits normal terminal-result
-- publication, but never after object retention has started.
CREATE FUNCTION fs2_scientific_assert_not_retained() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.operation_id::text,727201920030));
    IF EXISTS (
        SELECT 1 FROM fs2_scientific_retention_claims WHERE operation_id=NEW.operation_id
    ) THEN
        RAISE EXCEPTION USING ERRCODE='FS203', MESSAGE='operation retention is already claimed';
    END IF;
    RETURN NEW;
END
$function$;

-- Runtime may test only the boolean fence; claim tokens and lease metadata stay
-- private to maintenance.
CREATE FUNCTION fs2_scientific_retention_unclaimed(p_operation_id uuid) RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    SELECT NOT EXISTS (
        SELECT 1 FROM fs2_scientific_retention_claims WHERE operation_id=p_operation_id
    )
$function$;

CREATE TRIGGER fs2_scientific_attempts_retention_fence
BEFORE UPDATE ON fs2_scientific_stage_attempts
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_assert_not_retained();
CREATE TRIGGER fs2_scientific_uploads_retention_fence
BEFORE UPDATE ON fs2_scientific_uploads
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_assert_not_retained();
CREATE TRIGGER fs2_scientific_results_retention_fence
BEFORE INSERT ON fs2_scientific_run_results
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_assert_not_retained();
CREATE TRIGGER fs2_scientific_events_retention_fence
BEFORE INSERT ON fs2_scientific_artifact_events
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_assert_not_retained();
CREATE TRIGGER fs2_scientific_batches_retention_fence
BEFORE INSERT OR UPDATE ON fs2_scientific_batches
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_assert_not_retained();
CREATE TRIGGER fs2_scientific_batch_events_retention_fence
BEFORE INSERT ON fs2_scientific_batch_events
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_assert_not_retained();
CREATE TRIGGER fs2_scientific_outbox_retention_fence
BEFORE INSERT OR UPDATE ON fs2_scientific_admission_outbox
FOR EACH ROW EXECUTE FUNCTION fs2_scientific_assert_not_retained();

REVOKE ALL ON fs2_scientific_retention_claims FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_scientific_assert_writable(),
    fs2_scientific_assert_not_retained(),fs2_scientific_retention_unclaimed(uuid),
    fs2_scientific_guard_retention_delete() FROM PUBLIC;

COMMENT ON TABLE fs2_scientific_retention_claims IS
    'Short-lived maintenance ownership fence for idempotent object and metadata retention';

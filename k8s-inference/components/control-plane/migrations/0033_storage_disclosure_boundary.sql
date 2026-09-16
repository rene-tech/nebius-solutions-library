-- Remove the runtime-callable disclosure primitive. Credential envelopes are
-- released only through short-lived, database-bound entitlements owned by the
-- isolated disclosure workload. Authoritative actor, tenant and token/session
-- identity always come from live database rows, never function arguments.
REVOKE ALL ON FUNCTION fs2_consume_user_storage_disclosure(text, text, text, uuid) FROM PUBLIC;
DROP FUNCTION fs2_consume_user_storage_disclosure(text, text, text, uuid);

ALTER TABLE fs2_user_storage
    ADD COLUMN policy_suspension_requested boolean NOT NULL DEFAULT false,
    ADD COLUMN inference_user_id uuid;

UPDATE fs2_user_storage AS credential
SET inference_user_id = owner.id
FROM fs2_inference_users AS owner
WHERE owner.tenant_id = credential.tenant_id
  AND owner.principal_id = credential.principal_id;

CREATE UNIQUE INDEX fs2_user_storage_inference_user_idx
    ON fs2_user_storage (inference_user_id)
    WHERE inference_user_id IS NOT NULL;

-- An operator request and the identity which authorized it must survive the
-- HTTP task which submitted it.  The credential row remains the fenced cloud
-- transition; this table is its immutable action/audit outbox.
CREATE TABLE fs2_storage_actions (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    principal_id text NOT NULL,
    action text NOT NULL CHECK (action IN ('rotate', 'revoke')),
    actor text NOT NULL CHECK (octet_length(actor) BETWEEN 1 AND 200),
    token_id uuid REFERENCES fs2_tokens(id),
    operator_session_id uuid REFERENCES fs2_operator_sessions(id),
    idempotency_key uuid NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'requested'
        CHECK (status IN ('requested', 'succeeded')),
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    audit_event_id bigint REFERENCES fs2_audit_events(id),
    CHECK ((token_id IS NOT NULL)::integer + (operator_session_id IS NOT NULL)::integer <= 1),
    CHECK ((status = 'requested' AND completed_at IS NULL AND audit_event_id IS NULL)
        OR (status = 'succeeded' AND completed_at IS NOT NULL AND audit_event_id IS NOT NULL))
);

CREATE INDEX fs2_storage_actions_target_idx
    ON fs2_storage_actions (tenant_id, principal_id, requested_at, id);

ALTER TABLE fs2_user_storage
    ADD COLUMN current_action_id uuid REFERENCES fs2_storage_actions(id);

-- Adopt the migration-queued historical rotations/revocations into the same
-- durable action ledger.  These are controller actions, not user assertions.
INSERT INTO fs2_storage_actions (
    id, tenant_id, principal_id, action, actor, idempotency_key
)
SELECT md5(tenant_id || chr(31) || principal_id || chr(31) || 'fs2-storage-action')::uuid,
       tenant_id,
       principal_id,
       requested_action,
       'storage-reconciler',
       md5(tenant_id || chr(31) || principal_id || chr(31) || 'fs2-storage-idempotency')::uuid
FROM fs2_user_storage
WHERE requested_action IN ('rotate', 'revoke');

UPDATE fs2_user_storage AS credential
SET current_action_id = action.id
FROM fs2_storage_actions AS action
WHERE action.tenant_id = credential.tenant_id
  AND action.principal_id = credential.principal_id
  AND action.status = 'requested'
  AND credential.requested_action = action.action;

CREATE TABLE fs2_storage_disclosure_entitlements (
    id uuid PRIMARY KEY,
    token_id uuid REFERENCES fs2_tokens(id) ON DELETE CASCADE,
    operator_session_id uuid REFERENCES fs2_operator_sessions(id) ON DELETE CASCADE,
    actor text NOT NULL CHECK (octet_length(actor) BETWEEN 1 AND 200),
    tenant_id text NOT NULL,
    principal_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    CHECK ((token_id IS NOT NULL)::integer + (operator_session_id IS NOT NULL)::integer = 1),
    CHECK (expires_at > created_at)
);

CREATE INDEX fs2_storage_disclosure_entitlements_expiry_idx
    ON fs2_storage_disclosure_entitlements (expires_at, id);

CREATE FUNCTION fs2_request_user_storage_action(
    p_tenant_id text,
    p_principal_id text,
    p_action text,
    p_token_id uuid,
    p_operator_session_id uuid,
    p_idempotency_key uuid
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    bound_actor text;
    existing public.fs2_storage_actions%ROWTYPE;
    action_id uuid := gen_random_uuid();
BEGIN
    IF p_action NOT IN ('rotate', 'revoke')
       OR (p_token_id IS NOT NULL)::integer + (p_operator_session_id IS NOT NULL)::integer <> 1 THEN
        RETURN NULL;
    END IF;

    IF p_token_id IS NOT NULL THEN
        SELECT token.principal_id
        INTO bound_actor
        FROM public.fs2_tokens AS token
        WHERE token.id = p_token_id
          AND token.tenant_id = p_tenant_id
          AND token.principal_id = p_principal_id
          AND token.revoked_at IS NULL
          AND token.rotated_at IS NULL
          AND (token.expires_at IS NULL OR token.expires_at > clock_timestamp())
          AND token.scopes @> ARRAY['storage.credentials']::text[];
    ELSE
        SELECT operator.subject
        INTO bound_actor
        FROM public.fs2_operator_sessions AS session
        JOIN public.fs2_operator_principals AS operator
          ON operator.id = session.principal_id
        JOIN public.fs2_user_storage AS target
          ON target.tenant_id = p_tenant_id
         AND target.principal_id = p_principal_id
        WHERE session.id = p_operator_session_id
          AND session.revoked_at IS NULL
          AND session.expires_at > clock_timestamp()
          AND operator.enabled
          AND operator.role = 'admin'
          AND (operator.tenant_id IS NULL OR operator.tenant_id = target.tenant_id);
    END IF;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    SELECT * INTO existing
    FROM public.fs2_storage_actions
    WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF existing.tenant_id = p_tenant_id
           AND existing.principal_id = p_principal_id
           AND existing.action = p_action
           AND existing.actor = bound_actor
           AND existing.token_id IS NOT DISTINCT FROM p_token_id
           AND existing.operator_session_id IS NOT DISTINCT FROM p_operator_session_id THEN
            RETURN existing.id;
        END IF;
        RETURN NULL;
    END IF;

    INSERT INTO public.fs2_storage_actions (
        id, tenant_id, principal_id, action, actor, token_id,
        operator_session_id, idempotency_key
    ) VALUES (
        action_id, p_tenant_id, p_principal_id, p_action, bound_actor,
        p_token_id, p_operator_session_id, p_idempotency_key
    );

    UPDATE public.fs2_user_storage
    SET requested_action = p_action,
        requested_at = clock_timestamp(),
        desired_enabled = p_action = 'rotate',
        rotation_started_at = CASE WHEN p_action = 'rotate'
                                   THEN clock_timestamp() ELSE rotation_started_at END,
        current_action_id = action_id,
        version = version + 1,
        updated_at = clock_timestamp()
    WHERE tenant_id = p_tenant_id
      AND principal_id = p_principal_id
      AND requested_action IS NULL
      AND current_action_id IS NULL;
    IF NOT FOUND THEN
        DELETE FROM public.fs2_storage_actions WHERE id = action_id;
        RETURN NULL;
    END IF;
    RETURN action_id;
END
$function$;

CREATE FUNCTION fs2_storage_action_status(p_action_id uuid)
RETURNS text
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    SELECT action.status FROM public.fs2_storage_actions AS action
    WHERE action.id = p_action_id;
$function$;

CREATE FUNCTION fs2_begin_user_storage_disclosure(
    p_bearer text,
    p_entitlement_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    bound_token record;
BEGIN
    DELETE FROM public.fs2_storage_disclosure_entitlements
    WHERE expires_at < clock_timestamp() - interval '1 hour';
    IF p_bearer IS NULL
       OR octet_length(p_bearer) > 256
       OR p_bearer !~ '^fs2_pat_[0-9a-f]{32}_[A-Za-z0-9_-]{32,}$' THEN
        RETURN false;
    END IF;

    SELECT token.id, token.principal_id, token.tenant_id
    INTO bound_token
    FROM public.fs2_tokens AS token
    WHERE token.fingerprint = encode(sha256(convert_to(p_bearer, 'UTF8')), 'hex')
      AND token.revoked_at IS NULL
      AND token.rotated_at IS NULL
      AND (token.expires_at IS NULL OR token.expires_at > clock_timestamp())
      AND token.scopes @> ARRAY['storage.credentials']::text[]
      AND NOT EXISTS (
          SELECT 1
          FROM public.fs2_inference_users AS owner
          WHERE owner.tenant_id = token.tenant_id
            AND owner.principal_id = token.principal_id
            AND NOT owner.enabled
      );
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    INSERT INTO public.fs2_storage_disclosure_entitlements (
        id, token_id, actor, tenant_id, principal_id, expires_at
    ) VALUES (
        p_entitlement_id,
        bound_token.id,
        bound_token.principal_id,
        bound_token.tenant_id,
        bound_token.principal_id,
        clock_timestamp() + interval '30 seconds'
    );
    RETURN true;
EXCEPTION
    WHEN unique_violation THEN RETURN false;
END
$function$;

CREATE FUNCTION fs2_begin_admin_storage_disclosure(
    p_session_id uuid,
    p_verified_session_digest text,
    p_user_id uuid,
    p_entitlement_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    binding record;
BEGIN
    DELETE FROM public.fs2_storage_disclosure_entitlements
    WHERE expires_at < clock_timestamp() - interval '1 hour';
    IF p_verified_session_digest IS NULL
       OR p_verified_session_digest !~ '^[a-f0-9]{64}$' THEN
        RETURN false;
    END IF;

    SELECT session.id AS session_id,
           operator.subject AS actor,
           credential.tenant_id,
           credential.principal_id
    INTO binding
    FROM public.fs2_operator_sessions AS session
    JOIN public.fs2_operator_principals AS operator
      ON operator.id = session.principal_id
    JOIN public.fs2_user_storage AS credential
      ON credential.inference_user_id = p_user_id
    LEFT JOIN public.fs2_inference_users AS owner
      ON owner.tenant_id = credential.tenant_id
     AND owner.principal_id = credential.principal_id
    WHERE session.id = p_session_id
      AND session.digest = p_verified_session_digest
      AND session.revoked_at IS NULL
      AND session.expires_at > clock_timestamp()
      AND operator.enabled
      AND operator.role = 'admin'
      AND (operator.tenant_id IS NULL OR operator.tenant_id = credential.tenant_id)
      AND COALESCE(owner.enabled, true);
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    INSERT INTO public.fs2_storage_disclosure_entitlements (
        id, operator_session_id, actor, tenant_id, principal_id, expires_at
    ) VALUES (
        p_entitlement_id,
        binding.session_id,
        binding.actor,
        binding.tenant_id,
        binding.principal_id,
        clock_timestamp() + interval '30 seconds'
    );
    RETURN true;
EXCEPTION
    WHEN unique_violation THEN RETURN false;
END
$function$;

CREATE FUNCTION fs2_consume_storage_disclosure(p_entitlement_id uuid)
RETURNS TABLE (
    tenant_id text,
    principal_id text,
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
AS $function$
    WITH entitlement AS MATERIALIZED (
        SELECT *
        FROM public.fs2_storage_disclosure_entitlements
        WHERE id = p_entitlement_id
        FOR UPDATE
    ), authorized AS (
        SELECT entitlement.*
        FROM entitlement
        WHERE entitlement.consumed_at IS NULL
          AND entitlement.expires_at > clock_timestamp()
          AND (
            (
              entitlement.token_id IS NOT NULL
              AND EXISTS (
                SELECT 1
                FROM public.fs2_tokens AS token
                WHERE token.id = entitlement.token_id
                  AND token.tenant_id = entitlement.tenant_id
                  AND token.principal_id = entitlement.principal_id
                  AND token.revoked_at IS NULL
                  AND token.rotated_at IS NULL
                  AND (token.expires_at IS NULL OR token.expires_at > clock_timestamp())
                  AND token.scopes @> ARRAY['storage.credentials']::text[]
              )
              AND NOT EXISTS (
                SELECT 1
                FROM public.fs2_inference_users AS owner
                WHERE owner.tenant_id = entitlement.tenant_id
                  AND owner.principal_id = entitlement.principal_id
                  AND NOT owner.enabled
              )
            )
            OR
            (
              entitlement.operator_session_id IS NOT NULL
              AND EXISTS (
                SELECT 1
                FROM public.fs2_operator_sessions AS session
                JOIN public.fs2_operator_principals AS operator
                  ON operator.id = session.principal_id
                JOIN public.fs2_inference_users AS owner
                  ON owner.tenant_id = entitlement.tenant_id
                 AND owner.principal_id = entitlement.principal_id
                WHERE session.id = entitlement.operator_session_id
                  AND session.revoked_at IS NULL
                  AND session.expires_at > clock_timestamp()
                  AND operator.enabled
                  AND operator.role = 'admin'
                  AND operator.subject = entitlement.actor
                  AND (operator.tenant_id IS NULL OR operator.tenant_id = owner.tenant_id)
                  AND owner.enabled
              )
            )
          )
    ), consumed AS (
        UPDATE public.fs2_user_storage AS credential
        SET disclosure_consumed_at = clock_timestamp(),
            version = credential.version + 1,
            updated_at = clock_timestamp()
        FROM authorized
        WHERE credential.tenant_id = authorized.tenant_id
          AND credential.principal_id = authorized.principal_id
          AND credential.enabled
          AND credential.desired_enabled
          AND credential.revoked_at IS NULL
          AND credential.requested_action IS NULL
          AND NOT credential.policy_suspension_requested
          AND credential.disclosure_consumed_at IS NULL
          AND credential.expires_at > clock_timestamp()
          AND NOT EXISTS (
              SELECT 1
              FROM public.fs2_storage_policies AS policy
              WHERE policy.tenant_id = authorized.tenant_id
                AND NOT policy.enabled
          )
        RETURNING authorized.id AS entitlement_id,
                  authorized.token_id,
                  authorized.actor,
                  authorized.tenant_id,
                  authorized.principal_id,
                  credential.owner_key,
                  credential.access_key_id,
                  credential.secret_key_id,
                  credential.secret_nonce,
                  credential.secret_ciphertext,
                  credential.expires_at
    ), marked AS (
        UPDATE public.fs2_storage_disclosure_entitlements AS marker
        SET consumed_at = clock_timestamp()
        FROM consumed
        WHERE marker.id = consumed.entitlement_id
        RETURNING marker.id
    ), audited AS (
        INSERT INTO public.fs2_audit_events (
            actor, tenant_id, token_id, action, target_type, target_id, outcome
        )
        SELECT entitlement.actor,
               entitlement.tenant_id,
               entitlement.token_id,
               CASE WHEN EXISTS (SELECT 1 FROM marked)
                    THEN 'storage.credentials.disclose'
                    ELSE 'storage.credentials.disclose.replay_denied' END,
               'user_storage',
               entitlement.principal_id,
               CASE WHEN EXISTS (SELECT 1 FROM marked) THEN 'succeeded' ELSE 'denied' END
        FROM entitlement
        RETURNING id
    )
    SELECT consumed.tenant_id,
           consumed.principal_id,
           consumed.owner_key,
           consumed.access_key_id,
           consumed.secret_key_id,
           consumed.secret_nonce,
           consumed.secret_ciphertext,
           consumed.expires_at,
           bucket.bucket_name,
           bucket.endpoint,
           bucket.region
    FROM consumed
    JOIN public.fs2_storage_buckets AS bucket
      ON bucket.tenant_id = consumed.tenant_id
     AND bucket.owner_key = consumed.owner_key
    CROSS JOIN audited;
$function$;

REVOKE ALL ON TABLE fs2_storage_disclosure_entitlements FROM PUBLIC;
REVOKE ALL ON TABLE fs2_storage_actions FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_request_user_storage_action(text, text, text, uuid, uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_storage_action_status(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_begin_user_storage_disclosure(text, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_begin_admin_storage_disclosure(uuid, text, uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_consume_storage_disclosure(uuid) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime') THEN
        GRANT EXECUTE ON FUNCTION fs2_request_user_storage_action(text, text, text, uuid, uuid, uuid)
            TO fs2_serve_runtime;
        GRANT EXECUTE ON FUNCTION fs2_storage_action_status(uuid)
            TO fs2_serve_runtime;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_storage_disclosure') THEN
        GRANT EXECUTE ON FUNCTION fs2_begin_user_storage_disclosure(text, uuid)
            TO fs2_serve_storage_disclosure;
        GRANT EXECUTE ON FUNCTION fs2_begin_admin_storage_disclosure(uuid, text, uuid, uuid)
            TO fs2_serve_storage_disclosure;
        GRANT EXECUTE ON FUNCTION fs2_consume_storage_disclosure(uuid)
            TO fs2_serve_storage_disclosure;
    END IF;
END;
$$;

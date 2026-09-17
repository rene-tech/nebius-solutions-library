-- Durable provider-operation and reconciler-drain custody.
--
-- A process/session lock is not evidence that a remote cloud mutation ended:
-- connection loss releases the lock while the provider operation may still be
-- executing.  These append-only rows survive Pod/process loss and make an
-- indeterminate provider result a hard cutover blocker.
CREATE TABLE fs2_storage_provider_operations (
    id uuid PRIMARY KEY,
    reconciler_generation text NOT NULL
        CHECK (reconciler_generation ~ '^r[0-9]{14}-[a-f0-9]{12}$'),
    activation_epoch bigint NOT NULL CHECK (activation_epoch > 0),
    activation_state_head_sha256 char(64) NOT NULL
        CHECK (activation_state_head_sha256 ~ '^[a-f0-9]{64}$'),
    transition_id char(64) NOT NULL CHECK (transition_id ~ '^[a-f0-9]{64}$'),
    tenant_id text NOT NULL,
    principal_id text NOT NULL,
    operation_kind text NOT NULL CHECK (octet_length(operation_kind) BETWEEN 1 AND 80),
    target_identity_sha256 char(64) NOT NULL
        CHECK (target_identity_sha256 ~ '^[a-f0-9]{64}$'),
    provider_idempotency_id uuid NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'admitted'
        CHECK (status IN ('admitted','running','succeeded','failed_terminal','indeterminate','superseded')),
    superseded_by uuid REFERENCES fs2_storage_provider_operations(id),
    admitted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    terminal_at timestamptz,
    CHECK (
        (status IN ('succeeded','failed_terminal','superseded') AND terminal_at IS NOT NULL)
        OR
        (status IN ('admitted','running','indeterminate') AND terminal_at IS NULL)
    )
);

CREATE TABLE fs2_storage_provider_operation_attempts (
    operation_id uuid NOT NULL REFERENCES fs2_storage_provider_operations(id),
    provider_operation_id text NOT NULL CHECK (octet_length(provider_operation_id) BETWEEN 1 AND 256),
    status text NOT NULL
        CHECK (status IN ('submitted','succeeded','failed_terminal','superseded_indeterminate')),
    provider_code text CHECK (provider_code IS NULL OR octet_length(provider_code) BETWEEN 1 AND 80),
    submitted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    terminal_at timestamptz,
    PRIMARY KEY (operation_id, provider_operation_id),
    CHECK (
        (status IN ('succeeded','failed_terminal','superseded_indeterminate') AND terminal_at IS NOT NULL)
        OR (status = 'submitted' AND terminal_at IS NULL)
    )
);

CREATE TABLE fs2_storage_reconciler_drains (
    drain_id uuid PRIMARY KEY,
    reconciler_generation text NOT NULL
        CHECK (reconciler_generation ~ '^r[0-9]{14}-[a-f0-9]{12}$'),
    activation_epoch bigint NOT NULL CHECK (activation_epoch > 0),
    activation_state_head_sha256 char(64) NOT NULL
        CHECK (activation_state_head_sha256 ~ '^[a-f0-9]{64}$'),
    transition_id char(64) NOT NULL CHECK (transition_id ~ '^[a-f0-9]{64}$'),
    requested_at timestamptz NOT NULL,
    deadline_at timestamptz NOT NULL,
    operation_cutoff_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    status text NOT NULL DEFAULT 'draining' CHECK (status IN ('draining','drained')),
    receipt jsonb,
    receipt_sha256 char(64),
    completed_at timestamptz,
    UNIQUE (reconciler_generation, activation_epoch),
    CHECK (deadline_at > requested_at),
    CHECK (
        (status = 'draining' AND receipt IS NULL AND receipt_sha256 IS NULL AND completed_at IS NULL)
        OR
        (status = 'drained' AND receipt IS NOT NULL
         AND receipt_sha256 ~ '^[a-f0-9]{64}$' AND completed_at IS NOT NULL)
    )
);

CREATE INDEX fs2_storage_provider_operations_nonterminal_idx
    ON fs2_storage_provider_operations (reconciler_generation, admitted_at, id)
    WHERE terminal_at IS NULL;

CREATE FUNCTION fs2_begin_storage_provider_operation(
    p_id uuid,
    p_reconciler_generation text,
    p_activation_epoch bigint,
    p_activation_state_head_sha256 char(64),
    p_transition_id char(64),
    p_tenant_id text,
    p_principal_id text,
    p_operation_kind text,
    p_target_identity_sha256 char(64),
    p_provider_idempotency_id uuid
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    existing public.fs2_storage_provider_operations%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'fs2-customer-storage-provider-operation-admission', 35));
    IF EXISTS (
        SELECT 1 FROM public.fs2_storage_reconciler_drains
        WHERE reconciler_generation = p_reconciler_generation
          AND activation_epoch >= p_activation_epoch
    ) THEN
        RETURN NULL;
    END IF;
    SELECT * INTO existing FROM public.fs2_storage_provider_operations
    WHERE provider_idempotency_id = p_provider_idempotency_id;
    IF FOUND THEN
        IF existing.id = p_id
           AND existing.reconciler_generation = p_reconciler_generation
           AND existing.activation_epoch = p_activation_epoch
           AND existing.activation_state_head_sha256 = p_activation_state_head_sha256
           AND existing.transition_id = p_transition_id
           AND existing.tenant_id = p_tenant_id
           AND existing.principal_id = p_principal_id
           AND existing.operation_kind = p_operation_kind
           AND existing.target_identity_sha256 = p_target_identity_sha256 THEN
            RETURN existing.id;
        END IF;
        RETURN NULL;
    END IF;
    INSERT INTO public.fs2_storage_provider_operations (
        id,reconciler_generation,activation_epoch,activation_state_head_sha256,
        transition_id,tenant_id,principal_id,operation_kind,target_identity_sha256,
        provider_idempotency_id
    ) VALUES (
        p_id,p_reconciler_generation,p_activation_epoch,p_activation_state_head_sha256,
        p_transition_id,p_tenant_id,p_principal_id,p_operation_kind,
        p_target_identity_sha256,p_provider_idempotency_id
    );
    RETURN p_id;
END
$function$;

CREATE FUNCTION fs2_record_storage_provider_submission(
    p_operation_id uuid,
    p_provider_operation_id text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    INSERT INTO public.fs2_storage_provider_operation_attempts (
        operation_id,provider_operation_id,status
    ) VALUES (p_operation_id,p_provider_operation_id,'submitted')
    ON CONFLICT (operation_id,provider_operation_id) DO NOTHING;
    UPDATE public.fs2_storage_provider_operations
    SET status='running'
    WHERE id=p_operation_id AND status='admitted';
    RETURN FOUND OR EXISTS (
        SELECT 1 FROM public.fs2_storage_provider_operation_attempts
        WHERE operation_id=p_operation_id AND provider_operation_id=p_provider_operation_id
    );
END
$function$;

CREATE FUNCTION fs2_record_storage_provider_terminal(
    p_operation_id uuid,
    p_provider_operation_id text,
    p_succeeded boolean,
    p_provider_code text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    UPDATE public.fs2_storage_provider_operation_attempts
    SET status=CASE WHEN p_succeeded THEN 'succeeded' ELSE 'failed_terminal' END,
        provider_code=p_provider_code,
        terminal_at=clock_timestamp()
    WHERE operation_id=p_operation_id
      AND provider_operation_id=p_provider_operation_id
      AND status='submitted';
    RETURN FOUND OR EXISTS (
        SELECT 1 FROM public.fs2_storage_provider_operation_attempts
        WHERE operation_id=p_operation_id
          AND provider_operation_id=p_provider_operation_id
          AND status=CASE WHEN p_succeeded THEN 'succeeded' ELSE 'failed_terminal' END
    );
END
$function$;

CREATE FUNCTION fs2_finish_storage_provider_operation(
    p_operation_id uuid,
    p_succeeded boolean
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    current_operation public.fs2_storage_provider_operations%ROWTYPE;
    has_nonterminal boolean;
    has_failed boolean;
BEGIN
    SELECT * INTO current_operation
    FROM public.fs2_storage_provider_operations
    WHERE id=p_operation_id FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT EXISTS(
        SELECT 1 FROM public.fs2_storage_provider_operation_attempts
        WHERE operation_id=p_operation_id AND terminal_at IS NULL
    ), EXISTS(
        SELECT 1 FROM public.fs2_storage_provider_operation_attempts
        WHERE operation_id=p_operation_id AND status='failed_terminal'
    ) INTO has_nonterminal,has_failed;
    UPDATE public.fs2_storage_provider_operations
    SET status=CASE
            WHEN has_nonterminal THEN 'indeterminate'
            WHEN p_succeeded AND NOT has_failed THEN 'succeeded'
            ELSE 'failed_terminal'
        END,
        terminal_at=CASE WHEN has_nonterminal THEN NULL ELSE clock_timestamp() END
    WHERE id=p_operation_id AND terminal_at IS NULL;
    IF NOT FOUND THEN
        RETURN current_operation.status = CASE
            WHEN p_succeeded THEN 'succeeded' ELSE 'failed_terminal' END;
    END IF;
    IF p_succeeded AND NOT has_nonterminal AND NOT has_failed THEN
        -- A complete idempotent retry, including its caller's durable DB
        -- postcondition, closes an older process-loss window for the same exact
        -- generation/kind/target.  This includes a crash after admission or
        -- provider submission but before the predecessor could persist its
        -- uncertainty.  The prior rows and provider IDs remain terminal
        -- evidence; no unresolved attempt is silently discarded.
        WITH closed AS (
            UPDATE public.fs2_storage_provider_operations
            SET status='superseded',terminal_at=clock_timestamp(),superseded_by=p_operation_id
            WHERE id<>p_operation_id
              AND reconciler_generation=current_operation.reconciler_generation
              AND tenant_id=current_operation.tenant_id
              AND principal_id=current_operation.principal_id
              AND operation_kind=current_operation.operation_kind
              AND target_identity_sha256=current_operation.target_identity_sha256
              AND status IN ('admitted','running','indeterminate')
              AND terminal_at IS NULL
            RETURNING id
        )
        UPDATE public.fs2_storage_provider_operation_attempts
        SET status='superseded_indeterminate',
            provider_code='EXACT_RETRY_SUPERSEDED',
            terminal_at=clock_timestamp()
        WHERE operation_id IN (SELECT id FROM closed) AND status='submitted';
    END IF;
    RETURN true;
END
$function$;

CREATE FUNCTION fs2_mark_storage_provider_operation_indeterminate(p_operation_id uuid)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    UPDATE public.fs2_storage_provider_operations
    SET status='indeterminate',terminal_at=NULL
    WHERE id=p_operation_id AND terminal_at IS NULL;
    RETURN FOUND OR EXISTS (
        SELECT 1 FROM public.fs2_storage_provider_operations
        WHERE id=p_operation_id AND status='indeterminate' AND terminal_at IS NULL
    );
END
$function$;

CREATE FUNCTION fs2_begin_storage_reconciler_drain(
    p_drain_id uuid,
    p_reconciler_generation text,
    p_activation_epoch bigint,
    p_activation_state_head_sha256 char(64),
    p_transition_id char(64),
    p_requested_at timestamptz,
    p_deadline_at timestamptz
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    existing public.fs2_storage_reconciler_drains%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'fs2-customer-storage-provider-operation-admission', 35));
    SELECT * INTO existing FROM public.fs2_storage_reconciler_drains
    WHERE reconciler_generation=p_reconciler_generation
      AND activation_epoch=p_activation_epoch;
    IF FOUND THEN
        RETURN existing.drain_id=p_drain_id
           AND existing.activation_state_head_sha256=p_activation_state_head_sha256
           AND existing.transition_id=p_transition_id
           AND existing.requested_at=p_requested_at
           AND existing.deadline_at=p_deadline_at;
    END IF;
    INSERT INTO public.fs2_storage_reconciler_drains (
        drain_id,reconciler_generation,activation_epoch,activation_state_head_sha256,
        transition_id,requested_at,deadline_at
    ) VALUES (
        p_drain_id,p_reconciler_generation,p_activation_epoch,p_activation_state_head_sha256,
        p_transition_id,p_requested_at,p_deadline_at
    );
    RETURN true;
END
$function$;

CREATE FUNCTION fs2_complete_storage_reconciler_drain(p_drain_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    drain public.fs2_storage_reconciler_drains%ROWTYPE;
    operations jsonb;
    queued_user_actions bigint;
    queued_audit_actions bigint;
    receipt_body jsonb;
    receipt_hash text;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'fs2-customer-storage-provider-operation-admission', 35));
    SELECT * INTO drain FROM public.fs2_storage_reconciler_drains
    WHERE drain_id=p_drain_id FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF drain.status='drained' THEN RETURN drain.receipt; END IF;
    IF EXISTS (
        SELECT 1 FROM public.fs2_storage_provider_operations
        WHERE reconciler_generation=drain.reconciler_generation
          AND admitted_at <= drain.operation_cutoff_at
          AND terminal_at IS NULL
    ) THEN
        RETURN NULL;
    END IF;
    -- Requested-but-unstarted actions are durable successor work, not inflight
    -- provider effects. Count them in the receipt while admission remains shut;
    -- requiring them to disappear here would deadlock the drained predecessor.
    SELECT count(*) INTO queued_user_actions
    FROM public.fs2_user_storage
    WHERE requested_action IS NOT NULL
       OR desired_enabled IS DISTINCT FROM enabled
       OR policy_suspension_requested;
    SELECT count(*) INTO queued_audit_actions
    FROM public.fs2_storage_actions WHERE status='requested';
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'operation_id',operation.id,
        'provider_idempotency_id',operation.provider_idempotency_id,
        'operation_kind',operation.operation_kind,
        'target_identity_sha256',operation.target_identity_sha256,
        'status',operation.status,
        'superseded_by',operation.superseded_by,
        'provider_operations',COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'provider_operation_id',attempt.provider_operation_id,
                'status',attempt.status,
                'provider_code',attempt.provider_code
            ) ORDER BY attempt.provider_operation_id)
            FROM public.fs2_storage_provider_operation_attempts AS attempt
            WHERE attempt.operation_id=operation.id
        ),'[]'::jsonb)
    ) ORDER BY operation.id),'[]'::jsonb)
    INTO operations
    FROM public.fs2_storage_provider_operations AS operation
    WHERE operation.reconciler_generation=drain.reconciler_generation
      AND operation.admitted_at <= drain.operation_cutoff_at;
    receipt_body := jsonb_build_object(
        'schema','fs2-serve.nebius.ai/storage-reconciler-provider-drain-receipt/v1',
        'drain_id',drain.drain_id,
        'reconciler_generation',drain.reconciler_generation,
        'activation_epoch',drain.activation_epoch,
        'activation_state_head_sha256',drain.activation_state_head_sha256,
        'transition_id',drain.transition_id,
        'operation_cutoff_at',drain.operation_cutoff_at,
        'nonterminal_provider_operations',0,
        'queued_user_actions',queued_user_actions,
        'queued_audit_actions',queued_audit_actions,
        'operations',operations
    );
    receipt_hash := encode(sha256(convert_to(receipt_body::text,'UTF8')),'hex');
    receipt_body := receipt_body || jsonb_build_object(
        'postgres_jsonb_receipt_sha256',receipt_hash);
    UPDATE public.fs2_storage_reconciler_drains
    SET status='drained',receipt=receipt_body,receipt_sha256=receipt_hash,
        completed_at=clock_timestamp()
    WHERE drain_id=p_drain_id AND status='draining';
    RETURN receipt_body;
END
$function$;

CREATE FUNCTION fs2_storage_reconciler_drain_receipt(p_drain_id uuid)
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    SELECT receipt FROM public.fs2_storage_reconciler_drains
    WHERE drain_id=p_drain_id AND status='drained';
$function$;

REVOKE ALL ON TABLE fs2_storage_provider_operations FROM PUBLIC;
REVOKE ALL ON TABLE fs2_storage_provider_operation_attempts FROM PUBLIC;
REVOKE ALL ON TABLE fs2_storage_reconciler_drains FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_begin_storage_provider_operation(
    uuid,text,bigint,char(64),char(64),text,text,text,char(64),uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_record_storage_provider_submission(uuid,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_record_storage_provider_terminal(uuid,text,boolean,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_finish_storage_provider_operation(uuid,boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_mark_storage_provider_operation_indeterminate(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_begin_storage_reconciler_drain(
    uuid,text,bigint,char(64),char(64),timestamptz,timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_complete_storage_reconciler_drain(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_storage_reconciler_drain_receipt(uuid) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fs2_serve_storage') THEN
        GRANT EXECUTE ON FUNCTION fs2_begin_storage_provider_operation(
            uuid,text,bigint,char(64),char(64),text,text,text,char(64),uuid)
            TO fs2_serve_storage;
        GRANT EXECUTE ON FUNCTION fs2_record_storage_provider_submission(uuid,text)
            TO fs2_serve_storage;
        GRANT EXECUTE ON FUNCTION fs2_record_storage_provider_terminal(uuid,text,boolean,text)
            TO fs2_serve_storage;
        GRANT EXECUTE ON FUNCTION fs2_finish_storage_provider_operation(uuid,boolean)
            TO fs2_serve_storage;
        GRANT EXECUTE ON FUNCTION fs2_mark_storage_provider_operation_indeterminate(uuid)
            TO fs2_serve_storage;
        GRANT EXECUTE ON FUNCTION fs2_begin_storage_reconciler_drain(
            uuid,text,bigint,char(64),char(64),timestamptz,timestamptz)
            TO fs2_serve_storage;
        GRANT EXECUTE ON FUNCTION fs2_complete_storage_reconciler_drain(uuid)
            TO fs2_serve_storage;
        GRANT EXECUTE ON FUNCTION fs2_storage_reconciler_drain_receipt(uuid)
            TO fs2_serve_storage;
    END IF;
END;
$$;

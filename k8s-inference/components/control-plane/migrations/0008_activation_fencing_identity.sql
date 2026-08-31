-- Additive hardening for activation ownership. 0005-0007 are immutable.

ALTER TABLE fs2_activation_intents
    ADD COLUMN model_fencing_token bigint CHECK (
        model_fencing_token IS NULL OR model_fencing_token > 0
    ),
    ADD COLUMN leadership_fencing_token bigint CHECK (
        leadership_fencing_token IS NULL OR leadership_fencing_token > 0
    );

ALTER TABLE fs2_activation_target_state
    ADD COLUMN model_fencing_token bigint;

UPDATE fs2_activation_target_state
SET model_fencing_token=controller_fencing_token
WHERE model_fencing_token IS NULL;

ALTER TABLE fs2_activation_target_state
    ALTER COLUMN model_fencing_token SET NOT NULL,
    ADD CONSTRAINT fs2_activation_target_model_fence_positive
        CHECK (model_fencing_token > 0);

CREATE TABLE fs2_activation_model_fences (
    model_id text PRIMARY KEY,
    last_issued_fence bigint NOT NULL CHECK (last_issued_fence > 0)
);

INSERT INTO fs2_activation_model_fences(model_id,last_issued_fence)
SELECT model_id,model_fencing_token FROM fs2_activation_target_state
ON CONFLICT (model_id) DO UPDATE SET
    last_issued_fence=GREATEST(
        fs2_activation_model_fences.last_issued_fence,
        EXCLUDED.last_issued_fence
    );

-- No activation route has been admitted in the release lineage, but preserve
-- deterministic readability if an operator rehearsed the pre-0008 schema.
-- The former schema had one leadership fence only; bind matching terminal
-- observations to that durable target fence without inventing a newer claim.
UPDATE fs2_activation_intents AS intent
SET model_fencing_token=target.model_fencing_token,
    leadership_fencing_token=target.controller_fencing_token
FROM fs2_activation_target_state AS target
WHERE intent.status='ready'
  AND intent.model_id=target.model_id
  AND intent.target_uid=target.target_uid
  AND intent.target_resource_version=target.resource_version
  AND intent.target_observed_generation=target.observed_generation
  AND intent.target_template_digest=target.template_digest
  AND intent.target_active=target.active;

-- Heartbeats are leases, not release evidence. Drop any pre-upgrade lease and
-- require the next leader to publish the complete observed Kubernetes identity.
DELETE FROM fs2_activation_controller_status;

ALTER TABLE fs2_activation_controller_status
    ADD COLUMN pod_namespace text NOT NULL,
    ADD COLUMN pod_name text NOT NULL,
    ADD COLUMN pod_uid text NOT NULL,
    ADD COLUMN service_account_name text NOT NULL,
    ADD COLUMN service_account_uid text NOT NULL,
    ADD COLUMN lease_namespace text NOT NULL,
    ADD COLUMN lease_name text NOT NULL,
    ADD COLUMN lease_uid text NOT NULL,
    ADD COLUMN lease_resource_version text NOT NULL,
    ADD COLUMN lease_holder_identity text NOT NULL,
    ADD CONSTRAINT fs2_activation_status_controller_identity
        CHECK (controller_id = pod_namespace || '/' || pod_name || ':' || pod_uid),
    ADD CONSTRAINT fs2_activation_status_lease_holder
        CHECK (lease_holder_identity = 'fs2:' || pod_uid),
    ADD CONSTRAINT fs2_activation_status_identity_bounds CHECK (
        char_length(pod_namespace) BETWEEN 1 AND 63
        AND char_length(pod_name) BETWEEN 1 AND 253
        AND char_length(pod_uid) BETWEEN 1 AND 128
        AND char_length(service_account_name) BETWEEN 1 AND 253
        AND char_length(service_account_uid) BETWEEN 1 AND 128
        AND char_length(lease_namespace) BETWEEN 1 AND 63
        AND char_length(lease_name) BETWEEN 1 AND 253
        AND char_length(lease_uid) BETWEEN 1 AND 128
        AND char_length(lease_resource_version) BETWEEN 1 AND 128
        AND lease_resource_version ~ '^[1-9][0-9]*$'
    );

CREATE OR REPLACE FUNCTION fs2_activation_model_lock_key(p_model_id text)
RETURNS bigint
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
SET search_path = pg_catalog, public
AS $function$
    SELECT hashtextextended('fs2-activation-model' || chr(31) || p_model_id, 0)
$function$;

CREATE OR REPLACE FUNCTION fs2_runtime_ensure_activation_intent(
    p_operation_id uuid,
    p_operation_attempt integer,
    p_model_id text,
    p_model_revision text,
    p_binding_digest char(64),
    p_deadline_at timestamptz,
    p_max_attempts integer,
    p_worker_id text,
    p_operation_fencing_token bigint
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    current_operation record;
    current_intent record;
    result_id uuid;
BEGIN
    IF p_operation_attempt < 0 OR p_operation_attempt > 10
       OR p_max_attempts < 1 OR p_max_attempts > 10
       OR p_binding_digest !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'activation_intent_input_invalid' USING ERRCODE='22023';
    END IF;

    PERFORM pg_advisory_xact_lock(fs2_activation_model_lock_key(p_model_id));
    SELECT id,model_id,model_revision,attempt,max_attempts,deadline_at
      INTO current_operation
      FROM fs2_operations
     WHERE id=p_operation_id AND worker_id=p_worker_id
       AND fencing_token=p_operation_fencing_token AND status='activating'
       AND lease_expires_at>clock_timestamp()
       AND (deadline_at IS NULL OR deadline_at>clock_timestamp())
     FOR UPDATE;
    IF NOT FOUND
       OR current_operation.model_id<>p_model_id
       OR current_operation.model_revision<>p_model_revision
       OR current_operation.attempt<>p_operation_attempt
       OR current_operation.max_attempts<>p_max_attempts
       OR current_operation.deadline_at IS DISTINCT FROM p_deadline_at THEN
        RAISE EXCEPTION 'activation_operation_stale' USING ERRCODE='P0001';
    END IF;

    UPDATE fs2_activation_intents
       SET status='expired',controller_id=NULL,heartbeat_at=NULL,
           lease_expires_at=NULL,fencing_token=fencing_token+1,
           error_code='new_demand'
     WHERE model_id=p_model_id AND action='deactivate'
       AND status IN ('queued','claimed');

    SELECT * INTO current_intent
      FROM fs2_activation_intents
     WHERE operation_id=p_operation_id
     FOR UPDATE;
    IF FOUND THEN
        IF current_intent.model_id<>p_model_id
           OR current_intent.model_revision<>p_model_revision
           OR current_intent.binding_digest<>p_binding_digest
           OR current_intent.action<>'activate' THEN
            RAISE EXCEPTION 'activation_intent_conflict' USING ERRCODE='P0001';
        END IF;
        IF current_intent.operation_attempt<p_operation_attempt THEN
            UPDATE fs2_activation_intents
               SET operation_attempt=p_operation_attempt,status='queued',
                   available_at=clock_timestamp(),deadline_at=p_deadline_at,
                   controller_id=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                   fencing_token=fencing_token+1,model_fencing_token=NULL,
                   leadership_fencing_token=NULL,error_code=NULL
             WHERE id=current_intent.id
             RETURNING id INTO result_id;
        ELSE
            result_id := current_intent.id;
        END IF;
        RETURN result_id;
    END IF;

    INSERT INTO fs2_activation_intents(
        id,operation_id,operation_attempt,model_id,model_revision,
        binding_digest,action,status,deadline_at,max_attempts
    ) VALUES (
        p_operation_id,p_operation_id,p_operation_attempt,p_model_id,
        p_model_revision,p_binding_digest,'activate','queued',p_deadline_at,
        p_max_attempts
    ) RETURNING id INTO result_id;
    INSERT INTO fs2_activation_events(intent_id,event,status,attempt,fencing_token)
    VALUES(result_id,'activation_intent_queued','queued',0,0);
    RETURN result_id;
END
$function$;

REVOKE ALL ON FUNCTION fs2_activation_model_lock_key(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_runtime_ensure_activation_intent(
    uuid,integer,text,text,char(64),timestamptz,integer,text,bigint
) FROM PUBLIC;

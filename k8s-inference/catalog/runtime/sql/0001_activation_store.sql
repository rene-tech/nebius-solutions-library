BEGIN;

DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_activation_submitter') THEN
    CREATE ROLE fs2_activation_submitter LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_activation_claim_owner') THEN
    CREATE ROLE fs2_activation_claim_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END
$roles$;

CREATE TABLE IF NOT EXISTS fs2_activation_target_state (
  model_id text PRIMARY KEY CHECK (model_id ~ '^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$'),
  last_fencing_token bigint NOT NULL DEFAULT 0 CHECK (last_fencing_token >= 0),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS fs2_activation_intents (
  intent_id uuid PRIMARY KEY,
  operation_id uuid,
  operation_attempt integer NOT NULL CHECK (operation_attempt BETWEEN 0 AND 10),
  model_id text NOT NULL REFERENCES fs2_activation_target_state(model_id),
  model_revision text NOT NULL CHECK (length(model_revision) BETWEEN 1 AND 256),
  binding_digest text NOT NULL CHECK (binding_digest ~ '^[0-9a-f]{64}$'),
  action text NOT NULL CHECK (action IN ('activate', 'deactivate')),
  subject_sha256 text NOT NULL CHECK (subject_sha256 ~ '^[0-9a-f]{64}$'),
  submitter_service_account_uid uuid NOT NULL,
  state text NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending', 'claimed', 'completed', 'failed')),
  controller_id text,
  claim_owner_service_account_uid uuid,
  previous_fencing_token bigint,
  fencing_token bigint,
  claim_started_at timestamptz,
  claim_lease_expires_at timestamptz,
  completion_sha256 text CHECK (completion_sha256 IS NULL OR completion_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (
    (action = 'activate' AND operation_id = intent_id)
    OR (action = 'deactivate' AND operation_id IS NULL AND operation_attempt = 0)
  ),
  CHECK (
    (state = 'pending' AND controller_id IS NULL AND fencing_token IS NULL)
    OR (state <> 'pending' AND controller_id IS NOT NULL AND fencing_token IS NOT NULL)
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS fs2_activation_one_live_intent_per_model
  ON fs2_activation_intents(model_id)
  WHERE state IN ('pending', 'claimed');

CREATE TABLE IF NOT EXISTS fs2_activation_events (
  event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  intent_id uuid NOT NULL REFERENCES fs2_activation_intents(intent_id),
  model_id text NOT NULL,
  fencing_token bigint,
  event_type text NOT NULL,
  event_sha256 text NOT NULL CHECK (event_sha256 ~ '^[0-9a-f]{64}$'),
  observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (intent_id, event_type, event_sha256)
);

CREATE OR REPLACE FUNCTION fs2_submit_activation_intent(
  p_intent_id uuid,
  p_operation_id uuid,
  p_operation_attempt integer,
  p_model_id text,
  p_model_revision text,
  p_binding_digest text,
  p_action text,
  p_subject_sha256 text,
  p_submitter_service_account_uid uuid
) RETURNS fs2_activation_intents
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
  existing fs2_activation_intents;
BEGIN
  INSERT INTO fs2_activation_target_state(model_id) VALUES (p_model_id)
    ON CONFLICT (model_id) DO NOTHING;
  INSERT INTO fs2_activation_intents(
    intent_id, operation_id, operation_attempt, model_id, model_revision,
    binding_digest, action, subject_sha256, submitter_service_account_uid
  ) VALUES (
    p_intent_id, p_operation_id, p_operation_attempt, p_model_id, p_model_revision,
    p_binding_digest, p_action, p_subject_sha256, p_submitter_service_account_uid
  )
  ON CONFLICT (intent_id) DO NOTHING;
  SELECT * INTO STRICT existing FROM fs2_activation_intents WHERE intent_id = p_intent_id;
  IF existing.operation_id IS DISTINCT FROM p_operation_id
     OR existing.operation_attempt <> p_operation_attempt
     OR existing.model_id <> p_model_id
     OR existing.model_revision <> p_model_revision
     OR existing.binding_digest <> p_binding_digest
     OR existing.action <> p_action
     OR existing.subject_sha256 <> p_subject_sha256
     OR existing.submitter_service_account_uid <> p_submitter_service_account_uid THEN
    RAISE EXCEPTION 'activation intent idempotency subject mismatch' USING ERRCODE = '23505';
  END IF;
  RETURN existing;
END
$function$;

CREATE OR REPLACE FUNCTION fs2_claim_activation_intent(
  p_intent_id uuid,
  p_controller_id text,
  p_claim_owner_service_account_uid uuid,
  p_lease_seconds integer DEFAULT 30
) RETURNS fs2_activation_intents
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
  current_intent fs2_activation_intents;
  prior_token bigint;
  next_token bigint;
  database_now timestamptz;
BEGIN
  IF p_lease_seconds < 1 OR p_lease_seconds > 30 THEN
    RAISE EXCEPTION 'activation claim lease outside reviewed bounds';
  END IF;
  SELECT * INTO STRICT current_intent
    FROM fs2_activation_intents WHERE intent_id = p_intent_id FOR UPDATE;
  PERFORM pg_advisory_xact_lock(hashtextextended(current_intent.model_id, 0));
  IF current_intent.state = 'claimed'
     AND current_intent.controller_id = p_controller_id
     AND current_intent.claim_owner_service_account_uid = p_claim_owner_service_account_uid
     AND current_intent.claim_lease_expires_at > clock_timestamp() THEN
    RETURN current_intent;
  END IF;
  IF current_intent.state <> 'pending' THEN
    RAISE EXCEPTION 'activation intent is not claimable';
  END IF;
  SELECT last_fencing_token INTO prior_token
    FROM fs2_activation_target_state WHERE model_id = current_intent.model_id FOR UPDATE;
  next_token := prior_token + 1;
  database_now := clock_timestamp();
  UPDATE fs2_activation_target_state
    SET last_fencing_token = next_token, updated_at = database_now
    WHERE model_id = current_intent.model_id AND last_fencing_token = prior_token;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'activation fencing compare-and-set failed' USING ERRCODE = '40001';
  END IF;
  UPDATE fs2_activation_intents
    SET state = 'claimed', controller_id = p_controller_id,
        claim_owner_service_account_uid = p_claim_owner_service_account_uid,
        previous_fencing_token = prior_token, fencing_token = next_token,
        claim_started_at = database_now,
        claim_lease_expires_at = database_now + make_interval(secs => p_lease_seconds),
        updated_at = database_now
    WHERE intent_id = p_intent_id AND state = 'pending'
    RETURNING * INTO STRICT current_intent;
  RETURN current_intent;
END
$function$;

CREATE OR REPLACE FUNCTION fs2_complete_activation_intent(
  p_intent_id uuid,
  p_controller_id text,
  p_fencing_token bigint,
  p_completion_sha256 text
) RETURNS fs2_activation_intents
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
  current_intent fs2_activation_intents;
  database_now timestamptz := clock_timestamp();
BEGIN
  SELECT * INTO STRICT current_intent
    FROM fs2_activation_intents WHERE intent_id = p_intent_id FOR UPDATE;
  PERFORM pg_advisory_xact_lock(hashtextextended(current_intent.model_id, 0));
  IF current_intent.state = 'completed' THEN
    IF current_intent.controller_id = p_controller_id
       AND current_intent.fencing_token = p_fencing_token
       AND current_intent.completion_sha256 = p_completion_sha256 THEN
      RETURN current_intent;
    END IF;
    RAISE EXCEPTION 'activation completion replay subject mismatch' USING ERRCODE = '23505';
  END IF;
  IF current_intent.state <> 'claimed'
     OR current_intent.controller_id <> p_controller_id
     OR current_intent.fencing_token <> p_fencing_token
     OR current_intent.claim_lease_expires_at <= database_now THEN
    RAISE EXCEPTION 'activation completion lost its DB-clock CAS fence' USING ERRCODE = '40001';
  END IF;
  IF (SELECT last_fencing_token FROM fs2_activation_target_state
      WHERE model_id = current_intent.model_id) <> p_fencing_token THEN
    RAISE EXCEPTION 'activation completion used a stale per-model fence' USING ERRCODE = '40001';
  END IF;
  UPDATE fs2_activation_intents
    SET state = 'completed', completion_sha256 = p_completion_sha256,
        updated_at = database_now
    WHERE intent_id = p_intent_id AND state = 'claimed'
      AND controller_id = p_controller_id AND fencing_token = p_fencing_token
      AND claim_lease_expires_at > database_now
    RETURNING * INTO STRICT current_intent;
  RETURN current_intent;
END
$function$;

REVOKE ALL ON fs2_activation_intents, fs2_activation_target_state, fs2_activation_events FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_submit_activation_intent(uuid, uuid, integer, text, text, text, text, text, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_claim_activation_intent(uuid, text, uuid, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_complete_activation_intent(uuid, text, bigint, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fs2_submit_activation_intent(uuid, uuid, integer, text, text, text, text, text, uuid)
  TO fs2_activation_submitter;
GRANT SELECT (intent_id, operation_id, operation_attempt, model_id, state, created_at, updated_at)
  ON fs2_activation_intents TO fs2_activation_submitter;
GRANT EXECUTE ON FUNCTION fs2_claim_activation_intent(uuid, text, uuid, integer),
  fs2_complete_activation_intent(uuid, text, bigint, text)
  TO fs2_activation_claim_owner;
GRANT SELECT ON fs2_activation_intents, fs2_activation_target_state TO fs2_activation_claim_owner;
GRANT INSERT ON fs2_activation_events TO fs2_activation_claim_owner;
GRANT USAGE, SELECT ON SEQUENCE fs2_activation_events_event_id_seq TO fs2_activation_claim_owner;

COMMIT;

-- Append-only, broker-authenticated request-debug activation lifecycle.
-- Reserved as 0034 after independently accepted SAI-21 claimed 0030/0031
-- and SAI-19 reserved 0032/0033. Integration must reseal this ordering.
-- This is authorization state, not captured customer data: revocation never
-- deletes the activation evidence or the separately retained debug records.
CREATE TABLE fs2_request_debug_activation_events (
    session_id text NOT NULL,
    sequence bigint NOT NULL CHECK (sequence >= 1),
    event_type text NOT NULL CHECK (event_type IN ('activated', 'heartbeat', 'revoked')),
    event_at timestamptz NOT NULL,
    activation_expires_at timestamptz NOT NULL,
    activation_payload_sha256 text NOT NULL CHECK (activation_payload_sha256 ~ '^[a-f0-9]{64}$'),
    broker_id text NOT NULL,
    cluster_id text NOT NULL,
    deployment_id text NOT NULL,
    tenant_id text NOT NULL,
    model_id text NOT NULL,
    app_id uuid NOT NULL,
    revocation_epoch bigint NOT NULL CHECK (revocation_epoch >= 0),
    payload_sha256 text NOT NULL UNIQUE CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    prior_event_sha256 text CHECK (prior_event_sha256 IS NULL OR prior_event_sha256 ~ '^[a-f0-9]{64}$'),
    issuer_id text NOT NULL,
    key_id text NOT NULL CHECK (key_id ~ '^sha256:[a-f0-9]{64}$'),
    signature text NOT NULL,
    signed_envelope jsonb NOT NULL,
    signed_envelope_sha256 text NOT NULL UNIQUE CHECK (signed_envelope_sha256 ~ '^[a-f0-9]{64}$'),
    terminal_teardown_receipt jsonb,
    PRIMARY KEY (session_id, sequence),
    CHECK (
        (event_type != 'revoked' AND event_at <= activation_expires_at)
        OR
        (event_type = 'revoked' AND event_at <= activation_expires_at + interval '5 minutes')
    ),
    CHECK ((event_type = 'revoked') = (revocation_epoch > 0)),
    CHECK ((sequence = 1) = (prior_event_sha256 IS NULL)),
    CHECK ((event_type = 'revoked') = (terminal_teardown_receipt IS NOT NULL)),
    CHECK (signed_envelope->>'schema' = 'fs2-serve.nebius.ai/request-debug-session-event/v1'),
    CHECK (signed_envelope->>'payload_sha256' = payload_sha256),
    CHECK (signed_envelope->>'signature' = signature),
    CHECK (jsonb_typeof(signed_envelope->'payload') = 'object'),
    CHECK (signed_envelope->'payload'->>'schema' = 'fs2-serve.nebius.ai/request-debug-session-event/v1'),
    CHECK (signed_envelope->'payload'->>'session_id' = session_id),
    CHECK ((signed_envelope->'payload'->>'sequence')::bigint = sequence),
    CHECK (signed_envelope->'payload'->>'event_type' = event_type),
    CHECK ((signed_envelope->'payload'->>'event_at')::timestamptz = event_at),
    CHECK ((signed_envelope->'payload'->>'activation_expires_at')::timestamptz = activation_expires_at),
    CHECK (signed_envelope->'payload'->>'activation_payload_sha256' = activation_payload_sha256),
    CHECK (signed_envelope->'payload'->>'broker_id' = broker_id),
    CHECK (signed_envelope->'payload'->>'cluster_id' = cluster_id),
    CHECK (signed_envelope->'payload'->>'deployment_id' = deployment_id),
    CHECK (signed_envelope->'payload'->>'tenant_id' = tenant_id),
    CHECK (signed_envelope->'payload'->>'model_id' = model_id),
    CHECK ((signed_envelope->'payload'->>'app_id')::uuid = app_id),
    CHECK ((signed_envelope->'payload'->>'revocation_epoch')::bigint = revocation_epoch),
    CHECK (signed_envelope->'payload'->>'prior_event_sha256' IS NOT DISTINCT FROM prior_event_sha256),
    CHECK (signed_envelope->'payload'->'issuer' = jsonb_build_object('id',issuer_id,'key_id',key_id)),
    CHECK (
        signed_envelope->'payload'->'terminal_teardown_receipt'
        = COALESCE(terminal_teardown_receipt, 'null'::jsonb)
    ),
    CHECK (
        terminal_teardown_receipt IS NULL
        OR (
            terminal_teardown_receipt->>'schema' = 'fs2-serve.nebius.ai/internal-proxy-terminal/v1'
            AND terminal_teardown_receipt->>'session_id' = session_id
            AND (terminal_teardown_receipt->>'expires_at')::timestamptz = activation_expires_at
            AND (terminal_teardown_receipt->>'teardown_at')::timestamptz = event_at
            AND terminal_teardown_receipt->'backend_authorization_revoked' = 'true'::jsonb
            AND terminal_teardown_receipt->'children_reaped' = 'true'::jsonb
            AND terminal_teardown_receipt->'connections_closed' = 'true'::jsonb
            AND terminal_teardown_receipt->'listener_closed' = 'true'::jsonb
            AND terminal_teardown_receipt->'namespace_destroyed' = 'true'::jsonb
            AND terminal_teardown_receipt->'raw_transports_closed' = 'true'::jsonb
        )
    )
);

CREATE INDEX fs2_request_debug_activation_current
    ON fs2_request_debug_activation_events (session_id, sequence DESC);
CREATE INDEX fs2_request_debug_activation_scope
    ON fs2_request_debug_activation_events
    (tenant_id, model_id, app_id, sequence DESC);

ALTER TABLE fs2_request_debug_activation_events
    ADD CONSTRAINT fs2_request_debug_activation_capture_identity
    UNIQUE (session_id, sequence, activation_payload_sha256, app_id, tenant_id, model_id),
    ADD CONSTRAINT fs2_request_debug_activation_event_identity
    UNIQUE (session_id, sequence, activation_payload_sha256);

-- O(1) signed current-state projection. The original event envelopes remain
-- append-only and independently replayable; runtime reads reverify this signed
-- checkpoint instead of replaying up to 40k heartbeat signatures per request.
CREATE TABLE fs2_request_debug_session_current (
    session_id text PRIMARY KEY,
    sequence bigint NOT NULL,
    event_type text NOT NULL,
    event_at timestamptz NOT NULL,
    activation_expires_at timestamptz NOT NULL,
    activation_payload_sha256 text NOT NULL,
    broker_id text NOT NULL,
    cluster_id text NOT NULL,
    deployment_id text NOT NULL,
    tenant_id text NOT NULL,
    model_id text NOT NULL,
    app_id uuid NOT NULL,
    revocation_epoch bigint NOT NULL,
    payload_sha256 text NOT NULL,
    prior_event_sha256 text,
    issuer_id text NOT NULL,
    key_id text NOT NULL,
    signature text NOT NULL,
    signed_envelope jsonb NOT NULL,
    signed_envelope_sha256 text NOT NULL UNIQUE,
    terminal_teardown_receipt jsonb,
    FOREIGN KEY (session_id, sequence)
        REFERENCES fs2_request_debug_activation_events(session_id, sequence)
        ON DELETE RESTRICT
);

CREATE INDEX fs2_request_debug_session_current_scope
    ON fs2_request_debug_session_current
    (tenant_id, model_id, app_id, activation_expires_at);

CREATE TABLE fs2_request_debug_activation_roots (
    activation_payload_sha256 text PRIMARY KEY,
    session_id text NOT NULL UNIQUE,
    genesis_sequence bigint NOT NULL DEFAULT 1 CHECK (genesis_sequence = 1),
    tenant_id text NOT NULL,
    model_id text NOT NULL,
    app_id uuid NOT NULL,
    activation_expires_at timestamptz NOT NULL,
    genesis_envelope_sha256 text NOT NULL UNIQUE,
    FOREIGN KEY (session_id, genesis_sequence, activation_payload_sha256, app_id, tenant_id, model_id)
        REFERENCES fs2_request_debug_activation_events
        (session_id, sequence, activation_payload_sha256, app_id, tenant_id, model_id)
        ON DELETE RESTRICT
);

CREATE TABLE fs2_request_debug_activation_revocations (
    activation_payload_sha256 text PRIMARY KEY,
    session_id text,
    sequence bigint,
    revocation_epoch bigint NOT NULL CHECK (revocation_epoch >= 1),
    revoked_at timestamptz NOT NULL,
    reason text NOT NULL,
    source_schema text NOT NULL CHECK (source_schema IN (
        'fs2-serve.nebius.ai/request-debug-session-event/v1',
        'fs2-serve.nebius.ai/request-debug-activation-tombstone/v1'
    )),
    issuer_id text NOT NULL,
    key_id text NOT NULL CHECK (key_id ~ '^sha256:[a-f0-9]{64}$'),
    signature text NOT NULL,
    signed_envelope jsonb NOT NULL,
    terminal_envelope_sha256 text NOT NULL UNIQUE,
    terminal_teardown_receipt jsonb,
    CHECK (
        session_id IS NOT NULL AND sequence IS NOT NULL
        AND terminal_teardown_receipt IS NOT NULL
    ),
    CHECK (signed_envelope->>'schema' = source_schema),
    CHECK (signed_envelope->>'payload_sha256' ~ '^[a-f0-9]{64}$'),
    CHECK (signed_envelope->>'signature' = signature),
    CHECK (jsonb_typeof(signed_envelope->'payload') = 'object'),
    CHECK (signed_envelope->'payload'->>'schema' = source_schema),
    CHECK (signed_envelope->'payload'->>'activation_payload_sha256' = activation_payload_sha256),
    CHECK ((signed_envelope->'payload'->>'revocation_epoch')::bigint = revocation_epoch),
    CHECK (signed_envelope->'payload'->'issuer' = jsonb_build_object('id',issuer_id,'key_id',key_id)),
    CHECK (
        (source_schema='fs2-serve.nebius.ai/request-debug-activation-tombstone/v1'
         AND (signed_envelope->'payload'->>'revoked_at')::timestamptz=revoked_at
         AND signed_envelope->'payload'->>'reason'=reason
         AND (signed_envelope->'payload'->>'sequence')::bigint=sequence
         AND signed_envelope->'payload'->>'session_id'=session_id
         AND signed_envelope->'payload'->'terminal_teardown_receipt'=
             terminal_teardown_receipt)
        OR
        (source_schema='fs2-serve.nebius.ai/request-debug-session-event/v1'
         AND (signed_envelope->'payload'->>'sequence')::bigint=sequence
         AND signed_envelope->'payload'->>'session_id'=session_id
         AND (signed_envelope->'payload'->>'event_at')::timestamptz=revoked_at
         AND signed_envelope->'payload'->'terminal_teardown_receipt'=
             terminal_teardown_receipt
         AND terminal_teardown_receipt->>'reason'=reason)
    ),
    CHECK (
        terminal_teardown_receipt->>'schema'=
            'fs2-serve.nebius.ai/internal-proxy-terminal/v1'
        AND terminal_teardown_receipt->>'session_id'=session_id
        AND (terminal_teardown_receipt->>'teardown_at')::timestamptz=revoked_at
        AND terminal_teardown_receipt->>'reason'=reason
        AND terminal_teardown_receipt->'backend_authorization_revoked'='true'::jsonb
        AND terminal_teardown_receipt->'children_reaped'='true'::jsonb
        AND terminal_teardown_receipt->'connections_closed'='true'::jsonb
        AND terminal_teardown_receipt->'listener_closed'='true'::jsonb
        AND terminal_teardown_receipt->'namespace_destroyed'='true'::jsonb
        AND terminal_teardown_receipt->'raw_transports_closed'='true'::jsonb
    ),
    FOREIGN KEY (session_id, sequence, activation_payload_sha256)
        REFERENCES fs2_request_debug_activation_events
        (session_id, sequence, activation_payload_sha256)
        ON DELETE RESTRICT
);

-- Existing 0029 records remain readable with all four nullable linkage fields.
-- New captures must bind all fields to one exact signed current event.
ALTER TABLE fs2_request_debug
    ADD COLUMN debug_activation_sha256 text,
    ADD COLUMN debug_app_id uuid,
    ADD COLUMN debug_event_sequence bigint,
    ADD COLUMN debug_session_id text,
    ADD CONSTRAINT fs2_request_debug_session_link_all_or_none CHECK (
        (debug_activation_sha256 IS NULL AND debug_app_id IS NULL
         AND debug_event_sequence IS NULL AND debug_session_id IS NULL)
        OR
        (debug_activation_sha256 ~ '^[a-f0-9]{64}$' AND debug_app_id IS NOT NULL
         AND debug_event_sequence >= 1 AND debug_session_id ~ '^[a-f0-9]{32,64}$'
         AND tenant_id IS NOT NULL AND model_id IS NOT NULL)
    ),
    ADD CONSTRAINT fs2_request_debug_signed_session_event FOREIGN KEY
        (debug_session_id, debug_event_sequence, debug_activation_sha256,
         debug_app_id, tenant_id, model_id)
        REFERENCES fs2_request_debug_activation_events
        (session_id, sequence, activation_payload_sha256, app_id, tenant_id, model_id)
        ON DELETE RESTRICT;

CREATE FUNCTION fs2_request_debug_require_signed_link() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.debug_activation_sha256 IS NULL
       OR NEW.debug_app_id IS NULL
       OR NEW.debug_event_sequence IS NULL
       OR NEW.debug_session_id IS NULL THEN
        RAISE EXCEPTION 'new request-debug captures require one signed session event';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER fs2_request_debug_require_signed_link
BEFORE INSERT ON fs2_request_debug
FOR EACH ROW EXECUTE FUNCTION fs2_request_debug_require_signed_link();

CREATE TABLE fs2_request_debug_authorization_nonces (
    nonce_sha256 text PRIMARY KEY CHECK (nonce_sha256 ~ '^[a-f0-9]{64}$'),
    session_id text NOT NULL,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE FUNCTION fs2_request_debug_append_only_event() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    previous fs2_request_debug_activation_events%ROWTYPE;
    overlapping integer;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.session_id, 0));
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.activation_payload_sha256, 0));
    PERFORM pg_advisory_xact_lock(
        hashtextextended(NEW.tenant_id || chr(31) || NEW.model_id || chr(31) || NEW.app_id::text, 0)
    );
    IF EXISTS (
        SELECT 1 FROM fs2_request_debug_activation_revocations
         WHERE activation_payload_sha256=NEW.activation_payload_sha256
    ) THEN
        RAISE EXCEPTION 'tombstoned debug activation cannot append another event';
    END IF;
    SELECT * INTO previous
      FROM fs2_request_debug_activation_events
     WHERE session_id=NEW.session_id
     ORDER BY sequence DESC LIMIT 1 FOR UPDATE;
    IF NOT FOUND THEN
        IF NEW.sequence != 1 OR NEW.event_type != 'activated'
           OR NEW.revocation_epoch != 0 OR NEW.prior_event_sha256 IS NOT NULL THEN
            RAISE EXCEPTION 'debug session genesis is not exact';
        END IF;
        IF EXISTS (
            SELECT 1 FROM fs2_request_debug_activation_roots
             WHERE activation_payload_sha256=NEW.activation_payload_sha256
        ) OR EXISTS (
            SELECT 1 FROM fs2_request_debug_activation_revocations
             WHERE activation_payload_sha256=NEW.activation_payload_sha256
        ) THEN
            RAISE EXCEPTION 'debug activation identity cannot be reused';
        END IF;
        SELECT count(*) INTO overlapping FROM (
            SELECT DISTINCT ON (session_id) *
              FROM fs2_request_debug_activation_events
             ORDER BY session_id,sequence DESC
        ) current
        WHERE event_type != 'revoked'
          AND activation_expires_at > NEW.event_at
          AND tenant_id=NEW.tenant_id AND model_id=NEW.model_id AND app_id=NEW.app_id
          AND NOT EXISTS (
              SELECT 1 FROM fs2_request_debug_activation_revocations revoked
               WHERE revoked.activation_payload_sha256=current.activation_payload_sha256
          );
        IF overlapping != 0 THEN
            RAISE EXCEPTION 'debug scope already has a current session';
        END IF;
    ELSE
        IF previous.event_type='revoked'
           OR NEW.sequence != previous.sequence + 1
           OR NEW.prior_event_sha256 != previous.signed_envelope_sha256
           OR NEW.activation_payload_sha256 != previous.activation_payload_sha256
           OR NEW.broker_id != previous.broker_id
           OR NEW.cluster_id != previous.cluster_id
           OR NEW.deployment_id != previous.deployment_id
           OR NEW.tenant_id != previous.tenant_id
           OR NEW.model_id != previous.model_id
           OR NEW.app_id != previous.app_id
           OR NEW.activation_expires_at != previous.activation_expires_at
           OR NEW.issuer_id != previous.issuer_id
           OR NEW.key_id != previous.key_id
           OR NEW.event_at <= previous.event_at
           OR (NEW.event_type='heartbeat' AND NEW.revocation_epoch != previous.revocation_epoch)
           OR (NEW.event_type='revoked' AND NEW.revocation_epoch != previous.revocation_epoch + 1) THEN
            RAISE EXCEPTION 'debug session event is not the exact monotonic successor';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER fs2_request_debug_append_only_event
BEFORE INSERT ON fs2_request_debug_activation_events
FOR EACH ROW EXECUTE FUNCTION fs2_request_debug_append_only_event();

CREATE FUNCTION fs2_request_debug_serialize_revocation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    initial_root fs2_request_debug_activation_roots%ROWTYPE;
    locked_root fs2_request_debug_activation_roots%ROWTYPE;
    latest_sequence bigint;
    latest_type text;
    latest_activation_expires_at timestamptz;
BEGIN
    SELECT * INTO initial_root
      FROM fs2_request_debug_activation_roots
     WHERE activation_payload_sha256=NEW.activation_payload_sha256;
    IF FOUND THEN
        PERFORM pg_advisory_xact_lock(hashtextextended(initial_root.session_id,0));
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(NEW.activation_payload_sha256,0)
    );
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            NEW.signed_envelope->'payload'->>'tenant_id' || chr(31)
            || NEW.signed_envelope->'payload'->>'model_id' || chr(31)
            || NEW.signed_envelope->'payload'->>'app_id',
            0
        )
    );
    SELECT * INTO locked_root
      FROM fs2_request_debug_activation_roots
     WHERE activation_payload_sha256=NEW.activation_payload_sha256;
    IF initial_root.session_id IS NULL AND locked_root.session_id IS NOT NULL THEN
        RAISE EXCEPTION 'debug activation root changed during revocation';
    END IF;
    IF locked_root.session_id IS NULL THEN
        RAISE EXCEPTION 'debug activation has no retained session root';
    END IF;
    IF initial_root.session_id IS NOT NULL AND (
        locked_root.session_id IS NULL
        OR locked_root.session_id != initial_root.session_id
    ) THEN
        RAISE EXCEPTION 'debug activation root changed during revocation';
    END IF;
    IF locked_root.session_id IS NOT NULL AND (
        NEW.signed_envelope->'payload'->>'tenant_id' != locked_root.tenant_id
        OR NEW.signed_envelope->'payload'->>'model_id' != locked_root.model_id
        OR (NEW.signed_envelope->'payload'->>'app_id')::uuid != locked_root.app_id
    ) THEN
        RAISE EXCEPTION 'debug revocation differs from retained activation root';
    END IF;
    IF locked_root.session_id IS NOT NULL THEN
        SELECT sequence,event_type,activation_expires_at
          INTO latest_sequence,latest_type,latest_activation_expires_at
          FROM fs2_request_debug_activation_events
         WHERE session_id=locked_root.session_id
         ORDER BY sequence DESC LIMIT 1 FOR UPDATE;
        IF NEW.session_id != locked_root.session_id
           OR NEW.sequence != latest_sequence
           OR (
               NEW.source_schema=
                   'fs2-serve.nebius.ai/request-debug-activation-tombstone/v1'
               AND latest_type='revoked'
           )
           OR (
               NEW.source_schema=
                   'fs2-serve.nebius.ai/request-debug-session-event/v1'
               AND latest_type!='revoked'
           )
           OR (NEW.terminal_teardown_receipt->>'expires_at')::timestamptz
              != latest_activation_expires_at THEN
            RAISE EXCEPTION 'debug revocation does not bind the current session event';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER fs2_request_debug_serialize_revocation
BEFORE INSERT ON fs2_request_debug_activation_revocations
FOR EACH ROW EXECUTE FUNCTION fs2_request_debug_serialize_revocation();

CREATE FUNCTION fs2_request_debug_project_current() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
    PERFORM set_config('fs2.request_debug_projector', 'exact-v1', true);
    IF NEW.event_type='activated' THEN
        INSERT INTO fs2_request_debug_activation_roots
            (activation_payload_sha256,session_id,genesis_sequence,tenant_id,model_id,app_id,
             activation_expires_at,genesis_envelope_sha256)
        VALUES
            (NEW.activation_payload_sha256,NEW.session_id,NEW.sequence,NEW.tenant_id,NEW.model_id,
             NEW.app_id,NEW.activation_expires_at,NEW.signed_envelope_sha256);
    END IF;
    INSERT INTO fs2_request_debug_session_current
        (session_id,sequence,event_type,event_at,activation_expires_at,
         activation_payload_sha256,broker_id,cluster_id,deployment_id,
         tenant_id,model_id,app_id,revocation_epoch,payload_sha256,
         prior_event_sha256,issuer_id,key_id,signature,signed_envelope,
         signed_envelope_sha256,terminal_teardown_receipt)
    VALUES
        (NEW.session_id,NEW.sequence,NEW.event_type,NEW.event_at,NEW.activation_expires_at,
         NEW.activation_payload_sha256,NEW.broker_id,NEW.cluster_id,NEW.deployment_id,
         NEW.tenant_id,NEW.model_id,NEW.app_id,NEW.revocation_epoch,NEW.payload_sha256,
         NEW.prior_event_sha256,NEW.issuer_id,NEW.key_id,NEW.signature,NEW.signed_envelope,
         NEW.signed_envelope_sha256,NEW.terminal_teardown_receipt)
    ON CONFLICT (session_id) DO UPDATE SET
        sequence=EXCLUDED.sequence,
        event_type=EXCLUDED.event_type,
        event_at=EXCLUDED.event_at,
        activation_expires_at=EXCLUDED.activation_expires_at,
        revocation_epoch=EXCLUDED.revocation_epoch,
        payload_sha256=EXCLUDED.payload_sha256,
        prior_event_sha256=EXCLUDED.prior_event_sha256,
        signature=EXCLUDED.signature,
        signed_envelope=EXCLUDED.signed_envelope,
        signed_envelope_sha256=EXCLUDED.signed_envelope_sha256,
        terminal_teardown_receipt=EXCLUDED.terminal_teardown_receipt
    WHERE fs2_request_debug_session_current.signed_envelope_sha256
          = EXCLUDED.prior_event_sha256;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'debug current projection transition was not exact';
    END IF;
    IF NEW.event_type='revoked' THEN
        INSERT INTO fs2_request_debug_activation_revocations
            (activation_payload_sha256,session_id,sequence,revocation_epoch,
             revoked_at,reason,source_schema,issuer_id,key_id,signature,
             signed_envelope,terminal_envelope_sha256,terminal_teardown_receipt)
        VALUES
            (NEW.activation_payload_sha256,NEW.session_id,NEW.sequence,
             NEW.revocation_epoch,NEW.event_at,
             NEW.terminal_teardown_receipt->>'reason',
             'fs2-serve.nebius.ai/request-debug-session-event/v1',
             NEW.issuer_id,NEW.key_id,NEW.signature,NEW.signed_envelope,
             NEW.signed_envelope_sha256,NEW.terminal_teardown_receipt);
    END IF;
    PERFORM set_config('fs2.request_debug_projector', 'off', true);
    RETURN NEW;
END;
$$;

CREATE FUNCTION fs2_request_debug_guard_current() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE'
       OR current_setting('fs2.request_debug_projector', true) IS DISTINCT FROM 'exact-v1' THEN
        RAISE EXCEPTION 'request-debug current state is projector-owned';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER fs2_request_debug_guard_current
BEFORE INSERT OR UPDATE OR DELETE ON fs2_request_debug_session_current
FOR EACH ROW EXECUTE FUNCTION fs2_request_debug_guard_current();

CREATE TRIGGER fs2_request_debug_project_current
AFTER INSERT ON fs2_request_debug_activation_events
FOR EACH ROW EXECUTE FUNCTION fs2_request_debug_project_current();

CREATE FUNCTION fs2_request_debug_reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'request-debug authorization evidence is append-only';
END;
$$;

CREATE TRIGGER fs2_request_debug_activation_no_mutation
BEFORE UPDATE OR DELETE ON fs2_request_debug_activation_events
FOR EACH ROW EXECUTE FUNCTION fs2_request_debug_reject_mutation();
CREATE TRIGGER fs2_request_debug_nonce_no_mutation
BEFORE UPDATE OR DELETE ON fs2_request_debug_authorization_nonces
FOR EACH ROW EXECUTE FUNCTION fs2_request_debug_reject_mutation();
CREATE TRIGGER fs2_request_debug_roots_no_mutation
BEFORE UPDATE OR DELETE ON fs2_request_debug_activation_roots
FOR EACH ROW EXECUTE FUNCTION fs2_request_debug_reject_mutation();
CREATE TRIGGER fs2_request_debug_revocations_no_mutation
BEFORE UPDATE OR DELETE ON fs2_request_debug_activation_revocations
FOR EACH ROW EXECUTE FUNCTION fs2_request_debug_reject_mutation();
DO $$
BEGIN
    REVOKE ALL ON fs2_request_debug_session_current,
        fs2_request_debug_activation_roots,
        fs2_request_debug_activation_revocations FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime') THEN
        GRANT SELECT, INSERT ON fs2_request_debug_activation_events,
            fs2_request_debug_authorization_nonces TO fs2_serve_runtime;
        GRANT SELECT ON fs2_request_debug_session_current TO fs2_serve_runtime;
        GRANT SELECT ON fs2_request_debug_activation_roots,
            fs2_request_debug_activation_revocations TO fs2_serve_runtime;
        GRANT INSERT ON fs2_request_debug_activation_revocations TO fs2_serve_runtime;
    END IF;
END;
$$;

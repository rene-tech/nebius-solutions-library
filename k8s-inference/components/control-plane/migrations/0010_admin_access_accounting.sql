CREATE TABLE fs2_operator_principals (
    id uuid PRIMARY KEY,
    subject text NOT NULL CHECK (length(subject) BETWEEN 1 AND 200),
    display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
    kind text NOT NULL CHECK (kind IN ('human','service')),
    role text NOT NULL CHECK (role IN ('viewer','operator','admin')),
    tenant_id text CHECK (tenant_id IS NULL OR length(tenant_id) BETWEEN 1 AND 120),
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 200),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    disabled_at timestamptz,
    CHECK ((enabled AND disabled_at IS NULL) OR (NOT enabled AND disabled_at IS NOT NULL))
);

CREATE UNIQUE INDEX fs2_operator_principals_subject_tenant_idx
    ON fs2_operator_principals (COALESCE(tenant_id,''),subject);
CREATE INDEX fs2_operator_principals_tenant_idx
    ON fs2_operator_principals (tenant_id,created_at DESC,id DESC);

INSERT INTO fs2_operator_principals(
    id,subject,display_name,kind,role,tenant_id,enabled,created_by
) VALUES (
    '00000000-0000-0000-0000-000000000001',
    'bootstrap-admin','Bootstrap administrator','service','admin',NULL,true,'schema-migration'
);

INSERT INTO fs2_audit_events(
    actor,tenant_id,token_id,action,target_type,target_id,outcome,detail
) VALUES (
    'schema-migration',NULL,NULL,'principal.bootstrap','operator_principal',
    '00000000-0000-0000-0000-000000000001','succeeded',
    '{"role":"admin","scope":"global"}'::jsonb
);

CREATE TABLE fs2_operator_sessions (
    id uuid PRIMARY KEY,
    principal_id uuid NOT NULL REFERENCES fs2_operator_principals(id),
    pepper_key_id text NOT NULL CHECK (length(pepper_key_id) BETWEEN 1 AND 64),
    digest char(64) NOT NULL CHECK (digest ~ '^[a-f0-9]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    revoked_at timestamptz,
    created_by text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 200),
    CHECK (expires_at > created_at),
    CHECK (last_seen_at >= created_at),
    UNIQUE (id,digest)
);

CREATE INDEX fs2_operator_sessions_principal_idx
    ON fs2_operator_sessions (principal_id,created_at DESC,id DESC);
CREATE INDEX fs2_operator_sessions_expiry_idx
    ON fs2_operator_sessions (expires_at,id) WHERE revoked_at IS NULL;

ALTER TABLE fs2_tokens
    ADD COLUMN name text CHECK (name IS NULL OR length(name) BETWEEN 1 AND 120),
    ADD COLUMN fingerprint char(64) CHECK (fingerprint IS NULL OR fingerprint ~ '^[a-f0-9]{64}$'),
    ADD COLUMN last_used_at timestamptz,
    ADD COLUMN rotation_parent_id uuid REFERENCES fs2_tokens(id),
    ADD COLUMN rotated_at timestamptz,
    ADD COLUMN expiration_recorded_at timestamptz,
    ADD COLUMN rate_limit_requests integer CHECK (rate_limit_requests IS NULL OR rate_limit_requests > 0),
    ADD COLUMN rate_window_seconds integer CHECK (
        rate_window_seconds IS NULL OR rate_window_seconds BETWEEN 1 AND 86400
    ),
    ADD COLUMN rate_window_started_at timestamptz,
    ADD COLUMN rate_window_requests integer NOT NULL DEFAULT 0 CHECK (rate_window_requests >= 0),
    ADD CONSTRAINT fs2_tokens_rate_configuration_check CHECK (
        (rate_limit_requests IS NULL) = (rate_window_seconds IS NULL)
    ),
    ADD CONSTRAINT fs2_tokens_rotation_check CHECK (rotation_parent_id IS NULL OR rotation_parent_id <> id);

CREATE UNIQUE INDEX fs2_tokens_fingerprint_idx ON fs2_tokens (fingerprint) WHERE fingerprint IS NOT NULL;
CREATE INDEX fs2_tokens_rotation_parent_idx ON fs2_tokens (rotation_parent_id) WHERE rotation_parent_id IS NOT NULL;

ALTER TABLE fs2_operations
    ADD COLUMN input_tokens bigint CHECK (input_tokens IS NULL OR input_tokens >= 0),
    ADD COLUMN output_tokens bigint CHECK (output_tokens IS NULL OR output_tokens >= 0),
    ADD COLUMN modality_usage jsonb CHECK (
        modality_usage IS NULL OR (
            jsonb_typeof(modality_usage)='array' AND octet_length(modality_usage::text) <= 4096
        )
    );

ALTER TABLE fs2_usage_facts
    ADD COLUMN token_id uuid,
    ADD COLUMN input_tokens bigint CHECK (input_tokens IS NULL OR input_tokens >= 0),
    ADD COLUMN output_tokens bigint CHECK (output_tokens IS NULL OR output_tokens >= 0),
    ADD COLUMN modality_usage jsonb CHECK (
        modality_usage IS NULL OR (
            jsonb_typeof(modality_usage)='array' AND octet_length(modality_usage::text) <= 4096
        )
    );

UPDATE fs2_usage_facts usage
SET token_id=operation.token_id
FROM fs2_operations operation
WHERE operation.id=usage.operation_id AND usage.token_id IS NULL;

CREATE INDEX fs2_usage_facts_token_idx
    ON fs2_usage_facts (tenant_id,token_id,occurred_at) WHERE token_id IS NOT NULL;

CREATE OR REPLACE FUNCTION fs2_record_terminal_usage() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF NEW.status IN ('succeeded','failed','cancelled','preempted','expired')
       AND (TG_OP='INSERT' OR OLD.status NOT IN ('succeeded','failed','cancelled','preempted','expired')) THEN
        INSERT INTO fs2_usage_facts(
            operation_id,occurred_at,tenant_id,principal_id,token_id,model_id,protocol,
            outcome,status,attempt,estimated_gpu_seconds,duration_seconds,cold_start_seconds,
            input_tokens,output_tokens,modality_usage
        ) VALUES (
            NEW.id,COALESCE(NEW.completed_at,clock_timestamp()),NEW.tenant_id,NEW.principal_id,
            NEW.token_id,NEW.model_id,NEW.protocol,COALESCE(NEW.outcome,NEW.status::text),NEW.status,NEW.attempt,
            NEW.estimated_gpu_seconds,
            GREATEST(0,extract(epoch FROM COALESCE(NEW.completed_at,clock_timestamp())-NEW.accepted_at)),
            NEW.cold_start_seconds,NEW.input_tokens,NEW.output_tokens,NEW.modality_usage
        ) ON CONFLICT (operation_id) DO NOTHING;
    END IF;
    RETURN NEW;
END
$function$;

REVOKE ALL ON FUNCTION fs2_record_terminal_usage() FROM PUBLIC;

CREATE OR REPLACE VIEW fs2_reporting_model_usage AS
SELECT date_trunc('minute',occurred_at) AS time,model_id,protocol,status::text AS status,outcome,
       count(*)::bigint AS operations,
       sum(estimated_gpu_seconds)::double precision AS estimated_gpu_seconds,
       sum(duration_seconds)::double precision AS duration_seconds,
       sum(input_tokens)::bigint AS input_tokens,
       sum(output_tokens)::bigint AS output_tokens,
       count(*) FILTER (WHERE input_tokens IS NOT NULL AND output_tokens IS NOT NULL)::bigint
           AS token_reported_operations
FROM fs2_usage_facts
GROUP BY 1,2,3,4,5;

CREATE OR REPLACE VIEW fs2_reporting_principal_usage AS
SELECT date_trunc('minute',occurred_at) AS time,tenant_id,principal_id,model_id,
       count(*)::bigint AS operations,
       sum(estimated_gpu_seconds)::double precision AS estimated_gpu_seconds,
       sum(input_tokens)::bigint AS input_tokens,
       sum(output_tokens)::bigint AS output_tokens,
       count(*) FILTER (WHERE input_tokens IS NOT NULL AND output_tokens IS NOT NULL)::bigint
           AS token_reported_operations
FROM fs2_usage_facts
GROUP BY 1,2,3,4;

CREATE OR REPLACE VIEW fs2_reporting_terminal_totals AS
SELECT model_id,protocol,outcome,count(*)::bigint AS operations,
       sum(estimated_gpu_seconds)::double precision AS estimated_gpu_seconds,
       sum(duration_seconds)::double precision AS duration_seconds,
       sum(COALESCE(cold_start_seconds,0))::double precision AS cold_start_seconds,
       sum(input_tokens)::bigint AS input_tokens,
       sum(output_tokens)::bigint AS output_tokens,
       count(*) FILTER (WHERE input_tokens IS NOT NULL AND output_tokens IS NOT NULL)::bigint
           AS token_reported_operations
FROM fs2_usage_facts
GROUP BY 1,2,3;

COMMENT ON TABLE fs2_operator_sessions IS
    'Opaque operator sessions; raw cookie secrets are never persisted';
COMMENT ON COLUMN fs2_tokens.fingerprint IS
    'SHA-256 fingerprint of newly issued high-entropy PAT material; legacy rows remain NULL';
COMMENT ON COLUMN fs2_operations.modality_usage IS
    'Bounded runtime-reported usage only; NULL means unavailable and is never estimated';

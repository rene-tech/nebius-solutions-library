-- SAI-10 owns the independent 0030_customer_storage_credentials.sql lineage.
-- This additive SAI-16 state starts at 0032 and does not rename or overwrite
-- either sibling migration during later parent-branch reconciliation.
ALTER TABLE fs2_release_identity_receipts
    ADD COLUMN resource_sha256 char(64) CHECK (
        resource_sha256 IS NULL OR resource_sha256 ~ '^[a-f0-9]{64}$'
    ),
    ADD COLUMN resource_generation text CHECK (
        resource_generation IS NULL
        OR resource_generation ~ '^[a-z0-9][a-z0-9.-]{6,61}[a-z0-9]$'
    ),
    ADD CHECK ((resource_sha256 IS NULL) = (resource_generation IS NULL));

CREATE TABLE fs2_session_exchange_source_buckets (
    slot integer PRIMARY KEY CHECK (slot BETWEEN 0 AND 65535),
    source_fingerprint char(64) CHECK (
        source_fingerprint IS NULL OR source_fingerprint ~ '^[a-f0-9]{64}$'
    ),
    window_started_at timestamptz,
    admitted_count integer NOT NULL DEFAULT 0 CHECK (admitted_count BETWEEN 0 AND 10000),
    rejection_observed boolean NOT NULL DEFAULT false,
    first_rejected_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((source_fingerprint IS NULL) = (window_started_at IS NULL)),
    CHECK (rejection_observed = (first_rejected_at IS NOT NULL))
);

COMMENT ON TABLE fs2_session_exchange_source_buckets IS
    'Fixed 65536-slot, two-choice source limiter state; rows are reused by window and never attacker-appended';

CREATE INDEX fs2_session_exchange_source_forensics_idx
    ON fs2_session_exchange_source_buckets
        (window_started_at DESC, first_rejected_at DESC, slot)
    WHERE rejection_observed;

CREATE TABLE fs2_session_exchange_aggregate_buckets (
    shard smallint PRIMARY KEY CHECK (shard BETWEEN 0 AND 15),
    window_started_at timestamptz,
    admitted_count integer NOT NULL DEFAULT 0 CHECK (admitted_count BETWEEN 0 AND 10000),
    rejection_observed boolean NOT NULL DEFAULT false,
    first_rejected_at timestamptz,
    collision_rejection_observed boolean NOT NULL DEFAULT false,
    first_collision_rejected_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (rejection_observed = (first_rejected_at IS NOT NULL)),
    CHECK (collision_rejection_observed = (first_collision_rejected_at IS NOT NULL))
);

COMMENT ON TABLE fs2_session_exchange_aggregate_buckets IS
    'Fixed 16-shard aggregate limiter and one-write rejection transitions; no global lock or audit scan';

CREATE INDEX fs2_session_exchange_aggregate_forensics_idx
    ON fs2_session_exchange_aggregate_buckets
        (window_started_at DESC, first_rejected_at DESC, first_collision_rejected_at DESC, shard)
    WHERE rejection_observed OR collision_rejection_observed;

CREATE INDEX fs2_audit_session_exchange_forensics_idx
    ON fs2_audit_events (outcome, occurred_at DESC, id DESC)
    INCLUDE (target_id)
    WHERE action = 'session.exchange.attempt'
      AND target_type = 'network_source_fingerprint';

CREATE FUNCTION fs2_consume_session_exchange(
    p_source_fingerprint text,
    p_window_seconds integer,
    p_maximum_source_attempts integer,
    p_maximum_aggregate_attempts integer
)
RETURNS TABLE (
    admission text,
    retry_after_seconds double precision,
    source_slot integer,
    aggregate_shard smallint,
    evidence_kind text,
    emit_audit boolean,
    bucket_started_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_window_start timestamptz;
    v_retry_after double precision;
    v_digest bytea;
    v_slot_a integer;
    v_slot_b integer;
    v_source_probe fs2_session_exchange_source_buckets%ROWTYPE;
    v_source fs2_session_exchange_source_buckets%ROWTYPE;
    v_source_collision boolean := false;
    v_shard_count integer;
    v_shard smallint;
    v_shard_quota integer;
    v_aggregate fs2_session_exchange_aggregate_buckets%ROWTYPE;
    v_emit boolean;
BEGIN
    IF p_source_fingerprint IS NULL
       OR p_window_seconds IS NULL
       OR p_maximum_source_attempts IS NULL
       OR p_maximum_aggregate_attempts IS NULL
       OR p_source_fingerprint !~ '^[a-f0-9]{64}$'
       OR p_window_seconds NOT BETWEEN 1 AND 3600
       OR p_maximum_source_attempts NOT BETWEEN 1 AND 100
       OR p_maximum_aggregate_attempts NOT BETWEEN 10 AND 10000
       OR p_maximum_aggregate_attempts <= p_maximum_source_attempts THEN
        RAISE EXCEPTION 'session exchange limiter input is outside the closed contract';
    END IF;

    v_window_start := to_timestamp(
        floor(extract(epoch FROM v_now) / p_window_seconds) * p_window_seconds
    );
    v_retry_after := greatest(
        0.001,
        extract(epoch FROM (v_window_start + make_interval(secs => p_window_seconds) - v_now))
    );
    v_digest := decode(p_source_fingerprint, 'hex');
    v_slot_a := (
        get_byte(v_digest, 0)::bigint * 16777216
        + get_byte(v_digest, 1)::bigint * 65536
        + get_byte(v_digest, 2)::bigint * 256
        + get_byte(v_digest, 3)::bigint
    ) % 65536;
    v_slot_b := (
        get_byte(v_digest, 4)::bigint * 16777216
        + get_byte(v_digest, 5)::bigint * 65536
        + get_byte(v_digest, 6)::bigint * 256
        + get_byte(v_digest, 7)::bigint
    ) % 65536;
    IF v_slot_b = v_slot_a THEN
        v_slot_b := (v_slot_b + 1) % 65536;
    END IF;
    v_shard_count := least(
        16,
        greatest(1, p_maximum_aggregate_attempts / p_maximum_source_attempts)
    );
    v_shard := (
        get_byte(v_digest, 8)::integer * 256 + get_byte(v_digest, 9)::integer
    ) % v_shard_count;
    v_shard_quota := p_maximum_aggregate_attempts / v_shard_count
        + CASE WHEN v_shard < p_maximum_aggregate_attempts % v_shard_count THEN 1 ELSE 0 END;

    -- Stable rejection states are read-only. This is the first line of
    -- defense after a process restart or cache eviction: repeated 429s do not
    -- reacquire a row lock, update a counter, or append another audit event.
    SELECT bucket.* INTO v_source_probe
    FROM public.fs2_session_exchange_source_buckets AS bucket
    WHERE bucket.slot IN (v_slot_a, v_slot_b)
      AND bucket.source_fingerprint = p_source_fingerprint
      AND bucket.window_started_at = v_window_start
    ORDER BY bucket.slot
    LIMIT 1;
    IF FOUND
       AND v_source_probe.admitted_count >= p_maximum_source_attempts
       AND v_source_probe.rejection_observed THEN
        RETURN QUERY SELECT
            'source_throttled'::text,
            v_retry_after,
            v_source_probe.slot,
            v_shard,
            'source_limit'::text,
            false,
            v_window_start;
        RETURN;
    END IF;

    SELECT bucket.* INTO v_aggregate
    FROM public.fs2_session_exchange_aggregate_buckets AS bucket
    WHERE bucket.shard = v_shard
      AND bucket.window_started_at = v_window_start;
    IF FOUND
       AND v_aggregate.admitted_count >= v_shard_quota
       AND v_aggregate.rejection_observed THEN
        RETURN QUERY SELECT
            'aggregate_throttled'::text,
            v_retry_after,
            least(v_slot_a, v_slot_b),
            v_shard,
            'aggregate_shard_limit'::text,
            false,
            v_window_start;
        RETURN;
    END IF;

    SELECT count(*) = 2 INTO v_source_collision
    FROM public.fs2_session_exchange_source_buckets AS bucket
    WHERE bucket.slot IN (v_slot_a, v_slot_b)
      AND bucket.window_started_at = v_window_start
      AND bucket.source_fingerprint IS NOT NULL
      AND bucket.source_fingerprint <> p_source_fingerprint;
    IF v_source_collision
       AND v_aggregate.window_started_at = v_window_start
       AND v_aggregate.collision_rejection_observed THEN
        RETURN QUERY SELECT
            'source_throttled'::text,
            v_retry_after,
            least(v_slot_a, v_slot_b),
            v_shard,
            'source_slot_collision'::text,
            false,
            v_window_start;
        RETURN;
    END IF;

    INSERT INTO public.fs2_session_exchange_aggregate_buckets(shard)
    VALUES (v_shard)
    ON CONFLICT (shard) DO NOTHING;

    SELECT bucket.* INTO v_aggregate
    FROM public.fs2_session_exchange_aggregate_buckets AS bucket
    WHERE bucket.shard = v_shard
    FOR UPDATE;

    IF v_aggregate.window_started_at > v_window_start THEN
        RAISE EXCEPTION 'session exchange aggregate bucket is ahead of the database clock';
    END IF;
    IF v_aggregate.window_started_at IS DISTINCT FROM v_window_start THEN
        UPDATE public.fs2_session_exchange_aggregate_buckets AS bucket
        SET window_started_at = v_window_start,
            admitted_count = 0,
            rejection_observed = false,
            first_rejected_at = NULL,
            collision_rejection_observed = false,
            first_collision_rejected_at = NULL,
            updated_at = v_now
        WHERE bucket.shard = v_shard
        RETURNING bucket.* INTO v_aggregate;
    END IF;

    IF v_aggregate.admitted_count >= v_shard_quota THEN
        v_emit := NOT v_aggregate.rejection_observed;
        IF v_emit THEN
            UPDATE public.fs2_session_exchange_aggregate_buckets AS bucket
            SET rejection_observed = true,
                first_rejected_at = v_now,
                updated_at = v_now
            WHERE bucket.shard = v_shard;
        END IF;
        RETURN QUERY SELECT
            'aggregate_throttled'::text,
            v_retry_after,
            least(v_slot_a, v_slot_b),
            v_shard,
            'aggregate_shard_limit'::text,
            v_emit,
            v_window_start;
        RETURN;
    END IF;

    INSERT INTO public.fs2_session_exchange_source_buckets(slot)
    VALUES (least(v_slot_a, v_slot_b)), (greatest(v_slot_a, v_slot_b))
    ON CONFLICT (slot) DO NOTHING;

    PERFORM bucket.slot
    FROM public.fs2_session_exchange_source_buckets AS bucket
    WHERE bucket.slot IN (v_slot_a, v_slot_b)
    ORDER BY bucket.slot
    FOR UPDATE;

    SELECT bucket.* INTO v_source
    FROM public.fs2_session_exchange_source_buckets AS bucket
    WHERE bucket.slot IN (v_slot_a, v_slot_b)
    ORDER BY
        CASE
            WHEN bucket.source_fingerprint = p_source_fingerprint THEN 0
            WHEN bucket.source_fingerprint IS NULL OR bucket.window_started_at < v_window_start THEN 1
            ELSE 2
        END,
        bucket.slot
    LIMIT 1;

    IF v_source.window_started_at > v_window_start THEN
        RAISE EXCEPTION 'session exchange source bucket is ahead of the database clock';
    END IF;
    v_source_collision := v_source.source_fingerprint IS NOT NULL
        AND v_source.source_fingerprint <> p_source_fingerprint
        AND v_source.window_started_at = v_window_start;

    IF NOT v_source_collision AND (
        v_source.source_fingerprint IS DISTINCT FROM p_source_fingerprint
        OR v_source.window_started_at IS DISTINCT FROM v_window_start
    ) THEN
        UPDATE public.fs2_session_exchange_source_buckets AS bucket
        SET source_fingerprint = p_source_fingerprint,
            window_started_at = v_window_start,
            admitted_count = 0,
            rejection_observed = false,
            first_rejected_at = NULL,
            updated_at = v_now
        WHERE bucket.slot = v_source.slot
        RETURNING bucket.* INTO v_source;
    END IF;

    IF NOT v_source_collision AND v_source.admitted_count >= p_maximum_source_attempts THEN
        v_emit := NOT v_source.rejection_observed;
        IF v_emit THEN
            UPDATE public.fs2_session_exchange_source_buckets AS bucket
            SET rejection_observed = true,
                first_rejected_at = v_now,
                updated_at = v_now
            WHERE bucket.slot = v_source.slot;
        END IF;
        RETURN QUERY SELECT
            'source_throttled'::text,
            v_retry_after,
            v_source.slot,
            v_shard,
            'source_limit'::text,
            v_emit,
            v_window_start;
        RETURN;
    END IF;

    IF v_source_collision THEN
        v_emit := NOT v_aggregate.collision_rejection_observed;
        IF v_emit THEN
            UPDATE public.fs2_session_exchange_aggregate_buckets AS bucket
            SET collision_rejection_observed = true,
                first_collision_rejected_at = v_now,
                updated_at = v_now
            WHERE bucket.shard = v_shard;
        END IF;
        RETURN QUERY SELECT
            'source_throttled'::text,
            v_retry_after,
            v_source.slot,
            v_shard,
            'source_slot_collision'::text,
            v_emit,
            v_window_start;
        RETURN;
    END IF;

    UPDATE public.fs2_session_exchange_aggregate_buckets AS bucket
    SET admitted_count = bucket.admitted_count + 1,
        updated_at = v_now
    WHERE bucket.shard = v_shard;
    UPDATE public.fs2_session_exchange_source_buckets AS bucket
    SET admitted_count = bucket.admitted_count + 1,
        updated_at = v_now
    WHERE bucket.slot = v_source.slot;

    RETURN QUERY SELECT
        'admitted'::text,
        v_retry_after,
        v_source.slot,
        v_shard,
        'admitted'::text,
        false,
        v_window_start;
END;
$$;

COMMENT ON FUNCTION fs2_consume_session_exchange(text,integer,integer,integer) IS
    'Bounded sharded fixed-window admission; established rejection states are read-only';

REVOKE ALL ON TABLE fs2_session_exchange_source_buckets FROM PUBLIC;
REVOKE ALL ON TABLE fs2_session_exchange_aggregate_buckets FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_consume_session_exchange(text,integer,integer,integer) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime') THEN
        GRANT EXECUTE ON FUNCTION fs2_consume_session_exchange(text,integer,integer,integer)
            TO fs2_serve_runtime;
    END IF;
END
$$;

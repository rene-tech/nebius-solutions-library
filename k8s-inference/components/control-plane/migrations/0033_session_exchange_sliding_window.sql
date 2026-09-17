-- Additive successor to the rejected fixed-window/sharded limiter in 0032.
-- The old tables and function remain as provenance, but runtime execution is
-- moved to this exact global sliding-window contract.
CREATE TABLE fs2_session_exchange_sliding_state (
    singleton smallint PRIMARY KEY DEFAULT 1 CHECK (singleton = 1),
    next_sequence bigint NOT NULL DEFAULT 0 CHECK (next_sequence >= 0),
    window_seconds integer CHECK (window_seconds BETWEEN 1 AND 3600),
    maximum_source_attempts integer CHECK (maximum_source_attempts BETWEEN 1 AND 100),
    maximum_aggregate_attempts integer CHECK (maximum_aggregate_attempts BETWEEN 10 AND 10000),
    aggregate_rejection_anchor bigint,
    aggregate_rejection_until timestamptz,
    aggregate_first_rejected_at timestamptz,
    CHECK (
        (aggregate_rejection_anchor IS NULL) = (aggregate_rejection_until IS NULL)
        AND (aggregate_rejection_anchor IS NULL) = (aggregate_first_rejected_at IS NULL)
    ),
    CHECK (
        (window_seconds IS NULL) = (maximum_source_attempts IS NULL)
        AND (window_seconds IS NULL) = (maximum_aggregate_attempts IS NULL)
        AND (
            maximum_aggregate_attempts IS NULL
            OR maximum_aggregate_attempts > maximum_source_attempts
        )
    )
);

INSERT INTO fs2_session_exchange_sliding_state(singleton) VALUES (1);

CREATE TABLE fs2_session_exchange_admissions (
    slot integer PRIMARY KEY CHECK (slot BETWEEN 0 AND 9999),
    sequence bigint NOT NULL UNIQUE CHECK (sequence > 0),
    source_fingerprint char(64) NOT NULL CHECK (
        source_fingerprint ~ '^[a-f0-9]{64}$'
    ),
    admitted_at timestamptz NOT NULL
);

COMMENT ON TABLE fs2_session_exchange_admissions IS
    'Exact last-10000 admitted exchange attempts; fixed slots are reused only after their event leaves the active sliding interval';

CREATE INDEX fs2_session_exchange_admissions_time_idx
    ON fs2_session_exchange_admissions (admitted_at, sequence);

CREATE INDEX fs2_session_exchange_admissions_source_time_idx
    ON fs2_session_exchange_admissions (source_fingerprint, admitted_at, sequence);

CREATE TABLE fs2_session_exchange_rejection_evidence (
    slot integer PRIMARY KEY CHECK (slot BETWEEN 0 AND 65535),
    source_fingerprint char(64) NOT NULL CHECK (
        source_fingerprint ~ '^[a-f0-9]{64}$'
    ),
    anchor_sequence bigint NOT NULL CHECK (anchor_sequence > 0),
    evidence_until timestamptz NOT NULL,
    first_rejected_at timestamptz NOT NULL,
    CHECK (evidence_until > first_rejected_at)
);

COMMENT ON TABLE fs2_session_exchange_rejection_evidence IS
    'Bounded coalesced source-rejection evidence only; collisions suppress evidence and never influence admission';

CREATE INDEX fs2_session_exchange_rejection_evidence_forensics_idx
    ON fs2_session_exchange_rejection_evidence
        (first_rejected_at DESC, slot)
    INCLUDE (source_fingerprint, anchor_sequence, evidence_until);

CREATE FUNCTION fs2_consume_session_exchange_sliding(
    p_source_fingerprint text,
    p_window_seconds integer,
    p_maximum_source_attempts integer,
    p_maximum_aggregate_attempts integer
)
RETURNS TABLE (
    admission text,
    retry_after_seconds double precision,
    admission_slot integer,
    evidence_slot integer,
    evidence_kind text,
    emit_audit boolean,
    decision_at timestamptz,
    cutoff_exclusive timestamptz,
    anchor_sequence bigint
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_cutoff timestamptz;
    v_interval interval;
    v_source_count integer;
    v_source_oldest timestamptz;
    v_source_anchor bigint;
    v_aggregate_count integer;
    v_aggregate_oldest timestamptz;
    v_aggregate_anchor bigint;
    v_source_evidence_slot integer;
    v_evidence fs2_session_exchange_rejection_evidence%ROWTYPE;
    v_state fs2_session_exchange_sliding_state%ROWTYPE;
    v_emit boolean := false;
    v_retry double precision;
    v_sequence bigint;
    v_admission_slot integer;
    v_replaced_at timestamptz;
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

    v_interval := make_interval(secs => p_window_seconds);
    v_cutoff := v_now - v_interval;
    v_source_evidence_slot := (
        get_byte(decode(p_source_fingerprint, 'hex'), 0)::bigint * 16777216
        + get_byte(decode(p_source_fingerprint, 'hex'), 1)::bigint * 65536
        + get_byte(decode(p_source_fingerprint, 'hex'), 2)::bigint * 256
        + get_byte(decode(p_source_fingerprint, 'hex'), 3)::bigint
    ) % 65536;

    -- Bind the ring to one configuration. Widening a window after events have
    -- already rolled out of a bounded ring cannot be exact, so configuration
    -- drift fails closed and requires an explicitly reviewed state transition.
    SELECT state.* INTO v_state
    FROM public.fs2_session_exchange_sliding_state AS state
    WHERE state.singleton = 1;
    IF v_state.window_seconds IS NULL THEN
        SELECT state.* INTO v_state
        FROM public.fs2_session_exchange_sliding_state AS state
        WHERE state.singleton = 1
        FOR UPDATE;
        IF v_state.window_seconds IS NULL THEN
            UPDATE public.fs2_session_exchange_sliding_state AS state
            SET window_seconds = p_window_seconds,
                maximum_source_attempts = p_maximum_source_attempts,
                maximum_aggregate_attempts = p_maximum_aggregate_attempts
            WHERE state.singleton = 1
            RETURNING state.* INTO v_state;
        END IF;
    END IF;
    IF v_state.window_seconds <> p_window_seconds
       OR v_state.maximum_source_attempts <> p_maximum_source_attempts
       OR v_state.maximum_aggregate_attempts <> p_maximum_aggregate_attempts THEN
        RAISE EXCEPTION 'session exchange limiter settings differ from bound state';
    END IF;

    -- Established source saturation is a read-only path. The evidence slot is
    -- consulted without a lock; an unexpired collision suppresses only the
    -- optional audit transition, never the admission decision.
    SELECT count(*)::integer, min(item.admitted_at), max(item.sequence)
    INTO v_source_count, v_source_oldest, v_source_anchor
    FROM public.fs2_session_exchange_admissions AS item
    WHERE item.source_fingerprint = p_source_fingerprint
      AND item.admitted_at > v_cutoff;
    IF v_source_count >= p_maximum_source_attempts THEN
        v_retry := greatest(
            0.001,
            extract(epoch FROM (v_source_oldest + v_interval - v_now))
        );
        SELECT evidence.* INTO v_evidence
        FROM public.fs2_session_exchange_rejection_evidence AS evidence
        WHERE evidence.slot = v_source_evidence_slot;
        IF FOUND AND (
            (
                v_evidence.source_fingerprint = p_source_fingerprint
                AND v_evidence.anchor_sequence = v_source_anchor
            )
            OR v_evidence.evidence_until > v_now
        ) THEN
            RETURN QUERY SELECT
                'source_throttled'::text, v_retry, NULL::integer,
                v_source_evidence_slot, 'source_limit'::text, false,
                v_now, v_cutoff, v_source_anchor;
            RETURN;
        END IF;
        v_emit := false;
        INSERT INTO public.fs2_session_exchange_rejection_evidence(
            slot, source_fingerprint, anchor_sequence, evidence_until, first_rejected_at
        ) VALUES (
            v_source_evidence_slot, p_source_fingerprint, v_source_anchor,
            v_source_oldest + v_interval, v_now
        )
        ON CONFLICT (slot) DO UPDATE
        SET source_fingerprint = EXCLUDED.source_fingerprint,
            anchor_sequence = EXCLUDED.anchor_sequence,
            evidence_until = EXCLUDED.evidence_until,
            first_rejected_at = EXCLUDED.first_rejected_at
        WHERE fs2_session_exchange_rejection_evidence.evidence_until <= EXCLUDED.first_rejected_at
        RETURNING true INTO v_emit;
        RETURN QUERY SELECT
            'source_throttled'::text, v_retry, NULL::integer,
            v_source_evidence_slot, 'source_limit'::text, coalesce(v_emit, false),
            v_now, v_cutoff, v_source_anchor;
        RETURN;
    END IF;

    -- The aggregate ceiling is one exact count over the same global interval;
    -- there are no source-derived shards or local quotas.
    SELECT count(*)::integer, min(item.admitted_at), max(item.sequence)
    INTO v_aggregate_count, v_aggregate_oldest, v_aggregate_anchor
    FROM public.fs2_session_exchange_admissions AS item
    WHERE item.admitted_at > v_cutoff;
    IF v_aggregate_count >= p_maximum_aggregate_attempts THEN
        v_retry := greatest(
            0.001,
            extract(epoch FROM (v_aggregate_oldest + v_interval - v_now))
        );
        SELECT state.* INTO v_state
        FROM public.fs2_session_exchange_sliding_state AS state
        WHERE state.singleton = 1;
        IF v_state.aggregate_rejection_anchor = v_aggregate_anchor
           OR v_state.aggregate_rejection_until > v_now THEN
            RETURN QUERY SELECT
                'aggregate_throttled'::text, v_retry, NULL::integer, 0,
                'aggregate_limit'::text, false, v_now, v_cutoff, v_aggregate_anchor;
            RETURN;
        END IF;
        v_emit := false;
        UPDATE public.fs2_session_exchange_sliding_state AS state
        SET aggregate_rejection_anchor = v_aggregate_anchor,
            aggregate_rejection_until = v_aggregate_oldest + v_interval,
            aggregate_first_rejected_at = v_now
        WHERE state.singleton = 1
          AND (
              state.aggregate_rejection_until IS NULL
              OR state.aggregate_rejection_until <= v_now
          )
        RETURNING true INTO v_emit;
        RETURN QUERY SELECT
            'aggregate_throttled'::text, v_retry, NULL::integer, 0,
            'aggregate_limit'::text, coalesce(v_emit, false),
            v_now, v_cutoff, v_aggregate_anchor;
        RETURN;
    END IF;

    -- Only a request that can still be admitted reaches this cursor lock.
    -- Re-check against a fresh database clock after acquiring it so concurrent
    -- contenders cannot exceed either exact sliding-window ceiling.
    SELECT state.* INTO v_state
    FROM public.fs2_session_exchange_sliding_state AS state
    WHERE state.singleton = 1
    FOR UPDATE;
    v_now := clock_timestamp();
    v_cutoff := v_now - v_interval;

    SELECT count(*)::integer, min(item.admitted_at), max(item.sequence)
    INTO v_source_count, v_source_oldest, v_source_anchor
    FROM public.fs2_session_exchange_admissions AS item
    WHERE item.source_fingerprint = p_source_fingerprint
      AND item.admitted_at > v_cutoff;
    IF v_source_count >= p_maximum_source_attempts THEN
        v_retry := greatest(
            0.001,
            extract(epoch FROM (v_source_oldest + v_interval - v_now))
        );
        v_emit := false;
        INSERT INTO public.fs2_session_exchange_rejection_evidence(
            slot, source_fingerprint, anchor_sequence, evidence_until, first_rejected_at
        ) VALUES (
            v_source_evidence_slot, p_source_fingerprint, v_source_anchor,
            v_source_oldest + v_interval, v_now
        )
        ON CONFLICT (slot) DO UPDATE
        SET source_fingerprint = EXCLUDED.source_fingerprint,
            anchor_sequence = EXCLUDED.anchor_sequence,
            evidence_until = EXCLUDED.evidence_until,
            first_rejected_at = EXCLUDED.first_rejected_at
        WHERE fs2_session_exchange_rejection_evidence.evidence_until <= EXCLUDED.first_rejected_at
        RETURNING true INTO v_emit;
        RETURN QUERY SELECT
            'source_throttled'::text, v_retry, NULL::integer,
            v_source_evidence_slot, 'source_limit'::text, coalesce(v_emit, false),
            v_now, v_cutoff, v_source_anchor;
        RETURN;
    END IF;

    SELECT count(*)::integer, min(item.admitted_at), max(item.sequence)
    INTO v_aggregate_count, v_aggregate_oldest, v_aggregate_anchor
    FROM public.fs2_session_exchange_admissions AS item
    WHERE item.admitted_at > v_cutoff;
    IF v_aggregate_count >= p_maximum_aggregate_attempts THEN
        v_retry := greatest(
            0.001,
            extract(epoch FROM (v_aggregate_oldest + v_interval - v_now))
        );
        v_emit := v_state.aggregate_rejection_until IS NULL
            OR v_state.aggregate_rejection_until <= v_now;
        IF v_emit THEN
            UPDATE public.fs2_session_exchange_sliding_state AS state
            SET aggregate_rejection_anchor = v_aggregate_anchor,
                aggregate_rejection_until = v_aggregate_oldest + v_interval,
                aggregate_first_rejected_at = v_now
            WHERE state.singleton = 1;
        END IF;
        RETURN QUERY SELECT
            'aggregate_throttled'::text, v_retry, NULL::integer, 0,
            'aggregate_limit'::text, v_emit, v_now, v_cutoff, v_aggregate_anchor;
        RETURN;
    END IF;

    v_sequence := v_state.next_sequence + 1;
    v_admission_slot := (v_sequence - 1) % 10000;
    SELECT item.admitted_at INTO v_replaced_at
    FROM public.fs2_session_exchange_admissions AS item
    WHERE item.slot = v_admission_slot;
    IF FOUND AND v_replaced_at > v_cutoff THEN
        RAISE EXCEPTION 'session exchange admission ring would overwrite active state';
    END IF;

    INSERT INTO public.fs2_session_exchange_admissions(
        slot, sequence, source_fingerprint, admitted_at
    ) VALUES (
        v_admission_slot, v_sequence, p_source_fingerprint, v_now
    )
    ON CONFLICT (slot) DO UPDATE
    SET sequence = EXCLUDED.sequence,
        source_fingerprint = EXCLUDED.source_fingerprint,
        admitted_at = EXCLUDED.admitted_at;
    UPDATE public.fs2_session_exchange_sliding_state AS state
    SET next_sequence = v_sequence
    WHERE state.singleton = 1;

    RETURN QUERY SELECT
        'admitted'::text, 0.0::double precision, v_admission_slot,
        NULL::integer, 'admitted'::text, false,
        v_now, v_cutoff, v_sequence;
END;
$$;

COMMENT ON FUNCTION fs2_consume_session_exchange_sliding(text,integer,integer,integer) IS
    'Bounded exact global sliding-window admission with read-only established rejection paths';

REVOKE ALL ON TABLE fs2_session_exchange_sliding_state FROM PUBLIC;
REVOKE ALL ON TABLE fs2_session_exchange_admissions FROM PUBLIC;
REVOKE ALL ON TABLE fs2_session_exchange_rejection_evidence FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_consume_session_exchange_sliding(text,integer,integer,integer) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime') THEN
        REVOKE EXECUTE ON FUNCTION fs2_consume_session_exchange(text,integer,integer,integer)
            FROM fs2_serve_runtime;
        GRANT EXECUTE ON FUNCTION fs2_consume_session_exchange_sliding(text,integer,integer,integer)
            TO fs2_serve_runtime;
    END IF;
END
$$;

-- Mixed-version bridge for the rejected 0032 -> 0033 cutover.  The migration
-- runner publishes the last schema version which existed before this release
-- transaction.  Missing provenance is treated as an upgrade and therefore
-- takes the fail-closed path.
ALTER FUNCTION fs2_consume_session_exchange_sliding(text,integer,integer,integer)
    RENAME TO fs2_consume_session_exchange_exact_v2;

CREATE TABLE fs2_session_exchange_cutover_state (
    singleton smallint PRIMARY KEY DEFAULT 1 CHECK (singleton = 1),
    migration_started_at timestamptz NOT NULL,
    prior_schema_version text,
    cutover_required boolean NOT NULL,
    window_seconds integer CHECK (window_seconds BETWEEN 1 AND 3600),
    maximum_source_attempts integer CHECK (maximum_source_attempts BETWEEN 1 AND 100),
    maximum_aggregate_attempts integer CHECK (maximum_aggregate_attempts BETWEEN 10 AND 10000),
    cutover_not_before timestamptz,
    CHECK (
        (window_seconds IS NULL) = (maximum_source_attempts IS NULL)
        AND (window_seconds IS NULL) = (maximum_aggregate_attempts IS NULL)
        AND (window_seconds IS NULL) = (cutover_not_before IS NULL)
        AND (
            maximum_aggregate_attempts IS NULL
            OR maximum_aggregate_attempts > maximum_source_attempts
        )
    )
);

INSERT INTO fs2_session_exchange_cutover_state(
    singleton,
    migration_started_at,
    prior_schema_version,
    cutover_required,
    window_seconds,
    maximum_source_attempts,
    maximum_aggregate_attempts,
    cutover_not_before
)
SELECT
    1,
    clock_timestamp(),
    nullif(current_setting('fs2.preexisting_schema_version', true), '__fresh__'),
    coalesce(current_setting('fs2.preexisting_schema_version', true), '') <> '__fresh__',
    state.window_seconds,
    state.maximum_source_attempts,
    state.maximum_aggregate_attempts,
    CASE
        WHEN state.window_seconds IS NULL THEN NULL
        WHEN coalesce(current_setting('fs2.preexisting_schema_version', true), '') <> '__fresh__'
        THEN clock_timestamp() + make_interval(secs => state.window_seconds)
        ELSE clock_timestamp()
    END
FROM fs2_session_exchange_sliding_state AS state
WHERE state.singleton = 1;

COMMENT ON TABLE fs2_session_exchange_cutover_state IS
    'Immutable-origin fail-closed bridge: every preexisting database quiesces for one complete bound window before exact-v2 admission';

CREATE FUNCTION fs2_consume_session_exchange_bridge(
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
    v_state fs2_session_exchange_cutover_state%ROWTYPE;
    v_anchor bigint;
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

    SELECT state.* INTO STRICT v_state
    FROM public.fs2_session_exchange_cutover_state AS state
    WHERE state.singleton = 1;

    IF v_state.window_seconds IS NULL THEN
        SELECT state.* INTO STRICT v_state
        FROM public.fs2_session_exchange_cutover_state AS state
        WHERE state.singleton = 1
        FOR UPDATE;
        IF v_state.window_seconds IS NULL THEN
            UPDATE public.fs2_session_exchange_cutover_state AS state
            SET window_seconds = p_window_seconds,
                maximum_source_attempts = p_maximum_source_attempts,
                maximum_aggregate_attempts = p_maximum_aggregate_attempts,
                cutover_not_before = CASE
                    WHEN state.cutover_required
                    THEN state.migration_started_at + make_interval(secs => p_window_seconds)
                    ELSE state.migration_started_at
                END
            WHERE state.singleton = 1
            RETURNING state.* INTO STRICT v_state;
        END IF;
    END IF;

    IF v_state.window_seconds <> p_window_seconds
       OR v_state.maximum_source_attempts <> p_maximum_source_attempts
       OR v_state.maximum_aggregate_attempts <> p_maximum_aggregate_attempts THEN
        RAISE EXCEPTION 'session exchange cutover settings differ from bound state';
    END IF;

    -- Both legacy and exact-v2 callers reach this read-only response.  A full
    -- configured interval must pass after the bridge transaction before an
    -- empty exact ring can admit anything, conservatively preserving every
    -- possible active preexisting attempt without pretending it can be backfilled.
    IF v_now < v_state.cutover_not_before THEN
        SELECT state.next_sequence INTO v_anchor
        FROM public.fs2_session_exchange_sliding_state AS state
        WHERE state.singleton = 1;
        RETURN QUERY SELECT
            'aggregate_throttled'::text,
            greatest(0.001, extract(epoch FROM (v_state.cutover_not_before - v_now))),
            NULL::integer,
            0,
            'cutover_quiescence'::text,
            false,
            v_now,
            v_now - make_interval(secs => p_window_seconds),
            coalesce(v_anchor, 0);
        RETURN;
    END IF;

    RETURN QUERY
    SELECT * FROM public.fs2_consume_session_exchange_exact_v2(
        p_source_fingerprint,
        p_window_seconds,
        p_maximum_source_attempts,
        p_maximum_aggregate_attempts
    );
END;
$$;

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
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT * FROM public.fs2_consume_session_exchange_bridge($1,$2,$3,$4)
$$;

CREATE OR REPLACE FUNCTION fs2_consume_session_exchange(
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
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT
        decision.admission,
        decision.retry_after_seconds,
        coalesce(decision.admission_slot, decision.evidence_slot, 0),
        0::smallint,
        decision.evidence_kind,
        decision.emit_audit,
        decision.cutoff_exclusive
    FROM public.fs2_consume_session_exchange_bridge($1,$2,$3,$4) AS decision
$$;

COMMENT ON FUNCTION fs2_consume_session_exchange_bridge(text,integer,integer,integer) IS
    'Shared fail-closed mixed-version bridge followed by exact global sliding-window admission';
COMMENT ON FUNCTION fs2_consume_session_exchange_sliding(text,integer,integer,integer) IS
    'Exact-v2 caller compatibility wrapper over the shared fail-closed bridge';
COMMENT ON FUNCTION fs2_consume_session_exchange(text,integer,integer,integer) IS
    'Legacy caller compatibility wrapper over the shared fail-closed bridge';

REVOKE ALL ON TABLE fs2_session_exchange_cutover_state FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_consume_session_exchange_exact_v2(text,integer,integer,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_consume_session_exchange_bridge(text,integer,integer,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_consume_session_exchange_sliding(text,integer,integer,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_consume_session_exchange(text,integer,integer,integer) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime') THEN
        GRANT EXECUTE ON FUNCTION fs2_consume_session_exchange_sliding(text,integer,integer,integer)
            TO fs2_serve_runtime;
        GRANT EXECUTE ON FUNCTION fs2_consume_session_exchange(text,integer,integer,integer)
            TO fs2_serve_runtime;
    END IF;
END
$$;

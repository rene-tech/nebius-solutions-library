-- Mixed-version bridge for the rejected 0032 -> 0033 cutover.  The migration
-- runner publishes the last schema version which existed before this release
-- transaction.  Missing provenance is treated as an upgrade and therefore
-- takes the fail-closed path.
ALTER FUNCTION fs2_consume_session_exchange_sliding(text,integer,integer,integer)
    RENAME TO fs2_consume_session_exchange_exact_v2;

CREATE TABLE fs2_session_exchange_cutover_state (
    singleton smallint PRIMARY KEY DEFAULT 1 CHECK (singleton = 1),
    migration_recorded_at timestamptz NOT NULL,
    prior_schema_version text,
    cutover_required boolean NOT NULL,
    first_bridge_at timestamptz,
    legacy_admissions_imported boolean NOT NULL DEFAULT false,
    window_seconds integer CHECK (window_seconds BETWEEN 1 AND 3600),
    maximum_source_attempts integer CHECK (maximum_source_attempts BETWEEN 1 AND 100),
    maximum_aggregate_attempts integer CHECK (maximum_aggregate_attempts BETWEEN 10 AND 10000),
    CHECK (
        (window_seconds IS NULL) = (maximum_source_attempts IS NULL)
        AND (window_seconds IS NULL) = (maximum_aggregate_attempts IS NULL)
        AND (window_seconds IS NULL) = (first_bridge_at IS NULL)
        AND (NOT legacy_admissions_imported OR first_bridge_at IS NOT NULL)
        AND (
            maximum_aggregate_attempts IS NULL
            OR maximum_aggregate_attempts > maximum_source_attempts
        )
    )
);

INSERT INTO fs2_session_exchange_cutover_state(
    singleton,
    migration_recorded_at,
    prior_schema_version,
    cutover_required
) VALUES (
    1,
    clock_timestamp(),
    nullif(current_setting('fs2.preexisting_schema_version', true), '__fresh__'),
    coalesce(current_setting('fs2.preexisting_schema_version', true), '') <> '__fresh__'
);

COMMENT ON TABLE fs2_session_exchange_cutover_state IS
    'First-post-commit bridge state, visible-current legacy import, and conservative unknown-tail fence';

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
    v_state fs2_session_exchange_cutover_state%ROWTYPE;
    v_sliding_state fs2_session_exchange_sliding_state%ROWTYPE;
    v_first_bridge_at timestamptz;
    v_current_window_start timestamptz;
    v_unknown_tail_until timestamptz;
    v_decision_at timestamptz;
    v_interval interval;
    v_legacy_source_count integer;
    v_legacy_aggregate_count integer;
    v_existing_exact_count integer;
    v_imported_count integer := 0;
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

    -- The migration transaction never starts the compatibility transition.
    -- The first runtime call after commit serializes the visible-current
    -- import and anchors the conservative prior-tail fence.
    IF v_state.first_bridge_at IS NULL THEN
        SELECT state.* INTO STRICT v_state
        FROM public.fs2_session_exchange_cutover_state AS state
        WHERE state.singleton = 1
        FOR UPDATE;
        IF v_state.first_bridge_at IS NULL THEN
            v_first_bridge_at := clock_timestamp();
            v_interval := make_interval(secs => p_window_seconds);
            v_current_window_start := to_timestamp(
                floor(extract(epoch FROM v_first_bridge_at) / p_window_seconds)
                * p_window_seconds
            );

            SELECT state.* INTO STRICT v_sliding_state
            FROM public.fs2_session_exchange_sliding_state AS state
            WHERE state.singleton = 1
            FOR UPDATE;
            IF v_sliding_state.window_seconds IS NOT NULL AND (
                v_sliding_state.window_seconds <> p_window_seconds
                OR v_sliding_state.maximum_source_attempts <> p_maximum_source_attempts
                OR v_sliding_state.maximum_aggregate_attempts <> p_maximum_aggregate_attempts
            ) THEN
                RAISE EXCEPTION 'session exchange cutover settings differ from exact-v2 state';
            END IF;

            UPDATE public.fs2_session_exchange_sliding_state AS state
            SET window_seconds = p_window_seconds,
                maximum_source_attempts = p_maximum_source_attempts,
                maximum_aggregate_attempts = p_maximum_aggregate_attempts
            WHERE state.singleton = 1
              AND state.window_seconds IS NULL
            RETURNING state.* INTO v_sliding_state;
            IF NOT FOUND THEN
                SELECT state.* INTO STRICT v_sliding_state
                FROM public.fs2_session_exchange_sliding_state AS state
                WHERE state.singleton = 1;
            END IF;

            IF v_state.cutover_required THEN
                -- 0032 retained only one aligned bucket per source/aggregate
                -- slot. A pre-commit call in the current aligned window can
                -- overwrite the previous bucket even though its tail remains
                -- active under exact sliding semantics. Import the visible
                -- current bucket and conservatively fence all admission until
                -- that unobservable previous tail must have expired.
                SELECT coalesce(sum(bucket.admitted_count), 0)::integer
                INTO v_legacy_source_count
                FROM public.fs2_session_exchange_source_buckets AS bucket
                WHERE bucket.source_fingerprint IS NOT NULL
                  AND bucket.window_started_at = v_current_window_start;

                SELECT coalesce(sum(bucket.admitted_count), 0)::integer
                INTO v_legacy_aggregate_count
                FROM public.fs2_session_exchange_aggregate_buckets AS bucket
                WHERE bucket.window_started_at = v_current_window_start;

                SELECT count(*)::integer INTO v_existing_exact_count
                FROM public.fs2_session_exchange_admissions AS admission
                WHERE admission.admitted_at > v_first_bridge_at - v_interval;

                IF v_legacy_source_count <> v_legacy_aggregate_count THEN
                    RAISE EXCEPTION 'legacy session exchange source and aggregate budgets disagree';
                END IF;
                IF v_legacy_aggregate_count + v_existing_exact_count > p_maximum_aggregate_attempts
                   OR v_legacy_aggregate_count + v_existing_exact_count > 10000 THEN
                    RAISE EXCEPTION 'legacy session exchange budget exceeds exact-v2 capacity';
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM public.fs2_session_exchange_source_buckets AS bucket
                    WHERE bucket.source_fingerprint IS NOT NULL
                      AND bucket.window_started_at = v_current_window_start
                      AND bucket.admitted_count > p_maximum_source_attempts
                ) THEN
                    RAISE EXCEPTION 'legacy session exchange source budget exceeds exact-v2 capacity';
                END IF;

                WITH legacy_admissions AS (
                    SELECT
                        bucket.source_fingerprint,
                        row_number() OVER (
                            ORDER BY bucket.source_fingerprint, attempt.ordinal
                        )::bigint + v_sliding_state.next_sequence AS sequence
                    FROM public.fs2_session_exchange_source_buckets AS bucket
                    CROSS JOIN LATERAL generate_series(1, bucket.admitted_count) AS attempt(ordinal)
                    WHERE bucket.source_fingerprint IS NOT NULL
                      AND bucket.window_started_at = v_current_window_start
                )
                INSERT INTO public.fs2_session_exchange_admissions(
                    slot,
                    sequence,
                    source_fingerprint,
                    admitted_at
                )
                SELECT
                    ((legacy.sequence - 1) % 10000)::integer,
                    legacy.sequence,
                    legacy.source_fingerprint,
                    v_first_bridge_at
                FROM legacy_admissions AS legacy
                ORDER BY legacy.sequence
                ON CONFLICT (slot) DO UPDATE
                SET sequence = EXCLUDED.sequence,
                    source_fingerprint = EXCLUDED.source_fingerprint,
                    admitted_at = EXCLUDED.admitted_at;
                GET DIAGNOSTICS v_imported_count = ROW_COUNT;

                IF v_imported_count <> v_legacy_aggregate_count THEN
                    RAISE EXCEPTION 'legacy session exchange budget import was incomplete';
                END IF;
                UPDATE public.fs2_session_exchange_sliding_state AS state
                SET next_sequence = state.next_sequence + v_imported_count
                WHERE state.singleton = 1;
            END IF;

            UPDATE public.fs2_session_exchange_cutover_state AS state
            SET window_seconds = p_window_seconds,
                maximum_source_attempts = p_maximum_source_attempts,
                maximum_aggregate_attempts = p_maximum_aggregate_attempts,
                first_bridge_at = v_first_bridge_at,
                legacy_admissions_imported = state.cutover_required
            WHERE state.singleton = 1
            RETURNING state.* INTO STRICT v_state;
        END IF;
    END IF;

    IF v_state.window_seconds <> p_window_seconds
       OR v_state.maximum_source_attempts <> p_maximum_source_attempts
       OR v_state.maximum_aggregate_attempts <> p_maximum_aggregate_attempts THEN
        RAISE EXCEPTION 'session exchange cutover settings differ from bound state';
    END IF;

    IF v_state.cutover_required THEN
        -- The previous aligned bucket can contain an attempt immediately
        -- before this window started. It is certainly expired at the next
        -- aligned boundary. Until then the bridge is read-only and fail
        -- closed; visible current-window attempts remain in exact-v2 and
        -- continue to consume budget after the fence opens.
        v_interval := make_interval(secs => p_window_seconds);
        v_current_window_start := to_timestamp(
            floor(extract(epoch FROM v_state.first_bridge_at) / p_window_seconds)
            * p_window_seconds
        );
        v_unknown_tail_until := v_current_window_start + v_interval;
        v_decision_at := clock_timestamp();
        IF v_decision_at < v_unknown_tail_until THEN
            RETURN QUERY SELECT
                'aggregate_throttled'::text,
                greatest(
                    extract(epoch FROM (v_unknown_tail_until - v_decision_at)),
                    0.001
                )::double precision,
                NULL::integer,
                NULL::integer,
                'legacy_cutover_tail_fence'::text,
                false,
                v_decision_at,
                v_current_window_start,
                NULL::bigint;
            RETURN;
        END IF;
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
DECLARE
    known_role record;
    runtime_member record;
BEGIN
    FOR known_role IN
        SELECT role.rolname
        FROM pg_roles AS role
        WHERE role.rolname IN (
            'fs2_serve_runtime',
            'fs2_serve_reporting',
            'fs2_serve_maintenance',
            'fs2_serve_activation'
        )
    LOOP
        EXECUTE format(
            'REVOKE EXECUTE ON FUNCTION public.fs2_consume_session_exchange_exact_v2(text,integer,integer,integer) FROM %I',
            known_role.rolname
        );
        EXECUTE format(
            'REVOKE EXECUTE ON FUNCTION public.fs2_consume_session_exchange_bridge(text,integer,integer,integer) FROM %I',
            known_role.rolname
        );
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime') THEN
        -- ALTER FUNCTION ... RENAME preserves ACLs. Close both the inherited
        -- group grant and any direct member grants before exposing wrappers.
        FOR runtime_member IN
            SELECT member_role.rolname
            FROM pg_auth_members AS membership
            JOIN pg_roles AS group_role ON group_role.oid = membership.roleid
            JOIN pg_roles AS member_role ON member_role.oid = membership.member
            WHERE group_role.rolname = 'fs2_serve_runtime'
              AND member_role.rolname <> current_user
        LOOP
            EXECUTE format(
                'REVOKE EXECUTE ON FUNCTION public.fs2_consume_session_exchange_exact_v2(text,integer,integer,integer) FROM %I',
                runtime_member.rolname
            );
            EXECUTE format(
                'REVOKE EXECUTE ON FUNCTION public.fs2_consume_session_exchange_bridge(text,integer,integer,integer) FROM %I',
                runtime_member.rolname
            );
        END LOOP;
        GRANT EXECUTE ON FUNCTION fs2_consume_session_exchange_sliding(text,integer,integer,integer)
            TO fs2_serve_runtime;
        GRANT EXECUTE ON FUNCTION fs2_consume_session_exchange(text,integer,integer,integer)
            TO fs2_serve_runtime;
    END IF;
END
$$;

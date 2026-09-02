-- 0014 and 0015 are reserved for the independently integrated scientific
-- artifact and batch-controller migrations. This additive ledger deliberately
-- follows both so those owners can rebase without renumbering immutable DDL.

CREATE TABLE fs2_telemetry_subjects (
    subject_id uuid PRIMARY KEY,
    workload_kind text NOT NULL CHECK (workload_kind IN ('online','scientific_batch')),
    operation_id uuid,
    request_id uuid NOT NULL,
    batch_id uuid,
    workload_id uuid NOT NULL,
    attempt_id uuid,
    tenant_id text NOT NULL CHECK (
        length(tenant_id) BETWEEN 1 AND 120
        AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    ),
    principal_id text NOT NULL CHECK (length(principal_id) BETWEEN 1 AND 200),
    api_key_id uuid,
    api_key_fingerprint char(64) CHECK (
        api_key_fingerprint IS NULL OR api_key_fingerprint ~ '^[a-f0-9]{64}$'
    ),
    model_id text NOT NULL CHECK (length(model_id) BETWEEN 1 AND 128),
    model_revision text NOT NULL CHECK (length(model_revision) BETWEEN 1 AND 256),
    protocol text NOT NULL CHECK (length(protocol) BETWEEN 1 AND 64),
    trace_id char(32) CHECK (trace_id IS NULL OR trace_id ~ '^[a-f0-9]{32}$'),
    parent_span_id char(16) CHECK (parent_span_id IS NULL OR parent_span_id ~ '^[a-f0-9]{16}$'),
    accepted_at timestamptz NOT NULL,
    input_shape jsonb NOT NULL CHECK (
        jsonb_typeof(input_shape)='object' AND octet_length(input_shape::text) BETWEEN 2 AND 262144
    ),
    parameter_digest text CHECK (
        parameter_digest IS NULL OR parameter_digest ~ '^(sha256:)?[a-f0-9]{64}$'
    ),
    artifact_references jsonb NOT NULL CHECK (
        jsonb_typeof(artifact_references)='array'
        AND jsonb_array_length(artifact_references) <= 128
        AND octet_length(artifact_references::text) BETWEEN 2 AND 262144
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((api_key_id IS NULL) = (api_key_fingerprint IS NULL)),
    CHECK (
        (workload_kind='online' AND operation_id IS NOT NULL AND batch_id IS NULL AND attempt_id IS NULL)
        OR (
            workload_kind='scientific_batch' AND operation_id IS NOT NULL
            AND batch_id IS NOT NULL AND attempt_id IS NOT NULL
        )
    )
);

CREATE UNIQUE INDEX fs2_telemetry_subjects_online_operation_idx
    ON fs2_telemetry_subjects (operation_id) WHERE workload_kind='online';
CREATE UNIQUE INDEX fs2_telemetry_subjects_batch_attempt_idx
    ON fs2_telemetry_subjects (attempt_id) WHERE workload_kind='scientific_batch';
CREATE INDEX fs2_telemetry_subjects_admin_idx
    ON fs2_telemetry_subjects (tenant_id,accepted_at DESC,subject_id DESC);
CREATE INDEX fs2_telemetry_subjects_workload_idx
    ON fs2_telemetry_subjects (workload_id,attempt_id);
CREATE INDEX fs2_telemetry_subjects_trace_idx
    ON fs2_telemetry_subjects (trace_id) WHERE trace_id IS NOT NULL;

CREATE TABLE fs2_telemetry_correlations (
    correlation_key text PRIMARY KEY CHECK (
        length(correlation_key) BETWEEN 1 AND 512
        AND correlation_key ~ '^[A-Za-z0-9][A-Za-z0-9_.:/@+-]*$'
    ),
    subject_id uuid NOT NULL REFERENCES fs2_telemetry_subjects(subject_id),
    observed_at timestamptz NOT NULL,
    source text NOT NULL CHECK (
        source IN ('application','controller','kueue','kubernetes','kubelet','dcgm','derived')
    ),
    attempt integer NOT NULL CHECK (attempt BETWEEN 0 AND 1024),
    cluster text CHECK (cluster IS NULL OR length(cluster) BETWEEN 1 AND 128),
    namespace text CHECK (namespace IS NULL OR length(namespace) BETWEEN 1 AND 63),
    queue_name text CHECK (queue_name IS NULL OR length(queue_name) BETWEEN 1 AND 253),
    kueue_workload_name text CHECK (
        kueue_workload_name IS NULL OR length(kueue_workload_name) BETWEEN 1 AND 253
    ),
    kueue_workload_uid text CHECK (
        kueue_workload_uid IS NULL OR length(kueue_workload_uid) BETWEEN 1 AND 128
    ),
    job_name text CHECK (job_name IS NULL OR length(job_name) BETWEEN 1 AND 253),
    job_uid text CHECK (job_uid IS NULL OR length(job_uid) BETWEEN 1 AND 128),
    pod_name text CHECK (pod_name IS NULL OR length(pod_name) BETWEEN 1 AND 253),
    pod_uid text CHECK (pod_uid IS NULL OR length(pod_uid) BETWEEN 1 AND 128),
    node_name text CHECK (node_name IS NULL OR length(node_name) BETWEEN 1 AND 253),
    node_uid text CHECK (node_uid IS NULL OR length(node_uid) BETWEEN 1 AND 128),
    gpu_uuid text CHECK (
        gpu_uuid IS NULL OR gpu_uuid ~ '^(GPU|MIG)-[A-Za-z0-9_.:/-]{1,123}$'
    ),
    gpu_rank integer CHECK (gpu_rank IS NULL OR gpu_rank BETWEEN 0 AND 1023),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((gpu_uuid IS NULL) = (gpu_rank IS NULL)),
    CHECK (gpu_uuid IS NULL OR pod_uid IS NOT NULL),
    CHECK (
        queue_name IS NOT NULL OR kueue_workload_uid IS NOT NULL OR job_uid IS NOT NULL
        OR pod_uid IS NOT NULL OR node_uid IS NOT NULL OR gpu_uuid IS NOT NULL
    )
);

CREATE INDEX fs2_telemetry_correlations_subject_idx
    ON fs2_telemetry_correlations (subject_id,observed_at,correlation_key);
CREATE INDEX fs2_telemetry_correlations_pod_gpu_idx
    ON fs2_telemetry_correlations (pod_uid,gpu_uuid,gpu_rank) WHERE pod_uid IS NOT NULL;
CREATE INDEX fs2_telemetry_correlations_kueue_idx
    ON fs2_telemetry_correlations (kueue_workload_uid) WHERE kueue_workload_uid IS NOT NULL;

CREATE TABLE fs2_lifecycle_signals (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_key text NOT NULL UNIQUE CHECK (
        length(event_key) BETWEEN 1 AND 512
        AND event_key ~ '^[A-Za-z0-9][A-Za-z0-9_.:/@+-]*$'
    ),
    subject_id uuid NOT NULL REFERENCES fs2_telemetry_subjects(subject_id),
    occurred_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    source text NOT NULL CHECK (
        source IN ('application','controller','kueue','kubernetes','kubelet','dcgm','derived')
    ),
    source_resolution_seconds double precision NOT NULL CHECK (
        source_resolution_seconds >= 0 AND source_resolution_seconds <= 300
        AND source_resolution_seconds <> 'NaN'::double precision
    ),
    quality text NOT NULL CHECK (
        quality IN ('measured','application_observed','estimated','unavailable')
    ),
    phase text NOT NULL CHECK (
        phase IN (
            'receive','enqueue','admission_wait','admit','node_request','node_ready','image_pull',
            'artifact_load','restore','compile','container_ready','runtime_ready','warmup',
            'gpu_allocation','active_compute','workflow_wait','resident_idle','cooldown_grace',
            'checkpoint_drain','preemption','retry','teardown','release','unclassified'
        )
    ),
    edge text NOT NULL CHECK (edge IN ('start','end','instant')),
    clock text NOT NULL CHECK (
        clock IN ('lifecycle','quota_reserved','scheduler_occupied','device_allocated','phase')
    ),
    interval_key text CHECK (
        interval_key IS NULL OR (
            length(interval_key) BETWEEN 1 AND 512
            AND interval_key ~ '^[A-Za-z0-9][A-Za-z0-9_.:/@+-]*$'
        )
    ),
    attempt integer NOT NULL CHECK (attempt BETWEEN 0 AND 1024),
    gpu_count integer NOT NULL CHECK (gpu_count BETWEEN 0 AND 1024),
    cluster text CHECK (cluster IS NULL OR length(cluster) BETWEEN 1 AND 128),
    namespace text CHECK (namespace IS NULL OR length(namespace) BETWEEN 1 AND 63),
    queue_name text CHECK (queue_name IS NULL OR length(queue_name) BETWEEN 1 AND 253),
    kueue_workload_name text CHECK (
        kueue_workload_name IS NULL OR length(kueue_workload_name) BETWEEN 1 AND 253
    ),
    kueue_workload_uid text CHECK (
        kueue_workload_uid IS NULL OR length(kueue_workload_uid) BETWEEN 1 AND 128
    ),
    job_name text CHECK (job_name IS NULL OR length(job_name) BETWEEN 1 AND 253),
    job_uid text CHECK (job_uid IS NULL OR length(job_uid) BETWEEN 1 AND 128),
    pod_name text CHECK (pod_name IS NULL OR length(pod_name) BETWEEN 1 AND 253),
    pod_uid text CHECK (pod_uid IS NULL OR length(pod_uid) BETWEEN 1 AND 128),
    node_name text CHECK (node_name IS NULL OR length(node_name) BETWEEN 1 AND 253),
    node_uid text CHECK (node_uid IS NULL OR length(node_uid) BETWEEN 1 AND 128),
    gpu_uuid text CHECK (
        gpu_uuid IS NULL OR gpu_uuid ~ '^(GPU|MIG)-[A-Za-z0-9_.:/-]{1,123}$'
    ),
    gpu_rank integer CHECK (gpu_rank IS NULL OR gpu_rank BETWEEN 0 AND 1023),
    detail jsonb NOT NULL CHECK (
        jsonb_typeof(detail)='object' AND octet_length(detail::text) BETWEEN 2 AND 8192
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (observed_at >= occurred_at),
    CHECK (
        (edge='instant' AND interval_key IS NULL)
        OR (edge IN ('start','end') AND interval_key IS NOT NULL)
    ),
    CHECK ((gpu_uuid IS NULL) = (gpu_rank IS NULL)),
    CHECK (gpu_uuid IS NULL OR gpu_count=1),
    CHECK (clock<>'device_allocated' OR (pod_uid IS NOT NULL AND gpu_uuid IS NOT NULL AND gpu_count=1)),
    CHECK (clock NOT IN ('quota_reserved','scheduler_occupied') OR edge='instant' OR gpu_count>0),
    CHECK (clock<>'quota_reserved' OR edge='instant' OR kueue_workload_uid IS NOT NULL),
    CHECK (clock<>'scheduler_occupied' OR edge='instant' OR pod_uid IS NOT NULL),
    CHECK (clock<>'quota_reserved' OR phase='admit'),
    CHECK (clock NOT IN ('scheduler_occupied','device_allocated') OR phase='gpu_allocation'),
    CHECK (
        clock<>'phase' OR phase IN (
            'image_pull','artifact_load','restore','compile','warmup','active_compute',
            'workflow_wait','resident_idle','cooldown_grace','checkpoint_drain','teardown','unclassified'
        )
    )
);

CREATE INDEX fs2_lifecycle_signals_subject_idx
    ON fs2_lifecycle_signals (subject_id,id);
CREATE INDEX fs2_lifecycle_signals_pod_gpu_idx
    ON fs2_lifecycle_signals (pod_uid,gpu_uuid,gpu_rank,occurred_at) WHERE pod_uid IS NOT NULL;
CREATE INDEX fs2_lifecycle_signals_kueue_idx
    ON fs2_lifecycle_signals (kueue_workload_uid,occurred_at) WHERE kueue_workload_uid IS NOT NULL;

CREATE TABLE fs2_lifecycle_rollups (
    rollup_id uuid PRIMARY KEY,
    subject_id uuid NOT NULL REFERENCES fs2_telemetry_subjects(subject_id),
    generated_at timestamptz NOT NULL,
    event_watermark bigint NOT NULL CHECK (event_watermark >= 0),
    events_sha256 char(64) NOT NULL CHECK (events_sha256 ~ '^[a-f0-9]{64}$'),
    terminal boolean NOT NULL,
    outcome text CHECK (outcome IS NULL OR length(outcome) BETWEEN 1 AND 64),
    quota_reserved_gpu_seconds double precision NOT NULL CHECK (
        quota_reserved_gpu_seconds >= 0 AND quota_reserved_gpu_seconds < 'Infinity'::double precision
    ),
    scheduler_occupied_gpu_seconds double precision NOT NULL CHECK (
        scheduler_occupied_gpu_seconds >= 0 AND scheduler_occupied_gpu_seconds < 'Infinity'::double precision
    ),
    device_allocated_gpu_seconds double precision NOT NULL CHECK (
        device_allocated_gpu_seconds >= 0 AND device_allocated_gpu_seconds < 'Infinity'::double precision
    ),
    active_gpu_seconds double precision NOT NULL CHECK (
        active_gpu_seconds >= 0 AND active_gpu_seconds < 'Infinity'::double precision
    ),
    occupied_idle_gpu_seconds double precision NOT NULL CHECK (
        occupied_idle_gpu_seconds >= 0 AND occupied_idle_gpu_seconds < 'Infinity'::double precision
    ),
    phase_gpu_seconds jsonb NOT NULL CHECK (
        jsonb_typeof(phase_gpu_seconds)='object' AND octet_length(phase_gpu_seconds::text) BETWEEN 2 AND 8192
    ),
    reconciliation_delta_seconds double precision NOT NULL CHECK (
        abs(reconciliation_delta_seconds) < 'Infinity'::double precision
    ),
    device_scheduler_delta_seconds double precision NOT NULL CHECK (
        abs(device_scheduler_delta_seconds) < 'Infinity'::double precision
    ),
    tolerance_seconds double precision NOT NULL CHECK (
        tolerance_seconds >= 0 AND tolerance_seconds < 'Infinity'::double precision
    ),
    reconciled boolean NOT NULL,
    quality text NOT NULL CHECK (
        quality IN ('measured','application_observed','estimated','unavailable')
    ),
    data_gaps text[] NOT NULL CHECK (cardinality(data_gaps) <= 128),
    output_shape jsonb NOT NULL CHECK (
        jsonb_typeof(output_shape)='object' AND octet_length(output_shape::text) BETWEEN 2 AND 262144
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE UNIQUE INDEX fs2_lifecycle_rollups_identity_idx
    ON fs2_lifecycle_rollups (subject_id,events_sha256,terminal,outcome) NULLS NOT DISTINCT;
CREATE INDEX fs2_lifecycle_rollups_latest_idx
    ON fs2_lifecycle_rollups (subject_id,event_watermark DESC,generated_at DESC,rollup_id DESC);

CREATE FUNCTION fs2_reject_telemetry_mutation() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    RAISE EXCEPTION 'telemetry ledger rows are append-only' USING ERRCODE='55000';
END
$function$;

CREATE TRIGGER fs2_telemetry_subjects_append_only
    BEFORE UPDATE OR DELETE ON fs2_telemetry_subjects
    FOR EACH ROW EXECUTE FUNCTION fs2_reject_telemetry_mutation();
CREATE TRIGGER fs2_telemetry_correlations_append_only
    BEFORE UPDATE OR DELETE ON fs2_telemetry_correlations
    FOR EACH ROW EXECUTE FUNCTION fs2_reject_telemetry_mutation();
CREATE TRIGGER fs2_lifecycle_signals_append_only
    BEFORE UPDATE OR DELETE ON fs2_lifecycle_signals
    FOR EACH ROW EXECUTE FUNCTION fs2_reject_telemetry_mutation();
CREATE TRIGGER fs2_lifecycle_rollups_append_only
    BEFORE UPDATE OR DELETE ON fs2_lifecycle_rollups
    FOR EACH ROW EXECUTE FUNCTION fs2_reject_telemetry_mutation();

CREATE VIEW fs2_reporting_lifecycle_latest AS
SELECT DISTINCT ON (subject_id)
       rollup_id,subject_id,generated_at,event_watermark,events_sha256,terminal,outcome,
       quota_reserved_gpu_seconds,scheduler_occupied_gpu_seconds,device_allocated_gpu_seconds,
       active_gpu_seconds,occupied_idle_gpu_seconds,phase_gpu_seconds,
       reconciliation_delta_seconds,device_scheduler_delta_seconds,tolerance_seconds,
       reconciled,quality,data_gaps,output_shape
FROM fs2_lifecycle_rollups
ORDER BY subject_id,event_watermark DESC,generated_at DESC,rollup_id DESC;

CREATE VIEW fs2_reporting_gpu_phase_usage AS
SELECT subject.tenant_id,subject.model_id,subject.model_revision,subject.workload_kind,
       subject.accepted_at,rollup.generated_at,rollup.quality,
       phase.key AS phase,(phase.value #>> '{}')::double precision AS gpu_seconds
FROM fs2_reporting_lifecycle_latest rollup
JOIN fs2_telemetry_subjects subject USING(subject_id)
CROSS JOIN LATERAL jsonb_each(rollup.phase_gpu_seconds) AS phase(key,value)
WHERE rollup.terminal;

CREATE VIEW fs2_reporting_lifecycle_workloads AS
SELECT subject.subject_id,subject.workload_kind,subject.tenant_id,subject.model_id,
       subject.model_revision,subject.protocol,subject.accepted_at,
       rollup.generated_at,rollup.terminal,rollup.outcome,
       rollup.quota_reserved_gpu_seconds,rollup.scheduler_occupied_gpu_seconds,
       rollup.device_allocated_gpu_seconds,rollup.active_gpu_seconds,
       rollup.occupied_idle_gpu_seconds,rollup.reconciliation_delta_seconds,
       rollup.device_scheduler_delta_seconds,rollup.tolerance_seconds,
       rollup.reconciled,rollup.quality,rollup.data_gaps
FROM fs2_telemetry_subjects subject
LEFT JOIN fs2_reporting_lifecycle_latest rollup USING(subject_id);

REVOKE ALL ON fs2_telemetry_subjects,fs2_telemetry_correlations,fs2_lifecycle_signals,
    fs2_lifecycle_rollups,fs2_reporting_lifecycle_latest,fs2_reporting_gpu_phase_usage,
    fs2_reporting_lifecycle_workloads FROM PUBLIC;
REVOKE ALL ON fs2_lifecycle_signals_id_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION fs2_reject_telemetry_mutation() FROM PUBLIC;

COMMENT ON TABLE fs2_telemetry_subjects IS
    'Immutable payload-free online request or scientific attempt identity and reproducibility shape';
COMMENT ON TABLE fs2_telemetry_correlations IS
    'Append-only exact operation/batch/workload/attempt/Kueue/Job/Pod/node/GPU joins';
COMMENT ON TABLE fs2_lifecycle_signals IS
    'Append-only lifecycle interval edges from application, controller, Kubernetes, Kueue, kubelet, and DCGM';
COMMENT ON TABLE fs2_lifecycle_rollups IS
    'Immutable reconciliations over one exact lifecycle event watermark and digest';
COMMENT ON VIEW fs2_reporting_gpu_phase_usage IS
    'Bounded tenant/model/phase durable aggregate input; contains no principal, API-key, payload, or artifact URI';

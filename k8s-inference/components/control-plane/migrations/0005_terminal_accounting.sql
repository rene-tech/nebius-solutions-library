CREATE TABLE fs2_usage_facts (
    operation_id uuid PRIMARY KEY,
    occurred_at timestamptz NOT NULL,
    tenant_id text NOT NULL,
    principal_id text NOT NULL,
    model_id text NOT NULL,
    protocol text NOT NULL,
    outcome text NOT NULL,
    status fs2_operation_status NOT NULL,
    attempt integer NOT NULL CHECK (attempt >= 0),
    estimated_gpu_seconds double precision NOT NULL CHECK (estimated_gpu_seconds >= 0),
    duration_seconds double precision NOT NULL CHECK (duration_seconds >= 0),
    cold_start_seconds double precision CHECK (cold_start_seconds IS NULL OR cold_start_seconds >= 0)
);

CREATE INDEX fs2_usage_facts_occurred_idx ON fs2_usage_facts (occurred_at);
CREATE INDEX fs2_usage_facts_model_idx ON fs2_usage_facts (model_id,occurred_at);
CREATE INDEX fs2_usage_facts_principal_idx ON fs2_usage_facts (tenant_id,principal_id,occurred_at);

CREATE FUNCTION fs2_record_terminal_usage() RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.status IN ('succeeded','failed','cancelled','preempted','expired')
       AND (TG_OP='INSERT' OR OLD.status NOT IN ('succeeded','failed','cancelled','preempted','expired')) THEN
        INSERT INTO fs2_usage_facts(
            operation_id,occurred_at,tenant_id,principal_id,model_id,protocol,
            outcome,status,attempt,estimated_gpu_seconds,duration_seconds,cold_start_seconds
        ) VALUES (
            NEW.id,COALESCE(NEW.completed_at,clock_timestamp()),NEW.tenant_id,NEW.principal_id,
            NEW.model_id,NEW.protocol,COALESCE(NEW.outcome,NEW.status::text),NEW.status,NEW.attempt,
            NEW.estimated_gpu_seconds,
            GREATEST(0,extract(epoch FROM COALESCE(NEW.completed_at,clock_timestamp())-NEW.accepted_at)),
            NEW.cold_start_seconds
        ) ON CONFLICT (operation_id) DO NOTHING;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_operations_terminal_usage
AFTER INSERT OR UPDATE OF status ON fs2_operations
FOR EACH ROW EXECUTE FUNCTION fs2_record_terminal_usage();

INSERT INTO fs2_usage_facts(
    operation_id,occurred_at,tenant_id,principal_id,model_id,protocol,
    outcome,status,attempt,estimated_gpu_seconds,duration_seconds,cold_start_seconds
)
SELECT id,COALESCE(completed_at,clock_timestamp()),tenant_id,principal_id,model_id,protocol,
       COALESCE(outcome,status::text),status,attempt,estimated_gpu_seconds,
       GREATEST(0,extract(epoch FROM COALESCE(completed_at,clock_timestamp())-accepted_at)),cold_start_seconds
FROM fs2_operations
WHERE status IN ('succeeded','failed','cancelled','preempted','expired')
ON CONFLICT (operation_id) DO NOTHING;

CREATE VIEW fs2_reporting_model_usage AS
SELECT date_trunc('minute',occurred_at) AS time,model_id,protocol,status::text AS status,outcome,
       count(*)::bigint AS operations,
       sum(estimated_gpu_seconds)::double precision AS estimated_gpu_seconds,
       sum(duration_seconds)::double precision AS duration_seconds
FROM fs2_usage_facts
GROUP BY 1,2,3,4,5;

CREATE VIEW fs2_reporting_principal_usage AS
SELECT date_trunc('minute',occurred_at) AS time,tenant_id,principal_id,model_id,
       count(*)::bigint AS operations,
       sum(estimated_gpu_seconds)::double precision AS estimated_gpu_seconds
FROM fs2_usage_facts
GROUP BY 1,2,3,4;

CREATE VIEW fs2_reporting_terminal_totals AS
SELECT model_id,protocol,outcome,count(*)::bigint AS operations,
       sum(estimated_gpu_seconds)::double precision AS estimated_gpu_seconds,
       sum(duration_seconds)::double precision AS duration_seconds,
       sum(COALESCE(cold_start_seconds,0))::double precision AS cold_start_seconds
FROM fs2_usage_facts
GROUP BY 1,2,3;

-- Preserve HTTP transport status while recording the customer-visible outcome
-- of the protocol exchange. Existing rows remain NULL: their body-free
-- telemetry cannot prove a semantic outcome retroactively.
ALTER TABLE fs2_request_telemetry
    ADD COLUMN semantic_outcome text CHECK (
        semantic_outcome IN ('succeeded','accepted','failed','cancelled','timed_out','unknown')
    ),
    ADD COLUMN jsonrpc_error_code integer,
    ADD COLUMN semantic_error_type text,
    ADD COLUMN admission_stage text CHECK (
        admission_stage IN ('pre_admission','admitted','not_applicable','unknown')
    );

ALTER TABLE fs2_request_debug
    ADD COLUMN semantic_outcome text CHECK (
        semantic_outcome IN ('succeeded','accepted','failed','cancelled','timed_out','unknown')
    ),
    ADD COLUMN jsonrpc_error_code integer,
    ADD COLUMN semantic_error_type text,
    ADD COLUMN admission_stage text CHECK (
        admission_stage IN ('pre_admission','admitted','not_applicable','unknown')
    );

CREATE INDEX fs2_request_telemetry_semantic_time
    ON fs2_request_telemetry (semantic_outcome, admission_stage, started_at);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime') THEN
        GRANT SELECT, INSERT ON fs2_request_telemetry TO fs2_serve_runtime;
        GRANT SELECT, INSERT ON fs2_request_debug TO fs2_serve_runtime;
    END IF;
END;
$$;

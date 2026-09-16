-- Retention walks global time order rather than tenant-local read order. Keep
-- each bounded candidate scan indexable as the tables grow, while preserving
-- the child-operation indexes used by FK-safe eligibility checks.
CREATE INDEX fs2_operations_retention_idx
    ON fs2_operations (completed_at,id)
    WHERE status IN ('succeeded','failed','cancelled','preempted','expired');

CREATE INDEX fs2_tokens_revoked_retention_idx
    ON fs2_tokens (revoked_at,id)
    WHERE revoked_at IS NOT NULL;

CREATE INDEX fs2_tokens_expiry_retention_idx
    ON fs2_tokens (expires_at,id)
    WHERE expires_at IS NOT NULL;

CREATE INDEX fs2_audit_retention_idx
    ON fs2_audit_events (occurred_at,id);

CREATE INDEX fs2_request_telemetry_retention_idx
    ON fs2_request_telemetry (started_at,request_id);

CREATE INDEX fs2_scientific_stage_attempts_retention_idx
    ON fs2_scientific_stage_attempts (operation_id,retention_expires_at,status);

CREATE INDEX fs2_scientific_artifacts_retention_idx
    ON fs2_scientific_artifacts (operation_id,retention_expires_at);

-- PostgreSQL grants PUBLIC execute on new functions by default. Existing
-- functions are reconciled explicitly by the migrator; make every future
-- function created by this schema owner fail closed until a grant is reviewed.
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

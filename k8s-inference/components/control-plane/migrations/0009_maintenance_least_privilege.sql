-- Terminal usage facts are created only by the operation-state trigger. Make
-- that insertion owner-controlled so neither the gateway runtime nor the
-- retention principal needs direct INSERT, UPDATE, or DELETE authority on the
-- immutable accounting table.
ALTER TABLE fs2_operations ADD COLUMN payload_purged_at timestamptz;
UPDATE fs2_operations SET payload_purged_at=clock_timestamp()
WHERE request_key_id IS NULL AND request_nonce IS NULL AND request_ciphertext IS NULL
  AND response_key_id IS NULL AND response_nonce IS NULL AND response_ciphertext IS NULL;
ALTER TABLE fs2_operations ADD CONSTRAINT fs2_operations_payload_purged_check CHECK (
    payload_purged_at IS NULL OR (
        request_key_id IS NULL AND request_nonce IS NULL AND request_ciphertext IS NULL
        AND response_key_id IS NULL AND response_nonce IS NULL AND response_ciphertext IS NULL
    )
);
CREATE INDEX fs2_operations_unpurged_payload_expiry_idx
    ON fs2_operations (payload_expires_at,id) WHERE payload_purged_at IS NULL;

ALTER FUNCTION fs2_record_terminal_usage() SECURITY DEFINER;
ALTER FUNCTION fs2_record_terminal_usage() SET search_path = pg_catalog, public;

REVOKE ALL ON FUNCTION fs2_record_terminal_usage() FROM PUBLIC;
REVOKE ALL ON fs2_usage_facts, fs2_audit_events FROM PUBLIC;

COMMENT ON FUNCTION fs2_record_terminal_usage() IS
    'Owner-controlled exactly-once terminal accounting; application roles receive no direct usage-fact writes';

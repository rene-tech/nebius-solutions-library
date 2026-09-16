-- Isolate customer storage credentials behind a dedicated reconciler and
-- make key lifecycle explicit. Existing keys receive a short migration grace
-- period so operators can rotate them without an unbounded credential.
ALTER TABLE fs2_storage_policies ALTER COLUMN mode SET DEFAULT 'user';

ALTER TABLE fs2_user_storage
    ADD COLUMN expires_at timestamptz,
    ADD COLUMN desired_enabled boolean NOT NULL DEFAULT true,
    ADD COLUMN revoked_at timestamptz,
    ADD COLUMN requested_action text CHECK (requested_action IN ('rotate', 'revoke', 'disable')),
    ADD COLUMN requested_at timestamptz,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();

UPDATE fs2_user_storage SET expires_at = now() + interval '7 days' WHERE expires_at IS NULL;
ALTER TABLE fs2_user_storage ALTER COLUMN expires_at SET NOT NULL;

CREATE INDEX fs2_user_storage_requested_action_idx
    ON fs2_user_storage (requested_at, tenant_id, principal_id)
    WHERE requested_action IS NOT NULL OR desired_enabled <> enabled;

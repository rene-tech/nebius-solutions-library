-- SAI-34 expand-only migration: old binaries continue to accept a NULL legacy
-- fingerprint while new binaries use the versioned operator identifier. The
-- original PAT material is unnecessary because the legacy hash can be safely
-- truncated during migration.
ALTER TABLE fs2_tokens ADD COLUMN operator_fingerprint text CHECK (
    operator_fingerprint IS NULL OR operator_fingerprint ~ '^fp:v2:sha256-128:[a-f0-9]{32}$'
);

UPDATE fs2_tokens
SET operator_fingerprint = 'fp:v2:sha256-128:' || left(fingerprint, 32),
    fingerprint = NULL
WHERE fingerprint ~ '^[a-f0-9]{64}$';

-- Keep mixed-version rollouts safe. An older control-plane binary may still
-- write the legacy column after this migration is applied. Normalize that
-- value before constraints and RETURNING are evaluated, so neither storage
-- nor an operator response can regain the full unkeyed digest.
CREATE FUNCTION fs2_normalize_token_fingerprint() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
DECLARE
    normalized text;
BEGIN
    IF NEW.fingerprint IS NOT NULL AND NEW.fingerprint ~ '^[a-f0-9]{64}$' THEN
        normalized := 'fp:v2:sha256-128:' || left(NEW.fingerprint, 32);
        IF NEW.operator_fingerprint IS NOT NULL AND NEW.operator_fingerprint <> normalized THEN
            RAISE EXCEPTION 'conflicting token fingerprint representations'
                USING ERRCODE='23514';
        END IF;
        NEW.operator_fingerprint := normalized;
        NEW.fingerprint := NULL;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER fs2_tokens_normalize_fingerprint
    BEFORE INSERT OR UPDATE OF fingerprint ON fs2_tokens
    FOR EACH ROW EXECUTE FUNCTION fs2_normalize_token_fingerprint();

REVOKE ALL ON FUNCTION fs2_normalize_token_fingerprint() FROM PUBLIC;

CREATE UNIQUE INDEX fs2_tokens_operator_fingerprint_idx
    ON fs2_tokens (operator_fingerprint) WHERE operator_fingerprint IS NOT NULL;

COMMENT ON COLUMN fs2_tokens.fingerprint IS
    'Deprecated rolling-upgrade input for old binaries; new writers always leave it NULL';
COMMENT ON COLUMN fs2_tokens.operator_fingerprint IS
    'Stable fp:v2:sha256-128 operator correlation ID; never a full PAT digest or verifier';

-- Retirement removes active intent and App discovery, not historical evidence.
ALTER TABLE fs2_model_deployments
    ADD COLUMN retired_at timestamptz,
    ADD COLUMN retired_by text,
    ADD COLUMN retirement_archive_sha256 char(64),
    ADD CONSTRAINT fs2_model_retirement_complete CHECK (
        (retired_at IS NULL AND retired_by IS NULL AND retirement_archive_sha256 IS NULL)
        OR (retired_at IS NOT NULL AND retired_by IS NOT NULL
            AND length(retired_by) BETWEEN 1 AND 200 AND retirement_archive_sha256 IS NOT NULL
            AND retirement_archive_sha256 ~ '^[a-f0-9]{64}$')
    );

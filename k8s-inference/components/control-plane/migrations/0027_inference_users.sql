-- Inference owners, not console operators. Existing owners are discovered from
-- durable keys/operations and retain their original policy until configured.
CREATE TABLE fs2_inference_users (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    principal_id text NOT NULL,
    display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 160),
    kind text CHECK (kind IN ('human', 'service')),
    team text CHECK (length(team) <= 160),
    enabled boolean NOT NULL DEFAULT true,
    academic_eligible boolean,
    app_ids uuid[],
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (tenant_id, principal_id)
);

-- Startup selection is operator configuration, independent of dispatch limits.
-- Empty means use the execution map's normal model-loading policy. Each
-- admitted run freezes its resolved choice, so changing this row never
-- changes an already admitted or retrying run.
ALTER TABLE fs2_scientific_model_policies
    ADD COLUMN startup_policies jsonb NOT NULL DEFAULT '{}'::jsonb;


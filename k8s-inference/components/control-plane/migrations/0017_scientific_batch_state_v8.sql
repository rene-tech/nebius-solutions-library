-- Rolling state upgrade for aggregate runtime-tree identities and frozen
-- execution-map/stage-placement bindings. Existing v6 and v7 rows remain
-- readable; their first state transition emits v8 without weakening the
-- immutable admission fence.
ALTER TABLE fs2_scientific_batches
    DROP CONSTRAINT fs2_scientific_batches_state_check;

ALTER TABLE fs2_scientific_batches
    ADD CONSTRAINT fs2_scientific_batches_state_check CHECK (
        pg_column_size(state) <= 4194304
        AND jsonb_typeof(state) = 'object'
        AND state->>'schema_version' IN (
            'fs2-serve.nebius.ai/scientific-batch-state/v6',
            'fs2-serve.nebius.ai/scientific-batch-state/v7',
            'fs2-serve.nebius.ai/scientific-batch-state/v8'
        )
        AND state->>'operation_id' = operation_id::text
        AND state->>'batch_id' = batch_id::text
        AND state->>'workload_id' = workload_id::text
        AND state->>'tenant_id' = tenant_id
        AND state->>'model_id' = model_id
        AND state->>'variant_id' = variant_id
        AND state->>'input_artifact_id' = input_artifact_id::text
        AND state->>'status' = status
        AND (state->>'revision')::integer = revision
        AND (state->>'cancel_requested')::boolean = cancel_requested
    );

CREATE OR REPLACE FUNCTION fs2_scientific_batch_state_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    old_plan jsonb := OLD.state->'plan';
    old_scheduling jsonb := OLD.state->'scheduling';
    old_adapter_execution jsonb := OLD.state->'adapter_execution';
    old_runtime_artifacts jsonb := OLD.state->'runtime_artifacts';
    old_workload_namespace text;
    upgrading_v6 boolean := false;
    upgrading_v8 boolean := false;
    legacy_upgrade boolean := false;
BEGIN
    upgrading_v6 := OLD.state->>'schema_version' = 'fs2-serve.nebius.ai/scientific-batch-state/v6'
        AND NEW.state->>'schema_version' IN (
            'fs2-serve.nebius.ai/scientific-batch-state/v7',
            'fs2-serve.nebius.ai/scientific-batch-state/v8'
        );
    upgrading_v8 := OLD.state->>'schema_version' IN (
            'fs2-serve.nebius.ai/scientific-batch-state/v6',
            'fs2-serve.nebius.ai/scientific-batch-state/v7'
        )
        AND NEW.state->>'schema_version' = 'fs2-serve.nebius.ai/scientific-batch-state/v8';
    legacy_upgrade := upgrading_v6 OR upgrading_v8;

    -- Reproduce the released v6 -> v7 codec projection exactly. An applied
    -- attempt namespace is authoritative; fs2-models was the only v6 default.
    IF upgrading_v6 THEN
        SELECT value #>> '{}'
        INTO old_workload_namespace
        FROM jsonb_path_query(OLD.state, '$.stages[*].attempts[*].workload.namespace') value
        LIMIT 1;
        old_workload_namespace := COALESCE(old_workload_namespace, 'fs2-models');
        old_scheduling := old_scheduling || jsonb_build_object(
            'workload_namespace', old_workload_namespace,
            'route_namespace', old_workload_namespace
        );
        old_scheduling := jsonb_set(
            old_scheduling,
            '{stages}',
            COALESCE(
                (
                    SELECT jsonb_agg(
                        (decision.value - 'admitted_resource_flavor')
                        || jsonb_build_object(
                            'resource_class',
                            (
                                SELECT plan_stage.value->>'resource_class'
                                FROM jsonb_array_elements(OLD.state->'plan'->'stages')
                                    WITH ORDINALITY AS plan_stage(value, ordinal)
                                WHERE plan_stage.value->>'stage_id' = decision.value->>'stage_id'
                                LIMIT 1
                            )
                        )
                        ORDER BY decision.ordinal
                    )
                    FROM jsonb_array_elements(old_scheduling->'stages')
                        WITH ORDINALITY AS decision(value, ordinal)
                ),
                '[]'::jsonb
            ),
            false
        );
        IF jsonb_typeof(old_adapter_execution) = 'object' THEN
            old_adapter_execution := jsonb_set(
                old_adapter_execution,
                '{invocations}',
                COALESCE(
                    (
                        SELECT jsonb_agg(
                            jsonb_set(
                                invocation.value,
                                '{runtime_mounts}',
                                COALESCE(
                                    (
                                        SELECT jsonb_agg(
                                            mount.value || jsonb_build_object('expected_manifest_sha256', NULL)
                                            ORDER BY mount.ordinal
                                        )
                                        FROM jsonb_array_elements(invocation.value->'runtime_mounts')
                                            WITH ORDINALITY AS mount(value, ordinal)
                                    ),
                                    '[]'::jsonb
                                ),
                                false
                            )
                            ORDER BY invocation.ordinal
                        )
                        FROM jsonb_array_elements(old_adapter_execution->'invocations')
                            WITH ORDINALITY AS invocation(value, ordinal)
                    ),
                    '[]'::jsonb
                ),
                false
            );
        END IF;
    END IF;

    -- v8 freezes placement, the raw scheduling bytes, aggregate-tree evidence,
    -- and exact execution-map image/resource/source bindings. Legacy rows have
    -- no truthful value for those fields, so the one permitted upgrade value is
    -- the codec's explicit null/empty representation.
    IF upgrading_v8 THEN
        old_plan := jsonb_set(
            old_plan,
            '{stages}',
            COALESCE(
                (
                    SELECT jsonb_agg(
                        stage.value || jsonb_build_object('placement_class', NULL, 'resources', NULL)
                        ORDER BY stage.ordinal
                    )
                    FROM jsonb_array_elements(old_plan->'stages')
                        WITH ORDINALITY AS stage(value, ordinal)
                ),
                '[]'::jsonb
            ),
            false
        );
        old_scheduling := old_scheduling || jsonb_build_object('raw_contract_sha256', NULL);
        old_scheduling := jsonb_set(
            old_scheduling,
            '{stages}',
            COALESCE(
                (
                    SELECT jsonb_agg(
                        decision.value || jsonb_build_object(
                            'placement_class', NULL,
                            'workload_namespace', NULL,
                            'route_namespace', NULL,
                            'requested_resource_flavor', NULL,
                            'node_selector', '[]'::jsonb,
                            'tolerations', '[]'::jsonb
                        )
                        ORDER BY decision.ordinal
                    )
                    FROM jsonb_array_elements(old_scheduling->'stages')
                        WITH ORDINALITY AS decision(value, ordinal)
                ),
                '[]'::jsonb
            ),
            false
        );
        old_runtime_artifacts := COALESCE(
            (
                SELECT jsonb_agg(
                    artifact.value || jsonb_build_object('aggregate_tree', NULL)
                    ORDER BY artifact.ordinal
                )
                FROM jsonb_array_elements(old_runtime_artifacts)
                    WITH ORDINALITY AS artifact(value, ordinal)
            ),
            '[]'::jsonb
        );
        IF jsonb_typeof(old_adapter_execution) = 'object' THEN
            old_adapter_execution := old_adapter_execution || jsonb_build_object(
                'execution_map_sha256', NULL,
                'stage_bindings', '[]'::jsonb
            );
        END IF;
    END IF;

    IF NEW.operation_id <> OLD.operation_id OR NEW.batch_id <> OLD.batch_id
       OR NEW.workload_id <> OLD.workload_id OR NEW.tenant_id <> OLD.tenant_id
       OR NEW.model_id <> OLD.model_id OR NEW.variant_id <> OLD.variant_id
       OR NEW.input_artifact_id <> OLD.input_artifact_id
       OR (NEW.scheduling_digest <> OLD.scheduling_digest AND NOT legacy_upgrade)
       OR NEW.state->'plan' <> old_plan
       OR NEW.state->'scheduling' <> old_scheduling
       OR NEW.state->'adapter_execution' <> old_adapter_execution
       OR NEW.state->'access_context' <> OLD.state->'access_context'
       OR NEW.state->'input_manifest' <> OLD.state->'input_manifest'
       OR NEW.state->'runtime_artifacts' <> old_runtime_artifacts THEN
        RAISE EXCEPTION 'scientific batch admission is immutable';
    END IF;
    IF NEW.revision < OLD.revision THEN
        RAISE EXCEPTION 'scientific batch revision cannot move backwards';
    END IF;
    RETURN NEW;
END;
$$;

import pytest

from fs2_serve.scientific_batch.native_workflows import (
    WORKFLOWS,
    workflow_for_binding,
    workflow_for_collector,
    workflow_for_schema,
)


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda item: item.model_id)
def test_native_binding_requires_exact_model_stage_and_collector(workflow):
    assert workflow_for_collector(workflow.collector_id) == workflow
    assert workflow_for_schema(workflow.parameter_schema) == workflow
    assert workflow_for_binding(workflow.model_id, "workflow", workflow.collector_id) == workflow
    assert workflow_for_binding("other", "workflow", workflow.collector_id) is None
    assert workflow_for_binding(workflow.model_id, "other", workflow.collector_id) is None
    assert workflow_for_binding(workflow.model_id, "workflow", "other") is None


def test_registry_is_unambiguous_and_keeps_legacy_gromacs_route():
    assert len({item.model_id for item in WORKFLOWS}) == len(WORKFLOWS)
    assert len({item.collector_id for item in WORKFLOWS}) == len(WORKFLOWS)
    assert len({item.parameter_schema for item in WORKFLOWS}) == len(WORKFLOWS)
    for item in WORKFLOWS:
        family = "gromacs" if item.engine == "gromacs" else "native"
        assert item.storage_endpoint == f"/internal/scientific-workloads/{family}/storage"
    assert workflow_for_collector("unregistered") is None
    assert workflow_for_schema("unregistered") is None

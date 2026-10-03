import pytest

from capture_plans import plan_query


def test_only_exact_uuid_and_internal_md_scope_are_queried():
    operation = "0a828a8f-a743-4055-b605-efe24dba9a1f"
    query = plan_query([operation, operation])
    assert query.startswith("SELECT json_build_object(")
    assert query.count(operation) == 1
    assert "tenant_id='system'" in query
    assert "model_id IN ('gromacs','gromacs-mpi')" in query
    assert "state->'plan'" in query and "SELECT state" not in query


@pytest.mark.parametrize("identifiers", [[], ["not-an-operation"], ["'); DROP TABLE fs2_scientific_batches; --"]])
def test_invalid_or_unbounded_query_is_rejected(identifiers):
    with pytest.raises(ValueError):
        plan_query(identifiers)

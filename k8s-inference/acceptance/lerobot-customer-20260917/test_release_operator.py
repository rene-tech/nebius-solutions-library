import pytest
from release_operator import APP, source_policy


def row(**updates):
    return {
        "name": "timmothy-cosmos3",
        "revoked_at": None,
        "tenant_id": "robotics",
        "models": ["cosmos3-nano"],
        "max_concurrency": 1,
        **updates,
    }


def test_exact_existing_key_or_already_granted_key():
    assert source_policy([row()])["max_concurrency"] == 1
    assert APP in source_policy([row(models=["cosmos3-nano", APP])])["models"]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [row(), row()],
        [row(revoked_at="yesterday")],
        [row(tenant_id="another")],
        [row(models=["*"])],
        [row(max_concurrency=5)],
    ],
)
def test_refuses_unexpected_customer_key(rows):
    with pytest.raises(ValueError):
        source_policy(rows)

import copy

import pytest
from promote_template import MODEL, NEW, OLD, TEMPLATE_NAME, cold_observed, replacement


def test_replacement_changes_only_template_and_preserves_source():
    original = {"modelRef": MODEL, "runtime": {"templateRef": {"name": "old", "digest": OLD}},
                "policy": {"allowedPrincipalIds": ["example"]},
                "availability": {"minReplicas": 0, "maxReplicas": 2},
                "lifecycle": {"desiredState": "Enabled"}, "cache": {"mode": "snapshot"}}
    before = copy.deepcopy(original)
    changed = replacement(original)
    assert original == before
    assert changed["runtime"]["templateRef"] == {"name": TEMPLATE_NAME, "digest": NEW}
    changed["runtime"]["templateRef"] = before["runtime"]["templateRef"]
    assert changed == before


@pytest.mark.parametrize("model,digest", [("other", OLD), (MODEL, NEW)])
def test_refuses_unexpected_model_or_already_changed_template(model, digest):
    with pytest.raises(ValueError, match="unexpected_current_cosmos_template"):
        replacement({"modelRef": model, "runtime": {"templateRef": {"digest": digest}}})


def test_cold_requires_matching_observed_revision_and_zero_replicas():
    current = {"revision": 15, "etag": "sha256:example"}
    view = {"observation": {"revision": 15, "status": {"spec_digest": current["etag"],
            "phase": "Cold", "replicas": {"desired": 0, "ready": 0, "available": 0}}}}
    assert cold_observed(current, view)
    assert not cold_observed(current, {})
    assert not cold_observed({**current, "revision": 16}, view)
    view["observation"]["status"]["replicas"]["desired"] = 1
    assert not cold_observed(current, view)

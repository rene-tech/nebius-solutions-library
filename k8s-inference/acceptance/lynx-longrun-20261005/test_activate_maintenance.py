import copy

import pytest

from activate_maintenance import PREVIOUS, REPO, prepare


def cronjob():
    return {"metadata": {"uid": "existing"}, "spec": {
        "schedule": "*/1 * * * *", "jobTemplate": {"spec": {"template": {"spec": {
            "containers": [{"name": "maintenance", "image": PREVIOUS, "args": ["maintenance"],
                            "env": [{"name": "DATABASE", "valueFrom": {"secretKeyRef": "unchanged"}}]}],
        }}}},
    }}


def test_only_existing_maintenance_image_changes():
    before = cronjob()
    saved = copy.deepcopy(before)
    target = REPO + "@sha256:" + "a" * 64
    patch = prepare(before, target)
    expected = copy.deepcopy(before["spec"]["jobTemplate"]["spec"]["template"])
    expected["spec"]["containers"][0]["image"] = target
    assert patch[-1]["value"] == expected
    assert patch[1]["value"] == before["spec"]["jobTemplate"]["spec"]["template"]
    assert before == saved


@pytest.mark.parametrize("image", ["repo:mutable", REPO + "@sha256:bad", "other@sha256:" + "a" * 64])
def test_unpinned_or_wrong_repository_refused(image):
    with pytest.raises(ValueError):
        prepare(cronjob(), image)

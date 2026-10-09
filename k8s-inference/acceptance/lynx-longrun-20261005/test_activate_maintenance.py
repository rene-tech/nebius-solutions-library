import copy

import pytest

from activate_maintenance import PREVIOUS, REPO, prepare, verify


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


def test_successor_requires_the_exact_observed_current_image():
    before = cronjob()
    current, target = (REPO + "@sha256:" + value * 64 for value in ("a", "b"))
    before["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"] = current
    with pytest.raises(ValueError, match="Unexpected existing"):
        prepare(before, target)
    patch = prepare(before, target, expected_image=current)
    assert patch[-1]["value"]["spec"]["containers"][0]["image"] == target


def test_verification_needs_owned_success_and_no_active_previous_reader():
    current = cronjob()
    final = REPO + "@sha256:" + "a" * 64
    current["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"] = final
    job = {"metadata": {"name": "new", "uid": "job", "ownerReferences": [
        {"uid": "existing", "controller": True}]},
        "spec": {"template": copy.deepcopy(current["spec"]["jobTemplate"]["spec"]["template"])},
        "status": {"succeeded": 1, "completionTime": "now"}}
    assert verify(current, [job], final)["status"] == "passed"
    previous = copy.deepcopy(job)
    previous["status"] = {"active": 1}
    previous["spec"]["template"]["spec"]["containers"][0]["image"] = PREVIOUS
    with pytest.raises(ValueError, match="previous maintenance reader"):
        verify(current, [job, previous], final)
    job["metadata"]["ownerReferences"][0]["uid"] = "another-cronjob"
    with pytest.raises(ValueError, match="No new successful"):
        verify(current, [job], final)

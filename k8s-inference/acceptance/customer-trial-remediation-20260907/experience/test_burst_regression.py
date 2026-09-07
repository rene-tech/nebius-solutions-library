"""Offline verdict checks; these tests never invoke Kubernetes or inference."""

from copy import deepcopy

from qwen_burst_regression import verdict


def observation(second, *, burst=False, hot_ready=True, model_phase="Ready"):
    pods = [
        {"uid": "hot-1", "role": "hot-reserved", "ready": hot_ready, "deleting": False}
    ]
    if burst:
        pods.append(
            {
                "uid": "burst-1",
                "role": "burst-preemptible",
                "ready": False,
                "scheduled": True,
                "initialized": False,
                "deleting": False,
                "phase": "Pending",
            }
        )
    return {
        "observed_at": f"2026-09-07T17:00:{second:02d}Z",
        "pods": pods,
        "spec_sha256": "unchanged-spec",
        "model_status": {"phase": model_phase},
    }


def request(*, passed=True):
    return {
        "ordinal": 1,
        "started_at": "2026-09-07T17:00:01Z",
        "completed_at": "2026-09-07T17:00:04Z",
        "status": "passed" if passed else "failed",
        "operation_id": "operation-1",
        "operation": {"status": "succeeded"},
    }


def test_success_without_observed_burst_is_not_a_transition_pass():
    assert (
        verdict([observation(0), observation(2)], [request()])["status"]
        == "coverage-not-observed"
    )


def test_healthy_hot_initializing_burst_overlaps_successful_public_call():
    result = verdict([observation(0), observation(2, burst=True)], [request()])
    assert result["status"] == "passed"
    assert result["new_burst_pod_uids"] == ["burst-1"]
    assert result["overlapping_request_ordinals"] == [1]


def test_burst_after_requests_does_not_qualify_the_call_path():
    assert (
        verdict([observation(0), observation(8, burst=True)], [request()])["status"]
        == "coverage-not-observed"
    )


def test_unready_hot_or_failed_call_is_not_a_pass():
    assert (
        verdict(
            [observation(0), observation(2, burst=True, hot_ready=False)], [request()]
        )["status"]
        == "failed"
    )
    assert (
        verdict([observation(0), observation(2, burst=True)], [request(passed=False)])[
            "status"
        ]
        == "failed"
    )


def test_old_localizing_demotion_fails_even_when_hot_pod_is_healthy():
    assert (
        verdict(
            [observation(0), observation(2, burst=True, model_phase="Localizing")],
            [request()],
        )["status"]
        == "failed"
    )


def test_preexisting_burst_is_not_labeled_test_created():
    result = verdict(
        [observation(0, burst=True), observation(2, burst=True)], [request()]
    )
    assert result["status"] == "passed"
    assert (
        result["baseline_burst_uids"] == ["burst-1"]
        and result["new_burst_pod_uids"] == []
    )


def test_observation_failure_and_changed_policy_are_reported():
    samples = [observation(0), observation(2, burst=True)]
    changed = deepcopy(samples)
    changed[-1]["spec_sha256"] = "changed"
    assert verdict(changed, [request()])["status"] == "failed"
    samples.append(
        {"observed_at": "2026-09-07T17:00:05Z", "error": {"type": "TimeoutError"}}
    )
    assert verdict(samples, [request()])["status"] == "failed"

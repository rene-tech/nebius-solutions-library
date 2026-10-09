"""Regression for the real Pending Pod eviction observed on18September2026."""

import copy
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import test_scientific_batch_production as production

from fs2_serve.scientific_batch.kubernetes import _JOB_FAILURE_POLICY, STAGE_CONTAINER_NAME, _reported_failure
from fs2_serve.scientific_batch.models import FailureKind, WorkloadState


def disrupted(reason="DeletionByTaintManager", status="True"):
    return {
        "phase": "Pending",
        "conditions": [
            {
                "type": "DisruptionTarget",
                "reason": reason,
                "status": status,
                "message": "Taint manager: deleting due to NoExecute taint",
            }
        ],
    }


@pytest.mark.parametrize("model", ["esmfold2", "openfold3-openbind", "cosmos3-lerobot-augmentation"])
def test_taint_evicted_pending_pod_is_infrastructure_not_model_failure(model):
    state, kind, code = _reported_failure(["workload_failed"], [disrupted()], model_id=model)
    assert (state, kind, code) == (WorkloadState.FAILED, FailureKind.INFRASTRUCTURE, "DeletionByTaintManager")
    assert kind.retryable


def test_exact_scheduler_preemption_condition_is_retryable():
    assert _reported_failure(["workload_failed"], [disrupted("PreemptionByScheduler")], model_id="esmfold2") == (
        WorkloadState.PREEMPTED,
        FailureKind.PREEMPTION,
        "PreemptionByScheduler",
    )


@pytest.mark.parametrize(
    "reason,status",
    [
        ("DeletionByTaintManager", "False"),
        ("DeletionByTaintManager", "Unknown"),
        ("EvictionByEvictionAPI", "False"),
        ("EvictionByEvictionAPI", "Unknown"),
        ("GenericEviction", "True"),
        ("", "True"),
    ],
)
def test_generic_or_inactive_disruption_does_not_make_application_error_retryable(reason, status):
    assert (
        _reported_failure(["workload_failed"], [disrupted(reason, status)], model_id="esmfold2")[1]
        is FailureKind.APPLICATION
    )


@pytest.mark.parametrize("reason", ["OOMKilled", "MaximumExecutionTimeExceeded"])
def test_known_oom_or_execution_timeout_is_not_retried_due_to_coincident_disruption(reason):
    assert _reported_failure([reason], [disrupted()], model_id="esmfold2")[1] is FailureKind.APPLICATION


def test_model_exception_is_not_retried_due_to_later_disruption():
    pod = disrupted()
    pod["containerStatuses"] = [
        {"name": STAGE_CONTAINER_NAME, "state": {"terminated": {"exitCode": 1, "reason": "Error"}}}
    ]
    assert _reported_failure(["Error"], [pod], model_id="esmfold2")[1] is FailureKind.APPLICATION


@pytest.mark.parametrize("exit_code", [None, 137, 143])
def test_exact_api_eviction_is_retryable_without_changing_the_attempt_budget(exit_code):
    pod = disrupted("EvictionByEvictionAPI")
    if exit_code is not None:
        pod["containerStatuses"] = [
            {"name": STAGE_CONTAINER_NAME, "state": {"terminated": {"exitCode": exit_code, "reason": "Error"}}}
        ]
    assert _reported_failure(["workload_failed"], [pod], model_id="esmfold2") == (
        WorkloadState.FAILED, FailureKind.INFRASTRUCTURE, "EvictionByEvictionAPI",
    )


@pytest.mark.parametrize("reason,exit_code", [("OOMKilled", 137), ("MaximumExecutionTimeExceeded", 143), ("Error", 1)])
def test_api_eviction_does_not_override_known_model_failure(reason, exit_code):
    pod = disrupted("EvictionByEvictionAPI")
    pod["containerStatuses"] = [
        {"name": STAGE_CONTAINER_NAME, "state": {"terminated": {"exitCode": exit_code, "reason": reason}}}
    ]
    assert _reported_failure([reason], [pod], model_id="esmfold2")[1] is FailureKind.APPLICATION


def test_retained_real_h100_api_eviction_is_infrastructure():
    # Public baseline ba2003cd failed under 182. Preserve that original outcome;
    # replay only the bounded observed Pod fields, not a claimed live recovery.
    receipt = json.loads((Path(__file__).parent / "fixtures/api_eviction_20260919.json").read_text())
    assert receipt["source_sha256"] == "a743d05cbdcf861c1789addbfe75a58d60d6771a7782dadbe69bf57ec783cf88"
    assert _reported_failure(["workload_failed"], [receipt["status"]], model_id="esmfold2") == (
        WorkloadState.FAILED, FailureKind.INFRASTRUCTURE, "EvictionByEvictionAPI",
    )


@pytest.mark.asyncio
async def test_real_observer_classifies_failed_job_with_pending_evicted_pod(tmp_path, monkeypatch):
    original = production._live_workload

    def workload(*args, **kwargs):
        value = original(*args, **kwargs)
        value["status"]["failed"] = 1
        return value

    monkeypatch.setattr(production, "_live_workload", workload)
    attempt = uuid4()
    ref, decision = production._collection_ref(attempt)
    pod = production._staged_pod(stage={"exitCode": 0})
    pod["status"] = disrupted()
    cluster, client = production._collection_cluster(
        tmp_path, ref, attempt, pod=pod, now=production.STAGE_FINISHED_AT + timedelta(seconds=1)
    )
    try:
        observation = await cluster.observe(ref, scheduling=decision)
        assert observation.failure_kind is FailureKind.INFRASTRUCTURE
        assert observation.failure_code == "DeletionByTaintManager"
        assert observation.failure_kind.retryable
    finally:
        await client.aclose()


def retained_job():
    return {
        "metadata": {"namespace": "fs2-models", "name": "scientific-job"},
        "spec": {"backoffLimit": 0, "podFailurePolicy": copy.deepcopy(_JOB_FAILURE_POLICY)},
        "status": {"failed": 1, "conditions": [{
            "type": "Failed", "status": "True", "reason": "PodFailurePolicy",
            "message": (
                "Pod fs2-models/scientific-job-abc12 has condition DisruptionTarget "
                "matching FailJob rule at index 1"
            ),
        }]},
    }


def test_deleted_pod_disruption_survives_in_exact_job_failure_policy():
    assert _reported_failure(["PodFailurePolicy"], [], model_id="rfdiffusion", job=retained_job()) == (
        WorkloadState.FAILED, FailureKind.INFRASTRUCTURE, "JobDisruptionTarget",
    )


@pytest.mark.parametrize("mutation", [
    "other_policy", "backoff", "other_namespace", "other_pod", "exit_rule", "unknown_rule",
    "failure_target", "false_condition", "generic_reason", "no_policy",
])
def test_untrusted_or_ambiguous_job_failure_is_not_retried(mutation):
    job = retained_job()
    condition = job["status"]["conditions"][0]
    if mutation == "other_policy":
        job["spec"]["podFailurePolicy"]["rules"][1]["action"] = "Ignore"
    elif mutation == "backoff":
        job["spec"]["backoffLimit"] = 1
    elif mutation == "other_namespace":
        condition["message"] = condition["message"].replace("fs2-models/", "other/")
    elif mutation == "other_pod":
        condition["message"] = condition["message"].replace("scientific-job-", "unrelated-job-")
    elif mutation == "exit_rule":
        condition["message"] = (
            "Container scientific-stage for pod fs2-models/scientific-job-abc12 "
            "failed with exit code 137 matching FailJob rule at index 0"
        )
    elif mutation == "unknown_rule":
        condition["message"] = condition["message"].replace("index 1", "index 2")
    elif mutation == "failure_target":
        condition["type"] = "FailureTarget"
    elif mutation == "false_condition":
        condition["status"] = "False"
    elif mutation == "generic_reason":
        condition["reason"] = "BackoffLimitExceeded"
    elif mutation == "no_policy":
        del job["spec"]["podFailurePolicy"]
    assert _reported_failure(["PodFailurePolicy"], [], model_id="rfdiffusion", job=job)[1] is FailureKind.APPLICATION


@pytest.mark.parametrize("reason,exit_code", [("OOMKilled", 137), ("MaximumExecutionTimeExceeded", 143), ("Error", 1)])
def test_retained_job_policy_does_not_override_observed_application_failure(reason, exit_code):
    pod = {"containerStatuses": [{"name": STAGE_CONTAINER_NAME, "state": {
        "terminated": {"exitCode": exit_code, "reason": reason},
    }}]}
    assert _reported_failure([reason], [pod], model_id="rfdiffusion", job=retained_job())[1] is FailureKind.APPLICATION


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [True, False])
async def test_observer_uses_terminal_job_disruption_when_pod_status_is_gone(tmp_path, monkeypatch, terminal):
    original = production._live_workload

    def workload(*args, **kwargs):
        value = original(*args, **kwargs)
        evidence = retained_job()
        if not terminal:
            evidence["status"]["conditions"][0]["type"] = "FailureTarget"
        name = value["metadata"]["name"]
        evidence["status"]["conditions"][0]["message"] = evidence["status"]["conditions"][0]["message"].replace(
            "scientific-job-", name + "-",
        )
        value["spec"].update(evidence["spec"])
        value["status"].update(evidence["status"])
        return value

    monkeypatch.setattr(production, "_live_workload", workload)
    attempt = uuid4()
    ref, decision = production._collection_ref(attempt)
    cluster, client = production._collection_cluster(
        tmp_path, ref, attempt, pod={}, now=production.STAGE_FINISHED_AT + timedelta(seconds=1),
    )
    try:
        observation = await cluster.observe(ref, scheduling=decision)
        if terminal:
            assert observation.failure_kind is FailureKind.INFRASTRUCTURE
            assert observation.failure_code == "JobDisruptionTarget"
        else:
            assert observation.state is WorkloadState.PENDING
            assert observation.failure_kind is None
        assert observation.pod_uids == ()
    finally:
        await client.aclose()

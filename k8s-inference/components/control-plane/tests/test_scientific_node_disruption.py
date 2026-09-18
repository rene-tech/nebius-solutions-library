"""Regression for the real Pending Pod eviction observed on18September2026."""

from datetime import timedelta
from uuid import uuid4

import pytest
import test_scientific_batch_production as production

from fs2_serve.scientific_batch.kubernetes import STAGE_CONTAINER_NAME, _reported_failure
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
        ("EvictionByEvictionAPI", "True"),
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

"""A peer eviction must not be hidden by MPI's secondary leader exit."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
import test_scientific_batch_production as production

from fs2_serve.scientific_batch.kubernetes import MODEL_LABEL, HttpScientificBatchCluster, _gromacs_gang_disruption
from fs2_serve.scientific_batch.models import FailureKind, WorkloadKind, WorkloadState


def statuses(*, offset=1, reason="EvictionByEvictionAPI", active=True):
    leader = production._staged_pod(stage={
        "exitCode": 1, "reason": "Error",
        "finishedAt": (production.STAGE_FINISHED_AT + timedelta(seconds=offset)).isoformat(),
    })["status"]
    peer = production._staged_pod(stage=None)["status"]
    peer["conditions"].append({
        "type": "DisruptionTarget", "status": "True" if active else "False",
        "reason": reason, "lastTransitionTime": production.STAGE_FINISHED_AT.isoformat(),
    })
    return [leader, peer]


@pytest.mark.parametrize("offset", [0, 1, 10])
@pytest.mark.parametrize("reverse", [False, True])
def test_peer_eviction_precedes_secondary_mpi_abort(offset, reverse):
    pods = statuses(offset=offset)
    assert _gromacs_gang_disruption(["Error"], pods[::-1] if reverse else pods) == (
        WorkloadState.FAILED, FailureKind.INFRASTRUCTURE, "EvictionByEvictionAPI",
    )


def test_interrupts_whole_gang_before_leader_abort():
    pods = statuses()
    pods[0] = production._staged_pod(stage=None)["status"]
    assert _gromacs_gang_disruption([], pods)[1] is FailureKind.INFRASTRUCTURE


def test_application_error_before_peer_disruption_is_not_retried():
    assert _gromacs_gang_disruption(["Error"], statuses(offset=-1)) is None


@pytest.mark.parametrize("reason", ["OOMKilled", "DeadlineExceeded", "MaximumExecutionTimeExceeded"])
def test_disruption_never_overrides_oom_or_timeout(reason):
    assert _gromacs_gang_disruption([reason, "Error"], statuses()) is None


@pytest.mark.parametrize("mutation", ["inactive", "generic", "missing_disruption_time", "missing_exit_time"])
def test_ambiguous_disruption_keeps_application_verdict(mutation):
    pods = statuses(active=mutation != "inactive", reason="GenericEviction" if mutation == "generic"
                    else "EvictionByEvictionAPI")
    if mutation == "missing_disruption_time":
        del pods[1]["conditions"][-1]["lastTransitionTime"]
    if mutation == "missing_exit_time":
        del pods[0]["containerStatuses"][0]["state"]["terminated"]["finishedAt"]
    assert _gromacs_gang_disruption(["Error"], pods) is None


def test_scheduler_preemption_preserves_preemption_class():
    assert _gromacs_gang_disruption(["Error"], statuses(reason="PreemptionByScheduler")) == (
        WorkloadState.PREEMPTED, FailureKind.PREEMPTION, "PreemptionByScheduler",
    )


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("model", ["gromacs-mpi", "other-gang"])
async def test_observer_uses_complete_gang_before_stalled_leader(tmp_path, monkeypatch, terminal, model):
    attempt = uuid4()
    ref, decision = production._collection_ref(attempt)
    ref = replace(ref, kind=WorkloadKind.JOB_SET)
    decision = replace(decision, accelerator_count=2)
    cluster, client = production._collection_cluster(
        tmp_path, ref, attempt, pod={}, now=production.STAGE_FINISHED_AT + timedelta(seconds=5),
    )
    old_request = HttpScientificBatchCluster._request

    async def request(self, method, path, **kwargs):
        if "/pods?" in path:
            pods = []
            for index, status in enumerate(statuses()):
                pod = production._staged_pod(stage=None)
                pod["metadata"]["uid"] = f"rank-{index}"
                pod["status"] = status
                pods.append(pod)
            return httpx.Response(200, json={"items": pods})
        if path.endswith("/" + ref.name):
            value = production._live_workload(
                ref, attempt, uid="gang-uid", gpu=2,
                spec=production._workload_spec(WorkloadKind.JOB_SET, gpu=1, gang_size=2),
                status={"conditions": [{"type": "Failed", "status": "True", "reason": "FailedJobs"}]}
                if terminal else {"startTime": "2026-09-03T03:00:00Z"},
            )
            value["metadata"]["labels"][MODEL_LABEL] = model
            return httpx.Response(200, json=value)
        return await old_request(self, method, path, **kwargs)

    async def admitted(*args, **kwargs):
        return None, None, True, None

    monkeypatch.setattr(HttpScientificBatchCluster, "_request", request)
    monkeypatch.setattr(HttpScientificBatchCluster, "_scheduling_admission", admitted)
    try:
        observation = await cluster.observe(ref, scheduling=decision)
        if model == "gromacs-mpi":
            assert observation.failure_kind is FailureKind.INFRASTRUCTURE
            assert observation.failure_code == "EvictionByEvictionAPI"
        else:
            assert observation.failure_kind is FailureKind.APPLICATION
    finally:
        await client.aclose()

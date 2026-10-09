"""Regression for admission to a dead pool without disrupting slow startup."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
import yaml
from scientific_batch_fakes import FakeScientificBatchCluster, FakeScientificBatchRepository
from test_scientific_batch_controller import NOW, snapshot

from fs2_serve.scientific_batch.codec import state_from_value, state_to_value
from fs2_serve.scientific_batch.kubernetes import HttpScientificBatchCluster, _pods_unstarted
from fs2_serve.scientific_batch.models import (
    AttemptOutcome,
    BatchStatus,
    FailureKind,
    LifecyclePhase,
    SchedulingAdmission,
    ScientificBatchPlan,
    ScientificStagePlan,
    WorkloadState,
)
from fs2_serve.scientific_batch.observation import DiagnosedWorkloadObservation
from fs2_serve.scientific_batch.pool_recovery import (
    POOL_UNAVAILABLE,
    SCHEDULING_TIMEOUT,
    PoolRecoveryPolicy,
    PoolRecoveryScientificCluster,
    PoolRecoveryScientificController,
    confirmed_pool_unavailability,
    retry_pools,
)
from fs2_serve.scientific_batch.recovery_view import recovery_view


def health_fixture():
    nodes = [
        {
            "metadata": {
                "creationTimestamp": (NOW - timedelta(days=2)).isoformat(),
                "labels": {"nebius.com/node-group-id": "group-1"},
            },
            "status": {
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "Unknown",
                        "reason": "NodeStatusUnknown",
                        "lastTransitionTime": (NOW - timedelta(hours=1)).isoformat(),
                    }
                ]
            },
        }
    ]
    autoscaler = {
        "nodeGroups": [
            {
                "name": "group-1",
                "health": {
                    "status": "Unhealthy",
                    "lastProbeTime": NOW.isoformat(),
                    "cloudProviderTarget": 1,
                    "nodeCounts": {"registered": {"total": 1, "notStarted": 0, "ready": 0}, "unregistered": 0},
                },
                "scaleUp": {"status": "Unhealthy"},
            }
        ]
    }
    return nodes, autoscaler


def test_dead_pool_requires_complete_fresh_failure_evidence():
    nodes, autoscaler = health_fixture()
    since, reason = confirmed_pool_unavailability(nodes, autoscaler, now=NOW, policy=PoolRecoveryPolicy())
    assert since == NOW - timedelta(hours=1) and reason == "AdmittedPoolUnavailable"


@pytest.mark.parametrize(
    "scenario",
    ["zero", "new-node", "healthy", "scaleup", "unregistered", "target", "stale", "ready-mismatch", "missing"],
)
def test_scale_from_zero_progress_and_unknown_health_do_not_trigger_fast_eviction(scenario):
    nodes, autoscaler = health_fixture()
    group = autoscaler["nodeGroups"][0]
    if scenario == "zero":
        nodes.clear()
    elif scenario == "new-node":
        nodes[0]["status"]["conditions"][0].update(status="False", reason="KubeletNotReady")
    elif scenario == "healthy":
        healthy = deepcopy(nodes[0])
        healthy["status"]["conditions"][0]["status"] = "True"
        nodes.append(healthy)
    elif scenario == "scaleup":
        group["scaleUp"]["status"] = "InProgress"
    elif scenario == "unregistered":
        group["health"]["nodeCounts"]["unregistered"] = 1
    elif scenario == "target":
        group["health"]["cloudProviderTarget"] = 2
    elif scenario == "stale":
        group["health"]["lastProbeTime"] = (NOW - timedelta(minutes=3)).isoformat()
    elif scenario == "ready-mismatch":
        group["health"]["nodeCounts"]["registered"]["ready"] = 1
    else:
        autoscaler.clear()
    assert confirmed_pool_unavailability(nodes, autoscaler, now=NOW, policy=PoolRecoveryPolicy())[0] is None


async def setup_batch(*, shards=("window-22", "window-23"), max_attempts=2):
    repository, cluster, clock = FakeScientificBatchRepository(), FakeScientificBatchCluster(), [NOW]
    controller = PoolRecoveryScientificController(
        repository=repository,
        cluster=cluster,
        controller_id="recovery-controller",
        namespace="fs2-scientific",
        clock=lambda: clock[0],
    )
    plan = ScientificBatchPlan(
        stages=(ScientificStagePlan(stage_id="windows", shards=shards, max_attempts=max_attempts),)
    )
    operation = uuid4()
    await controller.admit(
        operation_id=operation, tenant_id="ordinary-customer", model_id="gromacs", plan=plan, scheduling=snapshot(plan)
    )
    await controller.reconcile_once()
    return repository, cluster, clock, controller, operation


def pending(attempt, *, dead=True, admitted=True):
    return DiagnosedWorkloadObservation(
        ref=attempt.workload,
        attempt_id=attempt.attempt_id,
        state=WorkloadState.PENDING,
        phases=((LifecyclePhase.ADMITTED, LifecyclePhase.NODE_PENDING) if admitted else (LifecyclePhase.NODE_PENDING,)),
        scheduling_admission=(
            SchedulingAdmission(
                resolved_pool_id="h100-preemptible",
                admitted_resource_flavor="inference-h100-preemptible",
                accelerator_resource_name="nvidia.com/gpu",
                accelerator_count=1,
                admitted_at=NOW,
            )
            if admitted
            else None
        ),
        kueue_workload_uid="kueue-" + str(attempt.attempt_id),
        pod_uids=("pod-" + str(attempt.attempt_id),),
        pending_code="AdmittedPoolUnavailable" if dead else "NodeProvisioning",
        pods_unstarted=True,
        pool_unavailable_since=NOW - timedelta(hours=1) if dead else None,
    )


@pytest.mark.asyncio
async def test_dead_window_recovers_while_peer_runs_and_restart_cannot_reset_budget_or_backoff():
    repo, cluster, clock, controller, operation = await setup_batch()
    first, peer = repo.records[operation].stage("windows").attempts
    cluster.set_observation(first.workload, pending(first))
    cluster.set_observation(
        peer.workload,
        replace(
            pending(peer, dead=False),
            pods_unstarted=False,
            phases=(LifecyclePhase.ADMITTED, LifecyclePhase.IMAGE_LOADING),
            pending_code=None,
        ),
    )
    clock[0] = NOW + timedelta(seconds=119)
    await controller.reconcile_once()
    assert repo.records[operation].stage("windows").attempts[0].outcome is AttemptOutcome.ACTIVE
    clock[0] += timedelta(seconds=1)
    await controller.reconcile_once()
    failed = repo.records[operation].stage("windows").attempts[0]
    assert (failed.failure_code, failed.failure_kind, failed.outcome) == (
        POOL_UNAVAILABLE,
        FailureKind.INFRASTRUCTURE,
        AttemptOutcome.FAILED,
    )
    assert failed.scheduling_admission == pending(first).scheduling_admission
    # Reopen exactly the persisted state in a fresh controller; no in-memory budget.
    repo.records[operation] = state_from_value(state_to_value(repo.records[operation]))
    controller = PoolRecoveryScientificController(
        repository=repo,
        cluster=cluster,
        controller_id="replacement-controller",
        namespace="fs2-scientific",
        clock=lambda: clock[0],
    )
    cluster.deletion_polls_before_absent[cluster.key(first.workload)] = 2
    for _ in range(4):
        await controller.reconcile_once()
    assert len(cluster.apply_history) == 2
    assert repo.records[operation].stage("windows").attempts[0].resource_released
    clock[0] = NOW + timedelta(seconds=134)
    await controller.reconcile_once()
    assert len(cluster.apply_history) == 2
    clock[0] += timedelta(seconds=1)
    await controller.reconcile_once()
    state = repo.records[operation]
    retry = state.stage("windows").latest_attempt("window-22")
    assert retry.attempt_number == 2 and retry.attempt_id != first.attempt_id
    assert state.stage("windows").latest_attempt("window-23").attempt_id == peer.attempt_id
    assert cluster.delete_history == [first.workload]
    assert retry_pools(state, stage_id="windows", shard_id="window-22", attempt_number=2) == ("h100-capacity-block",)
    view = recovery_view(state, retry, policy=controller.recovery_policy)
    assert view.state == "retrying" and view.failed_pool_id == "h100-preemptible"
    assert view.admitted_wait_seconds == 120 and view.avoided_pool_ids == ("h100-preemptible",)
    # Exhaustion is durable and terminal; another restart cannot create attempt3.
    cluster.set_observation(retry.workload, pending(retry))
    await controller.reconcile_once()
    for _ in range(8):
        await controller.reconcile_once()
    assert repo.records[operation].status is BatchStatus.FAILED
    assert len(repo.records[operation].stage("windows").attempts) == 3
    assert (
        recovery_view(
            repo.records[operation], repo.records[operation].stage("windows").latest_attempt("window-22")
        ).state
        == "capacity_unavailable"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["not-admitted", "scale-zero", "loading", "running", "historical-start"])
async def test_fast_recovery_never_evicts_pending_reservations_or_started_work(case):
    repo, cluster, clock, controller, operation = await setup_batch(shards=("window-22",))
    attempt = repo.records[operation].stage("windows").attempts[0]
    observation = pending(attempt, admitted=case != "not-admitted", dead=case != "scale-zero")
    if case in {"loading", "running"}:
        observation = replace(
            observation,
            pods_unstarted=False,
            pool_unavailable_since=None,
            phases=(LifecyclePhase.ADMITTED, LifecyclePhase.IMAGE_LOADING),
            pending_code=None,
        )
    if case == "historical-start":
        stage = repo.records[operation].stage("windows")
        repo.records[operation] = replace(
            repo.records[operation],
            stages=(replace(stage, attempts=(replace(attempt, last_phase=LifecyclePhase.ARTIFACT_LOADING),)),),
        )
    cluster.set_observation(attempt.workload, observation)
    clock[0] = NOW + timedelta(seconds=300)
    await controller.reconcile_once()
    assert repo.records[operation].stage("windows").attempts[0].outcome is AttemptOutcome.ACTIVE
    assert not cluster.delete_calls


@pytest.mark.asyncio
async def test_no_spare_is_bounded_but_original_pool_can_return_within_remaining_attempt_budget():
    repo, cluster, clock, controller, operation = await setup_batch(shards=("window-22",))
    state = repo.records[operation]
    decision = replace(state.scheduling.stages[0], resolved_pool_preference=("h100-preemptible",))
    repo.records[operation] = replace(state, scheduling=replace(state.scheduling, stages=(decision,)))
    first = state.stage("windows").attempts[0]
    cluster.set_observation(first.workload, pending(first))
    clock[0] = NOW + timedelta(seconds=120)
    for _ in range(3):
        await controller.reconcile_once()
    clock[0] += timedelta(seconds=15)
    for _ in range(2):
        await controller.reconcile_once()
    state = repo.records[operation]
    assert retry_pools(state, stage_id="windows", shard_id="window-22", attempt_number=2) == ("h100-preemptible",)
    retry = state.stage("windows").latest_attempt("window-22")
    cluster.set_observation(
        retry.workload,
        replace(
            pending(retry, dead=False),
            pods_unstarted=False,
            phases=(LifecyclePhase.ADMITTED, LifecyclePhase.ACTIVE_COMPUTE),
            pending_code=None,
            state=WorkloadState.RUNNING,
        ),
    )
    clock[0] += timedelta(seconds=300)
    await controller.reconcile_once()
    assert repo.records[operation].stage("windows").latest_attempt("window-22").outcome is AttemptOutcome.ACTIVE


@pytest.mark.asyncio
async def test_unknown_health_still_has_distinct_bounded_post_admission_deadline():
    repo, cluster, clock, controller, operation = await setup_batch(shards=("window-22",))
    attempt = repo.records[operation].stage("windows").attempts[0]
    cluster.set_observation(attempt.workload, pending(attempt, dead=False))
    clock[0] = NOW + timedelta(seconds=7199)
    await controller.reconcile_once()
    assert repo.records[operation].stage("windows").attempts[0].outcome is AttemptOutcome.ACTIVE
    clock[0] += timedelta(seconds=1)
    await controller.reconcile_once()
    assert repo.records[operation].stage("windows").attempts[0].failure_code == SCHEDULING_TIMEOUT


@pytest.mark.parametrize("progress", ["node", "regular", "init", "ephemeral", "scheduled", "missing", "running"])
def test_any_gang_member_with_assignment_or_initialization_prevents_prestart_recovery(progress):
    first = {"spec": {}, "status": {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False"}]}}
    second = deepcopy(first)
    if progress == "node":
        second["spec"]["nodeName"] = "real-node"
    elif progress in {"regular", "init", "ephemeral"}:
        field = {
            "regular": "containerStatuses",
            "init": "initContainerStatuses",
            "ephemeral": "ephemeralContainerStatuses",
        }[progress]
        second["status"][field] = [{"state": {"waiting": {"reason": "ContainerCreating"}}}]
    elif progress == "scheduled":
        second["status"]["conditions"][0]["status"] = "True"
    elif progress == "missing":
        second.pop("status")
    else:
        second["status"]["phase"] = "Running"
    assert _pods_unstarted([first])
    assert not _pods_unstarted([first, second])
    assert not _pods_unstarted([])


@pytest.mark.asyncio
async def test_recovery_decision_and_retry_reservation_are_fenced_before_cluster_mutation():
    repo, cluster, clock, controller, operation = await setup_batch(shards=("window-22",))
    first = repo.records[operation].stage("windows").attempts[0]
    cluster.set_observation(first.workload, pending(first))
    clock[0] = NOW + timedelta(seconds=120)
    repo.fail_next_replace = True
    with pytest.raises(RuntimeError, match="injected durable replace"):
        await controller.reconcile_once()
    assert not cluster.delete_calls and len(cluster.apply_history) == 1
    for _ in range(3):
        await controller.reconcile_once()
    clock[0] += timedelta(seconds=15)
    repo.fail_next_replace = True
    with pytest.raises(RuntimeError, match="injected durable replace"):
        await controller.reconcile_once()
    assert len(cluster.apply_history) == 1
    await controller.reconcile_once()
    assert len(cluster.apply_history) == 2
    assert len(repo.records[operation].stage("windows").attempts) == 2


@pytest.mark.asyncio
async def test_deleted_reservation_is_not_reported_as_provider_preemption():
    repo, cluster, clock, controller, operation = await setup_batch(shards=("window-22",))
    first = repo.records[operation].stage("windows").attempts[0]
    cluster.set_observation(first.workload, pending(first, dead=False))
    await controller.reconcile_once()
    cluster.set_observation(
        first.workload, replace(pending(first, dead=False), kueue_workload_uid="operator-recreated")
    )
    await controller.reconcile_once()
    failed = repo.records[operation].stage("windows").attempts[0]
    assert failed.failure_code == "kueue_workload_recreated"
    assert failed.failure_kind is FailureKind.INFRASTRUCTURE and failed.outcome is AttemptOutcome.FAILED
    assert failed.last_phase is LifecyclePhase.NODE_PENDING


@pytest.mark.asyncio
async def test_customer_cancel_during_recovery_never_creates_another_attempt():
    repo, cluster, clock, controller, operation = await setup_batch(shards=("window-22",))
    first = repo.records[operation].stage("windows").attempts[0]
    cluster.set_observation(first.workload, pending(first))
    clock[0] = NOW + timedelta(seconds=120)
    await controller.reconcile_once()
    repo.records[operation] = replace(repo.records[operation], cancel_requested=True)
    clock[0] += timedelta(seconds=100)
    for _ in range(8):
        await controller.reconcile_once()
    state = repo.records[operation]
    assert state.status is BatchStatus.CANCELLED and len(cluster.apply_history) == 1
    assert all(attempt.resource_released for attempt in state.stage("windows").attempts)
    assert recovery_view(state, state.stage("windows").attempts[0]).state == "cancelled"


@pytest.mark.asyncio
async def test_retry_writer_only_narrows_frozen_qualified_pools_and_replays_identically(tmp_path, monkeypatch):
    repo, cluster, clock, controller, operation = await setup_batch(shards=("window-22",))
    first = repo.records[operation].stage("windows").attempts[0]
    cluster.set_observation(first.workload, pending(first))
    clock[0] = NOW + timedelta(seconds=120)
    for _ in range(3):
        await controller.reconcile_once()
    clock[0] += timedelta(seconds=15)
    for _ in range(2):
        await controller.reconcile_once()
    original = cluster.apply_history[-1]
    assert original.attempt_number == 2
    written = []

    async def capture(self, resource, *, controller_fence):
        written.append(resource)
        return resource.ref

    monkeypatch.setattr(HttpScientificBatchCluster, "apply", capture)
    async with httpx.AsyncClient() as client:
        writer = PoolRecoveryScientificCluster(
            recovery_repository=repo,
            recovery_policy=PoolRecoveryPolicy(),
            base_url="https://kubernetes.test",
            token_file=tmp_path / "unused",
            ca_file=tmp_path / "unused",
            controller_id="recovery",
            fence=repo,
            renderer=None,
            writes_enabled=True,
            client=client,
        )
        await writer.apply(original, controller_fence=1)
        # A fresh process would derive exactly the same manifest after a lost apply response.
        await writer.apply(original, controller_fence=2)
    assert written[0] == written[1]
    assert written[0].scheduling.resolved_pool_preference == ("h100-capacity-block",)
    assert replace(written[0], scheduling=original.scheduling) == original
    assert written[0].scheduling_snapshot_digest == repo.records[operation].scheduling.digest
    assert written[0].scheduling.workload_priority_value == original.scheduling.workload_priority_value


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["dead", "paginated", "unavailable", "scaleup"])
async def test_real_health_reader_uses_scoped_kubernetes_and_autoscaler_evidence(tmp_path, monkeypatch, mode):
    repo, cluster, clock, controller, operation = await setup_batch(shards=("window-22",))
    attempt = repo.records[operation].stage("windows").attempts[0]
    observation = pending(attempt, dead=False)
    nodes, autoscaler = health_fixture()
    if mode == "scaleup":
        autoscaler["nodeGroups"][0]["scaleUp"]["status"] = "InProgress"
    calls = []

    async def base_observe(self, ref, *, scheduling):
        return observation

    async def handler(request):
        calls.append(request)
        if mode == "unavailable":
            return httpx.Response(403, json={"kind": "Status"})
        if request.url.path == "/api/v1/nodes":
            assert request.url.params["labelSelector"] == "accelerator.fs2.nebius/pool-id=h100-preemptible"
            return httpx.Response(
                200, json={"items": nodes, "metadata": {"continue": "next" if mode == "paginated" else ""}}
            )
        assert request.url.path == "/api/v1/namespaces/kube-system/configmaps/cluster-autoscaler-status"
        return httpx.Response(200, json={"data": {"status": yaml.safe_dump(autoscaler)}})

    monkeypatch.setattr(HttpScientificBatchCluster, "observe", base_observe)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.test") as client:
        reader = PoolRecoveryScientificCluster(
            recovery_repository=repo,
            recovery_policy=PoolRecoveryPolicy(),
            base_url="https://kubernetes.test",
            token_file=tmp_path / "unused",
            ca_file=tmp_path / "unused",
            controller_id="recovery",
            fence=repo,
            renderer=None,
            writes_enabled=False,
            client=client,
            clock=lambda: NOW,
        )
        monkeypatch.setattr(reader, "_headers", lambda: {})
        observed = await reader.observe(attempt.workload, scheduling=repo.records[operation].scheduling.stages[0])
    assert observed.state is WorkloadState.PENDING
    if mode == "dead":
        assert observed.pool_unavailable_since == NOW - timedelta(hours=1)
        assert observed.pending_code == "AdmittedPoolUnavailable"
    else:
        assert observed.pool_unavailable_since is None
        assert observed.pending_code == ("PoolScaleUpInProgress" if mode == "scaleup" else "PoolHealthUnknown")

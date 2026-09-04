from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_fast_start import evidence, with_evidence, with_fast_start
from test_model_deployment import (
    envelope,
    model_spec,
    render_gpu_resident,
    render_host_memory,
    render_regional_cache,
    renderer,
    reserved_and_preemptible_envelope,
)

from fs2_serve.fast_start import FastStartLevel
from fs2_serve.fast_start_policy import FastStartHistoryWindow
from fs2_serve.model_deployment import (
    FIELD_MANAGER,
    FINALIZER,
    CacheTier,
    DesiredState,
    DrainObservation,
    InfrastructureEnvelope,
    LifecycleSpec,
    ModelDeploymentSpec,
    ObservedResource,
    RenderContext,
    RenderedResource,
    RenderPlan,
    canonical_digest,
    plan_reconciliation,
)
from fs2_serve.model_deployment_bridge import _normalize_keys
from fs2_serve.model_deployment_controller import (
    BoundedKeyQueue,
    ControllerHealth,
    Discovery,
    FenceLostError,
    HttpKubernetesModelClient,
    LeaseFence,
    ModelControllerApi,
    ModelDeploymentController,
    ModelKey,
    PodSnapshot,
    PostgresActiveOperations,
    PrometheusActiveOperations,
    ResourceSnapshot,
    build_status,
)
from fs2_serve.model_deployment_records import ModelDeploymentObservedStatus


class FakePrometheusReader:
    def __init__(self, value: float | None) -> None:
        self.value = value
        self.queries: list[str] = []

    async def scalar(self, query: str, *, at: datetime) -> float | None:
        assert at.tzinfo is not None
        self.queries.append(query)
        return self.value


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeDatabaseConnection:
    def __init__(self, value: int) -> None:
        self.value = value
        self.calls: list[tuple[str, str]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, model_ref: str) -> None:
        self.calls.append(("execute", " ".join(query.split())))
        assert model_ref == "qwen3-8b"

    async def fetchval(self, query: str, model_ref: str) -> int:
        self.calls.append(("fetchval", " ".join(query.split())))
        assert model_ref == "qwen3-8b"
        return self.value


class FakeAcquire:
    def __init__(self, connection: FakeDatabaseConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeDatabaseConnection:
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeDatabasePool:
    def __init__(self, value: int) -> None:
        self.connection = FakeDatabaseConnection(value)

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


async def test_prometheus_active_operations_projects_dynamic_demand_conservatively() -> None:
    reader = FakePrometheusReader(2.1)
    active = PrometheusActiveOperations(reader)  # type: ignore[arg-type]

    assert await active.active_operations(tenant_id="tenant-a", model_ref="qwen3-8b") == 3
    assert reader.queries == [
        'sum(max by (model, state) (fs2_serve_operations{model="qwen3-8b",'
        'state=~"queued|activating|running"})) OR vector(0)'
    ]


async def test_postgres_active_operations_counts_under_the_admission_model_fence() -> None:
    pool = FakeDatabasePool(4)
    active = PostgresActiveOperations(pool)  # type: ignore[arg-type]

    assert await active.active_operations(tenant_id="tenant-a", model_ref="qwen3-8b") == 4
    assert pool.connection.calls == [
        ("execute", "SELECT pg_advisory_xact_lock(fs2_activation_model_lock_key($1))"),
        (
            "fetchval",
            "SELECT count(*) FROM fs2_operations WHERE model_id=$1 AND status IN ('queued','activating','running')",
        ),
    ]


def fence(token: str = "a" * 32) -> LeaseFence:
    return LeaseFence(
        namespace="fs2-system",
        name="fs2-model-controller",
        holder_identity="fs2-system/controller:pod-uid",
        token=token,
        resource_version="7",
        renew_time=datetime.now(UTC),
        duration_seconds=15,
    )


def model_object(*, generation: int = 1, deleting: bool = False) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "name": "qwen-live",
        "namespace": "fs2-models",
        "uid": "cr-uid-1",
        "resourceVersion": str(generation),
        "generation": generation,
        "finalizers": [],
    }
    if deleting:
        metadata["deletionTimestamp"] = "2026-09-02T08:00:00Z"
    return {
        "apiVersion": "inference.fs2.nebius.ai/v1alpha1",
        "kind": "ModelDeployment",
        "metadata": metadata,
        "spec": model_spec().model_dump(mode="json", by_alias=True),
    }


def snapshot(resource: RenderedResource, *, ready: bool = True, owner_uid: str = "cr-uid-1") -> ResourceSnapshot:
    requested_replicas = resource.manifest.get("spec", {}).get("replicas")
    replicas = (
        0
        if resource.kind == "Deployment" and requested_replicas == 0
        else 1
        if resource.kind == "Deployment" and ready
        else 0
        if resource.kind == "Deployment"
        else None
    )
    raw = copy.deepcopy(resource.manifest)
    metadata = raw["metadata"]
    managed_fields: list[dict[str, Any]] = [{"manager": FIELD_MANAGER, "fieldsV1": {"f:spec": {}}}]
    replica_managers: list[str] = []
    if resource.kind == "Deployment":
        raw["spec"]["replicas"] = replicas
        replica_manager = FIELD_MANAGER if requested_replicas is not None else "horizontal-pod-autoscaler"
        managed_fields.append({"manager": replica_manager, "fieldsV1": {"f:spec": {"f:replicas": {}}}})
        replica_managers.append(replica_manager)
    metadata.update(
        {
            "uid": f"uid-{resource.kind.lower()}-{resource.name}",
            "resourceVersion": "3",
            "generation": 1,
            "managedFields": managed_fields,
        }
    )
    if resource.kind == "Deployment":
        raw["status"] = {
            "observedGeneration": 1,
            "replicas": replicas,
            "updatedReplicas": replicas,
            "readyReplicas": replicas,
            "availableReplicas": replicas,
            "unavailableReplicas": 0,
        }
    return ResourceSnapshot(
        observed=ObservedResource(
            api_version=resource.api_version,
            kind=resource.kind,
            namespace=resource.namespace,
            name=resource.name,
            uid=metadata["uid"],
            digest=resource.digest,
            controller_owner_uid=owner_uid,
            field_managers=[FIELD_MANAGER],
        ),
        resource_version="3",
        generation=1,
        observed_generation=1 if resource.kind == "Deployment" else None,
        desired_replicas=replicas if resource.kind == "Deployment" else None,
        replicas=replicas,
        updated_replicas=replicas,
        ready_replicas=replicas,
        available_replicas=replicas,
        unavailable_replicas=0 if resource.kind == "Deployment" else None,
        replica_field_managers=replica_managers,
        raw=raw,
    )


def hpa_snapshot(scaler: ResourceSnapshot) -> ResourceSnapshot:
    name = f"keda-hpa-{scaler.observed.name}"
    target = scaler.raw["spec"]["scaleTargetRef"]
    raw = {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {
            "name": name,
            "namespace": "fs2-models",
            "uid": f"uid-hpa-{name}",
            "resourceVersion": "4",
            "generation": 1,
            "ownerReferences": [{"uid": scaler.observed.uid, "controller": True}],
            "managedFields": [{"manager": "keda-operator"}],
        },
        "spec": {"scaleTargetRef": copy.deepcopy(target)},
        "status": {
            "observedGeneration": 1,
            "currentReplicas": 0,
            "desiredReplicas": 0,
            "conditions": [
                {"type": "AbleToScale", "status": "True"},
                {"type": "ScalingActive", "status": "True"},
            ],
        },
    }
    return ResourceSnapshot(
        observed=ObservedResource(
            api_version="autoscaling/v2",
            kind="HorizontalPodAutoscaler",
            namespace="fs2-models",
            name=name,
            uid=f"uid-hpa-{name}",
            digest=canonical_digest(
                {
                    "apiVersion": "autoscaling/v2",
                    "kind": "HorizontalPodAutoscaler",
                    "metadata": {
                        "namespace": "fs2-models",
                        "name": name,
                        "uid": f"uid-hpa-{name}",
                        "resourceVersion": "4",
                    },
                }
            ),
            controller_owner_uid=scaler.observed.uid,
            field_managers=["keda-operator"],
        ),
        resource_version="4",
        generation=1,
        observed_generation=1,
        raw=raw,
    )


def idle_zero_hpa_snapshot(scaler: ResourceSnapshot) -> ResourceSnapshot:
    item = hpa_snapshot(scaler)
    item.raw["metadata"].pop("generation")
    item.raw["status"] = {
        "desiredReplicas": 0,
        "conditions": [
            {"type": "AbleToScale", "status": "True", "reason": "SucceededGetScale"},
            {"type": "ScalingActive", "status": "False", "reason": "ScalingDisabled"},
        ],
    }
    return item.model_copy(update={"generation": 0, "observed_generation": None})


class FakeApi(ModelControllerApi):
    def __init__(self, model: dict[str, Any], *, writes_enabled: bool = True) -> None:
        self.model = copy.deepcopy(model)
        self.writes_enabled = writes_enabled
        self.resources: dict[str, ResourceSnapshot] = {}
        self.calls: list[tuple[str, str]] = []
        self.live_fence = fence()
        self.status_writes: list[dict[str, Any]] = []
        self.auto_create_hpa = True
        self.hold_hpa_gc = False

    async def acquire_or_renew_lease(
        self,
        *,
        namespace: str,
        name: str,
        holder_identity: str,
        token: str | None,
        duration_seconds: int,
    ) -> LeaseFence | None:
        self.calls.append(("lease", f"{namespace}/{name}"))
        return self.live_fence if self.writes_enabled else None

    async def assert_fence(self, current: LeaseFence) -> None:
        if current.token != self.live_fence.token:
            raise FenceLostError("stale fence")

    async def list_models(self, namespace: str) -> list[dict[str, Any]]:
        self.calls.append(("list", namespace))
        return [copy.deepcopy(self.model)]

    async def get_model(self, key: ModelKey) -> dict[str, Any] | None:
        self.calls.append(("get", key.text))
        return copy.deepcopy(self.model)

    async def discover(self, *, key: ModelKey, owner_uid: str, render: RenderPlan) -> Discovery:
        self.calls.append(("discover", key.text))
        return Discovery(resources=[item.model_copy(deep=True) for item in self.resources.values()], complete=True)

    async def apply_resource(
        self,
        resource: RenderedResource,
        *,
        owner_uid: str,
        fence: LeaseFence,
    ) -> ResourceSnapshot:
        await self.assert_fence(fence)
        self.calls.append(("apply", resource.kind))
        item = snapshot(resource, ready=True, owner_uid=owner_uid)
        if resource.kind == "ScaledObject":
            item.raw["status"] = {
                "hpaName": f"keda-hpa-{resource.name}",
                "conditions": [{"type": "Ready", "status": "True"}],
            }
        self.resources[item.observed.identity] = item
        if resource.kind == "ScaledObject" and self.auto_create_hpa:
            generated = hpa_snapshot(item)
            self.resources[generated.observed.identity] = generated
        return item

    async def delete_resource(
        self,
        identity: str,
        *,
        owner_uid: str,
        fence: LeaseFence,
    ) -> bool:
        await self.assert_fence(fence)
        self.calls.append(("delete", identity))
        current = self.resources.get(identity)
        if current is None:
            return False
        assert current.observed.controller_owner_uid == owner_uid
        del self.resources[identity]
        if current.observed.kind == "ScaledObject" and not self.hold_hpa_gc:
            hpa_identity = f"autoscaling/v2/HorizontalPodAutoscaler/fs2-models/keda-hpa-{current.observed.name}"
            self.resources.pop(hpa_identity, None)
        return True

    async def set_finalizer(
        self,
        key: ModelKey,
        *,
        owner_uid: str,
        present: bool,
        fence: LeaseFence,
    ) -> None:
        await self.assert_fence(fence)
        self.calls.append(("finalizer", str(present)))
        assert self.model["metadata"]["uid"] == owner_uid
        values = self.model["metadata"].setdefault("finalizers", [])
        if present and FINALIZER not in values:
            values.append(FINALIZER)
        if not present and FINALIZER in values:
            values.remove(FINALIZER)

    async def patch_status(
        self,
        key: ModelKey,
        *,
        owner_uid: str,
        generation: int,
        status: dict[str, Any],
        fence: LeaseFence,
    ) -> bool:
        await self.assert_fence(fence)
        assert self.model["metadata"]["uid"] == owner_uid
        assert self.model["metadata"]["generation"] == generation
        current = self.model.get("status", {}).get("observedGeneration", 0)
        if current > generation:
            return False
        self.calls.append(("status", status["phase"]))
        self.model["status"] = copy.deepcopy(status)
        self.status_writes.append(copy.deepcopy(status))
        return True


class ZeroActiveOperations:
    async def active_operations(self, *, tenant_id: str, model_ref: str) -> int | None:
        return 0


class MutableActiveOperations:
    def __init__(self, value: int | None) -> None:
        self.value = value

    async def active_operations(self, *, tenant_id: str, model_ref: str) -> int | None:
        return self.value


def controller(
    api: FakeApi,
    *,
    writes_enabled: bool = True,
    active_operations: object | None = None,
) -> ModelDeploymentController:
    return ModelDeploymentController(
        api=api,
        envelope=envelope(),
        renderer=renderer(),
        namespace="fs2-models",
        holder_identity="fs2-system/controller:pod-uid",
        prometheus_server_address="http://prometheus:9090",
        writes_enabled=writes_enabled,
        active_operations=active_operations or ZeroActiveOperations(),  # type: ignore[arg-type]
        queue_capacity=2,
        worker_count=1,
        poll_seconds=0.01,
    )


def test_bounded_queue_deduplicates_and_defers_overload() -> None:
    queue = BoundedKeyQueue(2)
    first = ModelKey(namespace="fs2-models", name="a")
    assert queue.put(first)
    assert queue.put(first)
    assert queue.put(ModelKey(namespace="fs2-models", name="b"))
    assert not queue.put(ModelKey(namespace="fs2-models", name="c"))
    assert queue.depth == 2 and queue.dropped == 1


@pytest.mark.asyncio
async def test_observe_only_kill_switch_reads_but_never_mutates() -> None:
    api = FakeApi(model_object(), writes_enabled=False)
    subject = controller(api, writes_enabled=False)
    assert await subject.run_cycle()
    result = await subject.reconcile(ModelKey(namespace="fs2-models", name="qwen-live"))
    assert result.action == "observe-only:apply"
    assert not any(call[0] in {"apply", "delete", "finalizer", "status"} for call in api.calls)


@pytest.mark.asyncio
async def test_controller_adds_finalizer_before_apply_then_observes_exact_endpoint() -> None:
    api = FakeApi(model_object())
    subject = controller(api)
    key = ModelKey(namespace="fs2-models", name="qwen-live")

    first = await subject.reconcile(key, fence())
    assert first.action == "finalizer-added" and first.requeue
    assert not any(call[0] == "apply" for call in api.calls)

    second = await subject.reconcile(key, fence())
    assert second.action == "autoscaler-bootstrap" and second.requeue
    bootstrap = next(item for item in api.resources.values() if item.observed.kind == "Deployment")
    assert bootstrap.raw["spec"]["replicas"] == 0
    assert bootstrap.replica_field_managers == [FIELD_MANAGER]
    assert api.status_writes[-1]["phase"] != "Ready"

    third = await subject.reconcile(key, fence())
    assert third.action == "autoscaler-handoff" and third.requeue
    assert api.status_writes[-1]["phase"] == "Ready"
    assert api.status_writes[-1]["endpoint"] == {
        "namespace": "fs2-models",
        "serviceName": "qwen-runtime",
        "servicePort": 8000,
        "uid": "uid-service-qwen-runtime",
        "digest": next(item.observed.digest for item in api.resources.values() if item.observed.kind == "Service"),
    }
    deployment = next(item for item in api.resources.values() if item.observed.kind == "Deployment")
    assert FIELD_MANAGER not in deployment.replica_field_managers


@pytest.mark.asyncio
async def test_controller_hands_off_every_bounded_burst_segment_and_aggregates_status() -> None:
    infrastructure = reserved_and_preemptible_envelope()
    spec = model_spec().model_copy(
        update={
            "placement": model_spec().placement.model_copy(update={"pool_refs": ["reserved-h100", "preemptible-h100"]}),
            "availability": model_spec().availability.model_copy(update={"min_replicas": 1, "max_replicas": 4}),
        }
    )
    raw = model_object()
    raw["spec"] = spec.model_dump(mode="json", by_alias=True)
    api = FakeApi(raw)
    subject = ModelDeploymentController(
        api=api,
        envelope=infrastructure,
        renderer=renderer(),
        namespace="fs2-models",
        holder_identity="fs2-system/controller:pod-uid",
        prometheus_server_address="http://prometheus:9090",
        writes_enabled=True,
        active_operations=ZeroActiveOperations(),
        queue_capacity=2,
        worker_count=1,
        poll_seconds=0.01,
    )
    key = ModelKey(namespace="fs2-models", name="qwen-live")

    assert (await subject.reconcile(key, fence())).action == "finalizer-added"
    assert (await subject.reconcile(key, fence())).action == "autoscaler-bootstrap"
    assert (await subject.reconcile(key, fence())).action == "autoscaler-handoff"

    deployments = [item for item in api.resources.values() if item.observed.kind == "Deployment"]
    scalers = [item for item in api.resources.values() if item.observed.kind == "ScaledObject"]
    assert len(deployments) == 3 and len(scalers) == 2
    autoscaled_names = {item.raw["spec"]["scaleTargetRef"]["name"] for item in scalers}
    assert len(autoscaled_names) == 2
    assert all(
        FIELD_MANAGER not in item.replica_field_managers
        for item in deployments
        if item.observed.name in autoscaled_names
    )
    hot = next(item for item in deployments if item.observed.name not in autoscaled_names)
    assert hot.desired_replicas == 1 and hot.replica_field_managers == [FIELD_MANAGER]
    status = api.status_writes[-1]
    assert status["eligiblePoolRefs"] == ["preemptible-h100", "reserved-h100"]
    assert {(item["poolRef"], item["role"]) for item in status["placements"]} == {
        ("reserved-h100", "hot"),
        ("reserved-h100", "burst"),
        ("preemptible-h100", "burst"),
    }


@pytest.mark.asyncio
async def test_multi_pool_drain_preserves_the_existing_hot_boundary_while_work_is_active() -> None:
    infrastructure = reserved_and_preemptible_envelope()
    spec = model_spec().model_copy(
        update={
            "placement": model_spec().placement.model_copy(update={"pool_refs": ["reserved-h100", "preemptible-h100"]}),
            "availability": model_spec().availability.model_copy(update={"min_replicas": 1, "max_replicas": 4}),
        }
    )
    raw = model_object()
    raw["spec"] = spec.model_dump(mode="json", by_alias=True)
    api = FakeApi(raw)
    active = MutableActiveOperations(1)
    subject = ModelDeploymentController(
        api=api,
        envelope=infrastructure,
        renderer=renderer(),
        namespace="fs2-models",
        holder_identity="fs2-system/controller:pod-uid",
        prometheus_server_address="http://prometheus:9090",
        writes_enabled=True,
        active_operations=active,
        queue_capacity=2,
        worker_count=1,
        poll_seconds=0.01,
    )
    key = ModelKey(namespace="fs2-models", name="qwen-live")
    assert (await subject.reconcile(key, fence())).action == "finalizer-added"
    assert (await subject.reconcile(key, fence())).action == "autoscaler-bootstrap"
    assert (await subject.reconcile(key, fence())).action == "autoscaler-handoff"
    hot_before = next(
        item
        for item in api.resources.values()
        if item.observed.kind == "Deployment"
        and item.raw["metadata"]["annotations"]["fs2-serve.nebius.ai/workload-role"] == "hot"
    )
    hot_identity = hot_before.observed.identity

    draining = spec.model_copy(
        update={
            "lifecycle": LifecycleSpec(desired_state=DesiredState.DRAINING),
            "availability": spec.availability.model_copy(update={"min_replicas": 0}),
        }
    )
    api.model["spec"] = draining.model_dump(mode="json", by_alias=True)
    api.model["metadata"]["generation"] = 2
    api.calls.clear()

    first = await subject.reconcile(key, fence())
    assert first.action == "drain:delete-first"
    assert [identity for action, identity in api.calls if action == "delete"]
    assert all("publication" in identity for action, identity in api.calls if action == "delete")
    api.calls.clear()
    second = await subject.reconcile(key, fence())
    assert second.action in {"autoscaler-install-pending", "drain"}
    assert hot_identity in api.resources
    assert api.resources[hot_identity].desired_replicas == 1
    assert not any(action == "delete" and "/Deployment/" in identity for action, identity in api.calls)


def test_status_never_publishes_endpoint_or_ready_from_desired_state_alone() -> None:
    spec = model_spec()
    context = RenderContext(
        name="qwen-live",
        namespace="fs2-models",
        uid="cr-uid-1",
        generation=1,
        pool=envelope().pools["pool-b"],
        eligible_pools=[envelope().pools[pool_ref] for pool_ref in model_spec().placement.pool_refs],
        prometheus_server_address="http://prometheus:9090",
    )
    plan = plan_reconciliation(
        generation=1,
        deleting=False,
        spec=spec,
        envelope=envelope(),
        renderer=renderer(),
        render_context=context,
        observed=[],
        discovery_complete=True,
    )
    status = build_status(
        spec=spec,
        owner_uid="cr-uid-1",
        generation=1,
        plan=plan,
        discovery=Discovery(resources=[], complete=True),
        previous_status={},
        drain=None,
    )
    assert status["phase"] == "Desired"
    assert "endpoint" not in status
    ready = next(item for item in status["conditions"] if item["type"] == "Ready")
    assert ready["status"] == "False"


def test_status_time_is_strictly_monotonic_for_same_generation_reconciles() -> None:
    spec = model_spec()
    context = RenderContext(
        name="qwen-live",
        namespace="fs2-models",
        uid="cr-uid-1",
        generation=1,
        pool=envelope().pools["pool-b"],
        eligible_pools=[envelope().pools[pool_ref] for pool_ref in model_spec().placement.pool_refs],
        prometheus_server_address="http://prometheus:9090",
    )
    plan = plan_reconciliation(
        generation=1,
        deleting=False,
        spec=spec,
        envelope=envelope(),
        renderer=renderer(),
        render_context=context,
        observed=[],
        discovery_complete=True,
    )
    fixed = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
    first = build_status(
        spec=spec,
        owner_uid="cr-uid-1",
        generation=1,
        plan=plan,
        discovery=Discovery(resources=[], complete=True),
        previous_status={},
        drain=None,
        now=fixed,
    )
    second = build_status(
        spec=spec,
        owner_uid="cr-uid-1",
        generation=1,
        plan=plan,
        discovery=Discovery(resources=[], complete=True),
        previous_status=first,
        drain=None,
        now=fixed,
    )
    first_at = datetime.fromisoformat(first["lastReconcileTime"].replace("Z", "+00:00"))
    second_at = datetime.fromisoformat(second["lastReconcileTime"].replace("Z", "+00:00"))
    assert second_at - first_at == timedelta(microseconds=1)


def test_status_projects_localizing_and_warming_from_observed_runtime_pods() -> None:
    spec = model_spec()
    context = RenderContext(
        name="qwen-live",
        namespace="fs2-models",
        uid="cr-uid-1",
        generation=1,
        pool=envelope().pools["pool-b"],
        eligible_pools=[envelope().pools[pool_ref] for pool_ref in model_spec().placement.pool_refs],
        prometheus_server_address="http://prometheus:9090",
    )
    render = renderer().render(spec, context)
    resources = [snapshot(item, ready=True) for item in render.resources]
    deployment_index = next(index for index, item in enumerate(resources) if item.observed.kind == "Deployment")
    resources[deployment_index] = resources[deployment_index].model_copy(
        update={
            "desired_replicas": 1,
            "replicas": 1,
            "updated_replicas": 1,
            "ready_replicas": 0,
            "available_replicas": 0,
            "unavailable_replicas": 1,
            "replica_field_managers": ["horizontal-pod-autoscaler"],
        }
    )
    scaler = next(item for item in resources if item.observed.kind == "ScaledObject")
    scaler.raw["status"] = {
        "hpaName": f"keda-hpa-{scaler.observed.name}",
        "conditions": [{"type": "Ready", "status": "True"}],
    }
    resources.append(hpa_snapshot(scaler))
    plan = plan_reconciliation(
        generation=1,
        deleting=False,
        spec=spec,
        envelope=envelope(),
        renderer=renderer(),
        render_context=context,
        observed=[item.observed for item in resources],
        discovery_complete=True,
    )
    pod = PodSnapshot(
        name="qwen-runtime-1",
        uid="pod-uid",
        resource_version="4",
        phase="Pending",
        scheduled=True,
        initialized=False,
        containers_started=False,
        ready=False,
    )
    localizing = build_status(
        spec=spec,
        owner_uid="cr-uid-1",
        generation=1,
        plan=plan,
        discovery=Discovery(resources=resources, pods=[pod], complete=True),
        previous_status={},
        drain=None,
    )
    assert localizing["phase"] == "Localizing"
    assert localizing["replicas"] == {
        "desired": 1,
        "admitted": 1,
        "nodePending": 0,
        "localizing": 1,
        "runtimeStarting": 0,
        "warming": 0,
        "ready": 0,
        "available": 0,
    }
    assert localizing["cache"]["state"] == "Localizing"

    warming = build_status(
        spec=spec,
        owner_uid="cr-uid-1",
        generation=1,
        plan=plan,
        discovery=Discovery(
            resources=resources,
            pods=[pod.model_copy(update={"phase": "Running", "initialized": True, "containers_started": True})],
            complete=True,
        ),
        previous_status=localizing,
        drain=None,
    )
    assert warming["phase"] == "Warming"
    assert warming["replicas"]["warming"] == 1


def test_status_keeps_fast_start_levels_apart_and_claims_effective_only_when_converged() -> None:
    context = RenderContext(
        name="qwen-live",
        namespace="fs2-models",
        uid="cr-uid-1",
        generation=1,
        pool=envelope().pools["pool-b"],
        eligible_pools=[envelope().pools[pool_ref] for pool_ref in model_spec().placement.pool_refs],
        prometheus_server_address="http://prometheus:9090",
    )

    # Without evidence nothing is claimed: no target, no seconds, no effective level.
    plain = plan_reconciliation(
        generation=1,
        deleting=False,
        spec=model_spec(),
        envelope=envelope(),
        renderer=renderer(),
        render_context=context,
        observed=[],
        discovery_complete=True,
    )
    unqualified = build_status(
        spec=model_spec(),
        owner_uid="cr-uid-1",
        generation=1,
        plan=plain,
        discovery=Discovery(resources=[], complete=True),
        previous_status={},
        drain=None,
    )
    assert unqualified["fastStart"]["qualification"]["state"] == "NoTarget"
    assert unqualified["fastStart"]["assignedLevel"] == "Off"
    for absent in ("modelStart", "capacityWait", "endToEnd", "targetSeconds", "effectiveLevel"):
        assert absent not in unqualified["fastStart"]
    condition = next(item for item in unqualified["conditions"] if item["type"] == "FastStartQualified")
    assert (condition["status"], condition["reason"]) == ("True", "NoFastStartTarget")

    # pool-a qualifies L3 but the slower pool-b binds the model at L2.
    spec = with_fast_start(model_spec(), level="L3")
    pools = envelope().pools
    installed = with_evidence(
        envelope(),
        evidence(model_spec(), pools["pool-a"], seconds=[40, 45, 50, 55, 58] * 4),
        evidence(model_spec(), pools["pool-b"], seconds=[70, 80, 90, 100, 110] * 4),
    )
    plan = plan_reconciliation(
        generation=1,
        deleting=False,
        spec=spec,
        envelope=installed,
        renderer=renderer(),
        render_context=context,
        observed=[],
        discovery_complete=True,
    )
    assert plan.validation.fast_start is not None
    current_identity = plan.validation.fast_start.selected_identity_digest
    assert current_identity is not None
    pending = build_status(
        spec=spec,
        owner_uid="cr-uid-1",
        generation=1,
        plan=plan,
        discovery=Discovery(resources=[], complete=True),
        previous_status={
            "fastStart": {
                "effectiveLevel": "L1",
                "effectiveIdentityDigest": current_identity,
            }
        },
        drain=None,
    )
    fast_start = pending["fastStart"]
    assert fast_start["requestedLevel"] == "L3"
    assert (fast_start["qualifiedLevel"], fast_start["assignedLevel"]) == ("L2", "L2")
    assert (fast_start["requestedTargetSeconds"], fast_start["targetSeconds"]) == (60, 120)
    assert fast_start["qualification"]["state"] == "Fallback"
    # Not converged: the previously effective level is carried, the new one is not claimed.
    assert fast_start["effectiveLevel"] == "L1"
    assert fast_start["effectiveIdentityDigest"] == current_identity
    assert fast_start["hot"] is False
    assert fast_start["modelStart"]["p95Seconds"] == 110 and fast_start["modelStart"]["sampleCount"] == 20
    assert "capacityWait" not in fast_start and "endToEnd" not in fast_start
    assert {pool["poolRef"]: pool["qualifiedLevel"] for pool in fast_start["pools"]} == {"pool-a": "L3", "pool-b": "L2"}
    assert {pool["selectedMechanism"] for pool in fast_start["pools"]} == {"shared-cache"}
    assert all(pool["selectedCompatibilityTupleDigest"].startswith("sha256:") for pool in fast_start["pools"])
    condition = next(item for item in pending["conditions"] if item["type"] == "FastStartQualified")
    assert (condition["status"], condition["reason"]) == ("False", "RequestedLevelUnqualified")

    render = renderer().render(spec, context)
    converged = build_status(
        spec=spec,
        owner_uid="cr-uid-1",
        generation=1,
        plan=plan,
        discovery=Discovery(resources=[snapshot(item, ready=True) for item in render.resources], complete=True),
        previous_status=pending,
        drain=None,
    )
    assert converged["fastStart"]["effectiveLevel"] == "L2"
    assert converged["fastStart"]["effectiveIdentityDigest"] == current_identity
    assert converged["fastStart"]["hot"] is True
    assert converged["specDigest"] == plan.spec_digest

    # The durable records contract accepts the exact controller projection.
    observed = ModelDeploymentObservedStatus.model_validate(_normalize_keys(converged))
    assert observed.fast_start is not None
    assert observed.fast_start.effective_level is FastStartLevel.L2
    assert observed.fast_start.model_start is not None and observed.fast_start.model_start.p95_seconds == 110
    assert {item.type.value for item in observed.conditions} >= {"Ready", "Cached", "FastStartQualified"}


def test_automatic_status_uses_durable_demand_history_and_persists_hysteresis() -> None:
    base = model_spec()
    pools = envelope().pools
    installed = with_evidence(
        envelope(),
        evidence(base, pools["pool-a"], seconds=[50.0] * 20),
        evidence(base, pools["pool-b"], seconds=[100.0] * 20),
    )
    spec = with_fast_start(base, mode="Automatic", minimum_level="Off", maximum_level="L2")
    context = RenderContext(
        name="qwen-live",
        namespace="fs2-models",
        uid="cr-uid-1",
        generation=1,
        pool=pools["pool-b"],
        eligible_pools=[pools[pool_ref] for pool_ref in spec.placement.pool_refs],
        prometheus_server_address="http://prometheus:9090",
    )
    plan = plan_reconciliation(
        generation=1,
        deleting=False,
        spec=spec,
        envelope=installed,
        renderer=renderer(),
        render_context=context,
        observed=[],
        discovery_complete=True,
    )
    render = renderer().render(spec, context)
    discovery = Discovery(
        resources=[snapshot(item, ready=True) for item in render.resources],
        complete=True,
    )
    now = datetime(2026, 9, 2, 16, tzinfo=UTC)
    history = (
        FastStartHistoryWindow(
            started_at=now - timedelta(hours=1),
            ended_at=now,
            request_count=12,
            cold_activation_count=2,
            idle_gap_episode_count=2,
            target_miss_count=0,
        ),
        FastStartHistoryWindow(
            started_at=now - timedelta(days=7),
            ended_at=now,
            request_count=70,
            cold_activation_count=10,
            idle_gap_episode_count=10,
            target_miss_count=0,
        ),
    )

    missing_history = build_status(
        spec=spec,
        owner_uid="cr-uid-1",
        generation=1,
        plan=plan,
        discovery=discovery,
        previous_status={},
        drain=None,
        now=now,
        envelope=installed,
        fast_start_history=None,
    )
    assert missing_history["fastStart"]["assignedLevel"] == "Off"
    assert missing_history["fastStart"]["automatic"]["reason"] == "MissingDataMinimum"
    assert missing_history["fastStart"]["automatic"]["historyComplete"] is False

    status: dict[str, Any] = {}
    for offset in (0, 5, 10):
        status = build_status(
            spec=spec,
            owner_uid="cr-uid-1",
            generation=1,
            plan=plan,
            discovery=discovery,
            previous_status=status,
            drain=None,
            now=now + timedelta(minutes=offset),
            envelope=installed,
            fast_start_history=history,
        )

    fast_start = status["fastStart"]
    assert fast_start["assignedLevel"] == "L1"
    assert fast_start["qualification"]["state"] == "Qualified"
    assert fast_start["automatic"]["reason"] == "Promoted"
    assert fast_start["automatic"]["historyComplete"] is True
    assert fast_start["automatic"]["shortWindowRequests"] == 12
    assert fast_start["automatic"]["longWindowColdActivations"] == 10
    assert fast_start["automatic"]["shortWindowIdleGapEpisodes"] == 2
    assert fast_start["automatic"]["longWindowIdleGapEpisodes"] == 10


def test_automatic_status_selects_the_cheapest_common_qualified_mechanism() -> None:
    base = model_spec()
    pools = envelope().pools
    installed = with_evidence(
        envelope(),
        *[
            evidence(
                base,
                pool,
                seconds=[seconds] * 20,
                mechanism=mechanism,
                receipt_digest=canonical_digest({"pool": pool.pool_id, "mechanism": mechanism}),
            )
            for pool in pools.values()
            for mechanism, seconds in (("slow-cache", 100.0), ("fast-snapshot", 20.0))
        ],
    ).model_copy(
        update={
            "fast_start_mechanism_hourly_costs": {
                "slow-cache": 0.0,
                "fast-snapshot": 10.0,
            }
        }
    )
    spec = with_fast_start(base, mode="Automatic", minimum_level="Off", maximum_level="L4")
    context = RenderContext(
        name="qwen-live",
        namespace="fs2-models",
        uid="cr-uid-1",
        generation=1,
        pool=pools["pool-b"],
        eligible_pools=[pools[pool_ref] for pool_ref in spec.placement.pool_refs],
        prometheus_server_address="http://prometheus:9090",
    )
    plan = plan_reconciliation(
        generation=1,
        deleting=False,
        spec=spec,
        envelope=installed,
        renderer=renderer(),
        render_context=context,
        observed=[],
        discovery_complete=True,
    )
    render = renderer().render(spec, context)
    discovery = Discovery(resources=[snapshot(item, ready=True) for item in render.resources], complete=True)
    now = datetime(2026, 9, 2, 16, tzinfo=UTC)
    history = (
        FastStartHistoryWindow(
            started_at=now - timedelta(hours=1),
            ended_at=now,
            request_count=10,
            cold_activation_count=0,
            idle_gap_episode_count=1,
            target_miss_count=0,
        ),
        FastStartHistoryWindow(
            started_at=now - timedelta(days=7),
            ended_at=now,
            request_count=100,
            cold_activation_count=0,
            idle_gap_episode_count=10,
            target_miss_count=0,
        ),
    )

    status: dict[str, Any] = {}
    for offset in (0, 5, 10):
        status = build_status(
            spec=spec,
            owner_uid="cr-uid-1",
            generation=1,
            plan=plan,
            discovery=discovery,
            previous_status=status,
            drain=None,
            now=now + timedelta(minutes=offset),
            envelope=installed,
            fast_start_history=history,
        )

    assert status["fastStart"]["assignedLevel"] == "L1"
    assert status["fastStart"]["automatic"]["mechanismId"] == "slow-cache"
    assert status["fastStart"]["modelStart"]["p95Seconds"] == 100.0


def test_renderer_propagates_controller_identity_to_runtime_pod_template() -> None:
    plan = renderer().render(
        model_spec(),
        RenderContext(
            name="qwen-live",
            namespace="fs2-models",
            uid="cr-uid-1",
            generation=1,
            pool=envelope().pools["pool-b"],
            eligible_pools=[envelope().pools[pool_ref] for pool_ref in model_spec().placement.pool_refs],
            prometheus_server_address="http://prometheus:9090",
        ),
    )
    deployment = next(item.manifest for item in plan.resources if item.kind == "Deployment")
    assert deployment["spec"]["template"]["metadata"]["labels"]["fs2-serve.nebius.ai/model-deployment"] == "qwen-live"
    assert (
        deployment["spec"]["template"]["metadata"]["annotations"]["fs2-serve.nebius.ai/spec-digest"] == plan.spec_digest
    )


@pytest.mark.asyncio
async def test_controller_waits_for_generated_hpa_before_relinquishing_zero_bootstrap() -> None:
    api = FakeApi(model_object())
    api.auto_create_hpa = False
    subject = controller(api)
    key = ModelKey(namespace="fs2-models", name="qwen-live")

    await subject.reconcile(key, fence())
    bootstrap = await subject.reconcile(key, fence())
    assert bootstrap.action == "autoscaler-bootstrap"
    waiting = await subject.reconcile(key, fence())
    assert waiting.action == "autoscaler-install-pending"
    deployment = next(item for item in api.resources.values() if item.observed.kind == "Deployment")
    assert deployment.desired_replicas == 0
    assert deployment.replica_field_managers == [FIELD_MANAGER]
    assert api.status_writes[-1]["phase"] != "Ready"

    scaler = next(item for item in api.resources.values() if item.observed.kind == "ScaledObject")
    generated = hpa_snapshot(scaler)
    api.resources[generated.observed.identity] = generated
    handoff = await subject.reconcile(key, fence())
    assert handoff.action == "autoscaler-handoff"
    deployment = next(item for item in api.resources.values() if item.observed.kind == "Deployment")
    assert FIELD_MANAGER not in deployment.replica_field_managers


@pytest.mark.asyncio
async def test_controller_accepts_keda_idle_scale_to_zero_handoff() -> None:
    api = FakeApi(model_object())
    api.auto_create_hpa = False
    subject = controller(api)
    key = ModelKey(namespace="fs2-models", name="qwen-live")

    await subject.reconcile(key, fence())
    assert (await subject.reconcile(key, fence())).action == "autoscaler-bootstrap"
    scaler = next(item for item in api.resources.values() if item.observed.kind == "ScaledObject")
    scaler.raw["status"] = {
        "hpaName": f"keda-hpa-{scaler.observed.name}",
        "conditions": [
            {"type": "Ready", "status": "True", "reason": "ScaledObjectReady"},
            {"type": "HPAActive", "status": "True", "reason": "ScalingDisabled"},
        ],
    }
    idle_hpa = idle_zero_hpa_snapshot(scaler)
    api.resources[idle_hpa.observed.identity] = idle_hpa

    handoff = await subject.reconcile(key, fence())

    assert handoff.action == "autoscaler-handoff"
    deployment = next(item for item in api.resources.values() if item.observed.kind == "Deployment")
    assert FIELD_MANAGER not in deployment.replica_field_managers
    deployment.raw["spec"]["replicas"] = 0
    deployment.raw["status"] = {
        "observedGeneration": deployment.generation,
        "replicas": 0,
        "updatedReplicas": 0,
        "readyReplicas": 0,
        "availableReplicas": 0,
        "unavailableReplicas": 0,
    }
    api.resources[deployment.observed.identity] = deployment.model_copy(
        update={
            "desired_replicas": 0,
            "replicas": 0,
            "updated_replicas": 0,
            "ready_replicas": 0,
            "available_replicas": 0,
            "unavailable_replicas": 0,
        }
    )

    await subject.reconcile(key, fence())

    assert api.status_writes[-1]["phase"] == "Cold"


@pytest.mark.asyncio
async def test_controller_accepts_active_keda_hpa_with_omitted_zero_generation() -> None:
    api = FakeApi(model_object())
    api.auto_create_hpa = False
    subject = controller(api)
    key = ModelKey(namespace="fs2-models", name="qwen-live")

    await subject.reconcile(key, fence())
    assert (await subject.reconcile(key, fence())).action == "autoscaler-bootstrap"
    scaler = next(item for item in api.resources.values() if item.observed.kind == "ScaledObject")
    generated = hpa_snapshot(scaler)
    generated.raw["metadata"].pop("generation")
    generated.raw["status"].pop("observedGeneration")
    api.resources[generated.observed.identity] = generated.model_copy(
        update={"generation": 0, "observed_generation": None}
    )

    handoff = await subject.reconcile(key, fence())

    assert handoff.action == "autoscaler-handoff"


@pytest.mark.asyncio
async def test_controller_does_not_publish_cold_when_keda_metrics_are_unhealthy() -> None:
    api = FakeApi(model_object())
    subject = controller(api)
    key = ModelKey(namespace="fs2-models", name="qwen-live")
    await subject.reconcile(key, fence())
    await subject.reconcile(key, fence())
    scaler = next(item for item in api.resources.values() if item.observed.kind == "ScaledObject")
    scaler.raw["status"]["conditions"] = [{"type": "Ready", "status": "False"}]

    waiting = await subject.reconcile(key, fence())
    assert waiting.action == "autoscaler-install-pending"
    assert api.status_writes[-1]["phase"] not in {"Cold", "Ready"}
    deployment = next(item for item in api.resources.values() if item.observed.kind == "Deployment")
    assert FIELD_MANAGER in deployment.replica_field_managers


def test_controller_readiness_tracks_persistent_reconcile_errors_separately() -> None:
    health = ControllerHealth(reconcile_error_threshold=3)
    key = ModelKey(namespace="fs2-models", name="broken-model")
    health.cycle_succeeded()
    assert health.ready
    health.reconcile_failed(key, "ControllerError")
    health.reconcile_failed(key, "ControllerError")
    assert health.ready
    health.reconcile_failed(key, "ControllerError")
    assert not health.ready
    assert health.last_error == "ControllerError"
    health.cycle_succeeded()
    assert not health.ready
    health.reconcile_succeeded(key)
    assert health.ready


@pytest.mark.asyncio
async def test_ready_requires_exact_deployment_rollout_completion() -> None:
    api = FakeApi(model_object())
    subject = controller(api)
    key = ModelKey(namespace="fs2-models", name="qwen-live")
    await subject.reconcile(key, fence())
    await subject.reconcile(key, fence())
    await subject.reconcile(key, fence())
    assert api.status_writes[-1]["phase"] == "Ready"

    deployment = next(item for item in api.resources.values() if item.observed.kind == "Deployment")
    deployment.observed_generation = 0
    deployment.updated_replicas = 0
    deployment.unavailable_replicas = 1
    await subject.reconcile(key, fence())
    assert api.status_writes[-1]["phase"] != "Ready"
    ready = next(item for item in api.status_writes[-1]["conditions"] if item["type"] == "Ready")
    assert ready["status"] == "False"


@pytest.mark.asyncio
async def test_stale_scaler_is_deleted_before_zero_scale_apply() -> None:
    api = FakeApi(model_object())
    key = ModelKey(namespace="fs2-models", name="qwen-live")
    subject = controller(api)
    await subject.reconcile(key, fence())
    await subject.reconcile(key, fence())
    await subject.reconcile(key, fence())
    api.calls.clear()

    draining = model_spec().model_copy(
        update={
            "lifecycle": LifecycleSpec(desired_state=DesiredState.DRAINING),
            "availability": model_spec().availability.model_copy(update={"min_replicas": 0}),
        }
    )
    api.model["spec"] = draining.model_dump(mode="json", by_alias=True)
    api.model["metadata"]["generation"] = 2
    result = await subject.reconcile(key, fence())
    assert result.action == "drain:delete-first"
    assert [call[0] for call in api.calls if call[0] in {"delete", "apply"}]
    assert "apply" not in [call[0] for call in api.calls]

    api.calls.clear()
    await subject.reconcile(key, fence())
    deployment = next(item for item in api.resources.values() if item.observed.kind == "Deployment")
    assert deployment.raw["spec"]["replicas"] == 0


@pytest.mark.asyncio
async def test_drain_waits_for_delayed_hpa_garbage_collection_before_zero_scale() -> None:
    api = FakeApi(model_object())
    subject = controller(api)
    key = ModelKey(namespace="fs2-models", name="qwen-live")
    await subject.reconcile(key, fence())
    await subject.reconcile(key, fence())
    await subject.reconcile(key, fence())
    deployment = next(item for item in api.resources.values() if item.observed.kind == "Deployment")
    assert deployment.desired_replicas == 1

    draining = model_spec().model_copy(
        update={
            "lifecycle": LifecycleSpec(desired_state=DesiredState.DRAINING),
            "availability": model_spec().availability.model_copy(update={"min_replicas": 0}),
        }
    )
    api.model["spec"] = draining.model_dump(mode="json", by_alias=True)
    api.model["metadata"]["generation"] = 2
    api.hold_hpa_gc = True
    await subject.reconcile(key, fence())

    api.calls.clear()
    pending = await subject.reconcile(key, fence())
    assert pending.action == "drain:autoscaler-removal-pending" and pending.requeue
    assert not any(action == "apply" for action, _ in api.calls)
    deployment = next(item for item in api.resources.values() if item.observed.kind == "Deployment")
    assert deployment.desired_replicas == 1

    hpa_identity = next(
        identity for identity, item in api.resources.items() if item.observed.kind == "HorizontalPodAutoscaler"
    )
    del api.resources[hpa_identity]
    completed = await subject.reconcile(key, fence())
    assert completed.action == "drain"
    deployment = next(item for item in api.resources.values() if item.observed.kind == "Deployment")
    assert deployment.desired_replicas == 0


@pytest.mark.asyncio
async def test_drain_with_active_work_withdraws_publication_but_preserves_scaler_and_runtime() -> None:
    api = FakeApi(model_object())
    key = ModelKey(namespace="fs2-models", name="qwen-live")
    active = MutableActiveOperations(1)
    subject = controller(api, active_operations=active)
    await subject.reconcile(key, fence())
    await subject.reconcile(key, fence())
    await subject.reconcile(key, fence())

    draining = model_spec().model_copy(
        update={
            "lifecycle": LifecycleSpec(desired_state=DesiredState.DRAINING),
            "availability": model_spec().availability.model_copy(update={"min_replicas": 0}),
        }
    )
    api.model["spec"] = draining.model_dump(mode="json", by_alias=True)
    api.model["metadata"]["generation"] = 2
    api.calls.clear()

    first = await subject.reconcile(key, fence())
    assert first.action == "drain:delete-first"
    deleted = [value for action, value in api.calls if action == "delete"]
    assert deleted and all("publication" in identity for identity in deleted)
    assert any(item.observed.kind == "ScaledObject" for item in api.resources.values())
    deployment = next(item for item in api.resources.values() if item.observed.kind == "Deployment")
    assert FIELD_MANAGER not in deployment.replica_field_managers

    api.calls.clear()
    await subject.reconcile(key, fence())
    assert not any(action == "delete" and "ScaledObject" in value for action, value in api.calls)
    deployment = next(item for item in api.resources.values() if item.observed.kind == "Deployment")
    assert FIELD_MANAGER not in deployment.replica_field_managers

    active.value = 0
    api.calls.clear()
    stopping = await subject.reconcile(key, fence())
    assert stopping.action == "drain:delete-first"
    assert any(action == "delete" and "ScaledObject" in value for action, value in api.calls)
    assert not any(action == "apply" for action, _ in api.calls)

    await subject.reconcile(key, fence())
    deployment = next(item for item in api.resources.values() if item.observed.kind == "Deployment")
    assert deployment.raw["spec"]["replicas"] == 0


@pytest.mark.asyncio
async def test_unknown_active_work_does_not_recreate_a_proven_cold_runtime() -> None:
    api = FakeApi(model_object())
    key = ModelKey(namespace="fs2-models", name="qwen-live")
    subject = controller(api, active_operations=MutableActiveOperations(None))
    await subject.reconcile(key, fence())
    await subject.reconcile(key, fence())
    await subject.reconcile(key, fence())
    deployment = next(item for item in api.resources.values() if item.observed.kind == "Deployment")
    deployment.replicas = 0
    deployment.ready_replicas = 0
    deployment.available_replicas = 0

    draining = model_spec().model_copy(
        update={
            "lifecycle": LifecycleSpec(desired_state=DesiredState.DRAINING),
            "availability": model_spec().availability.model_copy(update={"min_replicas": 0}),
        }
    )
    api.model["spec"] = draining.model_dump(mode="json", by_alias=True)
    api.model["metadata"]["generation"] = 2
    await subject.reconcile(key, fence())
    await subject.reconcile(key, fence())
    await subject.reconcile(key, fence())
    deployment = next(item for item in api.resources.values() if item.observed.kind == "Deployment")
    assert deployment.raw["spec"]["replicas"] == 0


@pytest.mark.asyncio
async def test_delete_removes_only_owned_inventory_then_finalizer_after_empty_rediscovery() -> None:
    api = FakeApi(model_object())
    key = ModelKey(namespace="fs2-models", name="qwen-live")
    subject = controller(api)
    await subject.reconcile(key, fence())
    await subject.reconcile(key, fence())
    await subject.reconcile(key, fence())

    draining = model_spec().model_copy(
        update={
            "lifecycle": LifecycleSpec(desired_state=DesiredState.DRAINING),
            "availability": model_spec().availability.model_copy(update={"min_replicas": 0}),
        }
    )
    api.model["spec"] = draining.model_dump(mode="json", by_alias=True)
    api.model["metadata"]["generation"] = 2
    await subject.reconcile(key, fence())
    await subject.reconcile(key, fence())
    api.model["metadata"]["deletionTimestamp"] = "2026-09-02T08:00:00Z"

    deleting = await subject.reconcile(key, fence())
    assert deleting.action == "delete:delete-first" and FINALIZER in api.model["metadata"]["finalizers"]
    assert not api.resources
    complete = await subject.reconcile(key, fence())
    assert complete.action == "finalizer-removed"
    assert FINALIZER not in api.model["metadata"]["finalizers"]


@pytest.mark.asyncio
async def test_stale_fence_stops_mutation_before_finalizer() -> None:
    api = FakeApi(model_object())
    subject = controller(api)
    with pytest.raises(FenceLostError):
        await subject.reconcile(
            ModelKey(namespace="fs2-models", name="qwen-live"),
            fence("b" * 32),
        )
    assert FINALIZER not in api.model["metadata"]["finalizers"]


def test_drain_status_fails_closed_when_active_operations_are_unknown() -> None:
    spec = model_spec().model_copy(
        update={
            "lifecycle": LifecycleSpec(desired_state=DesiredState.DRAINING),
            "availability": model_spec().availability.model_copy(update={"min_replicas": 0}),
        }
    )
    context = RenderContext(
        name="qwen-live",
        namespace="fs2-models",
        uid="cr-uid-1",
        generation=1,
        pool=envelope().pools["pool-b"],
        eligible_pools=[envelope().pools[pool_ref] for pool_ref in model_spec().placement.pool_refs],
        prometheus_server_address="http://prometheus:9090",
    )
    plan = plan_reconciliation(
        generation=1,
        deleting=True,
        spec=spec,
        envelope=envelope(),
        renderer=renderer(),
        render_context=context,
        observed=[],
        discovery_complete=True,
        drain_observation=DrainObservation(
            publication_withdrawn=True,
            active_operations=None,
            observed_replicas=0,
            ready_replicas=0,
        ),
    )
    status = build_status(
        spec=spec,
        owner_uid="cr-uid-1",
        generation=1,
        plan=plan,
        discovery=Discovery(resources=[], complete=True),
        previous_status={},
        drain=DrainObservation(
            publication_withdrawn=True,
            active_operations=None,
            observed_replicas=0,
            ready_replicas=0,
        ),
    )
    assert status["phase"] == "Draining"


@pytest.mark.asyncio
async def test_http_writer_uses_non_forcing_ssa_resource_version_and_read_after_write(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("projected-service-account-token")
    desired = next(
        item
        for item in renderer()
        .render(
            model_spec(),
            RenderContext(
                name="qwen-live",
                namespace="fs2-models",
                uid="cr-uid-1",
                generation=1,
                pool=envelope().pools["pool-b"],
                eligible_pools=[envelope().pools[pool_ref] for pool_ref in model_spec().placement.pool_refs],
                prometheus_server_address="http://prometheus:9090",
            ),
        )
        .resources
        if item.kind == "Service"
    )
    actual = copy.deepcopy(desired.manifest)
    actual["metadata"].update(
        {
            "uid": "service-uid",
            "resourceVersion": "11",
            "managedFields": [{"manager": FIELD_MANAGER}],
        }
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/leases/fs2-model-controller"):
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "name": "fs2-model-controller",
                        "namespace": "fs2-system",
                        "uid": "lease-uid",
                        "resourceVersion": "7",
                        "annotations": {"inference.fs2.nebius.ai/fence-token": "a" * 32},
                    },
                    "spec": {
                        "holderIdentity": "fs2-system/controller:pod-uid",
                        "leaseDurationSeconds": 15,
                        "renewTime": datetime.now(UTC).isoformat(),
                    },
                },
            )
        if request.method == "PATCH":
            body = json.loads(request.content)
            assert body["metadata"]["resourceVersion"] == "11"
            assert request.url.params["fieldManager"] == FIELD_MANAGER
            assert request.url.params["force"] == "false"
            return httpx.Response(200, json=actual)
        return httpx.Response(200, json=actual)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.invalid")
    client = HttpKubernetesModelClient(
        base_url="https://kubernetes.invalid",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        writes_enabled=True,
        client=http,
    )
    result = await client.apply_resource(desired, owner_uid="cr-uid-1", fence=fence())
    assert result.observed.uid == "service-uid"
    assert [request.method for request in requests] == ["GET", "GET", "PATCH", "GET"]
    await http.aclose()


@pytest.mark.asyncio
async def test_http_discovery_exactly_gets_the_generated_hpa_descendant(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("projected-service-account-token")
    render = renderer().render(
        model_spec(),
        RenderContext(
            name="qwen-live",
            namespace="fs2-models",
            uid="cr-uid-1",
            generation=1,
            pool=envelope().pools["pool-b"],
            eligible_pools=[envelope().pools[pool_ref] for pool_ref in model_spec().placement.pool_refs],
            prometheus_server_address="http://prometheus:9090",
        ),
    )
    scaler = next(item for item in render.resources if item.kind == "ScaledObject")
    hpa_name = f"keda-hpa-{scaler.name}"
    hpa = {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {
            "name": hpa_name,
            "namespace": "fs2-models",
            "uid": "hpa-uid",
            "resourceVersion": "5",
            "generation": 2,
            "ownerReferences": [{"uid": "scaled-object-uid", "controller": True}],
        },
        "spec": {"scaleTargetRef": copy.deepcopy(scaler.manifest["spec"]["scaleTargetRef"])},
        "status": {"observedGeneration": 2},
    }
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path.endswith(f"/horizontalpodautoscalers/{hpa_name}"):
            return httpx.Response(200, json=hpa)
        if "labelSelector" in request.url.params:
            return httpx.Response(200, json={"items": []})
        return httpx.Response(404, json={"kind": "Status", "reason": "NotFound"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.invalid")
    client = HttpKubernetesModelClient(
        base_url="https://kubernetes.invalid",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        writes_enabled=True,
        client=http,
    )
    discovery = await client.discover(
        key=ModelKey(namespace="fs2-models", name="qwen-live"),
        owner_uid="cr-uid-1",
        render=render,
    )
    assert len(discovery.resources) == 1
    assert discovery.resources[0].observed.kind == "HorizontalPodAutoscaler"
    assert discovery.resources[0].observed_generation == 2
    assert any(path.endswith(f"/horizontalpodautoscalers/{hpa_name}") for path in requested_paths)
    await http.aclose()


@pytest.mark.asyncio
async def test_http_discovery_restores_typemeta_omitted_from_built_in_list_items(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("projected-service-account-token")
    render = renderer().render(
        model_spec(),
        RenderContext(
            name="qwen-live",
            namespace="fs2-models",
            uid="cr-uid-1",
            generation=1,
            pool=envelope().pools["pool-b"],
            eligible_pools=[envelope().pools[pool_ref] for pool_ref in model_spec().placement.pool_refs],
            prometheus_server_address="http://prometheus:9090",
        ),
    )
    service = next(item for item in render.resources if item.kind == "Service")
    listed_service = copy.deepcopy(service.manifest)
    listed_service.pop("apiVersion")
    listed_service.pop("kind")
    listed_service["metadata"].update({"uid": "service-uid", "resourceVersion": "11"})
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path.endswith("/services"):
            return httpx.Response(200, json={"items": [listed_service]})
        if "labelSelector" in request.url.params:
            return httpx.Response(200, json={"items": []})
        return httpx.Response(404, json={"kind": "Status", "reason": "NotFound"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.invalid")
    client = HttpKubernetesModelClient(
        base_url="https://kubernetes.invalid",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        writes_enabled=True,
        client=http,
    )
    discovery = await client.discover(
        key=ModelKey(namespace="fs2-models", name="qwen-live"),
        owner_uid="cr-uid-1",
        render=render,
    )
    assert len(discovery.resources) == 1
    assert discovery.resources[0].observed.api_version == "v1"
    assert discovery.resources[0].observed.kind == "Service"
    assert not any(path.endswith(f"/services/{service.name}") for path in requested_paths)
    await http.aclose()


@pytest.mark.asyncio
async def test_http_delete_carries_exact_uid_and_resource_version_preconditions(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("projected-service-account-token")
    current = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "old",
            "namespace": "fs2-models",
            "uid": "old-uid",
            "resourceVersion": "19",
            "ownerReferences": [{"uid": "cr-uid-1", "controller": True}],
        },
    }
    deleted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/leases/fs2-model-controller"):
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "resourceVersion": "7",
                        "annotations": {"inference.fs2.nebius.ai/fence-token": "a" * 32},
                    },
                    "spec": {
                        "holderIdentity": "fs2-system/controller:pod-uid",
                        "leaseDurationSeconds": 15,
                        "renewTime": datetime.now(UTC).isoformat(),
                    },
                },
            )
        if request.method == "DELETE":
            deleted.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "Success"})
        return httpx.Response(200, json=current)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.invalid")
    client = HttpKubernetesModelClient(
        base_url="https://kubernetes.invalid",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        writes_enabled=True,
        client=http,
    )
    assert await client.delete_resource(
        "v1/ConfigMap/fs2-models/old",
        owner_uid="cr-uid-1",
        fence=fence(),
    )
    assert deleted == [
        {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "propagationPolicy": "Background",
            "preconditions": {"uid": "old-uid", "resourceVersion": "19"},
        }
    ]
    current.update({"apiVersion": "keda.sh/v1alpha1", "kind": "ScaledObject"})
    current["metadata"].update({"name": "fs2-model-qwen-live", "uid": "scaled-object-uid", "resourceVersion": "20"})
    deleted.clear()
    assert await client.delete_resource(
        "keda.sh/v1alpha1/ScaledObject/fs2-models/fs2-model-qwen-live",
        owner_uid="cr-uid-1",
        fence=fence(),
    )
    assert deleted[0]["propagationPolicy"] == "Foreground"
    assert deleted[0]["preconditions"] == {"uid": "scaled-object-uid", "resourceVersion": "20"}
    await http.aclose()


def _mechanism_envelope() -> tuple[ModelDeploymentSpec, InfrastructureEnvelope]:
    """A model that declares and pins regional-cache, with no evidence at all."""

    base = envelope()
    pools = {
        key: value.model_copy(
            update={
                "node_selector": {
                    **value.node_selector,
                    "local-nvme.fs2.nebius/eligible": "false",
                    "snapshot.fs2.nebius/eligible": "false",
                }
            }
        )
        for key, value in base.pools.items()
    }
    declaration = render_regional_cache()
    qualification = base.qualifications["qwen.3-8b"].model_copy(update={"regional_cache": declaration})
    installed = base.model_copy(update={"pools": pools, "qualifications": {"qwen.3-8b": qualification}})
    wire = model_spec().model_dump(mode="json", by_alias=True)
    wire["cache"] = {**wire["cache"], "mechanism": "regional-cache"}
    return ModelDeploymentSpec.model_validate(wire), installed


def _all_mechanism_envelope(mechanism: str) -> tuple[ModelDeploymentSpec, InfrastructureEnvelope]:
    base = envelope()
    qualification = base.qualifications["qwen.3-8b"].model_copy(
        update={
            "regional_cache": render_regional_cache(),
            "host_memory_residency": render_host_memory(),
            "gpu_resident": render_gpu_resident(),
            "template_cache_tiers": {
                digest_value: CacheTier.SHARED_FILESYSTEM
                for digest_value in base.qualifications["qwen.3-8b"].template_digests
            },
        }
    )
    installed = base.model_copy(
        update={
            "qualifications": {qualification.model_ref: qualification},
            "residency_holder_image": model_spec().runtime.image,
        }
    )
    wire = model_spec().model_dump(mode="json", by_alias=True)
    wire["cache"] = {**wire["cache"], "mechanism": mechanism}
    wire["availability"] = {**wire["availability"], "maxReplicas": 20}
    if mechanism in {"regional-cache", "host-memory-residency"}:
        wire["cache"]["tier"] = "SharedFilesystem"
    return ModelDeploymentSpec.model_validate(wire), installed


@pytest.mark.asyncio
async def test_controller_passes_all_reviewed_mechanisms_and_holder_image_to_real_render() -> None:
    spec, installed = _all_mechanism_envelope("host-memory-residency")
    raw = model_object()
    raw["spec"] = spec.model_dump(mode="json", by_alias=True)
    api = FakeApi(raw)
    captured: list[RenderContext] = []
    delegate = renderer()

    class CapturingRenderer:
        def render(self, desired: ModelDeploymentSpec, context: RenderContext) -> RenderPlan:
            captured.append(context)
            return delegate.render(desired, context)

    subject = ModelDeploymentController(
        api=api,
        envelope=installed,
        renderer=CapturingRenderer(),
        namespace="fs2-models",
        holder_identity="fs2-system/controller:pod-uid",
        prometheus_server_address="http://prometheus:9090",
        writes_enabled=True,
        active_operations=ZeroActiveOperations(),
        queue_capacity=2,
        worker_count=1,
        poll_seconds=0.01,
    )
    key = ModelKey(namespace="fs2-models", name="qwen-live")

    assert (await subject.reconcile(key, fence())).action == "finalizer-added"
    assert (await subject.reconcile(key, fence())).requeue
    context = captured[-1]
    qualification = installed.qualifications[spec.model_ref]
    assert context.regional_cache == qualification.regional_cache
    assert context.host_memory_residency == qualification.host_memory_residency
    assert context.gpu_resident == qualification.gpu_resident
    assert context.residency_holder_image == installed.residency_holder_image
    holders = [item for item in api.resources.values() if item.observed.kind == "DaemonSet"]
    assert len(holders) == len(spec.placement.pool_refs)
    assert all(
        item.raw["spec"]["selector"]["matchLabels"]["fast-start.fs2.nebius/host-memory-holder"] for item in holders
    )
    statuses = api.status_writes[-1]["fastStart"]["cacheMechanisms"]
    host = statuses["host-memory-residency"]
    assert qualification.host_memory_residency is not None
    reserved_per_node = qualification.host_memory_residency.reserved_bytes
    assert host["selected"] is True
    assert host["reservedHostMemoryBytes"] == reserved_per_node
    assert host.get("reservedHostMemoryFraction") is None
    assert host["maximumReservedHostMemoryBytes"] == reserved_per_node * sum(
        installed.pools[pool_ref].max_nodes for pool_ref in spec.placement.pool_refs
    )
    assert statuses["regional-cache"]["state"] == "Available"
    assert statuses["gpu-resident"]["state"] == "Unavailable"


def test_a_configured_mechanism_is_reported_without_claiming_a_level() -> None:
    """The honest report: mechanism Configured, effective level still absent."""

    spec, installed = _mechanism_envelope()
    context = RenderContext(
        name="qwen-live",
        namespace="fs2-models",
        uid="cr-uid-1",
        generation=1,
        pool=installed.pools["pool-b"],
        eligible_pools=[installed.pools[pool_ref] for pool_ref in spec.placement.pool_refs],
        prometheus_server_address="http://prometheus:9090",
        regional_cache=installed.qualifications["qwen.3-8b"].regional_cache,
    )
    plan = plan_reconciliation(
        generation=1,
        deleting=False,
        spec=spec,
        envelope=installed,
        renderer=renderer(),
        render_context=context,
        observed=[],
        discovery_complete=True,
    )
    status = build_status(
        spec=spec,
        owner_uid="cr-uid-1",
        generation=1,
        plan=plan,
        discovery=Discovery(resources=[], complete=True),
        previous_status={},
        drain=None,
        envelope=installed,
    )
    fast_start = status["fastStart"]
    mechanisms = fast_start["cacheMechanisms"]

    # The selected mechanism is visible and attributed to its reviewed digest.
    conventional = mechanisms["conventional"]
    assert conventional["state"] == "Available"
    assert conventional["reason"] == "ConventionalLoaderAvailable"
    assert conventional["selected"] is False
    regional = mechanisms["regional-cache"]
    assert regional["selected"] is True
    assert regional["state"] in ("Configured", "Pending")
    assert regional["configDigest"] == installed.qualifications["qwen.3-8b"].regional_cache.config_digest
    assert regional["retainedCompileCacheAbi"] == "driver-580-sm90"

    # And it buys nothing: there is no evidence, so no level is claimed.
    assert fast_start["qualifiedLevel"] == "Off"
    assert fast_start.get("effectiveLevel") in (None, "Off")

    # The paths this cluster has no hardware for are reported, not hidden.
    assert mechanisms["node-local-restore"]["state"] == "Unavailable"
    assert mechanisms["node-local-restore"]["reason"] == "NoNodeLocalNvme"
    assert mechanisms["shared-restore"]["reason"] == "NoQualifiedSnapshotCapability"
    for pool in mechanisms["node-local-restore"]["pools"].values():
        assert pool["evidenceSelector"] == {"local-nvme.fs2.nebius/eligible": "false"}

    # GPU residency stays fail-closed until a production promotion controller
    # owns the readiness-gate condition.
    assert mechanisms["gpu-resident"]["state"] == "Unavailable"
    assert mechanisms["gpu-resident"]["reason"] == "PromotionControllerNotInstalled"
    assert mechanisms["gpu-resident"]["selected"] is False
    assert mechanisms["gpu-resident"].get("configDigest") is None

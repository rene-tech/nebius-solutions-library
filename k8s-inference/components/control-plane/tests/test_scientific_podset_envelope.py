"""Exactness tests for the canonical PodSet resource envelope.

Three things are pinned down here: the arithmetic that turns a rendered Pod
template into a per-replica and aggregate envelope, the exact comparison of
that envelope with Kueue's admitted ``resourceUsage`` for every PodSet and
resource, and the refusal of any rendered, frozen or admitted figure that does
not agree. Every positive assertion is paired with the mutation that must
break it, because an envelope that is only checked against itself would admit
a gang against the wrong quota in silence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from test_scientific_batch_production import (
    Fence,
    JobRenderer,
    _live_workload,
    _rendered_pod,
    _workload_spec,
    profile_catalog,
    profile_value,
    scheduling,
)

from fs2_serve.crypto import KeyedHasher
from fs2_serve.scientific_batch.capability import ScientificWorkloadCapabilityAuthority
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer
from fs2_serve.scientific_batch.kubernetes import (
    MANIFEST_ANNOTATION,
    PODSET_ENVELOPE_ANNOTATION,
    PODSET_ENVELOPE_DIGEST_ANNOTATION,
    HttpScientificBatchCluster,
    ScientificKubernetesError,
    _manifest_digest,
)
from fs2_serve.scientific_batch.models import (
    AdapterExecutionPlan,
    ArtifactAccessContext,
    ResourceClass,
    SchedulingAdmission,
    ScientificBatchPlan,
    ScientificStagePlan,
    ServiceClass,
    StageInvocation,
    WorkloadKind,
    WorkloadResource,
)
from fs2_serve.scientific_batch.podset_envelope import (
    DEFAULT_JOB_POD_SET_NAME,
    PodSetEnvelope,
    PodSetEnvelopeError,
    ResourceVector,
    WorkloadEnvelope,
    compare_kueue_usage,
    envelope_from_json,
    envelope_from_manifest,
    envelope_from_value,
    parse_bytes,
    parse_cpu_millis,
)
from fs2_serve.scientific_batch.protocols import BatchRepositoryConflictError

GIB = 1024**3
MIB = 1024**2

# The exact effective per-Pod request of the production stage Pod: the model
# container's frozen request plus the artifact collector's, with the workspace
# init container below that sum.
STAGE_POD_REQUESTS = ResourceVector.of(
    cpu_millis=4_100,
    memory_bytes=32 * GIB + 256 * MIB,
    ephemeral_storage_bytes=100 * GIB,
    accelerators={"nvidia.com/gpu": 1},
)
STAGE_POD_LIMITS = ResourceVector.of(
    cpu_millis=6_000,
    memory_bytes=32 * GIB + 2 * GIB,
    ephemeral_storage_bytes=100 * GIB,
    accelerators={"nvidia.com/gpu": 1},
)


def _pod(containers: list[dict[str, Any]], init: list[dict[str, Any]] | None = None, **spec: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"containers": containers}
    if init is not None:
        body["initContainers"] = init
    body.update(spec)
    return {"spec": {"template": {"spec": body}}}


def _requests(**quantities: str) -> dict[str, Any]:
    return {"resources": {"requests": dict(quantities), "limits": dict(quantities)}}


def test_effective_pod_request_sums_regular_containers_and_raises_to_init_maxima() -> None:
    """Kubernetes' own effective-request arithmetic, which Kueue also uses."""

    manifest = _pod(
        [
            {"name": "model", **_requests(cpu="4", memory="32Gi")},
            {"name": "collector", **_requests(cpu="100m", memory="256Mi")},
        ],
        [
            {"name": "small", **_requests(cpu="50m", memory="64Mi")},
            # One init container asks for more memory than both regular
            # containers together, so the Pod's effective memory is its own.
            {"name": "loader", **_requests(cpu="200m", memory="64Gi")},
        ],
    )
    pod_set = envelope_from_manifest(manifest, WorkloadKind.JOB).pod_sets[0]
    assert pod_set.per_replica_requests == ResourceVector.of(cpu_millis=4_100, memory_bytes=64 * GIB)
    # Init containers run one after another, so their requests never sum.
    assert pod_set.per_replica_requests.memory_bytes != 64 * GIB + 64 * MIB


def test_native_sidecar_is_additive_and_participates_in_the_init_maxima() -> None:
    """A restartPolicy: Always init container runs for the whole Pod lifetime."""

    manifest = _pod(
        [{"name": "model", **_requests(cpu="1", memory="4Gi")}],
        [
            {"name": "proxy", "restartPolicy": "Always", **_requests(cpu="500m", memory="1Gi")},
            {"name": "prepare", **_requests(cpu="100m", memory="8Gi")},
        ],
    )
    pod_set = envelope_from_manifest(manifest, WorkloadKind.JOB).pod_sets[0]
    # cpu: the sidecar is added to the regular sum (1000m + 500m). memory: the
    # plain init container's 8Gi plus the sidecar that stays running beside it.
    assert pod_set.per_replica_requests == ResourceVector.of(cpu_millis=1_500, memory_bytes=9 * GIB)


def test_pod_overhead_and_per_resource_request_defaulting_are_exact() -> None:
    manifest = _pod(
        [
            # Only limits are declared, so each request defaults to its limit.
            {"name": "model", "resources": {"limits": {"cpu": "2", "memory": "8Gi"}}},
            # A request for one resource does not suppress the other's default.
            {"name": "side", "resources": {"requests": {"cpu": "500m"}, "limits": {"cpu": "1", "memory": "2Gi"}}},
        ],
        overhead={"cpu": "250m", "memory": "512Mi"},
    )
    pod_set = envelope_from_manifest(manifest, WorkloadKind.JOB).pod_sets[0]
    assert pod_set.per_replica_requests == ResourceVector.of(
        cpu_millis=2_000 + 500 + 250,
        memory_bytes=8 * GIB + 2 * GIB + 512 * MIB,
    )
    assert pod_set.per_replica_limits == ResourceVector.of(cpu_millis=3_000 + 250, memory_bytes=10 * GIB + 512 * MIB)


def test_job_pod_set_is_named_main_and_counts_parallelism_bounded_by_completions() -> None:
    base = _pod([{"name": "model", **_requests(cpu="1", memory="1Gi")}])
    single = envelope_from_manifest(base, WorkloadKind.JOB).pod_sets[0]
    assert (single.name, single.count) == (DEFAULT_JOB_POD_SET_NAME, 1)

    parallel = json.loads(json.dumps(base))
    parallel["spec"]["parallelism"] = 6
    parallel["spec"]["completions"] = 4
    bounded = envelope_from_manifest(parallel, WorkloadKind.JOB).pod_sets[0]
    # Kueue reserves the Pods that can actually run concurrently.
    assert bounded.count == 4
    assert bounded.aggregate_requests.cpu_millis == 4_000


def test_jobset_pod_set_applies_replicas_times_parallelism_exactly_once() -> None:
    manifest = {
        "spec": {
            "replicatedJobs": [
                {
                    "name": "gang",
                    "replicas": 4,
                    "template": {
                        "spec": {
                            "parallelism": 2,
                            "template": {
                                "spec": {"containers": [{"name": "model", **_requests(cpu="8", memory="64Gi")}]}
                            },
                        }
                    },
                }
            ]
        }
    }
    pod_set = envelope_from_manifest(manifest, WorkloadKind.JOB_SET).pod_set("gang")
    assert pod_set.count == 8
    assert pod_set.per_replica_requests.cpu_millis == 8_000
    assert pod_set.aggregate_requests.cpu_millis == 64_000
    # The replica count is applied to the per-replica request and nowhere else.
    assert pod_set.aggregate_requests == pod_set.per_replica_requests.scaled(pod_set.count)


def test_frozen_document_round_trips_and_refuses_a_miscounted_aggregate() -> None:
    envelope = envelope_from_manifest({"spec": _workload_spec(WorkloadKind.JOB_SET, gang_size=4)}, WorkloadKind.JOB_SET)
    document = json.loads(envelope.to_json())
    assert envelope_from_value(document) == envelope
    assert envelope_from_json(envelope.to_json(), kind=WorkloadKind.JOB_SET) == envelope
    assert envelope.digest == hashlib.sha256(envelope.to_json().encode()).hexdigest()

    zeroed = json.loads(envelope.to_json())
    zeroed["pod_sets"][0]["aggregate"]["requests"]["accelerators"] = {}
    with pytest.raises(PodSetEnvelopeError, match="aggregate requests is not exactly 4 times"):
        envelope_from_value(zeroed)

    doubled = json.loads(envelope.to_json())
    doubled["pod_sets"][0]["aggregate"]["requests"]["cpu_millis"] *= 2
    with pytest.raises(PodSetEnvelopeError, match="aggregate requests is not exactly 4 times"):
        envelope_from_value(doubled)

    with pytest.raises(PodSetEnvelopeError, match="kind differs"):
        envelope_from_json(envelope.to_json(), kind=WorkloadKind.JOB)


def test_quantity_parsing_is_exact_in_canonical_units() -> None:
    assert parse_cpu_millis("0.5") == 500
    assert parse_cpu_millis("250m") == 250
    assert parse_cpu_millis(4) == 4_000
    assert parse_bytes("1Ki", label="memory") == 1024
    assert parse_bytes("1e3", label="memory") == 1000
    assert parse_bytes("2Gi", label="memory") == 2 * GIB
    for invalid in ("1.5", "100m", "sixteen", "", "1Gi7"):
        with pytest.raises(PodSetEnvelopeError):
            parse_bytes(invalid, label="memory")
    with pytest.raises(PodSetEnvelopeError):
        parse_cpu_millis("0.0005")


def _gang_envelope(*, replicas: int = 2, gpu: int = 4) -> WorkloadEnvelope:
    return WorkloadEnvelope(
        kind=WorkloadKind.JOB_SET,
        pod_sets=(
            PodSetEnvelope(
                name="coordinator",
                count=1,
                per_replica_requests=ResourceVector.of(cpu_millis=2_100, memory_bytes=8 * GIB),
                per_replica_limits=ResourceVector.of(cpu_millis=2_100, memory_bytes=8 * GIB),
            ),
            PodSetEnvelope(
                name="workers",
                count=replicas,
                per_replica_requests=ResourceVector.of(
                    cpu_millis=4_100, memory_bytes=32 * GIB, accelerators={"nvidia.com/gpu": gpu}
                ),
                per_replica_limits=ResourceVector.of(
                    cpu_millis=4_100, memory_bytes=32 * GIB, accelerators={"nvidia.com/gpu": gpu}
                ),
            ),
        ),
    )


def _assignment(name: str, usage: dict[str, str], count: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name, "resourceUsage": usage}
    if count is not None:
        body["count"] = count
    return body


def test_kueue_usage_matches_every_pod_set_and_resource_for_a_gang() -> None:
    envelope = _gang_envelope()
    result = compare_kueue_usage(
        envelope,
        [
            _assignment("coordinator", {"cpu": "2100m", "memory": "8Gi"}, count=1),
            _assignment("workers", {"cpu": "8200m", "memory": "64Gi", "nvidia.com/gpu": "8"}, count=2),
        ],
        accelerator_resource="nvidia.com/gpu",
    )
    assert result.accelerator_per_replica == 4
    assert result.accelerator_aggregate == 8
    assert result.cpu_millis_per_replica == 4_100
    assert result.memory_bytes_per_replica == 32 * GIB
    assert result.aggregate.cpu_millis == 2_100 + 8_200
    assert result.compared == (
        ("coordinator", ("cpu", "memory")),
        ("workers", ("cpu", "memory", "nvidia.com/gpu")),
    )


@pytest.mark.parametrize(
    ("assignments", "message"),
    [
        pytest.param(
            [_assignment("workers", {"cpu": "8200m", "memory": "64Gi", "nvidia.com/gpu": "8"})],
            "omits the frozen PodSet 'coordinator'",
            id="missing-frozen-pod-set",
        ),
        pytest.param(
            [
                _assignment("coordinator", {"cpu": "2100m", "memory": "8Gi"}),
                _assignment("workers", {"cpu": "8200m", "memory": "64Gi", "nvidia.com/gpu": "8"}),
                _assignment("strangers", {"cpu": "1"}),
            ],
            "has no PodSet 'strangers'",
            id="unknown-pod-set",
        ),
        pytest.param(
            [
                _assignment("coordinator", {"cpu": "2100m", "memory": "8Gi"}, count=1),
                _assignment("workers", {"cpu": "8200m", "memory": "64Gi", "nvidia.com/gpu": "8"}, count=1),
            ],
            "admitted 1 Pods for PodSet 'workers' instead of the frozen 2",
            id="partially-admitted-count",
        ),
        pytest.param(
            [
                _assignment("coordinator", {"cpu": "2100m", "memory": "8Gi"}),
                _assignment("workers", {"cpu": "4100m", "memory": "64Gi", "nvidia.com/gpu": "8"}),
            ],
            "admitted cpu=4100 for PodSet 'workers' instead of the frozen 8200",
            id="cpu-not-multiplied",
        ),
        pytest.param(
            [
                _assignment("coordinator", {"cpu": "2100m", "memory": "8Gi", "nvidia.com/gpu": "1"}),
                _assignment("workers", {"cpu": "8200m", "memory": "64Gi", "nvidia.com/gpu": "8"}),
            ],
            "charged PodSet 'coordinator' for nvidia.com/gpu that its Pods do not request",
            id="charged-for-unrequested-accelerator",
        ),
        pytest.param(
            [
                _assignment("coordinator", {"cpu": "2100m", "memory": "8Gi"}),
                _assignment("workers", {"cpu": "8200m", "memory": "64Gi"}),
            ],
            "without the frozen accelerator nvidia.com/gpu",
            id="accelerator-not-budgeted",
        ),
    ],
)
def test_kueue_usage_rejects_every_inexact_admission(assignments: list[dict[str, Any]], message: str) -> None:
    with pytest.raises(PodSetEnvelopeError, match=message):
        compare_kueue_usage(_gang_envelope(), assignments, accelerator_resource="nvidia.com/gpu")


def test_kueue_usage_ignores_unbudgeted_extended_resources_and_excluded_core_ones() -> None:
    """Only what a ClusterQueue budgets is reported, and it must be exact."""

    envelope = _gang_envelope()
    result = compare_kueue_usage(
        envelope,
        [
            # cpu and memory are excluded from this deployment's quota, and an
            # RDMA device sits outside every resource group.
            _assignment("coordinator", {}),
            _assignment("workers", {"example.com/rdma": "8", "nvidia.com/gpu": "8"}),
        ],
        accelerator_resource="nvidia.com/gpu",
    )
    assert (result.accelerator_per_replica, result.accelerator_aggregate) == (4, 8)
    with pytest.raises(PodSetEnvelopeError, match="admitted memory="):
        compare_kueue_usage(
            envelope,
            [
                _assignment("coordinator", {}),
                _assignment("workers", {"memory": "32Gi", "nvidia.com/gpu": "8"}),
            ],
            accelerator_resource="nvidia.com/gpu",
        )


def _production_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[FileScientificManifestRenderer, WorkloadResource]:
    """The real execution-map renderer bound to one frozen scientific attempt."""

    execution = {
        "schema": "fs2-serve.nebius.ai/scientific-execution-map/v3",
        "models": [
            {
                "model_id": "protein-design",
                "variant_id": "protein-design-h100",
                "workload_namespace": "fs2-models",
                "execution_identity_sha256": "f" * 64,
                "plan_adapter": {
                    "module": "fs2_serve.scientific_batch.adapters",
                    "function": "compile_adapter_run",
                },
                "stages": [
                    {
                        "stage_id": "design",
                        "image": "registry.example/protein@sha256:" + "a" * 64,
                        "collector_id": "protein-design-output-v1",
                        "validator_id": "protein-design-v1",
                        "mounts": [
                            {
                                "name": "artifact-workspace",
                                "kind": "artifact-workspace",
                                "claim_name": None,
                                "host_path": None,
                                "mount_path": "/mnt/fs2-scientific",
                                "sub_path": None,
                                "read_only": False,
                            }
                        ],
                        "service_account_name": "scientific-runner",
                        "workspace_uid": 10001,
                        "workspace_gid": 10001,
                        "resources": {
                            "requests": {"cpu": "4", "memory": "32Gi", "ephemeral_storage": "100Gi"},
                            "limits": {"cpu": "4", "memory": "32Gi", "ephemeral_storage": "100Gi"},
                        },
                        "active_deadline_seconds": 3600,
                        "termination_grace_seconds": 60,
                        "environment": {},
                        "required_node_labels": {},
                    }
                ],
            }
        ],
    }
    path = tmp_path / "execution-map.json"
    path.write_text(json.dumps(execution))
    plan = ScientificBatchPlan((ScientificStagePlan(stage_id="design"),))
    invocation = StageInvocation(
        stage_id="design",
        shard_id="main",
        argv=("python", "-m", "adapter", "run"),
        environment=(),
        working_directory="/mnt/fs2-scientific/work/design/main",
        consumes=(),
        produces="design-result",
        collector_id="protein-design-output-v1",
        validator_id="protein-design-v1",
        handoff_name=None,
    )
    adapter_plan = AdapterExecutionPlan(
        model_id="protein-design",
        variant_id="protein-design-h100",
        source_revision="b" * 40,
        request_sha256="1" * 64,
        controller_plan=plan,
        invocations=(invocation,),
        required_model_artifacts=(),
    )
    monkeypatch.setattr(
        "fs2_serve.scientific_batch.execution.importlib.import_module",
        lambda module: type(
            "Adapter",
            (),
            {"compile_adapter_run": staticmethod(lambda *args, **kwargs: adapter_plan)},
        ),
    )
    renderer = FileScientificManifestRenderer(
        path=path,
        profiles=profile_catalog(),
        tools_image="registry.example/control@sha256:" + "9" * 64,
        internal_api_url="http://control.fs2.svc:8080",
        capability_authority=ScientificWorkloadCapabilityAuthority(
            KeyedHasher(active_key_id="ledger-v1", keys={"ledger-v1": b"k" * 32})
        ),
    )
    bound = renderer.plan(
        profile_catalog().get("protein-design"),
        {},
        access_context=ArtifactAccessContext(profile="public", receipt_digest=None),
        input_artifacts=(),
    )
    snapshot = scheduling().freeze(
        service_class="customer-batch",
        model_id="protein-design",
        tenant_id="tenant-a",
        profile=profile_value(),
        plan=plan,
    )
    resource = WorkloadResource(
        operation_id=uuid4(),
        batch_id=uuid4(),
        workload_id=uuid4(),
        attempt_id=uuid4(),
        stage_id="design",
        shard_id="main",
        attempt_number=1,
        tenant_id="tenant-a",
        model_id="protein-design",
        variant_id="protein-design-h100",
        input_artifact_id=uuid4(),
        service_class=ServiceClass.CUSTOMER_BATCH,
        scheduling_snapshot_digest=snapshot.digest,
        namespace="fs2-models",
        name="scientific-design",
        kind=WorkloadKind.JOB,
        scheduling=snapshot.stages[0],
        invocation=bound.invocation("design", "main"),
        access_context=ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="tenant-a"),
        execution_map_sha256=bound.execution_map_sha256,
        execution_binding=bound.execution_binding("design"),
    )
    return renderer, resource


def test_production_renderer_freezes_the_exact_stage_pod_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real renderer's companions are inside the frozen envelope."""

    renderer, resource = _production_renderer(tmp_path, monkeypatch)
    envelope = envelope_from_manifest(renderer.render(resource), WorkloadKind.JOB)
    pod_set = envelope.pod_set(DEFAULT_JOB_POD_SET_NAME)
    assert pod_set.count == 1
    # The model's frozen 4 CPU / 32Gi request plus the artifact collector's
    # 100m / 256Mi, with the workspace init container below that sum.
    assert pod_set.per_replica_requests == STAGE_POD_REQUESTS
    assert pod_set.per_replica_limits == STAGE_POD_LIMITS
    assert pod_set.aggregate_requests == STAGE_POD_REQUESTS

    gang = replace(
        resource,
        attempt_id=uuid4(),
        shard_id=None,
        name="scientific-design-gang",
        kind=WorkloadKind.JOB_SET,
        gang_size=3,
        invocation=replace(resource.invocation, shard_id="gang"),  # type: ignore[arg-type]
    )
    gang_pod_set = envelope_from_manifest(renderer.render(gang), WorkloadKind.JOB_SET).pod_set("gang")
    assert gang_pod_set.count == 3
    assert gang_pod_set.per_replica_requests == STAGE_POD_REQUESTS
    assert gang_pod_set.aggregate_requests == ResourceVector.of(
        cpu_millis=3 * 4_100,
        memory_bytes=3 * (32 * GIB + 256 * MIB),
        ephemeral_storage_bytes=3 * 100 * GIB,
        accelerators={"nvidia.com/gpu": 3},
    )


def _cluster(
    tmp_path: Path,
    handler: Any,
    *,
    renderer: Any = None,
    name: str = "kube",
    fence: Fence | None = None,
) -> tuple[HttpScientificBatchCluster, httpx.AsyncClient]:
    token = tmp_path / f"{name}-token"
    token.write_text("x" * 32)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.test")
    return (
        HttpScientificBatchCluster(
            base_url="https://kubernetes.test",
            token_file=token,
            ca_file=tmp_path / "ca.crt",
            controller_id="controller-a",
            fence=fence or Fence(),
            renderer=renderer or JobRenderer(),
            writes_enabled=True,
            client=client,
        ),
        client,
    )


def _gang_resource(gang_size: int, *, accelerator_count: int = 1) -> WorkloadResource:
    snapshot = scheduling().freeze(
        service_class="customer-batch",
        model_id="protein-design",
        tenant_id="tenant-a",
        profile=profile_value(),
        plan=ScientificBatchPlan((ScientificStagePlan(stage_id="design"),)),
    )
    return WorkloadResource(
        operation_id=uuid4(),
        batch_id=uuid4(),
        workload_id=uuid4(),
        attempt_id=uuid4(),
        stage_id="design",
        shard_id=None,
        attempt_number=1,
        tenant_id="tenant-a",
        model_id="protein-design",
        variant_id="protein-design-h100",
        input_artifact_id=uuid4(),
        service_class=ServiceClass.CUSTOMER_BATCH,
        scheduling_snapshot_digest=snapshot.digest,
        namespace="fs2-models",
        name="scientific-gang",
        kind=WorkloadKind.JOB_SET,
        gang_size=gang_size,
        scheduling=replace(snapshot.stages[0], accelerator_count=accelerator_count),
    )


@pytest.mark.asyncio
async def test_apply_freezes_the_envelope_inside_the_manifest_digest(tmp_path: Path) -> None:
    """The frozen envelope travels with the object and binds its identity."""

    resource = _gang_resource(2)
    created: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            created.update(json.loads(request.content))
            body = json.loads(request.content)
            body["metadata"]["uid"] = "gang-uid"
            return httpx.Response(201, json=body)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    cluster, client = _cluster(tmp_path, handler, name="apply-token")
    await cluster.apply(resource, controller_fence=3)
    annotations = created["metadata"]["annotations"]
    envelope = envelope_from_json(annotations[PODSET_ENVELOPE_ANNOTATION], kind=WorkloadKind.JOB_SET)
    assert envelope.digest == annotations[PODSET_ENVELOPE_DIGEST_ANNOTATION]
    assert envelope.pod_set("gang").count == 2
    assert envelope.pod_set("gang").per_replica_requests.accelerator("nvidia.com/gpu") == 1
    assert envelope.pod_set("gang").aggregate_requests.accelerator("nvidia.com/gpu") == 2
    await client.aclose()

    # An object that already exists with a different envelope is refused,
    # because the envelope annotations are written before the manifest digest
    # and are therefore part of the attempt's immutable identity.
    stale = json.loads(json.dumps(created))
    stale["metadata"]["uid"] = "gang-uid"
    stale["spec"]["replicatedJobs"][0]["replicas"] = 4
    stale_envelope = envelope_from_manifest(stale, WorkloadKind.JOB_SET)
    stale["metadata"]["annotations"][PODSET_ENVELOPE_ANNOTATION] = stale_envelope.to_json()
    stale["metadata"]["annotations"][PODSET_ENVELOPE_DIGEST_ANNOTATION] = stale_envelope.digest
    assert _manifest_digest(stale) != created["metadata"]["annotations"][MANIFEST_ANNOTATION]
    stale["metadata"]["annotations"][MANIFEST_ANNOTATION] = _manifest_digest(stale)

    async def adopt(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409, json={"reason": "AlreadyExists"})
        return httpx.Response(200, json=stale)

    conflict_cluster, conflict_client = _cluster(tmp_path, adopt, name="adopt-token")
    with pytest.raises(BatchRepositoryConflictError, match="manifest differs from the frozen attempt"):
        await conflict_cluster.apply(resource, controller_fence=3)
    await conflict_client.aclose()


class _MisRenderer(JobRenderer):
    """A renderer whose resources disagree with the frozen decision."""

    def __init__(self, **mutation: Any) -> None:
        self.mutation = mutation

    def render(self, resource: WorkloadResource) -> dict[str, Any]:
        manifest = json.loads(json.dumps(dict(super().render(resource))))
        gpu = self.mutation.get("gpu")
        replicas = self.mutation.get("replicas")
        parallelism = self.mutation.get("parallelism")
        if resource.kind is WorkloadKind.JOB_SET:
            job = manifest["spec"]["replicatedJobs"][0]
            if replicas is not None:
                job["replicas"] = replicas
            pod = job["template"]["spec"]["template"]
        else:
            if parallelism is not None:
                manifest["spec"]["parallelism"] = parallelism
            pod = manifest["spec"]["template"]
        if gpu is not None:
            for block in ("requests", "limits"):
                resources = pod["spec"]["containers"][0]["resources"][block]
                if gpu == 0:
                    resources.pop("nvidia.com/gpu", None)
                else:
                    resources["nvidia.com/gpu"] = str(gpu)
        return manifest


@pytest.mark.parametrize(
    ("kind", "mutation", "message"),
    [
        pytest.param(WorkloadKind.JOB_SET, {"replicas": 1}, "instead of the frozen gang size 2", id="gang-not-applied"),
        pytest.param(WorkloadKind.JOB_SET, {"replicas": 4}, "instead of the frozen gang size 2", id="gang-doubled"),
        pytest.param(
            WorkloadKind.JOB_SET,
            {"gpu": 8},
            "per-Pod accelerator request differs from the frozen scheduling decision",
            id="wrong-per-pod-accelerator",
        ),
        pytest.param(WorkloadKind.JOB_SET, {"gpu": 0}, "requests no frozen accelerator", id="missing-accelerator"),
        pytest.param(
            WorkloadKind.JOB,
            {"parallelism": 4},
            "must reserve exactly one Pod per attempt",
            id="fanout-job-fans-out-internally",
        ),
    ],
)
@pytest.mark.asyncio
async def test_apply_refuses_a_rendered_envelope_that_is_wrong(
    tmp_path: Path, kind: WorkloadKind, mutation: dict[str, Any], message: str
) -> None:
    """A rendered envelope that disagrees with the frozen attempt never reaches the API."""

    resource = _gang_resource(2)
    if kind is WorkloadKind.JOB:
        resource = replace(resource, kind=WorkloadKind.JOB, shard_id="main", gang_size=None)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a refused envelope must not be created: {request.method} {request.url}")

    cluster, client = _cluster(
        tmp_path,
        handler,
        renderer=_MisRenderer(**mutation),
        name=f"mis-{kind}-{sorted(mutation)}-token".replace("/", "-"),
    )
    with pytest.raises(ScientificKubernetesError, match=message):
        await cluster.apply(resource, controller_fence=5)
    await client.aclose()


@pytest.mark.asyncio
async def test_apply_refuses_a_cpu_stage_that_renders_an_accelerator(tmp_path: Path) -> None:
    resource = replace(
        _gang_resource(2),
        kind=WorkloadKind.JOB,
        shard_id="main",
        gang_size=None,
    )
    resource = replace(
        resource,
        scheduling=replace(
            resource.scheduling,
            resource_class=ResourceClass.CPU,
            accelerator_resource_name=None,
            accelerator_count=0,
            placement_class=None,
            requested_resource_flavor="reference-data-cpu",
        ),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a CPU stage must never create an accelerator request")

    cluster, client = _cluster(tmp_path, handler, renderer=_MisRenderer(gpu=1), name="cpu-gpu-token")
    with pytest.raises(ScientificKubernetesError, match="CPU workload requests an accelerator"):
        await cluster.apply(resource, controller_fence=6)
    await client.aclose()


def _admitted_workload(usage: dict[str, str], *, count: int | None = None) -> dict[str, Any]:
    assignment: dict[str, Any] = {
        "name": "gang",
        "flavors": {"nvidia.com/gpu": "inference-h100-1x"},
        "resourceUsage": usage,
    }
    if count is not None:
        assignment["count"] = count
    return {
        "items": [
            {
                "metadata": {"uid": "gang-kueue-uid"},
                "status": {
                    "conditions": [
                        {"type": "QuotaReserved", "status": "True", "lastTransitionTime": "2026-09-03T08:00:00Z"},
                        {"type": "Admitted", "status": "True", "lastTransitionTime": "2026-09-03T08:00:01Z"},
                    ],
                    "admission": {"clusterQueue": "inference", "podSetAssignments": [assignment]},
                },
            }
        ]
    }


def _gang_handler(resource: WorkloadResource, usage: dict[str, str], *, count: int | None = None) -> Any:
    spec = _workload_spec(WorkloadKind.JOB_SET, gpu=1, gang_size=resource.gang_size)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/{resource.name}"):
            return httpx.Response(
                200,
                json=_live_workload(resource.ref, resource.attempt_id, uid="gang-uid", spec=spec),
            )
        if request.url.path.endswith("/workloads"):
            return httpx.Response(200, json=_admitted_workload(usage, count=count))
        if request.url.path.endswith("/resourceflavors/inference-h100-1x"):
            return httpx.Response(
                200, json={"metadata": {"labels": {"accelerator.fs2.nebius/pool-id": "h100-preemptible"}}}
            )
        return httpx.Response(200, json={"items": []})

    return handler


@pytest.mark.asyncio
async def test_observe_compares_the_gang_aggregate_and_records_the_per_replica_request(tmp_path: Path) -> None:
    resource = _gang_resource(4)
    ref = replace(resource.ref, uid="gang-uid")
    cluster, client = _cluster(
        tmp_path,
        _gang_handler(resource, {"nvidia.com/gpu": "4"}, count=4),
        name="observe-gang-token",
    )
    observation = await cluster.observe(ref, scheduling=resource.scheduling)
    # Kueue charged four accelerators for the gang; one Pod requested one.
    assert observation.scheduling_admission == SchedulingAdmission(
        resolved_pool_id="h100-preemptible",
        admitted_resource_flavor="inference-h100-1x",
        accelerator_resource_name="nvidia.com/gpu",
        accelerator_count=1,
        quota_reserved_at=datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
        admitted_at=datetime(2026, 9, 3, 8, 0, 1, tzinfo=UTC),
    )
    await client.aclose()


@pytest.mark.parametrize(
    ("usage", "count", "message"),
    [
        pytest.param({"nvidia.com/gpu": "1"}, None, "instead of the frozen 4", id="gang-not-multiplied"),
        pytest.param({"nvidia.com/gpu": "16"}, None, "instead of the frozen 4", id="gang-multiplied-twice"),
        pytest.param({"nvidia.com/gpu": "4"}, 1, "admitted 1 Pods for PodSet 'gang'", id="partial-admission"),
        pytest.param(
            {"nvidia.com/gpu": "4", "cpu": "4100m"},
            4,
            "admitted cpu=4100 for PodSet 'gang'",
            id="core-usage-not-multiplied",
        ),
    ],
)
@pytest.mark.asyncio
async def test_observe_refuses_an_admission_that_does_not_match_the_envelope(
    tmp_path: Path, usage: dict[str, str], count: int | None, message: str
) -> None:
    resource = _gang_resource(4)
    ref = replace(resource.ref, uid="gang-uid")
    cluster, client = _cluster(
        tmp_path,
        _gang_handler(resource, usage, count=count),
        name=f"observe-bad-{'-'.join(sorted(usage)).replace('/', '_')}-{count}-token",
    )
    with pytest.raises(ScientificKubernetesError, match=message):
        await cluster.observe(ref, scheduling=resource.scheduling)
    await client.aclose()


@pytest.mark.asyncio
async def test_observe_refuses_a_live_object_whose_resources_were_mutated(tmp_path: Path) -> None:
    """The annotation is the claim; the live Pod templates are the fact."""

    resource = _gang_resource(2)
    ref = replace(resource.ref, uid="gang-uid")
    frozen_spec = _workload_spec(WorkloadKind.JOB_SET, gpu=1, gang_size=2)

    def mutated(**mutation: Any) -> Any:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(f"/{resource.name}"):
                body = _live_workload(ref, resource.attempt_id, uid="gang-uid", spec=frozen_spec)
                job = body["spec"]["replicatedJobs"][0]
                if "replicas" in mutation:
                    job["replicas"] = mutation["replicas"]
                if "cpu" in mutation:
                    container = job["template"]["spec"]["template"]["spec"]["containers"][0]
                    container["resources"]["requests"]["cpu"] = mutation["cpu"]
                    container["resources"]["limits"]["cpu"] = mutation["cpu"]
                if mutation.get("drop_annotation"):
                    body["metadata"]["annotations"].pop(PODSET_ENVELOPE_ANNOTATION)
                if mutation.get("break_digest"):
                    body["metadata"]["annotations"][PODSET_ENVELOPE_DIGEST_ANNOTATION] = "0" * 64
                return httpx.Response(200, json=body)
            return httpx.Response(200, json={"items": []})

        return handler

    for mutation, message in (
        ({"replicas": 4}, "differ from the frozen PodSet envelope"),
        ({"cpu": "64"}, "differ from the frozen PodSet envelope"),
        ({"drop_annotation": True}, "no frozen PodSet resource envelope"),
        ({"break_digest": True}, "digest does not match its document"),
    ):
        cluster, client = _cluster(
            tmp_path, mutated(**mutation), name=f"mutated-{sorted(mutation)}-token".replace("/", "-")
        )
        with pytest.raises(BatchRepositoryConflictError, match=message):
            await cluster.observe(ref, scheduling=resource.scheduling)
        await client.aclose()


def test_live_h100_kueue_admission_matches_the_derived_envelope() -> None:
    """The arithmetic agrees with a real Kueue v0.17.8 admission.

    The fixture is a read-only recording from the shared H100 cluster: one
    admitted GPU Job in `fs2-models`, its Pod template resources, and the exact
    `status.admission` Kueue wrote for it. It pins three facts this module
    depends on and no unit test can assert on its own: Kueue names a batch/v1
    Job's single PodSet `main`, it reports the PodSet's Pod `count`, and with
    `excludeResourcePrefixes: [cpu, memory, ephemeral-storage]` it reports only
    the budgeted accelerator, so the comparison must accept an absent core
    resource while still matching every reported one exactly.
    """

    fixture = json.loads(Path(__file__).parent.joinpath("fixtures/live-kueue-admission-h100-complexa.json").read_text())
    admission = fixture["kueue_admission"]
    envelope = envelope_from_manifest(fixture["job"], WorkloadKind.JOB)
    pod_set = envelope.pod_sets[0]
    assert (pod_set.name, pod_set.count) == (DEFAULT_JOB_POD_SET_NAME, 1)
    assert [(item["name"], item["count"]) for item in fixture["kueue_spec_pod_sets"]] == [(pod_set.name, pod_set.count)]
    assert pod_set.per_replica_requests == ResourceVector.of(
        cpu_millis=8_000, memory_bytes=64 * GIB, accelerators={"nvidia.com/gpu": 1}
    )
    result = compare_kueue_usage(envelope, admission["podSetAssignments"], accelerator_resource="nvidia.com/gpu")
    assert (result.accelerator_per_replica, result.accelerator_aggregate) == (1, 1)
    # cpu and memory are excluded from this deployment's quota, so Kueue
    # reports neither and the envelope still holds their exact frozen figures.
    assert result.compared == (("main", ("nvidia.com/gpu",)),)
    assert result.aggregate.cpu_millis == 8_000

    doubled = json.loads(json.dumps(admission["podSetAssignments"]))
    doubled[0]["resourceUsage"]["nvidia.com/gpu"] = "2"
    with pytest.raises(PodSetEnvelopeError, match="instead of the frozen 1"):
        compare_kueue_usage(envelope, doubled, accelerator_resource="nvidia.com/gpu")


def test_envelope_shapes_are_bounded_and_self_consistent() -> None:
    with pytest.raises(PodSetEnvelopeError, match="exactly one PodSet"):
        WorkloadEnvelope(
            kind=WorkloadKind.JOB,
            pod_sets=(
                PodSetEnvelope(
                    name="a",
                    count=1,
                    per_replica_requests=ResourceVector.of(cpu_millis=1),
                    per_replica_limits=ResourceVector.of(cpu_millis=1),
                ),
                PodSetEnvelope(
                    name="b",
                    count=1,
                    per_replica_requests=ResourceVector.of(cpu_millis=1),
                    per_replica_limits=ResourceVector.of(cpu_millis=1),
                ),
            ),
        )
    with pytest.raises(PodSetEnvelopeError, match="replica count is outside the controller bound"):
        PodSetEnvelope(
            name="gang",
            count=0,
            per_replica_requests=ResourceVector.of(cpu_millis=1),
            per_replica_limits=ResourceVector.of(cpu_millis=1),
        )
    with pytest.raises(PodSetEnvelopeError, match="limit cannot be smaller than its request"):
        PodSetEnvelope(
            name="gang",
            count=1,
            per_replica_requests=ResourceVector.of(cpu_millis=2_000),
            per_replica_limits=ResourceVector.of(cpu_millis=1_000),
        )
    with pytest.raises(PodSetEnvelopeError, match="request and limit must be identical"):
        PodSetEnvelope(
            name="gang",
            count=1,
            per_replica_requests=ResourceVector.of(accelerators={"nvidia.com/gpu": 2}),
            per_replica_limits=ResourceVector.of(accelerators={"nvidia.com/gpu": 4}),
        )
    with pytest.raises(PodSetEnvelopeError, match="declares no container"):
        envelope_from_manifest({"spec": {"template": {"spec": {"containers": []}}}}, WorkloadKind.JOB)
    # A CPU-only rendered Pod claims no accelerator at all, not zero of one.
    assert (
        envelope_from_manifest({"spec": {"template": _rendered_pod(gpu=0)}}, WorkloadKind.JOB)
        .pod_sets[0]
        .per_replica_requests.accelerators
        == ()
    )

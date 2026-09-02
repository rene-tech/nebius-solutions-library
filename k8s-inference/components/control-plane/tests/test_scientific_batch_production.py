from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import CATALOG_ROOT, REPO_ROOT
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.mcpserver import Context
from scientific_batch_fakes import FakeScientificBatchCluster, FakeScientificBatchRepository

from fs2_serve.admission import AdmissionService
from fs2_serve.api import AppRuntime, create_app
from fs2_serve.auth import OperatorSessionService, PepperRing, TokenService
from fs2_serve.mcp_server import PATTokenVerifier, build_mcp_server
from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import Principal, Scope, TokenCreate
from fs2_serve.runtime import StubRuntimeClient
from fs2_serve.scientific_artifacts import (
    ArtifactAccess,
    ArtifactDirection,
    BeginArtifactUpload,
    ExecutionProvenance,
    FinalizeArtifactUpload,
    MemoryArtifactRepository,
    SemanticValidation,
    SemanticValidationStatus,
    TerminalResultDraft,
    TerminalResultStatus,
    VerifiedStoredObject,
    artifact_storage_key,
    build_terminal_manifest,
)
from fs2_serve.scientific_batch.artifact_bridge import ArtifactServiceBridge
from fs2_serve.scientific_batch.codec import state_from_value, state_to_value
from fs2_serve.scientific_batch.controller import ScientificBatchController
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer, ScientificExecutionMapError
from fs2_serve.scientific_batch.kubernetes import (
    ATTEMPT_LABEL,
    MANIFEST_ANNOTATION,
    HttpScientificBatchCluster,
    _failure,
)
from fs2_serve.scientific_batch.models import (
    ArtifactCommit,
    CheckpointMode,
    ExecutionMode,
    FailureKind,
    LifecyclePhase,
    PreemptionMode,
    ResourceClass,
    ScientificBatchPlan,
    ScientificBatchState,
    ScientificStagePlan,
    ServiceClass,
    WorkloadKind,
    WorkloadObservation,
    WorkloadResource,
    WorkloadState,
)
from fs2_serve.scientific_batch.profile_catalog import (
    SCIENTIFIC_REQUEST_SCHEMA,
    SCIENTIFIC_RESULT_SCHEMA,
    ScientificProfileCatalog,
    ScientificWorkloadProfile,
)
from fs2_serve.scientific_batch.scheduling import SchedulingContractResolver
from fs2_serve.scientific_batch.service import ScientificBatchService
from fs2_serve.scientific_batch.worker import ScientificBatchWorker
from fs2_serve.settings import Settings
from fs2_serve.telemetry import Metrics

PARAMETER_SCHEMA = "fs2-serve.nebius.ai/example-parameters/v1"


def profile_value() -> dict[str, object]:
    digest = "a" * 64
    return {
        "model_id": "protein-design",
        "state": "qualified",
        "route_exposed": True,
        "execution_identity": {
            "model_revision": "b" * 40,
            "runtime_image_digest": f"sha256:{digest}",
            "runtime_recipe_sha256": "c" * 64,
            "workload_recipe_sha256": "d" * 64,
            "artifact_manifest_digest": "e" * 64,
            "execution_identity_sha256": "f" * 64,
        },
        "interface": {
            "operations": ["design"],
            "service_classes": ["customer-batch"],
            "parameter_schema": PARAMETER_SCHEMA,
            "mcp": {"invocable": True},
        },
        "access": {"profile": "standard", "state": "not-required", "receipt_digest": None},
        "resources": {"gpu_count": 1, "compatible_pool_ids": ["h100-preemptible"]},
        "workload": {
            "retry": {"max_attempts": 2},
            "stages": [
                {
                    "id": "design",
                    "needs": [],
                    "resource_class": "gpu",
                    "admission_mode": "independent-jobs",
                    "min_parallelism": 1,
                    "max_parallelism": 4,
                    "checkpoint_mode": "restart",
                    "preemption_mode": "restartable",
                }
            ],
        },
        "semantic_validation": {"validator_id": "protein-design-v1", "state": "qualified"},
    }


def profile_catalog() -> ScientificProfileCatalog:
    def load(name: str) -> Draft202012Validator:
        return Draft202012Validator(json.loads((CATALOG_ROOT / "schema" / name).read_text()))

    profile = ScientificWorkloadProfile(MappingProxyType(profile_value()))
    return ScientificProfileCatalog(
        profiles={profile.model_id: profile},
        validators={
            SCIENTIFIC_REQUEST_SCHEMA: load("scientific-run-request.schema.json"),
            SCIENTIFIC_RESULT_SCHEMA: load("scientific-run-result.schema.json"),
            PARAMETER_SCHEMA: Draft202012Validator(
                {"type": "object", "additionalProperties": False, "maxProperties": 0}
            ),
        },
    )


def scheduling() -> SchedulingContractResolver:
    return SchedulingContractResolver(
        {
            "schema": "fs2-serve.nebius.ai/kueue-scheduling/v1",
            "service_classes": {
                "customer-batch": {
                    "workload_priority_class": "standard",
                    "priority": 0,
                    "default_local_queue": "scientific",
                    "preemption_mode": "restartable",
                    "pool_preference": ["h100-hot", "h100-preemptible"],
                }
            },
            "local_queues": {
                "scientific": {
                    "metadata": {"name": "scientific", "namespace": "fs2-models"},
                    "spec": {"clusterQueue": "inference"},
                }
            },
            "cluster_queues": {
                "inference": {
                    "metadata": {"name": "inference"},
                    "spec": {"resourceGroups": [{"coveredResources": ["nvidia.com/gpu"], "flavors": []}]},
                }
            },
        }
    )


class FakeArtifactAccess:
    def __init__(self, pointer: dict[str, object]) -> None:
        self.pointer = pointer

    async def validate_input(self, pointer, *, tenant_id: str) -> None:
        assert tenant_id == "tenant-a"
        assert pointer == self.pointer

    async def artifact_response(self, artifact_id, *, tenant_id: str):
        return self.pointer

    async def result_response(self, operation_id, *, tenant_id: str):
        assert tenant_id == "tenant-a"
        result = json.loads((CATALOG_ROOT / "contracts/examples/scientific-run-result.example.json").read_text())
        result["operation_id"] = str(operation_id)
        result["batch_id"] = f"batch.{operation_id}"
        result["workload_id"] = f"workload.{operation_id}"
        return result


async def principal(store: MemoryStore) -> Principal:
    token_id = uuid4()
    scopes = {
        Scope.INFERENCE_INVOKE,
        Scope.OPERATIONS_READ,
        Scope.OPERATIONS_RESULT,
        Scope.OPERATIONS_CANCEL,
    }
    await store.issue_token(
        token_id=token_id,
        prefix="fs2_pat_scientific",
        pepper_key_id="pepper-v1",
        digest="test-digest",
        request=TokenCreate(
            principal_id="scientist-a",
            tenant_id="tenant-a",
            scopes=scopes,
            models={"protein-design"},
            max_concurrency=4,
        ),
        created_by="test",
    )
    return Principal(
        token_id=token_id,
        token_prefix="fs2_pat_scientific",
        principal_id="scientist-a",
        tenant_id="tenant-a",
        scopes=frozenset(str(item) for item in scopes),
        models=frozenset({"protein-design"}),
        max_concurrency=4,
    )


def scientific_runtime(
    registry, cipher, hasher
) -> tuple[
    AppRuntime,
    ScientificBatchController,
    FakeScientificBatchRepository,
    FakeScientificBatchCluster,
    dict[str, object],
]:
    store = MemoryStore(cipher, hasher)
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()
    controller = ScientificBatchController(
        repository=repository,
        cluster=cluster,
        controller_id="controller-a",
        namespace="fs2-models",
    )
    pointer: dict[str, object] = {
        "artifact_id": str(uuid4()),
        "sha256": "1" * 64,
        "size_bytes": 100,
        "media_type": "application/vnd.fs2.scientific-manifest+json",
    }
    service = ScientificBatchService(
        store=store,
        repository=repository,
        controller=controller,
        profiles=profile_catalog(),
        scheduling=scheduling(),
        artifacts=FakeArtifactAccess(pointer),
    )
    settings = Settings(
        run_workers=False,
        max_request_bytes=64 * 1024,
        public_base_url="https://inference.test.invalid",
        authorization_server_url="https://identity.test.invalid",
        catalog_dir=CATALOG_ROOT,
        bindings_file=REPO_ROOT / "unused.json",
    )
    metrics = Metrics(registry.list(enabled_only=True))
    peppers = PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32})
    runtime = AppRuntime(
        settings=settings,
        registry=registry,
        store=store,
        tokens=TokenService(store, peppers),
        admission=AdmissionService(
            registry=registry,
            store=store,
            runtime=StubRuntimeClient(),
            metrics=metrics,
            worker_concurrency=1,
            poll_seconds=0.01,
            lease_seconds=30,
            maintenance_interval_seconds=1,
            shutdown_grace_seconds=1,
        ),
        metrics=metrics,
        admin_token=b"a" * 32,
        operator_sessions=OperatorSessionService(store, peppers),
        owns_store=False,
        scientific_batches=service,
    )
    return runtime, controller, repository, cluster, pointer


@pytest.mark.asyncio
async def test_submit_freezes_public_profile_and_never_enters_generic_worker(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    identity = await principal(store)
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()
    controller = ScientificBatchController(
        repository=repository,
        cluster=cluster,
        controller_id="controller-a",
        namespace="fs2-models",
    )
    pointer = {
        "artifact_id": str(uuid4()),
        "sha256": "1" * 64,
        "size_bytes": 100,
        "media_type": "application/json",
    }
    service = ScientificBatchService(
        store=store,
        repository=repository,  # type: ignore[arg-type]
        controller=controller,
        profiles=profile_catalog(),
        scheduling=scheduling(),
        artifacts=FakeArtifactAccess(pointer),
    )
    request = {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": "design",
        "service_class": "customer-batch",
        "input_manifest": pointer,
        "parameters": {},
    }
    first = await service.submit(
        principal=identity,
        model_id="protein-design",
        request=request,
        idempotency_key="scientific-idempotency-0001",
    )
    replay = await service.submit(
        principal=identity,
        model_id="protein-design",
        request=request,
        idempotency_key="scientific-idempotency-0001",
    )
    operation_id = UUID(first["operation"]["id"])
    state = repository.records[operation_id]
    assert replay["operation"]["id"] == first["operation"]["id"]
    assert replay["operation"]["reused"] is True
    assert state.model_id == "protein-design"
    assert state.scheduling.service_class is ServiceClass.CUSTOMER_BATCH
    assert state.scheduling.stages[0].resolved_pool_preference == ("h100-preemptible",)
    assert await store.claim_operation("generic-worker", lease_seconds=30) is None
    await controller.reconcile_once()
    assert cluster.apply_history[0].kind is WorkloadKind.JOB
    assert cluster.apply_history[0].scheduling == state.scheduling.stages[0]


@pytest.mark.asyncio
async def test_public_http_and_mcp_share_scientific_operation_lifecycle(registry, cipher, hasher) -> None:
    runtime, controller, repository, cluster, pointer = scientific_runtime(registry, cipher, hasher)
    scopes = {
        Scope.INFERENCE_INVOKE,
        Scope.MCP_INVOKE,
        Scope.OPERATIONS_READ,
        Scope.OPERATIONS_RESULT,
        Scope.OPERATIONS_CANCEL,
    }
    issued = await runtime.tokens.issue(
        TokenCreate(
            principal_id="scientist-a",
            tenant_id="tenant-a",
            scopes=scopes,
            models={"protein-design"},
            max_concurrency=4,
        ),
        created_by="test",
    )
    request = {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": "design",
        "service_class": "customer-batch",
        "input_manifest": pointer,
        "parameters": {},
    }
    app = create_app(runtime)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://inference.test.invalid",
            headers={"authorization": f"Bearer {issued.token}"},
        ) as client:
            submitted = await client.post(
                "/v1/models/protein-design:submit",
                headers={"idempotency-key": "scientific-http-0001"},
                json=request,
            )
            assert submitted.status_code == 202
            operation_id = UUID(submitted.json()["operation"]["id"])
            assert submitted.headers["location"] == f"/v1/operations/{operation_id}"

            await controller.reconcile_once()
            attempt = repository.records[operation_id].stage("design").attempts[0]
            cluster.set_observation(
                attempt.workload,
                WorkloadObservation(
                    ref=attempt.workload,
                    attempt_id=attempt.attempt_id,
                    state=WorkloadState.SUCCEEDED,
                    phases=(LifecyclePhase.ADMITTED, LifecyclePhase.ACTIVE_COMPUTE),
                ),
            )
            repository.put_commit(
                ArtifactCommit(
                    operation_id=operation_id,
                    stage_id="design",
                    attempt_ids=(attempt.attempt_id,),
                    manifest_digest="sha256:" + "2" * 64,
                    validation_digest="sha256:" + "3" * 64,
                    committed_at=datetime.now(UTC),
                    validated_at=datetime.now(UTC),
                    semantic_valid=True,
                )
            )
            await controller.reconcile_once()
            await controller.reconcile_once()
            await controller.reconcile_once()

            status = await client.get(f"/v1/operations/{operation_id}")
            events = await client.get(f"/v1/operations/{operation_id}/events")
            artifact = await client.get(f"/v1/artifacts/{pointer['artifact_id']}")
            result = await client.get(f"/v1/operations/{operation_id}/result")
            assert status.status_code == events.status_code == artifact.status_code == result.status_code == 200
            assert status.json()["batch"]["status"] == "succeeded"
            assert events.json()["data"][-1]["kind"] == "batch_succeeded"
            assert artifact.json() == pointer
            assert result.json()["schema"] == "fs2-serve.nebius.ai/scientific-run-result/v1"

    server = build_mcp_server(runtime)
    access = await PATTokenVerifier(runtime).verify_token(issued.token)
    assert access is not None
    context = Context(mcp_server=server, subscriptions=server._subscriptions)  # type: ignore[attr-defined]
    auth_token = auth_context_var.set(AuthenticatedUser(access))
    try:
        submitted_mcp = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "submit_scientific_run",
            {
                "model_id": "protein-design",
                "request": request,
                "idempotency_key": "scientific-mcp-0001",
            },
            context,
            convert_result=False,
        )
        mcp_operation_id = submitted_mcp["operation"]["id"]
        status_mcp = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "get_scientific_status", {"operation_id": mcp_operation_id}, context, convert_result=False
        )
        events_mcp = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "list_scientific_events", {"operation_id": mcp_operation_id}, context, convert_result=False
        )
        artifact_mcp = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "get_scientific_artifact",
            {"artifact_id": pointer["artifact_id"]},
            context,
            convert_result=False,
        )
        result_mcp = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "get_scientific_result", {"operation_id": str(operation_id)}, context, convert_result=False
        )
        cancelled_mcp = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "cancel_scientific_run", {"operation_id": mcp_operation_id}, context, convert_result=False
        )
        assert status_mcp["batch"]["status"] == "queued"
        assert events_mcp["data"] == []
        assert artifact_mcp == pointer
        assert result_mcp["schema"] == "fs2-serve.nebius.ai/scientific-run-result/v1"
        assert cancelled_mcp["batch"]["cancel_requested"] is True
    finally:
        auth_context_var.reset(auth_token)


def test_internal_state_codec_round_trip_and_rejects_type_coercion() -> None:
    plan = ScientificBatchPlan(
        (
            ScientificStagePlan(
                stage_id="design",
                mode=ExecutionMode.FANOUT,
                resource_class=ResourceClass.GPU,
                checkpoint_mode=CheckpointMode.RESTART,
                preemption_mode=PreemptionMode.RESTARTABLE,
            ),
        )
    )
    snapshot = scheduling().freeze(
        service_class="customer-batch",
        model_id="protein-design",
        profile=profile_value(),
        plan=plan,
        captured_at=datetime(2026, 9, 2, tzinfo=UTC),
    )
    state = ScientificBatchState.admit(
        operation_id=uuid4(),
        tenant_id="tenant-a",
        model_id="protein-design",
        plan=plan,
        scheduling=snapshot,
    )
    value = state_to_value(state)
    assert state_from_value(value) == state
    value["cancel_requested"] = "false"
    with pytest.raises(ValueError, match="not a boolean"):
        state_from_value(value)


class Fence:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str, int]] = []

    async def assert_fence(self, operation_id: UUID, *, controller_id: str, fencing_token: int) -> None:
        self.calls.append((operation_id, controller_id, fencing_token))


class JobRenderer:
    def render(self, resource: WorkloadResource):
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": resource.name, "namespace": resource.namespace},
            "spec": {
                "template": {
                    "metadata": {},
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [{"name": "model", "image": "registry/model@sha256:" + "a" * 64}],
                    },
                }
            },
        }


@pytest.mark.asyncio
async def test_kubernetes_writer_creates_real_kueue_job_shape_and_observes_attempt(tmp_path: Path) -> None:
    operation_id = uuid4()
    attempt_id = uuid4()
    plan = ScientificBatchPlan((ScientificStagePlan(stage_id="design"),))
    snapshot = scheduling().freeze(
        service_class="customer-batch",
        model_id="protein-design",
        profile=profile_value(),
        plan=plan,
    )
    resource = WorkloadResource(
        operation_id=operation_id,
        batch_id=uuid4(),
        workload_id=uuid4(),
        attempt_id=attempt_id,
        stage_id="design",
        shard_id="main",
        attempt_number=1,
        tenant_id="tenant-a",
        model_id="protein-design",
        service_class=ServiceClass.CUSTOMER_BATCH,
        scheduling_snapshot_digest=snapshot.digest,
        namespace="fs2-models",
        name="scientific-job",
        kind=WorkloadKind.JOB,
        scheduling=snapshot.stages[0],
    )
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "scientific"
            assert body["metadata"]["labels"]["kueue.x-k8s.io/priority-class"] == "standard"
            assert body["spec"]["suspend"] is True and body["spec"]["backoffLimit"] == 0
            assert body["spec"]["template"]["metadata"]["labels"][ATTEMPT_LABEL] == str(attempt_id)
            assert MANIFEST_ANNOTATION in body["metadata"]["annotations"]
            body["metadata"]["uid"] = "job-uid"
            return httpx.Response(201, json=body)
        if request.url.path.endswith("/scientific-job"):
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "name": "scientific-job",
                        "namespace": "fs2-models",
                        "uid": "job-uid",
                        "labels": {ATTEMPT_LABEL: str(attempt_id)},
                    },
                    "status": {"succeeded": 1},
                },
            )
        return httpx.Response(200, json={"items": []})

    token = tmp_path / "token"
    token.write_text("x" * 32)
    fence = Fence()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.test")
    cluster = HttpScientificBatchCluster(
        base_url="https://kubernetes.test",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        controller_id="controller-a",
        fence=fence,
        renderer=JobRenderer(),
        writes_enabled=True,
        client=client,
    )
    ref = await cluster.apply(resource, controller_fence=7)
    observation = await cluster.observe(ref)
    assert ref.uid == "job-uid"
    assert observation.attempt_id == attempt_id
    assert observation.state.value == "succeeded"
    assert fence.calls == [(operation_id, "controller-a", 7)]
    assert [request.method for request in requests] == ["POST", "GET", "GET"]
    await client.aclose()


def test_execution_map_renders_only_digest_qualified_direct_argv(tmp_path: Path) -> None:
    execution = {
        "schema": "fs2-serve.nebius.ai/scientific-execution-map/v1",
        "models": [
            {
                "model_id": "protein-design",
                "execution_identity_sha256": "f" * 64,
                "stages": [
                    {
                        "stage_id": "design",
                        "image": "registry.example/protein@sha256:" + "a" * 64,
                        "command": ["python", "-m", "adapter"],
                        "args": ["run"],
                        "service_account_name": "scientific-runner",
                        "resources": {"cpu": "4", "memory": "32Gi", "ephemeral_storage": "100Gi"},
                        "active_deadline_seconds": 3600,
                        "termination_grace_seconds": 60,
                        "environment": {"FS2_ADAPTER_MODE": "production"},
                    }
                ],
            }
        ],
    }
    path = tmp_path / "execution-map.json"
    path.write_text(json.dumps(execution))
    renderer = FileScientificManifestRenderer(path=path, profiles=profile_catalog())
    plan = ScientificBatchPlan((ScientificStagePlan(stage_id="design"),))
    snapshot = scheduling().freeze(
        service_class="customer-batch",
        model_id="protein-design",
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
        service_class=ServiceClass.CUSTOMER_BATCH,
        scheduling_snapshot_digest=snapshot.digest,
        namespace="fs2-models",
        name="scientific-design",
        kind=WorkloadKind.JOB,
        scheduling=snapshot.stages[0],
    )
    manifest = renderer.render(resource)
    container = manifest["spec"]["template"]["spec"]["containers"][0]  # type: ignore[index]
    assert container["image"].endswith("@sha256:" + "a" * 64)
    assert container["command"] == ["python", "-m", "adapter"]
    assert all("request" not in item["name"].lower() for item in container["env"])
    gang = replace(
        resource,
        attempt_id=uuid4(),
        shard_id=None,
        name="scientific-design-gang",
        kind=WorkloadKind.JOB_SET,
        gang_size=2,
    )
    jobset = renderer.render(gang)
    assert jobset["kind"] == "JobSet"
    assert jobset["spec"]["replicatedJobs"][0]["replicas"] == 2  # type: ignore[index]

    execution["models"][0]["stages"][0]["active_deadline_seconds"] = True
    path.write_text(json.dumps(execution))
    with pytest.raises(ScientificExecutionMapError, match="active deadline"):
        FileScientificManifestRenderer(path=path, profiles=profile_catalog())


def test_kubernetes_failure_taxonomy_retries_only_known_infrastructure() -> None:
    assert _failure(["BackoffLimitExceeded"])[1] is FailureKind.APPLICATION
    assert _failure(["Evicted"])[1] is FailureKind.PREEMPTION
    assert _failure(["NodeLost"])[1] is FailureKind.INFRASTRUCTURE


@pytest.mark.asyncio
async def test_scientific_worker_has_supervised_start_and_shutdown() -> None:
    worker = ScientificBatchWorker(
        ScientificBatchController(
            repository=FakeScientificBatchRepository(),
            cluster=FakeScientificBatchCluster(),
            controller_id="worker-a",
            namespace="fs2-models",
        ),
        workers=2,
        poll_seconds=0.05,
    )
    assert worker.health()["ready"] is False
    await worker.start()
    assert worker.health() == {"ready": True, "workers": 2, "consecutive_failures": 0}
    await worker.close()
    assert worker.health()["ready"] is False


@pytest.mark.asyncio
async def test_scientific_worker_paces_continuously_claimable_batches() -> None:
    class AlwaysClaimableController:
        def __init__(self) -> None:
            self.calls = 0

        async def reconcile_once(self):
            self.calls += 1
            return uuid4()

    controller = AlwaysClaimableController()
    worker = ScientificBatchWorker(controller, workers=1, poll_seconds=0.05)  # type: ignore[arg-type]
    await worker.start()
    await asyncio.sleep(0.13)
    await worker.close()
    assert 2 <= controller.calls <= 4


@pytest.mark.asyncio
async def test_artifact_bridge_consumes_owned_records_and_emits_canonical_result(registry, cipher, hasher) -> None:
    runtime, controller, batches, cluster, pointer = scientific_runtime(registry, cipher, hasher)
    identity = await principal(runtime.store)  # type: ignore[arg-type]
    assert runtime.scientific_batches is not None
    submitted = await runtime.scientific_batches.submit(
        principal=identity,
        model_id="protein-design",
        request={
            "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
            "operation": "design",
            "service_class": "customer-batch",
            "input_manifest": pointer,
            "parameters": {},
        },
        idempotency_key="scientific-artifact-bridge-0001",
    )
    operation_id = UUID(submitted["operation"]["id"])
    await controller.reconcile_once()
    attempt = batches.records[operation_id].stage("design").attempts[0]
    cluster.set_observation(
        attempt.workload,
        WorkloadObservation(
            ref=attempt.workload,
            attempt_id=attempt.attempt_id,
            state=WorkloadState.SUCCEEDED,
            phases=(LifecyclePhase.ADMITTED, LifecyclePhase.ACTIVE_COMPUTE),
        ),
    )
    batches.put_commit(
        ArtifactCommit(
            operation_id=operation_id,
            stage_id="design",
            attempt_ids=(attempt.attempt_id,),
            manifest_digest="sha256:" + "2" * 64,
            validation_digest="sha256:" + "3" * 64,
            committed_at=datetime.now(UTC),
            validated_at=datetime.now(UTC),
            semantic_valid=True,
        )
    )
    await controller.reconcile_once()
    await controller.reconcile_once()
    await controller.reconcile_once()

    artifacts = MemoryArtifactRepository()
    await artifacts.register_operation(operation_id, tenant_id="tenant-a", attempt=0)

    async def add_artifact(direction: ArtifactDirection, value: bytes, media_type: str):
        digest = "sha256:" + hashlib.sha256(value).hexdigest()
        request = BeginArtifactUpload(
            upload_id=uuid4(),
            operation_id=operation_id,
            tenant_id="tenant-a",
            attempt=0,
            direction=direction,
            expected_digest=digest,
            expected_size_bytes=len(value),
            media_type=media_type,
            access=ArtifactAccess(),
        )
        storage_key = artifact_storage_key(
            tenant_id="tenant-a",
            operation_id=operation_id,
            attempt=0,
            direction=direction,
            digest=digest,
        )
        await artifacts.begin_upload(request, storage_key)
        return await artifacts.finalize_upload(
            FinalizeArtifactUpload(
                upload_id=request.upload_id,
                operation_id=operation_id,
                tenant_id="tenant-a",
                attempt=0,
            ),
            VerifiedStoredObject(
                storage_key=storage_key,
                digest=digest,
                size_bytes=len(value),
                media_type=media_type,
            ),
            artifact_id=uuid4(),
        )

    input_manifest = await add_artifact(
        ArtifactDirection.INPUT,
        b'{"input":"manifest"}',
        "application/vnd.fs2.scientific-manifest+json",
    )
    output_manifest = await add_artifact(
        ArtifactDirection.OUTPUT,
        b'{"output":"manifest"}',
        "application/vnd.fs2.scientific-manifest+json",
    )
    evidence = await add_artifact(ArtifactDirection.OUTPUT, b'{"valid":true}', "application/json")
    completed_at = datetime.now(UTC)
    terminal = build_terminal_manifest(
        TerminalResultDraft(
            operation_id=operation_id,
            tenant_id="tenant-a",
            attempt=0,
            status=TerminalResultStatus.SUCCEEDED,
            input_artifacts=(input_manifest,),
            output_artifacts=(output_manifest, evidence),
            provenance=ExecutionProvenance(
                model_id="protein-design",
                model_revision="b" * 40,
                runtime_image_digest="sha256:" + "a" * 64,
                workload_spec_digest="sha256:" + "4" * 64,
                scheduling_snapshot_digest=batches.records[operation_id].scheduling.digest,
                job_uid=attempt.workload.uid or "job-a",
                pod_uids=("pod-a",),
                started_at=completed_at,
                completed_at=completed_at,
            ),
            validation=SemanticValidation(
                validator_id="protein-design-v1",
                validator_revision="sha256:" + "5" * 64,
                status=SemanticValidationStatus.PASSED,
                evidence_artifact=evidence,
            ),
            completed_at=completed_at,
        ),
        committed_at=datetime.now(UTC),
    )
    await artifacts.commit_terminal_result(terminal)
    bridge = ArtifactServiceBridge(
        artifacts=artifacts,
        batches=batches,
        profiles=profile_catalog(),
        store=runtime.store,
    )
    result = await bridge.result_response(operation_id, tenant_id="tenant-a")
    profile_catalog().validate_result(result)
    assert result["input_manifest"] == input_manifest.to_public_ref().model_dump(mode="json", exclude_none=True)
    assert result["output_manifest"] == output_manifest.to_public_ref().model_dump(mode="json", exclude_none=True)
    assert result["semantic_validation"]["status"] == "passed"  # type: ignore[index]
    assert "storage_key" not in json.dumps(result)

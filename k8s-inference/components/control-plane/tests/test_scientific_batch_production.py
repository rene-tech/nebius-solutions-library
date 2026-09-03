from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import CATALOG_ROOT, REPO_ROOT, SOLUTION_ROOT
from fastapi import FastAPI
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.mcpserver import Context
from scientific_batch_fakes import FakeScientificBatchCluster, FakeScientificBatchRepository
from test_scientific_artifacts import ALLOWED_MEDIA_TYPES, FakeObjectStore, digest

from fs2_serve.admission import AdmissionService
from fs2_serve.api import AppRuntime, create_app
from fs2_serve.auth import OperatorSessionService, PepperRing, TokenService
from fs2_serve.crypto import KeyedHasher
from fs2_serve.mcp_server import PATTokenVerifier, build_mcp_server
from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import Principal, Scope, TokenCreate
from fs2_serve.runtime import StubRuntimeClient
from fs2_serve.scientific_artifacts import (
    ArtifactAccess,
    ArtifactDirection,
    ArtifactDownload,
    ArtifactRecord,
    AttemptStatus,
    BeginArtifactUpload,
    BeginUploadResult,
    CloseStageAttempt,
    EphemeralHandle,
    FinalizeArtifactUpload,
    KueueAdmission,
    MemoryArtifactRepository,
    OpenStageAttempt,
    ScientificArtifactService,
    UploadIntent,
    VerifiedStoredObject,
    artifact_storage_key,
)
from fs2_serve.scientific_batch.artifact_bridge import ArtifactServiceBridge
from fs2_serve.scientific_batch.capability import ScientificWorkloadCapabilityAuthority
from fs2_serve.scientific_batch.codec import state_from_value, state_to_value
from fs2_serve.scientific_batch.controller import ScientificBatchController
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer, ScientificExecutionMapError
from fs2_serve.scientific_batch.kubernetes import (
    ACCELERATOR_COUNT_ANNOTATION,
    ACCELERATOR_RESOURCE_ANNOTATION,
    ATTEMPT_LABEL,
    CLUSTER_QUEUE_ANNOTATION,
    MANIFEST_ANNOTATION,
    ROUTE_NAMESPACE_ANNOTATION,
    WORKLOAD_NAMESPACE_ANNOTATION,
    HttpScientificBatchCluster,
    ScientificKubernetesError,
    _failure,
    _kueue_eviction,
)
from fs2_serve.scientific_batch.models import (
    COLLECTION_DEADLINE_MARGIN_SECONDS,
    COLLECTION_GRACE_SECONDS,
    AdapterExecutionPlan,
    ArtifactAccessContext,
    ArtifactMaterialization,
    AttemptArtifactCommit,
    BatchStatus,
    CheckpointMode,
    ExecutionMode,
    FailureKind,
    LifecyclePhase,
    MaterializationMode,
    PreemptionMode,
    ResourceClass,
    SchedulingAdmission,
    ScientificBatchPlan,
    ScientificBatchState,
    ScientificInputAdmission,
    ScientificInputArtifact,
    ScientificStagePlan,
    ServiceClass,
    StageExecutionBinding,
    StageInvocation,
    StageSchedulingDecision,
    StageVolumeBinding,
    VerifiedInputManifest,
    WorkloadKind,
    WorkloadObservation,
    WorkloadRef,
    WorkloadResource,
    WorkloadState,
)
from fs2_serve.scientific_batch.profile_catalog import (
    SCIENTIFIC_ARTIFACT_MANIFEST_SCHEMA,
    SCIENTIFIC_REQUEST_SCHEMA,
    SCIENTIFIC_RESULT_SCHEMA,
    ScientificProfileCatalog,
    ScientificWorkloadProfile,
)
from fs2_serve.scientific_batch.protocols import BatchRepositoryConflictError
from fs2_serve.scientific_batch.scheduling import SchedulingContractError, SchedulingContractResolver
from fs2_serve.scientific_batch.service import (
    ScientificBatchService,
    ScientificBatchStatusResponse,
    ScientificEventPage,
)
from fs2_serve.scientific_batch.worker import ScientificBatchWorker
from fs2_serve.scientific_batch.workload_routes import scientific_workload_artifact_router
from fs2_serve.scientific_input_uploads import ScientificInputUploadService
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


def profile_catalog_for(model_id: str) -> ScientificProfileCatalog:
    """The canonical profile fixture under one caller-chosen model identity."""

    def load(name: str) -> Draft202012Validator:
        return Draft202012Validator(json.loads((CATALOG_ROOT / "schema" / name).read_text()))

    profile = ScientificWorkloadProfile(MappingProxyType({**profile_value(), "model_id": model_id}))
    return ScientificProfileCatalog(
        profiles={profile.model_id: profile},
        validators={
            SCIENTIFIC_REQUEST_SCHEMA: load("scientific-run-request.schema.json"),
            SCIENTIFIC_RESULT_SCHEMA: load("scientific-run-result.schema.json"),
            SCIENTIFIC_ARTIFACT_MANIFEST_SCHEMA: load("scientific-artifact-manifest.schema.json"),
            PARAMETER_SCHEMA: Draft202012Validator(
                {"type": "object", "additionalProperties": False, "maxProperties": 0}
            ),
        },
    )


def profile_catalog() -> ScientificProfileCatalog:
    return profile_catalog_for("protein-design")


def scheduling() -> SchedulingContractResolver:
    return SchedulingContractResolver(
        {
            "schema": "fs2-serve.nebius.ai/kueue-scheduling/v1",
            "pool_node_label_key": "accelerator.fs2.nebius/pool-id",
            "service_classes": {
                "customer-batch": {
                    "workload_priority_class": "standard",
                    "priority": 0,
                    "default_local_queue": "scientific",
                    "preemption_mode": "restartable",
                    "pool_preference": ["h100-hot", "h100-preemptible"],
                    "max_queue_seconds": 600,
                    "max_execution_seconds": 3600,
                    "caller_selectable": True,
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
                    "spec": {
                        "resourceGroups": [
                            {
                                "coveredResources": ["nvidia.com/gpu"],
                                "flavors": [
                                    {"name": "h100-hot-flavor"},
                                    {"name": "h100-preemptible-flavor"},
                                ],
                            }
                        ]
                    },
                }
            },
            "workload_priority_classes": {"standard": {"value": 0}},
            "local_queue_routes": {
                "scientific": {
                    "namespace": "fs2-models",
                    "cluster_queue": "inference",
                    "model_ids": [],
                    "tenant_ids": [],
                    "service_classes": [],
                }
            },
            "model_eligible_pool_ids": {"protein-design": ["h100-preemptible"]},
            "cpu_classes": {},
            "cpu_stage_requests": {},
            "namespace_bound_models": {},
            "pools": {
                "h100-hot": {
                    "resource_flavor": "h100-hot-flavor",
                    "accelerator_resource_name": "nvidia.com/gpu",
                    "capacity": 8,
                },
                "h100-preemptible": {
                    "resource_flavor": "h100-preemptible-flavor",
                    "accelerator_resource_name": "nvidia.com/gpu",
                    "capacity": 2,
                },
            },
        }
    )


def test_controller_hashes_exact_mounted_scheduling_bytes_before_parse(tmp_path: Path) -> None:
    contract = json.loads(json.dumps(scheduling().contract))
    contract["service_classes"]["customer-batch"]["description"] = "café <queue> & λ"
    raw = json.dumps(contract, ensure_ascii=False, indent=2).encode("utf-8")
    path = tmp_path / "kueue-scheduling.json"
    path.write_bytes(raw)
    expected = hashlib.sha256(raw).hexdigest()

    resolver = SchedulingContractResolver.load(path, expected_sha256=expected)
    assert resolver.raw_contract_sha256 == f"sha256:{expected}"

    # These bytes decode to the same JSON value, but Terraform did not publish
    # them. Escaping or canonicalizing the mounted ConfigMap is therefore drift.
    path.write_bytes(json.dumps(contract, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
    with pytest.raises(SchedulingContractError, match="raw bytes differ from Terraform"):
        SchedulingContractResolver.load(path, expected_sha256=expected)


ACADEMIC_TENANT_ID = "tenant-academic"


def scheduling_with_academic_route(*, shadow: bool = False) -> dict[str, object]:
    academic_contract = json.loads((SOLUTION_ROOT / "academic-assets/contracts/academic-assets.json").read_text())
    execution = academic_contract["execution"]
    namespace = execution["namespace"]
    local_queue = execution["local_queue"]
    cluster_queue = execution["cluster_queue"]
    contract = json.loads(json.dumps(scheduling().contract))
    cluster_queue_manifest = json.loads(json.dumps(contract["cluster_queues"]["inference"]))
    cluster_queue_manifest["metadata"]["name"] = cluster_queue
    contract["cluster_queues"][cluster_queue] = cluster_queue_manifest
    contract["local_queues"][local_queue] = {
        "metadata": {"name": local_queue, "namespace": namespace},
        "spec": {"clusterQueue": cluster_queue},
    }
    contract["local_queue_routes"][local_queue] = {
        "namespace": namespace,
        "cluster_queue": cluster_queue,
        "model_ids": ["alphafold3", "bindcraft"],
        "tenant_ids": [ACADEMIC_TENANT_ID],
        "service_classes": ["customer-batch"],
    }
    contract["namespace_bound_models"] = {
        "alphafold3": namespace,
        "bindcraft": namespace,
    }
    contract["model_eligible_pool_ids"].update(
        {
            "alphafold3": ["h100-hot", "h100-preemptible"],
            "bindcraft": ["h100-hot", "h100-preemptible"],
        }
    )
    if shadow:
        contract["local_queues"]["academic-shadow"] = {
            "metadata": {"name": "academic-shadow", "namespace": "fs2-academic-shadow"},
            "spec": {"clusterQueue": cluster_queue},
        }
        contract["local_queue_routes"]["academic-shadow"] = {
            "namespace": "fs2-academic-shadow",
            "cluster_queue": cluster_queue,
            "model_ids": ["alphafold3"],
            "tenant_ids": [ACADEMIC_TENANT_ID],
            "service_classes": ["customer-batch"],
        }
    return contract


class FakeArtifactAccess:
    def __init__(self, pointer: dict[str, object]) -> None:
        self.pointer = pointer
        entry_id = uuid4()
        self.admission = ScientificInputAdmission(
            manifest=VerifiedInputManifest(
                manifest_id="request-inputs",
                manifest_artifact_id=UUID(str(pointer["artifact_id"])),
                manifest_digest=f"sha256:{pointer['sha256']}",
                entries=(
                    ScientificInputArtifact(
                        logical_artifact_id="request",
                        semantic_type="request/v1",
                        artifact_id=entry_id,
                        digest="sha256:" + "2" * 64,
                        size_bytes=10,
                        media_type="application/json",
                    ),
                ),
            ),
            access_context=ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="tenant-a"),
        )

    async def validate_input(self, pointer, *, tenant_id: str) -> ScientificInputAdmission:
        assert tenant_id == "tenant-a"
        assert pointer == self.pointer
        return self.admission

    async def artifact_response(self, artifact_id, *, tenant_id: str):
        return self.pointer

    async def result_response(self, operation_id, *, tenant_id: str):
        assert tenant_id == "tenant-a"
        result = json.loads((CATALOG_ROOT / "contracts/examples/scientific-run-result.example.json").read_text())
        result["operation_id"] = str(operation_id)
        result["batch_id"] = f"batch.{operation_id}"
        result["workload_id"] = f"workload.{operation_id}"
        return result


class FakeExecutionBinding:
    def access_context(self, profile, *, tenant_id: str) -> ArtifactAccessContext:
        assert profile.model_id == "protein-design"
        return ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id=tenant_id)

    def variant_id(self, model_id: str) -> str:
        assert model_id == "protein-design"
        return "protein-design-h100"

    def workload_namespace(self, model_id: str) -> str:
        assert model_id == "protein-design"
        return "fs2-models"

    def collector_id(self, model_id: str, stage_id: str) -> str:
        assert (model_id, stage_id) == ("protein-design", "design")
        return "protein-design-output-v1"

    def verify_runtime_artifacts(self, profile, execution_plan, access_context):
        del profile, execution_plan, access_context
        return ()

    def bind_runtime_artifacts(self, profile, execution_plan, access_context, localizations):
        del profile, access_context, localizations
        bound = replace(
            execution_plan,
            execution_map_sha256="sha256:" + "9" * 64,
            stage_bindings=tuple(
                StageExecutionBinding(
                    stage_id=stage.stage_id,
                    image="registry.example/protein@sha256:" + "a" * 64,
                    collector_id=self.collector_id(execution_plan.model_id, stage.stage_id),
                    validator_id="protein-design-v1",
                    mounts=(
                        StageVolumeBinding(
                            name="artifact-workspace",
                            kind="artifact-workspace",
                            claim_name=None,
                            host_path=None,
                            mount_path="/mnt/fs2-scientific",
                            sub_path=None,
                            read_only=False,
                        ),
                    ),
                    service_account_name="scientific-runner",
                    request_cpu="4",
                    request_memory="32Gi",
                    request_ephemeral_storage="100Gi",
                    limit_cpu="4",
                    limit_memory="32Gi",
                    limit_ephemeral_storage="100Gi",
                    active_deadline_seconds=3600,
                    termination_grace_seconds=60,
                    environment=(),
                    required_node_labels=(),
                )
                for stage in execution_plan.controller_plan.stages
            ),
        )
        bound.assert_controller_bound()
        return bound


class FakePlanFactory:
    def plan(
        self,
        profile,
        request,
        *,
        operation_id=None,
        access_context,
        input_artifacts,
    ) -> AdapterExecutionPlan:
        del request, access_context
        controller_plan = ScientificBatchPlan((ScientificStagePlan(stage_id="design", max_attempts=2),))
        logical_input = input_artifacts[0].logical_artifact_id
        invocation = StageInvocation(
            stage_id="design",
            shard_id="main",
            argv=("protein-design", "run", "--input", "/mnt/fs2-scientific/work/design/main/input.json"),
            environment=(),
            working_directory="/mnt/fs2-scientific/work/design/main",
            consumes=(logical_input,),
            produces="design-result",
            collector_id="protein-design-output-v1",
            validator_id="protein-design-v1",
            handoff_name=None,
            materializations=(
                ArtifactMaterialization(
                    artifact_id=logical_input,
                    destination="/mnt/fs2-scientific/work/design/main/input.json",
                    mode=MaterializationMode.COPY_FILE,
                ),
            ),
        )
        return AdapterExecutionPlan(
            model_id=profile.model_id,
            variant_id="protein-design-h100",
            source_revision=profile.model_revision,
            request_sha256=hashlib.sha256(str(operation_id).encode()).hexdigest(),
            controller_plan=controller_plan,
            invocations=(invocation,),
            required_model_artifacts=(),
        )


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
        execution_binding=FakeExecutionBinding(),
        plan_factory=FakePlanFactory(),
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


class _MemoryInputUploadPort:
    """Give the isolated artifact fake the operation anchor PostgreSQL supplies."""

    def __init__(self, repository, service) -> None:
        self.repository = repository
        self.service = service

    async def open_attempt(self, request):
        await self.repository.register_operation(request.operation_id, tenant_id=request.tenant_id)
        return await self.service.open_attempt(request)

    def __getattr__(self, name):
        return getattr(self.service, name)


class _MemoryArtifactContentReader:
    def __init__(self, repository, object_store) -> None:
        self.repository = repository
        self.object_store = object_store

    async def read(self, artifact_id, *, tenant_id, maximum_bytes):
        artifact = await self.repository.get_artifact(artifact_id, tenant_id=tenant_id)
        value = self.object_store.objects[artifact.storage_key][0]
        assert len(value) <= maximum_bytes
        return value


@pytest.mark.asyncio
async def test_customer_upload_is_idempotent_verified_downloadable_and_never_worker_claimed(
    registry, cipher, hasher
) -> None:
    runtime, _, batch_repository, _, _ = scientific_runtime(registry, cipher, hasher)
    object_store = FakeObjectStore(clock=lambda: datetime.now(UTC))
    artifact_repository = MemoryArtifactRepository()
    artifact_service = ScientificArtifactService(
        repository=artifact_repository,
        object_store=object_store,
        allowed_media_types=ALLOWED_MEDIA_TYPES,
        clock=lambda: datetime.now(UTC),
    )
    upload_port = _MemoryInputUploadPort(artifact_repository, artifact_service)
    runtime.artifact_service = artifact_service
    runtime.scientific_input_uploads = ScientificInputUploadService(
        store=runtime.store,
        artifacts=upload_port,
        profiles=profile_catalog(),
    )
    issued = await runtime.tokens.issue(
        TokenCreate(
            principal_id="scientist-upload",
            tenant_id="tenant-a",
            scopes={Scope.INFERENCE_INVOKE, Scope.OPERATIONS_READ, Scope.OPERATIONS_RESULT, Scope.MCP_INVOKE},
            models={"protein-design"},
            max_concurrency=4,
        ),
        created_by="test",
    )
    payload = b">target\nMKT"
    request = {
        "model_id": "protein-design",
        "sha256": digest(payload).removeprefix("sha256:"),
        "size_bytes": len(payload),
        "media_type": "text/x-fasta",
    }
    app = create_app(runtime)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://inference.test.invalid",
            headers={"authorization": f"Bearer {issued.token}"},
        ) as client:
            begun = await client.post(
                "/v1/scientific-artifacts/uploads",
                headers={"idempotency-key": "input-upload-0001"},
                json=request,
            )
            assert begun.status_code == 201, begun.text
            replay = await client.post(
                "/v1/scientific-artifacts/uploads",
                headers={"idempotency-key": "input-upload-0001"},
                json=request,
            )
            assert replay.status_code == 201
            assert replay.json()["operation_id"] == begun.json()["operation_id"]
            assert replay.json()["upload_id"] == begun.json()["upload_id"]
            assert await runtime.store.claim_operation("generic-worker", lease_seconds=30) is None

            operation_id = UUID(begun.json()["operation_id"])
            upload_id = UUID(begun.json()["upload_id"])
            intent = await artifact_repository.get_upload(
                FinalizeArtifactUpload(upload_id=upload_id, operation_id=operation_id, tenant_id="tenant-a")
            )
            object_store.put(intent.storage_key, payload, "text/x-fasta")
            finalized = await client.post(
                f"/v1/scientific-artifacts/uploads/{upload_id}:finalize",
                json={"operation_id": str(operation_id)},
            )
            assert finalized.status_code == 200, finalized.text
            pointer = finalized.json()
            assert pointer["sha256"] == request["sha256"]
            operation = await client.get(f"/v1/operations/{operation_id}")
            assert operation.json()["status"] == "succeeded"
            assert operation.json()["outcome"] == "artifact_uploaded"
            download = await client.get(f"/v1/artifacts/{pointer['artifact_id']}/download")
            assert download.status_code == 200
            assert download.json()["handle"]["method"] == "GET"
            assert "storage_key" not in download.text

            manifest = json.dumps(
                {
                    "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
                    "manifest_id": "customer-input-0001",
                    "entries": [
                        {
                            "name": "target-sequence",
                            "semantic_type": "protein.sequence/v1",
                            "artifact": pointer,
                        }
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            manifest_begin = await client.post(
                "/v1/scientific-artifacts/uploads",
                headers={"idempotency-key": "input-manifest-0001"},
                json={
                    "model_id": "protein-design",
                    "sha256": digest(manifest).removeprefix("sha256:"),
                    "size_bytes": len(manifest),
                    "media_type": "application/vnd.fs2.scientific-manifest+json",
                },
            )
            assert manifest_begin.status_code == 201
            manifest_operation_id = UUID(manifest_begin.json()["operation_id"])
            manifest_upload_id = UUID(manifest_begin.json()["upload_id"])
            manifest_intent = await artifact_repository.get_upload(
                FinalizeArtifactUpload(
                    upload_id=manifest_upload_id,
                    operation_id=manifest_operation_id,
                    tenant_id="tenant-a",
                )
            )
            object_store.put(
                manifest_intent.storage_key,
                manifest,
                "application/vnd.fs2.scientific-manifest+json",
            )
            manifest_final = await client.post(
                f"/v1/scientific-artifacts/uploads/{manifest_upload_id}:finalize",
                json={"operation_id": str(manifest_operation_id)},
            )
            assert manifest_final.status_code == 200
            manifest_pointer = manifest_final.json()
            assert runtime.scientific_batches is not None
            runtime.scientific_batches.artifacts = ArtifactServiceBridge(
                artifacts=artifact_repository,
                batches=batch_repository,
                profiles=runtime.scientific_batches.profiles,
                store=runtime.store,
                content_reader=_MemoryArtifactContentReader(artifact_repository, object_store),
                service=artifact_service,
            )
            submitted = await client.post(
                "/v1/models/protein-design:submit",
                headers={"idempotency-key": "uploaded-submit-0001"},
                json={
                    "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
                    "operation": "design",
                    "service_class": "customer-batch",
                    "input_manifest": manifest_pointer,
                    "parameters": {},
                },
            )
            assert submitted.status_code == 202, submitted.text
            assert submitted.json()["batch"]["input_artifact_id"] == manifest_pointer["artifact_id"]

            conflict = await client.post(
                "/v1/scientific-artifacts/uploads",
                headers={"idempotency-key": "input-upload-0001"},
                json={**request, "size_bytes": len(payload) + 1},
            )
            assert conflict.status_code == 409

    mcp_payload = b'{"sequence":"MKT"}'
    server = build_mcp_server(runtime)
    access = await PATTokenVerifier(runtime).verify_token(issued.token)
    assert access is not None
    context = Context(mcp_server=server, subscriptions=server._subscriptions)  # type: ignore[attr-defined]
    auth_token = auth_context_var.set(AuthenticatedUser(access))
    try:
        mcp_begin = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "begin_scientific_artifact_upload",
            {
                "model_id": "protein-design",
                "sha256": digest(mcp_payload).removeprefix("sha256:"),
                "size_bytes": len(mcp_payload),
                "media_type": "application/json",
                "idempotency_key": "mcp-input-upload-0001",
            },
            context,
            convert_result=False,
        )
        mcp_operation_id = UUID(mcp_begin["operation_id"])
        mcp_upload_id = UUID(mcp_begin["upload_id"])
        mcp_intent = await artifact_repository.get_upload(
            FinalizeArtifactUpload(
                upload_id=mcp_upload_id,
                operation_id=mcp_operation_id,
                tenant_id="tenant-a",
            )
        )
        object_store.put(mcp_intent.storage_key, mcp_payload, "application/json")
        mcp_pointer = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "finalize_scientific_artifact_upload",
            {"operation_id": str(mcp_operation_id), "upload_id": str(mcp_upload_id)},
            context,
            convert_result=False,
        )
        mcp_download = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "download_scientific_artifact",
            {"artifact_id": mcp_pointer["artifact_id"]},
            context,
            convert_result=False,
        )
        assert mcp_download["artifact"] == mcp_pointer
        assert mcp_download["handle"]["method"] == "GET"
    finally:
        auth_context_var.reset(auth_token)


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
        execution_binding=FakeExecutionBinding(),
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
            before_admission = await client.get(f"/v1/operations/{operation_id}")
            assert before_admission.status_code == 200
            assert before_admission.json()["batch"]["stages"][0]["attempts"][0]["scheduling_admission"] is None
            admitted_at = datetime.now(UTC)
            cluster.set_observation(
                attempt.workload,
                WorkloadObservation(
                    ref=attempt.workload,
                    attempt_id=attempt.attempt_id,
                    state=WorkloadState.SUCCEEDED,
                    phases=(LifecyclePhase.ADMITTED, LifecyclePhase.ACTIVE_COMPUTE),
                    scheduling_admission=SchedulingAdmission(
                        resolved_pool_id="h100-preemptible",
                        admitted_resource_flavor="inference-h100-1x",
                        accelerator_resource_name="nvidia.com/gpu",
                        accelerator_count=1,
                        admitted_at=admitted_at,
                    ),
                ),
            )
            repository.put_commit(
                AttemptArtifactCommit(
                    operation_id=operation_id,
                    stage_id="design",
                    attempt_ids=(attempt.attempt_id,),
                    logical_artifact_id="design-result",
                    handoff_artifact_id=uuid4(),
                    handoff_digest="sha256:" + "1" * 64,
                    handoff_size_bytes=10,
                    handoff_media_type="application/json",
                    handoff_compression=None,
                    manifest_artifact_id=uuid4(),
                    validation_artifact_id=uuid4(),
                    manifest_digest="sha256:" + "2" * 64,
                    validation_digest="sha256:" + "3" * 64,
                    committed_at=datetime.now(UTC),
                    validated_at=datetime.now(UTC),
                    semantic_valid=True,
                    collector_id="protein-design-output-v1",
                    validator_id="protein-design-v1",
                )
            )
            for _ in range(5):
                await controller.reconcile_once()

            status = await client.get(f"/v1/operations/{operation_id}")
            events = await client.get(f"/v1/operations/{operation_id}/events")
            artifact = await client.get(f"/v1/artifacts/{pointer['artifact_id']}")
            result = await client.get(f"/v1/operations/{operation_id}/result")
            assert status.status_code == events.status_code == artifact.status_code == result.status_code == 200
            assert status.json()["batch"]["status"] == "succeeded"
            assert status.json()["batch"]["variant_id"] == "protein-design-h100"
            actual_admission = status.json()["batch"]["stages"][0]["attempts"][0]["scheduling_admission"]
            assert actual_admission == {
                "resolved_pool_id": "h100-preemptible",
                "admitted_resource_flavor": "inference-h100-1x",
                "accelerator_resource_name": "nvidia.com/gpu",
                "accelerator_count": 1,
                "admitted_at": admitted_at.isoformat().replace("+00:00", "Z"),
            }
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
        tenant_id="tenant-a",
        profile=profile_value(),
        plan=plan,
        captured_at=datetime(2026, 9, 2, tzinfo=UTC),
    )
    assert snapshot.workload_namespace == "fs2-models"
    state = ScientificBatchState.admit(
        operation_id=uuid4(),
        tenant_id="tenant-a",
        model_id="protein-design",
        variant_id="protein-design-h100",
        input_artifact_id=uuid4(),
        plan=plan,
        scheduling=snapshot,
    )
    value = state_to_value(state)
    frozen_stage = value["scheduling"]["stages"][0]
    assert frozen_stage["resource_class"] == "gpu"
    assert "admitted_resource_flavor" not in frozen_stage
    assert state_from_value(value) == state
    value["cancel_requested"] = "false"
    with pytest.raises(ValueError, match="not a boolean"):
        state_from_value(value)


@pytest.mark.parametrize(
    ("fixture_name", "expected_sha256"),
    [
        ("scientific-batch-state-v6-ec3440a2.json", "06083938ee23816213b3d171fc5d7f4adfff262fd0e661515f12fa7f585605fe"),
        ("scientific-batch-state-v7-545d71d9.json", "4c1b6cb1e6db219b4fcbf71d9c118e00ac4c885c2d7a0268936c11c6bf662715"),
    ],
)
def test_internal_state_codec_reads_v6_and_v7_and_every_write_upgrades_to_v8(
    fixture_name: str, expected_sha256: str
) -> None:
    fixture_path = Path(__file__).parent / "fixtures" / fixture_name
    encoded = fixture_path.read_bytes()
    assert hashlib.sha256(encoded).hexdigest() == expected_sha256
    legacy = json.loads(encoded)
    decoded = state_from_value(legacy)
    assert decoded.scheduling.workload_namespace == "fs2-models"
    assert decoded.scheduling.route_namespace == "fs2-models"
    assert decoded.scheduling.stages[0].resource_class is ResourceClass.GPU
    assert decoded.execution_plan is not None
    assert decoded.execution_plan.invocations[0].runtime_mounts[0].expected_manifest_sha256 is None
    upgraded = state_to_value(decoded)
    assert upgraded["schema_version"] == "fs2-serve.nebius.ai/scientific-batch-state/v8"
    assert upgraded["scheduling"]["workload_namespace"] == "fs2-models"
    assert upgraded["scheduling"]["route_namespace"] == "fs2-models"
    assert upgraded["scheduling"]["stages"][0]["resource_class"] == "gpu"
    assert "admitted_resource_flavor" not in upgraded["scheduling"]["stages"][0]
    assert upgraded["adapter_execution"]["invocations"][0]["runtime_mounts"][0]["expected_manifest_sha256"] is None


def test_scientific_status_and_event_transport_models_are_closed() -> None:
    for model in (ScientificBatchStatusResponse, ScientificEventPage):
        schema = model.model_json_schema()
        object_schemas = [schema, *schema.get("$defs", {}).values()]
        for candidate in object_schemas:
            if candidate.get("type") == "object":
                assert candidate.get("additionalProperties") is False

    status_schema = ScientificBatchStatusResponse.model_json_schema()
    admission = status_schema["$defs"]["SchedulingAdmission"]
    assert admission["additionalProperties"] is False
    assert set(admission["properties"]) == {
        "resolved_pool_id",
        "admitted_resource_flavor",
        "accelerator_resource_name",
        "accelerator_count",
        "admitted_at",
    }


def test_scheduling_resolver_enforces_canonical_tenant_route_and_priority() -> None:
    batch_plan = ScientificBatchPlan((ScientificStagePlan(stage_id="design"),))
    tenant_resolver = scheduling()
    tenant_route = tenant_resolver.local_queue_routes["scientific"]
    assert isinstance(tenant_route, dict)
    tenant_route["tenant_ids"] = ["tenant-b"]
    with pytest.raises(SchedulingContractError, match="fallback LocalQueue is not explicitly unrestricted"):
        tenant_resolver.freeze(
            service_class="customer-batch",
            model_id="protein-design",
            tenant_id="tenant-a",
            profile=profile_value(),
            plan=batch_plan,
        )

    priority_resolver = scheduling()
    priority = priority_resolver.priority_classes["standard"]
    assert isinstance(priority, dict)
    priority["value"] = 1
    with pytest.raises(SchedulingContractError, match="PriorityClass value is inconsistent"):
        priority_resolver.freeze(
            service_class="customer-batch",
            model_id="protein-design",
            tenant_id="tenant-a",
            profile=profile_value(),
            plan=batch_plan,
        )


@pytest.mark.parametrize("model_id", ["alphafold3", "bindcraft"])
def test_scheduling_resolver_freezes_academic_execution_and_localqueue_namespace(model_id: str) -> None:
    resolver = SchedulingContractResolver(scheduling_with_academic_route())
    batch_plan = ScientificBatchPlan((ScientificStagePlan(stage_id="design"),))
    academic_profile = profile_value()
    academic_profile["model_id"] = model_id
    frozen = resolver.freeze(
        service_class="customer-batch",
        model_id=model_id,
        tenant_id=ACADEMIC_TENANT_ID,
        profile=academic_profile,
        plan=batch_plan,
        workload_namespace="fs2-academic-poc",
    )
    assert frozen.workload_namespace == "fs2-academic-poc"
    assert frozen.route_namespace == "fs2-academic-poc"
    assert frozen.stage("design").resolved_local_queue == "academic-scientific"
    assert frozen.stage("design").resolved_cluster_queue == "inference-accelerators"

    with pytest.raises(SchedulingContractError, match="execution namespace differs"):
        resolver.freeze(
            service_class="customer-batch",
            model_id=model_id,
            tenant_id=ACADEMIC_TENANT_ID,
            profile=academic_profile,
            plan=batch_plan,
            workload_namespace="fs2-models",
        )

    with pytest.raises(SchedulingContractError, match="execution namespace differs"):
        resolver.freeze(
            service_class="customer-batch",
            model_id=model_id,
            tenant_id="another-tenant",
            profile=academic_profile,
            plan=batch_plan,
            workload_namespace="fs2-academic-poc",
        )


def test_scheduling_resolver_rejects_ambiguous_model_tenant_execution_namespaces() -> None:
    resolver = SchedulingContractResolver(scheduling_with_academic_route(shadow=True))
    academic_profile = profile_value()
    academic_profile["model_id"] = "alphafold3"

    with pytest.raises(SchedulingContractError, match="multiple Kueue LocalQueues"):
        resolver.freeze(
            service_class="customer-batch",
            model_id="alphafold3",
            tenant_id=ACADEMIC_TENANT_ID,
            profile=academic_profile,
            plan=ScientificBatchPlan((ScientificStagePlan(stage_id="design"),)),
            workload_namespace="fs2-academic-poc",
        )


def test_scheduling_resolver_prefers_exact_route_and_fails_closed_for_bound_model() -> None:
    contract = scheduling_with_academic_route()
    profile = profile_value()
    profile["model_id"] = "alphafold3"
    plan = ScientificBatchPlan((ScientificStagePlan(stage_id="design"),))
    frozen = SchedulingContractResolver(contract).freeze(
        service_class="customer-batch",
        model_id="alphafold3",
        tenant_id=ACADEMIC_TENANT_ID,
        profile=profile,
        plan=plan,
        workload_namespace="fs2-academic-poc",
    )
    assert frozen.stage("design").resolved_local_queue == "academic-scientific"

    with pytest.raises(SchedulingContractError, match="namespace-bound model resolved outside"):
        SchedulingContractResolver(contract).freeze(
            service_class="customer-batch",
            model_id="alphafold3",
            tenant_id="unmatched-tenant",
            profile=profile,
            plan=plan,
        )


def test_scheduling_route_specificity_is_explicit_and_selector_bypasses_fail_closed() -> None:
    contract = json.loads(json.dumps(scheduling().contract))

    def add_route(
        name: str,
        *,
        tenant_ids: list[str] | None = None,
        model_ids: list[str] | None = None,
        service_classes: list[str] | None = None,
    ) -> None:
        contract["local_queues"][name] = {
            "metadata": {"name": name, "namespace": "fs2-models"},
            "spec": {"clusterQueue": "inference"},
        }
        contract["local_queue_routes"][name] = {
            "namespace": "fs2-models",
            "cluster_queue": "inference",
            "tenant_ids": tenant_ids or [],
            "model_ids": model_ids or [],
            "service_classes": service_classes or [],
        }

    add_route("tenant-only", tenant_ids=["tenant-a"])
    add_route("tenant-model", tenant_ids=["tenant-a"], model_ids=["protein-design"])
    add_route("model-class", model_ids=["protein-design"], service_classes=["customer-batch"])
    add_route(
        "tenant-model-class",
        tenant_ids=["tenant-a"],
        model_ids=["protein-design"],
        service_classes=["customer-batch"],
    )
    resolver = SchedulingContractResolver(contract)
    args = {
        "service_class": "customer-batch",
        "model_id": "protein-design",
        "tenant_id": "tenant-a",
        "default_local_queue": "scientific",
        "desired_local_queue": None,
    }
    assert resolver._resolve_route(**args)[0] == "tenant-model-class"

    del contract["local_queues"]["tenant-model-class"]
    del contract["local_queue_routes"]["tenant-model-class"]
    resolver = SchedulingContractResolver(contract)
    # Both remaining candidates constrain two dimensions.  The tenant-bound
    # lane wins rather than becoming ambiguous with a cross-tenant lane.
    assert resolver._resolve_route(**args)[0] == "tenant-model"

    del contract["local_queues"]["tenant-model"]
    del contract["local_queue_routes"]["tenant-model"]
    resolver = SchedulingContractResolver(contract)
    # More constrained model+class routing beats tenant-only routing.
    assert resolver._resolve_route(**args)[0] == "model-class"

    # Selectors for another class or tenant never act as wildcard bypasses.
    bypass_args = dict(args)
    bypass_args["tenant_id"] = "tenant-b"
    bypass_args["service_class"] = "interactive"
    assert resolver._resolve_route(**bypass_args)[0] == "scientific"

    add_route("model-class-shadow", model_ids=["protein-design"], service_classes=["customer-batch"])
    resolver = SchedulingContractResolver(contract)
    with pytest.raises(SchedulingContractError, match="multiple Kueue LocalQueues"):
        resolver._resolve_route(**args)


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
        tenant_id="tenant-a",
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
        variant_id="protein-design-h100",
        input_artifact_id=uuid4(),
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
            assert body["metadata"]["labels"]["kueue.x-k8s.io/max-exec-time-seconds"] == "3600"
            assert body["metadata"]["annotations"]["fs2.nebius.ai/max-queue-seconds"] == "600"
            assert body["metadata"]["annotations"]["fs2.nebius.ai/pool-preference"] == ("h100-preemptible")
            assert body["metadata"]["annotations"]["fs2.nebius.ai/preemption-mode"] == "restartable"
            assert body["metadata"]["annotations"][ACCELERATOR_RESOURCE_ANNOTATION] == "nvidia.com/gpu"
            assert body["metadata"]["annotations"][ACCELERATOR_COUNT_ANNOTATION] == "1"
            assert body["spec"]["suspend"] is True and body["spec"]["backoffLimit"] == 0
            assert body["spec"]["activeDeadlineSeconds"] == 3600
            assert body["spec"]["template"]["metadata"]["labels"][ATTEMPT_LABEL] == str(attempt_id)
            pool_expression = body["spec"]["template"]["spec"]["affinity"]["nodeAffinity"][
                "requiredDuringSchedulingIgnoredDuringExecution"
            ]["nodeSelectorTerms"][0]["matchExpressions"][-1]
            assert pool_expression == {
                "key": "accelerator.fs2.nebius/pool-id",
                "operator": "In",
                "values": ["h100-preemptible"],
            }
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
                        "annotations": {
                            CLUSTER_QUEUE_ANNOTATION: "inference",
                            ACCELERATOR_RESOURCE_ANNOTATION: "nvidia.com/gpu",
                            ACCELERATOR_COUNT_ANNOTATION: "1",
                            WORKLOAD_NAMESPACE_ANNOTATION: "fs2-models",
                            ROUTE_NAMESPACE_ANNOTATION: "fs2-models",
                        },
                    },
                    "status": {"succeeded": 1, "startTime": "2026-09-02T20:00:00Z"},
                },
            )
        if request.url.path.endswith("/workloads"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"uid": "kueue-workload-uid"},
                            "status": {
                                "conditions": [
                                    {
                                        "type": "QuotaReserved",
                                        "status": "True",
                                        "lastTransitionTime": "2026-09-02T19:59:00Z",
                                    },
                                    {
                                        "type": "Admitted",
                                        "status": "True",
                                        "lastTransitionTime": "2026-09-02T20:00:00Z",
                                    },
                                ],
                                "admission": {
                                    "clusterQueue": "inference",
                                    "podSetAssignments": [
                                        {
                                            "flavors": {"nvidia.com/gpu": "inference-h100-1x"},
                                            "resourceUsage": {"nvidia.com/gpu": "1"},
                                        }
                                    ],
                                },
                            },
                        }
                    ]
                },
            )
        if request.url.path.endswith("/resourceflavors/inference-h100-1x"):
            return httpx.Response(
                200,
                json={"metadata": {"labels": {"accelerator.fs2.nebius/pool-id": "h100-preemptible"}}},
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
    observation = await cluster.observe(ref, scheduling=resource.scheduling)
    assert ref.uid == "job-uid"
    assert observation.attempt_id == attempt_id
    assert observation.state.value == "succeeded"
    assert observation.scheduling_admission == SchedulingAdmission(
        resolved_pool_id="h100-preemptible",
        admitted_resource_flavor="inference-h100-1x",
        accelerator_resource_name="nvidia.com/gpu",
        accelerator_count=1,
        quota_reserved_at=datetime(2026, 9, 2, 19, 59, tzinfo=UTC),
        admitted_at=datetime(2026, 9, 2, 20, 0, tzinfo=UTC),
    )
    assert observation.kueue_workload_uid == "kueue-workload-uid"
    assert fence.calls == [(operation_id, "controller-a", 7)]
    assert [request.method for request in requests] == ["POST", "GET", "GET", "GET", "GET"]
    await client.aclose()


@pytest.mark.asyncio
async def test_kueue_admission_rejects_resource_flavor_with_only_retired_pool_label(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/workloads"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"uid": "kueue-workload-uid"},
                            "status": {
                                "conditions": [
                                    {
                                        "type": "QuotaReserved",
                                        "status": "True",
                                        "lastTransitionTime": "2026-09-02T19:59:00Z",
                                    }
                                ],
                                "admission": {
                                    "clusterQueue": "inference",
                                    "podSetAssignments": [
                                        {
                                            "flavors": {"nvidia.com/gpu": "inference-h100-1x"},
                                            "resourceUsage": {"nvidia.com/gpu": "1"},
                                        }
                                    ],
                                },
                            },
                        }
                    ]
                },
            )
        if request.url.path.endswith("/resourceflavors/inference-h100-1x"):
            return httpx.Response(
                200,
                json={"metadata": {"labels": {"fs2.nebius.ai/pool-id": "h100-preemptible"}}},
            )
        raise AssertionError(f"unexpected Kubernetes request: {request.method} {request.url}")

    token = tmp_path / "token"
    token.write_text("x" * 32)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.test")
    cluster = HttpScientificBatchCluster(
        base_url="https://kubernetes.test",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        controller_id="controller-a",
        fence=Fence(),
        renderer=JobRenderer(),
        writes_enabled=True,
        client=client,
    )

    with pytest.raises(ScientificKubernetesError, match="no canonical pool identity"):
        await cluster._scheduling_admission(
            WorkloadRef(
                namespace="fs2-models",
                route_namespace="fs2-models",
                name="scientific-job",
                kind=WorkloadKind.JOB,
                uid="job-uid",
            ),
            "job-uid",
            expected_cluster_queue="inference",
            expected_pool_preference=("h100-preemptible",),
            expected_accelerator_resource="nvidia.com/gpu",
            expected_accelerator_count=1,
            expected_resource_flavor=None,
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_kueue_cpu_admission_captures_exact_flavor_pool_quantities_and_two_clocks(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/workloads"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"uid": "cpu-kueue-workload-uid"},
                            "status": {
                                "conditions": [
                                    {
                                        "type": "QuotaReserved",
                                        "status": "True",
                                        "lastTransitionTime": "2026-09-03T09:00:00Z",
                                    },
                                    {
                                        "type": "Admitted",
                                        "status": "True",
                                        "lastTransitionTime": "2026-09-03T09:00:02Z",
                                    },
                                ],
                                "admission": {
                                    "clusterQueue": "reference-data-cpu",
                                    "podSetAssignments": [
                                        {
                                            "name": "main",
                                            "flavors": {
                                                "cpu": "reference-data-cpu",
                                                "memory": "reference-data-cpu",
                                            },
                                            "resourceUsage": {"cpu": "16100m", "memory": "65792Mi"},
                                        }
                                    ],
                                },
                            },
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected Kubernetes request: {request.method} {request.url}")

    token = tmp_path / "cpu-token"
    token.write_text("x" * 32)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.test")
    cluster = HttpScientificBatchCluster(
        base_url="https://kubernetes.test",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        controller_id="controller-a",
        fence=Fence(),
        renderer=JobRenderer(),
        writes_enabled=True,
        client=client,
    )
    admission, uid, admitted, reason = await cluster._scheduling_admission(
        WorkloadRef(
            namespace="fs2-academic-poc",
            route_namespace="fs2-academic-poc",
            name="af3-data-pipeline",
            kind=WorkloadKind.JOB,
            uid="cpu-job-uid",
        ),
        "cpu-job-uid",
        expected_cluster_queue="reference-data-cpu",
        expected_pool_preference=("reference-cpu",),
        expected_accelerator_resource=None,
        expected_accelerator_count=0,
        expected_resource_flavor="reference-data-cpu",
    )
    assert uid == "cpu-kueue-workload-uid" and admitted and reason is None
    assert admission == SchedulingAdmission(
        resolved_pool_id="reference-cpu",
        admitted_resource_flavor="reference-data-cpu",
        accelerator_resource_name=None,
        accelerator_count=0,
        quota_reserved_at=datetime(2026, 9, 3, 9, 0, tzinfo=UTC),
        admitted_at=datetime(2026, 9, 3, 9, 0, 2, tzinfo=UTC),
        cpu_millis=16_100,
        memory_bytes=65_792 * 1024**2,
    )
    await client.aclose()


@pytest.mark.asyncio
async def test_academic_namespace_is_frozen_and_used_for_apply_observe_delete_and_result(tmp_path: Path) -> None:
    model_profile = profile_value()
    model_profile["model_id"] = "alphafold3"
    model_profile["workload"]["stages"][0]["id"] = "inference"
    plan = ScientificBatchPlan((ScientificStagePlan(stage_id="inference"),))
    snapshot = SchedulingContractResolver(scheduling_with_academic_route()).freeze(
        service_class="customer-batch",
        model_id="alphafold3",
        tenant_id=ACADEMIC_TENANT_ID,
        profile=model_profile,
        plan=plan,
        workload_namespace="fs2-academic-poc",
        captured_at=datetime(2026, 9, 2, 19, 58, tzinfo=UTC),
    )
    assert snapshot.tenant_queue == "academic-scientific"
    assert snapshot.workload_namespace == snapshot.route_namespace == "fs2-academic-poc"

    operation_id = uuid4()
    attempt_id = uuid4()
    resource = WorkloadResource(
        operation_id=operation_id,
        batch_id=uuid4(),
        workload_id=uuid4(),
        attempt_id=attempt_id,
        stage_id="inference",
        shard_id="main",
        attempt_number=1,
        tenant_id=ACADEMIC_TENANT_ID,
        model_id="alphafold3",
        variant_id="upstream-v3-0-4",
        input_artifact_id=uuid4(),
        service_class=ServiceClass.CUSTOMER_BATCH,
        scheduling_snapshot_digest=snapshot.digest,
        namespace=snapshot.workload_namespace,
        route_namespace=snapshot.route_namespace,
        name="scientific-af3-inference",
        kind=WorkloadKind.JOB,
        scheduling=snapshot.stage("inference"),
    )
    rendered: dict[str, object] = {}
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            assert request.url.path == "/apis/batch/v1/namespaces/fs2-academic-poc/jobs"
            body = json.loads(request.content)
            assert body["metadata"]["namespace"] == "fs2-academic-poc"
            assert body["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "academic-scientific"
            body["metadata"]["uid"] = "academic-job-uid"
            rendered.update(body)
            return httpx.Response(201, json=body)
        if request.method == "DELETE":
            assert request.url.path == "/apis/batch/v1/namespaces/fs2-academic-poc/jobs/scientific-af3-inference"
            return httpx.Response(202, json={"status": "Success"})
        if request.url.path.endswith("/workloads"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"uid": "academic-kueue-uid"},
                            "status": {
                                "conditions": [
                                    {
                                        "type": "QuotaReserved",
                                        "status": "True",
                                        "lastTransitionTime": "2026-09-02T19:59:00Z",
                                    },
                                    {
                                        "type": "Admitted",
                                        "status": "True",
                                        "lastTransitionTime": "2026-09-02T20:00:00Z",
                                    },
                                ],
                                "admission": {
                                    "clusterQueue": "inference-accelerators",
                                    "podSetAssignments": [
                                        {
                                            "flavors": {"nvidia.com/gpu": "inference-h100-1x"},
                                            "resourceUsage": {"nvidia.com/gpu": "1"},
                                        }
                                    ],
                                },
                            },
                        }
                    ]
                },
            )
        if request.url.path.endswith("/resourceflavors/inference-h100-1x"):
            return httpx.Response(
                200,
                json={"metadata": {"labels": {"accelerator.fs2.nebius/pool-id": "h100-preemptible"}}},
            )
        if request.url.path.endswith("/scientific-af3-inference"):
            body = json.loads(json.dumps(rendered))
            body["status"] = {"succeeded": 1, "startTime": "2026-09-02T20:00:00Z"}
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"items": []})

    token = tmp_path / "academic-token"
    token.write_text("x" * 32)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.test")
    cluster = HttpScientificBatchCluster(
        base_url="https://kubernetes.test",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        controller_id="controller-a",
        fence=Fence(),
        renderer=JobRenderer(),
        writes_enabled=True,
        client=client,
    )
    ref = await cluster.apply(resource, controller_fence=11)
    observation = await cluster.observe(ref, scheduling=resource.scheduling)
    await cluster.delete(ref, controller_fence=11)
    assert observation.state is WorkloadState.SUCCEEDED
    assert observation.kueue_workload_uid == "academic-kueue-uid"
    assert all(
        "/namespaces/fs2-academic-poc/" in request.url.path
        for request in requests
        if "/namespaces/" in request.url.path
    )
    assert not any("/namespaces/fs2-models/" in request.url.path for request in requests)
    await client.aclose()

    state = ScientificBatchState.admit(
        operation_id=operation_id,
        tenant_id=ACADEMIC_TENANT_ID,
        model_id="alphafold3",
        variant_id="upstream-v3-0-4",
        input_artifact_id=uuid4(),
        plan=plan,
        scheduling=snapshot,
    )
    decoded = state_from_value(state_to_value(state))
    assert decoded.scheduling.workload_namespace == decoded.scheduling.route_namespace == "fs2-academic-poc"
    result_snapshot = ArtifactServiceBridge._scheduling_snapshot(decoded)
    assert "workload_namespace" not in result_snapshot
    assert "route_namespace" not in result_snapshot
    assert result_snapshot["stages"][0]["resolved_local_queue"] == "academic-scientific"


@pytest.mark.asyncio
async def test_kubernetes_observer_persists_quota_reservation_without_claiming_admitted(
    tmp_path: Path,
) -> None:
    attempt_id = uuid4()
    decision = (
        scheduling()
        .freeze(
            service_class="customer-batch",
            model_id="protein-design",
            tenant_id="tenant-a",
            profile=profile_value(),
            plan=ScientificBatchPlan((ScientificStagePlan(stage_id="design"),)),
        )
        .stages[0]
    )
    ref = WorkloadResource(
        operation_id=uuid4(),
        batch_id=uuid4(),
        workload_id=uuid4(),
        attempt_id=attempt_id,
        stage_id="design",
        shard_id="main",
        attempt_number=1,
        tenant_id="tenant-a",
        model_id="protein-design",
        variant_id="protein-design-h100",
        input_artifact_id=uuid4(),
        service_class=ServiceClass.CUSTOMER_BATCH,
        scheduling_snapshot_digest="sha256:" + "a" * 64,
        namespace="fs2-models",
        name="scientific-pending",
        kind=WorkloadKind.JOB,
        scheduling=decision,
    ).ref

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/scientific-pending"):
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "name": ref.name,
                        "namespace": ref.namespace,
                        "uid": "pending-job-uid",
                        "labels": {ATTEMPT_LABEL: str(attempt_id)},
                        "annotations": {
                            CLUSTER_QUEUE_ANNOTATION: "inference",
                            ACCELERATOR_RESOURCE_ANNOTATION: "nvidia.com/gpu",
                            ACCELERATOR_COUNT_ANNOTATION: "1",
                            WORKLOAD_NAMESPACE_ANNOTATION: "fs2-models",
                            ROUTE_NAMESPACE_ANNOTATION: "fs2-models",
                        },
                    },
                    "status": {},
                },
            )
        if request.url.path.endswith("/workloads"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"uid": "pending-kueue-uid"},
                            "status": {
                                "conditions": [
                                    {
                                        "type": "QuotaReserved",
                                        "status": "True",
                                        "lastTransitionTime": "2026-09-02T20:00:00Z",
                                    }
                                ],
                                "admission": {
                                    "clusterQueue": "inference",
                                    "podSetAssignments": [
                                        {
                                            "flavors": {"nvidia.com/gpu": "inference-h100-1x"},
                                            "resourceUsage": {"nvidia.com/gpu": "1"},
                                        }
                                    ],
                                },
                            },
                        }
                    ]
                },
            )
        if request.url.path.endswith("/resourceflavors/inference-h100-1x"):
            return httpx.Response(
                200,
                json={"metadata": {"labels": {"accelerator.fs2.nebius/pool-id": "h100-preemptible"}}},
            )
        return httpx.Response(200, json={"items": []})

    token = tmp_path / "pending-token"
    token.write_text("x" * 32)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.test")
    cluster = HttpScientificBatchCluster(
        base_url="https://kubernetes.test",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        controller_id="controller-a",
        fence=Fence(),
        renderer=JobRenderer(),
        writes_enabled=True,
        client=client,
    )
    observation = await cluster.observe(ref, scheduling=decision)
    assert observation.state is WorkloadState.PENDING
    assert observation.scheduling_admission == SchedulingAdmission(
        resolved_pool_id="h100-preemptible",
        admitted_resource_flavor="inference-h100-1x",
        accelerator_resource_name="nvidia.com/gpu",
        accelerator_count=1,
        quota_reserved_at=datetime(2026, 9, 2, 20, 0, tzinfo=UTC),
        admitted_at=None,
    )
    assert LifecyclePhase.ADMITTED not in observation.phases
    assert observation.kueue_workload_uid == "pending-kueue-uid"
    await client.aclose()


@pytest.mark.asyncio
async def test_kubernetes_observer_reports_eviction_after_admission_is_cleared(tmp_path: Path) -> None:
    attempt_id = uuid4()
    decision = (
        scheduling()
        .freeze(
            service_class="customer-batch",
            model_id="protein-design",
            tenant_id="tenant-a",
            profile=profile_value(),
            plan=ScientificBatchPlan((ScientificStagePlan(stage_id="design"),)),
        )
        .stages[0]
    )
    ref = WorkloadRef(
        namespace="fs2-models",
        name="scientific-evicted",
        kind=WorkloadKind.JOB,
        uid="evicted-job-uid",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/scientific-evicted"):
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "name": ref.name,
                        "namespace": ref.namespace,
                        "uid": ref.uid,
                        "labels": {ATTEMPT_LABEL: str(attempt_id)},
                        "annotations": {
                            CLUSTER_QUEUE_ANNOTATION: "inference",
                            ACCELERATOR_RESOURCE_ANNOTATION: "nvidia.com/gpu",
                            ACCELERATOR_COUNT_ANNOTATION: "1",
                            WORKLOAD_NAMESPACE_ANNOTATION: "fs2-models",
                            ROUTE_NAMESPACE_ANNOTATION: "fs2-models",
                        },
                    },
                    "status": {"startTime": "2026-09-03T07:55:00Z"},
                },
            )
        if request.url.path.endswith("/workloads"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"uid": "evicted-kueue-uid"},
                            "status": {
                                "conditions": [
                                    {"type": "Admitted", "status": "False"},
                                    {"type": "QuotaReserved", "status": "False"},
                                    {"type": "Evicted", "status": "True", "reason": "Preempted"},
                                ]
                            },
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"items": []})

    token = tmp_path / "evicted-token"
    token.write_text("x" * 32)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.test")
    cluster = HttpScientificBatchCluster(
        base_url="https://kubernetes.test",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        controller_id="controller-a",
        fence=Fence(),
        renderer=JobRenderer(),
        writes_enabled=True,
        client=client,
    )
    observation = await cluster.observe(ref, scheduling=decision)
    assert observation.state is WorkloadState.PREEMPTED
    assert observation.scheduling_admission is None
    assert observation.kueue_workload_uid == "evicted-kueue-uid"
    assert observation.failure_kind is FailureKind.PREEMPTION
    assert observation.failure_code == "Preempted"
    assert observation.phases == (LifecyclePhase.PREEMPTED,)
    await client.aclose()


@pytest.mark.parametrize(
    ("condition_type", "observed_reason", "expected_kind", "expected_code"),
    [
        ("DeactivationTarget", "MaximumExecutionTimeExceeded", FailureKind.APPLICATION, "EXECUTION_TIMEOUT"),
        (
            "Evicted",
            "DeactivatedDueToMaximumExecutionTimeExceeded",
            FailureKind.APPLICATION,
            "EXECUTION_TIMEOUT",
        ),
        (
            "Evicted",
            "EvictedDueToDeactivatedDueToMaximumExecutionTimeExceeded",
            FailureKind.APPLICATION,
            "EXECUTION_TIMEOUT",
        ),
        ("DeactivationTarget", "RequeuingLimitExceeded", FailureKind.INFRASTRUCTURE, "RequeuingLimitExceeded"),
        (
            "Evicted",
            "DeactivatedDueToRequeuingLimitExceeded",
            FailureKind.INFRASTRUCTURE,
            "RequeuingLimitExceeded",
        ),
        (
            "Evicted",
            "EvictedDueToDeactivatedDueToRequeuingLimitExceeded",
            FailureKind.INFRASTRUCTURE,
            "RequeuingLimitExceeded",
        ),
    ],
)
@pytest.mark.asyncio
async def test_kubernetes_observer_normalizes_raw_and_composed_kueue_deactivation_reasons(
    tmp_path: Path,
    condition_type: str,
    observed_reason: str,
    expected_kind: FailureKind,
    expected_code: str,
) -> None:
    attempt_id = uuid4()
    decision = (
        scheduling()
        .freeze(
            service_class="customer-batch",
            model_id="protein-design",
            tenant_id="tenant-a",
            profile=profile_value(),
            plan=ScientificBatchPlan((ScientificStagePlan(stage_id="design"),)),
        )
        .stage("design")
    )
    ref = WorkloadRef(
        namespace="fs2-models",
        route_namespace="fs2-models",
        name=f"scientific-{condition_type.lower()}-{hashlib.sha256(observed_reason.encode()).hexdigest()[:12]}",
        kind=WorkloadKind.JOB,
        uid="deactivated-job-uid",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/{ref.name}"):
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "name": ref.name,
                        "namespace": ref.namespace,
                        "uid": ref.uid,
                        "labels": {ATTEMPT_LABEL: str(attempt_id)},
                        "annotations": {
                            CLUSTER_QUEUE_ANNOTATION: "inference",
                            ACCELERATOR_RESOURCE_ANNOTATION: "nvidia.com/gpu",
                            ACCELERATOR_COUNT_ANNOTATION: "1",
                            WORKLOAD_NAMESPACE_ANNOTATION: "fs2-models",
                            ROUTE_NAMESPACE_ANNOTATION: "fs2-models",
                        },
                    },
                    "status": {},
                },
            )
        if request.url.path.endswith("/workloads"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"uid": "deactivated-kueue-uid"},
                            "status": {
                                "conditions": [
                                    {
                                        "type": condition_type,
                                        "status": "True",
                                        "reason": observed_reason,
                                    },
                                ]
                            },
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"items": []})

    token = tmp_path / f"{hashlib.sha256(observed_reason.encode()).hexdigest()[:12]}-token"
    token.write_text("x" * 32)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.test")
    cluster = HttpScientificBatchCluster(
        base_url="https://kubernetes.test",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        controller_id="controller-a",
        fence=Fence(),
        renderer=JobRenderer(),
        writes_enabled=True,
        client=client,
    )
    observation = await cluster.observe(ref, scheduling=decision)
    assert observation.state is WorkloadState.FAILED
    assert observation.failure_kind is expected_kind
    assert observation.failure_code == expected_code
    assert observation.kueue_workload_uid == "deactivated-kueue-uid"
    await client.aclose()


STAGE_FINISHED_AT = datetime(2026, 9, 3, 4, 0, tzinfo=UTC)


def _collection_ref(attempt_id: UUID) -> tuple[WorkloadRef, StageSchedulingDecision]:
    decision = (
        scheduling()
        .freeze(
            service_class="customer-batch",
            model_id="protein-design",
            tenant_id="tenant-a",
            profile=profile_value(),
            plan=ScientificBatchPlan((ScientificStagePlan(stage_id="design"),)),
        )
        .stages[0]
    )
    ref = WorkloadResource(
        operation_id=uuid4(),
        batch_id=uuid4(),
        workload_id=uuid4(),
        attempt_id=attempt_id,
        stage_id="design",
        shard_id="main",
        attempt_number=1,
        tenant_id="tenant-a",
        model_id="protein-design",
        variant_id="protein-design-h100",
        input_artifact_id=uuid4(),
        service_class=ServiceClass.CUSTOMER_BATCH,
        scheduling_snapshot_digest="sha256:" + "a" * 64,
        namespace="fs2-models",
        name="scientific-collecting",
        kind=WorkloadKind.JOB,
        scheduling=decision,
    ).ref
    return ref, decision


def _staged_pod(
    *,
    stage: dict[str, object] | None,
    collector_terminated: bool = False,
    pod_reason: str | None = None,
    phase: str = "Running",
) -> dict[str, object]:
    """One staged Pod: the model container beside its artifact collector."""

    statuses: list[dict[str, object]] = [
        {
            "name": "scientific-stage",
            "state": {"terminated": stage} if stage is not None else {"running": {}},
        },
        {
            "name": "artifact-collector",
            "state": (
                {"terminated": {"exitCode": 0, "reason": "Completed"}} if collector_terminated else {"running": {}}
            ),
        },
    ]
    status: dict[str, object] = {
        "phase": phase,
        "conditions": [{"type": "PodScheduled", "status": "True"}],
        "containerStatuses": statuses,
    }
    if pod_reason is not None:
        status["reason"] = pod_reason
    return {"metadata": {"uid": "collecting-pod-uid"}, "status": status}


def _collection_cluster(
    tmp_path: Path,
    ref: WorkloadRef,
    attempt_id: UUID,
    *,
    pod: dict[str, object],
    now: datetime,
) -> tuple[HttpScientificBatchCluster, httpx.AsyncClient]:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pods"):
            return httpx.Response(200, json={"items": [pod]})
        if request.url.path.endswith(f"/{ref.name}"):
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "name": ref.name,
                        "namespace": ref.namespace,
                        "uid": "collecting-job-uid",
                        "labels": {ATTEMPT_LABEL: str(attempt_id)},
                        "annotations": {
                            CLUSTER_QUEUE_ANNOTATION: "inference",
                            ACCELERATOR_RESOURCE_ANNOTATION: "nvidia.com/gpu",
                            ACCELERATOR_COUNT_ANNOTATION: "1",
                            WORKLOAD_NAMESPACE_ANNOTATION: "fs2-models",
                            ROUTE_NAMESPACE_ANNOTATION: "fs2-models",
                        },
                    },
                    "status": {"startTime": "2026-09-03T03:00:00Z"},
                },
            )
        if request.url.path.endswith("/workloads"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"uid": "collecting-kueue-uid"},
                            "status": {
                                "conditions": [
                                    {
                                        "type": "QuotaReserved",
                                        "status": "True",
                                        "lastTransitionTime": "2026-09-03T03:00:00Z",
                                    },
                                    {
                                        "type": "Admitted",
                                        "status": "True",
                                        "lastTransitionTime": "2026-09-03T03:00:00Z",
                                    },
                                ],
                                "admission": {
                                    "clusterQueue": "inference",
                                    "podSetAssignments": [
                                        {
                                            "flavors": {"nvidia.com/gpu": "inference-h100-1x"},
                                            "resourceUsage": {"nvidia.com/gpu": "1"},
                                        }
                                    ],
                                },
                            },
                        }
                    ]
                },
            )
        if request.url.path.endswith("/resourceflavors/inference-h100-1x"):
            return httpx.Response(
                200,
                json={"metadata": {"labels": {"accelerator.fs2.nebius/pool-id": "h100-preemptible"}}},
            )
        return httpx.Response(200, json={"items": []})

    token = tmp_path / "collecting-token"
    token.write_text("x" * 32)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.test")
    cluster = HttpScientificBatchCluster(
        base_url="https://kubernetes.test",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        controller_id="controller-a",
        fence=Fence(),
        renderer=JobRenderer(),
        writes_enabled=True,
        client=client,
        clock=lambda: now,
    )
    return cluster, client


@pytest.mark.asyncio
async def test_kubernetes_observer_fails_promptly_when_the_model_exits_without_a_result(
    tmp_path: Path,
) -> None:
    """A dead model must not hold its GPUs until activeDeadlineSeconds."""

    attempt_id = uuid4()
    ref, decision = _collection_ref(attempt_id)
    cluster, client = _collection_cluster(
        tmp_path,
        ref,
        attempt_id,
        pod=_staged_pod(
            stage={"exitCode": 1, "reason": "Error", "finishedAt": "2026-09-03T04:00:00Z"},
        ),
        # One second after the model died: no grace is spent on a failed stage.
        now=STAGE_FINISHED_AT + timedelta(seconds=1),
    )
    observation = await cluster.observe(ref, scheduling=decision)
    assert observation.state is WorkloadState.FAILED
    assert observation.failure_kind is FailureKind.APPLICATION
    assert observation.failure_code == "Error"
    # A dead model is the stage's own result, so the controller must not retry.
    assert not observation.failure_kind.retryable
    assert observation.pod_uids == ("collecting-pod-uid",)
    await client.aclose()


@pytest.mark.asyncio
async def test_kubernetes_observer_keeps_oom_and_preemption_classes_for_a_stalled_collection(
    tmp_path: Path,
) -> None:
    """The prompt verdict reuses the taxonomy the deadline would have applied."""

    attempt_id = uuid4()
    ref, decision = _collection_ref(attempt_id)
    cluster, client = _collection_cluster(
        tmp_path,
        ref,
        attempt_id,
        pod=_staged_pod(stage={"exitCode": 137, "reason": "OOMKilled", "finishedAt": "2026-09-03T04:00:00Z"}),
        now=STAGE_FINISHED_AT + timedelta(seconds=1),
    )
    oom = await cluster.observe(ref, scheduling=decision)
    assert (oom.state, oom.failure_kind, oom.failure_code) == (
        WorkloadState.FAILED,
        FailureKind.APPLICATION,
        "OOMKilled",
    )
    assert _failure(["OOMKilled"]) == (WorkloadState.FAILED, FailureKind.APPLICATION, "OOMKilled")
    await client.aclose()

    preempted_cluster, preempted_client = _collection_cluster(
        tmp_path,
        ref,
        attempt_id,
        pod=_staged_pod(
            stage={"exitCode": 137, "reason": "Error", "finishedAt": "2026-09-03T04:00:00Z"},
            pod_reason="Preempted",
        ),
        now=STAGE_FINISHED_AT + timedelta(seconds=1),
    )
    preempted = await preempted_cluster.observe(ref, scheduling=decision)
    assert preempted.state is WorkloadState.PREEMPTED
    assert preempted.failure_kind is FailureKind.PREEMPTION
    # Preemption stays retryable, and the attempt still reports the phase.
    assert preempted.failure_kind.retryable
    assert LifecyclePhase.PREEMPTED in preempted.phases
    await preempted_client.aclose()


@pytest.mark.asyncio
async def test_kubernetes_observer_bounds_collection_after_the_model_succeeded(
    tmp_path: Path,
) -> None:
    """A successful model that published nothing collectable is still bounded."""

    attempt_id = uuid4()
    ref, decision = _collection_ref(attempt_id)
    stage = {"exitCode": 0, "reason": "Completed", "finishedAt": "2026-09-03T04:00:00Z"}
    inside_cluster, inside_client = _collection_cluster(
        tmp_path,
        ref,
        attempt_id,
        pod=_staged_pod(stage=stage),
        now=STAGE_FINISHED_AT + timedelta(seconds=COLLECTION_GRACE_SECONDS - 1),
    )
    inside = await inside_cluster.observe(ref, scheduling=decision)
    # Inside the grace the collector is still legitimately publishing.
    assert inside.state is WorkloadState.RUNNING
    assert inside.failure_kind is None
    await inside_client.aclose()

    expired_cluster, expired_client = _collection_cluster(
        tmp_path,
        ref,
        attempt_id,
        pod=_staged_pod(stage=stage),
        now=STAGE_FINISHED_AT + timedelta(seconds=COLLECTION_GRACE_SECONDS),
    )
    expired = await expired_cluster.observe(ref, scheduling=decision)
    assert expired.state is WorkloadState.FAILED
    assert expired.failure_kind is FailureKind.APPLICATION
    assert expired.failure_code == "collection_deadline_exceeded"
    assert not expired.failure_kind.retryable
    await expired_client.aclose()


@pytest.mark.asyncio
async def test_kubernetes_observer_leaves_running_and_settled_pods_to_the_existing_paths(
    tmp_path: Path,
) -> None:
    """The handshake fires only while a collection is genuinely outstanding."""

    attempt_id = uuid4()
    ref, decision = _collection_ref(attempt_id)
    running_cluster, running_client = _collection_cluster(
        tmp_path,
        ref,
        attempt_id,
        pod=_staged_pod(stage=None),
        now=STAGE_FINISHED_AT + timedelta(days=1),
    )
    running = await running_cluster.observe(ref, scheduling=decision)
    assert running.state is WorkloadState.RUNNING
    assert running.failure_kind is None
    await running_client.aclose()

    settled_cluster, settled_client = _collection_cluster(
        tmp_path,
        ref,
        attempt_id,
        pod=_staged_pod(
            stage={"exitCode": 1, "reason": "Error", "finishedAt": "2026-09-03T04:00:00Z"},
            collector_terminated=True,
            phase="Failed",
        ),
        now=STAGE_FINISHED_AT + timedelta(days=1),
    )
    settled = await settled_cluster.observe(ref, scheduling=decision)
    # Both containers terminated, so the Job status settles this attempt.
    assert settled.failure_kind is None
    await settled_client.aclose()


@pytest.mark.asyncio
async def test_kubernetes_observer_counts_only_exact_gpu_podsets_in_mixed_jobset(tmp_path: Path) -> None:
    attempt_id = uuid4()
    decision = replace(
        scheduling()
        .freeze(
            service_class="customer-batch",
            model_id="protein-design",
            tenant_id="tenant-a",
            profile=profile_value(),
            plan=ScientificBatchPlan((ScientificStagePlan(stage_id="design"),)),
        )
        .stages[0],
        accelerator_count=4,
    )
    ref = WorkloadRef(
        namespace="fs2-models",
        name="scientific-mixed-gang",
        kind=WorkloadKind.JOB_SET,
        uid="mixed-jobset-uid",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/scientific-mixed-gang"):
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "name": ref.name,
                        "namespace": ref.namespace,
                        "uid": ref.uid,
                        "labels": {ATTEMPT_LABEL: str(attempt_id)},
                        "annotations": {
                            CLUSTER_QUEUE_ANNOTATION: "inference",
                            ACCELERATOR_RESOURCE_ANNOTATION: "nvidia.com/gpu",
                            ACCELERATOR_COUNT_ANNOTATION: "4",
                            WORKLOAD_NAMESPACE_ANNOTATION: "fs2-models",
                            ROUTE_NAMESPACE_ANNOTATION: "fs2-models",
                        },
                    },
                    "status": {},
                },
            )
        if request.url.path.endswith("/workloads"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"uid": "mixed-kueue-uid"},
                            "status": {
                                "conditions": [
                                    {
                                        "type": "QuotaReserved",
                                        "status": "True",
                                        "lastTransitionTime": "2026-09-03T08:00:00Z",
                                    }
                                ],
                                "admission": {
                                    "clusterQueue": "inference",
                                    "podSetAssignments": [
                                        {
                                            "name": "coordinator",
                                            "flavors": {"cpu": "cpu-general"},
                                            "resourceUsage": {"cpu": "2", "memory": "8Gi"},
                                        },
                                        {
                                            "name": "workers",
                                            "flavors": {
                                                "cpu": "cpu-general",
                                                "nvidia.com/gpu": "inference-h100-1x",
                                            },
                                            "resourceUsage": {
                                                "cpu": "32",
                                                "memory": "256Gi",
                                                "example.com/rdma": "4",
                                                "nvidia.com/gpu": "4",
                                            },
                                        },
                                    ],
                                },
                            },
                        }
                    ]
                },
            )
        if request.url.path.endswith("/resourceflavors/inference-h100-1x"):
            return httpx.Response(
                200,
                json={"metadata": {"labels": {"accelerator.fs2.nebius/pool-id": "h100-preemptible"}}},
            )
        return httpx.Response(200, json={"items": []})

    token = tmp_path / "mixed-token"
    token.write_text("x" * 32)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.test")
    cluster = HttpScientificBatchCluster(
        base_url="https://kubernetes.test",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        controller_id="controller-a",
        fence=Fence(),
        renderer=JobRenderer(),
        writes_enabled=True,
        client=client,
    )
    observation = await cluster.observe(ref, scheduling=decision)
    assert observation.scheduling_admission == SchedulingAdmission(
        resolved_pool_id="h100-preemptible",
        admitted_resource_flavor="inference-h100-1x",
        accelerator_resource_name="nvidia.com/gpu",
        accelerator_count=4,
        quota_reserved_at=datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
        admitted_at=None,
    )
    assert observation.kueue_workload_uid == "mixed-kueue-uid"
    assert LifecyclePhase.ADMITTED not in observation.phases
    await client.aclose()


@pytest.mark.asyncio
async def test_kubernetes_observer_rejects_nonpositive_exact_gpu_podset(tmp_path: Path) -> None:
    attempt_id = uuid4()
    decision = replace(
        scheduling()
        .freeze(
            service_class="customer-batch",
            model_id="protein-design",
            tenant_id="tenant-a",
            profile=profile_value(),
            plan=ScientificBatchPlan((ScientificStagePlan(stage_id="design"),)),
        )
        .stages[0],
        accelerator_count=4,
    )
    ref = WorkloadRef(
        namespace="fs2-models",
        name="scientific-invalid-gang",
        kind=WorkloadKind.JOB_SET,
        uid="invalid-jobset-uid",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/scientific-invalid-gang"):
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "name": ref.name,
                        "namespace": ref.namespace,
                        "uid": ref.uid,
                        "labels": {ATTEMPT_LABEL: str(attempt_id)},
                        "annotations": {
                            CLUSTER_QUEUE_ANNOTATION: "inference",
                            ACCELERATOR_RESOURCE_ANNOTATION: "nvidia.com/gpu",
                            ACCELERATOR_COUNT_ANNOTATION: "4",
                            WORKLOAD_NAMESPACE_ANNOTATION: "fs2-models",
                            ROUTE_NAMESPACE_ANNOTATION: "fs2-models",
                        },
                    },
                    "status": {},
                },
            )
        if request.url.path.endswith("/workloads"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"uid": "invalid-kueue-uid"},
                            "status": {
                                "conditions": [
                                    {
                                        "type": "QuotaReserved",
                                        "status": "True",
                                        "lastTransitionTime": "2026-09-03T08:00:00Z",
                                    }
                                ],
                                "admission": {
                                    "clusterQueue": "inference",
                                    "podSetAssignments": [
                                        {
                                            "name": "coordinator",
                                            "flavors": {"cpu": "cpu-general"},
                                            "resourceUsage": {"cpu": "2"},
                                        },
                                        {
                                            "name": "workers",
                                            "flavors": {"nvidia.com/gpu": "inference-h100-8x"},
                                            "resourceUsage": {"nvidia.com/gpu": "0"},
                                        },
                                    ],
                                },
                            },
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"items": []})

    token = tmp_path / "invalid-mixed-token"
    token.write_text("x" * 32)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.test")
    cluster = HttpScientificBatchCluster(
        base_url="https://kubernetes.test",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        controller_id="controller-a",
        fence=Fence(),
        renderer=JobRenderer(),
        writes_enabled=True,
        client=client,
    )
    with pytest.raises(ScientificKubernetesError, match="GPU PodSet admission is not positive"):
        await cluster.observe(ref, scheduling=decision)
    await client.aclose()


@pytest.mark.asyncio
async def test_kubernetes_delete_waits_for_uid_absence_and_fences_precondition_conflict(tmp_path: Path) -> None:
    operation_id = uuid4()
    ref = WorkloadRef(namespace="fs2-models", name="scientific-delete", kind=WorkloadKind.JOB, uid="job-uid")
    get_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        if request.method == "DELETE":
            body = json.loads(request.content)
            assert body["preconditions"] == {"uid": "job-uid"}
            assert body["propagationPolicy"] == "Foreground"
            return httpx.Response(202, json={"status": "Success"})
        get_count += 1
        if get_count == 3:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "metadata": {
                    "name": ref.name,
                    "namespace": ref.namespace,
                    "uid": ref.uid,
                    "labels": {"fs2.nebius.ai/operation-id": str(operation_id)},
                    "annotations": {
                        WORKLOAD_NAMESPACE_ANNOTATION: "fs2-models",
                        ROUTE_NAMESPACE_ANNOTATION: "fs2-models",
                    },
                }
            },
        )

    token = tmp_path / "delete-token"
    token.write_text("x" * 32)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.test")
    cluster = HttpScientificBatchCluster(
        base_url="https://kubernetes.test",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        controller_id="controller-a",
        fence=Fence(),
        renderer=JobRenderer(),
        writes_enabled=True,
        client=client,
    )
    await cluster.delete(ref, controller_fence=9)
    assert await cluster.absent(ref) is False
    assert await cluster.absent(ref) is True
    await client.aclose()

    async def conflict_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(409, json={"reason": "Conflict"})
        return httpx.Response(
            200,
            json={
                "metadata": {
                    "name": ref.name,
                    "namespace": ref.namespace,
                    "uid": ref.uid,
                    "labels": {"fs2.nebius.ai/operation-id": str(operation_id)},
                    "annotations": {
                        WORKLOAD_NAMESPACE_ANNOTATION: "fs2-models",
                        ROUTE_NAMESPACE_ANNOTATION: "fs2-models",
                    },
                }
            },
        )

    conflict_client = httpx.AsyncClient(
        transport=httpx.MockTransport(conflict_handler), base_url="https://kubernetes.test"
    )
    conflict_cluster = HttpScientificBatchCluster(
        base_url="https://kubernetes.test",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        controller_id="controller-a",
        fence=Fence(),
        renderer=JobRenderer(),
        writes_enabled=True,
        client=conflict_client,
    )
    with pytest.raises(BatchRepositoryConflictError, match="precondition"):
        await conflict_cluster.delete(ref, controller_fence=10)
    await conflict_client.aclose()


@pytest.mark.asyncio
async def test_kubernetes_delete_rejects_synchronous_success(tmp_path: Path) -> None:
    operation_id = uuid4()
    ref = WorkloadRef(namespace="fs2-models", name="scientific-delete", kind=WorkloadKind.JOB, uid="job-uid")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "Success"})
        return httpx.Response(
            200,
            json={
                "metadata": {
                    "name": ref.name,
                    "namespace": ref.namespace,
                    "uid": ref.uid,
                    "labels": {"fs2.nebius.ai/operation-id": str(operation_id)},
                    "annotations": {
                        WORKLOAD_NAMESPACE_ANNOTATION: "fs2-models",
                        ROUTE_NAMESPACE_ANNOTATION: "fs2-models",
                    },
                }
            },
        )

    token = tmp_path / "delete-token-200"
    token.write_text("x" * 32)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.test")
    cluster = HttpScientificBatchCluster(
        base_url="https://kubernetes.test",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        controller_id="controller-a",
        fence=Fence(),
        renderer=JobRenderer(),
        writes_enabled=True,
        client=client,
    )
    with pytest.raises(ScientificKubernetesError, match="not accepted asynchronously"):
        await cluster.delete(ref, controller_fence=9)
    await client.aclose()


@pytest.mark.asyncio
async def test_workload_capability_materializes_and_commits_through_single_artifact_port(hasher) -> None:
    now = datetime(2026, 9, 2, 21, tzinfo=UTC)
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()
    reconciler = ScientificBatchController(
        repository=repository,
        cluster=cluster,
        controller_id="controller-a",
        namespace="fs2-models",
        clock=lambda: now,
    )
    operation_id = uuid4()
    input_id = uuid4()
    manifest_id = uuid4()
    controller_plan = ScientificBatchPlan((ScientificStagePlan(stage_id="design"),))
    invocation = StageInvocation(
        stage_id="design",
        shard_id="main",
        argv=("protein-design", "run", "--input", "/mnt/fs2-scientific/work/design/main/input.json"),
        environment=(),
        working_directory="/mnt/fs2-scientific/work/design/main",
        consumes=("request",),
        produces="design-result",
        collector_id="protein-design-output-v1",
        validator_id="protein-design-v1",
        handoff_name="structure",
        materializations=(
            ArtifactMaterialization(
                "request",
                "/mnt/fs2-scientific/work/design/main/input.json",
                MaterializationMode.COPY_FILE,
            ),
        ),
    )
    execution = AdapterExecutionPlan(
        model_id="protein-design",
        variant_id="protein-design-h100",
        source_revision="b" * 40,
        request_sha256="1" * 64,
        controller_plan=controller_plan,
        invocations=(invocation,),
        required_model_artifacts=(),
    )
    execution = FakeExecutionBinding().bind_runtime_artifacts(
        profile_catalog().get("protein-design"),
        execution,
        ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="tenant-a"),
        (),
    )
    await reconciler.admit(
        operation_id=operation_id,
        tenant_id="tenant-a",
        model_id="protein-design",
        variant_id="protein-design-h100",
        input_artifact_id=manifest_id,
        plan=controller_plan,
        scheduling=scheduling().freeze(
            service_class="customer-batch",
            model_id="protein-design",
            tenant_id="tenant-a",
            profile=profile_value(),
            plan=controller_plan,
            captured_at=now,
        ),
        execution_plan=execution,
        access_context=ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="tenant-a"),
        input_manifest=VerifiedInputManifest(
            manifest_id="request-inputs",
            manifest_artifact_id=manifest_id,
            manifest_digest="sha256:" + "2" * 64,
            entries=(
                ScientificInputArtifact(
                    logical_artifact_id="request",
                    semantic_type="request/v1",
                    artifact_id=input_id,
                    digest="sha256:" + "3" * 64,
                    size_bytes=10,
                    media_type="application/json",
                ),
            ),
        ),
    )
    await reconciler.reconcile_once()
    resource = cluster.apply_history[0]
    authority = ScientificWorkloadCapabilityAuthority(hasher)
    capability = authority.issue(resource)

    input_owner = uuid4()
    input_attempt = uuid4()
    input_record = ArtifactRecord(
        artifact_id=input_id,
        attempt_id=input_attempt,
        operation_id=input_owner,
        tenant_id="tenant-a",
        stage_id="input",
        shard_id=None,
        direction=ArtifactDirection.INPUT,
        digest="sha256:" + "3" * 64,
        size_bytes=10,
        media_type="application/json",
        storage_key=artifact_storage_key(
            tenant_id="tenant-a",
            operation_id=input_owner,
            stage_id="input",
            shard_id=None,
            attempt_id=input_attempt,
            direction=ArtifactDirection.INPUT,
            digest="sha256:" + "3" * 64,
        ),
        access=ArtifactAccess(),
        retention_expires_at=now + timedelta(days=1),
        created_at=now,
    )

    class Artifacts:
        def __init__(self) -> None:
            self.uploads: dict[UUID, UploadIntent] = {}
            self.upload_attempts: list[UUID] = []
            self.attempts: dict[UUID, object] = {}

        async def open_attempt(self, request):
            self.attempts[request.attempt_id] = request
            return request

        async def download(self, artifact_id, *, tenant_id, handle_ttl=None):
            assert artifact_id == input_id and tenant_id == "tenant-a" and handle_ttl is None
            return ArtifactDownload(
                artifact=input_record,
                handle=EphemeralHandle(
                    method="GET",
                    url="https://objects.test/input",
                    expires_at=now + timedelta(minutes=5),
                ),
            )

        async def begin_upload(self, request, *, handle_ttl=None):
            assert handle_ttl is None
            opened = self.attempts[request.attempt_id]
            self.upload_attempts.append(request.attempt_id)
            intent = UploadIntent(
                upload_id=request.upload_id,
                attempt_id=request.attempt_id,
                operation_id=request.operation_id,
                tenant_id=request.tenant_id,
                stage_id=opened.stage_id,
                shard_id=opened.shard_id,
                direction=request.direction,
                expected_digest=request.expected_digest,
                expected_size_bytes=request.expected_size_bytes,
                media_type=request.media_type,
                compression=request.compression,
                storage_key=artifact_storage_key(
                    tenant_id=request.tenant_id,
                    operation_id=request.operation_id,
                    stage_id=opened.stage_id,
                    shard_id=opened.shard_id,
                    attempt_id=request.attempt_id,
                    direction=request.direction,
                    digest=request.expected_digest,
                ),
                access=request.access,
                begun_at=now,
            )
            self.uploads[request.upload_id] = intent
            return BeginUploadResult(
                upload=intent,
                handle=EphemeralHandle(
                    method="PUT",
                    url=f"https://objects.test/{request.upload_id}",
                    expires_at=now + timedelta(minutes=5),
                    write_once=True,
                ),
            )

        async def finalize_upload(self, request):
            intent = self.uploads[request.upload_id]
            return ArtifactRecord(
                artifact_id=request.upload_id,
                attempt_id=intent.attempt_id,
                operation_id=request.operation_id,
                tenant_id=request.tenant_id,
                stage_id=intent.stage_id,
                shard_id=intent.shard_id,
                direction=ArtifactDirection.OUTPUT,
                digest=intent.expected_digest,
                size_bytes=intent.expected_size_bytes,
                media_type=intent.media_type,
                compression=intent.compression,
                storage_key=intent.storage_key,
                access=intent.access,
                retention_expires_at=now + timedelta(days=1),
                created_at=now,
            )

    artifacts = Artifacts()
    app = FastAPI()
    app.include_router(
        scientific_workload_artifact_router(
            authority=authority,
            artifacts=artifacts,  # type: ignore[arg-type]
            batches=repository,
        )
    )
    headers = {"authorization": f"Bearer {capability}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://control.test",
        headers=headers,
    ) as client:
        downloaded = await client.get(f"/internal/scientific-workloads/artifacts/{input_id}:download")
        assert downloaded.status_code == 200
        refs = []
        for index, media_type in enumerate(
            ("chemical/x-pdb", "application/vnd.fs2.scientific-manifest+json", "application/json"),
            start=4,
        ):
            upload_id = uuid4()
            begun = await client.post(
                "/internal/scientific-workloads/uploads",
                json={
                    "upload_id": str(upload_id),
                    "sha256": str(index) * 64,
                    "size_bytes": index,
                    "media_type": media_type,
                },
            )
            assert begun.status_code == 201
            finalized = await client.post(f"/internal/scientific-workloads/uploads/{upload_id}:finalize")
            assert finalized.status_code == 200
            refs.append(finalized.json())
        assert len(refs) == 3
        assert not [route for route in app.routes if getattr(route, "path", "").endswith("/commit")]
        await repository.request_cancel(operation_id, tenant_id="tenant-a", actor="test")
        await reconciler.reconcile_once()
        await reconciler.reconcile_once()
        stale = await client.get(f"/internal/scientific-workloads/artifacts/{input_id}:download")
        assert stale.status_code == 409
    assert artifacts.upload_attempts == [resource.attempt_id] * 3
    assert repository.records[operation_id].status is BatchStatus.CANCELLED


def test_execution_map_renders_only_digest_qualified_direct_argv(tmp_path: Path, monkeypatch) -> None:
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
                            },
                        ],
                        "service_account_name": "scientific-runner",
                        "resources": {
                            "requests": {"cpu": "4", "memory": "32Gi", "ephemeral_storage": "100Gi"},
                            "limits": {"cpu": "4", "memory": "32Gi", "ephemeral_storage": "100Gi"},
                        },
                        "active_deadline_seconds": 3600,
                        "termination_grace_seconds": 60,
                        "environment": {"FS2_ADAPTER_MODE": "production"},
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
    assert renderer.variant_id("protein-design") == "protein-design-h100"
    bound_plan = renderer.plan(
        profile_catalog().get("protein-design"),
        {},
        access_context=ArtifactAccessContext(profile="public", receipt_digest=None),
        input_artifacts=(),
    )
    assert replace(bound_plan, execution_map_sha256=None, stage_bindings=()) == adapter_plan
    assert bound_plan.execution_map_sha256 == f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    assert bound_plan.execution_binding("design").image == "registry.example/protein@sha256:" + "a" * 64
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
        invocation=bound_plan.invocation("design", "main"),
        access_context=ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="tenant-a"),
        execution_map_sha256=bound_plan.execution_map_sha256,
        execution_binding=bound_plan.execution_binding("design"),
    )
    manifest = renderer.render(resource)
    container = manifest["spec"]["template"]["spec"]["containers"][0]  # type: ignore[index]
    pool_expressions = manifest["spec"]["template"]["spec"]["affinity"]["nodeAffinity"][  # type: ignore[index]
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]["matchExpressions"]
    assert {
        "key": "accelerator.fs2.nebius/pool-id",
        "operator": "In",
        "values": ["h100-preemptible"],
    } in pool_expressions
    assert container["image"].endswith("@sha256:" + "a" * 64)
    assert container["command"] == ["python", "-m", "adapter", "run"]
    assert "args" not in container
    assert {item["name"] for item in container["volumeMounts"]} == {"artifact-workspace"}
    environment = {item["name"]: item["value"] for item in container["env"]}
    assert environment["FS2_VARIANT_ID"] == "protein-design-h100"
    assert environment["FS2_TENANT_ID"] == "tenant-a"
    assert environment["FS2_ARTIFACT_ACCESS_PROFILE"] == "public"
    assert environment["FS2_ARTIFACT_ACCESS_RECEIPT_DIGEST"] == ""
    assert UUID(environment["FS2_INPUT_ARTIFACT_ID"])
    assert environment["FS2_COLLECTOR_ID"] == "protein-design-output-v1"
    assert environment["FS2_VALIDATOR_ID"] == "protein-design-v1"
    assert manifest["spec"]["template"]["spec"]["initContainers"][0]["name"] == "prepare-workspace"  # type: ignore[index]
    collector = manifest["spec"]["template"]["spec"]["containers"][1]  # type: ignore[index]
    assert collector["name"] == "artifact-collector"
    # The collector must give up before the Job's own deadline kills the Pod,
    # so a stalled collection still yields a deterministic controller failure.
    collection_bound = int(collector["command"][collector["command"].index("--collection-deadline-seconds") + 1])
    assert collection_bound == 3600 - COLLECTION_DEADLINE_MARGIN_SECONDS
    assert 0 < collection_bound < manifest["spec"]["activeDeadlineSeconds"]  # type: ignore[index]
    assert all("request" not in item["name"].lower() for item in container["env"])
    gang = replace(
        resource,
        attempt_id=uuid4(),
        shard_id=None,
        name="scientific-design-gang",
        kind=WorkloadKind.JOB_SET,
        gang_size=2,
        invocation=replace(invocation, shard_id="gang"),
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
    assert _failure(["Evicted"])[1] is FailureKind.APPLICATION
    assert _kueue_eviction("Preempted")[1] is FailureKind.PREEMPTION
    assert _kueue_eviction("EvictedOnManagerCluster")[1] is FailureKind.INFRASTRUCTURE
    assert _kueue_eviction("DeactivatedByUser")[1] is FailureKind.APPLICATION
    assert _kueue_eviction("MaximumExecutionTimeExceeded") == (
        WorkloadState.FAILED,
        FailureKind.APPLICATION,
        "EXECUTION_TIMEOUT",
    )
    assert _kueue_eviction("DeactivatedDueToMaximumExecutionTimeExceeded") == (
        WorkloadState.FAILED,
        FailureKind.APPLICATION,
        "EXECUTION_TIMEOUT",
    )
    assert _kueue_eviction("RequeuingLimitExceeded") == (
        WorkloadState.FAILED,
        FailureKind.INFRASTRUCTURE,
        "RequeuingLimitExceeded",
    )
    assert _kueue_eviction("DeactivatedDueToRequeuingLimitExceeded") == (
        WorkloadState.FAILED,
        FailureKind.INFRASTRUCTURE,
        "RequeuingLimitExceeded",
    )
    assert _failure(["EvictedDueToDeactivatedDueToMaximumExecutionTimeExceeded"]) == (
        WorkloadState.FAILED,
        FailureKind.APPLICATION,
        "EXECUTION_TIMEOUT",
    )
    assert _failure(["EvictedDueToDeactivatedDueToRequeuingLimitExceeded"]) == (
        WorkloadState.FAILED,
        FailureKind.INFRASTRUCTURE,
        "RequeuingLimitExceeded",
    )
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
    now = datetime.now(UTC) + timedelta(seconds=1)

    class ObjectStore:
        def __init__(self) -> None:
            self.objects: dict[str, tuple[bytes, str]] = {}

        async def presign_upload(self, *, storage_key, media_type, compression, ttl):
            del compression
            return EphemeralHandle(
                method="PUT",
                url=f"https://objects.test/{storage_key}",
                expires_at=now + ttl,
                write_once=True,
                headers={"content-type": media_type},
            )

        async def presign_download(self, *, storage_key, ttl):
            return EphemeralHandle(
                method="GET",
                url=f"https://objects.test/{storage_key}",
                expires_at=now + ttl,
            )

        async def inspect(self, storage_key, *, max_bytes=None):
            value, media_type = self.objects[storage_key]
            assert max_bytes is None or len(value) <= max_bytes
            return VerifiedStoredObject(
                storage_key=storage_key,
                digest="sha256:" + hashlib.sha256(value).hexdigest(),
                size_bytes=len(value),
                media_type=media_type,
            )

        async def delete(self, storage_key):
            self.objects.pop(storage_key, None)

    class ContentReader:
        def __init__(self) -> None:
            self.values: dict[UUID, bytes] = {}

        async def read(self, artifact_id, *, tenant_id, maximum_bytes):
            assert tenant_id == "tenant-a"
            value = self.values[artifact_id]
            assert len(value) <= maximum_bytes
            return value

    artifact_repository = MemoryArtifactRepository(clock=lambda: now)
    object_store = ObjectStore()
    reader = ContentReader()
    artifact_service = ScientificArtifactService(
        repository=artifact_repository,
        object_store=object_store,  # type: ignore[arg-type]
        allowed_media_types={
            "application/json",
            "application/vnd.fs2.scientific-manifest+json",
            "application/vnd.fs2.scientific-validation+json",
            "chemical/x-pdb",
        },
        clock=lambda: now,
    )

    async def upload(
        *, operation_id: UUID, attempt_id: UUID, direction: ArtifactDirection, value: bytes, media_type: str
    ) -> ArtifactRecord:
        request = BeginArtifactUpload(
            upload_id=uuid4(),
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id="tenant-a",
            direction=direction,
            expected_digest="sha256:" + hashlib.sha256(value).hexdigest(),
            expected_size_bytes=len(value),
            media_type=media_type,
            access=ArtifactAccess(),
        )
        begun = await artifact_service.begin_upload(request)
        object_store.objects[begun.upload.storage_key] = (value, media_type)
        record = await artifact_service.finalize_upload(
            FinalizeArtifactUpload(
                upload_id=request.upload_id,
                operation_id=operation_id,
                tenant_id="tenant-a",
            )
        )
        reader.values[record.artifact_id] = value
        return record

    # The public input manifest is a prior, immutable tenant artifact.
    input_operation = uuid4()
    input_attempt = uuid4()
    await artifact_repository.register_operation(input_operation, tenant_id="tenant-a")
    await artifact_service.open_attempt(
        OpenStageAttempt(
            attempt_id=input_attempt,
            operation_id=input_operation,
            tenant_id="tenant-a",
            stage_id="input",
            attempt_number=1,
            started_at=now,
        )
    )
    request_artifact = await upload(
        operation_id=input_operation,
        attempt_id=input_attempt,
        direction=ArtifactDirection.INPUT,
        value=b'{"sequence":"ACDE"}',
        media_type="application/json",
    )
    input_document = {
        "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
        "manifest_id": "request-inputs",
        "entries": [
            {
                "name": "request",
                "semantic_type": "request/v1",
                "artifact": request_artifact.to_public_ref().model_dump(mode="json", exclude_none=True),
            }
        ],
    }
    input_bytes = json.dumps(input_document, sort_keys=True, separators=(",", ":")).encode()
    input_manifest = await upload(
        operation_id=input_operation,
        attempt_id=input_attempt,
        direction=ArtifactDirection.INPUT,
        value=input_bytes,
        media_type="application/vnd.fs2.scientific-manifest+json",
    )
    await artifact_service.close_attempt(
        CloseStageAttempt(
            attempt_id=input_attempt,
            operation_id=input_operation,
            tenant_id="tenant-a",
            status=AttemptStatus.SUCCEEDED,
            completed_at=now,
            admission=KueueAdmission(accelerator_count=0, admitted_at=now),
        )
    )

    runtime, _, batches, cluster, _ = scientific_runtime(registry, cipher, hasher)
    assert runtime.scientific_batches is not None
    runtime.scientific_batches.artifacts = FakeArtifactAccess(
        input_manifest.to_public_ref().model_dump(mode="json", exclude_none=True)
    )
    bridge = ArtifactServiceBridge(
        artifacts=artifact_repository,
        batches=batches,
        profiles=profile_catalog(),
        store=runtime.store,
        content_reader=reader,
        service=artifact_service,
    )
    controller = ScientificBatchController(
        repository=batches,
        cluster=cluster,
        controller_id="controller-a",
        namespace="fs2-models",
        clock=lambda: now,
        artifact_lifecycle=bridge,
        result_publisher=bridge,
    )
    runtime.scientific_batches.controller = controller
    submitted = await runtime.scientific_batches.submit(
        principal=await principal(runtime.store),  # type: ignore[arg-type]
        model_id="protein-design",
        request={
            "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
            "operation": "design",
            "service_class": "customer-batch",
            "input_manifest": input_manifest.to_public_ref().model_dump(mode="json", exclude_none=True),
            "parameters": {},
        },
        idempotency_key="scientific-artifact-bridge-0001",
    )
    operation_id = UUID(submitted["operation"]["id"])
    await artifact_repository.register_operation(operation_id, tenant_id="tenant-a")
    await controller.reconcile_once()
    attempt = batches.records[operation_id].stage("design").attempts[0]

    structure = await upload(
        operation_id=operation_id,
        attempt_id=attempt.attempt_id,
        direction=ArtifactDirection.OUTPUT,
        value=b"ATOM      1  CA  ALA A   1\n",
        media_type="chemical/x-pdb",
    )
    output_document = {
        "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
        "manifest_id": "design-result",
        "entries": [
            {
                "name": "structure",
                "semantic_type": "protein-structure/v1",
                "artifact": structure.to_public_ref().model_dump(mode="json", exclude_none=True),
            }
        ],
    }
    output_bytes = json.dumps(output_document, sort_keys=True, separators=(",", ":")).encode()
    output_manifest = await upload(
        operation_id=operation_id,
        attempt_id=attempt.attempt_id,
        direction=ArtifactDirection.OUTPUT,
        value=output_bytes,
        media_type="application/vnd.fs2.scientific-manifest+json",
    )
    validation_bytes = json.dumps(
        {
            "status": "passed",
            "collector_id": "protein-design-output-v1",
            "validator_id": "protein-design-v1",
            "stage_id": "design",
            "shard_id": "main",
            "logical_output_id": "design-result",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    await upload(
        operation_id=operation_id,
        attempt_id=attempt.attempt_id,
        direction=ArtifactDirection.OUTPUT,
        value=validation_bytes,
        media_type="application/vnd.fs2.scientific-validation+json",
    )
    cluster.set_observation(
        attempt.workload,
        WorkloadObservation(
            ref=attempt.workload,
            attempt_id=attempt.attempt_id,
            state=WorkloadState.SUCCEEDED,
            phases=(LifecyclePhase.ADMITTED, LifecyclePhase.ACTIVE_COMPUTE),
            scheduling_admission=SchedulingAdmission(
                resolved_pool_id="h100-preemptible",
                admitted_resource_flavor="inference-h100-1x",
                accelerator_resource_name="nvidia.com/gpu",
                accelerator_count=1,
                admitted_at=now,
            ),
        ),
    )
    for _ in range(8):
        await controller.reconcile_once()
        if batches.records[operation_id].result_published:
            break
    assert batches.records[operation_id].result_published is True
    result = await bridge.result_response(operation_id, tenant_id="tenant-a")
    profile_catalog().validate_result(result)
    assert result["input_manifest"] == input_manifest.to_public_ref().model_dump(mode="json", exclude_none=True)
    assert result["output_manifest"] == output_manifest.to_public_ref().model_dump(mode="json", exclude_none=True)
    assert result["semantic_validation"]["status"] == "passed"  # type: ignore[index]
    assert result["attempts"][0]["scheduling_admission"]["admitted_resource_flavor"] == "inference-h100-1x"  # type: ignore[index]
    assert "storage_key" not in json.dumps(result)

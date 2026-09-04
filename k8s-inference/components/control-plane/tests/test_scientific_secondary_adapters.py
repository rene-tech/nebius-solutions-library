"""Executable contracts for the four non-AF3 secondary structure adapters."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, ModuleType
from unittest import mock
from uuid import UUID, uuid4

import pytest
from scientific_batch_fakes import FakeScientificBatchCluster, FakeScientificBatchRepository

from fs2_serve.crypto import KeyedHasher
from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import Principal, Scope, TokenCreate
from fs2_serve.scientific_artifacts import (
    ArtifactAccess,
    ArtifactDirection,
    ArtifactRecord,
    artifact_storage_key,
)
from fs2_serve.scientific_batch import (
    ResourceClass,
    ScientificAdapterError,
    ScientificInputArtifact,
    companion,
    compile_adapter_run,
)
from fs2_serve.scientific_batch.adapters import (
    CollectedStageOutput,
    CollectionPendingError,
    esmfold2,
    esmfold2_fast,
    openfold3,
    protenix_v2,
)
from fs2_serve.scientific_batch.adapters import (
    collect_stage_output as collect_registered_stage_output,
)
from fs2_serve.scientific_batch.adapters.common import load_output_manifest
from fs2_serve.scientific_batch.artifact_bridge import ArtifactServiceBridge
from fs2_serve.scientific_batch.capability import ScientificWorkloadCapabilityAuthority
from fs2_serve.scientific_batch.controller import ScientificBatchController
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer, _invocation_json
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog, ScientificWorkloadProfile
from fs2_serve.scientific_batch.scheduling import SchedulingContractResolver
from fs2_serve.scientific_batch.service import ScientificBatchService

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
CATALOG_ROOT = SOLUTION_ROOT / "catalog/runtime"
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters"
IMAGE_HANDOFF = ADAPTER_ROOT / "secondary-r4-image-handoff.json"
WRAPPER_ROOT_ENV = "FS2_SECONDARY_WRAPPER_ROOT"

MODULES = {
    "esmfold2": esmfold2,
    "esmfold2-fast": esmfold2_fast,
    "protenix-v2": protenix_v2,
    "openfold3-openbind": openfold3,
}
ADAPTER_DIRECTORIES = {model_id: "openfold3" if model_id == "openfold3-openbind" else model_id for model_id in MODULES}
POSITIVE_FIXTURES = {
    "esmfold2": ("positive-sequence", "positive-msa"),
    "esmfold2-fast": ("positive-short", "positive-recycles"),
    "protenix-v2": ("positive-complex", "positive-monomer"),
    "openfold3-openbind": ("positive-complex", "positive-monomer"),
}
NEGATIVE_FIXTURES = {
    "esmfold2": "negative-invalid-sequence",
    "esmfold2-fast": "negative-msa",
    "protenix-v2": "negative-duplicate-seeds",
    "openfold3-openbind": "negative-alphafold-alias",
}
STAGE_SHAPES = {
    "esmfold2": (
        ("prepare-input", "cpu", 64, "restart", "restartable"),
        ("fold", "gpu", 64, "restart", "restartable"),
    ),
    "esmfold2-fast": (
        ("prepare-input", "cpu", 64, "restart", "restartable"),
        ("fold", "gpu", 128, "restart", "restartable"),
    ),
    "protenix-v2": (
        ("prepare-data", "cpu", 64, "restart", "restartable"),
        ("sample-structure", "gpu", 32, "restart", "restartable"),
    ),
    "openfold3-openbind": (
        ("data-pipeline", "cpu", 32, "none", "non_preemptible"),
        ("inference", "gpu", 32, "restart", "restartable"),
    ),
}


def fixture(model_id: str, name: str) -> dict[str, object]:
    value = json.loads(
        (ADAPTER_ROOT / ADAPTER_DIRECTORIES[model_id] / "fixtures" / f"{name}.json").read_text(encoding="utf-8")
    )
    assert isinstance(value, dict)
    return value


def contract(model_id: str) -> dict[str, object]:
    value = json.loads((ADAPTER_ROOT / ADAPTER_DIRECTORIES[model_id] / "contract.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def profile(model_id: str) -> dict[str, object]:
    projection = json.loads(
        (ADAPTER_ROOT / ADAPTER_DIRECTORIES[model_id] / "activation/workload-profile.json").read_text(encoding="utf-8")
    )
    value = projection["profile"]
    assert isinstance(value, dict)
    return value


def verified_input(model_id: str, **overrides: object) -> ScientificInputArtifact:
    module = MODULES[model_id]
    values: dict[str, object] = {
        "logical_artifact_id": module.INPUT_ARTIFACT_ID,
        "semantic_type": module.INPUT_SEMANTIC_TYPE,
        "artifact_id": UUID(f"00000000-0000-4000-8000-{len(model_id):012d}"),
        "digest": "sha256:" + hashlib.sha256(model_id.encode()).hexdigest(),
        "size_bytes": 1024,
        "media_type": module.INPUT_MEDIA_TYPE,
        "compression": "none",
    }
    values.update(overrides)
    return ScientificInputArtifact(**values)  # type: ignore[arg-type]


def compile_fixture(model_id: str, name: str):
    return MODULES[model_id].compile_run(
        profile(model_id),
        fixture(model_id, name),
        operation_id=f"op-{model_id}-contract",
        input_artifacts=(verified_input(model_id),),
    )


def scheduling() -> SchedulingContractResolver:
    model_ids = list(MODULES)
    return SchedulingContractResolver(
        {
            "schema": "fs2-serve.nebius.ai/kueue-scheduling/v1",
            "pool_node_label_key": "accelerator.fs2.nebius/pool-id",
            "service_classes": {
                "customer-batch": {
                    "workload_priority_class": "customer-batch",
                    "priority": 0,
                    "default_local_queue": "scientific",
                    "preemption_mode": "restartable",
                    "pool_preference": ["h100-reserved-8x", "h100-1x"],
                    "max_queue_seconds": 900,
                    "max_execution_seconds": 86400,
                    "caller_selectable": True,
                }
            },
            "local_queues": {
                "scientific": {
                    "metadata": {"name": "scientific", "namespace": "fs2-models"},
                    "spec": {"clusterQueue": "inference"},
                },
                "general-cpu": {
                    "metadata": {"name": "general-cpu", "namespace": "fs2-models"},
                    "spec": {"clusterQueue": "general-cpu"},
                },
            },
            "cluster_queues": {
                "inference": {"metadata": {"name": "inference"}, "spec": {}},
                "general-cpu": {"metadata": {"name": "general-cpu"}, "spec": {}},
            },
            "workload_priority_classes": {"customer-batch": {"value": 0}},
            "local_queue_routes": {
                "scientific": {
                    "namespace": "fs2-models",
                    "cluster_queue": "inference",
                    "model_ids": [],
                    "tenant_ids": [],
                    "service_classes": [],
                },
                "general-cpu": {
                    "namespace": "fs2-models",
                    "cluster_queue": "general-cpu",
                    "model_ids": [],
                    "tenant_ids": [],
                    "service_classes": [],
                },
            },
            "model_eligible_pool_ids": {model_id: ["h100-1x", "h100-reserved-8x"] for model_id in model_ids},
            "cpu_classes_schema": "fs2-serve.nebius.ai/cpu-stage-classes/v1",
            "cpu_classes": {
                "general-cpu": {
                    "local_queue": "general-cpu",
                    "cluster_queue": "general-cpu",
                    "namespace": "fs2-models",
                    "resource_flavor": "general-cpu",
                    "pool_resolution": {"mode": "per-pool-flavor", "pool_id": "general-cpu-8x"},
                    "node_selector": {
                        "workload.fs2.nebius/general-cpu": "true",
                        "capacity.fs2.nebius/pool-id": "general-cpu-8x",
                    },
                    "tolerations": [
                        {
                            "key": "workload.fs2.nebius/general-cpu",
                            "operator": "Equal",
                            "value": "true",
                            "effect": "NoSchedule",
                        }
                    ],
                    "eligible_pool_ids": ["general-cpu-8x"],
                    "schedulable_capacity": {
                        "cpu_millicores": 8000,
                        "memory_mib": 32768,
                        "ephemeral_storage_mib": 131072,
                    },
                }
            },
            "cpu_stage_requests": {"general-cpu": {"cpu_millicores": 4000, "memory_mib": 16384}},
            "namespace_bound_models": {},
            "pools": {
                "h100-1x": {
                    "resource_flavor": "h100-1x",
                    "accelerator_resource_name": "nvidia.com/gpu",
                    "capacity": 1,
                },
                "h100-reserved-8x": {
                    "resource_flavor": "h100-reserved-8x",
                    "accelerator_resource_name": "nvidia.com/gpu",
                    "capacity": 8,
                },
            },
        }
    )


def _qualified_profile(model_id: str) -> ScientificWorkloadProfile:
    value = copy.deepcopy(profile(model_id))
    identity = value["execution_identity"]
    assert isinstance(identity, dict)
    identity.update(
        {
            "runtime_image_digest": "sha256:" + "a" * 64,
            "runtime_recipe_sha256": "b" * 64,
            "workload_recipe_sha256": "c" * 64,
            "artifact_manifest_digest": "d" * 64,
            "execution_identity_sha256": "e" * 64,
        }
    )
    value["state"] = "qualified"
    value["route_exposed"] = True
    semantic = value["semantic_validation"]
    assert isinstance(semantic, dict)
    semantic["state"] = "qualified"
    value["runtime_artifacts"] = [
        {
            "artifact_id": item["artifact_id"],
            "content_digest_sha256": item["content_sha256"],
            "required_files": ["content.marker"],
            "file_manifest": [{"path": "content.marker", "sha256": item["content_sha256"], "size_bytes": 1}],
        }
        for item in contract(model_id)["runtime_artifacts"]  # type: ignore[union-attr]
    ]
    return ScientificWorkloadProfile(MappingProxyType(value))


def _profile_catalog(model_id: str) -> ScientificProfileCatalog:
    loaded = ScientificProfileCatalog.load(CATALOG_ROOT)
    return ScientificProfileCatalog(
        profiles={model_id: _qualified_profile(model_id)},
        validators=loaded._validators,  # type: ignore[attr-defined]
    )


def _quantity(value: int) -> str:
    gibibyte = 1024**3
    assert value % gibibyte == 0
    return f"{value // gibibyte}Gi"


def _execution_renderer(
    model_id: str, catalog: ScientificProfileCatalog, tmp_path: Path
) -> FileScientificManifestRenderer:
    model_profile = catalog.get(model_id)
    identity = model_profile.value["execution_identity"]
    assert isinstance(identity, Mapping)
    model_contract = contract(model_id)
    runtime_artifacts = model_contract["runtime_artifacts"]
    assert isinstance(runtime_artifacts, list)
    artifact_mounts = {item["artifact_id"]: item["mount_path"] for item in runtime_artifacts}
    stages = []
    for stage in model_profile.value["workload"]["stages"]:  # type: ignore[index]
        resources = stage["resources"]
        limits = resources["limits"]
        execution = MODULES[model_id].STAGE_EXECUTION_CONTRACTS[stage["id"]]
        stage_artifact_paths = [artifact_mounts[item] for item in execution["runtime_artifacts"]]
        mounts = [
            {
                "name": "artifact-workspace",
                "kind": "artifact-workspace",
                "claim_name": None,
                "host_path": None,
                "mount_path": "/mnt/fs2-scientific",
                "sub_path": None,
                "read_only": False,
            }
        ]
        if any(path == "/models" or path.startswith("/models/") for path in stage_artifact_paths):
            mounts.append(
                {
                    "name": "model-artifacts",
                    "kind": "reference",
                    "claim_name": "scientific-model-artifacts",
                    "host_path": None,
                    "mount_path": "/models",
                    "sub_path": None,
                    "read_only": True,
                }
            )
        if any(path == "/databases" or path.startswith("/databases/") for path in stage_artifact_paths):
            mounts.append(
                {
                    "name": "model-databases",
                    "kind": "reference",
                    "claim_name": "scientific-model-artifacts",
                    "host_path": None,
                    "mount_path": "/databases",
                    "sub_path": None,
                    "read_only": True,
                }
            )
        stages.append(
            {
                "stage_id": stage["id"],
                "image": f"registry.test/{model_id}@{identity['runtime_image_digest']}",
                "collector_id": execution["collector_id"],
                "validator_id": execution["validator_id"],
                "mounts": mounts,
                "service_account_name": "scientific-runner",
                "resources": {
                    "requests": {
                        "cpu": f"{resources['cpu_millis']}m",
                        "memory": _quantity(resources["memory_bytes"]),
                        "ephemeral_storage": _quantity(resources["ephemeral_storage_bytes"]),
                    },
                    "limits": {
                        "cpu": f"{limits['cpu_millis']}m",
                        "memory": _quantity(limits["memory_bytes"]),
                        "ephemeral_storage": _quantity(limits["ephemeral_storage_bytes"]),
                    },
                },
                "active_deadline_seconds": 86400,
                "termination_grace_seconds": 60,
                "environment": {},
                "required_node_labels": {},
            }
        )
    execution_map = {
        "schema": "fs2-serve.nebius.ai/scientific-execution-map/v3",
        "models": [
            {
                "model_id": model_id,
                "variant_id": MODULES[model_id].VARIANT_ID,
                "workload_namespace": "fs2-models",
                "execution_identity_sha256": identity["execution_identity_sha256"],
                "plan_adapter": {
                    "module": "fs2_serve.scientific_batch.adapters",
                    "function": "compile_adapter_run",
                },
                "runtime_artifacts": [
                    {
                        "artifact_id": item["artifact_id"],
                        "mount_path": item["mount_path"],
                        "content_digest": "sha256:" + item["content_sha256"],
                        "localization_receipt_digest": "sha256:" + "f" * 64,
                        "file_manifest": [
                            {"path": "content.marker", "sha256": item["content_sha256"], "size_bytes": 1}
                        ],
                    }
                    for item in runtime_artifacts
                ],
                "stages": stages,
            }
        ],
    }
    path = tmp_path / f"{model_id}-execution-map.json"
    path.write_text(json.dumps(execution_map, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return FileScientificManifestRenderer(
        path=path,
        profiles=catalog,
        tools_image="registry.test/control@sha256:" + "9" * 64,
        internal_api_url="http://control.fs2.svc:8080",
        capability_authority=ScientificWorkloadCapabilityAuthority(
            KeyedHasher(active_key_id="ledger-v1", keys={"ledger-v1": b"k" * 32})
        ),
    )


class _ArtifactRecords:
    def __init__(self, records: tuple[ArtifactRecord, ...]) -> None:
        self.records = {item.artifact_id: item for item in records}

    async def get_artifact(self, artifact_id: UUID, *, tenant_id: str) -> ArtifactRecord:
        record = self.records[artifact_id]
        assert record.tenant_id == tenant_id
        return record


class _BytesReader:
    def __init__(self, artifact_id: UUID, value: bytes) -> None:
        self.artifact_id = artifact_id
        self.value = value

    async def read(self, artifact_id: UUID, *, tenant_id: str, maximum_bytes: int) -> bytes:
        assert artifact_id == self.artifact_id
        assert tenant_id == "secondary-test"
        assert len(self.value) <= maximum_bytes
        return self.value


def _artifact_record(
    operation_id: UUID,
    artifact_id: UUID,
    value: bytes,
    media_type: str,
) -> ArtifactRecord:
    attempt_id = uuid4()
    digest = "sha256:" + hashlib.sha256(value).hexdigest()
    return ArtifactRecord(
        artifact_id=artifact_id,
        attempt_id=attempt_id,
        operation_id=operation_id,
        tenant_id="secondary-test",
        stage_id="input",
        shard_id=None,
        direction=ArtifactDirection.INPUT,
        digest=digest,
        size_bytes=len(value),
        media_type=media_type,
        storage_key=artifact_storage_key(
            tenant_id="secondary-test",
            operation_id=operation_id,
            stage_id="input",
            shard_id=None,
            attempt_id=attempt_id,
            direction=ArtifactDirection.INPUT,
            digest=digest,
        ),
        access=ArtifactAccess(),
        retention_expires_at=datetime(2027, 9, 4, tzinfo=UTC),
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
    )


async def _principal(store: MemoryStore, model_id: str) -> Principal:
    token_id = uuid4()
    scopes = {Scope.CATALOG_READ, Scope.INFERENCE_INVOKE, Scope.OPERATIONS_READ}
    await store.issue_token(
        token_id=token_id,
        prefix="fs2_pat_secondary",
        pepper_key_id="pepper-v1",
        digest="secondary-test-digest",
        request=TokenCreate(
            principal_id="secondary-test",
            tenant_id="secondary-test",
            scopes=scopes,
            models={model_id},
            max_concurrency=4,
        ),
        created_by="test",
    )
    return Principal(
        token_id=token_id,
        token_prefix="fs2_pat_secondary",
        principal_id="secondary-test",
        tenant_id="secondary-test",
        scopes=frozenset(str(item) for item in scopes),
        models=frozenset({model_id}),
        max_concurrency=4,
    )


def _argument(argv: tuple[str, ...], name: str) -> str:
    assert argv.count(name) == 1, argv
    return argv[argv.index(name) + 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", tuple(MODULES))
async def test_service_bridge_scheduler_controller_and_renderer_use_one_frozen_contract(
    model_id: str,
    tmp_path: Path,
    cipher,
    hasher,
) -> None:
    model_input = b'{"fixture":"verified-controller-input"}\n'
    input_operation, input_artifact_id, manifest_artifact_id = uuid4(), uuid4(), uuid4()
    input_record = _artifact_record(
        input_operation,
        input_artifact_id,
        model_input,
        MODULES[model_id].INPUT_MEDIA_TYPE,
    )
    input_manifest = {
        "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
        "manifest_id": f"{model_id}-request-inputs",
        "entries": [
            {
                "name": MODULES[model_id].INPUT_ARTIFACT_ID,
                "semantic_type": MODULES[model_id].INPUT_SEMANTIC_TYPE,
                "artifact": input_record.to_public_ref().model_dump(mode="json", exclude_none=True),
            }
        ],
    }
    manifest_bytes = json.dumps(input_manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_record = _artifact_record(
        input_operation,
        manifest_artifact_id,
        manifest_bytes,
        "application/vnd.fs2.scientific-manifest+json",
    )
    request = copy.deepcopy(fixture(model_id, POSITIVE_FIXTURES[model_id][0]))
    request["service_class"] = "customer-batch"
    request["input_manifest"] = manifest_record.to_public_ref().model_dump(mode="json", exclude_none=True)

    catalog = _profile_catalog(model_id)
    renderer = _execution_renderer(model_id, catalog, tmp_path)
    store = MemoryStore(cipher, hasher)
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()
    controller = ScientificBatchController(
        repository=repository,
        cluster=cluster,
        controller_id="secondary-test-controller",
        namespace="fs2-models",
    )
    bridge = ArtifactServiceBridge(
        artifacts=_ArtifactRecords((manifest_record, input_record)),  # type: ignore[arg-type]
        batches=repository,
        profiles=catalog,
        store=store,
        content_reader=_BytesReader(manifest_artifact_id, manifest_bytes),
    )
    service = ScientificBatchService(
        store=store,
        repository=repository,
        controller=controller,
        profiles=catalog,
        scheduling=scheduling(),
        artifacts=bridge,
        execution_binding=renderer,
        plan_factory=renderer,
    )
    submitted = await service.submit(
        principal=await _principal(store, model_id),
        model_id=model_id,
        request=request,
        idempotency_key=f"secondary-{model_id}-production-path",
    )
    operation_id = UUID(submitted["operation"]["id"])
    state = repository.records[operation_id]
    assert state.input_manifest is not None
    assert state.input_manifest.entries[0].artifact_id == input_artifact_id
    assert state.input_manifest.manifest_artifact_id == manifest_artifact_id
    assert state.execution_plan is not None
    first = state.execution_plan.invocations[0]
    assert first.materializations[0].artifact_id == MODULES[model_id].INPUT_ARTIFACT_ID
    assert first.materializations[0].artifact_id != str(manifest_artifact_id)

    assert await controller.reconcile_once() == operation_id
    resource = cluster.apply_history[0]
    assert resource.invocation == first
    rendered = renderer.render(resource)
    pod = rendered["spec"]["template"]["spec"]
    model_container = pod["containers"][0]
    assert model_container["command"] == list(first.argv)
    assert model_container["workingDir"] == first.working_directory
    materializer = pod["initContainers"][0]
    frozen_invocation = next(
        item["value"] for item in materializer["env"] if item["name"] == "FS2_STAGE_INVOCATION_JSON"
    )
    assert json.loads(frozen_invocation)["materializations"][0]["artifact_id"] == MODULES[model_id].INPUT_ARTIFACT_ID


@pytest.mark.parametrize("model_id", tuple(MODULES))
def test_explicit_stage_envelopes_freeze_against_cpu_and_accelerator_lanes(model_id: str) -> None:
    plan = compile_fixture(model_id, POSITIVE_FIXTURES[model_id][0]).controller_plan
    assert all(stage.placement_class is not None and stage.resources is not None for stage in plan.stages)

    snapshot = scheduling().freeze(
        service_class="customer-batch",
        model_id=model_id,
        tenant_id="secondary-adapter-test",
        profile=profile(model_id),
        plan=plan,
        workload_namespace="fs2-models",
    )
    cpu, gpu = snapshot.stages
    assert cpu.resource_class is ResourceClass.CPU
    assert cpu.resolved_local_queue == "general-cpu"
    assert cpu.resolved_cluster_queue == "general-cpu"
    assert cpu.resolved_pool_preference == ("general-cpu-8x",)
    assert gpu.resource_class is ResourceClass.GPU
    assert gpu.resolved_local_queue == "scientific"
    assert gpu.resolved_cluster_queue == "inference"
    assert gpu.resolved_pool_preference == ("h100-reserved-8x", "h100-1x")
    assert gpu.accelerator_resource_name == "nvidia.com/gpu"
    assert gpu.accelerator_count == 1


@pytest.mark.parametrize("model_id", tuple(MODULES))
def test_two_positive_fixtures_compile_to_cpu_then_gpu(model_id: str) -> None:
    for name in POSITIVE_FIXTURES[model_id]:
        plan = compile_fixture(model_id, name)
        assert plan.model_id == model_id
        assert plan.variant_id == MODULES[model_id].VARIANT_ID
        assert [stage.resource_class for stage in plan.controller_plan.stages] == [
            ResourceClass.CPU,
            ResourceClass.GPU,
        ]
        assert plan.controller_plan.stages[1].depends_on == (plan.controller_plan.stages[0].stage_id,)
        assert plan.invocations[0].runtime_artifacts == (
            (protenix_v2.MODEL_ARTIFACT,) if model_id == "protenix-v2" else ()
        )
        assert plan.invocations[1].runtime_artifacts == tuple(
            MODULES[model_id].STAGE_EXECUTION_CONTRACTS[plan.invocations[1].stage_id]["runtime_artifacts"]
        )


@pytest.mark.parametrize("model_id", tuple(MODULES))
def test_negative_fixture_fails_before_any_workload(model_id: str) -> None:
    with pytest.raises(ScientificAdapterError):
        compile_fixture(model_id, NEGATIVE_FIXTURES[model_id])


@pytest.mark.parametrize("model_id", tuple(MODULES))
def test_public_dispatch_is_explicit_and_variant_fenced(model_id: str) -> None:
    plan = compile_adapter_run(
        model_id,
        profile(model_id),
        fixture(model_id, POSITIVE_FIXTURES[model_id][0]),
        operation_id=f"op-{model_id}-dispatch",
        variant_id=MODULES[model_id].VARIANT_ID,
        input_artifacts=(verified_input(model_id),),
    )
    assert plan.model_id == model_id
    with pytest.raises(ScientificAdapterError, match="variant_id"):
        compile_adapter_run(
            model_id,
            profile(model_id),
            fixture(model_id, POSITIVE_FIXTURES[model_id][0]),
            operation_id=f"op-{model_id}-dispatch",
            variant_id="wrong-backend",
            input_artifacts=(verified_input(model_id),),
        )


@pytest.mark.parametrize("model_id", tuple(MODULES))
def test_verified_manifest_entry_is_the_only_controller_materialization_source(model_id: str) -> None:
    request = fixture(model_id, POSITIVE_FIXTURES[model_id][0])
    payload = verified_input(model_id)
    plan = compile_adapter_run(
        model_id,
        profile(model_id),
        request,
        operation_id=f"op-{model_id}-verified-materialization",
        variant_id=MODULES[model_id].VARIANT_ID,
        input_artifacts=(payload,),
    )
    first = plan.invocations[0]
    assert first.consumes == (payload.logical_artifact_id,)
    assert first.materializations[0].artifact_id == payload.logical_artifact_id
    assert first.materializations[0].artifact_id != request["input_manifest"]["artifact_id"]  # type: ignore[index]

    with pytest.raises(ScientificAdapterError, match="exactly one verified"):
        compile_adapter_run(
            model_id,
            profile(model_id),
            request,
            operation_id=f"op-{model_id}-missing-materialization",
            variant_id=MODULES[model_id].VARIANT_ID,
            input_artifacts=(),
        )
    with pytest.raises(ScientificAdapterError, match="semantic type"):
        compile_adapter_run(
            model_id,
            profile(model_id),
            request,
            operation_id=f"op-{model_id}-mistyped-materialization",
            variant_id=MODULES[model_id].VARIANT_ID,
            input_artifacts=(verified_input(model_id, semantic_type="wrong-input/v1"),),
        )
    aliased = copy.deepcopy(request)
    aliased["input_manifest"]["artifact_id"] = str(payload.artifact_id)  # type: ignore[index]
    with pytest.raises(ScientificAdapterError, match="distinct artifacts"):
        compile_adapter_run(
            model_id,
            profile(model_id),
            aliased,
            operation_id=f"op-{model_id}-manifest-alias",
            variant_id=MODULES[model_id].VARIANT_ID,
            input_artifacts=(payload,),
        )


def test_esmf2_variants_use_distinct_artifacts_and_fast_rejects_msa() -> None:
    full = compile_fixture("esmfold2", "positive-sequence").invocation("fold", "main")
    fast = compile_fixture("esmfold2-fast", "positive-short").invocation("fold", "main")
    assert _argument(full.argv, "--variant") == "esmfold2"
    assert _argument(fast.argv, "--variant") == "esmfold2-fast"
    assert _argument(full.argv, "--model-dir") == "/models/esmfold2"
    assert _argument(fast.argv, "--model-dir") == "/models/esmfold2-fast"
    assert set(full.runtime_artifacts) == {"esmfold2-trunk", "esmc-6b", "esmfold2-ccd"}
    assert set(fast.runtime_artifacts) == {"esmfold2-fast-trunk", "esmc-6b", "esmfold2-ccd"}
    with pytest.raises(ScientificAdapterError, match="rejects MSA"):
        compile_fixture("esmfold2-fast", "negative-msa")


def test_exact_r4_argv_and_cache_contracts() -> None:
    esm = compile_fixture("esmfold2", "positive-sequence")
    prepare, fold = esm.invocations
    assert prepare.argv[:2] == ("/usr/local/bin/fs2-run-esmfold2", "prepare-input")
    assert fold.argv[:2] == ("/usr/local/bin/fs2-run-esmfold2", "fold")
    assert _argument(fold.argv, "--hardware-mode") == "h100"
    assert _argument(fold.argv, "--esmc-precision") == "bf16"
    assert _argument(fold.argv, "--num-loops") == "20"
    assert _argument(fold.argv, "--num-sampling-steps") == "200"
    assert _argument(fold.argv, "--ccd-path") == "/databases/esmfold2/ccd.pkl"

    protenix = compile_fixture("protenix-v2", "positive-monomer")
    prep, pred = protenix.invocations
    assert prep.argv[:2] == ("/usr/local/bin/fs2-run-protenix", "prep")
    assert pred.argv[:2] == ("/usr/local/bin/fs2-run-protenix", "pred")
    assert _argument(pred.argv, "--seeds") == "7,19"
    assert _argument(pred.argv, "--sample-count") == "2"
    assert _argument(pred.argv, "--checkpoint") == "/models/protenix-v2/checkpoint/protenix-v2.pt"
    assert _argument(pred.argv, "--common-dir") == "/models/protenix-v2/common"
    assert {"--disable-templates", "--disable-rna-msa"} <= set(pred.argv)
    protenix_cache = {
        "TRITON_CACHE_DIR": "/cache/protenix/triton",
        "CUEQ_TRITON_CACHE_DIR": "/cache/protenix/cueq-triton",
        "TORCH_EXTENSIONS_DIR": "/cache/protenix/torch-extensions",
        "XDG_CACHE_HOME": "/cache/protenix/xdg",
    }
    assert protenix_cache.items() <= dict(pred.environment).items()

    openfold = compile_fixture("openfold3-openbind", "positive-complex")
    data, inference = openfold.invocations
    assert data.argv[:2] == ("/usr/local/bin/fs2-run-openfold3", "prepare")
    assert inference.argv[:2] == ("/usr/local/bin/fs2-run-openfold3", "predict")
    assert not data.runtime_artifacts
    assert _argument(inference.argv, "--checkpoint") == "/models/openfold3/of3-ob-2025-06-30-174k.pt"
    assert _argument(inference.argv, "--ccd-path") == "/databases/openfold3/components.bcif"
    assert _argument(inference.argv, "--num-model-seeds") == "2"
    assert _argument(inference.argv, "--model-seeds") == "13,31"
    assert _argument(inference.argv, "--use-templates") == "false"
    openfold_cache = {
        "TRITON_CACHE_DIR": "/cache/openfold3/triton",
        "TORCH_EXTENSIONS_DIR": "/cache/openfold3/torch-extensions",
        "XDG_CACHE_HOME": "/cache/openfold3/xdg",
    }
    assert openfold_cache.items() <= dict(inference.environment).items()

    for plan in (esm, protenix, openfold):
        for invocation in plan.invocations:
            assert invocation.argv[0] not in {"sh", "bash", "/bin/sh", "/bin/bash"}
            assert not any(token in {"-c", ";", "&&", "|"} or "$(" in token for token in invocation.argv)
            stage_contract = MODULES[plan.model_id].STAGE_EXECUTION_CONTRACTS[invocation.stage_id]
            environment = dict(invocation.environment)
            assert environment["FS2_SCIENTIFIC_COLLECTOR_ID"] == stage_contract["collector_id"]
            assert environment["FS2_SCIENTIFIC_VALIDATOR_ID"] == stage_contract["validator_id"]


def _load_wrapper(root: Path, filename: str) -> ModuleType:
    sys.path.insert(0, str(root))
    try:
        spec = importlib.util.spec_from_file_location(f"fs2_secondary_contract_{filename}", root / f"{filename}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(root))


def _parse_wrapper_argv(wrapper: ModuleType, argv: tuple[str, ...]):
    handlers = {
        "prepare-input": "_prepare",
        "fold": "_fold",
        "prep": "_prep",
        "pred": "_pred",
        "prepare": "_prepare",
        "predict": "_predict",
    }
    handler = handlers[argv[1]]
    with mock.patch.object(wrapper, handler) as parsed_handler:
        if len(inspect.signature(wrapper.main).parameters) == 0:
            with mock.patch.object(sys, "argv", [argv[0], *argv[1:]]):
                wrapper.main()
        else:
            wrapper.main(list(argv[1:]))
        assert parsed_handler.call_count == 1
        return parsed_handler.call_args.args[0]


def _marker_artifacts(model_id: str, invocation) -> list[dict[str, object]]:
    by_id = {item["artifact_id"]: item for item in contract(model_id)["runtime_artifacts"] if isinstance(item, dict)}
    receipt = "d" * 64
    return [
        {
            "artifact_id": artifact_id,
            "mount_path": by_id[artifact_id]["mount_path"],
            "content_digest": f"sha256:{by_id[artifact_id]['content_sha256']}",
            "localization_receipt_digest": f"sha256:{receipt}",
            "sub_path": None,
            "expected_manifest_sha256": by_id[artifact_id].get("localization_manifest_sha256"),
            "readiness_receipt_sha256": receipt,
            "authorization_receipt_sha256": None,
        }
        for artifact_id in invocation.runtime_artifacts
    ]


@pytest.mark.skipif(
    not os.environ.get(WRAPPER_ROOT_ENV),
    reason=f"set {WRAPPER_ROOT_ENV} to the exact r4 wrapper source",
)
def test_every_generated_argv_cross_runs_through_exact_r4_parsers_and_markers(tmp_path: Path) -> None:
    root = Path(os.environ[WRAPPER_ROOT_ENV]).resolve()
    source_commit = json.loads(IMAGE_HANDOFF.read_text(encoding="utf-8"))["image_source_commit"]
    git = shutil.which("git")
    assert git is not None
    repository = Path(
        subprocess.run(  # noqa: S603 - fixed git executable and an operator-supplied local path
            [git, "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    relative = root.relative_to(repository)
    parser_sources = (
        "run_esmfold2.py",
        "run_protenix.py",
        "run_openfold3.py",
        "handoff_contract.py",
        "result_contract.py",
        "runtime_localization.py",
    )
    subprocess.run(  # noqa: S603 - fixed git executable and validated immutable revision
        [
            git,
            "-C",
            str(repository),
            "diff",
            "--quiet",
            source_commit,
            "--",
            *(str(relative / filename) for filename in parser_sources),
        ],
        check=True,
    )
    wrappers = {
        "esmfold2": _load_wrapper(root, "run_esmfold2"),
        "esmfold2-fast": _load_wrapper(root, "run_esmfold2"),
        "protenix-v2": _load_wrapper(root, "run_protenix"),
        "openfold3-openbind": _load_wrapper(root, "run_openfold3"),
    }
    for model_id, wrapper in wrappers.items():
        plan = compile_fixture(model_id, POSITIVE_FIXTURES[model_id][0])
        for invocation in plan.invocations:
            parsed = _parse_wrapper_argv(wrapper, invocation.argv)
            if not invocation.runtime_artifacts:
                continue
            marker_path = tmp_path / f"{model_id}-{invocation.stage_id}.json"
            marker = {
                "schema": "fs2-serve.nebius.ai/runtime-localization-marker/v1",
                "operation_id": "00000000-0000-4000-8000-000000000010",
                "attempt_id": "00000000-0000-4000-8000-000000000011",
                "tenant_id": "adapter-contract-tenant",
                "model_id": model_id,
                "variant_id": plan.variant_id,
                "stage_id": invocation.stage_id,
                "artifacts": _marker_artifacts(model_id, invocation),
            }
            marker_path.write_text(json.dumps(marker, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            parsed.runtime_localization_marker = str(marker_path)
            environment = {
                "FS2_OPERATION_ID": marker["operation_id"],
                "FS2_ATTEMPT_ID": marker["attempt_id"],
                "FS2_TENANT_ID": marker["tenant_id"],
                "FS2_VARIANT_ID": plan.variant_id,
                "FS2_STAGE_ID": invocation.stage_id,
                "FS2_RUNTIME_LOCALIZATION_MARKER": str(marker_path),
                "FS2_ARTIFACT_ACCESS_RECEIPT_DIGEST": "",
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                validated = wrapper._validate_runtime_localization_args(invocation.argv[1], parsed)
            assert validated["model_id"] == model_id


def _mmcif(seed: int, sample: int) -> bytes:
    rows = [
        f"ATOM {index} C CA A {index + seed / 1000:.3f} {index + sample / 1000:.3f} {index + 1:.3f}"
        for index in range(1, 11)
    ]
    return (
        "data_prediction\n#\nloop_\n_atom_site.group_PDB\n_atom_site.id\n"
        "_atom_site.type_symbol\n_atom_site.label_atom_id\n_atom_site.label_asym_id\n"
        "_atom_site.Cartn_x\n_atom_site.Cartn_y\n_atom_site.Cartn_z\n" + "\n".join(rows) + "\n#\n"
    ).encode("ascii")


def _write_confidence_workspace(
    tmp_path: Path,
    *,
    runtime_id: str,
    model_revision: str,
    seeds: tuple[int, ...],
    samples: int,
) -> Path:
    workspace = tmp_path / runtime_id
    outputs = workspace / "outputs"
    outputs.mkdir(parents=True)
    results = []
    for seed in seeds:
        for sample in range(samples):
            structure = outputs / f"prediction-{seed}-{sample}.cif"
            structure.write_bytes(_mmcif(seed, sample))
            summary = outputs / f"summary-{seed}-{sample}.json"
            summary.write_text(json.dumps({"seed": seed, "sample": sample}) + "\n", encoding="utf-8")
            results.append(
                {
                    "seed": seed,
                    "sample_index": sample,
                    "upstream_summary": summary.name,
                    "structure": {
                        "filename": structure.name,
                        "sha256": hashlib.sha256(structure.read_bytes()).hexdigest(),
                        "bytes": structure.stat().st_size,
                    },
                    "metrics": {"plddt": 87.5, "ptm": 0.71},
                }
            )
    envelope = {
        "schema": "fs2.nebius.ai/structure-confidence/v1",
        "runtime_id": runtime_id,
        "model_revision": model_revision,
        "seeds": list(seeds),
        "samples_per_seed": samples,
        "results": results,
    }
    (outputs / "confidence.json").write_text(json.dumps(envelope, sort_keys=True) + "\n", encoding="utf-8")
    return workspace


@pytest.mark.parametrize("model_id", tuple(MODULES))
def test_result_collectors_validate_and_publish_the_exact_closure(model_id: str, tmp_path: Path) -> None:
    request = fixture(model_id, POSITIVE_FIXTURES[model_id][0])
    module = MODULES[model_id]
    if model_id in {"esmfold2", "esmfold2-fast"}:
        parameters = esmfold2.Parameters.parse(request["parameters"], fast=model_id.endswith("-fast"))
        seeds, samples = (parameters.seed,), 1
        revision = module.MODEL_REVISION
    elif model_id == "protenix-v2":
        parameters = protenix_v2.Parameters.parse(request["parameters"])
        seeds, samples = parameters.seeds, parameters.sample_count
        revision = protenix_v2.OUTPUT_MODEL_REVISION
    else:
        parameters = openfold3.Parameters.parse(request["parameters"])
        seeds, samples = parameters.seeds, 1
        revision = openfold3.SOURCE_REVISION
    workspace = _write_confidence_workspace(
        tmp_path,
        runtime_id=getattr(module, "RUNTIME_ID", model_id),
        model_revision=revision,
        seeds=seeds,
        samples=samples,
    )
    first = module.collect_stage_output(module.RESULT_COLLECTOR_ID, request, workspace)
    second = module.collect_stage_output(module.RESULT_COLLECTOR_ID, request, workspace)
    assert first == second
    entries = first.manifest["entries"]
    assert isinstance(entries, list)
    assert len(entries) == len(seeds) * samples * 2 + 1
    assert {entry["name"] for entry in entries} >= {"confidence", f"prediction.{seeds[0]}.0"}
    assert len(first.blobs) == len(entries)
    reopened = load_output_manifest(
        first.manifest,
        artifact_loader=lambda artifact_id: first.blobs[artifact_id],
        maximum_entries=64,
        maximum_total_bytes=8 * 1024 * 1024 * 1024,
    )
    assert len(reopened) == len(entries)

    envelope_path = workspace / "outputs/confidence.json"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["results"][0]["metrics"]["plddt"] = 101
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ScientificAdapterError, match="metric"):
        module.collect_stage_output(module.RESULT_COLLECTOR_ID, request, workspace)


@pytest.mark.parametrize("model_id", tuple(MODULES))
def test_prepare_collectors_are_deterministic_and_unknown_ids_fail(model_id: str, tmp_path: Path) -> None:
    module = MODULES[model_id]
    request = fixture(model_id, POSITIVE_FIXTURES[model_id][0])
    workspace = tmp_path / model_id
    workspace.mkdir()
    if model_id in {"esmfold2", "esmfold2-fast"}:
        filename, content = "prepared-input.json", b'{"sequences":[]}\n'
    else:
        filename, content = "handoff.tar.zst", b"deterministic-stage-handoff"
    (workspace / filename).write_bytes(content)
    first = module.collect_stage_output(module.PREPARE_COLLECTOR_ID, request, workspace)
    second = module.collect_stage_output(module.PREPARE_COLLECTOR_ID, request, workspace)
    assert first == second
    with pytest.raises(ScientificAdapterError, match="unsupported"):
        module.collect_stage_output("unknown-collector-v1", request, workspace)


@pytest.mark.parametrize("model_id", tuple(MODULES))
def test_companion_registry_collects_frozen_prepare_and_result_invocations(model_id: str, tmp_path: Path) -> None:
    module = MODULES[model_id]
    request = fixture(model_id, POSITIVE_FIXTURES[model_id][0])
    plan = compile_fixture(model_id, POSITIVE_FIXTURES[model_id][0])
    prepare, result = plan.invocations

    prepare_workspace = tmp_path / f"{model_id}-prepare"
    prepare_workspace.mkdir()
    if model_id in {"esmfold2", "esmfold2-fast"}:
        filename, content = "prepared-input.json", b'{"sequences":[]}\n'
    else:
        filename, content = "handoff.tar.zst", b"deterministic-stage-handoff"
    (prepare_workspace / filename).write_bytes(content)
    prepared = collect_registered_stage_output(prepare, prepare_workspace)
    assert isinstance(prepared, CollectedStageOutput)
    assert prepared.validation["validator_id"] == prepare.validator_id
    assert tuple(item.name for item in prepared.artifacts) == (prepare.handoff_name,)

    if model_id in {"esmfold2", "esmfold2-fast"}:
        parameters = esmfold2.Parameters.parse(request["parameters"], fast=model_id.endswith("-fast"))
        seeds, samples, revision = (parameters.seed,), 1, module.MODEL_REVISION
    elif model_id == "protenix-v2":
        parameters = protenix_v2.Parameters.parse(request["parameters"])
        seeds, samples, revision = parameters.seeds, parameters.sample_count, module.OUTPUT_MODEL_REVISION
    else:
        parameters = openfold3.Parameters.parse(request["parameters"])
        seeds, samples, revision = parameters.seeds, 1, module.SOURCE_REVISION
    result_workspace = _write_confidence_workspace(
        tmp_path,
        runtime_id=getattr(module, "RUNTIME_ID", model_id),
        model_revision=revision,
        seeds=seeds,
        samples=samples,
    )
    collected = collect_registered_stage_output(result, result_workspace)
    assert isinstance(collected, CollectedStageOutput)
    assert collected.validation["validator_id"] == result.validator_id
    assert {item.name for item in collected.artifacts} >= {"confidence", f"prediction.{seeds[0]}.0"}

    missing = tmp_path / f"{model_id}-pending"
    missing.mkdir()
    with pytest.raises(CollectionPendingError):
        collect_registered_stage_output(prepare, missing)


@pytest.mark.parametrize("model_id", tuple(MODULES))
def test_production_companion_publishes_result_manifest_and_validation(model_id: str, tmp_path: Path) -> None:
    module = MODULES[model_id]
    request = fixture(model_id, POSITIVE_FIXTURES[model_id][0])
    result = compile_fixture(model_id, POSITIVE_FIXTURES[model_id][0]).invocations[1]
    if model_id in {"esmfold2", "esmfold2-fast"}:
        parameters = esmfold2.Parameters.parse(request["parameters"], fast=model_id.endswith("-fast"))
        seeds, samples, revision = (parameters.seed,), 1, module.MODEL_REVISION
    elif model_id == "protenix-v2":
        parameters = protenix_v2.Parameters.parse(request["parameters"])
        seeds, samples, revision = parameters.seeds, parameters.sample_count, module.OUTPUT_MODEL_REVISION
    else:
        parameters = openfold3.Parameters.parse(request["parameters"])
        seeds, samples, revision = parameters.seeds, 1, module.SOURCE_REVISION
    workspace = _write_confidence_workspace(
        tmp_path,
        runtime_id=getattr(module, "RUNTIME_ID", model_id),
        model_revision=revision,
        seeds=seeds,
        samples=samples,
    )

    class Client:
        def __init__(self) -> None:
            self.uploads: dict[str, tuple[bytes, dict[str, object]]] = {}

        def upload(self, *, identity, content, media_type, compression):
            pointer: dict[str, object] = {
                "artifact_id": str(uuid4()),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "media_type": media_type,
            }
            if compression is not None:
                pointer["compression"] = compression
            self.uploads[identity] = (content, pointer)
            return pointer

    client = Client()
    companion.collect_and_commit(
        client=client,  # type: ignore[arg-type]
        collector_id=result.collector_id,
        validator_id=result.validator_id,
        invocation_json=_invocation_json(result),
        workspace=workspace,
        catalog_dir=CATALOG_ROOT,
        collection_deadline_seconds=30,
        poll_seconds=0.001,
        max_artifacts=result.max_output_artifacts,
        max_output_bytes=result.max_output_bytes,
    )
    manifest = json.loads(client.uploads[f"{result.produces}:manifest"][0])
    validation = json.loads(client.uploads[f"{result.produces}:validation"][0])
    assert manifest["manifest_id"] == result.produces
    assert {entry["name"] for entry in manifest["entries"]} >= {
        "confidence",
        f"prediction.{seeds[0]}.0",
    }
    assert validation["status"] == "passed"
    assert validation["collector_id"] == result.collector_id
    assert validation["validator_id"] == result.validator_id
    assert validation["logical_output_id"] == result.produces


def test_contract_documents_match_code_and_the_exact_r4_handoff() -> None:
    handoff = json.loads(IMAGE_HANDOFF.read_text(encoding="utf-8"))
    assert handoff["state"] == "build-only-not-activated"
    assert handoff["semantic_h100_qualification"] is False
    assert handoff["route_activation_allowed"] is False
    assert "alphafold3" not in {item["model_id"] for item in handoff["images"]}
    images = {item["model_id"]: item for item in handoff["images"]}
    assert set(images) == set(MODULES)
    for model_id, module in MODULES.items():
        value = contract(model_id)
        assert value["model_id"] == module.MODEL_ID
        assert value["variant_id"] == module.VARIANT_ID
        assert value["source"]["repository"] == module.SOURCE_REPOSITORY
        assert value["source"]["revision"] == module.SOURCE_REVISION
        assert value["activation"] == {
            "profile_state": "candidate-unqualified",
            "route_exposed": False,
            "semantic_h100_qualified": False,
        }
        assert value["runtime_image"]["repository"] == images[model_id]["repository"]
        assert value["runtime_image"]["tag"] == images[model_id]["tag"]
        assert value["runtime_image"]["digest"] == images[model_id]["digest"]
        assert value["runtime_image"]["state"] == "build-only-not-semantic-qualified"
        assert {
            stage["stage_id"]: {
                "collector_id": stage["collector_id"],
                "validator_id": stage["validator_id"],
                "runtime_artifacts": tuple(stage["runtime_artifacts"]),
            }
            for stage in value["stages"]
        } == dict(module.STAGE_EXECUTION_CONTRACTS)


def test_exact_published_receipts_match_the_committed_image_handoff_when_available() -> None:
    handoff = json.loads(IMAGE_HANDOFF.read_text(encoding="utf-8"))
    root = Path(handoff["evidence"]["source"])
    if not root.is_absolute():
        # The committed handoff intentionally records a home-relative path so the
        # public export contains no developer username; resolve it against the
        # operator's own home directory rather than the process cwd.
        root = Path.home() / root
    if not root.is_dir():
        pytest.skip("operator-local r4 publication receipts are not present")
    for image in handoff["images"]:
        evidence_directory = image.get("evidence_directory", image["model_id"])
        receipt = json.loads((root / evidence_directory / "build-receipt.json").read_text(encoding="utf-8"))
        entries = receipt["images"]
        assert len(entries) == 1
        assert entries[0]["target"] == f"{image['repository']}:{image['tag']}"
        assert entries[0]["published_digest"] == image["digest"]
        assert receipt["image_source_revision"] == handoff["image_source_commit"]

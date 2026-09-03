from __future__ import annotations

import hashlib
import io
import json
import tarfile
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest
from conftest import CATALOG_ROOT
from scientific_batch_fakes import FakeScientificBatchCluster, FakeScientificBatchRepository

from fs2_serve.crypto import KeyedHasher
from fs2_serve.scientific_artifacts import (
    ArtifactAccess,
    ArtifactAccessProfile,
    ArtifactDirection,
    ArtifactRecord,
    artifact_storage_key,
)
from fs2_serve.scientific_batch import adapters as scientific_adapters
from fs2_serve.scientific_batch import companion
from fs2_serve.scientific_batch.adapters import (
    CollectedArtifactFile,
    CollectedStageOutput,
    register_adapter,
)
from fs2_serve.scientific_batch.adapters import alphafold3 as alphafold3_adapter
from fs2_serve.scientific_batch.artifact_bridge import ArtifactServiceBridge
from fs2_serve.scientific_batch.capability import ScientificWorkloadCapabilityAuthority
from fs2_serve.scientific_batch.codec import state_from_value, state_to_value
from fs2_serve.scientific_batch.controller import ScientificBatchController
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer, ScientificExecutionMapError
from fs2_serve.scientific_batch.models import (
    AdapterExecutionPlan,
    ArtifactAccessContext,
    ArtifactMaterialization,
    AttemptArtifactCommit,
    LifecyclePhase,
    MaterializationMode,
    ResolvedArtifactMaterialization,
    ResourceClass,
    RuntimeArtifactFile,
    RuntimeArtifactLocalization,
    RuntimeArtifactMount,
    SchedulingSnapshot,
    ScientificBatchPlan,
    ScientificBatchState,
    ScientificInputArtifact,
    ScientificStagePlan,
    ServiceClass,
    StageInvocation,
    StageSchedulingDecision,
    VerifiedInputManifest,
    WorkloadKind,
    WorkloadObservation,
    WorkloadResource,
    WorkloadState,
)
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog, ScientificWorkloadProfile
from fs2_serve.scientific_batch.scheduling import SchedulingContractError, SchedulingContractResolver

NOW = datetime(2026, 9, 2, 21, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[3]
AF3_TREE_SHA256 = "c" * 64
AF3_MANIFEST_SHA256 = "d" * 64
AF3_DATASET_RELATIVE_PATH = (
    f"datasets/alphafold3-public-databases-v3.0/v3.0-paper-snapshot-2022-09-28/sha256/{AF3_TREE_SHA256}"
)
AF3_DATASET_URI = f"file:///mnt/fs2-reference-data/data/{AF3_DATASET_RELATIVE_PATH}"


def sha(value: bytes | str) -> str:
    raw = value.encode() if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class ArtifactRecords:
    def __init__(self, records: tuple[ArtifactRecord, ...]) -> None:
        self.records = {item.artifact_id: item for item in records}

    async def get_artifact(self, artifact_id: UUID, *, tenant_id: str) -> ArtifactRecord:
        record = self.records[artifact_id]
        if record.tenant_id != tenant_id:
            raise KeyError(artifact_id)
        return record


class BytesReader:
    def __init__(self, values: dict[UUID, bytes]) -> None:
        self.values = values

    async def read(self, artifact_id: UUID, *, tenant_id: str, maximum_bytes: int) -> bytes:
        assert tenant_id == "academic-poc"
        value = self.values[artifact_id]
        assert len(value) <= maximum_bytes
        return value


def artifact(
    operation_id: UUID,
    artifact_id: UUID,
    value: bytes,
    media_type: str,
    access: ArtifactAccess,
) -> ArtifactRecord:
    digest = sha(value)
    attempt_id = uuid4()
    return ArtifactRecord(
        artifact_id=artifact_id,
        attempt_id=attempt_id,
        operation_id=operation_id,
        tenant_id="academic-poc",
        stage_id="input",
        shard_id=None,
        direction=ArtifactDirection.INPUT,
        digest=digest,
        size_bytes=len(value),
        media_type=media_type,
        storage_key=artifact_storage_key(
            tenant_id="academic-poc",
            operation_id=operation_id,
            stage_id="input",
            shard_id=None,
            attempt_id=attempt_id,
            direction=ArtifactDirection.INPUT,
            digest=digest,
        ),
        access=access,
        retention_expires_at=NOW.replace(year=2027),
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_input_manifest_resolves_and_verifies_contained_logical_artifacts() -> None:
    operation_id = uuid4()
    receipt = sha("academic-access")
    access = ArtifactAccess(profile=ArtifactAccessProfile.ACADEMIC, receipt_digest=receipt)
    request_id, a3m_id, manifest_id = uuid4(), uuid4(), uuid4()
    request_bytes, a3m_bytes = b'{"sequences":[]}', b">query\nACDE\n"
    request_record = artifact(operation_id, request_id, request_bytes, "application/json", access)
    a3m_record = artifact(operation_id, a3m_id, a3m_bytes, "text/plain", access)
    manifest = {
        "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
        "manifest_id": "af3-inputs",
        "entries": [
            {
                "name": "request-json",
                "semantic_type": "alphafold-input/v1",
                "artifact": request_record.to_public_ref().model_dump(mode="json", exclude_none=True),
            },
            {
                "name": "optional-a3m",
                "semantic_type": "protein-msa/v1",
                "artifact": a3m_record.to_public_ref().model_dump(mode="json", exclude_none=True),
            },
        ],
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_record = artifact(
        operation_id,
        manifest_id,
        manifest_bytes,
        "application/vnd.fs2.scientific-manifest+json",
        access,
    )
    bridge = ArtifactServiceBridge(
        artifacts=ArtifactRecords((manifest_record, request_record, a3m_record)),  # type: ignore[arg-type]
        batches=object(),  # type: ignore[arg-type]
        profiles=ScientificProfileCatalog.load(CATALOG_ROOT),
        store=object(),  # type: ignore[arg-type]
        content_reader=BytesReader({manifest_id: manifest_bytes}),
    )
    admitted = await bridge.validate_input(
        manifest_record.to_public_ref().model_dump(mode="json", exclude_none=True),
        tenant_id="academic-poc",
    )
    assert admitted.access_context == ArtifactAccessContext(
        profile="academic", receipt_digest=receipt, tenant_id="academic-poc"
    )
    assert tuple(item.logical_artifact_id for item in admitted.manifest.entries) == (
        "request-json",
        "optional-a3m",
    )
    assert admitted.manifest.artifact("optional-a3m").artifact_id == a3m_id


def runtime_profile() -> ScientificWorkloadProfile:
    files = [
        {"path": name, "sha256": hashlib.sha256(name.encode()).hexdigest(), "size_bytes": index + 1}
        for index, name in enumerate(
            (
                "components.cif",
                "components.cif.rdkit_mol.pkl",
                "clusters-by-entity-40.txt",
                "obsolete_release_date.csv",
            )
        )
    ]
    return ScientificWorkloadProfile(
        MappingProxyType(
            {
                "model_id": "protenix-v2",
                "state": "qualified",
                "route_exposed": True,
                "execution_identity": {
                    "model_revision": "b" * 40,
                    "runtime_image_digest": "sha256:" + "a" * 64,
                    "runtime_recipe_sha256": "1" * 64,
                    "workload_recipe_sha256": "2" * 64,
                    "artifact_manifest_digest": "3" * 64,
                    "execution_identity_sha256": "f" * 64,
                },
                "access": {"state": "not-required"},
                "semantic_validation": {"state": "qualified"},
                "artifact_requirements": [
                    {
                        "artifact_id": "protenix-common",
                        "content_digest_sha256": "c" * 64,
                        "required_files": [item["path"] for item in files],
                        "file_manifest": files,
                    }
                ],
                "workload": {
                    "stages": [
                        {
                            "id": "prepare",
                        },
                        {
                            "id": "inference",
                        },
                    ]
                },
            }
        )
    )


def runtime_execution_map(tmp_path: Path, *, omit_file: bool = False) -> FileScientificManifestRenderer:
    profile = runtime_profile()
    file_names = (
        "components.cif",
        "components.cif.rdkit_mol.pkl",
        "clusters-by-entity-40.txt",
        "obsolete_release_date.csv",
    )
    files = [
        {"path": name, "sha256": hashlib.sha256(name.encode()).hexdigest(), "size_bytes": index + 1}
        for index, name in enumerate(file_names[:-1] if omit_file else file_names)
    ]
    value = {
        "schema": "fs2-serve.nebius.ai/scientific-execution-map/v3",
        "controller_service_account": {
            "namespace": "fs2-system",
            "name": "fs2-serve-control-plane-runtime",
        },
        "models": [
            {
                "model_id": "protenix-v2",
                "variant_id": "upstream-v2-0-0",
                "execution_identity_sha256": "f" * 64,
                "plan_adapter": {
                    "module": "fs2_serve.scientific_batch.adapters",
                    "function": "compile_adapter_run",
                },
                "runtime_artifacts": [
                    {
                        "artifact_id": "protenix-common",
                        "mount_path": "/models/protenix-v2/common",
                        "content_digest": "sha256:" + "c" * 64,
                        "file_manifest": files,
                        "localization_receipt_digest": sha("localized-protenix-common"),
                    }
                ],
                "stages": [
                    {
                        "stage_id": "prepare",
                        "execution_namespace": "fs2-models",
                        "local_queue_name": "scientific",
                        "image": "registry.test/protenix@sha256:" + "a" * 64,
                        "collector_id": "protenix-prepared-v1",
                        "validator_id": "protenix-prepared-validator-v1",
                        "mounts": [
                            {
                                "name": "artifact-workspace",
                                "kind": "artifact-workspace",
                                "artifact_id": None,
                                "claim_name": None,
                                "claim_namespace": None,
                                "host_path": None,
                                "operator_owned": False,
                                "mount_path": "/mnt/fs2-scientific",
                                "sub_path": None,
                                "read_only": False,
                            },
                            {
                                "name": "model-artifacts",
                                "kind": "reference",
                                "artifact_id": "protenix-common",
                                "claim_name": "scientific-model-artifacts",
                                "claim_namespace": "fs2-models",
                                "host_path": None,
                                "operator_owned": True,
                                "mount_path": "/models",
                                "sub_path": None,
                                "read_only": True,
                            },
                        ],
                        "service_account_name": "scientific-runner",
                        "resources": {"cpu": "4", "memory": "32Gi", "ephemeral_storage": "20Gi"},
                        "active_deadline_seconds": 3600,
                        "termination_grace_seconds": 60,
                        "environment": {},
                    },
                    {
                        "stage_id": "inference",
                        "execution_namespace": "fs2-models",
                        "local_queue_name": "scientific",
                        "image": "registry.test/protenix@sha256:" + "a" * 64,
                        "collector_id": "protenix-results-v1",
                        "validator_id": "protenix-validator-v1",
                        "mounts": [
                            {
                                "name": "artifact-workspace",
                                "kind": "artifact-workspace",
                                "artifact_id": None,
                                "claim_name": None,
                                "claim_namespace": None,
                                "host_path": None,
                                "operator_owned": False,
                                "mount_path": "/mnt/fs2-scientific",
                                "sub_path": None,
                                "read_only": False,
                            },
                            {
                                "name": "model-artifacts",
                                "kind": "reference",
                                "artifact_id": "protenix-common",
                                "claim_name": "scientific-model-artifacts",
                                "claim_namespace": "fs2-models",
                                "host_path": None,
                                "operator_owned": True,
                                "mount_path": "/models",
                                "sub_path": None,
                                "read_only": True,
                            },
                        ],
                        "service_account_name": "scientific-runner",
                        "resources": {"cpu": "4", "memory": "32Gi", "ephemeral_storage": "20Gi"},
                        "active_deadline_seconds": 3600,
                        "termination_grace_seconds": 60,
                        "environment": {},
                    },
                ],
            }
        ],
    }
    path = tmp_path / ("missing.json" if omit_file else "complete.json")
    path.write_text(json.dumps(value))
    catalog = ScientificProfileCatalog(
        profiles={profile.model_id: profile},
        validators=ScientificProfileCatalog.load(CATALOG_ROOT)._validators,  # type: ignore[attr-defined]
    )
    return FileScientificManifestRenderer(
        path=path,
        profiles=catalog,
        tools_image="registry.test/control@sha256:" + "9" * 64,
        internal_api_url="http://control.fs2.svc:8080",
        capability_authority=ScientificWorkloadCapabilityAuthority(
            KeyedHasher(active_key_id="ledger-v1", keys={"ledger-v1": b"k" * 32})
        ),
    )


def af3_qualified_profile() -> tuple[ScientificWorkloadProfile, dict[str, object]]:
    profile_set = json.loads((CATALOG_ROOT / "contracts/scientific-workload-profiles.json").read_text())
    value = deepcopy(next(item for item in profile_set["profiles"] if item["model_id"] == "alphafold3"))
    value["state"] = "qualified"
    value["route_exposed"] = True
    value["access"]["state"] = "verified"
    value["semantic_validation"]["state"] = "qualified"
    value["execution_identity"].update(
        {
            "runtime_image_digest": "sha256:" + "a" * 64,
            "runtime_image_state": "qualified",
            "artifact_manifest_digest": "8" * 64,
            "execution_identity_sha256": "9" * 64,
        }
    )
    reference = next(
        item for item in value["artifact_requirements"] if item["artifact_id"] == "alphafold3-public-databases-v3.0"
    )
    reference.update(
        {
            "content_digest_sha256": AF3_TREE_SHA256,
            "localization_manifest_sha256": AF3_MANIFEST_SHA256,
            "required_files": [".fs2-manifest-sha256"],
            "aggregate_tree": {
                "kind": "aggregate-tree",
                "dataset_relative_path": AF3_DATASET_RELATIVE_PATH,
                "dataset_uri": AF3_DATASET_URI,
                "file_count": 5001,
            },
            "total_size_bytes": 2 * 1024**4,
            "supply_state": "fixture-promoted",
        }
    )
    parameters = next(item for item in value["artifact_requirements"] if item["artifact_id"] == "alphafold3-parameters")
    parameters["localization_manifest_sha256"] = "f" * 64
    profile = ScientificWorkloadProfile(MappingProxyType(value))
    return profile, value


def af3_execution_value(profile_value: dict[str, object]) -> dict[str, object]:
    requirements = {
        item["artifact_id"]: item
        for item in profile_value["artifact_requirements"]  # type: ignore[index]
    }
    stage_common = {
        "image": "registry.test/alphafold3@sha256:" + "a" * 64,
        "execution_namespace": "fs2-academic-poc",
        "local_queue_name": "academic-scientific",
        "cluster_queue_name": "inference-accelerators",
        "service_account_name": "fs2-academic-runner",
        "resources": {"cpu": "8", "memory": "64Gi", "ephemeral_storage": "64Gi"},
        "active_deadline_seconds": 3600,
        "termination_grace_seconds": 60,
        "environment": {"FS2_NETWORK_MODE": "offline"},
    }
    workspace = {
        "name": "artifact-workspace",
        "kind": "artifact-workspace",
        "artifact_id": None,
        "claim_name": None,
        "claim_namespace": None,
        "host_path": None,
        "operator_owned": False,
        "mount_path": "/mnt/fs2-scientific",
        "sub_path": None,
        "read_only": False,
    }
    return {
        "schema": "fs2-serve.nebius.ai/scientific-execution-map/v3",
        "controller_service_account": {
            "namespace": "fs2-system",
            "name": "fs2-serve-control-plane-runtime",
        },
        "models": [
            {
                "model_id": "alphafold3",
                "variant_id": "upstream-v3-0-4",
                "execution_identity_sha256": "9" * 64,
                "plan_adapter": {
                    "module": "fs2_serve.scientific_batch.adapters",
                    "function": "compile_adapter_run",
                },
                "runtime_artifacts": [
                    {
                        "artifact_id": "alphafold3-public-databases-v3.0",
                        "mount_path": "/databases",
                        "content_digest": "sha256:" + AF3_TREE_SHA256,
                        "aggregate_tree": {
                            "manifest_digest": "sha256:" + AF3_MANIFEST_SHA256,
                            "dataset_relative_path": AF3_DATASET_RELATIVE_PATH,
                            "dataset_uri": AF3_DATASET_URI,
                            "file_count": 5001,
                            "node_accessibility": {
                                "evidence_receipt_digest": sha("af3-node-accessibility"),
                                "required_node_labels": {
                                    "storage.fs2.nebius/reference-data": "true",
                                },
                                "node_names": [],
                            },
                        },
                        "localization_receipt_digest": sha("localized-alphafold3-public-databases-v3.0"),
                    },
                    {
                        "artifact_id": "alphafold3-parameters",
                        "mount_path": "/models/af3.bin.zst",
                        "content_digest": "sha256:" + requirements["alphafold3-parameters"]["content_digest_sha256"],
                        "file_manifest": [
                            {
                                "path": item["path"],
                                "sha256": item["sha256"],
                                "size_bytes": item["size_bytes"],
                            }
                            for item in requirements["alphafold3-parameters"]["file_manifest"]
                        ],
                        "localization_receipt_digest": sha("localized-alphafold3-parameters"),
                    },
                ],
                "stages": [
                    {
                        **stage_common,
                        "stage_id": "data-pipeline",
                        "collector_id": "alphafold3-data-collector-v1",
                        "validator_id": "alphafold3-data-validator-v1",
                        "mounts": [
                            workspace,
                            {
                                "name": "alphafold3-databases",
                                "kind": "operator-host-path",
                                "artifact_id": "alphafold3-public-databases-v3.0",
                                "claim_name": None,
                                "claim_namespace": None,
                                "host_path": "/mnt/fs2-reference-data/data",
                                "operator_owned": True,
                                "mount_path": "/databases",
                                "sub_path": AF3_DATASET_RELATIVE_PATH,
                                "supplemental_groups": [1000],
                                "read_only": True,
                            },
                        ],
                        "node_selector": {
                            "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb",
                            "storage.fs2.nebius/reference-data": "true",
                        },
                        "tolerations": [
                            {
                                "key": "dedicated",
                                "operator": "Equal",
                                "value": "fs2-inference",
                                "effect": "NoSchedule",
                            }
                        ],
                    },
                    {
                        **stage_common,
                        "stage_id": "inference",
                        "collector_id": "alphafold3-result-collector-v1",
                        "validator_id": "alphafold3-upstream-v3-0-4",
                        "mounts": [
                            workspace,
                            {
                                "name": "alphafold3-parameters",
                                "kind": "private",
                                "artifact_id": "alphafold3-parameters",
                                "claim_name": "academic-assets-runtime-rwx",
                                "claim_namespace": "fs2-academic-poc",
                                "host_path": None,
                                "operator_owned": True,
                                "mount_path": "/models/af3.bin.zst",
                                "sub_path": "alphafold3/af3.bin.zst",
                                "supplemental_groups": [65532],
                                "read_only": True,
                            },
                            {
                                "name": "alphafold3-warm-cache",
                                "kind": "cache",
                                "artifact_id": None,
                                "claim_name": "scientific-alphafold3-cache",
                                "claim_namespace": "fs2-academic-poc",
                                "host_path": None,
                                "operator_owned": True,
                                "mount_path": "/cache/alphafold3",
                                "sub_path": None,
                                "supplemental_groups": [],
                                "read_only": False,
                            },
                        ],
                        "environment": {
                            "FS2_NETWORK_MODE": "offline",
                            "FS2_AF3_CACHE_ROOT": "/cache/alphafold3",
                            "FS2_AF3_JAX_CACHE_DIR": "/cache/alphafold3/jax",
                            "FS2_AF3_TRITON_CACHE_DIR": "/cache/alphafold3/triton",
                            "FS2_AF3_XDG_CACHE_DIR": "/cache/alphafold3/xdg",
                        },
                        "node_selector": {
                            "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb",
                        },
                        "tolerations": [
                            {
                                "key": "dedicated",
                                "operator": "Equal",
                                "value": "fs2-inference",
                                "effect": "NoSchedule",
                            }
                        ],
                    },
                ],
            }
        ],
    }


def af3_renderer(
    tmp_path: Path,
    *,
    mutate: object | None = None,
) -> tuple[FileScientificManifestRenderer, ScientificWorkloadProfile]:
    profile, profile_value = af3_qualified_profile()
    value = af3_execution_value(profile_value)
    if callable(mutate):
        mutate(value)
    path = tmp_path / f"af3-execution-{uuid4()}.json"
    path.write_text(json.dumps(value))
    catalog = ScientificProfileCatalog(
        profiles={profile.model_id: profile},
        validators=ScientificProfileCatalog.load(CATALOG_ROOT)._validators,  # type: ignore[attr-defined]
    )
    return (
        FileScientificManifestRenderer(
            path=path,
            profiles=catalog,
            tools_image="registry.test/control@sha256:" + "9" * 64,
            internal_api_url="http://control.fs2.svc:8080",
            capability_authority=ScientificWorkloadCapabilityAuthority(
                KeyedHasher(active_key_id="ledger-v1", keys={"ledger-v1": b"k" * 32})
            ),
        ),
        profile,
    )


def compiled_af3_plan(
    renderer: FileScientificManifestRenderer,
    profile: ScientificWorkloadProfile,
) -> tuple[AdapterExecutionPlan, ArtifactAccessContext, ScientificInputArtifact]:
    request = json.loads((REPO_ROOT / "models/structure/runtime/alphafold3/fixtures/positive-raw.json").read_text())
    model_input = ScientificInputArtifact(
        logical_artifact_id="model-input",
        semantic_type="request/v1",
        artifact_id=uuid4(),
        digest="sha256:" + "b" * 64,
        size_bytes=1024,
        media_type="application/json",
    )
    access = ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="ordinary-poc")
    plan = renderer.plan(
        profile,
        request,
        operation_id=uuid4(),
        access_context=access,
        input_artifacts=(model_input,),
    )
    return plan, access, model_input


def af3_scheduling() -> SchedulingContractResolver:
    return SchedulingContractResolver(
        {
            "schema": "fs2-serve.nebius.ai/kueue-scheduling/v1",
            "service_classes": {
                "customer-batch": {
                    "workload_priority_class": "customer-batch",
                    "priority": 10,
                    "default_local_queue": "scientific",
                    "preemption_mode": "restartable",
                    "pool_preference": ["h100-reserved-8x", "h100-1x"],
                    "max_queue_seconds": 600,
                    "max_execution_seconds": 3600,
                    "caller_selectable": True,
                }
            },
            "local_queues": {
                "scientific": {
                    "metadata": {"name": "scientific", "namespace": "fs2-models"},
                    "spec": {"clusterQueue": "inference-accelerators"},
                },
                "academic-scientific": {
                    "metadata": {"name": "academic-scientific", "namespace": "fs2-academic-poc"},
                    "spec": {"clusterQueue": "inference-accelerators"},
                },
            },
            "cluster_queues": {
                "inference-accelerators": {
                    "metadata": {"name": "inference-accelerators"},
                    "spec": {
                        "resourceGroups": [
                            {
                                "coveredResources": ["nvidia.com/gpu"],
                                "flavors": [
                                    {"name": "inference-h100-reserved-8x"},
                                    {"name": "inference-h100-1x"},
                                ],
                            }
                        ]
                    },
                }
            },
            "workload_priority_classes": {"customer-batch": {"value": 10}},
            "local_queue_routes": {
                "scientific": {
                    "namespace": "fs2-models",
                    "cluster_queue": "inference-accelerators",
                    "model_ids": [],
                    "tenant_ids": [],
                },
                "academic-scientific": {
                    "namespace": "fs2-academic-poc",
                    "cluster_queue": "inference-accelerators",
                    "model_ids": ["alphafold3"],
                    "tenant_ids": [],
                },
            },
            "pools": {
                "h100-reserved-8x": {
                    "resource_flavor": "inference-h100-reserved-8x",
                    "accelerator_resource_name": "nvidia.com/gpu",
                },
                "h100-1x": {
                    "resource_flavor": "inference-h100-1x",
                    "accelerator_resource_name": "nvidia.com/gpu",
                },
            },
        }
    )


def test_af3_two_digest_execution_target_and_physical_mounts(tmp_path: Path) -> None:
    renderer, profile = af3_renderer(tmp_path)
    assert renderer.controller_service_account == (
        "fs2-system",
        "fs2-serve-control-plane-runtime",
    )
    parameters_requirement = next(
        item for item in profile.value["artifact_requirements"] if item["artifact_id"] == "alphafold3-parameters"
    )
    assert parameters_requirement["content_digest_sha256"] == (
        "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
    )
    assert parameters_requirement["total_size_bytes"] == 1_020_545_840
    plan, access, model_input = compiled_af3_plan(renderer, profile)
    raw_plan = alphafold3_adapter.compile_run(
        profile.value,
        json.loads((REPO_ROOT / "models/structure/runtime/alphafold3/fixtures/positive-raw.json").read_text()),
        operation_id=str(uuid4()),
        variant_id="upstream-v3-0-4",
        access_context=access,
        input_artifacts=(model_input,),
    )
    assert all(not mount.supplemental_groups for item in raw_plan.invocations for mount in item.runtime_mounts)
    assert all(item.namespace == "fs2-academic-poc" for item in plan.invocations)
    assert all(item.local_queue_name == "academic-scientific" for item in plan.invocations)
    assert plan.invocation("data-pipeline", "main").runtime_mounts[0].supplemental_groups == (1000,)
    assert plan.invocation("inference", "main").runtime_mounts[0].supplemental_groups == (65532,)
    data = plan.invocation("data-pipeline", "main")
    inference = plan.invocation("inference", "main")
    assert all(mount.mount_path != "/cache/alphafold3" for mount in inference.runtime_mounts)
    assert data.argv[data.argv.index("--expected-db-content-sha256") + 1] == AF3_TREE_SHA256
    assert data.argv[data.argv.index("--expected-db-manifest-sha256") + 1] == AF3_MANIFEST_SHA256
    assert data.argv[data.argv.index("--db-ready-marker") + 1] == "/databases/.fs2-manifest-sha256"
    assert inference.argv[inference.argv.index("--expected-reference-content-sha256") + 1] == AF3_TREE_SHA256
    assert inference.argv[inference.argv.index("--expected-reference-manifest-sha256") + 1] == AF3_MANIFEST_SHA256
    localized = renderer.verify_runtime_artifacts(profile, plan, access)
    database_localization = next(
        item for item in localized if item.logical_artifact_id == "alphafold3-public-databases-v3.0"
    )
    assert database_localization.files == ()
    assert database_localization.aggregate_tree is not None
    assert database_localization.aggregate_tree.file_count == 5001
    assert database_localization.aggregate_tree.dataset_relative_path == AF3_DATASET_RELATIVE_PATH
    assert database_localization.aggregate_tree.manifest_digest == "sha256:" + AF3_MANIFEST_SHA256
    assert database_localization.aggregate_tree.node_accessibility.evidence_receipt_digest == sha(
        "af3-node-accessibility"
    )
    assert database_localization.aggregate_tree.node_accessibility.required_node_labels == (
        ("storage.fs2.nebius/reference-data", "true"),
    )
    scheduling_path = tmp_path / "kueue-scheduling.json"
    scheduling_path.write_text(json.dumps(af3_scheduling().contract))
    scheduling = SchedulingContractResolver.load(scheduling_path)
    assert scheduling.local_queue_routes["academic-scientific"] == {
        "namespace": "fs2-academic-poc",
        "cluster_queue": "inference-accelerators",
        "model_ids": ["alphafold3"],
        "tenant_ids": [],
    }
    assert renderer.scheduling_targets() == {
        ("alphafold3", "data-pipeline"): (
            "fs2-academic-poc",
            "academic-scientific",
            "inference-accelerators",
        ),
        ("alphafold3", "inference"): (
            "fs2-academic-poc",
            "academic-scientific",
            "inference-accelerators",
        ),
    }
    scheduling.require_execution_targets(renderer.scheduling_targets())
    snapshot = scheduling.freeze_for_execution(
        service_class="customer-batch",
        model_id="alphafold3",
        tenant_id="ordinary-poc",
        profile=profile.value,
        execution=plan,
        captured_at=NOW,
    )
    assert all(item.execution_namespace == "fs2-academic-poc" for item in snapshot.stages)
    assert all(item.resolved_local_queue == "academic-scientific" for item in snapshot.stages)
    inference_scheduling = snapshot.stage("inference")
    assert inference_scheduling.resolved_pool_preference == (
        "h100-reserved-8x",
        "h100-1x",
    )
    assert inference_scheduling.accelerator_resource_name == "nvidia.com/gpu"
    assert inference_scheduling.accelerator_count == 1
    assert inference_scheduling.admitted_resource_flavor is None
    codec_state = ScientificBatchState.admit(
        operation_id=uuid4(),
        tenant_id="ordinary-poc",
        model_id="alphafold3",
        variant_id="upstream-v3-0-4",
        input_artifact_id=uuid4(),
        plan=plan.controller_plan,
        scheduling=snapshot,
        runtime_artifacts=localized,
    )
    encoded_state = state_to_value(codec_state)
    assert len(json.dumps(encoded_state).encode()) < 4 * 1024 * 1024
    assert state_from_value(encoded_state) == codec_state

    def workload(invocation: StageInvocation) -> WorkloadResource:
        materializations = tuple(
            ResolvedArtifactMaterialization.resolve(
                item,
                artifact_id=(model_input.artifact_id if item.artifact_id == "model-input" else uuid4()),
                digest=(model_input.digest if item.artifact_id == "model-input" else sha("handoff")),
                size_bytes=(model_input.size_bytes if item.artifact_id == "model-input" else 4096),
                media_type=(model_input.media_type if item.artifact_id == "model-input" else "application/x-tar"),
                compression=(model_input.compression if item.artifact_id == "model-input" else "zstd"),
            )
            for item in invocation.materializations
        )
        return WorkloadResource(
            operation_id=uuid4(),
            batch_id=uuid4(),
            workload_id=uuid4(),
            attempt_id=uuid4(),
            stage_id=invocation.stage_id,
            shard_id="main",
            attempt_number=1,
            tenant_id="ordinary-poc",
            model_id="alphafold3",
            variant_id="upstream-v3-0-4",
            input_artifact_id=uuid4(),
            service_class=ServiceClass.CUSTOMER_BATCH,
            scheduling_snapshot_digest=snapshot.digest,
            namespace="fs2-academic-poc",
            name=f"af3-{invocation.stage_id}",
            kind=WorkloadKind.JOB,
            scheduling=snapshot.stage(invocation.stage_id),
            invocation=invocation,
            materializations=materializations,
            access_context=access,
            runtime_artifacts=tuple(
                item for item in localized if item.logical_artifact_id in invocation.runtime_artifacts
            ),
        )

    data_resource = workload(data)
    data_manifest = renderer.render(data_resource)
    data_pod = data_manifest["spec"]["template"]["spec"]  # type: ignore[index]
    database_volume = next(item for item in data_pod["volumes"] if item["name"] == "alphafold3-databases")
    assert database_volume["hostPath"] == {
        "path": "/mnt/fs2-reference-data/data",
        "type": "Directory",
    }
    database_mount = next(
        item for item in data_pod["containers"][0]["volumeMounts"] if item["name"] == "alphafold3-databases"
    )
    assert database_mount == {
        "name": "alphafold3-databases",
        "mountPath": "/databases",
        "readOnly": True,
        "subPath": AF3_DATASET_RELATIVE_PATH,
    }
    assert "affinity" not in data_pod
    assert data_pod["nodeSelector"] == {
        "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb",
        "storage.fs2.nebius/reference-data": "true",
    }
    assert data_pod["tolerations"] == [
        {
            "key": "dedicated",
            "operator": "Equal",
            "value": "fs2-inference",
            "effect": "NoSchedule",
        }
    ]
    trusted_data_execution = renderer.executions[("alphafold3", "data-pipeline")]
    renderer.executions[("alphafold3", "data-pipeline")] = replace(
        trusted_data_execution,
        node_selector=MappingProxyType({"accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb"}),
    )
    with pytest.raises(ScientificExecutionMapError, match="hostPath lost its trusted stage node selector"):
        renderer.render(data_resource)
    renderer.executions[("alphafold3", "data-pipeline")] = trusted_data_execution
    assert data_pod["securityContext"] == {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
        "supplementalGroups": [1000],
        "supplementalGroupsPolicy": "Strict",
    }
    assert "fsGroup" not in data_pod["securityContext"]
    assert "fsGroupChangePolicy" not in data_pod["securityContext"]
    runtime_marker_json = next(
        item["value"] for item in data_pod["containers"][0]["env"] if item["name"] == "FS2_RUNTIME_ARTIFACTS_JSON"
    )
    runtime_marker = json.loads(runtime_marker_json)
    database_marker = next(
        item for item in runtime_marker["artifacts"] if item["artifact_id"] == "alphafold3-public-databases-v3.0"
    )
    assert database_marker["expected_manifest_sha256"] == AF3_MANIFEST_SHA256
    assert database_marker["content_digest"] == "sha256:" + AF3_TREE_SHA256
    assert database_marker["mount_path"] == "/databases"
    assert database_marker["sub_path"] is None
    inference_resource = workload(inference)
    inference_manifest = renderer.render(inference_resource)
    inference_pod = inference_manifest["spec"]["template"]["spec"]  # type: ignore[index]
    private_volume = next(item for item in inference_pod["volumes"] if item["name"] == "alphafold3-parameters")
    assert private_volume["persistentVolumeClaim"] == {
        "claimName": "academic-assets-runtime-rwx",
        "readOnly": True,
    }
    private_mount = next(
        item for item in inference_pod["containers"][0]["volumeMounts"] if item["name"] == "alphafold3-parameters"
    )
    assert private_mount["mountPath"] == "/models/af3.bin.zst"
    assert private_mount["subPath"] == "alphafold3/af3.bin.zst"
    cache_mount = next(
        item
        for item in inference_pod["containers"][0]["volumeMounts"]
        if item["name"] == "alphafold3-warm-cache"
    )
    assert cache_mount == {
        "name": "alphafold3-warm-cache",
        "mountPath": "/cache/alphafold3",
        "readOnly": False,
    }
    cache_volume = next(
        item for item in inference_pod["volumes"] if item["name"] == "alphafold3-warm-cache"
    )
    assert cache_volume["persistentVolumeClaim"] == {
        "claimName": "scientific-alphafold3-cache",
        "readOnly": False,
    }
    inference_environment = {
        item["name"]: item["value"] for item in inference_pod["containers"][0]["env"]
    }
    assert {
        key: inference_environment[key]
        for key in (
            "FS2_AF3_CACHE_ROOT",
            "FS2_AF3_JAX_CACHE_DIR",
            "FS2_AF3_TRITON_CACHE_DIR",
            "FS2_AF3_XDG_CACHE_DIR",
        )
    } == {
        "FS2_AF3_CACHE_ROOT": "/cache/alphafold3",
        "FS2_AF3_JAX_CACHE_DIR": "/cache/alphafold3/jax",
        "FS2_AF3_TRITON_CACHE_DIR": "/cache/alphafold3/triton",
        "FS2_AF3_XDG_CACHE_DIR": "/cache/alphafold3/xdg",
    }
    assert inference_pod["serviceAccountName"] == "fs2-academic-runner"
    assert inference_pod["nodeSelector"] == {
        "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb",
    }
    assert inference_pod["tolerations"] == data_pod["tolerations"]
    assert inference_pod["securityContext"] == {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
        "supplementalGroups": [65532],
        "supplementalGroupsPolicy": "Strict",
    }
    assert "fsGroup" not in inference_pod["securityContext"]
    assert "fsGroupChangePolicy" not in inference_pod["securityContext"]
    with pytest.raises(ValueError, match="namespace differs from the frozen scheduling decision"):
        renderer.render(replace(data_resource, namespace="fs2-models"))
    with pytest.raises(ValueError, match="invocation target differs"):
        renderer.render(
            replace(
                data_resource,
                scheduling=replace(data_resource.scheduling, resolved_local_queue="scientific"),
            )
        )
    with pytest.raises(ScientificExecutionMapError, match="ClusterQueue differs"):
        renderer.render(
            replace(
                data_resource,
                scheduling=replace(data_resource.scheduling, resolved_cluster_queue="tenant-cluster-queue"),
            )
        )


def test_af3_requires_route_in_the_immutable_scheduling_contract(tmp_path: Path) -> None:
    renderer, profile = af3_renderer(tmp_path)
    plan, _access, _model_input = compiled_af3_plan(renderer, profile)
    contract = deepcopy(af3_scheduling().contract)
    del contract["local_queue_routes"]["academic-scientific"]
    resolver = SchedulingContractResolver(contract)

    with pytest.raises(SchedulingContractError, match="Kueue LocalQueue route is not an object"):
        resolver.require_execution_targets(renderer.scheduling_targets())
    with pytest.raises(SchedulingContractError, match="Kueue LocalQueue route is not an object"):
        resolver.freeze_for_execution(
            service_class="customer-batch",
            model_id="alphafold3",
            tenant_id="ordinary-poc",
            profile=profile.value,
            execution=plan,
            captured_at=NOW,
        )

    drifted = deepcopy(af3_scheduling().contract)
    drifted["local_queue_routes"]["academic-scientific"]["cluster_queue"] = "tenant-selected"
    with pytest.raises(SchedulingContractError, match="route is inconsistent"):
        SchedulingContractResolver(drifted).require_execution_targets(renderer.scheduling_targets())


def test_af3_adapter_cannot_supply_even_the_trusted_storage_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer, profile = af3_renderer(tmp_path)
    bound_plan, access, model_input = compiled_af3_plan(renderer, profile)
    monkeypatch.setattr(scientific_adapters, "compile_adapter_run", lambda *args, **kwargs: bound_plan)

    with pytest.raises(
        ScientificExecutionMapError,
        match="adapter cannot declare deployment-owned runtime storage groups",
    ):
        renderer.plan(
            profile,
            json.loads((REPO_ROOT / "models/structure/runtime/alphafold3/fixtures/positive-raw.json").read_text()),
            operation_id=uuid4(),
            access_context=access,
            input_artifacts=(model_input,),
        )


def test_af3_operator_storage_cannot_cross_namespace_or_change_host_path(tmp_path: Path) -> None:
    def wrong_controller_service_account(value: dict[str, object]) -> None:
        value["controller_service_account"]["name"] = "tenant-controller"  # type: ignore[index]

    with pytest.raises(ScientificExecutionMapError, match="differs from deployment configuration"):
        af3_renderer(tmp_path, mutate=wrong_controller_service_account)

    def cross_namespace(value: dict[str, object]) -> None:
        value["models"][0]["stages"][1]["mounts"][1]["claim_namespace"] = "fs2-models"  # type: ignore[index]

    with pytest.raises(ScientificExecutionMapError, match="execution namespace"):
        af3_renderer(tmp_path, mutate=cross_namespace)

    def arbitrary_host_path(value: dict[str, object]) -> None:
        value["models"][0]["stages"][0]["mounts"][1]["host_path"] = "/tenant-controlled/path"  # type: ignore[index]

    with pytest.raises(ScientificExecutionMapError, match="outside the exact"):
        af3_renderer(tmp_path, mutate=arbitrary_host_path)

    def broad_host_root(value: dict[str, object]) -> None:
        value["models"][0]["stages"][0]["mounts"][1]["host_path"] = "/mnt/fs2-reference-data"  # type: ignore[index]

    with pytest.raises(ScientificExecutionMapError, match="outside the exact"):
        af3_renderer(tmp_path, mutate=broad_host_root)

    def unpinned_root(value: dict[str, object]) -> None:
        value["models"][0]["stages"][0]["mounts"][1]["sub_path"] = None  # type: ignore[index]

    with pytest.raises(ScientificExecutionMapError, match="pinned read-only"):
        af3_renderer(tmp_path, mutate=unpinned_root)

    def mismatched_mount_tree(value: dict[str, object]) -> None:
        value["models"][0]["stages"][0]["mounts"][1]["sub_path"] = (  # type: ignore[index]
            "datasets/alphafold3-public-databases-v3.0/v3.0-paper-snapshot-2022-09-28/sha256/" + "b" * 64
        )

    with pytest.raises(ScientificExecutionMapError, match="outside the exact"):
        af3_renderer(tmp_path, mutate=mismatched_mount_tree)

    def wrong_queue(value: dict[str, object]) -> None:
        value["models"][0]["stages"][0]["local_queue_name"] = "tenant-queue"  # type: ignore[index]

    with pytest.raises(ScientificExecutionMapError, match="trusted academic placement"):
        af3_renderer(tmp_path, mutate=wrong_queue)

    def wrong_service_account(value: dict[str, object]) -> None:
        value["models"][0]["stages"][1]["service_account_name"] = "tenant-runner"  # type: ignore[index]

    with pytest.raises(ScientificExecutionMapError, match="trusted academic placement"):
        af3_renderer(tmp_path, mutate=wrong_service_account)

    def missing_reference_selector(value: dict[str, object]) -> None:
        value["models"][0]["stages"][0]["node_selector"].pop(  # type: ignore[index]
            "storage.fs2.nebius/reference-data"
        )

    with pytest.raises(ScientificExecutionMapError, match="trusted academic placement"):
        af3_renderer(tmp_path, mutate=missing_reference_selector)

    def wrong_toleration(value: dict[str, object]) -> None:
        value["models"][0]["stages"][0]["tolerations"][0]["value"] = "tenant"  # type: ignore[index]

    with pytest.raises(ScientificExecutionMapError, match="trusted academic placement"):
        af3_renderer(tmp_path, mutate=wrong_toleration)

    def wrong_parameter_group(value: dict[str, object]) -> None:
        value["models"][0]["stages"][1]["mounts"][1]["supplemental_groups"] = [1000]  # type: ignore[index]

    with pytest.raises(ScientificExecutionMapError, match="trusted storage identity"):
        af3_renderer(tmp_path, mutate=wrong_parameter_group)

    def wrong_reference_group(value: dict[str, object]) -> None:
        value["models"][0]["stages"][0]["mounts"][1]["supplemental_groups"] = [65532]  # type: ignore[index]

    with pytest.raises(ScientificExecutionMapError, match="trusted storage identity"):
        af3_renderer(tmp_path, mutate=wrong_reference_group)

    def cross_namespace_cache(value: dict[str, object]) -> None:
        value["models"][0]["stages"][1]["mounts"][2]["claim_namespace"] = "fs2-models"  # type: ignore[index]

    with pytest.raises(ScientificExecutionMapError, match="execution namespace"):
        af3_renderer(tmp_path, mutate=cross_namespace_cache)

    def cache_artifact_impersonation(value: dict[str, object]) -> None:
        value["models"][0]["stages"][1]["mounts"][2]["artifact_id"] = "alphafold3-parameters"  # type: ignore[index]

    with pytest.raises(ScientificExecutionMapError, match="cannot impersonate"):
        af3_renderer(tmp_path, mutate=cache_artifact_impersonation)

    def wrong_cache_path(value: dict[str, object]) -> None:
        value["models"][0]["stages"][1]["mounts"][2]["mount_path"] = "/cache/tenant"  # type: ignore[index]

    with pytest.raises(ScientificExecutionMapError, match="model/stage allowlist"):
        af3_renderer(tmp_path, mutate=wrong_cache_path)

    def missing_cache_environment(value: dict[str, object]) -> None:
        value["models"][0]["stages"][1]["environment"].pop("FS2_AF3_JAX_CACHE_DIR")  # type: ignore[index]

    with pytest.raises(ScientificExecutionMapError, match="warm-cache contract"):
        af3_renderer(tmp_path, mutate=missing_cache_environment)


def test_af3_aggregate_tree_rejects_traversal_wrong_identity_and_missing_node_evidence(
    tmp_path: Path,
) -> None:
    def traversal(value: dict[str, object]) -> None:
        artifact = value["models"][0]["runtime_artifacts"][0]  # type: ignore[index]
        artifact["aggregate_tree"]["dataset_relative_path"] = "../tenant-controlled"  # type: ignore[index]

    with pytest.raises(ValueError, match="dataset path is unsafe"):
        af3_renderer(tmp_path, mutate=traversal)

    def terminal_digest_mismatch(value: dict[str, object]) -> None:
        artifact = value["models"][0]["runtime_artifacts"][0]  # type: ignore[index]
        artifact["content_digest"] = "sha256:" + "b" * 64  # type: ignore[index]

    with pytest.raises(ScientificExecutionMapError, match="outside the exact AF3 dataset layout"):
        af3_renderer(tmp_path, mutate=terminal_digest_mismatch)

    def no_accessibility_selector(value: dict[str, object]) -> None:
        artifact = value["models"][0]["runtime_artifacts"][0]  # type: ignore[index]
        artifact["aggregate_tree"]["node_accessibility"]["required_node_labels"] = {}  # type: ignore[index]

    with pytest.raises(ScientificExecutionMapError, match="selector differs"):
        af3_renderer(tmp_path, mutate=no_accessibility_selector)

    def hardcoded_node(value: dict[str, object]) -> None:
        artifact = value["models"][0]["runtime_artifacts"][0]  # type: ignore[index]
        artifact["aggregate_tree"]["node_accessibility"]["node_names"] = [  # type: ignore[index]
            "live-h100-node-1"
        ]

    with pytest.raises(ScientificExecutionMapError, match="trusted selector, not node IDs"):
        af3_renderer(tmp_path, mutate=hardcoded_node)

    def wrong_manifest(value: dict[str, object]) -> None:
        artifact = value["models"][0]["runtime_artifacts"][0]  # type: ignore[index]
        artifact["aggregate_tree"]["manifest_digest"] = "sha256:" + "e" * 64  # type: ignore[index]

    renderer, profile = af3_renderer(tmp_path, mutate=wrong_manifest)
    plan, access, _ = compiled_af3_plan(renderer, profile)
    with pytest.raises(ScientificExecutionMapError, match="localization evidence differs"):
        renderer.verify_runtime_artifacts(profile, plan, access)

    def wrong_tree(value: dict[str, object]) -> None:
        artifact = value["models"][0]["runtime_artifacts"][0]  # type: ignore[index]
        wrong_digest = "b" * 64
        wrong_relative = (
            f"datasets/alphafold3-public-databases-v3.0/v3.0-paper-snapshot-2022-09-28/sha256/{wrong_digest}"
        )
        artifact["content_digest"] = "sha256:" + wrong_digest  # type: ignore[index]
        artifact["aggregate_tree"]["dataset_relative_path"] = wrong_relative  # type: ignore[index]
        artifact["aggregate_tree"]["dataset_uri"] = (  # type: ignore[index]
            f"file:///mnt/fs2-reference-data/data/{wrong_relative}"
        )
        value["models"][0]["stages"][0]["mounts"][1]["sub_path"] = wrong_relative  # type: ignore[index]

    renderer, profile = af3_renderer(tmp_path, mutate=wrong_tree)
    plan, access, _ = compiled_af3_plan(renderer, profile)
    with pytest.raises(ScientificExecutionMapError, match="localization evidence differs"):
        renderer.verify_runtime_artifacts(profile, plan, access)


def test_protenix_gpu_stage_gets_exact_deployment_owned_warm_cache(tmp_path: Path) -> None:
    profile_set = json.loads((CATALOG_ROOT / "contracts/scientific-workload-profiles.json").read_text())
    value = deepcopy(next(item for item in profile_set["profiles"] if item["model_id"] == "protenix-v2"))
    value["state"] = "qualified"
    value["route_exposed"] = True
    value["semantic_validation"]["state"] = "qualified"
    value["execution_identity"].update(
        {
            "runtime_image_digest": "sha256:" + "a" * 64,
            "runtime_image_state": "qualified",
            "artifact_manifest_digest": "8" * 64,
            "execution_identity_sha256": "9" * 64,
        }
    )
    profile = ScientificWorkloadProfile(MappingProxyType(value))
    requirement = value["artifact_requirements"][0]
    workspace = {
        "name": "artifact-workspace",
        "kind": "artifact-workspace",
        "artifact_id": None,
        "claim_name": None,
        "claim_namespace": None,
        "host_path": None,
        "operator_owned": False,
        "mount_path": "/mnt/fs2-scientific",
        "sub_path": None,
        "read_only": False,
    }
    model_mount = {
        "name": "protenix-v2",
        "kind": "reference",
        "artifact_id": "protenix-v2",
        "claim_name": "scientific-model-artifacts",
        "claim_namespace": "fs2-models",
        "host_path": None,
        "operator_owned": True,
        "mount_path": "/models/protenix-v2",
        "sub_path": "protenix-v2",
        "read_only": True,
    }
    stage_common = {
        "execution_namespace": "fs2-models",
        "local_queue_name": "scientific",
        "image": "registry.test/protenix@sha256:" + "a" * 64,
        "service_account_name": "scientific-runner",
        "resources": {"cpu": "8", "memory": "64Gi", "ephemeral_storage": "64Gi"},
        "active_deadline_seconds": 3600,
        "termination_grace_seconds": 60,
    }
    cache_environment = {
        "TRITON_CACHE_DIR": "/cache/protenix/triton",
        "CUEQ_TRITON_CACHE_DIR": "/cache/protenix/cueq-triton",
        "TORCH_EXTENSIONS_DIR": "/cache/protenix/torch-extensions",
        "XDG_CACHE_HOME": "/cache/protenix/xdg",
    }
    execution = {
        "schema": "fs2-serve.nebius.ai/scientific-execution-map/v3",
        "controller_service_account": {
            "namespace": "fs2-system",
            "name": "fs2-serve-control-plane-runtime",
        },
        "models": [
            {
                "model_id": "protenix-v2",
                "variant_id": "upstream-v2-0-0",
                "execution_identity_sha256": "9" * 64,
                "plan_adapter": {
                    "module": "fs2_serve.scientific_batch.adapters",
                    "function": "compile_adapter_run",
                },
                "runtime_artifacts": [
                    {
                        "artifact_id": "protenix-v2",
                        "mount_path": "/models/protenix-v2",
                        "content_digest": "sha256:" + requirement["content_digest_sha256"],
                        "file_manifest": [
                            {
                                "path": item["path"],
                                "sha256": item["sha256"],
                                "size_bytes": item["size_bytes"],
                            }
                            for item in requirement["file_manifest"]
                        ],
                        "localization_receipt_digest": sha("localized-protenix-v2"),
                    }
                ],
                "stages": [
                    {
                        **stage_common,
                        "stage_id": "prepare-data",
                        "collector_id": "protenix-v2-prep-collector-v1",
                        "validator_id": "protenix-v2-prep-validator-v1",
                        "mounts": [workspace, model_mount],
                        "environment": {},
                    },
                    {
                        **stage_common,
                        "stage_id": "sample-structure",
                        "collector_id": "protenix-v2-result-collector-v1",
                        "validator_id": "protenix-v2-upstream-v2-0-0",
                        "mounts": [
                            workspace,
                            model_mount,
                            {
                                "name": "protenix-warm-cache",
                                "kind": "cache",
                                "artifact_id": None,
                                "claim_name": "scientific-protenix-cache",
                                "claim_namespace": "fs2-models",
                                "host_path": None,
                                "operator_owned": True,
                                "mount_path": "/cache/protenix",
                                "sub_path": None,
                                "read_only": False,
                            },
                        ],
                        "environment": cache_environment,
                    },
                ],
            }
        ],
    }
    path = tmp_path / "protenix-execution.json"
    path.write_text(json.dumps(execution))
    catalog = ScientificProfileCatalog(
        profiles={profile.model_id: profile},
        validators=ScientificProfileCatalog.load(CATALOG_ROOT)._validators,  # type: ignore[attr-defined]
    )
    renderer = FileScientificManifestRenderer(
        path=path,
        profiles=catalog,
        tools_image="registry.test/control@sha256:" + "9" * 64,
        internal_api_url="http://control.fs2.svc:8080",
        capability_authority=ScientificWorkloadCapabilityAuthority(
            KeyedHasher(active_key_id="ledger-v1", keys={"ledger-v1": b"k" * 32})
        ),
    )
    request = json.loads(
        (REPO_ROOT / "models/structure/runtime/protenix_v2/fixtures/positive-monomer.json").read_text()
    )
    model_input = ScientificInputArtifact(
        logical_artifact_id="model-input",
        semantic_type="request/v1",
        artifact_id=uuid4(),
        digest="sha256:" + "b" * 64,
        size_bytes=1024,
        media_type="application/json",
    )
    access = ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="tenant-a")
    plan = renderer.plan(
        profile,
        request,
        operation_id=uuid4(),
        access_context=access,
        input_artifacts=(model_input,),
    )
    localized = renderer.verify_runtime_artifacts(profile, plan, access)
    sample = plan.invocation("sample-structure", "main")
    materialization = ResolvedArtifactMaterialization.resolve(
        sample.materializations[0],
        artifact_id=uuid4(),
        digest=sha("processed"),
        size_bytes=4096,
        media_type="application/x-tar",
        compression="zstd",
    )
    snapshot = scheduling(plan.controller_plan)
    resource = WorkloadResource(
        operation_id=uuid4(),
        batch_id=uuid4(),
        workload_id=uuid4(),
        attempt_id=uuid4(),
        stage_id="sample-structure",
        shard_id="main",
        attempt_number=1,
        tenant_id="tenant-a",
        model_id="protenix-v2",
        variant_id="upstream-v2-0-0",
        input_artifact_id=uuid4(),
        service_class=ServiceClass.CUSTOMER_BATCH,
        scheduling_snapshot_digest=snapshot.digest,
        namespace="fs2-models",
        name="protenix-sample",
        kind=WorkloadKind.JOB,
        scheduling=snapshot.stage("sample-structure"),
        invocation=sample,
        materializations=(materialization,),
        access_context=access,
        runtime_artifacts=localized,
    )
    manifest = renderer.render(resource)
    pod = manifest["spec"]["template"]["spec"]  # type: ignore[index]
    cache_mount = next(item for item in pod["containers"][0]["volumeMounts"] if item["name"] == "protenix-warm-cache")
    assert cache_mount == {
        "name": "protenix-warm-cache",
        "mountPath": "/cache/protenix",
        "readOnly": False,
    }
    cache_volume = next(item for item in pod["volumes"] if item["name"] == "protenix-warm-cache")
    assert cache_volume["persistentVolumeClaim"] == {
        "claimName": "scientific-protenix-cache",
        "readOnly": False,
    }
    environment = {item["name"]: item["value"] for item in pod["containers"][0]["env"]}
    assert {key: environment[key] for key in cache_environment} == cache_environment
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert "fsGroup" not in pod["securityContext"]
    assert "fsGroupChangePolicy" not in pod["securityContext"]

    execution["models"][0]["stages"][1]["environment"].pop("TRITON_CACHE_DIR")
    path.write_text(json.dumps(execution))
    with pytest.raises(ScientificExecutionMapError, match="warm-cache contract"):
        FileScientificManifestRenderer(path=path, profiles=catalog)


def test_openfold3_gpu_stage_gets_exact_deployment_owned_warm_cache(tmp_path: Path) -> None:
    profile_set = json.loads((CATALOG_ROOT / "contracts/scientific-workload-profiles.json").read_text())
    value = deepcopy(next(item for item in profile_set["profiles"] if item["model_id"] == "openfold3"))
    value["state"] = "qualified"
    value["route_exposed"] = True
    value["semantic_validation"]["state"] = "qualified"
    value["execution_identity"].update(
        {
            "runtime_image_digest": "sha256:" + "a" * 64,
            "runtime_image_state": "qualified",
            "artifact_manifest_digest": "8" * 64,
            "execution_identity_sha256": "9" * 64,
        }
    )
    profile = ScientificWorkloadProfile(MappingProxyType(value))
    requirements = {item["artifact_id"]: item for item in value["artifact_requirements"]}
    workspace = {
        "name": "artifact-workspace",
        "kind": "artifact-workspace",
        "artifact_id": None,
        "claim_name": None,
        "claim_namespace": None,
        "host_path": None,
        "operator_owned": False,
        "mount_path": "/mnt/fs2-scientific",
        "sub_path": None,
        "read_only": False,
    }
    checkpoint_mount = {
        "name": "openfold3-openbind-0",
        "kind": "reference",
        "artifact_id": "openfold3-openbind-0",
        "claim_name": "scientific-model-artifacts",
        "claim_namespace": "fs2-models",
        "host_path": None,
        "operator_owned": True,
        "mount_path": "/models/openfold3",
        "sub_path": "openfold3-openbind-0",
        "read_only": True,
    }
    ccd_mount = {
        "name": "openfold3-components-bcif",
        "kind": "reference",
        "artifact_id": "openfold3-components-bcif",
        "claim_name": "scientific-model-artifacts",
        "claim_namespace": "fs2-models",
        "host_path": None,
        "operator_owned": True,
        "mount_path": "/databases/openfold3",
        "sub_path": "openfold3-components-bcif",
        "read_only": True,
    }
    cache_mount = {
        "name": "openfold3-warm-cache",
        "kind": "cache",
        "artifact_id": None,
        "claim_name": "scientific-openfold3-cache",
        "claim_namespace": "fs2-models",
        "host_path": None,
        "operator_owned": True,
        "mount_path": "/cache/openfold3",
        "sub_path": None,
        "read_only": False,
    }
    cache_environment = {
        "TRITON_CACHE_DIR": "/cache/openfold3/triton",
        "TORCH_EXTENSIONS_DIR": "/cache/openfold3/torch-extensions",
        "XDG_CACHE_HOME": "/cache/openfold3/xdg",
    }
    stage_common = {
        "execution_namespace": "fs2-models",
        "local_queue_name": "inference-models",
        "cluster_queue_name": "inference-accelerators",
        "image": "registry.test/openfold3@sha256:" + "a" * 64,
        "service_account_name": "scientific-runner",
        "resources": {"cpu": "8", "memory": "64Gi", "ephemeral_storage": "64Gi"},
        "active_deadline_seconds": 3600,
        "termination_grace_seconds": 60,
        "node_selector": {"accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb"},
        "tolerations": [
            {
                "key": "dedicated",
                "operator": "Equal",
                "value": "fs2-inference",
                "effect": "NoSchedule",
            }
        ],
    }
    execution = {
        "schema": "fs2-serve.nebius.ai/scientific-execution-map/v3",
        "controller_service_account": {
            "namespace": "fs2-system",
            "name": "fs2-serve-control-plane-runtime",
        },
        "models": [
            {
                "model_id": "openfold3",
                "variant_id": "upstream-openbind-v0-5-0",
                "execution_identity_sha256": "9" * 64,
                "plan_adapter": {
                    "module": "fs2_serve.scientific_batch.adapters",
                    "function": "compile_adapter_run",
                },
                "runtime_artifacts": [
                    {
                        "artifact_id": artifact_id,
                        "mount_path": mount_path,
                        "content_digest": "sha256:" + requirement["content_digest_sha256"],
                        "file_manifest": [
                            {
                                "path": item["path"],
                                "sha256": item["sha256"],
                                "size_bytes": item["size_bytes"],
                            }
                            for item in requirement["file_manifest"]
                        ],
                        "localization_receipt_digest": sha(f"localized-{artifact_id}"),
                    }
                    for artifact_id, mount_path in (
                        ("openfold3-openbind-0", "/models/openfold3"),
                        ("openfold3-components-bcif", "/databases/openfold3"),
                    )
                    for requirement in (requirements[artifact_id],)
                ],
                "stages": [
                    {
                        **stage_common,
                        "stage_id": "data-pipeline",
                        "collector_id": "openfold3-data-collector-v1",
                        "validator_id": "openfold3-data-validator-v1",
                        "mounts": [workspace],
                        "environment": {"FS2_NETWORK_MODE": "offline"},
                    },
                    {
                        **stage_common,
                        "stage_id": "inference",
                        "collector_id": "openfold3-result-collector-v1",
                        "validator_id": "openfold3-upstream-openbind-v0-5-0",
                        "mounts": [workspace, checkpoint_mount, ccd_mount, cache_mount],
                        "environment": {"FS2_NETWORK_MODE": "offline", **cache_environment},
                    },
                ],
            }
        ],
    }
    path = tmp_path / "openfold3-execution.json"
    path.write_text(json.dumps(execution))
    catalog = ScientificProfileCatalog(
        profiles={profile.model_id: profile},
        validators=ScientificProfileCatalog.load(CATALOG_ROOT)._validators,  # type: ignore[attr-defined]
    )

    def load(candidate: dict[str, object]) -> FileScientificManifestRenderer:
        path.write_text(json.dumps(candidate))
        return FileScientificManifestRenderer(
            path=path,
            profiles=catalog,
            tools_image="registry.test/control@sha256:" + "9" * 64,
            internal_api_url="http://control.fs2.svc:8080",
            capability_authority=ScientificWorkloadCapabilityAuthority(
                KeyedHasher(active_key_id="ledger-v1", keys={"ledger-v1": b"k" * 32})
            ),
        )

    renderer = load(execution)
    request = json.loads(
        (REPO_ROOT / "models/structure/runtime/openfold3/fixtures/positive-monomer.json").read_text()
    )
    model_input = ScientificInputArtifact(
        logical_artifact_id="model-input",
        semantic_type="request/v1",
        artifact_id=uuid4(),
        digest="sha256:" + "b" * 64,
        size_bytes=1024,
        media_type="application/json",
    )
    access = ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="tenant-a")
    plan = renderer.plan(
        profile,
        request,
        operation_id=uuid4(),
        access_context=access,
        input_artifacts=(model_input,),
    )
    localized = renderer.verify_runtime_artifacts(profile, plan, access)
    inference = plan.invocation("inference", "main")
    assert all(mount.mount_path != "/cache/openfold3" for mount in inference.runtime_mounts)
    handoff = ResolvedArtifactMaterialization.resolve(
        inference.materializations[0],
        artifact_id=uuid4(),
        digest=sha("openfold3-processed"),
        size_bytes=4096,
        media_type="application/x-tar",
        compression="zstd",
    )
    base_snapshot = scheduling(plan.controller_plan)
    snapshot = replace(
        base_snapshot,
        stages=tuple(
            replace(
                stage,
                resolved_cluster_queue="inference-accelerators",
                resolved_local_queue="inference-models",
            )
            for stage in base_snapshot.stages
        ),
    )
    resource = WorkloadResource(
        operation_id=uuid4(),
        batch_id=uuid4(),
        workload_id=uuid4(),
        attempt_id=uuid4(),
        stage_id="inference",
        shard_id="main",
        attempt_number=1,
        tenant_id="tenant-a",
        model_id="openfold3",
        variant_id="upstream-openbind-v0-5-0",
        input_artifact_id=uuid4(),
        service_class=ServiceClass.CUSTOMER_BATCH,
        scheduling_snapshot_digest=snapshot.digest,
        namespace="fs2-models",
        name="openfold3-inference",
        kind=WorkloadKind.JOB,
        scheduling=snapshot.stage("inference"),
        invocation=inference,
        materializations=(handoff,),
        access_context=access,
        runtime_artifacts=localized,
    )
    manifest = renderer.render(resource)
    pod = manifest["spec"]["template"]["spec"]  # type: ignore[index]
    rendered_mount = next(
        item
        for item in pod["containers"][0]["volumeMounts"]
        if item["name"] == "openfold3-warm-cache"
    )
    assert rendered_mount == {
        "name": "openfold3-warm-cache",
        "mountPath": "/cache/openfold3",
        "readOnly": False,
    }
    rendered_volume = next(
        item for item in pod["volumes"] if item["name"] == "openfold3-warm-cache"
    )
    assert rendered_volume["persistentVolumeClaim"] == {
        "claimName": "scientific-openfold3-cache",
        "readOnly": False,
    }
    environment = {item["name"]: item["value"] for item in pod["containers"][0]["env"]}
    assert {key: environment[key] for key in cache_environment} == cache_environment
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert "fsGroup" not in pod["securityContext"]
    assert "fsGroupChangePolicy" not in pod["securityContext"]

    for mutation, error in (
        (
            lambda candidate: candidate["models"][0]["stages"][1]["mounts"][3].__setitem__(  # type: ignore[index,union-attr]
                "artifact_id", "openfold3-openbind-0"
            ),
            "cannot impersonate",
        ),
        (
            lambda candidate: candidate["models"][0]["stages"][1]["mounts"][3].__setitem__(  # type: ignore[index,union-attr]
                "claim_namespace", "tenant-a"
            ),
            "execution namespace",
        ),
        (
            lambda candidate: candidate["models"][0]["stages"][1]["mounts"][3].__setitem__(  # type: ignore[index,union-attr]
                "mount_path", "/cache/tenant"
            ),
            "model/stage allowlist",
        ),
        (
            lambda candidate: candidate["models"][0]["stages"][1]["environment"].pop(  # type: ignore[index,union-attr]
                "TORCH_EXTENSIONS_DIR"
            ),
            "warm-cache contract",
        ),
    ):
        candidate = deepcopy(execution)
        mutation(candidate)
        with pytest.raises(ScientificExecutionMapError, match=error):
            load(candidate)


def runtime_plan() -> AdapterExecutionPlan:
    controller_plan = ScientificBatchPlan(
        (
            ScientificStagePlan("prepare", resource_class=ResourceClass.CPU),
            ScientificStagePlan("inference", depends_on=("prepare",)),
        )
    )
    mount = RuntimeArtifactMount(
        artifact_id="protenix-common",
        mount_path="/models/protenix-v2/common",
        sub_path="protenix-v2/common",
        expected_content_sha256="c" * 64,
        expected_manifest_sha256="e" * 64,
        readiness_receipt_sha256=sha("localized-protenix-common").removeprefix("sha256:"),
        supplemental_groups=(10001,),
    )
    prepare = StageInvocation(
        stage_id="prepare",
        shard_id="main",
        argv=(
            "protenix-wrapper",
            "prep",
            "--common",
            "/models/protenix-v2/common",
            "--runtime-localization-marker",
            "/mnt/fs2-scientific/work/prepare/main/.fs2/runtime-localization.json",
        ),
        environment=(),
        working_directory="/mnt/fs2-scientific/work/prepare/main",
        consumes=(),
        produces="processed-input",
        collector_id="protenix-prepared-v1",
        validator_id="protenix-prepared-validator-v1",
        handoff_name="processed-envelope",
        namespace="fs2-models",
        local_queue_name="scientific",
        runtime_artifacts=("protenix-common",),
        runtime_mounts=(mount,),
    )
    inference = StageInvocation(
        stage_id="inference",
        shard_id="main",
        argv=(
            "protenix-wrapper",
            "pred",
            "--common",
            "/models/protenix-v2/common",
            "--runtime-localization-marker",
            "/mnt/fs2-scientific/work/inference/main/.fs2/runtime-localization.json",
        ),
        environment=(),
        working_directory="/mnt/fs2-scientific/work/inference/main",
        consumes=("processed-input",),
        produces="result-manifest",
        collector_id="protenix-results-v1",
        validator_id="protenix-validator-v1",
        handoff_name=None,
        namespace="fs2-models",
        local_queue_name="scientific",
        materializations=(
            ArtifactMaterialization(
                "processed-input",
                "/mnt/fs2-scientific/work/inference/main/prepared",
                MaterializationMode.EXTRACT_TAR,
            ),
        ),
        runtime_artifacts=("protenix-common",),
        runtime_mounts=(mount,),
    )
    return AdapterExecutionPlan(
        model_id="protenix-v2",
        variant_id="upstream-v2-0-0",
        source_revision="b" * 40,
        request_sha256="d" * 64,
        controller_plan=controller_plan,
        invocations=(prepare, inference),
        required_model_artifacts=("protenix-common",),
    )


def test_runtime_artifact_file_manifest_is_hard_admission_gate(tmp_path: Path) -> None:
    plan = runtime_plan()
    complete = runtime_execution_map(tmp_path)
    access = ArtifactAccessContext(profile="public", receipt_digest=None)
    localized = complete.verify_runtime_artifacts(runtime_profile(), plan, access)
    assert tuple(item.path for item in localized[0].files) == (
        "components.cif",
        "components.cif.rdkit_mol.pkl",
        "clusters-by-entity-40.txt",
        "obsolete_release_date.csv",
    )
    with pytest.raises(ScientificExecutionMapError, match="localization evidence differs"):
        runtime_execution_map(tmp_path, omit_file=True).verify_runtime_artifacts(runtime_profile(), plan, access)


def test_controller_owns_live_localization_receipt_binding(tmp_path: Path) -> None:
    plan = runtime_plan()
    invocations = tuple(
        replace(
            invocation,
            runtime_mounts=tuple(replace(mount, readiness_receipt_sha256=None) for mount in invocation.runtime_mounts),
        )
        for invocation in plan.invocations
    )
    adapter_plan = replace(plan, invocations=invocations)
    localized = runtime_execution_map(tmp_path).verify_runtime_artifacts(
        runtime_profile(),
        adapter_plan,
        ArtifactAccessContext(profile="public", receipt_digest=None),
    )
    assert localized[0].localization_receipt_digest == sha("localized-protenix-common")
    assert adapter_plan.invocations[0].runtime_mounts[0].expected_content_sha256 == "c" * 64
    assert adapter_plan.invocations[0].runtime_mounts[0].artifact_manifest_sha256 == "e" * 64


def test_disabled_profile_runtime_artifact_may_be_unused_but_not_undeclared(tmp_path: Path) -> None:
    base = runtime_profile()
    value = dict(base.value)
    requirements = list(value["artifact_requirements"])  # type: ignore[arg-type]
    requirements.append(
        {
            "artifact_id": "disabled-reference-databases",
            "content_digest_sha256": "e" * 64,
            "required_files": ["disabled.db"],
            "file_manifest": [{"path": "disabled.db", "sha256": "f" * 64, "size_bytes": 1}],
        }
    )
    value["artifact_requirements"] = requirements
    enriched_profile = ScientificWorkloadProfile(MappingProxyType(value))
    renderer = runtime_execution_map(tmp_path)
    access = ArtifactAccessContext(profile="public", receipt_digest=None)
    assert renderer.verify_runtime_artifacts(enriched_profile, runtime_plan(), access)

    base_plan = runtime_plan()
    undeclared_invocations = tuple(
        replace(
            invocation,
            runtime_artifacts=("unknown-artifact",),
            runtime_mounts=(replace(invocation.runtime_mounts[0], artifact_id="unknown-artifact"),),
        )
        for invocation in base_plan.invocations
    )
    undeclared = replace(
        base_plan,
        invocations=undeclared_invocations,
        required_model_artifacts=("unknown-artifact",),
    )
    with pytest.raises(ScientificExecutionMapError, match="no verified localization"):
        renderer.verify_runtime_artifacts(enriched_profile, undeclared, access)


def scheduling(plan: ScientificBatchPlan) -> SchedulingSnapshot:
    return SchedulingSnapshot(
        policy_revision=sha("scheduling"),
        captured_at=NOW,
        service_class=ServiceClass.CUSTOMER_BATCH,
        tenant_queue="tenant-academic",
        model_lane="alphafold3",
        stages=tuple(
            StageSchedulingDecision(
                stage_id=stage.stage_id,
                execution_namespace="fs2-models",
                resolved_cluster_queue="inference",
                resolved_local_queue="scientific",
                workload_priority_class="customer-batch",
                workload_priority_value=10,
                resolved_pool_preference=("h100",),
                admitted_resource_flavor=None,
                accelerator_resource_name="nvidia.com/gpu",
                accelerator_count=0 if stage.resource_class is ResourceClass.CPU else 1,
                max_queue_seconds=600,
                max_execution_seconds=3600,
                checkpoint_mode=stage.checkpoint_mode,
                preemption_mode=stage.preemption_mode,
            )
            for stage in plan.stages
        ),
    )


def test_runtime_binding_renders_exact_subpath_and_never_requests_recursive_chown(tmp_path: Path) -> None:
    plan = runtime_plan()
    renderer = runtime_execution_map(tmp_path)
    access = ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="tenant-a")
    localized = renderer.verify_runtime_artifacts(runtime_profile(), plan, access)
    snapshot = scheduling(plan.controller_plan)
    invocation = plan.invocation("prepare", "main")
    resource = WorkloadResource(
        operation_id=uuid4(),
        batch_id=uuid4(),
        workload_id=uuid4(),
        attempt_id=uuid4(),
        stage_id="prepare",
        shard_id="main",
        attempt_number=1,
        tenant_id="tenant-a",
        model_id="protenix-v2",
        variant_id="upstream-v2-0-0",
        input_artifact_id=uuid4(),
        service_class=ServiceClass.CUSTOMER_BATCH,
        scheduling_snapshot_digest=snapshot.digest,
        namespace="fs2-models",
        name="protenix-prepare",
        kind=WorkloadKind.JOB,
        scheduling=snapshot.stage("prepare"),
        invocation=invocation,
        access_context=access,
        runtime_artifacts=localized,
    )
    manifest = renderer.render(resource)
    pod = manifest["spec"]["template"]["spec"]  # type: ignore[index]
    model = pod["containers"][0]
    assert {item["mountPath"] for item in model["volumeMounts"]} == {
        "/mnt/fs2-scientific",
        "/models/protenix-v2/common",
    }
    runtime_mount = next(item for item in model["volumeMounts"] if item["name"] == "model-artifacts")
    assert runtime_mount["subPath"] == "protenix-v2/common"
    assert runtime_mount["readOnly"] is True
    assert pod["securityContext"]["supplementalGroups"] == [10001]
    assert pod["securityContext"]["supplementalGroupsPolicy"] == "Strict"
    assert "fsGroup" not in pod["securityContext"]
    assert "fsGroupChangePolicy" not in pod["securityContext"]
    runtime_env = next(item["value"] for item in model["env"] if item["name"] == "FS2_RUNTIME_ARTIFACTS_JSON")
    marker = json.loads(runtime_env)
    assert marker["schema"] == companion.RUNTIME_LOCALIZATION_SCHEMA
    assert marker["attempt_id"] == str(resource.attempt_id)
    assert marker["artifacts"][0]["readiness_receipt_sha256"] == sha("localized-protenix-common").removeprefix(
        "sha256:"
    )
    assert marker["artifacts"][0]["sub_path"] == "protenix-v2/common"
    assert marker["artifacts"][0]["expected_manifest_sha256"] == "e" * 64
    model_environment = {item["name"]: item["value"] for item in model["env"]}
    assert model_environment["FS2_RUNTIME_LOCALIZATION_MARKER"] == (
        "/mnt/fs2-scientific/work/prepare/main/.fs2/runtime-localization.json"
    )
    prepare = pod["initContainers"][0]
    assert prepare["env"] == [{"name": "FS2_RUNTIME_ARTIFACTS_JSON", "value": runtime_env}]

    tampered = replace(resource, runtime_artifacts=(replace(localized[0], mount_path="/models/changed"),))
    with pytest.raises(ScientificExecutionMapError, match="lost its verified localization"):
        renderer.render(tampered)


@pytest.mark.asyncio
async def test_af3_gpu_stage_materializes_only_validated_cpu_handoff() -> None:
    controller_plan = ScientificBatchPlan(
        (
            ScientificStagePlan("data-pipeline", resource_class=ResourceClass.CPU),
            ScientificStagePlan("inference", depends_on=("data-pipeline",)),
        )
    )
    raw_id = uuid4()
    raw = ScientificInputArtifact(
        logical_artifact_id="raw-request",
        semantic_type="alphafold-input/v1",
        artifact_id=raw_id,
        digest=sha("raw"),
        size_bytes=3,
        media_type="application/json",
    )
    cpu = StageInvocation(
        stage_id="data-pipeline",
        shard_id="main",
        argv=("run_alphafold.py", "--run_data_pipeline=true", "--run_inference=false"),
        environment=(),
        working_directory="/mnt/fs2-scientific/work/data-pipeline/main",
        consumes=("raw-request",),
        produces="processed-input",
        collector_id="af3-processed-input-v1",
        validator_id="af3-processed-input-validator-v1",
        handoff_name="processed-json",
        namespace="fs2-academic-poc",
        local_queue_name="academic-scientific",
        materializations=(
            ArtifactMaterialization(
                "raw-request",
                "/mnt/fs2-scientific/work/data-pipeline/main/input.json",
                MaterializationMode.COPY_FILE,
            ),
        ),
    )
    gpu = StageInvocation(
        stage_id="inference",
        shard_id="main",
        argv=(
            "run_alphafold.py",
            "--run_data_pipeline=false",
            "--run_inference=true",
            "--runtime-localization-marker",
            "/mnt/fs2-scientific/work/inference/main/.fs2/runtime-localization.json",
        ),
        environment=(),
        working_directory="/mnt/fs2-scientific/work/inference/main",
        consumes=("processed-input",),
        produces="structure-results",
        collector_id="af3-structure-results-v1",
        validator_id="af3-structure-validator-v1",
        handoff_name=None,
        namespace="fs2-academic-poc",
        local_queue_name="academic-scientific",
        materializations=(
            ArtifactMaterialization(
                "processed-input",
                "/mnt/fs2-scientific/work/inference/main/prepared",
                MaterializationMode.EXTRACT_TAR,
            ),
        ),
        runtime_artifacts=("af3-parameters",),
        runtime_mounts=(
            RuntimeArtifactMount(
                artifact_id="af3-parameters",
                mount_path="/opt/fs2/artifacts/af3-parameters",
                expected_content_sha256=sha("af3-parameters").removeprefix("sha256:"),
                authorization_receipt_sha256=sha("af3-access").removeprefix("sha256:"),
                readiness_receipt_sha256=sha("af3-localized").removeprefix("sha256:"),
            ),
        ),
    )
    execution = AdapterExecutionPlan(
        model_id="alphafold3",
        variant_id="upstream-v3-0-4",
        source_revision="b" * 40,
        request_sha256="d" * 64,
        controller_plan=controller_plan,
        invocations=(cpu, gpu),
        required_model_artifacts=("af3-parameters",),
    )
    localized = RuntimeArtifactLocalization(
        logical_artifact_id="af3-parameters",
        mount_path="/opt/fs2/artifacts/af3-parameters",
        content_digest=sha("af3-parameters"),
        files=(RuntimeArtifactFile("af3.bin.zst", sha("af3.bin.zst"), 10),),
        localization_receipt_digest=sha("af3-localized"),
    )
    manifest_id = uuid4()
    verified_manifest = VerifiedInputManifest(
        manifest_id="af3-inputs",
        manifest_artifact_id=manifest_id,
        manifest_digest=sha("manifest"),
        entries=(raw,),
    )
    access = ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="ordinary-poc")
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()
    controller = ScientificBatchController(
        repository=repository,
        cluster=cluster,
        controller_id="controller-a",
        namespace="fs2-models",
        clock=lambda: NOW,
    )
    operation_id = uuid4()
    await controller.admit(
        operation_id=operation_id,
        tenant_id="ordinary-poc",
        model_id="alphafold3",
        variant_id="upstream-v3-0-4",
        input_artifact_id=manifest_id,
        plan=controller_plan,
        scheduling=af3_scheduling().freeze_for_execution(
            service_class="customer-batch",
            model_id="alphafold3",
            tenant_id="ordinary-poc",
            profile=af3_qualified_profile()[0].value,
            execution=execution,
            captured_at=NOW,
        ),
        execution_plan=execution,
        access_context=access,
        input_manifest=verified_manifest,
        runtime_artifacts=(localized,),
    )
    assert not cluster.apply_history
    assert repository.records[operation_id].runtime_artifacts == (localized,)
    assert state_from_value(state_to_value(repository.records[operation_id])) == repository.records[operation_id]
    await controller.reconcile_once()
    assert cluster.apply_history[0].namespace == "fs2-academic-poc"
    cpu_attempt = repository.records[operation_id].stage("data-pipeline").attempts[0]
    cluster.set_observation(
        cpu_attempt.workload,
        WorkloadObservation(
            ref=cpu_attempt.workload,
            attempt_id=cpu_attempt.attempt_id,
            state=WorkloadState.SUCCEEDED,
            phases=(LifecyclePhase.ADMITTED, LifecyclePhase.ACTIVE_COMPUTE),
        ),
    )
    processed_id = uuid4()
    repository.put_commit(
        AttemptArtifactCommit(
            operation_id=operation_id,
            stage_id="data-pipeline",
            attempt_ids=(cpu_attempt.attempt_id,),
            logical_artifact_id="processed-input",
            handoff_artifact_id=processed_id,
            handoff_digest=sha("processed"),
            handoff_size_bytes=9,
            handoff_media_type="application/x-tar",
            handoff_compression=None,
            manifest_artifact_id=uuid4(),
            validation_artifact_id=uuid4(),
            manifest_digest=sha("processed-manifest"),
            validation_digest=sha("processed-validation"),
            committed_at=NOW,
            validated_at=NOW,
            semantic_valid=True,
            collector_id=cpu.collector_id,
            validator_id=cpu.validator_id,
        )
    )
    for _ in range(3):
        await controller.reconcile_once()
    gpu_resource = cluster.apply_history[-1]
    assert gpu_resource.stage_id == "inference"
    assert gpu_resource.namespace == "fs2-academic-poc"
    assert tuple(item.artifact_id for item in gpu_resource.materializations) == (processed_id,)
    assert raw_id not in {item.artifact_id for item in gpu_resource.materializations}
    assert gpu_resource.access_context == access
    assert gpu_resource.runtime_artifacts == (localized,)
    assert cluster.delete_history[0].namespace == "fs2-academic-poc"
    repository.force_cancel(operation_id)
    await controller.reconcile_once()
    assert all(ref.namespace == "fs2-academic-poc" for ref in cluster.delete_calls)


@pytest.mark.parametrize("model", ["alphafold3", "protenix-v2"])
def test_processed_envelope_relocates_with_relative_marker_contract(
    model: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "scientific"
    root.mkdir()
    monkeypatch.setattr(companion, "_ROOT", root)
    processed = b'{"dialect":"alphafold3","sequences":[]}'
    marker = {
        "schema": f"fs2-serve.nebius.ai/{model}-processed-envelope/v1",
        "logical_artifact_id": "processed-input",
        "member": "processed.json",
        "sha256": hashlib.sha256(processed).hexdigest(),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in (
            ("processed.json", processed),
            ("provenance.json", json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o400
            archive.addfile(member, io.BytesIO(content))
    envelope = buffer.getvalue()

    class Client:
        def download(self, artifact_id, *, expected_digest, expected_size_bytes, expected_media_type):
            assert artifact_id == envelope_id
            assert expected_digest == sha(envelope)
            assert expected_size_bytes == len(envelope)
            assert expected_media_type == "application/x-tar"
            return envelope

    envelope_id = uuid4()
    for attempt in ("attempt-one", "attempt-two"):
        destination = root / "work" / model / attempt / "prepared"
        companion.materialize_artifact(
            client=Client(),  # type: ignore[arg-type]
            artifact_id=envelope_id,
            destination=destination,
            mode=MaterializationMode.EXTRACT_TAR,
            compression=None,
            yaml_name=None,
            reuse_prefix=None,
            expected_digest=sha(envelope),
            expected_size_bytes=len(envelope),
            expected_media_type="application/x-tar",
        )
        assert (destination / "processed.json").read_bytes() == processed
        relocated = json.loads((destination / "provenance.json").read_bytes())
        assert relocated == marker
        assert not any(key.endswith("path") for key in relocated)


@pytest.mark.asyncio
async def test_missing_runtime_localization_fails_before_any_gpu_apply() -> None:
    execution = runtime_plan()
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()
    controller = ScientificBatchController(
        repository=repository,
        cluster=cluster,
        controller_id="controller-a",
        namespace="fs2-models",
    )
    manifest_id = uuid4()
    with pytest.raises(ValueError, match="runtime artifact"):
        await controller.admit(
            operation_id=uuid4(),
            tenant_id="tenant-a",
            model_id="protenix-v2",
            variant_id="upstream-v2-0-0",
            input_artifact_id=manifest_id,
            plan=execution.controller_plan,
            scheduling=scheduling(execution.controller_plan),
            execution_plan=execution,
            access_context=ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="tenant-a"),
            input_manifest=VerifiedInputManifest(
                manifest_id="inputs",
                manifest_artifact_id=manifest_id,
                manifest_digest=sha("inputs"),
                entries=(
                    ScientificInputArtifact(
                        logical_artifact_id="unused",
                        semantic_type="request/v1",
                        artifact_id=uuid4(),
                        digest=sha("unused"),
                        size_bytes=1,
                        media_type="application/json",
                    ),
                ),
            ),
            runtime_artifacts=(),
        )
    assert not repository.records
    assert not cluster.apply_history


def test_companion_materializes_collects_validates_and_commits_exact_handoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "scientific"
    root.mkdir()
    monkeypatch.setattr(companion, "_ROOT", root)
    workspace = root / "work" / "fold" / "main"
    runtime_marker = {
        "schema": companion.RUNTIME_LOCALIZATION_SCHEMA,
        "operation_id": str(uuid4()),
        "attempt_id": str(uuid4()),
        "tenant_id": "tenant-a",
        "model_id": "fold-model",
        "variant_id": "fold-model-h100",
        "stage_id": "fold",
        "artifacts": [],
    }
    companion.prepare_workspace(
        workspace,
        runtime_localization_json=json.dumps(runtime_marker),
    )
    assert json.loads((workspace / ".fs2/runtime-localization.json").read_text()) == runtime_marker
    input_bytes = b">target\nACDE\n"

    class Client:
        def __init__(self) -> None:
            self.uploads: dict[str, tuple[bytes, dict[str, object]]] = {}
            self.committed: dict[str, object] | None = None

        def download(self, artifact_id, *, expected_digest, expected_size_bytes, expected_media_type):
            assert artifact_id == input_id
            assert expected_digest == sha(input_bytes)
            assert expected_size_bytes == len(input_bytes)
            assert expected_media_type == "text/plain"
            return input_bytes

        def upload(self, *, identity, content, media_type, compression):
            digest = hashlib.sha256(content).hexdigest()
            ref: dict[str, object] = {
                "artifact_id": str(uuid4()),
                "sha256": digest,
                "size_bytes": len(content),
                "media_type": media_type,
            }
            if compression is not None:
                ref["compression"] = compression
            self.uploads[identity] = (content, ref)
            return ref

        def commit(self, **value):
            self.committed = value

    input_id = uuid4()
    client = Client()
    companion.materialize_artifact(
        client=client,  # type: ignore[arg-type]
        artifact_id=input_id,
        destination=workspace / "input.fasta",
        mode=MaterializationMode.COPY_FILE,
        compression=None,
        yaml_name=None,
        reuse_prefix=None,
        expected_digest=sha(input_bytes),
        expected_size_bytes=len(input_bytes),
        expected_media_type="text/plain",
    )
    assert (workspace / "input.fasta").read_bytes() == input_bytes

    invocation = StageInvocation(
        stage_id="fold",
        shard_id="main",
        argv=("fold-model", "--input", "/mnt/fs2-scientific/work/fold/main/input.fasta"),
        environment=(),
        working_directory="/mnt/fs2-scientific/work/fold/main",
        consumes=("request-fasta",),
        produces="fold-result",
        collector_id="test-contained-fold-collector-v1",
        validator_id="test-contained-fold-validator-v1",
        handoff_name="structure",
        materializations=(
            ArtifactMaterialization(
                "request-fasta",
                "/mnt/fs2-scientific/work/fold/main/input.fasta",
                MaterializationMode.COPY_FILE,
            ),
        ),
    )

    def collect(bound: StageInvocation, path: Path) -> CollectedStageOutput:
        assert bound == invocation
        assert path == workspace
        output = path / "structure.pdb"
        output.write_bytes(b"ATOM      1  CA  ALA A   1\n")
        return CollectedStageOutput(
            artifacts=(
                CollectedArtifactFile(
                    name="structure",
                    semantic_type="protein-structure-pdb/v1",
                    path=output,
                    media_type="chemical/x-pdb",
                ),
            ),
            validation={"validator_id": bound.validator_id, "status": "passed"},
        )

    register_adapter(
        model_id="test-contained-fold-model",
        compiler=lambda *args, **kwargs: None,  # type: ignore[arg-type,return-value]
        collectors={invocation.collector_id: collect},
    )
    companion.collect_and_commit(
        client=client,  # type: ignore[arg-type]
        collector_id=invocation.collector_id,
        validator_id=invocation.validator_id,
        invocation_json=json.dumps(
            {
                "stage_id": invocation.stage_id,
                "shard_id": invocation.shard_id,
                "argv": list(invocation.argv),
                "environment": [],
                "working_directory": invocation.working_directory,
                "consumes": list(invocation.consumes),
                "produces": invocation.produces,
                "collector_id": invocation.collector_id,
                "validator_id": invocation.validator_id,
                "handoff_name": invocation.handoff_name,
                "max_output_artifacts": invocation.max_output_artifacts,
                "max_output_bytes": invocation.max_output_bytes,
                "materializations": [
                    {
                        "artifact_id": "request-fasta",
                        "destination": "/mnt/fs2-scientific/work/fold/main/input.fasta",
                        "mode": "copy-file",
                        "compression": None,
                        "yaml_name": None,
                        "reuse_prefix": None,
                    }
                ],
                "runtime_artifacts": [],
                "runtime_mounts": [],
            }
        ),
        workspace=workspace,
        catalog_dir=CATALOG_ROOT,
        max_artifacts=invocation.max_output_artifacts,
        max_output_bytes=invocation.max_output_bytes,
        poll_seconds=0.001,
    )
    structure_ref = client.uploads["fold-result:structure"][1]
    manifest_bytes = client.uploads["fold-result:manifest"][0]
    manifest = json.loads(manifest_bytes)
    assert manifest["entries"][0]["artifact"] == structure_ref
    validation = json.loads(client.uploads["fold-result:validation"][0])
    assert validation["status"] == "passed"
    assert validation["logical_output_id"] == "fold-result"

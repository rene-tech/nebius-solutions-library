from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest
from conftest import CATALOG_ROOT, SOLUTION_ROOT
from jsonschema import Draft202012Validator
from scientific_batch_fakes import FakeScientificBatchCluster, FakeScientificBatchRepository

from fs2_serve.crypto import KeyedHasher
from fs2_serve.scientific_artifacts import (
    ArtifactAccess,
    ArtifactAccessProfile,
    ArtifactDirection,
    ArtifactRecord,
    artifact_storage_key,
)
from fs2_serve.scientific_batch import companion
from fs2_serve.scientific_batch.adapters import (
    CollectedArtifactFile,
    CollectedStageOutput,
    CollectionPendingError,
    register_adapter,
)
from fs2_serve.scientific_batch.artifact_bridge import ArtifactServiceBridge
from fs2_serve.scientific_batch.capability import ScientificWorkloadCapabilityAuthority
from fs2_serve.scientific_batch.codec import state_from_value, state_to_value
from fs2_serve.scientific_batch.controller import ScientificBatchController
from fs2_serve.scientific_batch.execution import (
    FileScientificManifestRenderer,
    ScientificExecutionMapError,
    _invocation_json,
    _runtime_volume_sub_path,
)
from fs2_serve.scientific_batch.models import (
    AdapterExecutionPlan,
    ArtifactAccessContext,
    ArtifactMaterialization,
    AttemptArtifactCommit,
    LifecyclePhase,
    MaterializationMode,
    ResolvedArtifactMaterialization,
    ResourceClass,
    RuntimeArtifactAdmissionRole,
    RuntimeArtifactAdmissionSpec,
    RuntimeArtifactMount,
    RuntimeArtifactTreeKind,
    SchedulingAdmission,
    SchedulingSnapshot,
    ScientificBatchPlan,
    ScientificInputArtifact,
    ScientificStagePlan,
    ServiceClass,
    StageInvocation,
    StagePlacementClass,
    StageResourceEnvelope,
    StageSchedulingDecision,
    StageWorkspaceDocument,
    VerifiedInputManifest,
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

NOW = datetime(2026, 9, 2, 21, tzinfo=UTC)


def sha(value: bytes | str) -> str:
    raw = value.encode() if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def prepare_invocation_json(stage_id: str) -> str:
    return json.dumps(
        {
            "stage_id": stage_id,
            "shard_id": "main",
            "argv": ["scientific-stage"],
            "environment": [],
            "working_directory": f"/mnt/fs2-scientific/work/{stage_id}/main",
            "consumes": [],
            "produces": f"{stage_id}-result",
            "collector_id": "test-collector-v1",
            "validator_id": "test-validator-v1",
            "handoff_name": None,
            "max_output_artifacts": 1,
            "max_output_bytes": 1024,
            "materializations": [],
            "runtime_artifacts": [],
            "runtime_mounts": [],
            "workspace_documents": [],
            "runtime_admission": None,
        }
    )


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


def runtime_execution_map(
    tmp_path: Path,
    *,
    omit_file: bool = False,
    runtime_cache: bool = False,
    runtime_cache_claim: str = "fs2-scientific-runtime-cache",
    include_unused_variant_source: bool = False,
    unused_variant_source: str | None = None,
) -> FileScientificManifestRenderer:
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
        "models": [
            {
                "model_id": "protenix-v2",
                "variant_id": "upstream-v2-0-0",
                "workload_namespace": "fs2-models",
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
                        "image": "registry.test/protenix@sha256:" + "a" * 64,
                        "collector_id": "protenix-prepared-v1",
                        "validator_id": "protenix-prepared-validator-v1",
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
                            {
                                "name": "model-artifacts",
                                "kind": "reference",
                                "claim_name": "scientific-model-artifacts",
                                "host_path": None,
                                "mount_path": "/models",
                                "sub_path": None,
                                "read_only": True,
                            },
                            *(
                                [
                                    {
                                        "name": "unused-variant",
                                        "kind": "reference",
                                        "claim_name": "scientific-model-artifacts",
                                        "host_path": None,
                                        "mount_path": "/models/unused-variant",
                                        "sub_path": unused_variant_source,
                                        "read_only": True,
                                    }
                                ]
                                if include_unused_variant_source
                                else []
                            ),
                            *(
                                [
                                    {
                                        "name": "runtime-cache",
                                        "kind": "runtime-cache",
                                        "claim_name": runtime_cache_claim,
                                        "host_path": None,
                                        "mount_path": "/cache",
                                        "sub_path": None,
                                        "read_only": False,
                                    }
                                ]
                                if runtime_cache
                                else []
                            ),
                        ],
                        "service_account_name": "scientific-runner",
                        "workspace_uid": 10001,
                        "workspace_gid": 10001,
                        "resources": {
                            "requests": {"cpu": "4", "memory": "32Gi", "ephemeral_storage": "20Gi"},
                            "limits": {"cpu": "4", "memory": "32Gi", "ephemeral_storage": "20Gi"},
                        },
                        "active_deadline_seconds": 3600,
                        "termination_grace_seconds": 60,
                        "environment": (
                            {"JAX_COMPILATION_CACHE_DIR": "/cache/protenix-v2/jax"} if runtime_cache else {}
                        ),
                        "required_node_labels": {},
                    },
                    {
                        "stage_id": "inference",
                        "image": "registry.test/protenix@sha256:" + "a" * 64,
                        "collector_id": "protenix-results-v1",
                        "validator_id": "protenix-validator-v1",
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
                            {
                                "name": "model-artifacts",
                                "kind": "reference",
                                "claim_name": "scientific-model-artifacts",
                                "host_path": None,
                                "mount_path": "/models",
                                "sub_path": None,
                                "read_only": True,
                            },
                        ],
                        "service_account_name": "scientific-runner",
                        "workspace_uid": 10001,
                        "workspace_gid": 10001,
                        "resources": {
                            "requests": {"cpu": "4", "memory": "32Gi", "ephemeral_storage": "20Gi"},
                            "limits": {"cpu": "4", "memory": "32Gi", "ephemeral_storage": "20Gi"},
                        },
                        "active_deadline_seconds": 3600,
                        "termination_grace_seconds": 60,
                        "environment": {},
                        "required_node_labels": {},
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
        readiness_receipt_sha256=None,
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
        workload_namespace="fs2-models",
        route_namespace="fs2-models",
        stages=tuple(
            StageSchedulingDecision(
                stage_id=stage.stage_id,
                resource_class=stage.resource_class,
                resolved_cluster_queue="inference",
                resolved_local_queue="scientific",
                workload_priority_class="customer-batch",
                workload_priority_value=10,
                resolved_pool_preference=(() if stage.resource_class is ResourceClass.CPU else ("h100",)),
                accelerator_resource_name=(None if stage.resource_class is ResourceClass.CPU else "nvidia.com/gpu"),
                accelerator_count=0 if stage.resource_class is ResourceClass.CPU else 1,
                max_queue_seconds=600,
                max_execution_seconds=3600,
                checkpoint_mode=stage.checkpoint_mode,
                preemption_mode=stage.preemption_mode,
            )
            for stage in plan.stages
        ),
    )


@pytest.mark.parametrize(
    ("source", "binding", "expected"),
    (
        (None, None, None),
        ("physical/tree", None, "physical/tree"),
        (None, "logical/tree", "logical/tree"),
        ("physical/tree", "child.bin", "physical/tree/child.bin"),
        ("alphafold3/af3.bin.zst", "alphafold3/af3.bin.zst", "alphafold3/af3.bin.zst"),
    ),
)
def test_runtime_volume_sub_path_composes_relative_bindings_but_deduplicates_exact_identity(
    source: str | None,
    binding: str | None,
    expected: str | None,
) -> None:
    assert _runtime_volume_sub_path(source, binding) == expected


def test_runtime_binding_renders_exact_subpath_and_never_requests_recursive_chown(tmp_path: Path) -> None:
    plan = runtime_plan()
    renderer = runtime_execution_map(tmp_path)
    access = ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="tenant-a")
    localized = renderer.verify_runtime_artifacts(runtime_profile(), plan, access)
    plan = renderer.bind_runtime_artifacts(runtime_profile(), plan, access, localized)
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
        execution_map_sha256=plan.execution_map_sha256,
        execution_binding=plan.execution_binding("prepare"),
    )
    manifest = renderer.render(resource)
    pod = manifest["spec"]["template"]["spec"]  # type: ignore[index]
    assert "affinity" not in pod
    model = pod["containers"][0]
    assert {item["mountPath"] for item in model["volumeMounts"]} == {
        "/mnt/fs2-scientific",
        "/models/protenix-v2/common",
    }
    runtime_mount = next(item for item in model["volumeMounts"] if item["name"] == "model-artifacts")
    assert runtime_mount["subPath"] == "protenix-v2/common"
    assert runtime_mount["readOnly"] is True
    assert pod["securityContext"]["supplementalGroups"] == [10001]
    assert "fsGroup" not in pod["securityContext"]
    assert "fsGroupChangePolicy" not in pod["securityContext"]
    for container in (*pod["initContainers"], *pod["containers"]):
        assert container["securityContext"]["runAsUser"] == 10001
        assert container["securityContext"]["runAsGroup"] == 10001
    runtime_env = next(item["value"] for item in model["env"] if item["name"] == "FS2_RUNTIME_ARTIFACTS_JSON")
    marker = json.loads(runtime_env)
    assert marker["schema"] == companion.RUNTIME_LOCALIZATION_SCHEMA
    assert marker["attempt_id"] == str(resource.attempt_id)
    assert marker["artifacts"][0]["readiness_receipt_sha256"] == sha("localized-protenix-common").removeprefix(
        "sha256:"
    )
    assert marker["artifacts"][0]["sub_path"] == "protenix-v2/common"
    model_environment = {item["name"]: item["value"] for item in model["env"]}
    assert model_environment["FS2_RUNTIME_LOCALIZATION_MARKER"] == (
        "/mnt/fs2-scientific/work/prepare/main/.fs2/runtime-localization.json"
    )
    prepare = pod["initContainers"][0]
    assert {item["name"] for item in prepare["env"]} == {
        "FS2_RUNTIME_ARTIFACTS_JSON",
        "FS2_STAGE_INVOCATION_JSON",
    }
    verifier = pod["initContainers"][1]
    assert verifier["name"] == "verify-runtime-artifacts"
    assert {item["mountPath"] for item in verifier["volumeMounts"]} == {
        "/mnt/fs2-scientific",
        "/models/protenix-v2/common",
    }
    collector = next(item for item in pod["containers"] if item["name"] == "artifact-collector")
    collector_environment = {item["name"]: item["value"] for item in collector["env"]}
    assert collector_environment["FS2_CATALOG_DIR"] == "/opt/fs2/catalog"

    tampered = replace(resource, runtime_artifacts=(replace(localized[0], mount_path="/models/changed"),))
    with pytest.raises(ScientificExecutionMapError, match="lost its verified localization"):
        renderer.render(tampered)


@pytest.mark.parametrize(
    ("unused_variant_source", "allowed"),
    [
        ("variants/unused/sha256/" + "d" * 64, True),
        (None, False),
    ],
)
def test_runtime_binding_emits_only_the_selected_exact_variant_source(
    tmp_path: Path,
    unused_variant_source: str | None,
    allowed: bool,
) -> None:
    plan = runtime_plan()
    renderer = runtime_execution_map(
        tmp_path,
        include_unused_variant_source=True,
        unused_variant_source=unused_variant_source,
    )
    access = ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="tenant-a")
    localized = renderer.verify_runtime_artifacts(runtime_profile(), plan, access)
    bound = renderer.bind_runtime_artifacts(runtime_profile(), plan, access, localized)
    snapshot = scheduling(bound.controller_plan)
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
        name="protenix-prepare-exact-variant",
        kind=WorkloadKind.JOB,
        scheduling=snapshot.stage("prepare"),
        invocation=bound.invocation("prepare", "main"),
        access_context=access,
        runtime_artifacts=localized,
        execution_map_sha256=bound.execution_map_sha256,
        execution_binding=bound.execution_binding("prepare"),
    )

    if not allowed:
        with pytest.raises(ScientificExecutionMapError, match="unbound broad runtime artifact volume"):
            renderer.render(resource)
        return

    pod = renderer.render(resource)["spec"]["template"]["spec"]  # type: ignore[index]
    assert "unused-variant" not in {item["name"] for item in pod["volumes"]}
    for container in (*pod["initContainers"], *pod["containers"]):
        assert "unused-variant" not in {item["name"] for item in container["volumeMounts"]}


def test_runtime_cache_is_terraform_owned_model_only_and_never_triggers_recursive_chown(tmp_path: Path) -> None:
    plan = runtime_plan()
    renderer = runtime_execution_map(tmp_path, runtime_cache=True)
    access = ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="tenant-a")
    localized = renderer.verify_runtime_artifacts(runtime_profile(), plan, access)
    bound = renderer.bind_runtime_artifacts(runtime_profile(), plan, access, localized)
    snapshot = scheduling(bound.controller_plan)
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
        name="protenix-prepare-cache",
        kind=WorkloadKind.JOB,
        scheduling=snapshot.stage("prepare"),
        invocation=bound.invocation("prepare", "main"),
        access_context=access,
        runtime_artifacts=localized,
        execution_map_sha256=bound.execution_map_sha256,
        execution_binding=bound.execution_binding("prepare"),
    )
    pod = renderer.render(resource)["spec"]["template"]["spec"]  # type: ignore[index]
    model = pod["containers"][0]
    mounts = {item["name"]: item for item in model["volumeMounts"]}
    assert mounts["runtime-cache"] == {
        "name": "runtime-cache",
        "mountPath": "/cache",
        "readOnly": False,
    }
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["runtime-cache"]["persistentVolumeClaim"] == {
        "claimName": "fs2-scientific-runtime-cache",
        "readOnly": False,
    }
    assert all(
        "runtime-cache" not in {item["name"] for item in container["volumeMounts"]}
        for container in (*pod["initContainers"], pod["containers"][1])
    )
    assert "fsGroup" not in pod["securityContext"]
    assert "fsGroupChangePolicy" not in pod["securityContext"]


def test_runtime_cache_refuses_a_non_terraform_claim(tmp_path: Path) -> None:
    with pytest.raises(ScientificExecutionMapError, match="Terraform-owned writable claim"):
        runtime_execution_map(tmp_path, runtime_cache=True, runtime_cache_claim="tenant-supplied-cache")


def test_mounted_runtime_verifier_supports_real_size_aggregate_tree_without_enumeration(tmp_path: Path) -> None:
    small = tmp_path / "common"
    small.mkdir()
    common_file = small / "components.cif"
    common_file.write_bytes(b"components")
    tree = tmp_path / "pyrosetta"
    tree.mkdir()
    tree_identity = {
        "schema": companion.RUNTIME_TREE_IDENTITY_SCHEMA,
        "artifact_id": "bindcraft-pyrosetta-installed-tree",
        "generation": "a" * 64,
        "inventory_algorithm": "fs2-tree-manifest/v1",
        "inventory_sha256": "a" * 64,
        "entry_count": 8_697,
        "directory_count": 796,
        "total_bytes": 3_287_122_494,
        "sub_path": "pyrosetta-bindcraft/site-packages/sha256/" + "a" * 64,
        "read_only": True,
    }
    sidecar = json.dumps(tree_identity, sort_keys=True, separators=(",", ":")).encode()
    (tree / companion.RUNTIME_TREE_IDENTITY_FILE).write_bytes(sidecar)
    marker = {
        "schema": companion.RUNTIME_LOCALIZATION_SCHEMA,
        "operation_id": str(uuid4()),
        "attempt_id": str(uuid4()),
        "tenant_id": "tenant-academic",
        "model_id": "bindcraft",
        "variant_id": "upstream-pyrosetta",
        "stage_id": "design",
        "artifacts": [
            {
                "artifact_id": "protenix-common",
                "mount_path": str(small),
                "content_digest": sha("common-tree"),
                "artifact_manifest_sha256": "1" * 64,
                "localization_receipt_digest": sha("common-receipt"),
                "sub_path": "protenix-v2/common",
                "readiness_receipt_sha256": "2" * 64,
                "authorization_receipt_sha256": None,
                "verification_receipt": None,
                "files": [
                    {
                        "path": "components.cif",
                        "digest": sha(b"components"),
                        "size_bytes": len(b"components"),
                    }
                ],
                "aggregate_tree": None,
            },
            {
                "artifact_id": "bindcraft-pyrosetta-installed-tree",
                "mount_path": str(tree),
                "content_digest": "sha256:" + "a" * 64,
                "artifact_manifest_sha256": hashlib.sha256(sidecar).hexdigest(),
                "localization_receipt_digest": sha("pyrosetta-receipt"),
                "sub_path": tree_identity["sub_path"],
                "readiness_receipt_sha256": "3" * 64,
                "authorization_receipt_sha256": "4" * 64,
                "verification_receipt": None,
                "files": [],
                "aggregate_tree": {
                    "tree_digest": "sha256:" + "a" * 64,
                    "manifest_digest": "sha256:" + hashlib.sha256(sidecar).hexdigest(),
                    "inventory_digest": "sha256:" + "a" * 64,
                    "manifest_algorithm": "fs2-tree-manifest/v1",
                    "file_count": 8_697,
                    "directory_count": 796,
                    "expanded_bytes": 3_287_122_494,
                    "canonical_path": tree_identity["sub_path"],
                    "storage_kind": RuntimeArtifactTreeKind.LOCALIZATION_GENERATION,
                    "marker_relative_path": companion.RUNTIME_TREE_IDENTITY_FILE,
                },
            },
        ],
    }
    encoded = json.dumps(marker, sort_keys=True, separators=(",", ":"))
    companion.verify_runtime_artifacts(runtime_localization_json=encoded)
    (tree / companion.RUNTIME_TREE_IDENTITY_FILE).write_bytes(sidecar + b"\n")
    with pytest.raises(ValueError, match="marker digest differs"):
        companion.verify_runtime_artifacts(runtime_localization_json=encoded)


def _bindcraft_renderer(
    tmp_path: Path,
) -> tuple[FileScientificManifestRenderer, ScientificWorkloadProfile, AdapterExecutionPlan]:
    image_digest = "9ec7eb93208ffd5ec88669e9a6714d8d1e9bffcea1bd5130ab81271095736aa1"
    pyrosetta_digest = "a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d"
    requirements = (
        {
            "artifact_id": "alphafold2-params-bindcraft",
            "content_digest_sha256": "a" * 64,
            "required_files": ["manifest.json"],
            "file_manifest": [{"path": "manifest.json", "sha256": "1" * 64, "size_bytes": 512}],
        },
        {
            "artifact_id": "colabdesign-mpnn-weights-vanilla",
            "content_digest_sha256": "b" * 64,
            "required_files": ["v_48_020.pt"],
            "file_manifest": [
                {
                    "path": "v_48_020.pt",
                    "sha256": "2" * 64,
                    "size_bytes": 6_681_301,
                },
            ],
        },
        {
            "artifact_id": "colabdesign-mpnn-weights-soluble",
            "content_digest_sha256": "c" * 64,
            "required_files": ["v_48_020.pt"],
            "file_manifest": [
                {
                    "path": "v_48_020.pt",
                    "sha256": "3" * 64,
                    "size_bytes": 6_650_310,
                },
            ],
        },
        {
            "artifact_id": "bindcraft-pyrosetta-installed-tree",
            "content_identity": {"digest_sha256": pyrosetta_digest, "size_bytes": 3_287_122_494},
            "required_files": [],
            "file_manifest": [],
        },
    )
    profile = ScientificWorkloadProfile(
        MappingProxyType(
            {
                "model_id": "bindcraft",
                "state": "qualified",
                "route_exposed": True,
                "execution_identity": {
                    "model_revision": "7cd4ace1b7407adf66a50dfefa47de2270f5e4a9",
                    "runtime_image_digest": f"sha256:{image_digest}",
                    "runtime_recipe_sha256": "5" * 64,
                    "workload_recipe_sha256": "6" * 64,
                    "artifact_manifest_digest": "7" * 64,
                    "execution_identity_sha256": "8" * 64,
                },
                "access": {"profile": "academic", "state": "verified"},
                "semantic_validation": {"state": "qualified"},
                "artifact_requirements": list(requirements),
                "workload": {"stages": [{"id": "design"}]},
            }
        )
    )
    localizations = [
        {
            "artifact_id": requirement["artifact_id"],
            "mount_path": mount_path,
            "content_digest": f"sha256:{requirement.get('content_digest_sha256', pyrosetta_digest)}",
            **(
                {
                    "aggregate_tree": {
                        "storage_kind": "localization-generation",
                        "tree_sha256": pyrosetta_digest,
                        "manifest_sha256": "4" * 64,
                        "inventory_sha256": pyrosetta_digest,
                        "manifest_algorithm": "fs2-tree-manifest/v1",
                        "file_count": 8_697,
                        "directory_count": 796,
                        "expanded_bytes": 3_287_122_494,
                        "canonical_path": f"pyrosetta-bindcraft/site-packages/sha256/{pyrosetta_digest}",
                        "marker_relative_path": ".fs2-runtime-tree.json",
                    }
                }
                if requirement["artifact_id"] == "bindcraft-pyrosetta-installed-tree"
                else {"file_manifest": requirement["file_manifest"]}
            ),
            "localization_receipt_digest": sha(f"localized-{requirement['artifact_id']}"),
        }
        for requirement, mount_path in zip(
            requirements,
            (
                "/models/alphafold2",
                "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights",
                "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights_soluble",
                "/opt/fs2/academic/pyrosetta-bindcraft/site-packages",
            ),
            strict=True,
        )
    ]
    physical_mounts = [
        {
            "name": "artifact-workspace",
            "kind": "artifact-workspace",
            "claim_name": None,
            "host_path": None,
            "mount_path": "/mnt/fs2-scientific",
            "sub_path": None,
            "read_only": False,
        },
        {
            "name": "alphafold2-params",
            "kind": "reference",
            "claim_name": "scientific-model-artifacts",
            "host_path": None,
            "mount_path": "/models/alphafold2",
            "sub_path": "bindcraft/alphafold2",
            "read_only": True,
        },
        {
            "name": "proteinmpnn-vanilla",
            "kind": "reference",
            "claim_name": "scientific-model-artifacts",
            "host_path": None,
            "mount_path": "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights",
            "sub_path": "bindcraft/proteinmpnn/vanilla",
            "read_only": True,
        },
        {
            "name": "proteinmpnn-soluble",
            "kind": "reference",
            "claim_name": "scientific-model-artifacts",
            "host_path": None,
            "mount_path": "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights_soluble",
            "sub_path": "bindcraft/proteinmpnn/soluble",
            "read_only": True,
        },
        {
            "name": "pyrosetta",
            "kind": "private",
            "claim_name": "academic-assets-runtime-rwx",
            "host_path": None,
            "mount_path": "/opt/fs2/academic/pyrosetta-bindcraft/site-packages",
            "sub_path": f"pyrosetta-bindcraft/site-packages/sha256/{pyrosetta_digest}",
            "read_only": True,
        },
    ]
    execution_map = {
        "schema": "fs2-serve.nebius.ai/scientific-execution-map/v3",
        "models": [
            {
                "model_id": "bindcraft",
                "variant_id": "upstream-pyrosetta",
                "workload_namespace": "fs2-academic-poc",
                "access_profile": "academic",
                "execution_identity_sha256": "8" * 64,
                "plan_adapter": {
                    "module": "fs2_serve.scientific_batch.adapters",
                    "function": "compile_adapter_run",
                },
                "runtime_artifacts": localizations,
                "stages": [
                    {
                        "stage_id": "design",
                        "image": f"registry.test/bindcraft@sha256:{image_digest}",
                        "collector_id": "bindcraft-output-v1",
                        "validator_id": "bindcraft-v1",
                        "mounts": physical_mounts,
                        "service_account_name": "fs2-academic-runner",
                        "workspace_uid": 10001,
                        "workspace_gid": 10001,
                        "resources": {
                            "requests": {"cpu": "16", "memory": "96Gi", "ephemeral_storage": "64Gi"},
                            "limits": {"cpu": "16", "memory": "96Gi", "ephemeral_storage": "64Gi"},
                        },
                        "active_deadline_seconds": 7200,
                        "termination_grace_seconds": 120,
                        "environment": {
                            "FS2_NETWORK_MODE": "offline",
                            "PYTHONPATH": ("/opt/fs2/academic/pyrosetta-bindcraft/site-packages:/opt/bindcraft"),
                        },
                        "required_node_labels": {},
                    }
                ],
            }
        ],
    }
    path = tmp_path / "bindcraft-execution-map.json"
    path.write_text(json.dumps(execution_map))
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
    marker = "/mnt/fs2-scientific/work/design/main/.fs2/runtime-localization.json"
    mounts = (
        RuntimeArtifactMount("alphafold2-params-bindcraft", "/models/alphafold2"),
        RuntimeArtifactMount(
            "colabdesign-mpnn-weights-vanilla",
            "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights",
        ),
        RuntimeArtifactMount(
            "colabdesign-mpnn-weights-soluble",
            "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights_soluble",
        ),
        RuntimeArtifactMount(
            "bindcraft-pyrosetta-installed-tree",
            "/opt/fs2/academic/pyrosetta-bindcraft/site-packages",
            supplemental_groups=(65532,),
        ),
    )
    invocation = StageInvocation(
        stage_id="design",
        shard_id="main",
        argv=(
            "python",
            "/opt/fs2/runtime_entrypoint.py",
            "/opt/fs2/bin/bindcraft-batch",
            "--runtime-localization-marker",
            marker,
        ),
        environment=(),
        working_directory="/mnt/fs2-scientific/work/design/main",
        consumes=(),
        produces="design-result",
        collector_id="bindcraft-output-v1",
        validator_id="bindcraft-v1",
        runtime_artifacts=(
            "alphafold2-params-bindcraft",
            "colabdesign-mpnn-weights-vanilla",
            "colabdesign-mpnn-weights-soluble",
            "bindcraft-pyrosetta-installed-tree",
        ),
        runtime_mounts=mounts,
    )
    controller_plan = ScientificBatchPlan((ScientificStagePlan("design"),))
    plan = AdapterExecutionPlan(
        model_id="bindcraft",
        variant_id="upstream-pyrosetta",
        source_revision="7cd4ace1b7407adf66a50dfefa47de2270f5e4a9",
        request_sha256="c" * 64,
        controller_plan=controller_plan,
        invocations=(invocation,),
        required_model_artifacts=(
            "alphafold2-params-bindcraft",
            "colabdesign-mpnn-weights-vanilla",
            "colabdesign-mpnn-weights-soluble",
            "bindcraft-pyrosetta-installed-tree",
        ),
    )
    return renderer, profile, plan


def test_bindcraft_projects_distinct_verified_mpnn_artifacts_to_exact_package_paths(tmp_path: Path) -> None:
    renderer, profile, plan = _bindcraft_renderer(tmp_path)
    access = ArtifactAccessContext(
        profile="academic",
        receipt_digest=sha("bindcraft-access"),
        tenant_id="academic-poc",
    )
    localized = renderer.verify_runtime_artifacts(profile, plan, access)
    plan = renderer.bind_runtime_artifacts(profile, plan, access, localized)
    snapshot = replace(
        scheduling(plan.controller_plan),
        tenant_queue="academic-scientific",
        model_lane="bindcraft",
        workload_namespace="fs2-academic-poc",
        route_namespace="fs2-academic-poc",
        stages=(
            replace(
                scheduling(plan.controller_plan).stages[0],
                resolved_local_queue="academic-scientific",
            ),
        ),
    )
    resource = WorkloadResource(
        operation_id=uuid4(),
        batch_id=uuid4(),
        workload_id=uuid4(),
        attempt_id=uuid4(),
        stage_id="design",
        shard_id="main",
        attempt_number=1,
        tenant_id="academic-poc",
        model_id="bindcraft",
        variant_id="upstream-pyrosetta",
        input_artifact_id=uuid4(),
        service_class=ServiceClass.CUSTOMER_BATCH,
        scheduling_snapshot_digest=snapshot.digest,
        namespace="fs2-academic-poc",
        route_namespace="fs2-academic-poc",
        name="bindcraft-design",
        kind=WorkloadKind.JOB,
        scheduling=snapshot.stage("design"),
        invocation=plan.invocation("design", "main"),
        access_context=access,
        runtime_artifacts=localized,
        execution_map_sha256=plan.execution_map_sha256,
        execution_binding=plan.execution_binding("design"),
    )
    pod = renderer.render(resource)["spec"]["template"]["spec"]  # type: ignore[index]
    model = pod["containers"][0]
    assert model["command"][:2] == ["python", "/opt/fs2/runtime_entrypoint.py"]
    by_path = {item["mountPath"]: item for item in model["volumeMounts"]}
    vanilla = "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights"
    soluble = "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights_soluble"
    assert by_path[vanilla]["subPath"] == "bindcraft/proteinmpnn/vanilla"
    assert by_path[soluble]["subPath"] == "bindcraft/proteinmpnn/soluble"
    assert "/models/alphafold2" in by_path
    assert "/opt/fs2/academic/pyrosetta-bindcraft/site-packages" in by_path
    assert pod["securityContext"]["supplementalGroups"] == [65532]
    marker = json.loads(next(item["value"] for item in model["env"] if item["name"] == "FS2_RUNTIME_ARTIFACTS_JSON"))
    mpnn_markers = [item for item in marker["artifacts"] if item["artifact_id"].startswith("colabdesign-mpnn-")]
    assert {item["mount_path"] for item in mpnn_markers} == {vanilla, soluble}
    assert len({item["readiness_receipt_sha256"] for item in mpnn_markers}) == 2
    assert (
        next(item for item in marker["artifacts"] if item["artifact_id"] == "bindcraft-pyrosetta-installed-tree")[
            "aggregate_tree"
        ]["file_count"]
        == 8_697
    )

    bypass = replace(
        plan,
        invocations=(
            replace(
                plan.invocations[0],
                argv=(
                    "/opt/fs2/bin/bindcraft-batch",
                    "--runtime-localization-marker",
                    "/mnt/fs2-scientific/work/design/main/.fs2/runtime-localization.json",
                ),
            ),
        ),
    )
    with pytest.raises(ScientificExecutionMapError, match="runtime artifact gate"):
        renderer.verify_runtime_artifacts(profile, bypass, access)

    missing_manifest = list(localized)
    missing_manifest[0] = replace(
        missing_manifest[0], files=(replace(missing_manifest[0].files[0], path="params.bin"),)
    )
    broken_map = renderer.runtime_artifacts.copy()
    broken_map[("bindcraft", "alphafold2-params-bindcraft")] = missing_manifest[0]
    object.__setattr__(renderer, "runtime_artifacts", MappingProxyType(broken_map))
    with pytest.raises(ScientificExecutionMapError, match="localization evidence differs"):
        renderer.verify_runtime_artifacts(profile, plan, access)


def _academic_af3_renderer(
    tmp_path: Path,
    *,
    database_sub_path: str = (
        "datasets/alphafold3-public-databases-v3.0/v3.0-paper-snapshot-2022-09-28/sha256/" + "d" * 64
    ),
) -> tuple[FileScientificManifestRenderer, ScientificWorkloadProfile, AdapterExecutionPlan]:
    requirements = (
        {
            "artifact_id": "alphafold3-parameters",
            "content_digest_sha256": "a" * 64,
            "required_files": ["af3.bin.zst"],
            "file_manifest": [{"path": "af3.bin.zst", "sha256": "1" * 64, "size_bytes": 1024}],
        },
        {
            "artifact_id": "alphafold3-public-databases-v3.0",
            "content_identity": {"digest_sha256": "d" * 64, "size_bytes": 1_000_000_000},
            "required_files": [],
            "file_manifest": [],
        },
    )
    profile = ScientificWorkloadProfile(
        MappingProxyType(
            {
                "model_id": "alphafold3",
                "state": "qualified",
                "route_exposed": True,
                "execution_identity": {
                    "model_revision": "3" * 40,
                    "runtime_image_digest": "sha256:" + "4" * 64,
                    "runtime_recipe_sha256": "5" * 64,
                    "workload_recipe_sha256": "6" * 64,
                    "artifact_manifest_digest": "7" * 64,
                    "execution_identity_sha256": "8" * 64,
                },
                "access": {"profile": "academic", "state": "verified"},
                "semantic_validation": {"state": "qualified"},
                "artifact_requirements": list(requirements),
                "workload": {
                    "stages": [
                        {"id": "data-pipeline"},
                        {"id": "inference"},
                    ]
                },
            }
        )
    )
    reference_receipt = {
        "schema": "fs2-serve.nebius.ai/reference-data-terminal-receipt/v1",
        "bundle_id": "alphafold3-public-databases-v3.0",
        "revision": "v3.0-paper-snapshot-2022-09-28",
        "created_at": "2026-09-03T00:00:00Z",
        "storage": {
            "host_root": "/mnt/fs2-reference-data/data",
            "mount_path": "/reference-data",
            "dataset_sub_path": database_sub_path,
            "read_only": True,
        },
        "content": {
            "tree_sha256": "d" * 64,
            "manifest_sha256": "2" * 64,
            "inventory_sha256": "e" * 64,
            "inventory_marker": ".fs2-manifest-sha256",
            "file_count": 20_000,
            "expanded_bytes": 1_000_000_000,
            "inline_inventory": False,
        },
        "placement": {
            "resource_class": "cpu",
            "pool": "reference-cpu",
            "node_selector": {"storage.fs2.nebius/reference-data": "true"},
            "tolerations": [
                {
                    "key": "workload.fs2.nebius/reference-data",
                    "operator": "Equal",
                    "value": "true",
                    "effect": "NoSchedule",
                }
            ],
        },
    }
    reference_receipt_json = json.dumps(reference_receipt, sort_keys=True, separators=(",", ":"))
    localizations = (
        {
            "artifact_id": "alphafold3-parameters",
            "mount_path": "/models/af3.bin.zst",
            "content_digest": "sha256:" + "a" * 64,
            "file_manifest": requirements[0]["file_manifest"],
            "localization_receipt_digest": sha("localized-af3-parameters"),
        },
        {
            "artifact_id": "alphafold3-public-databases-v3.0",
            "mount_path": "/reference-data",
            "content_digest": "sha256:" + "d" * 64,
            "aggregate_tree": {
                "storage_kind": "reference-data-plane",
                "tree_sha256": "d" * 64,
                "manifest_sha256": "2" * 64,
                "inventory_sha256": "e" * 64,
                "manifest_algorithm": "fs2-serve.nebius.ai/reference-data-manifest/v1",
                "file_count": 20_000,
                "directory_count": 0,
                "expanded_bytes": 1_000_000_000,
                "canonical_path": database_sub_path,
                "marker_relative_path": ".fs2-manifest-sha256",
            },
            "verification_receipt": reference_receipt,
            "localization_receipt_digest": sha(reference_receipt_json),
        },
    )
    value = {
        "schema": "fs2-serve.nebius.ai/scientific-execution-map/v3",
        "models": [
            {
                "model_id": "alphafold3",
                "variant_id": "upstream-v3-0-4",
                "workload_namespace": "fs2-academic-poc",
                "access_profile": "academic",
                "execution_identity_sha256": "8" * 64,
                "plan_adapter": {
                    "module": "fs2_serve.scientific_batch.adapters",
                    "function": "compile_adapter_run",
                },
                "runtime_artifacts": list(localizations),
                "stages": [
                    {
                        "stage_id": "data-pipeline",
                        "image": "registry.test/alphafold3@sha256:" + "4" * 64,
                        "collector_id": "alphafold3-processed-envelope-v1",
                        "validator_id": "alphafold3-processed-envelope-validator-v1",
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
                            {
                                "name": "alphafold3-databases",
                                "kind": "reference",
                                "claim_name": None,
                                "host_path": "/mnt/fs2-reference-data/data",
                                "mount_path": "/reference-data",
                                "sub_path": None,
                                "read_only": True,
                            },
                        ],
                        "service_account_name": "fs2-academic-runner",
                        "workspace_uid": 1001,
                        "workspace_gid": 1001,
                        "resources": {
                            "requests": {"cpu": "16", "memory": "64Gi", "ephemeral_storage": "64Gi"},
                            "limits": {"cpu": "32", "memory": "192Gi", "ephemeral_storage": "64Gi"},
                        },
                        "active_deadline_seconds": 3600,
                        "termination_grace_seconds": 60,
                        "environment": {"FS2_NETWORK_MODE": "offline"},
                        "required_node_labels": {"storage.fs2.nebius/reference-data": "true"},
                    },
                    {
                        "stage_id": "inference",
                        "image": "registry.test/alphafold3@sha256:" + "4" * 64,
                        "collector_id": "alphafold3-result-collector-v1",
                        "validator_id": "alphafold3-upstream-v3-0-4",
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
                            {
                                "name": "alphafold3-parameters",
                                "kind": "private",
                                "claim_name": "academic-assets-runtime-rwx",
                                "host_path": None,
                                "mount_path": "/models/af3.bin.zst",
                                "sub_path": "alphafold3/af3.bin.zst",
                                "read_only": True,
                            },
                        ],
                        "service_account_name": "fs2-academic-runner",
                        "workspace_uid": 1001,
                        "workspace_gid": 1001,
                        "resources": {
                            "requests": {"cpu": "8", "memory": "64Gi", "ephemeral_storage": "64Gi"},
                            "limits": {"cpu": "32", "memory": "192Gi", "ephemeral_storage": "64Gi"},
                        },
                        "active_deadline_seconds": 3600,
                        "termination_grace_seconds": 60,
                        "environment": {"FS2_NETWORK_MODE": "offline"},
                        "required_node_labels": {},
                    },
                ],
            }
        ],
    }
    path = tmp_path / f"af3-{hashlib.sha256(database_sub_path.encode()).hexdigest()[:12]}.json"
    path.write_text(json.dumps(value))

    def validator(name: str) -> Draft202012Validator:
        return Draft202012Validator(json.loads((CATALOG_ROOT / "schema" / name).read_text(encoding="utf-8")))

    catalog = ScientificProfileCatalog(
        profiles={profile.model_id: profile},
        validators={
            SCIENTIFIC_REQUEST_SCHEMA: validator("scientific-run-request.schema.json"),
            SCIENTIFIC_RESULT_SCHEMA: validator("scientific-run-result.schema.json"),
        },
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
    preprocessing = StageInvocation(
        stage_id="data-pipeline",
        shard_id="main",
        argv=(
            "/alphafold3_venv/bin/python3",
            "/opt/fs2/af3_runtime.py",
            "data",
            "--json-path",
            "/mnt/fs2-scientific/work/data-pipeline/main/input.json",
            "--output-dir",
            "/mnt/fs2-scientific/work/data-pipeline/main/output",
            "--reference-receipt",
            "/mnt/fs2-scientific/work/data-pipeline/main/.fs2/runtime-artifacts/alphafold3-public-databases-v3.0.receipt.json",
            "--threads",
            "16",
            "--cpu-request",
            "16",
        ),
        environment=(),
        working_directory="/mnt/fs2-scientific/work/data-pipeline/main",
        consumes=("raw-request",),
        produces="processed-input",
        collector_id="alphafold3-processed-envelope-v1",
        validator_id="alphafold3-processed-envelope-validator-v1",
        handoff_name="processed-envelope",
        materializations=(
            ArtifactMaterialization(
                "raw-request",
                "/mnt/fs2-scientific/work/data-pipeline/main/input.json",
                MaterializationMode.COPY_FILE,
            ),
        ),
        runtime_artifacts=("alphafold3-public-databases-v3.0",),
        runtime_mounts=(
            RuntimeArtifactMount(
                artifact_id="alphafold3-public-databases-v3.0",
                mount_path="/reference-data",
                supplemental_groups=(1000,),
            ),
        ),
    )
    inference = StageInvocation(
        stage_id="inference",
        shard_id="main",
        argv=(
            "/alphafold3_venv/bin/python3",
            "/opt/fs2/af3_runtime.py",
            "inference",
            "--handoff-dir",
            "/mnt/fs2-scientific/work/inference/main/prepared",
            "--output-dir",
            "/mnt/fs2-scientific/work/inference/main/output",
        ),
        environment=(),
        working_directory="/mnt/fs2-scientific/work/inference/main",
        consumes=("processed-input",),
        produces="structure-results",
        collector_id="alphafold3-result-collector-v1",
        validator_id="alphafold3-upstream-v3-0-4",
        materializations=(
            ArtifactMaterialization(
                "processed-input",
                "/mnt/fs2-scientific/work/inference/main/prepared",
                MaterializationMode.EXTRACT_TAR,
            ),
        ),
        runtime_artifacts=("alphafold3-parameters",),
        runtime_mounts=(
            RuntimeArtifactMount(
                artifact_id="alphafold3-parameters",
                mount_path="/models/af3.bin.zst",
                sub_path="alphafold3/af3.bin.zst",
                supplemental_groups=(65532,),
            ),
        ),
    )
    cpu_resources = StageResourceEnvelope(
        cpu_millis=16_000,
        memory_bytes=64 * 1024**3,
        ephemeral_storage_bytes=64 * 1024**3,
        limit_cpu_millis=32_000,
        limit_memory_bytes=192 * 1024**3,
        limit_ephemeral_storage_bytes=64 * 1024**3,
    )
    gpu_resources = StageResourceEnvelope(
        cpu_millis=8_000,
        memory_bytes=64 * 1024**3,
        ephemeral_storage_bytes=64 * 1024**3,
        limit_cpu_millis=32_000,
        limit_memory_bytes=192 * 1024**3,
        limit_ephemeral_storage_bytes=64 * 1024**3,
    )
    controller_plan = ScientificBatchPlan(
        (
            ScientificStagePlan(
                "data-pipeline",
                resource_class=ResourceClass.CPU,
                placement_class=StagePlacementClass.REFERENCE_DATA_CPU,
                resources=cpu_resources,
            ),
            ScientificStagePlan(
                "inference",
                depends_on=("data-pipeline",),
                placement_class=StagePlacementClass.ACCELERATOR,
                resources=gpu_resources,
            ),
        )
    )
    plan = AdapterExecutionPlan(
        model_id="alphafold3",
        variant_id="upstream-v3-0-4",
        source_revision="3" * 40,
        request_sha256="c" * 64,
        controller_plan=controller_plan,
        invocations=(preprocessing, inference),
        required_model_artifacts=("alphafold3-parameters", "alphafold3-public-databases-v3.0"),
    )
    return renderer, profile, plan


def test_af3_academic_v3_map_binds_exact_params_and_content_addressed_database(tmp_path: Path) -> None:
    renderer, profile, plan = _academic_af3_renderer(tmp_path)
    access = ArtifactAccessContext(
        profile="academic",
        receipt_digest=sha("af3-access"),
        tenant_id="academic-poc",
    )
    localized = renderer.verify_runtime_artifacts(profile, plan, access)
    plan = renderer.bind_runtime_artifacts(profile, plan, access, localized)
    base_snapshot = scheduling(plan.controller_plan)
    snapshot = replace(
        base_snapshot,
        tenant_queue="academic-scientific",
        workload_namespace="fs2-academic-poc",
        route_namespace="fs2-academic-poc",
        stages=tuple(
            replace(
                decision,
                resolved_local_queue=(
                    "academic-reference-data" if decision.stage_id == "data-pipeline" else "academic-scientific"
                ),
                resolved_cluster_queue=(
                    "reference-data-cpu" if decision.stage_id == "data-pipeline" else "inference-accelerators"
                ),
                workload_namespace="fs2-academic-poc",
                route_namespace="fs2-academic-poc",
                placement_class=(
                    StagePlacementClass.REFERENCE_DATA_CPU
                    if decision.stage_id == "data-pipeline"
                    else StagePlacementClass.ACCELERATOR
                ),
                node_selector=(
                    (("storage.fs2.nebius/reference-data", "true"),) if decision.stage_id == "data-pipeline" else ()
                ),
            )
            for decision in base_snapshot.stages
        ),
    )
    raw_materialization = ResolvedArtifactMaterialization.resolve(
        plan.invocation("data-pipeline", "main").materializations[0],
        artifact_id=uuid4(),
        digest=sha("raw-af3-request"),
        size_bytes=10,
        media_type="application/json",
        compression=None,
    )
    common = {
        "operation_id": uuid4(),
        "batch_id": uuid4(),
        "workload_id": uuid4(),
        "attempt_number": 1,
        "tenant_id": "academic-poc",
        "model_id": "alphafold3",
        "variant_id": "upstream-v3-0-4",
        "input_artifact_id": uuid4(),
        "service_class": ServiceClass.CUSTOMER_BATCH,
        "scheduling_snapshot_digest": snapshot.digest,
        "namespace": "fs2-academic-poc",
        "route_namespace": "fs2-academic-poc",
        "kind": WorkloadKind.JOB,
        "access_context": access,
        "execution_map_sha256": plan.execution_map_sha256,
    }
    cpu_resource = WorkloadResource(
        **common,
        attempt_id=uuid4(),
        stage_id="data-pipeline",
        shard_id="main",
        name="af3-data-pipeline",
        scheduling=snapshot.stage("data-pipeline"),
        invocation=plan.invocation("data-pipeline", "main"),
        materializations=(raw_materialization,),
        runtime_artifacts=tuple(
            item for item in localized if item.logical_artifact_id == "alphafold3-public-databases-v3.0"
        ),
        execution_binding=plan.execution_binding("data-pipeline"),
    )
    gpu_resource = WorkloadResource(
        **common,
        attempt_id=uuid4(),
        stage_id="inference",
        shard_id="main",
        name="af3-inference",
        scheduling=snapshot.stage("inference"),
        invocation=plan.invocation("inference", "main"),
        materializations=(
            ResolvedArtifactMaterialization.resolve(
                plan.invocation("inference", "main").materializations[0],
                artifact_id=uuid4(),
                digest=sha("processed-envelope"),
                size_bytes=10,
                media_type="application/x-tar",
                compression=None,
            ),
        ),
        runtime_artifacts=tuple(item for item in localized if item.logical_artifact_id == "alphafold3-parameters"),
        execution_binding=plan.execution_binding("inference"),
    )
    cpu_pod = renderer.render(cpu_resource)["spec"]["template"]["spec"]  # type: ignore[index]
    assert cpu_pod["serviceAccountName"] == "fs2-academic-runner"
    assert cpu_pod["nodeSelector"] == {"storage.fs2.nebius/reference-data": "true"}
    assert cpu_pod["securityContext"]["supplementalGroups"] == [1000]
    assert "fsGroup" not in cpu_pod["securityContext"]
    for container in (*cpu_pod["initContainers"], *cpu_pod["containers"]):
        assert container["securityContext"]["runAsUser"] == 1001
        assert container["securityContext"]["runAsGroup"] == 1001
    cpu_volumes = {item["name"]: item for item in cpu_pod["volumes"]}
    assert set(cpu_volumes) == {"artifact-workspace", "alphafold3-databases"}
    assert cpu_volumes["alphafold3-databases"]["hostPath"] == {
        "path": "/mnt/fs2-reference-data/data",
        "type": "Directory",
    }
    cpu_mounts = {item["name"]: item for item in cpu_pod["containers"][0]["volumeMounts"]}
    assert cpu_mounts["alphafold3-databases"]["mountPath"] == "/reference-data"
    assert "subPath" not in cpu_mounts["alphafold3-databases"]
    assert cpu_pod["containers"][0]["resources"]["requests"]["cpu"] == "16"
    assert cpu_pod["containers"][0]["resources"]["requests"]["memory"] == "64Gi"
    assert cpu_pod["containers"][0]["resources"]["limits"]["cpu"] == "32"
    assert cpu_pod["containers"][0]["resources"]["limits"]["memory"] == "192Gi"
    command = cpu_pod["containers"][0]["command"]
    assert command[:3] == ["/alphafold3_venv/bin/python3", "/opt/fs2/af3_runtime.py", "data"]
    assert command[command.index("--threads") + 1] == "16"
    assert command[command.index("--cpu-request") + 1] == "16"

    gpu_pod = renderer.render(gpu_resource)["spec"]["template"]["spec"]  # type: ignore[index]
    assert gpu_pod["securityContext"]["supplementalGroups"] == [65532]
    assert "fsGroup" not in gpu_pod["securityContext"]
    for container in (*gpu_pod["initContainers"], *gpu_pod["containers"]):
        assert container["securityContext"]["runAsUser"] == 1001
        assert container["securityContext"]["runAsGroup"] == 1001
    gpu_volumes = {item["name"]: item for item in gpu_pod["volumes"]}
    assert set(gpu_volumes) == {"artifact-workspace", "alphafold3-parameters"}
    assert gpu_volumes["alphafold3-parameters"]["persistentVolumeClaim"] == {
        "claimName": "academic-assets-runtime-rwx",
        "readOnly": True,
    }
    gpu_mounts = {item["name"]: item for item in gpu_pod["containers"][0]["volumeMounts"]}
    assert gpu_mounts["alphafold3-parameters"]["subPath"] == "alphafold3/af3.bin.zst"
    verifier = next(item for item in gpu_pod["initContainers"] if item["name"] == "verify-runtime-artifacts")
    verifier_mounts = {item["name"]: item for item in verifier["volumeMounts"]}
    assert verifier_mounts["alphafold3-parameters"]["subPath"] == "alphafold3/af3.bin.zst"
    runtime_marker = json.loads(
        next(item["value"] for item in gpu_pod["containers"][0]["env"] if item["name"] == "FS2_RUNTIME_ARTIFACTS_JSON")
    )
    assert runtime_marker["artifacts"][0]["sub_path"] == "alphafold3/af3.bin.zst"
    assert not any(item["mountPath"] == "/reference-data" for item in gpu_mounts.values())

    with pytest.raises(ScientificExecutionMapError, match="immutable execution-map route"):
        renderer.render(replace(gpu_resource, namespace="fs2-models", route_namespace="fs2-models"))
    with pytest.raises(ValueError, match="dataset path differs from its tree digest"):
        _academic_af3_renderer(
            tmp_path,
            database_sub_path="datasets/alphafold3-public-databases-v3.0/v3.0-paper-snapshot/current",
        )


@pytest.mark.asyncio
async def test_af3_gpu_stage_materializes_only_validated_cpu_handoff(tmp_path: Path) -> None:
    renderer, profile, execution = _academic_af3_renderer(tmp_path)
    raw_id = uuid4()
    raw = ScientificInputArtifact(
        logical_artifact_id="raw-request",
        semantic_type="alphafold-input/v1",
        artifact_id=raw_id,
        digest=sha("raw"),
        size_bytes=3,
        media_type="application/json",
    )
    manifest_id = uuid4()
    verified_manifest = VerifiedInputManifest(
        manifest_id="af3-inputs",
        manifest_artifact_id=manifest_id,
        manifest_digest=sha("manifest"),
        entries=(raw,),
    )
    access = ArtifactAccessContext(
        profile="academic",
        receipt_digest=sha("af3-access"),
        tenant_id="academic-poc",
    )
    localized = renderer.verify_runtime_artifacts(profile, execution, access)
    execution = renderer.bind_runtime_artifacts(profile, execution, access, localized)
    controller_plan = execution.controller_plan
    base_snapshot = scheduling(controller_plan)
    snapshot = replace(
        base_snapshot,
        workload_namespace="fs2-academic-poc",
        route_namespace="fs2-academic-poc",
        tenant_queue="academic-scientific",
        stages=tuple(
            replace(
                decision,
                resolved_local_queue=(
                    "academic-reference-data" if decision.stage_id == "data-pipeline" else "academic-scientific"
                ),
                resolved_cluster_queue=(
                    "reference-data-cpu" if decision.stage_id == "data-pipeline" else "inference-accelerators"
                ),
                workload_namespace="fs2-academic-poc",
                route_namespace="fs2-academic-poc",
                placement_class=controller_plan.stage(decision.stage_id).placement_class,
            )
            for decision in base_snapshot.stages
        ),
    )
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
        tenant_id="academic-poc",
        model_id="alphafold3",
        variant_id="upstream-v3-0-4",
        input_artifact_id=manifest_id,
        plan=controller_plan,
        scheduling=snapshot,
        execution_plan=execution,
        access_context=access,
        input_manifest=verified_manifest,
        runtime_artifacts=localized,
    )
    assert not cluster.apply_history
    assert repository.records[operation_id].runtime_artifacts == localized
    assert state_from_value(state_to_value(repository.records[operation_id])) == repository.records[operation_id]
    await controller.reconcile_once()
    cpu_attempt = repository.records[operation_id].stage("data-pipeline").attempts[0]
    cluster.set_observation(
        cpu_attempt.workload,
        WorkloadObservation(
            ref=cpu_attempt.workload,
            attempt_id=cpu_attempt.attempt_id,
            state=WorkloadState.SUCCEEDED,
            phases=(LifecyclePhase.ADMITTED, LifecyclePhase.ACTIVE_COMPUTE),
            scheduling_admission=SchedulingAdmission(
                resolved_pool_id=None,
                admitted_resource_flavor=None,
                accelerator_resource_name=None,
                accelerator_count=0,
                admitted_at=NOW,
            ),
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
            collector_id=execution.invocation("data-pipeline", "main").collector_id,
            validator_id=execution.invocation("data-pipeline", "main").validator_id,
        )
    )
    for _ in range(5):
        await controller.reconcile_once()
    gpu_resource = cluster.apply_history[-1]
    assert gpu_resource.stage_id == "inference"
    assert tuple(item.artifact_id for item in gpu_resource.materializations) == (processed_id,)
    assert raw_id not in {item.artifact_id for item in gpu_resource.materializations}
    assert gpu_resource.access_context == access
    assert tuple(item.logical_artifact_id for item in gpu_resource.runtime_artifacts) == ("alphafold3-parameters",)
    assert not any(binding.host_path for binding in gpu_resource.execution_binding.mounts)


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
async def test_missing_runtime_localization_fails_before_any_gpu_apply(tmp_path: Path) -> None:
    execution = runtime_plan()
    cluster = FakeScientificBatchCluster()
    renderer = runtime_execution_map(tmp_path)
    access = ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="tenant-a")
    with pytest.raises(ValueError, match="runtime localization"):
        renderer.bind_runtime_artifacts(runtime_profile(), execution, access, ())
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
        stage_invocation_json=prepare_invocation_json("fold"),
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
        collection_deadline_seconds=30,
        poll_seconds=0.001,
    )
    structure_ref = client.uploads["fold-result:structure"][1]
    manifest_bytes = client.uploads["fold-result:manifest"][0]
    manifest = json.loads(manifest_bytes)
    assert manifest["entries"][0]["artifact"] == structure_ref
    validation = json.loads(client.uploads["fold-result:validation"][0])
    assert validation["status"] == "passed"
    assert validation["logical_output_id"] == "fold-result"


def test_companion_verifies_reference_plane_and_materializes_exact_terminal_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_root = SOLUTION_ROOT / "models/cancer-immunotherapy/images/alphafold3/fixtures"
    receipt = json.loads((fixture_root / "reference-terminal-receipt.json").read_bytes())
    manifest = json.loads((fixture_root / "reference-published-manifest.json").read_bytes())
    receipt_bytes = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    reference_root = tmp_path / "reference-data"
    dataset = reference_root.joinpath(*PurePosixPath(receipt["storage"]["dataset_sub_path"]).parts)
    dataset.mkdir(parents=True)
    manifest_digest = receipt["content"]["manifest_sha256"]
    (dataset / companion.REFERENCE_DATA_TREE_MARKER).write_text(manifest_digest, encoding="utf-8")
    manifest_path = reference_root / "manifests" / "sha256" / f"{manifest_digest}.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(manifest_bytes)
    assert hashlib.sha256(manifest_bytes).hexdigest() == manifest_digest

    marker = {
        "schema": companion.RUNTIME_LOCALIZATION_SCHEMA,
        "operation_id": str(uuid4()),
        "attempt_id": str(uuid4()),
        "tenant_id": "academic-poc",
        "model_id": "alphafold3",
        "variant_id": "upstream-v3-0-4",
        "stage_id": "data-pipeline",
        "artifacts": [
            {
                "artifact_id": receipt["bundle_id"],
                "mount_path": str(reference_root),
                "content_digest": f"sha256:{receipt['content']['tree_sha256']}",
                "artifact_manifest_sha256": manifest_digest,
                "localization_receipt_digest": f"sha256:{hashlib.sha256(receipt_bytes).hexdigest()}",
                "sub_path": None,
                "readiness_receipt_sha256": manifest_digest,
                "authorization_receipt_sha256": None,
                "verification_receipt": receipt,
                "files": [],
                "aggregate_tree": {
                    "tree_digest": f"sha256:{receipt['content']['tree_sha256']}",
                    "manifest_digest": f"sha256:{manifest_digest}",
                    "inventory_digest": f"sha256:{receipt['content']['inventory_sha256']}",
                    "manifest_algorithm": companion.REFERENCE_DATA_MANIFEST_SCHEMA,
                    "file_count": receipt["content"]["file_count"],
                    "directory_count": 1,
                    "expanded_bytes": receipt["content"]["expanded_bytes"],
                    "canonical_path": receipt["storage"]["dataset_sub_path"],
                    "storage_kind": "reference-data-plane",
                    "marker_relative_path": receipt["content"]["inventory_marker"],
                },
            }
        ],
    }
    marker_json = json.dumps(marker, sort_keys=True, separators=(",", ":"), allow_nan=False)
    companion.verify_runtime_artifacts(runtime_localization_json=marker_json)

    scientific_root = tmp_path / "scientific"
    scientific_root.mkdir()
    monkeypatch.setattr(companion, "_ROOT", scientific_root)
    workspace = scientific_root / "work" / "data-pipeline" / "main"
    companion.prepare_workspace(
        workspace,
        runtime_localization_json=marker_json,
        stage_invocation_json=prepare_invocation_json("data-pipeline"),
    )
    receipt_path = workspace / ".fs2/runtime-artifacts/alphafold3-public-databases-v3.0.receipt.json"
    assert receipt_path.read_bytes() == receipt_bytes

    (dataset / companion.REFERENCE_DATA_TREE_MARKER).write_text("0" * 64, encoding="utf-8")
    with pytest.raises(ValueError, match="publication marker differs"):
        companion.verify_runtime_artifacts(runtime_localization_json=marker_json)


def test_prepare_workspace_materializes_frozen_documents_and_controller_fills_tree_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "scientific"
    root.mkdir()
    monkeypatch.setattr(companion, "_ROOT", root)
    documents = (
        StageWorkspaceDocument(".fs2/request.json", '{"operation":"design-binder"}'),
        StageWorkspaceDocument(".fs2/input-manifest.json", '{"entries":[],"schema":"manifest/v1"}'),
    )
    roles = (
        RuntimeArtifactAdmissionRole("alphafold2-params", "alphafold2-params-bindcraft", "/models/af2"),
        RuntimeArtifactAdmissionRole("pyrosetta-site-packages", "bindcraft-pyrosetta", "/opt/fs2/academic/p"),
    )
    invocation = StageInvocation(
        stage_id="design",
        shard_id="main",
        argv=("scientific-stage",),
        environment=(),
        working_directory="/mnt/fs2-scientific/work/design/main",
        consumes=(),
        produces="design-result",
        runtime_artifacts=tuple(item.artifact_id for item in roles),
        runtime_mounts=tuple(RuntimeArtifactMount(item.artifact_id, item.mount_path) for item in roles),
        workspace_documents=documents,
        runtime_admission=RuntimeArtifactAdmissionSpec(
            schema="fs2.nebius.ai/bindcraft-external-tree-admission/v1",
            relative_path=".fs2/external-trees.json",
            roles=roles,
        ),
    )
    marker = {
        "schema": companion.RUNTIME_LOCALIZATION_SCHEMA,
        "operation_id": str(uuid4()),
        "attempt_id": str(uuid4()),
        "tenant_id": "academic-poc",
        "model_id": "bindcraft",
        "variant_id": "v1-5-3-pyrosetta-academic",
        "stage_id": "design",
        "artifacts": [
            {
                "artifact_id": role.artifact_id,
                "mount_path": role.mount_path,
                "content_digest": f"sha256:{index + 1}".ljust(71, str(index + 1)),
                "artifact_manifest_sha256": str(index + 3) * 64,
                "localization_receipt_digest": f"sha256:{str(index + 5) * 64}",
                "sub_path": None,
                "readiness_receipt_sha256": str(index + 6) * 64,
                "authorization_receipt_sha256": None,
                "verification_receipt": None,
                "files": [{"path": "identity", "digest": f"sha256:{str(index + 7) * 64}", "size_bytes": 1}],
                "aggregate_tree": None,
            }
            for index, role in enumerate(roles)
        ],
    }
    marker_json = json.dumps(marker, sort_keys=True, separators=(",", ":"))
    workspace = root / "work" / "design" / "main"
    companion.prepare_workspace(
        workspace,
        runtime_localization_json=marker_json,
        stage_invocation_json=_invocation_json(invocation),
    )
    assert (workspace / ".fs2/request.json").read_text() == documents[0].canonical_json
    assert (workspace / ".fs2/input-manifest.json").read_text() == documents[1].canonical_json
    admission = json.loads((workspace / ".fs2/external-trees.json").read_bytes())
    assert admission["schema"] == invocation.runtime_admission.schema
    assert admission["generation"] == hashlib.sha256(marker_json.encode()).hexdigest()
    assert admission["trees"] == [
        {
            "role": role.role,
            "artifact_id": role.artifact_id,
            "root": role.mount_path,
            "sha256": marker["artifacts"][index]["content_digest"].removeprefix("sha256:"),
        }
        for index, role in enumerate(roles)
    ]


def _collection_invocation(*, collector_id: str, validator_id: str) -> dict[str, object]:
    """The canonical collector payload, matching the renderer's environment."""

    return {
        "stage_id": "fold",
        "shard_id": "main",
        "argv": ["fold-model", "--input", "/mnt/fs2-scientific/work/fold/main/input.fasta"],
        "environment": [],
        "working_directory": "/mnt/fs2-scientific/work/fold/main",
        "consumes": [],
        "produces": "fold-result",
        "collector_id": collector_id,
        "validator_id": validator_id,
        "handoff_name": "structure",
        "max_output_artifacts": 8,
        "max_output_bytes": 1024,
        "materializations": [],
        "runtime_artifacts": [],
        "runtime_mounts": [],
    }


class _CollectorUploads:
    """Minimal artifact port; the collector only uploads on the success path."""

    def __init__(self) -> None:
        self.uploads: dict[str, tuple[bytes, dict[str, object]]] = {}

    def upload(
        self,
        *,
        identity: str,
        content: bytes,
        media_type: str,
        compression: str | None,
    ) -> dict[str, object]:
        reference: dict[str, object] = {
            "artifact_id": str(uuid4()),
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "media_type": media_type,
        }
        if compression is not None:
            reference["compression"] = compression
        self.uploads[identity] = (content, reference)
        return reference


def _prepared_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "scientific"
    root.mkdir()
    monkeypatch.setattr(companion, "_ROOT", root)
    workspace = root / "work" / "fold" / "main"
    companion.prepare_workspace(
        workspace,
        runtime_localization_json=json.dumps(
            {
                "schema": companion.RUNTIME_LOCALIZATION_SCHEMA,
                "operation_id": str(uuid4()),
                "attempt_id": str(uuid4()),
                "tenant_id": "tenant-a",
                "model_id": "fold-model",
                "variant_id": "fold-model-h100",
                "stage_id": "fold",
                "artifacts": [],
            }
        ),
        stage_invocation_json=prepare_invocation_json("fold"),
    )
    return workspace


def test_collector_waits_through_pending_polls_then_publishes_and_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow but successful model still collects, well inside the bound."""

    workspace = _prepared_workspace(tmp_path, monkeypatch)
    payload = _collection_invocation(
        collector_id="handshake-positive-collector-v1",
        validator_id="handshake-positive-validator-v1",
    )
    polls = 0

    def collect(bound: StageInvocation, path: Path) -> CollectedStageOutput:
        nonlocal polls
        polls += 1
        if polls < 3:
            # The model is still computing; nothing is published atomically yet.
            raise CollectionPendingError("stage output is not published")
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
        model_id="handshake-positive-model",
        compiler=lambda *args, **kwargs: None,  # type: ignore[arg-type]
        collectors={str(payload["collector_id"]): collect},
    )
    elapsed = 0.0
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        nonlocal elapsed
        slept.append(seconds)
        elapsed += seconds

    client = _CollectorUploads()
    companion.collect_and_commit(
        client=client,  # type: ignore[arg-type]
        collector_id=str(payload["collector_id"]),
        validator_id=str(payload["validator_id"]),
        invocation_json=json.dumps(payload),
        workspace=workspace,
        catalog_dir=CATALOG_ROOT,
        max_artifacts=int(payload["max_output_artifacts"]),  # type: ignore[call-overload]
        max_output_bytes=int(payload["max_output_bytes"]),  # type: ignore[call-overload]
        collection_deadline_seconds=600,
        poll_seconds=5,
        monotonic=lambda: elapsed,
        sleep=sleep,
    )
    assert polls == 3
    assert slept == [5, 5]
    assert elapsed < 600
    # Exact positive completion: the handoff entry, manifest, and validation
    # are all published before the collector returns successfully.
    assert set(client.uploads) == {
        "fold-result:structure",
        "fold-result:manifest",
        "fold-result:validation",
    }
    manifest = json.loads(client.uploads["fold-result:manifest"][0])
    assert manifest["entries"][0]["name"] == "structure"
    assert json.loads(client.uploads["fold-result:validation"][0])["status"] == "passed"


def test_collector_fails_its_bound_when_the_model_publishes_no_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing result terminates the collector instead of looping forever."""

    workspace = _prepared_workspace(tmp_path, monkeypatch)
    payload = _collection_invocation(
        collector_id="handshake-missing-collector-v1",
        validator_id="handshake-missing-validator-v1",
    )
    polls = 0

    def collect(bound: StageInvocation, path: Path) -> CollectedStageOutput:
        nonlocal polls
        polls += 1
        raise CollectionPendingError("stage output is never published")

    register_adapter(
        model_id="handshake-missing-model",
        compiler=lambda *args, **kwargs: None,  # type: ignore[arg-type]
        collectors={str(payload["collector_id"]): collect},
    )
    elapsed = 0.0

    def sleep(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    client = _CollectorUploads()
    with pytest.raises(companion.CollectionDeadlineError):
        companion.collect_and_commit(
            client=client,  # type: ignore[arg-type]
            collector_id=str(payload["collector_id"]),
            validator_id=str(payload["validator_id"]),
            invocation_json=json.dumps(payload),
            workspace=workspace,
            catalog_dir=CATALOG_ROOT,
            max_artifacts=int(payload["max_output_artifacts"]),  # type: ignore[call-overload]
            max_output_bytes=int(payload["max_output_bytes"]),  # type: ignore[call-overload]
            collection_deadline_seconds=30,
            poll_seconds=4,
            monotonic=lambda: elapsed,
            sleep=sleep,
        )
    # The wait is bounded exactly, and the final poll never sleeps past it.
    assert elapsed == 30
    assert polls == 9
    assert not client.uploads


def test_collector_rejects_an_unbounded_collection_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No caller may reintroduce the unbounded poll that held GPUs open."""

    workspace = _prepared_workspace(tmp_path, monkeypatch)
    payload = _collection_invocation(
        collector_id="handshake-unbounded-collector-v1",
        validator_id="handshake-unbounded-validator-v1",
    )
    for deadline, poll in ((0, 2), (-1, 2), (30, 0)):
        with pytest.raises(ValueError, match="must be positive"):
            companion.collect_and_commit(
                client=_CollectorUploads(),  # type: ignore[arg-type]
                collector_id=str(payload["collector_id"]),
                validator_id=str(payload["validator_id"]),
                invocation_json=json.dumps(payload),
                workspace=workspace,
                catalog_dir=CATALOG_ROOT,
                max_artifacts=int(payload["max_output_artifacts"]),  # type: ignore[call-overload]
                max_output_bytes=int(payload["max_output_bytes"]),  # type: ignore[call-overload]
                collection_deadline_seconds=deadline,
                poll_seconds=poll,
            )

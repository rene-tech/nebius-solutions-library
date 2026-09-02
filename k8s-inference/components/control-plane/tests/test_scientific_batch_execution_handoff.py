from __future__ import annotations

import hashlib
import json
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
from fs2_serve.scientific_batch import companion
from fs2_serve.scientific_batch.adapters import (
    CollectedArtifactFile,
    CollectedStageOutput,
    register_adapter,
)
from fs2_serve.scientific_batch.artifact_bridge import ArtifactServiceBridge
from fs2_serve.scientific_batch.capability import ScientificWorkloadCapabilityAuthority
from fs2_serve.scientific_batch.controller import ScientificBatchController
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer, ScientificExecutionMapError
from fs2_serve.scientific_batch.models import (
    AdapterExecutionPlan,
    ArtifactAccessContext,
    ArtifactCommit,
    ArtifactMaterialization,
    LifecyclePhase,
    MaterializationMode,
    ResourceClass,
    RuntimeArtifactFile,
    RuntimeArtifactLocalization,
    SchedulingSnapshot,
    ScientificBatchPlan,
    ScientificInputArtifact,
    ScientificStagePlan,
    ServiceClass,
    StageInvocation,
    StageSchedulingDecision,
    VerifiedInputManifest,
    WorkloadObservation,
    WorkloadState,
)
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog, ScientificWorkloadProfile

NOW = datetime(2026, 9, 2, 21, tzinfo=UTC)


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
    return ArtifactRecord(
        artifact_id=artifact_id,
        operation_id=operation_id,
        tenant_id="academic-poc",
        attempt=0,
        direction=ArtifactDirection.INPUT,
        digest=digest,
        size_bytes=len(value),
        media_type=media_type,
        storage_key=artifact_storage_key(
            tenant_id="academic-poc",
            operation_id=operation_id,
            attempt=0,
            direction=ArtifactDirection.INPUT,
            digest=digest,
        ),
        access=access,
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
        for index, name in enumerate(("ccd.pkl", "components.cif", "model.pt", "tokens.json"))
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
                            "id": "inference",
                        }
                    ]
                },
            }
        )
    )


def runtime_execution_map(tmp_path: Path, *, omit_file: bool = False) -> FileScientificManifestRenderer:
    profile = runtime_profile()
    file_names = ("ccd.pkl", "components.cif", "model.pt", "tokens.json")
    files = [
        {"path": name, "sha256": hashlib.sha256(name.encode()).hexdigest(), "size_bytes": index + 1}
        for index, name in enumerate(file_names[:-1] if omit_file else file_names)
    ]
    value = {
        "schema": "fs2-serve.nebius.ai/scientific-execution-map/v2",
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
                        "mount_path": "/opt/fs2/artifacts/protenix-common",
                        "content_digest": "sha256:" + "c" * 64,
                        "file_manifest": files,
                        "localization_receipt_digest": sha("localized-protenix-common"),
                    }
                ],
                "stages": [
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
                                "mount_path": "/mnt/fs2-scientific",
                                "sub_path": None,
                                "read_only": False,
                            },
                            {
                                "name": "model-artifacts",
                                "kind": "reference",
                                "claim_name": "scientific-model-artifacts",
                                "mount_path": "/opt/fs2/artifacts",
                                "sub_path": None,
                                "read_only": True,
                            },
                        ],
                        "service_account_name": "scientific-runner",
                        "resources": {"cpu": "4", "memory": "32Gi", "ephemeral_storage": "20Gi"},
                        "active_deadline_seconds": 3600,
                        "termination_grace_seconds": 60,
                        "environment": {},
                    }
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
    controller_plan = ScientificBatchPlan((ScientificStagePlan("inference"),))
    invocation = StageInvocation(
        stage_id="inference",
        shard_id="main",
        argv=("protenix", "pred", "--model", "/opt/fs2/artifacts/protenix-common/model.pt"),
        environment=(),
        working_directory="/mnt/fs2-scientific/work/inference/main",
        consumes=(),
        produces="result-manifest",
        collector_id="protenix-results-v1",
        validator_id="protenix-validator-v1",
        handoff_name=None,
        runtime_artifacts=("protenix-common",),
    )
    return AdapterExecutionPlan(
        model_id="protenix-v2",
        variant_id="upstream-v2-0-0",
        source_revision="b" * 40,
        request_sha256="d" * 64,
        controller_plan=controller_plan,
        invocations=(invocation,),
        required_model_artifacts=("protenix-common",),
    )


def test_runtime_artifact_file_manifest_is_hard_admission_gate(tmp_path: Path) -> None:
    plan = runtime_plan()
    complete = runtime_execution_map(tmp_path)
    localized = complete.verify_runtime_artifacts(runtime_profile(), plan)
    assert tuple(item.path for item in localized[0].files) == (
        "ccd.pkl",
        "components.cif",
        "model.pt",
        "tokens.json",
    )
    with pytest.raises(ScientificExecutionMapError, match="localization evidence differs"):
        runtime_execution_map(tmp_path, omit_file=True).verify_runtime_artifacts(runtime_profile(), plan)


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
        argv=("run_alphafold.py", "--run_data_pipeline=false", "--run_inference=true"),
        environment=(),
        working_directory="/mnt/fs2-scientific/work/inference/main",
        consumes=("processed-input",),
        produces="structure-results",
        collector_id="af3-structure-results-v1",
        validator_id="af3-structure-validator-v1",
        handoff_name=None,
        materializations=(
            ArtifactMaterialization(
                "processed-input",
                "/mnt/fs2-scientific/work/inference/main/processed.json",
                MaterializationMode.COPY_FILE,
            ),
        ),
        runtime_artifacts=("af3-parameters",),
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
    access = ArtifactAccessContext(
        profile="academic",
        receipt_digest=sha("af3-access"),
        tenant_id="academic-poc",
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
        scheduling=scheduling(controller_plan),
        execution_plan=execution,
        access_context=access,
        input_manifest=verified_manifest,
        runtime_artifacts=(localized,),
    )
    assert not cluster.apply_history
    assert repository.records[operation_id].runtime_artifacts == (localized,)
    await controller.reconcile_once()
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
        ArtifactCommit(
            operation_id=operation_id,
            stage_id="data-pipeline",
            attempt_ids=(cpu_attempt.attempt_id,),
            logical_artifact_id="processed-input",
            handoff_artifact_id=processed_id,
            handoff_digest=sha("processed"),
            handoff_size_bytes=9,
            handoff_media_type="application/json",
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
    assert tuple(item.artifact_id for item in gpu_resource.materializations) == (processed_id,)
    assert raw_id not in {item.artifact_id for item in gpu_resource.materializations}
    assert gpu_resource.access_context == access
    assert gpu_resource.runtime_artifacts == (localized,)


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
    companion.prepare_workspace(workspace)
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
            }
        ),
        workspace=workspace,
        catalog_dir=CATALOG_ROOT,
        max_artifacts=invocation.max_output_artifacts,
        max_output_bytes=invocation.max_output_bytes,
        poll_seconds=0.001,
    )
    assert client.committed is not None
    structure_ref = client.uploads["fold-result:structure"][1]
    assert client.committed["handoff_artifact_id"] == structure_ref["artifact_id"]
    manifest_bytes = client.uploads["fold-result:manifest"][0]
    manifest = json.loads(manifest_bytes)
    assert manifest["entries"][0]["artifact"] == structure_ref
    assert client.committed["semantic_valid"] is True

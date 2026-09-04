"""Production-controller regressions for active Mosaic and RFdiffusion profiles."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

import pytest

from fs2_serve.scientific_batch import MaterializationMode, ScientificInputArtifact, companion
from fs2_serve.scientific_batch.adapters import (
    ScientificAdapterError,
    collect_stage_output,
    compile_adapter_run,
    mosaic,
    rfdiffusion,
)
from fs2_serve.scientific_batch.adapters.production_registry import install_production_adapters
from fs2_serve.scientific_batch.adapters.staged_workspace import STAGE_COMPLETION_SCHEMA
from fs2_serve.scientific_batch.models import AdapterExecutionPlan, StageInvocation

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_ROOT = SOLUTION_ROOT / "models/cancer-immunotherapy/runtime-images"
MOSAIC_PDB = RUNTIME_ROOT / "mosaic/evidence/design/candidate_000_seed7300.pdb"
RFDIFFUSION_PDB = RUNTIME_ROOT / "rfdiffusion/evidence/design/design_8100.pdb"
RFDIFFUSION_MOTIF_PDB = RUNTIME_ROOT / "rfdiffusion/evidence/design/design_9100_motif.pdb"
RFDIFFUSION_TARGET_PDB = RUNTIME_ROOT / "rfdiffusion/contract/fixtures/scaffold-motif/1UBQ.pdb"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _fragment(model_id: str) -> dict[str, Any]:
    return _load(RUNTIME_ROOT / model_id / "activation/fragment.json")


def _profile(model_id: str) -> dict[str, Any]:
    return _fragment(model_id)["profile_projection"]["profile"]


def _request(model_id: str) -> dict[str, Any]:
    return _load(RUNTIME_ROOT / model_id / "activation/public-request.json")


def _input(model_id: str, **overrides: object) -> ScientificInputArtifact:
    values: dict[str, object]
    if model_id == mosaic.MODEL_ID:
        values = {
            "logical_artifact_id": mosaic.TARGET_INPUT_ID,
            "semantic_type": mosaic.TARGET_SEMANTIC_TYPE,
            "artifact_id": UUID("10000000-0000-4000-8000-000000000001"),
            "digest": "sha256:" + "a" * 64,
            "size_bytes": 512,
            "media_type": mosaic.TARGET_MEDIA_TYPE,
            "compression": "none",
        }
    else:
        values = {
            "logical_artifact_id": rfdiffusion.DESIGN_INPUT_ID,
            "semantic_type": rfdiffusion.DESIGN_INPUT_SEMANTIC_TYPE,
            "artifact_id": UUID("20000000-0000-4000-8000-000000000001"),
            "digest": "sha256:" + "b" * 64,
            "size_bytes": 45,
            "media_type": "text/plain",
            "compression": "none",
        }
    values.update(overrides)
    return ScientificInputArtifact(**values)  # type: ignore[arg-type]


def _plan(model_id: str, *, count: int = 1) -> AdapterExecutionPlan:
    request = _request(model_id)
    if model_id == mosaic.MODEL_ID:
        request["parameters"]["shard_count"] = count
        artifact = _input(model_id)
        variant = mosaic.VARIANT_ID
    else:
        request["parameters"]["num_designs"] = count
        artifact = _input(model_id)
        variant = rfdiffusion.VARIANT_ID
    install_production_adapters()
    return compile_adapter_run(
        model_id,
        _profile(model_id),
        request,
        operation_id=f"operation-{model_id}-controller-test",
        variant_id=variant,
        input_artifacts=(artifact,),
    )


def _rfdiffusion_motif_plan() -> AdapterExecutionPlan:
    request = _request(rfdiffusion.MODEL_ID)
    request["operation"] = "scaffold-motif"
    request["parameters"] = {
        "schema": rfdiffusion.PARAMETER_SCHEMA,
        "operation": "scaffold-motif",
        "contigs": ["10-10/A23-34/10-10"],
        "num_designs": 1,
        "seed": 9100,
        "diffuser_T": 50,
        "hotspot_residues": ["A23"],
        "input_pdb_artifact_id": "artifact.rfdiffusion.target.1ubq",
        "motif_ca_rmsd_limit": 1.5,
    }
    target = ScientificInputArtifact(
        logical_artifact_id=rfdiffusion.TARGET_INPUT_ID,
        semantic_type=rfdiffusion.TARGET_INPUT_SEMANTIC_TYPE,
        artifact_id=UUID("20000000-0000-4000-8000-000000000002"),
        digest="sha256:" + hashlib.sha256(RFDIFFUSION_TARGET_PDB.read_bytes()).hexdigest(),
        size_bytes=RFDIFFUSION_TARGET_PDB.stat().st_size,
        media_type="chemical/x-pdb",
        compression="none",
    )
    return compile_adapter_run(
        rfdiffusion.MODEL_ID,
        _profile(rfdiffusion.MODEL_ID),
        request,
        operation_id="operation-rfdiffusion-motif-controller-test",
        variant_id=rfdiffusion.VARIANT_ID,
        input_artifacts=(target,),
    )


def _completion(invocation: StageInvocation, **overrides: object) -> bytes:
    command = invocation.argv[3:]
    value: dict[str, object] = {
        "schema": STAGE_COMPLETION_SCHEMA,
        "status": "passed",
        "stage_id": invocation.stage_id,
        "shard_id": invocation.shard_id,
        "logical_output_id": invocation.produces,
        "collector_id": invocation.collector_id,
        "validator_id": invocation.validator_id,
        "argv_sha256": hashlib.sha256(
            json.dumps(command, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest(),
    }
    value.update(overrides)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _publish_completion(invocation: StageInvocation, workspace: Path, **overrides: object) -> None:
    marker = workspace / ".fs2/stage-complete.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(_completion(invocation, **overrides))


def _write_workspace_documents(invocation: StageInvocation, workspace: Path) -> None:
    for document in invocation.workspace_documents:
        path = workspace.joinpath(*Path(document.relative_path).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document.canonical_json, encoding="utf-8")


class _ArtifactClient:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def download(
        self,
        _artifact_id: UUID,
        *,
        expected_digest: str,
        expected_size_bytes: int,
        expected_media_type: str,
    ) -> bytes:
        assert expected_digest == "sha256:" + hashlib.sha256(self.content).hexdigest()
        assert expected_size_bytes == len(self.content)
        assert expected_media_type == "application/octet-stream"
        return self.content


def _assert_shell_free(invocation: StageInvocation) -> None:
    assert invocation.argv[:3] == (
        "python",
        f"{invocation.working_directory}/.fs2/stage-runner.py",
        "--",
    )
    assert not any(value in {"sh", "bash", "/bin/sh", "/bin/bash", "-c"} for value in invocation.argv)


@pytest.mark.parametrize(
    ("model_id", "expected_stages", "gpu_stage", "cpu_stage", "expected_artifacts"),
    [
        (
            mosaic.MODEL_ID,
            (("design", ()), ("aggregate", ("design",))),
            "design",
            "aggregate",
            mosaic.MODEL_ARTIFACTS,
        ),
        (
            rfdiffusion.MODEL_ID,
            (("inference", ()), ("collect", ("inference",))),
            "inference",
            "collect",
            (rfdiffusion.CHECKPOINT_ARTIFACT,),
        ),
    ],
)
def test_fragment_dag_compiles_through_global_registry_with_exact_shell_free_invocations(
    model_id: str,
    expected_stages: tuple[tuple[str, tuple[str, ...]], ...],
    gpu_stage: str,
    cpu_stage: str,
    expected_artifacts: tuple[str, ...],
) -> None:
    fragment = _fragment(model_id)
    profile = fragment["profile_projection"]["profile"]
    assert profile["state"] == "active"
    assert profile["route_exposed"] is True
    assert profile["source"]["classification"] == "qualified-input"
    assert profile["interface"]["mcp"]["invocable"] is True
    assert profile["semantic_validation"]["state"] == "active"
    assert fragment["execution_projection"]["state"] == "ready-for-serialized-integration"

    plan = _plan(model_id, count=2)
    stages = tuple((stage.stage_id, stage.depends_on) for stage in plan.controller_plan.stages)
    assert stages == expected_stages
    assert tuple(item["id"] for item in fragment["execution_projection"]["stages"]) == tuple(
        stage for stage, _needs in expected_stages
    )
    assert plan.required_model_artifacts == expected_artifacts
    assert len([item for item in plan.invocations if item.stage_id == gpu_stage]) == 2
    assert len([item for item in plan.invocations if item.stage_id == cpu_stage]) == 1

    produced = {item.produces for item in plan.invocations if item.stage_id == gpu_stage}
    terminal = next(item for item in plan.invocations if item.stage_id == cpu_stage)
    assert set(terminal.consumes) == produced
    assert all(item.collector_id == item.validator_id == plan.variant_id for item in plan.invocations)
    assert all(item.runtime_artifacts == expected_artifacts for item in plan.invocations if item.stage_id == gpu_stage)
    assert terminal.runtime_artifacts == ()
    assert all(item.handoff_name == "stage-handoff" for item in plan.invocations if item.stage_id == gpu_stage)
    assert terminal.handoff_name is None
    for invocation in plan.invocations:
        _assert_shell_free(invocation)
        assert "rfdiffusion-upstream" not in json.dumps(invocation.argv)
    assert plan.model_id in {"mosaic", "rfdiffusion"}

    if model_id == mosaic.MODEL_ID:
        design = plan.invocations[0]
        assert design.argv[3:] == (
            mosaic.RUNTIME_ENTRYPOINT,
            "run-shard",
            "--request",
            f"{design.working_directory}/.fs2/request.json",
            "--input-manifest",
            f"{design.working_directory}/.fs2/input-manifest.json",
            "--recipe",
            mosaic.RUNTIME_RECIPE,
            "--recipe-sha256",
            mosaic.RECIPE_SHA256,
            "--shard-index",
            "0",
            "--seed",
            "7300",
            "--output",
            f"{design.working_directory}/shards/000",
        )
        assert (
            dict(terminal.environment)["FS2_RUNTIME_IMAGE_DIGEST"]
            == profile["execution_identity"]["runtime_image_digest"]
        )
    else:
        inference = plan.invocations[0]
        assert inference.argv[3:6] == ("python", rfdiffusion.RUNTIME_ENTRYPOINT, "run")
        assert inference.argv[inference.argv.index("--artifact-root") + 1] == rfdiffusion.CHECKPOINT_MOUNT
        assert inference.argv[inference.argv.index("--input-artifact-root") + 1] == (
            f"{inference.working_directory}/shards/000"
        )
        assert inference.argv[inference.argv.index("--checkpoint-artifact-id") + 1] == (
            rfdiffusion.CHECKPOINT_RUNTIME_ID
        )


@pytest.mark.parametrize("model_id", [mosaic.MODEL_ID, rfdiffusion.MODEL_ID])
def test_nonterminal_model_handoffs_round_trip_through_real_materializer(
    model_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(model_id)
    invocation = plan.invocations[0]
    workspace = tmp_path / model_id / "producer"
    payload = workspace / (
        "shards/000/shard-result.json" if model_id == mosaic.MODEL_ID else "shards/000/result/result.json"
    )
    payload.parent.mkdir(parents=True)
    payload.write_text('{"status":"succeeded"}', encoding="utf-8")
    if model_id == mosaic.MODEL_ID:
        (payload.parent / "candidate-metrics.json").write_text("{}", encoding="utf-8")
        (payload.parent / "candidate.pdb").write_text("ATOM fixture", encoding="utf-8")
    else:
        structure = payload.parent / "designs/design_8100.pdb"
        structure.parent.mkdir()
        structure.write_text("ATOM fixture", encoding="utf-8")
    private_input = workspace / "inputs/request-artifact"
    private_input.parent.mkdir(parents=True)
    private_input.write_text("must not enter a stage handoff", encoding="utf-8")
    scratch = workspace / "scratch/cache.bin"
    scratch.parent.mkdir(parents=True)
    scratch.write_bytes(b"must not enter a stage handoff")
    _publish_completion(invocation, workspace)

    first = collect_stage_output(invocation, workspace)
    second = collect_stage_output(invocation, workspace)
    handoff = first.artifacts[0].path.read_bytes()
    assert second.artifacts[0].path.read_bytes() == handoff
    assert first.artifacts[0].name == "stage-handoff"
    assert first.artifacts[0].compression == "zstd"

    root = tmp_path / "materializer-root"
    root.mkdir()
    monkeypatch.setattr(companion, "_ROOT", root)
    destination = root / "consumer"
    destination.mkdir()
    companion.materialize_artifact(
        client=_ArtifactClient(handoff),  # type: ignore[arg-type]
        artifact_id=UUID("30000000-0000-4000-8000-000000000001"),
        destination=destination,
        mode=MaterializationMode.OVERLAY_TAR,
        compression="zstd",
        yaml_name=None,
        reuse_prefix=None,
        expected_digest="sha256:" + hashlib.sha256(handoff).hexdigest(),
        expected_size_bytes=len(handoff),
        expected_media_type="application/octet-stream",
    )
    restored = destination / payload.relative_to(workspace)
    assert restored.read_text(encoding="utf-8") == '{"status":"succeeded"}'
    assert not (destination / "inputs/request-artifact").exists()
    assert not (destination / "scratch").exists()
    assert not (destination / ".fs2/stage-complete.json").exists()


def _pointer(path: Path, artifact_id: str, media_type: str) -> dict[str, object]:
    content = path.read_bytes()
    return {
        "artifact_id": artifact_id,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "media_type": media_type,
        "compression": "none",
    }


def _mosaic_final_workspace(invocation: StageInvocation, workspace: Path, *, metrics_seed: int = 7300) -> None:
    _write_workspace_documents(invocation, workspace)
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True)
    request_sha256 = dict(invocation.environment)["FS2_MOSAIC_REQUEST_SHA256"]
    image_digest = dict(invocation.environment)["FS2_RUNTIME_IMAGE_DIGEST"]
    values: tuple[tuple[str, str, str, str, bytes], ...] = (
        (
            "shard-000",
            "mosaic-shard-result-json/v1",
            "artifact.mosaic.shard.000",
            "application/json",
            json.dumps(
                {
                    "backend_id": mosaic.VARIANT_ID,
                    "source_revision": mosaic.SOURCE_REVISION,
                    "recipe_sha256": mosaic.RECIPE_SHA256,
                    "index": 0,
                    "seed": 7300,
                    "status": "succeeded",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        ),
        (
            "aggregate",
            "mosaic-aggregate-json/v1",
            "artifact.mosaic.aggregate",
            "application/json",
            json.dumps(
                {
                    "backend_id": mosaic.VARIANT_ID,
                    "source_revision": mosaic.SOURCE_REVISION,
                    "recipe_sha256": mosaic.RECIPE_SHA256,
                    "request_sha256": request_sha256,
                    "runtime_image_digest": image_digest,
                    "expected_shards": 1,
                    "succeeded_shards": 1,
                    "atomic_commit": True,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        ),
        (
            "candidate-000-metrics",
            "mosaic-design-metrics-json/v1",
            "artifact.mosaic.candidate.000.metrics",
            "application/json",
            json.dumps(
                {
                    "candidate_id": "design-000",
                    "shard_index": 0,
                    "seed": metrics_seed,
                    "sequence": "VGLALYCLWPELFDGDAEEHHDEEALSEGKLPNEAYLAIG",
                    "iptm": 0.5,
                    "mean_plddt": 0.75,
                    "objective": 1.25,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        ),
        (
            "candidate-000-structure",
            "protein-structure-pdb/v1",
            "artifact.mosaic.candidate.000.pdb",
            "chemical/x-pdb",
            MOSAIC_PDB.read_bytes(),
        ),
    )
    entries: list[dict[str, object]] = []
    index: dict[str, str] = {}
    for name, semantic_type, artifact_id, media_type, content in values:
        path = artifacts / artifact_id
        path.write_bytes(content)
        index[artifact_id] = str(path.resolve())
        entries.append(
            {
                "name": name,
                "semantic_type": semantic_type,
                "artifact": _pointer(path, artifact_id, media_type),
            }
        )
    (workspace / "output-manifest.json").write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
                "manifest_id": "manifest.mosaic.output",
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    (workspace / "artifact-index.json").write_text(json.dumps(index), encoding="utf-8")
    _publish_completion(invocation, workspace)


def test_mosaic_terminal_collector_accepts_only_bound_semantic_outputs(tmp_path: Path) -> None:
    invocation = _plan(mosaic.MODEL_ID).invocations[-1]
    workspace = tmp_path / "mosaic-final"
    _mosaic_final_workspace(invocation, workspace)
    result = collect_stage_output(invocation, workspace)
    assert tuple(item.name for item in result.artifacts) == (
        "aggregate",
        "candidate-000-metrics",
        "candidate-000-structure",
    )
    assert result.validation["status"] == "passed"
    assert result.validation["candidate_count"] == 1
    assert (workspace / ".fs2/mosaic-semantic-validation.json").is_file()

    invalid = tmp_path / "mosaic-invalid"
    _mosaic_final_workspace(invocation, invalid, metrics_seed=7299)
    with pytest.raises(ScientificAdapterError, match="identity, seed, or sequence"):
        collect_stage_output(invocation, invalid)


def _rfdiffusion_final_workspace(
    invocation: StageInvocation,
    workspace: Path,
    *,
    operation: str = "design-backbone",
    seed: int = 8100,
    contigs: tuple[str, ...] = ("76-76",),
    source_pdb: Path = RFDIFFUSION_PDB,
    residue_count: int = 76,
    motif_positions: int | None = None,
    motif_rmsd: float | None = None,
    pdb_path: str | None = None,
) -> None:
    _write_workspace_documents(invocation, workspace)
    if operation == "scaffold-motif":
        target_materialization = next(
            item for item in invocation.materializations if item.artifact_id == rfdiffusion.TARGET_INPUT_ID
        )
        target_path = workspace / "inputs" / PurePosixPath(target_materialization.destination).name
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(RFDIFFUSION_TARGET_PDB.read_bytes())
    output = workspace / "shards/000/result"
    relative_pdb = pdb_path or f"designs/design_{seed}.pdb"
    structure = output / f"designs/design_{seed}.pdb"
    structure.parent.mkdir(parents=True)
    structure.write_bytes(source_pdb.read_bytes())
    minimum_residues = 32 if operation == "scaffold-motif" else residue_count
    maximum_residues = 32 if operation == "scaffold-motif" else residue_count
    superposition: dict[str, object] | None = None
    if motif_rmsd is not None:
        superposition = {
            "method": "horn-quaternion-optimal-superposition",
            "rmsd_angstrom": motif_rmsd,
            "rmsd_unaligned_angstrom": 47.9563,
            "rigid_body_rotation_degrees": 0.978,
            "rigid_body_translation_angstrom": 47.9561,
            "note": "test fixture",
        }
    request_document = next(
        document for document in invocation.workspace_documents if document.relative_path == ".fs2/request.json"
    )
    parameters = rfdiffusion.Parameters.parse(json.loads(request_document.canonical_json)["parameters"])
    motif_input = rfdiffusion._motif_input(invocation, workspace, parameters)  # noqa: SLF001
    upstream_argv = rfdiffusion._expected_upstream_argv(  # noqa: SLF001
        invocation,
        index=0,
        parameters=parameters,
        motif_input=motif_input,
    )
    producer = PurePosixPath(invocation.working_directory).parent / "design-000"
    result = {
        "schema": "fs2-serve.nebius.ai/scientific-run-result/v1",
        "model_id": rfdiffusion.MODEL_ID,
        "adapter_id": rfdiffusion.RUNTIME_ADAPTER_ID,
        "status": "succeeded",
        "operation": operation,
        "checkpoint": {
            "artifact_id": rfdiffusion.CHECKPOINT_RUNTIME_ID,
            "path": f"{rfdiffusion.CHECKPOINT_MOUNT}/Base_ckpt.pt",
            "sha256": rfdiffusion.CHECKPOINT_SHA256,
            "size_bytes": rfdiffusion.CHECKPOINT_BYTES,
            "digest_verified": True,
        },
        "request": {
            "operation": operation,
            "contigs": list(contigs),
            "num_designs": 1,
            "seed": seed,
            "diffuser_T": 50,
            "length": None,
            "hotspot_residues": ["A23"] if operation == "scaffold-motif" else [],
            "requested_residues": {"minimum": minimum_residues, "maximum": maximum_residues},
        },
        "accelerator": {
            "devices": ["NVIDIA H100 80GB HBM3"],
            "cuda_execution_confirmed": True,
            "evidence": "upstream .trb run metadata records torch.cuda.get_device_name()",
        },
        "shell_free": True,
        "upstream_argv": list(upstream_argv),
        "upstream": {
            "returncode": 0,
            "log_path": str(producer / "shards/000/result/upstream.log"),
            "model_ready_seconds": 12.0,
        },
        "cache_level": {
            "declared": "artifact-local",
            "source": "submitter-declared",
            "note": "No GPU snapshot was used by this fixture.",
            "gpu_snapshot_used": False,
        },
        "designs": [
            {
                "design_index": seed,
                "seed": seed,
                "pdb": {
                    "path": relative_pdb,
                    "sha256": hashlib.sha256(structure.read_bytes()).hexdigest(),
                    "size_bytes": structure.stat().st_size,
                },
                "residue_count": residue_count,
                "chains": ["A"],
                "device": "NVIDIA H100 80GB HBM3",
                "upstream_seconds": 70.266,
                "motif_positions_preserved": motif_positions,
                "motif_ca_rmsd_angstrom": motif_rmsd,
                "motif_superposition": superposition,
            }
        ],
        "phases_seconds": {
            "validate_request": 0.004,
            "resolve_checkpoint": 0.36,
            "resolve_inputs": 0.0,
            "upstream_execute": 70.266,
            "verify_artifacts": 0.059,
            "write_envelope": 0.003,
        },
        "total_seconds": 70.692,
    }
    if motif_input is not None:
        result["input_pdb"] = {
            "artifact_id": motif_input.artifact_id,
            "sha256": motif_input.sha256,
            "size_bytes": motif_input.size_bytes,
            "residue_count": motif_input.structure.residue_count,
        }
    (output / "result.json").write_text(json.dumps(result), encoding="utf-8")
    _publish_completion(invocation, workspace)


def test_rfdiffusion_terminal_collector_uses_real_evidence_and_rejects_traversal(tmp_path: Path) -> None:
    invocation = _plan(rfdiffusion.MODEL_ID).invocations[-1]
    workspace = tmp_path / "rfdiffusion-final"
    _rfdiffusion_final_workspace(invocation, workspace)
    result = collect_stage_output(invocation, workspace)
    assert tuple(item.name for item in result.artifacts) == (
        "design-000-result",
        "design-000-structure",
    )
    assert result.validation["status"] == "passed"
    summary = json.loads(result.artifacts[0].path.read_text(encoding="utf-8"))
    assert summary["model_id"] == "rfdiffusion"
    assert summary["design"]["pdb"]["sha256"] == hashlib.sha256(RFDIFFUSION_PDB.read_bytes()).hexdigest()

    invalid = tmp_path / "rfdiffusion-traversal"
    _rfdiffusion_final_workspace(invocation, invalid, pdb_path="../../outside.pdb")
    with pytest.raises(ScientificAdapterError, match="escapes its shard output"):
        collect_stage_output(invocation, invalid)


def test_rfdiffusion_motif_collector_binds_integer_preservation_evidence(tmp_path: Path) -> None:
    plan = _rfdiffusion_motif_plan()
    inference = plan.invocations[0]
    assert dict(inference.environment)["FS2_INPUT_ARTIFACT_ROOT"] == (f"{inference.working_directory}/shards/000")
    runtime_request = json.loads(inference.workspace_documents[0].canonical_json)
    assert runtime_request["parameters"]["input_pdb_artifact_id"] == ("20000000-0000-4000-8000-000000000002")

    invocation = plan.invocations[-1]
    assert invocation.consumes[-1] == rfdiffusion.TARGET_INPUT_ID
    assert invocation.materializations[-1].artifact_id == rfdiffusion.TARGET_INPUT_ID
    workspace = tmp_path / "rfdiffusion-motif-final"
    _rfdiffusion_final_workspace(
        invocation,
        workspace,
        operation="scaffold-motif",
        seed=9100,
        contigs=("10-10/A23-34/10-10",),
        source_pdb=RFDIFFUSION_MOTIF_PDB,
        residue_count=32,
        motif_positions=12,
        motif_rmsd=0.1129,
    )
    result = collect_stage_output(invocation, workspace)
    summary = json.loads(result.artifacts[0].path.read_text(encoding="utf-8"))
    assert summary["operation"] == "scaffold-motif"
    assert summary["design"]["motif_positions_preserved"] == 12
    assert summary["design"]["motif_ca_rmsd_angstrom"] == 0.1129

    target_path = workspace / "inputs" / PurePosixPath(invocation.materializations[-1].destination).name
    target_path.write_bytes(b"not the frozen target")
    with pytest.raises(ScientificAdapterError, match="target bytes differ"):
        collect_stage_output(invocation, workspace)


@pytest.mark.parametrize(
    ("model_id", "bad_artifact"),
    [
        (mosaic.MODEL_ID, {"logical_artifact_id": "wrong"}),
        (mosaic.MODEL_ID, {"semantic_type": "wrong-type/v1"}),
        (mosaic.MODEL_ID, {"media_type": "application/json"}),
        (rfdiffusion.MODEL_ID, {"logical_artifact_id": "wrong"}),
        (rfdiffusion.MODEL_ID, {"semantic_type": "wrong-type/v1"}),
        (rfdiffusion.MODEL_ID, {"media_type": "application/json"}),
    ],
)
def test_public_inputs_are_selected_only_from_verified_manifest_entries(
    model_id: str, bad_artifact: dict[str, object]
) -> None:
    module = mosaic if model_id == mosaic.MODEL_ID else rfdiffusion
    with pytest.raises(ScientificAdapterError):
        module.compile_run(
            _profile(model_id),
            _request(model_id),
            operation_id=f"operation-{model_id}-bad-input",
            input_artifacts=(_input(model_id, **bad_artifact),),
        )
    with pytest.raises(ScientificAdapterError):
        module.compile_run(
            _profile(model_id),
            _request(model_id),
            operation_id=f"operation-{model_id}-missing-input",
            input_artifacts=(),
        )


def test_parameter_bounds_reject_untrusted_shell_or_hydra_tokens() -> None:
    mosaic_request = _request(mosaic.MODEL_ID)
    mosaic_request["parameters"]["hotspots"] = [7, 3]
    with pytest.raises(ScientificAdapterError, match="sorted unique"):
        mosaic.compile_run(
            _profile(mosaic.MODEL_ID),
            mosaic_request,
            operation_id="operation-mosaic-bad-parameters",
            input_artifacts=(_input(mosaic.MODEL_ID),),
        )

    for contig in ("76-76; touch /tmp/pwned", "inference.output_prefix=/tmp/escape", "../76-76"):
        request = copy.deepcopy(_request(rfdiffusion.MODEL_ID))
        request["parameters"]["contigs"] = [contig]
        with pytest.raises(ScientificAdapterError, match="unsupported Hydra token"):
            rfdiffusion.compile_run(
                _profile(rfdiffusion.MODEL_ID),
                request,
                operation_id="operation-rfdiffusion-bad-token",
                input_artifacts=(_input(rfdiffusion.MODEL_ID),),
            )

    unreachable = copy.deepcopy(_request(rfdiffusion.MODEL_ID))
    unreachable["parameters"]["length"] = 75
    with pytest.raises(ScientificAdapterError, match="reachable from the bounded contigs"):
        rfdiffusion.compile_run(
            _profile(rfdiffusion.MODEL_ID),
            unreachable,
            operation_id="operation-rfdiffusion-unreachable-length",
            input_artifacts=(_input(rfdiffusion.MODEL_ID),),
        )


def test_stale_collector_identity_and_oversized_handoff_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation = _plan(mosaic.MODEL_ID).invocations[0]
    workspace = tmp_path / "stale-mosaic"
    output = workspace / "output.bin"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"x")
    _publish_completion(invocation, workspace, logical_output_id="run.stale.output")
    with pytest.raises(ScientificAdapterError, match="differs from the frozen invocation"):
        collect_stage_output(invocation, workspace)

    oversized = tmp_path / "oversized-rfdiffusion"
    content = oversized / "shards/000/result/designs/design_8100.pdb"
    content.parent.mkdir(parents=True)
    content.write_bytes(b"12345")
    (oversized / "shards/000/result/result.json").write_text("{}", encoding="utf-8")
    rf_invocation = _plan(rfdiffusion.MODEL_ID).invocations[0]
    _publish_completion(rf_invocation, oversized)
    monkeypatch.setattr(rfdiffusion, "MAX_HANDOFF_CONTENT_BYTES", 4)
    with pytest.raises(ScientificAdapterError, match="outside the bound"):
        collect_stage_output(rf_invocation, oversized)

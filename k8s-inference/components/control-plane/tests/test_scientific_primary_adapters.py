"""Adversarial integration tests for the primary scientific adapters."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import tarfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import zstandard
from scientific_batch_fakes import FakeScientificBatchCluster, FakeScientificBatchRepository

from fs2_serve.scientific_batch import (
    ArtifactAccessContext,
    CheckpointMode,
    MaterializationMode,
    PreemptionMode,
    ResourceClass,
    SchedulingSnapshot,
    ScientificAdapterError,
    ScientificBatchController,
    ScientificInputArtifact,
    ScientificStagePlan,
    ServiceClass,
    StageSchedulingDecision,
    VerifiedInputManifest,
    compile_adapter_run,
    load_json_request,
    profile_from_catalog,
)
from fs2_serve.scientific_batch.adapters import boltzgen, proteina_complexa
from fs2_serve.scientific_batch.adapters.common import runtime_recipe_sha256
from fs2_serve.scientific_batch.adapters.materialization import materialize_boltzgen_input
from fs2_serve.scientific_batch.adapters.staged_workspace import STAGE_COMPLETION_SCHEMA
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters"
PROFILE_PATH = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json"
SOURCE_QUALIFICATION_PATH = SOLUTION_ROOT / "models/cancer-immunotherapy/model-source-qualification.json"


def fixture(model_id: str, name: str) -> dict[str, Any]:
    return json.loads((ADAPTER_ROOT / model_id / "fixtures" / name).read_text(encoding="utf-8"))


def profile(model_id: str) -> Mapping[str, object]:
    return profile_from_catalog(json.loads(PROFILE_PATH.read_text(encoding="utf-8")), model_id)


def boltzgen_verified_input(
    *,
    logical_artifact_id: str = "campaign-input",
    semantic_type: str = "boltzgen-campaign-input/v1",
    artifact_id: UUID | None = None,
    size_bytes: int = 1024,
    media_type: str = "application/gzip",
    compression: str | None = "gzip",
) -> ScientificInputArtifact:
    return ScientificInputArtifact(
        logical_artifact_id=logical_artifact_id,
        semantic_type=semantic_type,
        artifact_id=artifact_id or UUID("00000000-0000-0000-0000-000000000001"),
        digest="sha256:" + "c" * 64,
        size_bytes=size_bytes,
        media_type=media_type,
        compression=compression,
    )


def boltzgen_manifest_request() -> dict[str, Any]:
    request = fixture("boltzgen", "positive-design.json")
    request["input_manifest"] = {
        "artifact_id": "00000000-0000-0000-0000-000000000002",
        "sha256": "d" * 64,
        "size_bytes": 512,
        "media_type": "application/vnd.fs2.scientific-manifest+json",
        "compression": "none",
    }
    return request


def proteina_verified_input(
    *,
    logical_artifact_id: str = "target-bundle",
    semantic_type: str = "proteina-complexa-target-bundle/v1",
    artifact_id: UUID | None = None,
    size_bytes: int = 32 * 1024,
    media_type: str = "application/x-tar",
    compression: str | None = "gzip",
) -> ScientificInputArtifact:
    return ScientificInputArtifact(
        logical_artifact_id=logical_artifact_id,
        semantic_type=semantic_type,
        artifact_id=artifact_id or UUID("00000000-0000-0000-0000-000000000011"),
        digest="sha256:" + "a" * 64,
        size_bytes=size_bytes,
        media_type=media_type,
        compression=compression,
    )


def proteina_manifest_request() -> dict[str, Any]:
    request = fixture("proteina-complexa", "positive-protein.json")
    request["input_manifest"] = {
        "artifact_id": "00000000-0000-0000-0000-000000000012",
        "sha256": "b" * 64,
        "size_bytes": 512,
        "media_type": "application/vnd.fs2.scientific-manifest+json",
        "compression": "none",
    }
    return request


def pointer(artifact_id: str, content: bytes, media_type: str) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "media_type": media_type,
    }


def pdb_bytes() -> bytes:
    rows = [
        (1, "N", "A", 1, 11.104, 13.207, 14.099),
        (2, "CA", "A", 1, 12.104, 13.207, 14.099),
        (3, "N", "B", 1, 21.104, 23.207, 24.099),
        (4, "CA", "B", 1, 22.104, 23.207, 24.099),
    ]
    return (
        "\n".join(
            f"ATOM  {serial:5d} {atom:^4s} ALA {chain}{residue:4d}    {x:8.3f}{y:8.3f}{z:8.3f}  1.00 20.00           C"
            for serial, atom, chain, residue, x, y, z in rows
        )
        + "\n"
    ).encode("ascii")


def mmcif_bytes() -> bytes:
    return b"""data_design
#
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.type_symbol
_atom_site.label_atom_id
_atom_site.label_asym_id
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
ATOM 1 N N A 11.104 13.207 14.099
ATOM 2 C CA A 12.104 13.207 14.099
ATOM 3 N N B 21.104 23.207 24.099
ATOM 4 C CA B 22.104 23.207 24.099
#
"""


def publish_stage_completion(invocation: Any, workspace: Path) -> str:
    """Materialize controller documents and the runner-owned terminal marker."""

    metadata = workspace / ".fs2"
    metadata.mkdir(parents=True, exist_ok=True)
    for document in invocation.workspace_documents:
        destination = workspace.joinpath(*Path(document.relative_path).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            assert destination.read_text(encoding="utf-8") == document.canonical_json
        else:
            destination.write_text(document.canonical_json, encoding="utf-8")
    argv = invocation.argv[3:]
    payload = json.dumps(
        {
            "schema": STAGE_COMPLETION_SCHEMA,
            "status": "passed",
            "stage_id": invocation.stage_id,
            "shard_id": invocation.shard_id,
            "logical_output_id": invocation.produces,
            "collector_id": invocation.collector_id,
            "validator_id": invocation.validator_id,
            "argv_sha256": hashlib.sha256(
                json.dumps(argv, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    (metadata / "stage-complete.json").write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def output_manifest(items: list[tuple[str, str, str, bytes]]) -> tuple[dict[str, object], dict[str, bytes]]:
    blobs = {artifact_id: content for _, _, artifact_id, content in items}
    return (
        {
            "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
            "manifest_id": "result.manifest.01",
            "entries": [
                {
                    "name": name,
                    "semantic_type": semantic_type,
                    "artifact": pointer(
                        artifact_id,
                        content,
                        (
                            "text/csv"
                            if semantic_type.endswith("csv/v1")
                            else "application/json"
                            if semantic_type.endswith("analysis/v1")
                            else "chemical/x-mmcif"
                            if content.startswith(b"data_")
                            else "chemical/x-pdb"
                        ),
                    ),
                }
                for name, semantic_type, artifact_id, content in items
            ],
        },
        blobs,
    )


def proteina_output_manifest(results: bytes) -> tuple[dict[str, object], dict[str, bytes]]:
    return output_manifest(
        [
            ("design-1-structure", "protein-complex-structure/v1", "proteina.structure.1", pdb_bytes()),
            ("results.1", "proteina-complexa-results-csv/v1", "proteina.results.1", results),
        ]
    )


def test_real_catalog_profiles_project_to_canonical_controller_types() -> None:
    proteina_request = proteina_manifest_request()
    proteina = compile_adapter_run(
        "proteina-complexa",
        profile("proteina-complexa"),
        proteina_request,
        operation_id="op-proteina-01",
        input_artifacts=(proteina_verified_input(),),
    )
    assert all(isinstance(stage, ScientificStagePlan) for stage in proteina.controller_plan.stages)
    assert [stage.stage_id for stage in proteina.controller_plan.stages] == [
        "generate",
        "filter",
        "evaluate",
        "analyze",
    ]
    assert [stage.depends_on for stage in proteina.controller_plan.stages] == [
        (),
        ("generate",),
        ("filter",),
        ("evaluate",),
    ]
    assert [stage.resource_class for stage in proteina.controller_plan.stages] == [
        ResourceClass.GPU,
        ResourceClass.CPU,
        ResourceClass.GPU,
        ResourceClass.CPU,
    ]

    boltz_request = boltzgen_manifest_request()
    boltz = compile_adapter_run(
        "boltzgen",
        profile("boltzgen"),
        boltz_request,
        operation_id="op-boltz-01",
        input_artifacts=(boltzgen_verified_input(),),
    )
    assert [stage.stage_id for stage in boltz.controller_plan.stages] == [
        "configure",
        "design",
        "inverse-folding",
        "folding",
        "design-folding",
        "analysis",
        "filtering",
    ]
    assert all(stage.shards == ("pdl1-a", "pdl1-b") for stage in boltz.controller_plan.stages)
    assert [stage.resource_class for stage in boltz.controller_plan.stages] == [
        ResourceClass.GPU,
        ResourceClass.GPU,
        ResourceClass.GPU,
        ResourceClass.GPU,
        ResourceClass.GPU,
        ResourceClass.CPU,
        ResourceClass.CPU,
    ]
    assert boltz.controller_plan.stage("analysis").depends_on == ("design-folding",)
    assert boltz.controller_plan.stage("filtering").depends_on == ("analysis",)
    assert boltz.controller_plan.stage("configure").checkpoint_mode is CheckpointMode.RESTART
    assert boltz.controller_plan.stage("configure").preemption_mode is PreemptionMode.RESTARTABLE
    assert boltz.controller_plan.stage("configure").max_attempts == 2


@pytest.mark.asyncio
async def test_controller_rejects_adapter_plan_before_operator_execution_binding() -> None:
    request = proteina_manifest_request()
    operation_id = uuid4()
    execution = compile_adapter_run(
        "proteina-complexa",
        profile("proteina-complexa"),
        request,
        operation_id=str(operation_id),
        variant_id=proteina_complexa.VARIANT_ID,
        input_artifacts=(proteina_verified_input(),),
    )
    captured_at = datetime(2026, 9, 2, tzinfo=UTC)
    scheduling = SchedulingSnapshot(
        policy_revision="sha256:" + "1" * 64,
        captured_at=captured_at,
        service_class=ServiceClass.CUSTOMER_BATCH,
        tenant_queue="cancer-immunotherapy",
        model_lane="protein-design",
        workload_namespace="fs2-scientific",
        route_namespace="fs2-scientific",
        stages=tuple(
            StageSchedulingDecision(
                stage_id=stage.stage_id,
                resource_class=stage.resource_class,
                resolved_cluster_queue="inference-accelerators",
                resolved_local_queue="scientific-batch",
                workload_priority_class="fs2-customer-batch",
                workload_priority_value=100,
                resolved_pool_preference=(
                    ("h100-reserved-8x", "h100-1x") if stage.resource_class is ResourceClass.GPU else ()
                ),
                admitted_resource_flavor="inference-h100-1x" if stage.resource_class is ResourceClass.GPU else None,
                accelerator_resource_name="nvidia.com/gpu" if stage.resource_class is ResourceClass.GPU else None,
                accelerator_count=1 if stage.resource_class is ResourceClass.GPU else 0,
                max_queue_seconds=600,
                max_execution_seconds=3600,
                checkpoint_mode=stage.checkpoint_mode,
                preemption_mode=stage.preemption_mode,
            )
            for stage in execution.controller_plan.stages
        ),
    )
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()
    controller = ScientificBatchController(
        repository=repository,
        cluster=cluster,
        controller_id="controller:one",
        namespace="fs2-scientific",
        clock=lambda: captured_at,
    )
    with pytest.raises(ValueError, match="not bound"):
        await controller.admit(
            operation_id=operation_id,
            tenant_id="cancer-immunotherapy",
            model_id="proteina-complexa",
            variant_id=proteina_complexa.VARIANT_ID,
            plan=execution.controller_plan,
            scheduling=scheduling,
            execution_plan=execution,
        )
    assert cluster.apply_history == []
    assert repository.records == {}


def test_adapter_identities_match_the_checked_in_source_qualification() -> None:
    qualification = json.loads(SOURCE_QUALIFICATION_PATH.read_text(encoding="utf-8"))["models"]
    proteina_source = qualification["proteina-complexa"]
    proteina_contract = json.loads((ADAPTER_ROOT / "proteina-complexa/contract.json").read_text(encoding="utf-8"))
    assert proteina_contract["source"]["repository"] == proteina_source["code"]["repository"].removeprefix(
        "https://github.com/"
    )
    assert proteina_contract["source"]["revision"] == proteina_source["code"]["revision"]
    assert proteina_contract["source"]["release_tag"] is None
    assert len(proteina_contract["variants"]) == 3
    runtime_variant_ids = ("complexa-protein", "complexa-ligand", "complexa-ame")
    for contract_variant, source_variant, runtime_artifact_id in zip(
        proteina_contract["variants"], proteina_source["weights"], runtime_variant_ids, strict=True
    ):
        assert contract_variant["artifact_id"] == source_variant["artifact_id"]
        assert contract_variant["runtime_artifact_id"] == runtime_artifact_id
        assert contract_variant["repository"] == source_variant["locator"]
        assert contract_variant["revision"] == source_variant["revision"]
        assert contract_variant["files"] == source_variant["files"]
        assert contract_variant["gate"] == source_variant["gate"] == "none"

    boltz_source = qualification["boltzgen"]
    boltz_contract = json.loads((ADAPTER_ROOT / "boltzgen/contract.json").read_text(encoding="utf-8"))
    assert boltz_contract["source"]["repository"] == boltz_source["code"]["repository"].removeprefix(
        "https://github.com/"
    )
    assert boltz_contract["source"]["release_tag"] == "v0.3.2"
    assert boltz_contract["source"]["revision"] == boltz_source["code"]["revision"]
    assert boltz_contract["weights"]["artifact_id"] == boltz_source["weights"][0]["artifact_id"]
    assert boltz_contract["weights"]["repository"] == boltz_source["weights"][0]["locator"]
    assert boltz_contract["weights"]["revision"] == boltz_source["weights"][0]["revision"]
    assert boltz_contract["weights"]["files"] == boltz_source["weights"][0]["files"]
    assert boltz_contract["weights"]["gate"] == boltz_source["weights"][0]["gate"] == "none"
    assert boltz_contract["weights"]["runtime_artifact_id"] == "boltzgen-checkpoints"
    assert boltz_contract["runtime_reference_artifact"] == "boltzgen-inference-molecules"
    assert proteina_contract["runtime_artifact_mount_root"] == "/opt/fs2/artifacts"
    assert boltz_contract["runtime_artifact_mount_root"] == "/opt/fs2/artifacts"


def test_catalog_recipe_hashes_cover_the_adapter_and_canonical_workload() -> None:
    for model_id in ("proteina-complexa", "boltzgen"):
        candidate = profile(model_id)
        identity = candidate["execution_identity"]
        workload = candidate["workload"]
        runtime_hash = runtime_recipe_sha256(SOLUTION_ROOT, model_id)
        workload_bytes = json.dumps(workload, separators=(",", ":"), sort_keys=True).encode()
        workload_hash = hashlib.sha256(workload_bytes).hexdigest()
        assert identity["runtime_recipe_sha256"] == runtime_hash
        assert identity["workload_recipe_sha256"] == workload_hash


def test_proteina_commands_and_artifact_handoffs_are_exact_and_shell_free() -> None:
    request = fixture("proteina-complexa", "positive-ligand.json")
    result = proteina_complexa.compile_run(profile("proteina-complexa"), request, operation_id="op-ligand-02")
    assert result.required_model_artifacts == (
        "complexa-ligand",
        "rosettafold3-checkpoint",
    )
    previous = request["input_manifest"]["artifact_id"]
    for invocation, stage_id in zip(result.invocations, ("generate", "filter", "evaluate", "analyze"), strict=True):
        assert invocation.argv[:3] == (
            "python",
            f"{invocation.working_directory}/.fs2/stage-runner.py",
            "--",
        )
        assert invocation.argv[3:5] == ("complexa", stage_id)
        config_index = 5
        if stage_id == "filter":
            assert invocation.argv[5] == "--verbose"
            config_index = 6
        assert invocation.argv[config_index] == "/opt/fs2/source/configs/search_ligand_binder_local_pipeline.yaml"
        assert invocation.argv[0] not in {"sh", "bash"}
        assert invocation.consumes == (previous,)
        assert invocation.produces.startswith("run.")
        previous = invocation.produces
        assert invocation.materializations[0].artifact_id == invocation.consumes[0]
        assert invocation.materializations[0].destination.startswith("/mnt/fs2-scientific/work/proteina-complexa/")
        assert invocation.materializations[0].destination.endswith("/main")
        assert invocation.collector_id == invocation.validator_id == proteina_complexa.VALIDATOR_ID
        assert invocation.handoff_name == (None if stage_id == "analyze" else proteina_complexa.STAGE_HANDOFF_NAME)
        assert all("artifact://" not in value for value in invocation.argv)
        assert all("/mnt/fs2-scientific/models" not in value for value in invocation.argv)
    assert "++ckpt_name=complexa_ligand.ckpt" in result.invocations[0].argv
    assert any(value.endswith("/complexa_ligand_ae.ckpt") for value in result.invocations[0].argv)
    assert all("++ckpt_name=" not in argument for item in result.invocations[1:] for argument in item.argv)
    assert "++generation.args.nsteps=30" in result.invocations[0].argv
    assert "++generation.dataloader.dataset.nres.nsamples=3" in result.invocations[0].argv
    assert result.invocations[0].runtime_artifacts == ("complexa-ligand", "rosettafold3-checkpoint")
    assert result.invocations[1].runtime_artifacts == ()
    assert result.invocations[2].runtime_artifacts == ("rosettafold3-checkpoint",)
    environment = dict(result.invocations[0].environment)
    assert environment["DATA_PATH"] == result.invocations[0].working_directory
    assert environment["RF3_CKPT_PATH"] == (
        "/opt/fs2/artifacts/rosettafold3-checkpoint/rf3_foundry_01_24_latest_remapped.ckpt"
    )
    assert environment["RF3_EXEC_PATH"] == "/opt/venv/bin/rf3"
    assert "ESM_DIR" not in environment
    assert "++metric.compute_esm_metrics=false" in result.invocations[2].argv
    assert "++metric.compute_monomer_metrics=false" in result.invocations[2].argv
    assert "++metric.sequence_types=[self]" in result.invocations[2].argv


@pytest.mark.parametrize(
    ("variant", "config_stem"),
    (
        ("protein-target", "search_binder_local_pipeline"),
        ("ligand-target", "search_ligand_binder_local_pipeline"),
        ("ame", "search_ame_local_pipeline"),
    ),
)
def test_proteina_filter_reuses_generated_workspace_without_a_gpu(
    variant: str,
    config_stem: str,
) -> None:
    request = fixture("proteina-complexa", "positive-protein.json")
    request["parameters"]["variant"] = variant
    parameters = request["parameters"]
    result = proteina_complexa.compile_run(
        profile("proteina-complexa"),
        request,
        operation_id=f"op-{variant}-cpu-filter",
    )

    filter_invocation = result.invocations[1]
    common_overrides = (
        f"++run_name={parameters['run_name']}",
        f"++generation.task_name={parameters['target_id']}",
        f"++seed={parameters['seed']}",
    )
    assert filter_invocation.argv[3:] == (
        "complexa",
        "filter",
        "--verbose",
        proteina_complexa.VARIANTS[variant].config,
        *common_overrides,
        (f"++root_path=./inference/{config_stem}_{parameters['target_id']}_{parameters['run_name']}"),
    )
    assert result.controller_plan.stage("filter").resource_class is ResourceClass.CPU

    for invocation in (result.invocations[0], *result.invocations[2:]):
        assert invocation.argv[6:9] == common_overrides
        assert "--verbose" not in invocation.argv
        assert not any(argument.startswith("++root_path=") for argument in invocation.argv)


def test_proteina_runtime_dependencies_are_variant_specific_and_operation_isolated() -> None:
    cases = (
        ("protein-target", "complexa-protein", "alphafold2-params"),
        ("ligand-target", "complexa-ligand", "rosettafold3-checkpoint"),
        ("ame", "complexa-ame", "rosettafold3-checkpoint"),
    )
    for variant, weight_artifact, folding_artifact in cases:
        request = fixture("proteina-complexa", "positive-protein.json")
        request["parameters"]["variant"] = variant
        first = proteina_complexa.compile_run(profile("proteina-complexa"), request, operation_id=f"op-{variant}-one")
        second = proteina_complexa.compile_run(profile("proteina-complexa"), request, operation_id=f"op-{variant}-two")
        assert first.invocations[0].working_directory != second.invocations[0].working_directory
        assert len({item.working_directory for item in first.invocations}) == 1
        assert first.required_model_artifacts == (weight_artifact, folding_artifact)
        assert first.invocations[0].runtime_artifacts[0] == weight_artifact
        assert first.invocations[2].runtime_artifacts == (folding_artifact,)
        argv = "\0".join(argument for invocation in first.invocations for argument in invocation.argv)
        assert "/mnt/fs2-scientific/models" not in argv
        assert "rf3.ckpt" not in argv
        assert "esm2-checkpoints" not in argv


def test_boltzgen_campaign_workspaces_are_operation_isolated() -> None:
    request = fixture("boltzgen", "positive-design.json")
    first = boltzgen.compile_run(profile("boltzgen"), request, operation_id="op-boltz-isolation-one")
    second = boltzgen.compile_run(profile("boltzgen"), request, operation_id="op-boltz-isolation-two")
    first_workspaces = {item.working_directory for item in first.invocations}
    second_workspaces = {item.working_directory for item in second.invocations}
    assert first_workspaces.isdisjoint(second_workspaces)
    assert len(first_workspaces) == len(request["parameters"]["batches"])


def test_boltzgen_verified_manifest_entry_drives_campaign_materialization() -> None:
    request = boltzgen_manifest_request()
    campaign = boltzgen_verified_input()
    result = compile_adapter_run(
        "boltzgen",
        profile("boltzgen"),
        request,
        operation_id="op-boltz-verified-input",
        input_artifacts=(campaign,),
    )

    configure = [item for item in result.invocations if item.stage_id == "configure"]
    assert configure
    for invocation in configure:
        assert invocation.consumes == ("campaign-input",)
        assert len(invocation.materializations) == 1
        materialization = invocation.materializations[0]
        assert materialization.artifact_id == "campaign-input"
        assert materialization.mode is MaterializationMode.BOLTZGEN_INPUT
        assert materialization.compression == "gzip"

    manifest_id = UUID(request["input_manifest"]["artifact_id"])
    verified_manifest = VerifiedInputManifest(
        manifest_id="boltzgen-pdl1-inputs",
        manifest_artifact_id=manifest_id,
        manifest_digest="sha256:" + request["input_manifest"]["sha256"],
        entries=(campaign,),
    )
    controller_source = verified_manifest.artifact(configure[0].materializations[0].artifact_id)
    assert controller_source.artifact_id == campaign.artifact_id
    assert controller_source.artifact_id != manifest_id

    with pytest.raises(ScientificAdapterError, match="verified input_artifacts"):
        boltzgen.compile_run(
            profile("boltzgen"),
            request,
            operation_id="op-boltz-unverified-input",
        )
    with pytest.raises(ScientificAdapterError, match="exactly one campaign-input"):
        compile_adapter_run(
            "boltzgen",
            profile("boltzgen"),
            request,
            operation_id="op-boltz-missing-input",
            input_artifacts=(),
        )

    invalid_entries = (
        boltzgen_verified_input(logical_artifact_id="wrong-input"),
        boltzgen_verified_input(semantic_type="wrong-input/v1"),
        boltzgen_verified_input(media_type="application/json"),
        boltzgen_verified_input(compression="none"),
        boltzgen_verified_input(size_bytes=boltzgen.MAX_INPUT_BYTES + 1),
        boltzgen_verified_input(
            artifact_id=UUID(request["input_manifest"]["artifact_id"]),
        ),
    )
    for index, invalid in enumerate(invalid_entries):
        with pytest.raises(ScientificAdapterError):
            compile_adapter_run(
                "boltzgen",
                profile("boltzgen"),
                request,
                operation_id=f"op-boltz-invalid-input-{index}",
                input_artifacts=(invalid,),
            )

    wrong_manifest = copy.deepcopy(request)
    wrong_manifest["input_manifest"]["compression"] = "gzip"
    with pytest.raises(ScientificAdapterError, match="manifest must not be compressed"):
        compile_adapter_run(
            "boltzgen",
            profile("boltzgen"),
            wrong_manifest,
            operation_id="op-boltz-compressed-manifest",
            input_artifacts=(campaign,),
        )


def test_proteina_verified_manifest_entry_drives_target_bundle_materialization() -> None:
    request = proteina_manifest_request()
    target_bundle = proteina_verified_input()
    result = compile_adapter_run(
        "proteina-complexa",
        profile("proteina-complexa"),
        request,
        operation_id="op-proteina-verified-input",
        input_artifacts=(target_bundle,),
    )

    generate = result.invocations[0]
    assert generate.stage_id == "generate"
    assert generate.consumes == (proteina_complexa.TARGET_BUNDLE_ID,)
    assert len(generate.materializations) == 1
    materialization = generate.materializations[0]
    assert materialization.artifact_id == proteina_complexa.TARGET_BUNDLE_ID
    assert materialization.mode is MaterializationMode.EXTRACT_TAR
    assert materialization.compression == "gzip"
    assert all(invocation.consumes != (request["input_manifest"]["artifact_id"],) for invocation in result.invocations)

    manifest_id = UUID(request["input_manifest"]["artifact_id"])
    verified_manifest = VerifiedInputManifest(
        manifest_id="proteina-complexa-pdl1-inputs",
        manifest_artifact_id=manifest_id,
        manifest_digest="sha256:" + request["input_manifest"]["sha256"],
        entries=(target_bundle,),
    )
    controller_source = verified_manifest.artifact(materialization.artifact_id)
    assert controller_source.artifact_id == target_bundle.artifact_id
    assert controller_source.artifact_id != manifest_id

    with pytest.raises(ScientificAdapterError, match="verified input_artifacts"):
        proteina_complexa.compile_run(
            profile("proteina-complexa"),
            request,
            operation_id="op-proteina-unverified-input",
        )
    with pytest.raises(ScientificAdapterError, match="exactly one target-bundle"):
        compile_adapter_run(
            "proteina-complexa",
            profile("proteina-complexa"),
            request,
            operation_id="op-proteina-missing-input",
            input_artifacts=(),
        )

    invalid_entries = (
        proteina_verified_input(logical_artifact_id="wrong-input"),
        proteina_verified_input(semantic_type="wrong-input/v1"),
        proteina_verified_input(media_type="application/json"),
        proteina_verified_input(compression="none"),
        proteina_verified_input(size_bytes=proteina_complexa.MAX_INPUT_BYTES + 1),
        proteina_verified_input(
            artifact_id=UUID(request["input_manifest"]["artifact_id"]),
        ),
    )
    for index, invalid in enumerate(invalid_entries):
        with pytest.raises(ScientificAdapterError):
            compile_adapter_run(
                "proteina-complexa",
                profile("proteina-complexa"),
                request,
                operation_id=f"op-proteina-invalid-input-{index}",
                input_artifacts=(invalid,),
            )

    wrong_manifest = copy.deepcopy(request)
    wrong_manifest["input_manifest"]["compression"] = "gzip"
    with pytest.raises(ScientificAdapterError, match="manifest must not be compressed"):
        compile_adapter_run(
            "proteina-complexa",
            profile("proteina-complexa"),
            wrong_manifest,
            operation_id="op-proteina-compressed-manifest",
            input_artifacts=(target_bundle,),
        )


def test_boltzgen_uses_one_gpu_shard_commands_and_every_exact_checkpoint_identity() -> None:
    request = fixture("boltzgen", "positive-design.json")
    result = boltzgen.compile_run(profile("boltzgen"), request, operation_id="op-boltz-02")
    assert len(result.invocations) == 14
    assert {invocation.stage_id for invocation in result.invocations} == {
        "configure",
        "design",
        "inverse-folding",
        "folding",
        "design-folding",
        "analysis",
        "filtering",
    }
    assert tuple(stage.stage_id for stage in result.controller_plan.stages) == (
        "configure",
        "design",
        "inverse-folding",
        "folding",
        "design-folding",
        "analysis",
        "filtering",
    )
    assert result.controller_plan.stage("configure").shards == ("pdl1-a", "pdl1-b")
    configure = [item for item in result.invocations if item.stage_id == "configure"]
    for invocation, designs in zip(configure, (10, 12), strict=True):
        assert invocation.argv[:3] == (
            "python",
            f"{invocation.working_directory}/.fs2/stage-runner.py",
            "--",
        )
        argv = invocation.argv[3:]
        assert argv[:2] == ("boltzgen", "configure")
        assert argv[argv.index("--devices") + 1] == "1"
        assert argv[argv.index("--num_designs") + 1] == str(designs)
        assert argv[argv.index("--protocol") + 1] == "protein-anything"
        assert argv[argv.index("--design_checkpoints") + 1].endswith("boltzgen1_diverse.ckpt")
        assert argv[argv.index("--design_checkpoints") + 2].endswith("boltzgen1_adherence.ckpt")
        assert argv[argv.index("--inverse_fold_checkpoint") + 1].endswith("boltzgen1_ifold.ckpt")
        assert argv[argv.index("--folding_checkpoint") + 1].endswith("boltz2_conf_final.ckpt")
        assert argv[argv.index("--affinity_checkpoint") + 1].endswith("boltz2_aff.ckpt")
        assert argv[argv.index("--moldir") + 1] == "/opt/fs2/artifacts/boltzgen-inference-molecules"
        assert invocation.runtime_artifacts == ("boltzgen-checkpoints", "boltzgen-inference-molecules")
        assert invocation.materializations[0].yaml_name == f"design-specs/{invocation.shard_id}.yaml"
        environment = dict(invocation.environment)
        assert environment["FS2_BOLTZGEN_NUM_DESIGNS"] == str(designs)
        assert environment["FS2_BOLTZGEN_BUDGET"] == "2"
        assert len(environment["FS2_BOLTZGEN_REQUEST_SHA256"]) == 64
        assert invocation.handoff_name == boltzgen.STAGE_HANDOFF_NAME
        assert invocation.max_output_artifacts == 1
        assert invocation.max_output_bytes == boltzgen.MAX_STAGE_HANDOFF_BYTES
        assert "--models_token" not in argv
        assert all("huggingface:" not in value and "https://" not in value for value in argv)
    assert boltzgen.CHECKPOINTS == (
        "boltzgen1_diverse.ckpt",
        "boltzgen1_adherence.ckpt",
        "boltzgen1_ifold.ckpt",
        "boltz2_conf_final.ckpt",
        "boltz2_aff.ckpt",
        "boltzgen1_structuretrained_small.ckpt",
    )
    assert result.required_model_artifacts == ("boltzgen-checkpoints", "boltzgen-inference-molecules")
    reuse = boltzgen.compile_run(
        profile("boltzgen"), fixture("boltzgen", "positive-reuse.json"), operation_id="op-boltz-reuse"
    )
    assert "--reuse" in reuse.invocations[0].argv
    assert reuse.controller_plan.stage("affinity").resource_class is ResourceClass.GPU
    assert reuse.controller_plan.stage("analysis").resource_class is ResourceClass.CPU
    assert reuse.controller_plan.stage("configure").resource_class is ResourceClass.GPU
    assert all(
        item.runtime_artifacts == (boltzgen.MOLECULES_ARTIFACT_ID,)
        for item in reuse.invocations
        if item.stage_id in {"analysis", "filtering"}
    )
    assert all(
        tuple(mount.artifact_id for mount in item.runtime_mounts) == (boltzgen.MOLECULES_ARTIFACT_ID,)
        for item in reuse.invocations
        if item.stage_id in {"analysis", "filtering"}
    )
    assert all(
        item.runtime_trees == (boltzgen.molecules_tree_binding(),)
        for item in reuse.invocations
        if item.stage_id in {"analysis", "filtering"}
    )
    assert all(item.materializations for item in reuse.invocations)
    for item in reuse.invocations[1:]:
        assert item.argv[:3] == (
            "python",
            f"{item.working_directory}/.fs2/stage-runner.py",
            "--",
        )
        assert item.argv[3:5] == ("boltzgen", "execute")
        assert "--reuse" not in item.argv
    for item in reuse.invocations:
        if item.stage_id == "filtering":
            assert item.handoff_name is None
            assert item.max_output_artifacts == int(dict(item.environment)["FS2_BOLTZGEN_BUDGET"]) + 1
        else:
            assert item.handoff_name == boltzgen.STAGE_HANDOFF_NAME
            assert item.max_output_artifacts == 1


def test_boltzgen_public_execution_binding_accepts_a_canonical_optional_stage_subset() -> None:
    """A protocol may omit an optional catalog stage without changing stage order."""

    catalog = ScientificProfileCatalog.load(SOLUTION_ROOT / "catalog/runtime")
    renderer = FileScientificManifestRenderer(
        path=SOLUTION_ROOT / "catalog/runtime/contracts/scientific-execution-map.json",
        profiles=catalog,
    )
    request = boltzgen_manifest_request()

    plan = renderer.plan(
        catalog.get("boltzgen"),
        request,
        access_context=ArtifactAccessContext(profile="public", receipt_digest=None),
        input_artifacts=(boltzgen_verified_input(),),
    )

    assert tuple(stage.stage_id for stage in plan.controller_plan.stages) == (
        "configure",
        "design",
        "inverse-folding",
        "folding",
        "design-folding",
        "analysis",
        "filtering",
    )
    assert "affinity" not in {stage.stage_id for stage in plan.controller_plan.stages}


def test_public_requests_reject_duplicate_unknown_path_and_execution_fields() -> None:
    duplicate = b'{"schema":"a","operation":"one","operation":"two"}'
    with pytest.raises(ScientificAdapterError, match="duplicate JSON field: operation"):
        load_json_request(duplicate)
    with pytest.raises(ScientificAdapterError, match="duplicate JSON field: operation"):
        compile_adapter_run("proteina-complexa", profile("proteina-complexa"), duplicate, operation_id="op-bad-json")

    request = fixture("proteina-complexa", "positive-protein.json")
    for mutation in (
        lambda value: value.update({"command": ["sh", "-c", "id"]}),
        lambda value: value["parameters"].update({"runtime_image": "attacker.invalid/image"}),
        lambda value: value["parameters"].update({"target_id": "/etc/passwd"}),
        lambda value: value["input_manifest"].update({"artifact_id": "../escape"}),
        lambda value: value["input_manifest"].update({"local_path": "/absolute/input"}),
    ):
        candidate = copy.deepcopy(request)
        mutation(candidate)
        with pytest.raises(ScientificAdapterError):
            proteina_complexa.compile_run(profile("proteina-complexa"), candidate, operation_id="op-bad-01")


def test_identity_and_all_numeric_batch_bounds_fail_closed() -> None:
    request = fixture("boltzgen", "positive-design.json")
    for field, value in (
        ("num_designs", True),
        ("num_designs", boltzgen.MAX_DESIGNS_PER_BATCH + 1),
        ("budget", boltzgen.MAX_BUDGET_PER_BATCH + 1),
    ):
        candidate = copy.deepcopy(request)
        candidate["parameters"]["batches"][0][field] = value
        with pytest.raises(ScientificAdapterError):
            boltzgen.compile_run(profile("boltzgen"), candidate, operation_id="op-bad-02")
    accepted_limit = copy.deepcopy(request)
    accepted_limit["parameters"]["batches"][0]["num_designs"] = 20
    accepted_limit["parameters"]["batches"][0]["budget"] = 3
    accepted_limit["parameters"]["batches"][1]["num_designs"] = 4
    boltzgen.compile_run(profile("boltzgen"), accepted_limit, operation_id="op-bounded-limit")
    too_many_batches = copy.deepcopy(request)
    too_many_batches["parameters"]["batches"].append(
        {"shard_id": "pdl1-c", "num_designs": 1, "budget": 1, "reuse_completed": False}
    )
    with pytest.raises(ScientificAdapterError, match="1..2"):
        boltzgen.compile_run(profile("boltzgen"), too_many_batches, operation_id="op-too-many-batches")
    too_many_total = copy.deepcopy(request)
    too_many_total["parameters"]["batches"][0]["num_designs"] = 13
    with pytest.raises(ScientificAdapterError, match="total design bound"):
        boltzgen.compile_run(profile("boltzgen"), too_many_total, operation_id="op-too-many-designs")
    duplicate = copy.deepcopy(request)
    duplicate["parameters"]["batches"][1]["shard_id"] = "pdl1-a"
    with pytest.raises(ScientificAdapterError, match="unique"):
        boltzgen.compile_run(profile("boltzgen"), duplicate, operation_id="op-bad-03")
    wrong_profile = copy.deepcopy(dict(profile("boltzgen")))
    wrong_profile["source"]["revision"] = "f" * 40
    with pytest.raises(ScientificAdapterError, match="source identity"):
        boltzgen.compile_run(wrong_profile, request, operation_id="op-bad-04")
    with pytest.raises(ScientificAdapterError, match="variant_id"):
        compile_adapter_run(
            "boltzgen",
            profile("boltzgen"),
            request,
            operation_id="op-bad-variant",
            variant_id="upstream-wrong",
        )


def test_public_payloads_contain_only_logical_artifact_identity() -> None:
    for model_id, name in (
        ("proteina-complexa", "positive-protein.json"),
        ("boltzgen", "positive-design.json"),
        ("boltzgen", "positive-reuse.json"),
    ):
        request = fixture(model_id, name)
        serialized = json.dumps(request, sort_keys=True).lower()
        assert "http://" not in serialized
        assert "https://" not in serialized
        assert "artifact://" not in serialized
        assert "/mnt/" not in serialized
        assert "../" not in serialized
        assert all(marker not in serialized for marker in ("token", "secret", "password", "local_path", "url"))


def test_proteina_semantics_bind_request_structure_and_finite_metrics() -> None:
    request = fixture("proteina-complexa", "positive-protein.json")
    structure = pdb_bytes()
    results = b"id_gen,_res_pLDDT_self,_res_i_pae_self,_res_scRMSD_self\ndesign-1,0.81,4.2,1.1\n"
    manifest, blobs = output_manifest(
        [
            ("design-1-structure", "protein-complex-structure/v1", "proteina.structure.1", structure),
            ("results.1", "proteina-complexa-results-csv/v1", "proteina.results.1", results),
        ]
    )
    result = proteina_complexa.validate_output(request, manifest, artifact_loader=blobs.__getitem__)
    assert result["status"] == "passed"
    assert result["design_count"] == 1

    tampered = dict(blobs)
    tampered["proteina.structure.1"] = b"tampered"
    with pytest.raises(ScientificAdapterError, match="do not match"):
        proteina_complexa.validate_output(request, manifest, artifact_loader=tampered.__getitem__)
    nonfinite = results.replace(b"0.81", b"NaN")
    invalid_manifest, invalid_blobs = output_manifest(
        [
            ("design-1-structure", "protein-complex-structure/v1", "proteina.structure.1", structure),
            ("results.1", "proteina-complexa-results-csv/v1", "proteina.results.1", nonfinite),
        ]
    )
    with pytest.raises(ScientificAdapterError, match="finite"):
        proteina_complexa.validate_output(request, invalid_manifest, artifact_loader=invalid_blobs.__getitem__)


@pytest.mark.parametrize(
    ("metric_name", "metric_value"),
    (
        ("self_complex_pLDDT", "0.91"),
        ("self_complex_pAE", "4.2"),
        ("self_complex_i_pAE", "3.8"),
        ("self_binder_scRMSD", "1.1"),
        ("self_binder_scRMSD_ca", "1.1"),
        ("self_binder_scRMSD_bb3", "1.2"),
        ("self_binder_scRMSD_bb3o", "1.3"),
        ("self_binder_scRMSD_allatom", "1.4"),
        ("self_complex_scRMSD", "1.5"),
        ("self_complex_scRMSD_ca", "1.5"),
    ),
)
def test_proteina_semantics_accept_pinned_upstream_scalar_metrics(
    metric_name: str,
    metric_value: str,
) -> None:
    request = fixture("proteina-complexa", "positive-protein.json")
    results = f"id_gen,{metric_name}\ndesign-1,{metric_value}\n".encode()
    manifest, blobs = proteina_output_manifest(results)

    validation = proteina_complexa.validate_output(request, manifest, artifact_loader=blobs.__getitem__)

    assert validation["status"] == "passed"
    assert validation["design_count"] == 1


@pytest.mark.parametrize(
    "header",
    (
        "self_complex_pTM",
        "self_complex_i_pTM",
        "mpnn_complex_pLDDT",
        "self_complex_i_pAE_all",
        "self_binder_scRMSD_all",
        "self_binder_scRMSD_unknown",
    ),
)
def test_proteina_semantics_reject_unknown_or_array_only_metrics(header: str) -> None:
    request = fixture("proteina-complexa", "positive-protein.json")
    results = f'id_gen,{header}\ndesign-1,"[0.8, 0.9]"\n'.encode()
    manifest, blobs = proteina_output_manifest(results)

    with pytest.raises(ScientificAdapterError, match="no recognized scientific metrics"):
        proteina_complexa.validate_output(request, manifest, artifact_loader=blobs.__getitem__)


@pytest.mark.parametrize(
    ("metric_name", "metric_value"),
    (
        ("self_complex_pLDDT", "1.01"),
        ("self_complex_pAE", "100.01"),
        ("self_complex_i_pAE", "NaN"),
        ("self_binder_scRMSD_ca", "-0.01"),
    ),
)
def test_proteina_semantics_bound_current_scalar_metrics(metric_name: str, metric_value: str) -> None:
    request = fixture("proteina-complexa", "positive-protein.json")
    results = f"id_gen,{metric_name}\ndesign-1,{metric_value}\n".encode()
    manifest, blobs = proteina_output_manifest(results)

    with pytest.raises(ScientificAdapterError, match="must be finite"):
        proteina_complexa.validate_output(request, manifest, artifact_loader=blobs.__getitem__)


def test_boltzgen_semantics_enforce_identity_budget_and_degenerate_sequence_gate() -> None:
    request = fixture("boltzgen", "positive-design.json")
    structure = mmcif_bytes()
    items: list[tuple[str, str, str, bytes]] = []
    file_names = ("one.cif", "two.cif", "three.cif", "four.cif")
    shard_ids = ("pdl1-a", "pdl1-a", "pdl1-b", "pdl1-b")
    for index, (shard_id, file_name) in enumerate(zip(shard_ids, file_names, strict=True), start=1):
        name = f"structure.{shard_id}.{hashlib.sha256(json.dumps(file_name).encode()).hexdigest()}"
        items.append((name, "protein-complex-structure/v1", f"boltz.structure.{index}", structure))
    header = b"id,file_name,designed_chain_sequence,design_to_target_iptm,designfolding-filter_rmsd\n"
    ranking_a = header + (
        b"design-1,one.cif,ACDEFGHIKLMNPQRSTVWY,0.67,1.2\ndesign-2,two.cif,ACDEFGHIKLMNPQRSTVWY,0.69,1.1\n"
    )
    ranking_b = header + (
        b"design-3,three.cif,ACDEFGHIKLMNPQRSTVWY,0.71,1.0\ndesign-4,four.cif,ACDEFGHIKLMNPQRSTVWY,0.73,0.9\n"
    )
    items.extend(
        (
            ("ranking.pdl1-a", "boltzgen-ranking-csv/v1", "boltz.ranking.a", ranking_a),
            ("ranking.pdl1-b", "boltzgen-ranking-csv/v1", "boltz.ranking.b", ranking_b),
        )
    )
    manifest, blobs = output_manifest(items)
    result = boltzgen.validate_output(request, manifest, artifact_loader=blobs.__getitem__)
    assert result["status"] == "passed"
    assert result["design_count"] == 4

    biased = ranking_a.replace(b"ACDEFGHIKLMNPQRSTVWY", b"AAAAAAAAAACDEFGHIKLM", 1)
    biased_items = [*items[:-2], ("ranking.pdl1-a", "boltzgen-ranking-csv/v1", "boltz.ranking.a", biased), items[-1]]
    biased_manifest, biased_blobs = output_manifest(biased_items)
    with pytest.raises(ScientificAdapterError, match="composition-bias"):
        boltzgen.validate_output(request, biased_manifest, artifact_loader=biased_blobs.__getitem__)

    nonfinite = ranking_a.replace(b"0.67", b"NaN")
    invalid_items = [
        *items[:-2],
        ("ranking.pdl1-a", "boltzgen-ranking-csv/v1", "boltz.ranking.a", nonfinite),
        items[-1],
    ]
    invalid_manifest, invalid_blobs = output_manifest(invalid_items)
    with pytest.raises(ScientificAdapterError, match="finite"):
        boltzgen.validate_output(request, invalid_manifest, artifact_loader=invalid_blobs.__getitem__)

    unbound = ranking_a.replace(b"one.cif", b"unrelated.cif")
    unbound_items = [
        *items[:-2],
        ("ranking.pdl1-a", "boltzgen-ranking-csv/v1", "boltz.ranking.a", unbound),
        items[-1],
    ]
    unbound_manifest, unbound_blobs = output_manifest(unbound_items)
    with pytest.raises(ScientificAdapterError, match="do not match emitted structure artifacts"):
        boltzgen.validate_output(request, unbound_manifest, artifact_loader=unbound_blobs.__getitem__)


def tar_bytes(files: Mapping[str, bytes], *, symlink: tuple[str, str] | None = None) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        if symlink is not None:
            info = tarfile.TarInfo(symlink[0])
            info.type = tarfile.SYMTYPE
            info.linkname = symlink[1]
            archive.addfile(info)
    return stream.getvalue()


def test_boltzgen_archive_materializer_rewrites_nested_paths_and_rejects_escape(tmp_path: Path) -> None:
    yaml_content = b"entities:\n- protein:\n    path: structures/target.cif\n    chain: A\n"
    destination = tmp_path / "localized"
    rewritten = materialize_boltzgen_input(
        tar_bytes(
            {
                "design-specs/pdl1-a.yaml": yaml_content,
                "structures/target.cif": mmcif_bytes(),
            }
        ),
        destination,
        yaml_name="design-specs/pdl1-a.yaml",
        compression=None,
    )
    text = rewritten.read_text(encoding="utf-8")
    assert str((destination / "inputs/structures/target.cif").resolve()) in text
    assert "../" not in text

    with pytest.raises(ScientificAdapterError, match="relative POSIX path"):
        materialize_boltzgen_input(
            tar_bytes({"../escape": b"bad"}),
            tmp_path / "escape",
            yaml_name="design.yaml",
            compression=None,
        )
    with pytest.raises(ScientificAdapterError, match="duplicate field path"):
        materialize_boltzgen_input(
            tar_bytes(
                {
                    "design.yaml": (b"entities:\n- protein:\n    path: target.cif\n    path: target.cif\n"),
                    "target.cif": mmcif_bytes(),
                }
            ),
            tmp_path / "duplicate-yaml",
            yaml_name="design.yaml",
            compression=None,
        )
    duplicate_tar = io.BytesIO()
    with tarfile.open(fileobj=duplicate_tar, mode="w") as archive:
        for content in (b"one", b"two"):
            info = tarfile.TarInfo("design.yaml")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    with pytest.raises(ScientificAdapterError, match="duplicate normalized paths"):
        materialize_boltzgen_input(
            duplicate_tar.getvalue(),
            tmp_path / "duplicate-archive",
            yaml_name="design.yaml",
            compression=None,
        )
    with pytest.raises(ScientificAdapterError, match="regular files"):
        materialize_boltzgen_input(
            tar_bytes({"design.yaml": b"entities: []\n"}, symlink=("target.cif", "/etc/passwd")),
            tmp_path / "link",
            yaml_name="design.yaml",
            compression=None,
        )
    with pytest.raises(ScientificAdapterError, match="relative POSIX path"):
        materialize_boltzgen_input(
            tar_bytes({"design.yaml": b"entities:\n- protein:\n    path: ../../etc/passwd\n"}),
            tmp_path / "yaml-escape",
            yaml_name="design.yaml",
            compression=None,
        )
    with pytest.raises(ScientificAdapterError, match="non-finite"):
        materialize_boltzgen_input(
            tar_bytes({"design.yaml": b"entities: []\nscore: .nan\n"}),
            tmp_path / "yaml-nan",
            yaml_name="design.yaml",
            compression=None,
        )


def test_collectors_consume_real_upstream_csv_and_structures_without_exposing_paths(tmp_path: Path) -> None:
    proteina_request = fixture("proteina-complexa", "positive-protein.json")
    proteina_root = tmp_path / "proteina"
    structure = proteina_root / "evaluation" / "design-1" / "design-1.pdb"
    structure.parent.mkdir(parents=True)
    structure.write_bytes(pdb_bytes())
    csv_path = proteina_root / "evaluation" / "RAW_binder_results_pipeline_combined.csv"
    csv_path.write_text(
        f"id_gen,pdb_path,_res_pLDDT_self,_res_i_pae_self,_res_scRMSD_self\ndesign-1,{structure},0.81,4.2,1.1\n",
        encoding="utf-8",
    )
    proteina = proteina_complexa.collect_output(proteina_request, proteina_root)
    assert b"pdb_path" not in next(blob for blob in proteina.blobs.values() if blob.startswith(b"id_gen"))
    assert str(tmp_path) not in json.dumps(proteina.manifest)
    assert (
        proteina_complexa.validate_output(
            proteina_request,
            proteina.manifest,
            artifact_loader=proteina.blobs.__getitem__,
        )["design_count"]
        == 1
    )

    boltz_request = fixture("boltzgen", "positive-design.json")
    workspaces: dict[str, Path] = {}
    for batch in boltz_request["parameters"]["batches"]:
        shard_id = batch["shard_id"]
        budget = batch["budget"]
        root = tmp_path / shard_id
        final = root / "final_ranked_designs"
        structures = final / f"final_{budget}_designs"
        structures.mkdir(parents=True)
        rows = ["id,file_name,designed_chain_sequence,design_to_target_iptm,designfolding-filter_rmsd"]
        for index in range(1, budget + 1):
            filename = f"rank{index}_{shard_id}.cif"
            (structures / filename).write_bytes(mmcif_bytes())
            rows.append(f"design-{index},{filename},ACDEFGHIKLMNPQRSTVWY,0.7,1.1")
        (final / f"final_designs_metrics_{budget}.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        workspaces[shard_id] = root
    boltz = boltzgen.collect_output(boltz_request, workspaces)
    assert str(tmp_path) not in json.dumps(boltz.manifest)
    assert (
        boltzgen.validate_output(
            boltz_request,
            boltz.manifest,
            artifact_loader=boltz.blobs.__getitem__,
        )["design_count"]
        == 4
    )


def test_proteina_collector_bounds_the_pinned_best_of_n_expansion(tmp_path: Path) -> None:
    request = fixture("proteina-complexa", "positive-protein.json")
    assert request["parameters"]["num_samples"] == 2
    root = tmp_path / "proteina-best-of-n"
    csv_path = root / "evaluation" / "RAW_binder_results_pipeline_combined.csv"
    rows = ["id_gen,pdb_path,_res_pLDDT_self,_res_i_pae_self,_res_scRMSD_self"]
    for index in range(4):
        structure = root / "evaluation" / f"design-{index}" / f"design-{index}.pdb"
        structure.parent.mkdir(parents=True)
        structure.write_bytes(pdb_bytes())
        rows.append(f"design-{index},{structure},0.81,4.2,1.1")
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    collected = proteina_complexa.collect_output(request, root)
    semantic = proteina_complexa.validate_output(
        request,
        collected.manifest,
        artifact_loader=collected.blobs.__getitem__,
    )
    assert semantic["design_count"] == 4

    fifth = root / "evaluation" / "design-4" / "design-4.pdb"
    fifth.parent.mkdir(parents=True)
    fifth.write_bytes(pdb_bytes())
    csv_path.write_text(
        "\n".join((*rows, f"design-4,{fifth},0.81,4.2,1.1")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ScientificAdapterError, match="exceeds the CSV row bound"):
        proteina_complexa.collect_output(request, root)


def test_proteina_companion_collects_only_runner_completed_handoffs_and_final_outputs(tmp_path: Path) -> None:
    request = fixture("proteina-complexa", "positive-protein.json")
    plan = proteina_complexa.compile_run(
        profile("proteina-complexa"),
        request,
        operation_id="op-proteina-companion",
    )

    expected_by_stage = {
        "generate": {"assets/target_data/target.pdb", "inference/run/sample.pdb"},
        "filter": {"assets/target_data/target.pdb", "inference/run/sample.pdb"},
        "evaluate": {
            "assets/target_data/target.pdb",
            "inference/run/sample.pdb",
            "evaluation_results/run/result.csv",
        },
    }
    for stage_id, expected_files in expected_by_stage.items():
        intermediate_workspace = tmp_path / stage_id
        for relative_path, content in (
            ("assets/target_data/target.pdb", pdb_bytes()),
            ("inference/run/sample.pdb", pdb_bytes()),
        ):
            path = intermediate_workspace / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        if stage_id == "evaluate":
            result = intermediate_workspace / "evaluation_results" / "run" / "result.csv"
            result.parent.mkdir(parents=True)
            result.write_bytes(b"id,score\ndesign-1,0.81\n")
        # Successful upstream filter runs create this empty Hydra file. It is
        # not downstream scientific state and must neither make a handoff fail
        # nor leak into the next stage.
        incidental_log = intermediate_workspace / "logs" / "hydra_outputs" / "filter.log"
        incidental_log.parent.mkdir(parents=True)
        incidental_log.touch()
        intermediate = plan.invocation(stage_id, "main")
        completion_sha256 = publish_stage_completion(intermediate, intermediate_workspace)
        handoff = proteina_complexa.collect_companion_output(intermediate, intermediate_workspace)
        assert tuple(item.name for item in handoff.artifacts) == (proteina_complexa.STAGE_HANDOFF_NAME,)
        assert handoff.artifacts[0].semantic_type == proteina_complexa.STAGE_HANDOFF_SEMANTIC_TYPE
        assert handoff.artifacts[0].compression == "zstd"
        assert handoff.validation["completion_marker_sha256"] == completion_sha256
        assert handoff.validation["status"] == "passed"
        raw_handoff = zstandard.ZstdDecompressor().decompress(
            handoff.artifacts[0].path.read_bytes(),
            max_output_size=proteina_complexa.MAX_STAGE_HANDOFF_BYTES,
        )
        with tarfile.open(fileobj=io.BytesIO(raw_handoff), mode="r:") as archive:
            members = {member.name.rstrip("/"): member for member in archive.getmembers()}
            observed_files = {name for name, member in members.items() if member.isfile()}
            assert observed_files == expected_files
            for name in expected_files:
                source = archive.extractfile(members[name])
                assert source is not None and source.read()
            assert all(not name.startswith("logs") for name in members)

    terminal_workspace = tmp_path / "analyze"
    structure = terminal_workspace / "evaluation" / "design-1" / "design-1.pdb"
    structure.parent.mkdir(parents=True)
    structure.write_bytes(pdb_bytes())
    csv_path = terminal_workspace / "evaluation" / "RAW_binder_results_pipeline_combined.csv"
    csv_path.write_text(
        f"id_gen,pdb_path,_res_pLDDT_self,_res_i_pae_self,_res_scRMSD_self\ndesign-1,{structure},0.81,4.2,1.1\n",
        encoding="utf-8",
    )
    terminal = plan.invocation("analyze", "main")
    terminal_completion = publish_stage_completion(terminal, terminal_workspace)
    collected = proteina_complexa.collect_companion_output(terminal, terminal_workspace)
    assert collected.validation["completion_marker_sha256"] == terminal_completion
    assert collected.validation["design_count"] == 1
    assert collected.validation["status"] == "passed"
    assert {item.semantic_type for item in collected.artifacts} == {
        "proteina-complexa-results-csv/v1",
        "protein-complex-structure/v1",
    }
    result_csv = next(
        item.path.read_bytes()
        for item in collected.artifacts
        if item.semantic_type == "proteina-complexa-results-csv/v1"
    )
    assert b"pdb_path" not in result_csv
    assert str(tmp_path).encode() not in result_csv

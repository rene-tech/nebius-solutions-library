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
from uuid import uuid4

import pytest
from scientific_batch_fakes import FakeScientificBatchCluster, FakeScientificBatchRepository

import fs2_serve.scientific_batch.models as scientific_models
from fs2_serve.scientific_batch import (
    CheckpointMode,
    PreemptionMode,
    ResourceClass,
    SchedulingSnapshot,
    ScientificAdapterError,
    ScientificBatchController,
    ScientificStagePlan,
    ServiceClass,
    StageSchedulingDecision,
    compile_adapter_run,
    load_json_request,
    profile_from_catalog,
)
from fs2_serve.scientific_batch.adapters import boltzgen, proteina_complexa
from fs2_serve.scientific_batch.adapters.common import runtime_recipe_sha256
from fs2_serve.scientific_batch.adapters.materialization import (
    HANDOFF_SCHEMA,
    materialize_boltzgen_input,
    materialize_stage_input,
    validate_relocatable_handoff,
)
from fs2_serve.scientific_batch.models import ArtifactMaterialization, MaterializationMode

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters"
PROFILE_PATH = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json"
SOURCE_QUALIFICATION_PATH = SOLUTION_ROOT / "models/cancer-immunotherapy/model-source-qualification.json"


def fixture(model_id: str, name: str) -> dict[str, Any]:
    return json.loads((ADAPTER_ROOT / model_id / "fixtures" / name).read_text(encoding="utf-8"))


def profile(model_id: str) -> Mapping[str, object]:
    return profile_from_catalog(json.loads(PROFILE_PATH.read_text(encoding="utf-8")), model_id)


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


def test_real_catalog_profiles_project_to_canonical_controller_types() -> None:
    proteina_request = fixture("proteina-complexa", "positive-protein.json")
    proteina = compile_adapter_run(
        "proteina-complexa", profile("proteina-complexa"), proteina_request, operation_id="op-proteina-01"
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

    boltz_request = fixture("boltzgen", "positive-design.json")
    boltz = compile_adapter_run("boltzgen", profile("boltzgen"), boltz_request, operation_id="op-boltz-01")
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
async def test_controller_admission_persists_adapter_identity_and_renders_exact_invocation() -> None:
    request = fixture("proteina-complexa", "positive-protein.json")
    operation_id = uuid4()
    execution = compile_adapter_run(
        "proteina-complexa",
        profile("proteina-complexa"),
        request,
        operation_id=str(operation_id),
        variant_id=proteina_complexa.VARIANT_ID,
    )
    captured_at = datetime(2026, 9, 2, tzinfo=UTC)
    scheduling = SchedulingSnapshot(
        policy_revision="sha256:" + "1" * 64,
        captured_at=captured_at,
        service_class=ServiceClass.CUSTOMER_BATCH,
        tenant_queue="cancer-immunotherapy",
        model_lane="protein-design",
        stages=tuple(
            StageSchedulingDecision(
                stage_id=stage.stage_id,
                resolved_cluster_queue="inference-accelerators",
                resolved_local_queue="scientific-batch",
                workload_priority_class="fs2-customer-batch",
                workload_priority_value=100,
                resolved_pool_preference=(
                    ("h100-1x", "h100-reserved-8x") if stage.resource_class is ResourceClass.GPU else ()
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
    admitted = await controller.admit_adapter_run(
        operation_id=operation_id,
        tenant_id="cancer-immunotherapy",
        model_id="proteina-complexa",
        variant_id=proteina_complexa.VARIANT_ID,
        profile=profile("proteina-complexa"),
        request=request,
        scheduling=scheduling,
    )
    assert admitted.execution_plan == execution
    assert (admitted.model_id, admitted.variant_id) == ("proteina-complexa", proteina_complexa.VARIANT_ID)
    await controller.reconcile_once()
    workload = cluster.apply_history[0]
    assert workload.invocation == execution.invocation("generate", "main")
    assert workload.invocation.argv[:2] == ("complexa", "generate")
    assert workload.invocation.runtime_artifacts == execution.invocation("generate", "main").runtime_artifacts
    assert workload.model_id == admitted.model_id
    assert workload.variant_id == admitted.variant_id
    assert repository.events[operation_id]
    assert all(event.draft.model_id == admitted.model_id for event in repository.events[operation_id])
    assert all(event.draft.variant_id == admitted.variant_id for event in repository.events[operation_id])


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


def test_catalog_recipe_hashes_cover_the_adapter_and_canonical_workload(tmp_path: Path) -> None:
    for model_id in ("proteina-complexa", "boltzgen"):
        candidate = profile(model_id)
        identity = candidate["execution_identity"]
        workload = candidate["workload"]
        runtime_hash = runtime_recipe_sha256(SOLUTION_ROOT, model_id)
        workload_bytes = json.dumps(workload, separators=(",", ":"), sort_keys=True).encode()
        workload_hash = hashlib.sha256(workload_bytes).hexdigest()
        assert identity["runtime_recipe_sha256"] == runtime_hash
        assert identity["workload_recipe_sha256"] == workload_hash
    with pytest.raises(ScientificAdapterError, match="runtime recipe input is missing"):
        runtime_recipe_sha256(tmp_path, "boltzgen")


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
            "complexa",
            stage_id,
            "/opt/fs2/source/configs/search_ligand_binder_local_pipeline.yaml",
        )
        assert invocation.argv[0] not in {"sh", "bash"}
        assert invocation.execution_working_directory == "/opt/fs2/source"
        assert invocation.consumes == (previous,)
        assert invocation.produces.startswith("run.")
        previous = invocation.produces
        assert invocation.materializations[0].artifact_id == invocation.consumes[0]
        assert invocation.materializations[0].destination.startswith("/mnt/fs2-scientific/work/proteina-complexa/")
        assert invocation.materializations[0].destination.endswith("/main")
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


def test_boltzgen_uses_one_gpu_shard_commands_and_every_exact_checkpoint_identity() -> None:
    request = fixture("boltzgen", "positive-design.json")
    result = boltzgen.compile_run(profile("boltzgen"), request, operation_id="op-boltz-02")
    assert len(result.invocations) == 14
    assert result.controller_plan.stage("configure").shards == ("pdl1-a", "pdl1-b")
    configure = [item for item in result.invocations if item.stage_id == "configure"]
    for invocation, designs in zip(configure, (10, 12), strict=True):
        argv = invocation.argv
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
        assert argv[argv.index("--verbose")] == "--verbose"
        assert all(not value.startswith("--") for value in argv[argv.index("--steps") + 1 : argv.index("--verbose")])
        assert invocation.materializations[0].yaml_name == f"design-specs/{invocation.shard_id}.yaml"
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
    assert all(item.runtime_artifacts == () for item in reuse.invocations if item.stage_id in {"analysis", "filtering"})
    assert all(item.materializations for item in reuse.invocations)
    for item in reuse.invocations[1:]:
        assert item.argv[:2] == ("boltzgen", "execute")
        assert "--reuse" not in item.argv


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


def test_cross_stage_handoff_is_bound_to_logical_identity_after_relocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zstandard

    monkeypatch.setattr(scientific_models, "_CONTROLLER_MOUNT_ROOT", tmp_path)
    processed = b'{"name":"prepared"}\n'
    artifact_id = "run.operation.stage.main"
    marker = json.dumps(
        {
            "schema": HANDOFF_SCHEMA,
            "artifact_id": artifact_id,
            "member": "processed.json",
            "sha256": hashlib.sha256(processed).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    payload = tar_bytes({"processed.json": processed, "provenance.json": marker})
    compressed = zstandard.ZstdCompressor().compress(payload)
    for name in ("producer-layout", "gpu-relocated-layout"):
        destination = tmp_path / name
        specification = ArtifactMaterialization(
            artifact_id,
            str(destination),
            MaterializationMode.EXTRACT_TAR,
            compression="zstd",
            expected_members=("processed.json", "provenance.json"),
        )
        materialize_stage_input(compressed, specification)
        assert validate_relocatable_handoff(destination, artifact_id=artifact_id).read_bytes() == processed

    hostile_marker = json.dumps(
        {
            "schema": HANDOFF_SCHEMA,
            "artifact_id": artifact_id,
            "member": "/producer/processed.json",
            "sha256": hashlib.sha256(processed).hexdigest(),
        }
    ).encode()
    hostile = zstandard.ZstdCompressor().compress(
        tar_bytes({"processed.json": processed, "provenance.json": hostile_marker})
    )
    destination = tmp_path / "hostile"
    materialize_stage_input(
        hostile,
        ArtifactMaterialization(
            artifact_id,
            str(destination),
            MaterializationMode.EXTRACT_TAR,
            compression="zstd",
            expected_members=("processed.json", "provenance.json"),
        ),
    )
    with pytest.raises(ScientificAdapterError, match="provenance"):
        validate_relocatable_handoff(destination, artifact_id=artifact_id)


def test_identity_and_all_numeric_batch_bounds_fail_closed() -> None:
    request = fixture("boltzgen", "positive-design.json")
    for field, value in (("num_designs", True), ("num_designs", 10001), ("budget", 1001)):
        candidate = copy.deepcopy(request)
        candidate["parameters"]["batches"][0][field] = value
        with pytest.raises(ScientificAdapterError):
            boltzgen.compile_run(profile("boltzgen"), candidate, operation_id="op-bad-02")
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

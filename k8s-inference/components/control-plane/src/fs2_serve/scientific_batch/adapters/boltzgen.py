"""BoltzGen v0.3.2 adapter for independent one-GPU design campaigns."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import zstandard

from ..catalog_adapter import ScientificStageExpansion
from ..models import (
    AdapterExecutionPlan,
    ArtifactMaterialization,
    MaterializationMode,
    RuntimeArtifactMount,
    RuntimeTreeBinding,
    ScientificInputArtifact,
    StageInvocation,
)
from .common import (
    ArtifactLoader,
    ArtifactPointer,
    CollectedOutput,
    LoadedArtifact,
    PublicRunRequest,
    ScientificAdapterError,
    assert_profile_identity,
    bounded_int,
    build_execution_plan,
    canonical_digest,
    canonicalize_upstream_csv,
    collect_output_files,
    finite_number,
    load_output_manifest,
    logical_stage_artifact,
    model_file,
    model_root,
    parse_csv_artifact,
    parse_public_request,
    protein_sequence,
    run_workspace,
    safe_name,
    strict_object,
    structure_atom_count,
)

if TYPE_CHECKING:
    from . import CollectedStageOutput

MODEL_ID = "boltzgen"
VARIANT_ID = "upstream-v0-3-2"
SOURCE_REPOSITORY = "HannesStark/boltzgen"
SOURCE_REVISION = "31d9d9b9c72245b4ed6fe8742d6fbf4e1a3552a0"
RELEASE_TAG = "v0.3.2"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/boltzgen-parameters/v1"
WEIGHTS_ARTIFACT_ID = "boltzgen-checkpoints"
WEIGHTS_REVISION = "c1be29e1f82ffcc72264f64b993c43fb4e0d17f0"
MOLECULES_ARTIFACT_ID = "boltzgen-inference-molecules"
# The molecule dictionary is published as one zip and consumed as a directory.
# The archive digest is provenance only; the inventory digest is the identity of
# the extracted tree that `--moldir` actually reads. They are deliberately two
# separate constants and `RuntimeTreeBinding` rejects them being equal.
MOLECULES_ARCHIVE_SHA256 = "3d4f56ac4262e745bb3d09cfaa19099b1d01be208122d501667b952e45521e53"
MOLECULES_TREE_INVENTORY_SHA256 = "8ab1a59c72fc27a37dea61aab9408d7619f7a91fe32409f7a2b36fd59ebeecdc"
MOLECULES_TREE_ENTRY_COUNT = 45_227
CHECKPOINTS = (
    "boltzgen1_diverse.ckpt",
    "boltzgen1_adherence.ckpt",
    "boltzgen1_ifold.ckpt",
    "boltz2_conf_final.ckpt",
    "boltz2_aff.ckpt",
    "boltzgen1_structuretrained_small.ckpt",
)
PROTOCOLS = frozenset(
    {
        "protein-anything",
        "peptide-anything",
        "protein-small_molecule",
        "nanobody-anything",
        "antibody-anything",
        "protein-redesign",
    }
)
# Keep the live, qualified v0.3.2 path inside the current bounded in-memory
# handoff implementation.  The accepted fixtures exercise a 20-design shard,
# a two-shard 22-design campaign, and budget 3.  Cap the public shape just
# above that qualified envelope; larger campaigns remain closed until the
# streaming handoff successor is qualified.
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024 * 1024
# The current companion safely buffers and extracts at most 256 MiB.  Keep
# producer content below that after tar framing, and fail closed instead of
# producing a handoff that the next stage cannot consume.  Larger campaigns
# need the separately tracked streaming materializer before these bounds move.
MAX_STAGE_HANDOFF_BYTES = 256 * 1024 * 1024
MAX_STAGE_HANDOFF_CONTENT_BYTES = 240 * 1024 * 1024
MAX_STAGE_HANDOFF_MEMBERS = 4_096
STAGE_HANDOFF_NAME = "stage-handoff"
STAGE_HANDOFF_SEMANTIC_TYPE = "boltzgen-workspace-handoff-tar/v1"
STAGE_HANDOFF_MEDIA_TYPE = "application/octet-stream"
STAGE_HANDOFF_COMPRESSION = "zstd"
FINAL_RANKING_MEDIA_TYPE = "text/csv"
FINAL_STRUCTURE_MEDIA_TYPE = "chemical/x-mmcif"
STAGE_RUNNER_RELATIVE_PATH = ".fs2/stage-runner.py"
STAGE_COMPLETION_RELATIVE_PATH = ".fs2/stage-complete.json"
STAGE_COMPLETION_SCHEMA = "fs2-serve.nebius.ai/scientific-stage-completion/v1"
# Public localization generations are group-readable from the reference-data
# host plane.  The renderer refuses a hostPath projection unless the frozen
# invocation carries that exact published group.
REFERENCE_DATA_SUPPLEMENTAL_GROUP = 1000
MAX_BATCHES = 2
MAX_DESIGNS_PER_BATCH = 20
MAX_BUDGET_PER_BATCH = 3
MAX_TOTAL_DESIGNS = 24
# Upstream analysis defaults to 32 spawned processes. Each worker materializes
# its own structure/metric working set, so even this adapter's bounded 20-design
# shard can multiply memory until Kubernetes terminates the CPU stage. Process
# one candidate at a time instead: all oversampled designs still reach the
# upstream analysis and filtering gates, while peak analysis memory is bounded
# independently of the host's visible CPU count.
ANALYSIS_MAX_PROCESSES = 1
ANALYSIS_DATA_WORKERS = 0
INPUT_MANIFEST_MEDIA_TYPE = "application/vnd.fs2.scientific-manifest+json"
CAMPAIGN_INPUT_ID = "campaign-input"
CAMPAIGN_INPUT_SEMANTIC_TYPE = "boltzgen-campaign-input/v1"
CAMPAIGN_INPUT_MEDIA_TYPE = "application/gzip"
# The production registry must pass artifact-service-verified manifest entries
# to this adapter. Direct calls without this argument remain available for the
# adapter's legacy payload-pointer fixtures and offline unit tests.
REQUIRES_VERIFIED_INPUT_ARTIFACTS = True


@dataclass(frozen=True, slots=True)
class DesignBatch:
    shard_id: str
    num_designs: int
    budget: int
    reuse_completed: bool

    @classmethod
    def parse(cls, value: object, *, index: int) -> DesignBatch:
        item = strict_object(
            value,
            required=frozenset({"shard_id", "num_designs", "budget", "reuse_completed"}),
            label=f"BoltzGen batches[{index}]",
        )
        reuse = item["reuse_completed"]
        if not isinstance(reuse, bool):
            raise ScientificAdapterError("reuse_completed must be a boolean")
        designs = bounded_int(
            item["num_designs"],
            minimum=1,
            maximum=MAX_DESIGNS_PER_BATCH,
            label=f"batches[{index}].num_designs",
        )
        budget = bounded_int(
            item["budget"],
            minimum=1,
            maximum=MAX_BUDGET_PER_BATCH,
            label=f"batches[{index}].budget",
        )
        if budget > designs:
            raise ScientificAdapterError("BoltzGen budget cannot exceed num_designs")
        return cls(
            shard_id=safe_name(item["shard_id"], label=f"batches[{index}].shard_id", maximum=32),
            num_designs=designs,
            budget=budget,
            reuse_completed=reuse,
        )


@dataclass(frozen=True, slots=True)
class BoltzGenParameters:
    protocol: str
    batches: tuple[DesignBatch, ...]

    @classmethod
    def parse(cls, value: object) -> BoltzGenParameters:
        item = strict_object(
            value,
            required=frozenset({"protocol", "batches"}),
            label="BoltzGen parameters",
        )
        protocol = item["protocol"]
        if not isinstance(protocol, str) or protocol not in PROTOCOLS:
            raise ScientificAdapterError("BoltzGen protocol is unsupported")
        raw_batches = item["batches"]
        if not isinstance(raw_batches, list) or not 1 <= len(raw_batches) <= MAX_BATCHES:
            raise ScientificAdapterError(f"BoltzGen batches must contain 1..{MAX_BATCHES} items")
        batches = tuple(DesignBatch.parse(value, index=index) for index, value in enumerate(raw_batches))
        if len({batch.shard_id for batch in batches}) != len(batches):
            raise ScientificAdapterError("BoltzGen shard IDs must be unique")
        if sum(batch.num_designs for batch in batches) > MAX_TOTAL_DESIGNS:
            raise ScientificAdapterError("BoltzGen request exceeds the total design bound")
        return cls(protocol, batches)


def _request(value: object) -> tuple[PublicRunRequest, BoltzGenParameters]:
    request = parse_public_request(value, maximum_input_bytes=MAX_INPUT_BYTES)
    return request, BoltzGenParameters.parse(request.parameters)


def molecules_tree_binding() -> RuntimeTreeBinding:
    """Bind the extracted 45,227-entry molecule tree that `--moldir` consumes."""

    return RuntimeTreeBinding(
        artifact_id=MOLECULES_ARTIFACT_ID,
        mount_path=model_root(MOLECULES_ARTIFACT_ID),
        archive_sha256=MOLECULES_ARCHIVE_SHA256,
        tree_inventory_sha256=MOLECULES_TREE_INVENTORY_SHA256,
        entry_count=MOLECULES_TREE_ENTRY_COUNT,
    )


GPU_STAGES = ("configure", "design", "inverse-folding", "folding", "design-folding", "affinity")
FINAL_STAGES = ("analysis", "filtering")
WEIGHTS_ARTIFACT_STAGES = frozenset(GPU_STAGES)
MOLECULES_ARTIFACT_STAGES = frozenset((*GPU_STAGES, *FINAL_STAGES))


def _stage_runtime_artifacts(stage_id: str) -> tuple[str, ...]:
    """Return only the immutable trees that the selected upstream step reads."""

    artifacts: list[str] = []
    if stage_id in WEIGHTS_ARTIFACT_STAGES:
        artifacts.append(WEIGHTS_ARTIFACT_ID)
    if stage_id in MOLECULES_ARTIFACT_STAGES:
        artifacts.append(MOLECULES_ARTIFACT_ID)
    return tuple(artifacts)


def _protocol_steps(protocol: str) -> tuple[str, ...]:
    optional = ("design-folding",) if protocol in {"protein-anything", "protein-small_molecule"} else ()
    affinity = ("affinity",) if protocol == "protein-small_molecule" else ()
    return ("configure", "design", "inverse-folding", "folding", *optional, *affinity, *FINAL_STAGES)


def _upstream_step(stage_id: str) -> str:
    return stage_id.replace("-", "_")


def _campaign_workspace(batch: DesignBatch, operation_id: str) -> str:
    return run_workspace(MODEL_ID, operation_id, batch.shard_id)


def _design_yaml(batch: DesignBatch, operation_id: str) -> str:
    return f"{_campaign_workspace(batch, operation_id)}/inputs/design-specs/{batch.shard_id}.yaml"


def _configure_argv(parameters: BoltzGenParameters, batch: DesignBatch, operation_id: str) -> tuple[str, ...]:
    output = _campaign_workspace(batch, operation_id)
    weights = {filename: model_file(WEIGHTS_ARTIFACT_ID, filename) for filename in CHECKPOINTS}
    selected = tuple(_upstream_step(stage) for stage in _protocol_steps(parameters.protocol) if stage != "configure")
    values = [
        "boltzgen",
        "configure",
        _design_yaml(batch, operation_id),
        "--output",
        output,
        "--protocol",
        parameters.protocol,
        "--devices",
        "1",
        "--num_workers",
        "1",
        "--num_designs",
        str(batch.num_designs),
        "--budget",
        str(batch.budget),
        "--design_checkpoints",
        weights["boltzgen1_diverse.ckpt"],
        weights["boltzgen1_adherence.ckpt"],
        "--inverse_fold_checkpoint",
        weights["boltzgen1_ifold.ckpt"],
        "--folding_checkpoint",
        weights["boltz2_conf_final.ckpt"],
        "--affinity_checkpoint",
        weights["boltz2_aff.ckpt"],
        "--moldir",
        molecules_tree_binding().mount_path,
        "--steps",
        *selected,
        "--config",
        "analysis",
        f"num_processes={ANALYSIS_MAX_PROCESSES}",
        f"data.cfg.num_workers={ANALYSIS_DATA_WORKERS}",
        "data.cfg.pin_memory=false",
    ]
    if batch.reuse_completed:
        values.append("--reuse")
    return tuple(values)


def _execute_argv(batch: DesignBatch, stage_id: str, operation_id: str) -> tuple[str, ...]:
    return (
        "boltzgen",
        "execute",
        _campaign_workspace(batch, operation_id),
        "--steps",
        _upstream_step(stage_id),
    )


def _stage_argv(workspace: str, command: tuple[str, ...]) -> tuple[str, ...]:
    """Run upstream behind the controller-materialized completion publisher."""

    return ("python", f"{workspace}/{STAGE_RUNNER_RELATIVE_PATH}", "--", *command)


def _unwrapped_stage_argv(invocation: StageInvocation) -> tuple[str, ...]:
    expected_runner = f"{invocation.working_directory}/{STAGE_RUNNER_RELATIVE_PATH}"
    if invocation.argv[:3] != ("python", expected_runner, "--") or len(invocation.argv) < 4:
        raise ScientificAdapterError("BoltzGen stage does not use the trusted completion publisher")
    return invocation.argv[3:]


def _structure_entry_name(shard_id: str, file_name: str) -> str:
    """Bind an upstream ranking filename to one path-free manifest identity."""

    if (
        not file_name
        or len(file_name) > 255
        or Path(file_name).name != file_name
        or "\\" in file_name
        or Path(file_name).suffix.lower() not in {".cif", ".mmcif"}
    ):
        raise ScientificAdapterError("BoltzGen ranking file_name must be a bounded mmCIF basename")
    filename_digest = canonical_digest(file_name)
    return f"structure.{shard_id}.{filename_digest}"


def _bind_ranked_structures(
    shard_id: str,
    ranked_names: tuple[str, ...],
    structures: tuple[Path, ...],
) -> tuple[tuple[str, Path], ...]:
    """Bind CSV names to upstream copies without trusting rank prefixes.

    BoltzGen keeps the original basename in ``final_designs_metrics_*.csv``
    while copying each selected structure as ``rank<integer>_<basename>``.
    Accept that exact deterministic transformation (and the legacy unchanged
    basename), but require a complete one-to-one association so an ambiguous,
    missing, or extra structure remains a hard failure.
    """

    bound: list[tuple[str, Path]] = []
    used: set[Path] = set()
    for ranked_name in ranked_names:
        _structure_entry_name(shard_id, ranked_name)
        suffix = f"_{ranked_name}"
        matches: list[Path] = []
        for structure in structures:
            if structure in used:
                continue
            physical_name = structure.name
            prefix = physical_name[: -len(suffix)] if physical_name.endswith(suffix) else ""
            if physical_name == ranked_name or (
                prefix.startswith("rank") and prefix.removeprefix("rank").isdigit()
            ):
                matches.append(structure)
        if len(matches) != 1:
            raise ScientificAdapterError(
                "BoltzGen ranking filenames do not match emitted structure artifacts"
            )
        used.add(matches[0])
        bound.append((ranked_name, matches[0]))
    if len(used) != len(structures):
        raise ScientificAdapterError(
            "BoltzGen ranking filenames do not match emitted structure artifacts"
        )
    return tuple(bound)


def _campaign_input(
    request: PublicRunRequest,
    input_artifacts: tuple[ScientificInputArtifact, ...] | None,
) -> tuple[str, str | None]:
    if input_artifacts is None:
        if request.input_manifest.media_type == INPUT_MANIFEST_MEDIA_TYPE:
            raise ScientificAdapterError("BoltzGen manifest requests require verified input_artifacts")
        return request.input_manifest.artifact_id, request.input_manifest.compression

    if request.input_manifest.media_type != INPUT_MANIFEST_MEDIA_TYPE:
        raise ScientificAdapterError("BoltzGen input_manifest must identify a scientific manifest")
    if request.input_manifest.compression not in {None, "none"}:
        raise ScientificAdapterError("BoltzGen input manifest must not be compressed")
    if len(input_artifacts) != 1:
        raise ScientificAdapterError("BoltzGen input manifest must contain exactly one campaign-input")
    campaign = input_artifacts[0]
    if campaign.logical_artifact_id != CAMPAIGN_INPUT_ID:
        raise ScientificAdapterError("BoltzGen input manifest must contain campaign-input")
    if campaign.semantic_type != CAMPAIGN_INPUT_SEMANTIC_TYPE:
        raise ScientificAdapterError("BoltzGen campaign-input semantic type is invalid")
    if campaign.media_type != CAMPAIGN_INPUT_MEDIA_TYPE or campaign.compression != "gzip":
        raise ScientificAdapterError("BoltzGen campaign-input must be application/gzip with gzip compression")
    if not 1 <= campaign.size_bytes <= MAX_INPUT_BYTES:
        raise ScientificAdapterError("BoltzGen campaign-input size is outside the adapter bound")
    if str(campaign.artifact_id) == request.input_manifest.artifact_id:
        raise ScientificAdapterError("BoltzGen campaign payload and manifest must be distinct artifacts")
    return campaign.logical_artifact_id, campaign.compression


def compile_run(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
    input_artifacts: tuple[ScientificInputArtifact, ...] | None = None,
) -> AdapterExecutionPlan:
    """Compile bounded campaigns into exact upstream configure/execute steps."""

    request, parameters = _request(request_value)
    assert_profile_identity(
        profile,
        model_id=MODEL_ID,
        repository=SOURCE_REPOSITORY,
        revision=SOURCE_REVISION,
        parameter_schema=PARAMETER_SCHEMA,
        request=request,
    )
    campaign_artifact_id, campaign_compression = _campaign_input(request, input_artifacts)
    shard_ids = tuple(batch.shard_id for batch in parameters.batches)
    selected_stages = _protocol_steps(parameters.protocol)
    expansions: dict[str, ScientificStageExpansion] = {}
    previous_stage: str | None = None
    for stage_id in (*GPU_STAGES, *FINAL_STAGES):
        enabled = stage_id in selected_stages
        expansions[stage_id] = ScientificStageExpansion(
            shard_ids=shard_ids,
            enabled=enabled,
            depends_on=(() if previous_stage is None else (previous_stage,)) if enabled else None,
        )
        if enabled:
            previous_stage = stage_id

    environment = (
        ("HF_HUB_DISABLE_TELEMETRY", "1"),
        ("HF_HUB_OFFLINE", "1"),
        ("TRANSFORMERS_OFFLINE", "1"),
    )
    request_sha256 = canonical_digest(request.to_dict())
    prior_artifacts = {batch.shard_id: campaign_artifact_id for batch in parameters.batches}
    invocations: list[StageInvocation] = []
    for stage_id in selected_stages:
        runtime_artifacts = _stage_runtime_artifacts(stage_id)
        for batch in parameters.batches:
            previous = prior_artifacts[batch.shard_id]
            output = logical_stage_artifact(operation_id, stage_id, batch.shard_id)
            workspace = _campaign_workspace(batch, operation_id)
            materialization = ArtifactMaterialization(
                artifact_id=previous,
                destination=workspace,
                mode=(
                    MaterializationMode.BOLTZGEN_INPUT if stage_id == "configure" else MaterializationMode.OVERLAY_TAR
                ),
                compression=campaign_compression if stage_id == "configure" else "zstd",
                yaml_name=f"design-specs/{batch.shard_id}.yaml" if stage_id == "configure" else None,
                reuse_prefix=(
                    f"reusable-workspaces/{batch.shard_id}"
                    if stage_id == "configure" and batch.reuse_completed
                    else None
                ),
            )
            invocations.append(
                StageInvocation(
                    stage_id=stage_id,
                    shard_id=batch.shard_id,
                    argv=_stage_argv(
                        workspace,
                        (
                            _configure_argv(parameters, batch, operation_id)
                            if stage_id == "configure"
                            else _execute_argv(batch, stage_id, operation_id)
                        ),
                    ),
                    environment=(
                        *environment,
                        ("FS2_BOLTZGEN_BUDGET", str(batch.budget)),
                        ("FS2_BOLTZGEN_NUM_DESIGNS", str(batch.num_designs)),
                        ("FS2_BOLTZGEN_REQUEST_SHA256", request_sha256),
                    ),
                    working_directory=workspace,
                    consumes=(previous,),
                    produces=output,
                    handoff_name=(STAGE_HANDOFF_NAME if stage_id != selected_stages[-1] else None),
                    max_output_artifacts=(1 if stage_id != selected_stages[-1] else batch.budget + 1),
                    max_output_bytes=(MAX_STAGE_HANDOFF_BYTES if stage_id != selected_stages[-1] else MAX_OUTPUT_BYTES),
                    materializations=(materialization,),
                    runtime_artifacts=runtime_artifacts,
                    runtime_mounts=tuple(
                        RuntimeArtifactMount(
                            artifact_id=artifact_id,
                            mount_path=model_root(artifact_id),
                            supplemental_groups=(REFERENCE_DATA_SUPPLEMENTAL_GROUP,),
                        )
                        for artifact_id in runtime_artifacts
                    ),
                    runtime_trees=((molecules_tree_binding(),) if MOLECULES_ARTIFACT_ID in runtime_artifacts else ()),
                )
            )
            prior_artifacts[batch.shard_id] = output
    return build_execution_plan(
        model_id=MODEL_ID,
        variant_id=VARIANT_ID,
        source_revision=SOURCE_REVISION,
        request=request,
        profile=profile,
        expansions=expansions,
        invocations=tuple(invocations),
        required_model_artifacts=(WEIGHTS_ARTIFACT_ID, MOLECULES_ARTIFACT_ID),
    )


def validate_output(
    request_value: object,
    output_manifest: object,
    *,
    artifact_loader: ArtifactLoader,
) -> dict[str, object]:
    request, parameters = _request(request_value)
    expected_designs = sum(batch.budget for batch in parameters.batches)
    artifacts = load_output_manifest(
        output_manifest,
        artifact_loader=artifact_loader,
        maximum_entries=expected_designs + len(parameters.batches),
        maximum_total_bytes=MAX_OUTPUT_BYTES,
    )
    ranking_items = [item for item in artifacts if item.semantic_type == "boltzgen-ranking-csv/v1"]
    structures = [item for item in artifacts if item.semantic_type == "protein-complex-structure/v1"]
    if len(ranking_items) != len(parameters.batches) or len(structures) != expected_designs:
        raise ScientificAdapterError("BoltzGen output does not account for the requested budget")
    batch_budgets = {batch.shard_id: batch.budget for batch in parameters.batches}
    seen: set[str] = set()
    expected_structure_names: set[str] = set()
    observed_batches: Counter[str] = Counter()
    for ranking in ranking_items:
        if not ranking.name.startswith("ranking."):
            raise ScientificAdapterError("BoltzGen ranking artifact has no shard identity")
        shard_id = ranking.name.removeprefix("ranking.")
        if shard_id not in batch_budgets or observed_batches[shard_id]:
            raise ScientificAdapterError("BoltzGen ranking shard identity is invalid")
        fields, rows = parse_csv_artifact(
            ranking.content,
            label=f"BoltzGen ranking {shard_id}",
            maximum_rows=batch_budgets[shard_id],
        )
        required = {"id", "file_name", "designed_chain_sequence"}
        if not required.issubset(fields) or len(rows) != batch_budgets[shard_id]:
            raise ScientificAdapterError("BoltzGen ranking CSV is missing required rows or columns")
        confidence_key = "design_to_target_iptm" if "design_to_target_iptm" in fields else "design_iptm"
        rmsd_key = "designfolding-filter_rmsd" if "designfolding-filter_rmsd" in fields else "filter_rmsd"
        if confidence_key not in fields or rmsd_key not in fields:
            raise ScientificAdapterError("BoltzGen ranking CSV lacks confidence or refold RMSD")
        for row in rows:
            design_id = row["id"]
            if not 1 <= len(design_id) <= 128 or any(character in design_id for character in ("/", "\\", "\x00")):
                raise ScientificAdapterError("BoltzGen design ID is invalid")
            structure_name = _structure_entry_name(shard_id, row["file_name"])
            if structure_name in expected_structure_names:
                raise ScientificAdapterError("BoltzGen ranking contains a duplicate structure filename")
            expected_structure_names.add(structure_name)
            qualified = f"{shard_id}:{design_id}"
            if qualified in seen:
                raise ScientificAdapterError("BoltzGen ranking contains a duplicate design")
            seen.add(qualified)
            sequence = protein_sequence(row["designed_chain_sequence"], label="BoltzGen sequence")
            if max(Counter(sequence).values()) / len(sequence) > 0.30:
                raise ScientificAdapterError("BoltzGen sequence fails the composition-bias gate")
            if finite_number(float(row[confidence_key]), minimum=0.0, maximum=1.0, label=confidence_key) <= 0:
                raise ScientificAdapterError("BoltzGen design has no meaningful target interface confidence")
            finite_number(float(row[rmsd_key]), minimum=0.0, maximum=10.0, label=rmsd_key)
            if "unresolved_residues" in fields and row["unresolved_residues"] not in {"0", "0.0"}:
                raise ScientificAdapterError("BoltzGen design contains unresolved residues")
        observed_batches[shard_id] = len(rows)
    if dict(observed_batches) != batch_budgets:
        raise ScientificAdapterError("BoltzGen output has missing shard rankings")
    structure_names = {item.name for item in structures}
    if structure_names != expected_structure_names:
        raise ScientificAdapterError("BoltzGen ranking filenames do not match emitted structure artifacts")
    atom_count = sum(structure_atom_count(item, require_two_chains=True) for item in structures)
    return {
        "validator_id": "boltzgen-v0-3-2",
        "status": "passed",
        "request_sha256": canonical_digest(request.to_dict()),
        "design_count": expected_designs,
        "atom_count": atom_count,
        "qualification_effect": "none-offline-validation-only",
    }


def collect_output(request_value: object, workspaces: Mapping[str, Path]) -> CollectedOutput:
    """Collect real BoltzGen filter CSVs and final refolded mmCIF files."""

    _request_value, parameters = _request(request_value)
    if set(workspaces) != {batch.shard_id for batch in parameters.batches}:
        raise ScientificAdapterError("BoltzGen collector workspaces must exactly match request shards")
    collected: list[CollectedOutput] = []
    for batch in parameters.batches:
        root = workspaces[batch.shard_id]
        final_root = root / "final_ranked_designs"
        ranking = final_root / f"final_designs_metrics_{batch.budget}.csv"
        structures = sorted((final_root / f"final_{batch.budget}_designs").glob("*.cif"))
        structures += sorted((final_root / f"final_{batch.budget}_designs").glob("*.mmcif"))
        if len(structures) != batch.budget:
            raise ScientificAdapterError("BoltzGen final structure count does not equal the requested budget")
        fields, rows = parse_csv_artifact(
            ranking.read_bytes(),
            label=f"BoltzGen ranking {batch.shard_id}",
            maximum_rows=batch.budget,
        )
        if "file_name" not in fields or len(rows) != batch.budget:
            raise ScientificAdapterError("BoltzGen ranking CSV does not account for the requested budget")
        ranked_names = tuple(row["file_name"] for row in rows)
        if len(set(ranked_names)) != batch.budget:
            raise ScientificAdapterError("BoltzGen ranking contains duplicate structure filenames")
        bound_structures = _bind_ranked_structures(
            batch.shard_id,
            ranked_names,
            tuple(structures),
        )
        entries = [(f"ranking.{batch.shard_id}", "boltzgen-ranking-csv/v1", ranking, True)]
        entries.extend(
            (
                _structure_entry_name(batch.shard_id, ranked_name),
                "protein-complex-structure/v1",
                structure,
                False,
            )
            for ranked_name, structure in bound_structures
        )
        collected.append(
            collect_output_files(
                root,
                tuple(entries),
                manifest_id=f"boltzgen.{batch.shard_id}.results",
                maximum_total_bytes=MAX_OUTPUT_BYTES,
            )
        )
    blobs = {key: value for item in collected for key, value in item.blobs.items()}
    manifest_entries: list[object] = []
    for item in collected:
        raw_entries = item.manifest.get("entries")
        if not isinstance(raw_entries, list):
            raise AssertionError("internal collector manifest entries are invalid")
        manifest_entries.extend(raw_entries)
    return CollectedOutput(
        manifest={
            "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
            "manifest_id": "boltzgen.results",
            "entries": manifest_entries,
        },
        blobs=blobs,
    )


def _completion_marker(invocation: StageInvocation, workspace: Path) -> tuple[Path, str]:
    """Read the exact atomic marker emitted after the upstream child exits zero."""

    from . import CollectionPendingError

    marker = workspace.joinpath(*Path(STAGE_COMPLETION_RELATIVE_PATH).parts)
    if not marker.exists():
        raise CollectionPendingError("BoltzGen stage has not atomically published completion")
    try:
        root = workspace.resolve(strict=True)
        resolved = marker.resolve(strict=True)
    except OSError as error:
        raise ScientificAdapterError("BoltzGen completion marker is unavailable") from error
    if root not in resolved.parents or marker.is_symlink() or not resolved.is_file():
        raise ScientificAdapterError("BoltzGen completion marker is not a contained regular file")
    payload = resolved.read_bytes()
    if not 1 <= len(payload) <= 16 * 1024:
        raise ScientificAdapterError("BoltzGen completion marker size is outside the bound")
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ScientificAdapterError("BoltzGen terminal completion marker is invalid JSON") from error
    command = _unwrapped_stage_argv(invocation)
    command_bytes = json.dumps(
        command,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    expected = {
        "schema": STAGE_COMPLETION_SCHEMA,
        "status": "passed",
        "stage_id": invocation.stage_id,
        "shard_id": invocation.shard_id,
        "logical_output_id": invocation.produces,
        "collector_id": invocation.collector_id,
        "validator_id": invocation.validator_id,
        "argv_sha256": hashlib.sha256(command_bytes).hexdigest(),
    }
    if value != expected:
        raise ScientificAdapterError("BoltzGen completion marker differs from the frozen invocation")
    return resolved, hashlib.sha256(payload).hexdigest()


def _atomic_publish(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".partial",
        delete=False,
    ) as output:
        temporary = Path(output.name)
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    try:
        os.chmod(temporary, 0o400)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _workspace_snapshot(workspace: Path) -> tuple[tuple[str, bool, bytes], ...]:
    """Capture one bounded, symlink-free workspace after terminal completion."""

    root = workspace.resolve(strict=True)
    entries: list[tuple[str, bool, bytes]] = []
    total = 0
    file_count = 0
    for path in sorted(root.rglob("*"), key=lambda candidate: candidate.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if relative.parts[0] == ".fs2":
            continue
        if path.is_symlink():
            raise ScientificAdapterError("BoltzGen workspace handoff contains a symbolic link")
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise ScientificAdapterError("BoltzGen workspace changed during handoff collection") from error
        if root not in resolved.parents:
            raise ScientificAdapterError("BoltzGen workspace handoff escapes its attempt root")
        name = relative.as_posix()
        if path.is_dir():
            entries.append((name, True, b""))
        elif path.is_file():
            before = path.stat()
            content = path.read_bytes()
            after = path.stat()
            if (
                len(content) != before.st_size
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ino != after.st_ino
            ):
                raise ScientificAdapterError("BoltzGen workspace changed during handoff collection")
            total += len(content)
            file_count += 1
            if total > MAX_STAGE_HANDOFF_CONTENT_BYTES:
                raise ScientificAdapterError("BoltzGen workspace handoff exceeds the extracted byte bound")
            entries.append((name, False, content))
        else:
            raise ScientificAdapterError("BoltzGen workspace handoff contains an unsupported entry")
        if len(entries) > MAX_STAGE_HANDOFF_MEMBERS:
            raise ScientificAdapterError("BoltzGen workspace handoff exceeds the member bound")
    if not entries or file_count == 0:
        raise ScientificAdapterError("BoltzGen workspace handoff contains no regular files")
    return tuple(entries)


def _encode_handoff(entries: tuple[tuple[str, bool, bytes], ...]) -> bytes:
    stream = io.BytesIO()
    try:
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name, is_directory, content in entries:
                member = tarfile.TarInfo(f"{name}/" if is_directory else name)
                member.uid = 0
                member.gid = 0
                member.uname = ""
                member.gname = ""
                member.mtime = 0
                member.mode = 0o755 if is_directory else 0o400
                if is_directory:
                    member.type = tarfile.DIRTYPE
                else:
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
                    continue
                archive.addfile(member)
        raw = stream.getvalue()
        if len(raw) > MAX_STAGE_HANDOFF_BYTES:
            raise ScientificAdapterError("BoltzGen workspace handoff exceeds the framed tar bound")
        result = zstandard.ZstdCompressor(level=3, write_checksum=True, write_content_size=True).compress(raw)
    except (OSError, tarfile.TarError, zstandard.ZstdError) as error:
        raise ScientificAdapterError("BoltzGen workspace handoff could not be encoded") from error
    if not 1 <= len(result) <= MAX_STAGE_HANDOFF_BYTES:
        raise ScientificAdapterError("BoltzGen workspace handoff exceeds the compressed byte bound")
    return result


def _validate_handoff(content: bytes, entries: tuple[tuple[str, bool, bytes], ...]) -> None:
    expected = {name: (is_directory, payload) for name, is_directory, payload in entries}
    try:
        raw = zstandard.ZstdDecompressor().decompress(content, max_output_size=MAX_STAGE_HANDOFF_BYTES)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            members = archive.getmembers()
            observed: dict[str, tuple[bool, bytes]] = {}
            for member in members:
                name = member.name.rstrip("/")
                if name in observed or not (member.isdir() or member.isfile()):
                    raise ScientificAdapterError("BoltzGen handoff archive has duplicate or unsupported entries")
                if member.isdir():
                    observed[name] = (True, b"")
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise ScientificAdapterError("BoltzGen handoff archive member has no payload")
                payload = source.read(MAX_STAGE_HANDOFF_CONTENT_BYTES + 1)
                if len(payload) != member.size:
                    raise ScientificAdapterError("BoltzGen handoff archive member size differs")
                observed[name] = (False, payload)
    except (tarfile.TarError, zstandard.ZstdError) as error:
        raise ScientificAdapterError("BoltzGen handoff archive failed its terminal validation") from error
    if observed != expected:
        raise ScientificAdapterError("BoltzGen handoff archive differs from the completed workspace")


def _collect_handoff_stage(
    invocation: StageInvocation,
    workspace: Path,
    *,
    completion_sha256: str,
) -> CollectedStageOutput:
    from . import CollectedArtifactFile, CollectedStageOutput

    if invocation.handoff_name != STAGE_HANDOFF_NAME or invocation.stage_id == "filtering":
        raise ScientificAdapterError("BoltzGen handoff collector received another stage contract")
    entries = _workspace_snapshot(workspace)
    content = _encode_handoff(entries)
    _validate_handoff(content, entries)
    output = workspace / ".fs2" / "boltzgen-stage-handoff.tar.zst"
    _atomic_publish(output, content)
    return CollectedStageOutput(
        artifacts=(
            CollectedArtifactFile(
                name=STAGE_HANDOFF_NAME,
                semantic_type=STAGE_HANDOFF_SEMANTIC_TYPE,
                path=output,
                media_type=STAGE_HANDOFF_MEDIA_TYPE,
                compression=STAGE_HANDOFF_COMPRESSION,
            ),
        ),
        validation={
            "validator_id": invocation.validator_id,
            "status": "passed",
            "completion_marker_sha256": completion_sha256,
            "handoff_sha256": hashlib.sha256(content).hexdigest(),
            "handoff_size_bytes": len(content),
            "member_count": len(entries),
            "expanded_bytes": sum(len(payload) for _name, is_directory, payload in entries if not is_directory),
        },
    )


def _environment_int(invocation: StageInvocation, name: str, *, minimum: int, maximum: int) -> int:
    try:
        raw = dict(invocation.environment)[name]
        value = int(raw)
    except (KeyError, ValueError) as error:
        raise ScientificAdapterError(f"BoltzGen invocation has no valid {name}") from error
    return bounded_int(value, minimum=minimum, maximum=maximum, label=name)


def _collect_final_stage(
    invocation: StageInvocation,
    workspace: Path,
    *,
    completion_sha256: str,
) -> CollectedStageOutput:
    from . import CollectedArtifactFile, CollectedStageOutput

    if invocation.stage_id != "filtering" or invocation.handoff_name is not None:
        raise ScientificAdapterError("BoltzGen result collector received another stage contract")
    command = _unwrapped_stage_argv(invocation)
    if command[:2] != ("boltzgen", "execute") or command[-2:] != ("--steps", "filtering"):
        raise ScientificAdapterError("BoltzGen result invocation is not the filtering stage")
    budget = _environment_int(invocation, "FS2_BOLTZGEN_BUDGET", minimum=1, maximum=1_000)
    request_sha256 = dict(invocation.environment).get("FS2_BOLTZGEN_REQUEST_SHA256", "")
    if len(request_sha256) != 64 or any(character not in "0123456789abcdef" for character in request_sha256):
        raise ScientificAdapterError("BoltzGen invocation request digest is invalid")

    root = workspace.resolve(strict=True)
    final_root = root / "final_ranked_designs"
    ranking = final_root / f"final_designs_metrics_{budget}.csv"
    structures_root = final_root / f"final_{budget}_designs"
    try:
        structures = sorted((*structures_root.glob("*.cif"), *structures_root.glob("*.mmcif")))
        ranking_content = ranking.read_bytes()
    except OSError as error:
        raise ScientificAdapterError("BoltzGen terminal result files are unavailable") from error
    if ranking.is_symlink() or len(structures) != budget:
        raise ScientificAdapterError("BoltzGen terminal result count differs from the requested budget")
    fields, rows = parse_csv_artifact(
        ranking_content,
        label=f"BoltzGen ranking {invocation.shard_id}",
        maximum_rows=budget,
    )
    required = {"id", "file_name", "designed_chain_sequence"}
    if not required.issubset(fields) or len(rows) != budget:
        raise ScientificAdapterError("BoltzGen ranking CSV is missing required rows or columns")
    confidence_key = "design_to_target_iptm" if "design_to_target_iptm" in fields else "design_iptm"
    rmsd_key = "designfolding-filter_rmsd" if "designfolding-filter_rmsd" in fields else "filter_rmsd"
    if confidence_key not in fields or rmsd_key not in fields:
        raise ScientificAdapterError("BoltzGen ranking CSV lacks confidence or refold RMSD")
    ranked_names: set[str] = set()
    ranked_names_in_order: list[str] = []
    for row in rows:
        _structure_entry_name(invocation.shard_id, row["file_name"])
        if row["file_name"] in ranked_names:
            raise ScientificAdapterError("BoltzGen ranking contains a duplicate structure filename")
        ranked_names.add(row["file_name"])
        ranked_names_in_order.append(row["file_name"])
        sequence = protein_sequence(row["designed_chain_sequence"], label="BoltzGen sequence")
        if max(Counter(sequence).values()) / len(sequence) > 0.30:
            raise ScientificAdapterError("BoltzGen sequence fails the composition-bias gate")
        if finite_number(float(row[confidence_key]), minimum=0.0, maximum=1.0, label=confidence_key) <= 0:
            raise ScientificAdapterError("BoltzGen design has no meaningful target interface confidence")
        finite_number(float(row[rmsd_key]), minimum=0.0, maximum=10.0, label=rmsd_key)
        if "unresolved_residues" in fields and row["unresolved_residues"] not in {"0", "0.0"}:
            raise ScientificAdapterError("BoltzGen design contains unresolved residues")
    bound_structures = _bind_ranked_structures(
        invocation.shard_id,
        tuple(ranked_names_in_order),
        tuple(structures),
    )

    sanitized_ranking = canonicalize_upstream_csv(
        ranking_content,
        label=f"BoltzGen ranking {invocation.shard_id}",
        maximum_rows=budget,
    )
    ranking_output = root / ".fs2" / "boltzgen-final-ranking.csv"
    _atomic_publish(ranking_output, sanitized_ranking)
    artifacts = [
        CollectedArtifactFile(
            name=f"ranking.{invocation.shard_id}",
            semantic_type="boltzgen-ranking-csv/v1",
            path=ranking_output,
            media_type=FINAL_RANKING_MEDIA_TYPE,
        )
    ]
    atom_count = 0
    total_bytes = len(sanitized_ranking)
    for index, (ranked_name, structure) in enumerate(bound_structures):
        try:
            resolved = structure.resolve(strict=True)
        except OSError as error:
            raise ScientificAdapterError("BoltzGen terminal structure is unavailable") from error
        if root not in resolved.parents or structure.is_symlink() or not resolved.is_file():
            raise ScientificAdapterError("BoltzGen terminal structure is not a contained regular file")
        content = resolved.read_bytes()
        total_bytes += len(content)
        if total_bytes > invocation.max_output_bytes:
            raise ScientificAdapterError("BoltzGen terminal outputs exceed the invocation byte bound")
        pointer = ArtifactPointer(
            artifact_id=f"validation.boltzgen.{index}",
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            media_type=FINAL_STRUCTURE_MEDIA_TYPE,
        )
        name = _structure_entry_name(invocation.shard_id, ranked_name)
        atom_count += structure_atom_count(
            LoadedArtifact(name, "protein-complex-structure/v1", pointer, content),
            require_two_chains=True,
        )
        artifacts.append(
            CollectedArtifactFile(
                name=name,
                semantic_type="protein-complex-structure/v1",
                path=resolved,
                media_type=FINAL_STRUCTURE_MEDIA_TYPE,
            )
        )
    return CollectedStageOutput(
        artifacts=tuple(artifacts),
        validation={
            "validator_id": invocation.validator_id,
            "status": "passed",
            "request_sha256": request_sha256,
            "completion_marker_sha256": completion_sha256,
            "design_count": budget,
            "atom_count": atom_count,
        },
    )


def collect_companion_output(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    """Collect one exact production stage after an atomic process boundary."""

    if invocation.collector_id != "boltzgen-v0-3-2" or invocation.validator_id != "boltzgen-v0-3-2":
        raise ScientificAdapterError("BoltzGen collector received another execution identity")
    _marker, completion_sha256 = _completion_marker(invocation, workspace)
    if invocation.stage_id == "filtering":
        return _collect_final_stage(invocation, workspace, completion_sha256=completion_sha256)
    return _collect_handoff_stage(invocation, workspace, completion_sha256=completion_sha256)

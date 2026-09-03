"""BoltzGen v0.3.2 adapter for independent one-GPU design campaigns."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..catalog_adapter import ScientificStageExpansion
from ..models import (
    AdapterExecutionPlan,
    ArtifactMaterialization,
    MaterializationMode,
    RuntimeTreeBinding,
    StageInvocation,
)
from .common import (
    ArtifactLoader,
    CollectedOutput,
    PublicRunRequest,
    ScientificAdapterError,
    assert_profile_identity,
    bounded_int,
    build_execution_plan,
    canonical_digest,
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
MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024 * 1024
MAX_BATCHES = 32
MAX_TOTAL_DESIGNS = 60_000


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
        designs = bounded_int(item["num_designs"], minimum=1, maximum=10_000, label=f"batches[{index}].num_designs")
        budget = bounded_int(item["budget"], minimum=1, maximum=1000, label=f"batches[{index}].budget")
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
RUNTIME_ARTIFACT_STAGES = frozenset(GPU_STAGES)


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


def compile_run(profile: Mapping[str, object], request_value: object, *, operation_id: str) -> AdapterExecutionPlan:
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
    prior_artifacts = {batch.shard_id: request.input_manifest.artifact_id for batch in parameters.batches}
    invocations: list[StageInvocation] = []
    for stage_id in selected_stages:
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
                compression=request.input_manifest.compression if stage_id == "configure" else "zstd",
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
                    argv=(
                        _configure_argv(parameters, batch, operation_id)
                        if stage_id == "configure"
                        else _execute_argv(batch, stage_id, operation_id)
                    ),
                    environment=environment,
                    working_directory=workspace,
                    consumes=(previous,),
                    produces=output,
                    materializations=(materialization,),
                    runtime_artifacts=(
                        (WEIGHTS_ARTIFACT_ID, MOLECULES_ARTIFACT_ID) if stage_id in RUNTIME_ARTIFACT_STAGES else ()
                    ),
                    runtime_trees=((molecules_tree_binding(),) if stage_id in RUNTIME_ARTIFACT_STAGES else ()),
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
        ranked_names = {row["file_name"] for row in rows}
        if len(ranked_names) != batch.budget or ranked_names != {structure.name for structure in structures}:
            raise ScientificAdapterError("BoltzGen ranking filenames do not match collected structures")
        entries = [(f"ranking.{batch.shard_id}", "boltzgen-ranking-csv/v1", ranking, True)]
        entries.extend(
            (
                _structure_entry_name(batch.shard_id, structure.name),
                "protein-complex-structure/v1",
                structure,
                False,
            )
            for structure in structures
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

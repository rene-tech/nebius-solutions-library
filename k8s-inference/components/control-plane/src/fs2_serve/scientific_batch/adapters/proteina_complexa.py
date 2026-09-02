"""Proteina-Complexa v1.1.0-observed staged adapter.

The upstream repository has no release tag at the pinned commit.  The package
version is therefore descriptive only; the immutable identity is the commit
and the selected Hugging Face weight revision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..models import AdapterExecutionPlan, ArtifactMaterialization, MaterializationMode, StageInvocation
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
    run_workspace,
    safe_name,
    strict_object,
    structure_atom_count,
)

MODEL_ID = "proteina-complexa"
VARIANT_ID = "upstream-dev-20260827"
SOURCE_REPOSITORY = "NVIDIA-BioNeMo/Proteina-Complexa"
SOURCE_REVISION = "54058860d43444c7289873f77d3e50b5b02348cd"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/proteina-complexa-parameters/v1"
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024 * 1024

AF2_ARTIFACT_ID = "alphafold2-params"
RF3_ARTIFACT_ID = "rosettafold3-checkpoint"


@dataclass(frozen=True, slots=True)
class Variant:
    name: str
    source_artifact_id: str
    runtime_artifact_id: str
    repository: str
    revision: str
    config: str
    checkpoint: str
    autoencoder: str


VARIANTS = {
    "protein-target": Variant(
        "protein-target",
        "proteina-complexa-protein-target-160m-v1",
        "complexa-protein",
        "nvidia/NV-Proteina-Complexa-Protein-Target-160M-v1",
        "ffed199e32612b98ffa04f4640d34d37b137fca5",
        "/opt/fs2/source/configs/search_binder_local_pipeline.yaml",
        "complexa.ckpt",
        "complexa_ae.ckpt",
    ),
    "ligand-target": Variant(
        "ligand-target",
        "proteina-complexa-ligand-target-160m-v1",
        "complexa-ligand",
        "nvidia/NV-Proteina-Complexa-Ligand-Target-160M-v1",
        "bc90c8b2c701ceb52d5faef72600b6b5be880244",
        "/opt/fs2/source/configs/search_ligand_binder_local_pipeline.yaml",
        "complexa_ligand.ckpt",
        "complexa_ligand_ae.ckpt",
    ),
    "ame": Variant(
        "ame",
        "proteina-complexa-ame-160m-v1",
        "complexa-ame",
        "nvidia/NV-Proteina-Complexa-AME-160M-v1",
        "9743d749a8754080a32fda857d95579dfa4dabae",
        "/opt/fs2/source/configs/search_ame_local_pipeline.yaml",
        "complexa_ame.ckpt",
        "complexa_ame_ae.ckpt",
    ),
}


@dataclass(frozen=True, slots=True)
class ProteinaParameters:
    variant: Variant
    target_id: str
    run_name: str
    seed: int
    num_samples: int
    diffusion_steps: int

    @classmethod
    def parse(cls, value: object) -> ProteinaParameters:
        item = strict_object(
            value,
            required=frozenset({"variant", "target_id", "run_name", "seed", "num_samples", "diffusion_steps"}),
            label="Proteina-Complexa parameters",
        )
        raw_variant = item["variant"]
        if not isinstance(raw_variant, str) or raw_variant not in VARIANTS:
            raise ScientificAdapterError("Proteina-Complexa variant is unsupported")
        target_id = item["target_id"]
        if (
            not isinstance(target_id, str)
            or not 1 <= len(target_id) <= 64
            or not target_id[0].isalnum()
            or any(
                character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
                for character in target_id
            )
        ):
            raise ScientificAdapterError("target_id must be a logical target name, never a path")
        return cls(
            variant=VARIANTS[raw_variant],
            target_id=target_id,
            run_name=safe_name(item["run_name"], label="run_name"),
            seed=bounded_int(item["seed"], minimum=0, maximum=2**31 - 1, label="seed"),
            num_samples=bounded_int(item["num_samples"], minimum=1, maximum=1024, label="num_samples"),
            diffusion_steps=bounded_int(item["diffusion_steps"], minimum=1, maximum=2000, label="diffusion_steps"),
        )


def _request(value: object) -> tuple[PublicRunRequest, ProteinaParameters]:
    request = parse_public_request(value, maximum_input_bytes=MAX_INPUT_BYTES)
    return request, ProteinaParameters.parse(request.parameters)


def _environment(parameters: ProteinaParameters, stage_id: str, workspace: str) -> tuple[tuple[str, str], ...]:
    values = [
        ("COMPLEXA_INIT", "1"),
        ("DATA_PATH", workspace),
        ("HF_HUB_OFFLINE", "1"),
        ("TRANSFORMERS_OFFLINE", "1"),
    ]
    runtime_artifacts = _stage_runtime_artifacts(parameters, stage_id)
    if AF2_ARTIFACT_ID in runtime_artifacts:
        values.append(("AF2_DIR", model_root(AF2_ARTIFACT_ID)))
    if RF3_ARTIFACT_ID in runtime_artifacts:
        values.extend(
            (
                ("RF3_CKPT_PATH", model_file(RF3_ARTIFACT_ID, "rf3_foundry_01_24_latest_remapped.ckpt")),
                ("RF3_EXEC_PATH", "/opt/venv/bin/rf3"),
            )
        )
    return tuple(values)


def _argv(parameters: ProteinaParameters, stage_id: str) -> tuple[str, ...]:
    variant = parameters.variant
    values = [
        "complexa",
        stage_id,
        variant.config,
        f"++run_name={parameters.run_name}",
        f"++generation.task_name={parameters.target_id}",
        f"++seed={parameters.seed}",
    ]
    if stage_id == "generate":
        weights_root = model_file(variant.runtime_artifact_id, variant.checkpoint).rsplit("/", 1)[0]
        values.extend(
            (
                f"++ckpt_path={weights_root}",
                f"++ckpt_name={variant.checkpoint}",
                f"++autoencoder_ckpt_path={model_file(variant.runtime_artifact_id, variant.autoencoder)}",
                f"++generation.args.nsteps={parameters.diffusion_steps}",
                f"++generation.dataloader.dataset.nres.nsamples={parameters.num_samples}",
            )
        )
    if stage_id == "evaluate":
        # The source defaults load ESM2, ESMFold and MPNN artifacts that have
        # not yet crossed the target cache/readiness gate. Keep the core AF2
        # or RF3 self-refolding metric enabled while failing closed on those
        # optional online-download paths.
        values.extend(
            (
                "++metric.compute_esm_metrics=false",
                "++metric.compute_monomer_metrics=false",
                "++metric.compute_designability=false",
                "++metric.compute_codesignability=false",
                "++metric.sequence_types=[self]",
            )
        )
    return tuple(values)


def _stage_runtime_artifacts(parameters: ProteinaParameters, stage_id: str) -> tuple[str, ...]:
    variant_artifact = parameters.variant.runtime_artifact_id
    if stage_id == "generate":
        if parameters.variant.name == "protein-target":
            return (variant_artifact, AF2_ARTIFACT_ID)
        if parameters.variant.name == "ligand-target":
            return (variant_artifact, RF3_ARTIFACT_ID)
        return (variant_artifact,)
    if stage_id == "evaluate":
        return (AF2_ARTIFACT_ID,) if parameters.variant.name == "protein-target" else (RF3_ARTIFACT_ID,)
    return ()


def compile_run(profile: Mapping[str, object], request_value: object, *, operation_id: str) -> AdapterExecutionPlan:
    """Compile a canonical request into the canonical four-stage controller plan."""

    request, parameters = _request(request_value)
    assert_profile_identity(
        profile,
        model_id=MODEL_ID,
        repository=SOURCE_REPOSITORY,
        revision=SOURCE_REVISION,
        parameter_schema=PARAMETER_SCHEMA,
        request=request,
    )
    stage_ids = ("generate", "filter", "evaluate", "analyze")
    invocations: list[StageInvocation] = []
    previous = request.input_manifest.artifact_id
    workspace = run_workspace(MODEL_ID, operation_id, "main")
    for stage_id in stage_ids:
        output = logical_stage_artifact(operation_id, stage_id, "main")
        invocations.append(
            StageInvocation(
                stage_id=stage_id,
                shard_id="main",
                argv=_argv(parameters, stage_id),
                environment=_environment(parameters, stage_id, workspace),
                working_directory=workspace,
                consumes=(previous,),
                produces=output,
                materializations=(
                    ArtifactMaterialization(
                        artifact_id=previous,
                        destination=workspace,
                        mode=(
                            MaterializationMode.EXTRACT_TAR
                            if stage_id == "generate"
                            else MaterializationMode.OVERLAY_TAR
                        ),
                        compression=request.input_manifest.compression if stage_id == "generate" else "zstd",
                    ),
                ),
                runtime_artifacts=_stage_runtime_artifacts(parameters, stage_id),
            )
        )
        previous = output
    return build_execution_plan(
        model_id=MODEL_ID,
        variant_id=VARIANT_ID,
        source_revision=SOURCE_REVISION,
        request=request,
        profile=profile,
        expansions=None,
        invocations=tuple(invocations),
        required_model_artifacts=tuple(
            dict.fromkeys(
                artifact_id
                for stage_id in stage_ids
                for artifact_id in _stage_runtime_artifacts(parameters, stage_id)
            )
        ),
    )


def validate_output(
    request_value: object,
    output_manifest: object,
    *,
    artifact_loader: ArtifactLoader,
) -> dict[str, object]:
    """Validate immutable final artifacts without accepting public file paths."""

    request, parameters = _request(request_value)
    artifacts = load_output_manifest(
        output_manifest,
        artifact_loader=artifact_loader,
        maximum_entries=parameters.num_samples + 1,
        maximum_total_bytes=MAX_OUTPUT_BYTES,
    )
    result_items = [item for item in artifacts if item.semantic_type == "proteina-complexa-results-csv/v1"]
    structures = [item for item in artifacts if item.semantic_type == "protein-complex-structure/v1"]
    if not result_items or not structures:
        raise ScientificAdapterError("Proteina output requires upstream result CSV and structure artifacts")
    seen: set[str] = set()
    design_count = 0
    for result in result_items:
        fields, rows = parse_csv_artifact(
            result.content,
            label="Proteina upstream results",
            maximum_rows=parameters.num_samples,
        )
        if "id_gen" not in fields or any("path" in field.lower() for field in fields):
            raise ScientificAdapterError("Proteina result CSV identity or path sanitization is invalid")
        metric_fields = tuple(
            field
            for field in fields
            if field.startswith("_res_")
            and not field.endswith("_all")
            and any(marker in field.lower() for marker in ("plddt", "pae", "rmsd"))
        )
        if not metric_fields:
            raise ScientificAdapterError("Proteina result CSV contains no recognized scientific metrics")
        for row in rows:
            design_id = row["id_gen"]
            if not 1 <= len(design_id) <= 128 or any(character in design_id for character in ("/", "\\", "\x00")):
                raise ScientificAdapterError("Proteina design ID is invalid")
            if design_id in seen:
                raise ScientificAdapterError("Proteina result CSV contains a duplicate design")
            seen.add(design_id)
            observed = 0
            for field in metric_fields:
                raw = row[field]
                if not raw or raw.startswith("["):
                    continue
                try:
                    value = float(raw)
                except ValueError as error:
                    raise ScientificAdapterError(f"Proteina metric {field} is not numeric") from error
                maximum = 1.0 if "plddt" in field.lower() else 100.0
                finite_number(value, minimum=0.0, maximum=maximum, label=field)
                observed += 1
            if observed == 0:
                raise ScientificAdapterError("Proteina result row has no finite scalar scientific metric")
        design_count += len(rows)
    if not 1 <= design_count <= parameters.num_samples or len(structures) != design_count:
        raise ScientificAdapterError("Proteina result rows and structures do not match the request bound")
    atom_count = sum(structure_atom_count(item, require_two_chains=True) for item in structures)
    return {
        "validator_id": "proteina-complexa-v1",
        "status": "passed",
        "request_sha256": canonical_digest(request.to_dict()),
        "design_count": design_count,
        "atom_count": atom_count,
        "qualification_effect": "none-offline-validation-only",
    }


def collect_output(request_value: object, workspace: Path) -> CollectedOutput:
    """Collect upstream combined binder CSV rows and their referenced PDBs."""

    _request_value, parameters = _request(request_value)
    root = workspace.resolve(strict=True)
    csv_paths = sorted(root.rglob("RAW_*binder*_results_*_combined.csv"))
    if not csv_paths:
        csv_paths = sorted(root.rglob("binder_results_*.csv"))
    if not csv_paths:
        raise ScientificAdapterError("Proteina collector found no upstream binder result CSV")
    entries: list[tuple[str, str, Path, bool]] = []
    structure_paths: dict[Path, None] = {}
    for csv_index, csv_path in enumerate(csv_paths, start=1):
        fields, rows = parse_csv_artifact(
            csv_path.read_bytes(),
            label="Proteina upstream results",
            maximum_rows=parameters.num_samples,
        )
        if "pdb_path" not in fields:
            raise ScientificAdapterError("Proteina upstream result CSV has no pdb_path column")
        for row in rows:
            raw_path = Path(row["pdb_path"])
            candidate = raw_path if raw_path.is_absolute() else root / raw_path
            resolved = candidate.resolve(strict=True)
            if root not in resolved.parents or not resolved.is_file() or resolved.is_symlink():
                raise ScientificAdapterError("Proteina upstream CSV references a structure outside the workspace")
            structure_paths[resolved] = None
        entries.append((f"results.{csv_index}", "proteina-complexa-results-csv/v1", csv_path, True))
    if not 1 <= len(structure_paths) <= parameters.num_samples:
        raise ScientificAdapterError("Proteina collector structure count is outside the request bound")
    entries.extend(
        (f"structure.{index}", "protein-complex-structure/v1", path, False)
        for index, path in enumerate(structure_paths, start=1)
    )
    return collect_output_files(
        root,
        tuple(entries),
        manifest_id="proteina-complexa.results",
        maximum_total_bytes=MAX_OUTPUT_BYTES,
    )

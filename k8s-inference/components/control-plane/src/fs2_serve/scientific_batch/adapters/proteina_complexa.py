"""Proteina-Complexa v1.1.0-observed staged adapter.

The upstream repository has no release tag at the pinned commit.  The package
version is therefore descriptive only; the immutable identity is the commit
and the selected Hugging Face weight revision.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import (
    AdapterExecutionPlan,
    ArtifactMaterialization,
    MaterializationMode,
    RuntimeArtifactMount,
    RuntimeTreeBinding,
    ScientificInputArtifact,
    StageInvocation,
    StageWorkspaceDocument,
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
    run_workspace,
    safe_name,
    strict_object,
    structure_atom_count,
)
from .staged_workspace import (
    collect_workspace_handoff,
    completion_marker,
    materialize_collected_output,
    wrap_stage_argv,
)

if TYPE_CHECKING:
    from . import CollectedStageOutput

MODEL_ID = "proteina-complexa"
VARIANT_ID = "upstream-dev-20260827"
SOURCE_REPOSITORY = "NVIDIA-BioNeMo/Proteina-Complexa"
SOURCE_REVISION = "54058860d43444c7289873f77d3e50b5b02348cd"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/proteina-complexa-parameters/v1"
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024 * 1024
MAX_STAGE_HANDOFF_CONTENT_BYTES = MAX_OUTPUT_BYTES
MAX_STAGE_HANDOFF_BYTES = 3 * 1024 * 1024 * 1024
MAX_STAGE_HANDOFF_MEMBERS = 16_384
COLLECTOR_ID = "proteina-complexa-v1"
VALIDATOR_ID = "proteina-complexa-v1"
STAGE_HANDOFF_NAME = "stage-handoff"
STAGE_HANDOFF_SEMANTIC_TYPE = "proteina-complexa-workspace-handoff-tar/v1"
# Complexa/Hydra creates logs independently of scientific stage state.  In
# particular, the CPU filter creates an empty ``filter.log`` even after a
# successful run.  Keep handoffs bound to the directories consumed by the
# next stage instead of transporting incidental logs or other workspace data.
STAGE_HANDOFF_PATHS = {
    "generate": ("assets", "inference"),
    "filter": ("assets", "inference"),
    "evaluate": ("assets", "inference", "evaluation_results"),
}
PUBLIC_REQUEST_DOCUMENT = ".fs2/public-request.json"
REFERENCE_DATA_SUPPLEMENTAL_GROUP = 1000
REQUIRES_VERIFIED_INPUT_ARTIFACTS = True
INPUT_MANIFEST_MEDIA_TYPE = "application/vnd.fs2.scientific-manifest+json"
TARGET_BUNDLE_ID = "target-bundle"
TARGET_BUNDLE_SEMANTIC_TYPE = "proteina-complexa-target-bundle/v1"
TARGET_BUNDLE_MEDIA_TYPE = "application/x-tar"

AF2_ARTIFACT_ID = "alphafold2-params"
RF3_ARTIFACT_ID = "rosettafold3-checkpoint"

# AlphaFold2 parameters are published as one 5,587,968,000-byte tar and consumed
# as a directory of parameter files. ColabDesign resolves
# ``AF2_DIR/params/params_<model>.npz`` and then ``AF2_DIR/params_<model>.npz``,
# and upstream ``download_startup.sh`` expands the archive flat into AF2_DIR, so
# the canonical localized tree is the flat sixteen-entry set. The archive digest
# below is provenance only and never qualifies the mount; the inventory digest is
# the identity of the tree AF2_DIR points at.
AF2_ARCHIVE_SHA256 = "36d4b0220f3c735f3296d301152b738c9776d16981d054845a68a1370b26cfe3"
AF2_TREE_INVENTORY_SHA256 = "cdbb7c7c475442712c73f8f8ea40b42fb5dd4fb5c1bf81fdb4642ca9e27f5ac4"
AF2_TREE_ENTRY_COUNT = 16


def af2_tree_binding() -> RuntimeTreeBinding:
    """Bind the extracted AlphaFold2 parameter tree that AF2_DIR must name."""

    return RuntimeTreeBinding(
        artifact_id=AF2_ARTIFACT_ID,
        mount_path=model_root(AF2_ARTIFACT_ID),
        archive_sha256=AF2_ARCHIVE_SHA256,
        tree_inventory_sha256=AF2_TREE_INVENTORY_SHA256,
        entry_count=AF2_TREE_ENTRY_COUNT,
    )


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
        values.append(("AF2_DIR", af2_tree_binding().mount_path))
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
    ]
    if stage_id == "filter":
        # The upstream CLI otherwise suppresses the subprocess traceback, and
        # its implicit workspace setup asserts that CUDA is available. Point
        # the CPU-only filter at the deterministic workspace produced by the
        # generate stage so it skips that accelerator-only setup path. argparse
        # cannot resume the trailing ``overrides`` nargs="*" positional after
        # an optional follows ``config``, so --verbose must precede config.
        values.append("--verbose")
    values.append(variant.config)
    values.extend(
        (
            f"++run_name={parameters.run_name}",
            f"++generation.task_name={parameters.target_id}",
            f"++seed={parameters.seed}",
        )
    )
    if stage_id == "filter":
        config_stem = Path(variant.config).stem
        values.append(f"++root_path=./inference/{config_stem}_{parameters.target_id}_{parameters.run_name}")
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


def _stage_runtime_trees(parameters: ProteinaParameters, stage_id: str) -> tuple[RuntimeTreeBinding, ...]:
    """Every stage that mounts AlphaFold2 carries the exact tree it must contain."""

    if AF2_ARTIFACT_ID in _stage_runtime_artifacts(parameters, stage_id):
        return (af2_tree_binding(),)
    return ()


def _stage_runtime_mounts(parameters: ProteinaParameters, stage_id: str) -> tuple[RuntimeArtifactMount, ...]:
    """Name every external tree at the exact path consumed by the runtime."""

    return tuple(
        RuntimeArtifactMount(
            artifact_id=artifact_id,
            mount_path=model_root(artifact_id),
            supplemental_groups=(REFERENCE_DATA_SUPPLEMENTAL_GROUP,),
        )
        for artifact_id in _stage_runtime_artifacts(parameters, stage_id)
    )


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ScientificAdapterError("Proteina-Complexa request is not canonical JSON") from error


def _public_request_document(request: PublicRunRequest) -> StageWorkspaceDocument:
    return StageWorkspaceDocument(PUBLIC_REQUEST_DOCUMENT, _canonical_json(request.to_dict()))


def _load_public_request(workspace: Path) -> Mapping[str, object]:
    path = workspace.joinpath(*Path(PUBLIC_REQUEST_DOCUMENT).parts)
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ScientificAdapterError("Proteina-Complexa collector request document is unavailable") from error
    try:
        value = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ScientificAdapterError("Proteina-Complexa collector request document is invalid") from error
    if not isinstance(value, Mapping) or _canonical_json(value).encode() != content:
        raise ScientificAdapterError("Proteina-Complexa collector request document is not canonical")
    return value


def _target_bundle(
    request: PublicRunRequest,
    input_artifacts: tuple[ScientificInputArtifact, ...] | None,
) -> tuple[str, str | None]:
    """Bind the verified public manifest entry that seeds the campaign workspace."""

    if input_artifacts is None:
        if request.input_manifest.media_type == INPUT_MANIFEST_MEDIA_TYPE:
            raise ScientificAdapterError("Proteina-Complexa manifest requests require verified input_artifacts")
        if request.input_manifest.media_type != TARGET_BUNDLE_MEDIA_TYPE:
            raise ScientificAdapterError("Proteina-Complexa direct input must be an application/x-tar target bundle")
        if request.input_manifest.compression not in {"gzip", "zstd"}:
            raise ScientificAdapterError("Proteina-Complexa direct target bundle must use gzip or zstd compression")
        return request.input_manifest.artifact_id, request.input_manifest.compression

    if request.input_manifest.media_type != INPUT_MANIFEST_MEDIA_TYPE:
        raise ScientificAdapterError("Proteina-Complexa input_manifest must identify a scientific manifest")
    if request.input_manifest.compression not in {None, "none"}:
        raise ScientificAdapterError("Proteina-Complexa input manifest must not be compressed")
    if len(input_artifacts) != 1:
        raise ScientificAdapterError("Proteina-Complexa input manifest must contain exactly one target-bundle")
    target = input_artifacts[0]
    if target.logical_artifact_id != TARGET_BUNDLE_ID:
        raise ScientificAdapterError("Proteina-Complexa input manifest must contain target-bundle")
    if target.semantic_type != TARGET_BUNDLE_SEMANTIC_TYPE:
        raise ScientificAdapterError("Proteina-Complexa target-bundle semantic type is invalid")
    if target.media_type != TARGET_BUNDLE_MEDIA_TYPE or target.compression not in {"gzip", "zstd"}:
        raise ScientificAdapterError("Proteina-Complexa target-bundle media or compression type is invalid")
    if not 1 <= target.size_bytes <= MAX_INPUT_BYTES:
        raise ScientificAdapterError("Proteina-Complexa target-bundle size is outside the adapter bound")
    if str(target.artifact_id) == request.input_manifest.artifact_id:
        raise ScientificAdapterError("Proteina-Complexa target bundle and manifest must be distinct artifacts")
    return target.logical_artifact_id, target.compression


def compile_run(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
    input_artifacts: tuple[ScientificInputArtifact, ...] | None = None,
) -> AdapterExecutionPlan:
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
    target_bundle_id, target_bundle_compression = _target_bundle(request, input_artifacts)
    stage_ids = ("generate", "filter", "evaluate", "analyze")
    invocations: list[StageInvocation] = []
    previous = target_bundle_id
    workspace = run_workspace(MODEL_ID, operation_id, "main")
    for stage_id in stage_ids:
        output = logical_stage_artifact(operation_id, stage_id, "main")
        invocations.append(
            StageInvocation(
                stage_id=stage_id,
                shard_id="main",
                argv=wrap_stage_argv(workspace, _argv(parameters, stage_id)),
                environment=_environment(parameters, stage_id, workspace),
                working_directory=workspace,
                consumes=(previous,),
                produces=output,
                collector_id=COLLECTOR_ID,
                validator_id=VALIDATOR_ID,
                handoff_name=(STAGE_HANDOFF_NAME if stage_id != stage_ids[-1] else None),
                max_output_artifacts=(1 if stage_id != stage_ids[-1] else parameters.num_samples + 1),
                max_output_bytes=(MAX_STAGE_HANDOFF_BYTES if stage_id != stage_ids[-1] else MAX_OUTPUT_BYTES),
                materializations=(
                    ArtifactMaterialization(
                        artifact_id=previous,
                        destination=workspace,
                        mode=(
                            MaterializationMode.EXTRACT_TAR
                            if stage_id == "generate"
                            else MaterializationMode.OVERLAY_TAR
                        ),
                        compression=target_bundle_compression if stage_id == "generate" else "zstd",
                    ),
                ),
                runtime_artifacts=_stage_runtime_artifacts(parameters, stage_id),
                runtime_trees=_stage_runtime_trees(parameters, stage_id),
                runtime_mounts=_stage_runtime_mounts(parameters, stage_id),
                workspace_documents=(_public_request_document(request),),
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
                artifact_id for stage_id in stage_ids for artifact_id in _stage_runtime_artifacts(parameters, stage_id)
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


def collect_companion_output(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    """Collect one exact production stage after trusted zero-exit completion."""

    if invocation.collector_id != COLLECTOR_ID or invocation.validator_id != VALIDATOR_ID:
        raise ScientificAdapterError("Proteina-Complexa collector received another execution identity")
    if invocation.stage_id != "analyze":
        try:
            included_paths = STAGE_HANDOFF_PATHS[invocation.stage_id]
        except KeyError as error:
            raise ScientificAdapterError("Proteina-Complexa collector received an unsupported stage") from error
        return collect_workspace_handoff(
            invocation,
            workspace,
            label="ProteinaComplexa",
            name=STAGE_HANDOFF_NAME,
            semantic_type=STAGE_HANDOFF_SEMANTIC_TYPE,
            maximum_members=MAX_STAGE_HANDOFF_MEMBERS,
            maximum_content_bytes=MAX_STAGE_HANDOFF_CONTENT_BYTES,
            maximum_archive_bytes=MAX_STAGE_HANDOFF_BYTES,
            included_paths=included_paths,
        )
    if invocation.handoff_name is not None:
        raise ScientificAdapterError("Proteina-Complexa terminal stage declares an intermediate handoff")
    completion_sha256 = completion_marker(invocation, workspace, label="ProteinaComplexa")
    request = _load_public_request(workspace)
    collected = collect_output(request, workspace)
    semantic = validate_output(request, collected.manifest, artifact_loader=collected.blobs.__getitem__)
    return materialize_collected_output(
        invocation,
        workspace,
        collected,
        label="ProteinaComplexa",
        completion_sha256=completion_sha256,
        validation=semantic,
    )

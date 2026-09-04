"""Production controller adapter for the RFdiffusion v1.1.0 candidate."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from ..catalog_adapter import ScientificStageExpansion
from ..models import (
    AdapterExecutionPlan,
    ArtifactMaterialization,
    MaterializationMode,
    RuntimeArtifactMount,
    ScientificInputArtifact,
    StageInvocation,
    StageWorkspaceDocument,
)
from .common import (
    PublicRunRequest,
    ScientificAdapterError,
    assert_profile_identity,
    bounded_int,
    build_execution_plan,
    finite_number,
    logical_stage_artifact,
    parse_json_artifact,
    parse_public_request,
    run_workspace,
    strict_object,
)
from .staged_workspace import (
    atomic_publish,
    collect_workspace_handoff,
    completion_marker,
    contained_stable_file,
    wrap_stage_argv,
)
from .verified_input import verified_manifest_entry

if TYPE_CHECKING:
    from . import CollectedStageOutput

MODEL_ID = "rfdiffusion"
VARIANT_ID = "rfdiffusion-v1-1-0"
SOURCE_REPOSITORY = "RosettaCommons/RFdiffusion"
SOURCE_REVISION = "9273ef67335acaf91df0150473a274759229cdf6"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/rfdiffusion-parameters/v1"
RUNTIME_ADAPTER_ID = "rfdiffusion-v1-1-0-base-v1"
COLLECTOR_ID = VARIANT_ID
VALIDATOR_ID = VARIANT_ID
CHECKPOINT_ARTIFACT = "rfdiffusion-base-checkpoint"
CHECKPOINT_RUNTIME_ID = "artifact.rfdiffusion.base-ckpt"
CHECKPOINT_SHA256 = "0fcf7d7c32b4848030aca3a051e6768de194616f96ba6c38186351a33bfc6eca"
CHECKPOINT_BYTES = 483_616_107
CHECKPOINT_MOUNT = "/opt/fs2/artifacts/rfdiffusion-base-checkpoint"
DESIGN_INPUT_ID = "design_constraint"
DESIGN_INPUT_SEMANTIC_TYPE = "rfdiffusion-design-constraint/v1"
TARGET_INPUT_ID = "target_structure"
TARGET_INPUT_SEMANTIC_TYPE = "protein-structure-pdb/v1"
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_DESIGNS = 64
MAX_SEED = 1_000_000
MAX_HANDOFF_MEMBERS = 256
MAX_HANDOFF_CONTENT_BYTES = 240 * 1024 * 1024
MAX_HANDOFF_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_FINAL_BYTES = 8 * 1024 * 1024 * 1024
HANDOFF_NAME = "stage-handoff"
HANDOFF_SEMANTIC_TYPE = "rfdiffusion-inference-workspace-handoff/v1"
RUNTIME_ENTRYPOINT = "/opt/fs2/runtime_entrypoint.py"
REFERENCE_DATA_SUPPLEMENTAL_GROUP = 1000
REQUIRES_VERIFIED_INPUT_ARTIFACTS = True

_DIFFUSED_SPAN = re.compile(r"^(\d{1,5})-(\d{1,5})$")
_MOTIF_SPAN = re.compile(r"^([A-Za-z])(\d{1,5})-(\d{1,5})$")
_HOTSPOT = re.compile(r"^([A-Za-z])(\d{1,5})$")
_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_STANDARD_RESIDUES = frozenset(
    {
        "ALA",
        "ARG",
        "ASN",
        "ASP",
        "CYS",
        "GLN",
        "GLU",
        "GLY",
        "HIS",
        "ILE",
        "LEU",
        "LYS",
        "MET",
        "PHE",
        "PRO",
        "SER",
        "THR",
        "TRP",
        "TYR",
        "VAL",
    }
)


@dataclass(frozen=True, slots=True)
class Parameters:
    operation: str
    contigs: tuple[str, ...]
    num_designs: int
    seed: int
    diffuser_t: int
    length: int | None
    hotspots: tuple[str, ...]
    input_pdb_artifact_id: str | None
    motif_rmsd_limit: float
    minimum_residues: int
    maximum_residues: int
    motif_residues: int

    @classmethod
    def parse(cls, value: object) -> Parameters:
        item = strict_object(
            value,
            required=frozenset({"schema", "operation", "contigs", "num_designs", "seed", "diffuser_T"}),
            optional=frozenset({"length", "hotspot_residues", "input_pdb_artifact_id", "motif_ca_rmsd_limit"}),
            label="RFdiffusion parameters",
        )
        if item["schema"] != PARAMETER_SCHEMA:
            raise ScientificAdapterError("RFdiffusion parameter schema selects another backend")
        operation = item["operation"]
        if operation not in {"design-backbone", "scaffold-motif"}:
            raise ScientificAdapterError("RFdiffusion operation is unsupported")
        raw_contigs = item["contigs"]
        if not isinstance(raw_contigs, list) or not 1 <= len(raw_contigs) <= 4:
            raise ScientificAdapterError("RFdiffusion contigs must contain one to four groups")
        contigs: list[str] = []
        minimum_residues = 0
        maximum_residues = 0
        motif_residues = 0
        for group_index, raw_group in enumerate(raw_contigs):
            if not isinstance(raw_group, str) or not raw_group or len(raw_group) > 256:
                raise ScientificAdapterError(f"RFdiffusion contigs[{group_index}] is invalid")
            if raw_group != raw_group.strip():
                raise ScientificAdapterError("RFdiffusion contigs cannot contain surrounding whitespace")
            pieces = raw_group.split("/")
            if len(pieces) > 32:
                raise ScientificAdapterError("RFdiffusion contig group exceeds the segment bound")
            residues_in_group = 0
            for piece in pieces:
                if piece == "0":
                    continue
                motif = _MOTIF_SPAN.fullmatch(piece)
                if motif is not None:
                    start, end = int(motif.group(2)), int(motif.group(3))
                    if not 1 <= start <= end <= 99_999:
                        raise ScientificAdapterError("RFdiffusion motif span is reversed or out of bounds")
                    length = end - start + 1
                    minimum_residues += length
                    maximum_residues += length
                    motif_residues += length
                    residues_in_group += length
                    continue
                generated = _DIFFUSED_SPAN.fullmatch(piece)
                if generated is None:
                    raise ScientificAdapterError("RFdiffusion contig contains an unsupported Hydra token")
                lower, upper = int(generated.group(1)), int(generated.group(2))
                if not 1 <= lower <= upper:
                    raise ScientificAdapterError("RFdiffusion generated span is reversed or empty")
                minimum_residues += lower
                maximum_residues += upper
                residues_in_group += upper
            if residues_in_group == 0:
                raise ScientificAdapterError("RFdiffusion contig group contains no residues")
            contigs.append(raw_group)
        if maximum_residues > 512:
            raise ScientificAdapterError("RFdiffusion request exceeds 512 total residues")
        num_designs = bounded_int(item["num_designs"], minimum=1, maximum=MAX_DESIGNS, label="num_designs")
        seed = bounded_int(item["seed"], minimum=0, maximum=MAX_SEED, label="seed")
        if seed + num_designs - 1 > MAX_SEED:
            raise ScientificAdapterError("RFdiffusion seed range exceeds the deterministic design bound")
        diffuser_t = bounded_int(item["diffuser_T"], minimum=1, maximum=200, label="diffuser_T")
        raw_length = item.get("length")
        length_value = None if raw_length is None else bounded_int(raw_length, minimum=1, maximum=512, label="length")
        if length_value is not None and not minimum_residues <= length_value <= maximum_residues:
            raise ScientificAdapterError("RFdiffusion length must be reachable from the bounded contigs")
        raw_hotspots = item.get("hotspot_residues", [])
        if not isinstance(raw_hotspots, list) or len(raw_hotspots) > 64:
            raise ScientificAdapterError("RFdiffusion hotspot_residues exceeds the bound")
        hotspots: list[str] = []
        for value in raw_hotspots:
            if not isinstance(value, str) or _HOTSPOT.fullmatch(value) is None:
                raise ScientificAdapterError("RFdiffusion hotspot residue is invalid")
            hotspots.append(value.upper())
        if len(hotspots) != len(set(hotspots)):
            raise ScientificAdapterError("RFdiffusion hotspot residues must be unique")
        raw_pdb = item.get("input_pdb_artifact_id")
        if raw_pdb is not None and (not isinstance(raw_pdb, str) or _ARTIFACT_ID.fullmatch(raw_pdb) is None):
            raise ScientificAdapterError("RFdiffusion input_pdb_artifact_id is invalid")
        raw_rmsd = item.get("motif_ca_rmsd_limit", 1.5)
        motif_rmsd_limit = finite_number(
            raw_rmsd,
            minimum=0.000_001,
            maximum=10.0,
            label="motif_ca_rmsd_limit",
        )
        if operation == "scaffold-motif":
            if motif_residues == 0 or not raw_pdb:
                raise ScientificAdapterError("scaffold-motif requires motif spans and input_pdb_artifact_id")
        elif motif_residues or raw_pdb or hotspots:
            raise ScientificAdapterError("design-backbone cannot select motif, PDB, or hotspot inputs")
        return cls(
            operation=cast(str, operation),
            contigs=tuple(contigs),
            num_designs=num_designs,
            seed=seed,
            diffuser_t=diffuser_t,
            length=length_value,
            hotspots=tuple(hotspots),
            input_pdb_artifact_id=raw_pdb,
            motif_rmsd_limit=motif_rmsd_limit,
            minimum_residues=minimum_residues,
            maximum_residues=maximum_residues,
            motif_residues=motif_residues,
        )


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _runtime_request(
    request: Mapping[str, object],
    parameters: Parameters,
    *,
    index: int,
    target: ScientificInputArtifact | None,
) -> dict[str, object]:
    value = cast(dict[str, object], json.loads(json.dumps(request, allow_nan=False)))
    raw_parameters = cast(dict[str, object], value["parameters"])
    raw_parameters["num_designs"] = 1
    raw_parameters["seed"] = parameters.seed + index
    if target is not None:
        raw_parameters["input_pdb_artifact_id"] = str(target.artifact_id)
    return value


def _runtime_manifest(target: ScientificInputArtifact | None) -> dict[str, object]:
    entries: list[dict[str, object]] = [
        {
            "name": "base_checkpoint",
            "semantic_type": "rfdiffusion-checkpoint/v1",
            "artifact": {
                "artifact_id": CHECKPOINT_RUNTIME_ID,
                "sha256": CHECKPOINT_SHA256,
                "size_bytes": CHECKPOINT_BYTES,
                "media_type": "application/octet-stream",
                "compression": "none",
                "path": "Base_ckpt.pt",
            },
        }
    ]
    if target is not None:
        entries.append(
            {
                "name": TARGET_INPUT_ID,
                "semantic_type": TARGET_INPUT_SEMANTIC_TYPE,
                "artifact": {
                    "artifact_id": str(target.artifact_id),
                    "sha256": target.digest.removeprefix("sha256:"),
                    "size_bytes": target.size_bytes,
                    "media_type": target.media_type,
                    "compression": target.compression or "none",
                    "path": f"inputs/{target.artifact_id}",
                },
            }
        )
    return {
        "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
        "manifest_id": "manifest.rfdiffusion.controller-input",
        "entries": entries,
    }


def _select_input(
    request: PublicRunRequest,
    parameters: Parameters,
    input_artifacts: tuple[ScientificInputArtifact, ...] | None,
) -> ScientificInputArtifact:
    if parameters.operation == "design-backbone":
        return verified_manifest_entry(
            request,
            input_artifacts,
            logical_artifact_id=DESIGN_INPUT_ID,
            semantic_type=DESIGN_INPUT_SEMANTIC_TYPE,
            media_type="text/plain",
            compressions=frozenset({None, "none"}),
            maximum_bytes=64 * 1024,
            label="RFdiffusion",
        )
    return verified_manifest_entry(
        request,
        input_artifacts,
        logical_artifact_id=TARGET_INPUT_ID,
        semantic_type=TARGET_INPUT_SEMANTIC_TYPE,
        media_type="chemical/x-pdb",
        compressions=frozenset({None, "none"}),
        maximum_bytes=MAX_INPUT_BYTES,
        label="RFdiffusion",
    )


def compile_run(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
    input_artifacts: tuple[ScientificInputArtifact, ...] | None = None,
) -> AdapterExecutionPlan:
    """Compile a bounded request into per-design GPU work and CPU collection."""

    request = parse_public_request(request_value, maximum_input_bytes=MAX_INPUT_BYTES)
    parameters = Parameters.parse(request.parameters)
    if request.operation != parameters.operation:
        raise ScientificAdapterError("RFdiffusion public and parameter operations differ")
    selected_input = _select_input(request, parameters, input_artifacts)
    target = selected_input if parameters.operation == "scaffold-motif" else None
    assert_profile_identity(
        profile,
        model_id=MODEL_ID,
        repository=SOURCE_REPOSITORY,
        revision=SOURCE_REVISION,
        parameter_schema=PARAMETER_SCHEMA,
        request=request,
    )
    shard_ids = tuple(f"design-{index:03d}" for index in range(parameters.num_designs))
    expansions = {
        "inference": ScientificStageExpansion(shard_ids=shard_ids),
        "collect": ScientificStageExpansion(shard_ids=("main",)),
    }
    outputs: list[str] = []
    invocations: list[StageInvocation] = []
    for index, shard_id in enumerate(shard_ids):
        workspace = run_workspace(MODEL_ID, operation_id, shard_id)
        output = logical_stage_artifact(operation_id, "inference", shard_id)
        outputs.append(output)
        runtime_request = _runtime_request(request.to_dict(), parameters, index=index, target=target)
        request_document = _canonical_json(runtime_request)
        manifest_document = _canonical_json(_runtime_manifest(target))
        materialized_path = f"{workspace}/shards/{index:03d}/inputs/{selected_input.artifact_id}"
        command = (
            "python",
            RUNTIME_ENTRYPOINT,
            "run",
            "--request",
            f"{workspace}/.fs2/request.json",
            "--input-manifest",
            f"{workspace}/.fs2/input-manifest.json",
            "--output",
            f"{workspace}/shards/{index:03d}/result",
            "--artifact-root",
            CHECKPOINT_MOUNT,
            "--input-artifact-root",
            f"{workspace}/shards/{index:03d}",
            "--checkpoint-artifact-id",
            CHECKPOINT_RUNTIME_ID,
            "--scratch",
            f"{workspace}/shards/{index:03d}/scratch",
            "--timeout-seconds",
            "21600",
            "--cache-level",
            "artifact-local",
        )
        invocations.append(
            StageInvocation(
                stage_id="inference",
                shard_id=shard_id,
                argv=wrap_stage_argv(workspace, command),
                environment=(
                    ("FS2_INPUT_ARTIFACT_ROOT", f"{workspace}/shards/{index:03d}"),
                    ("FS2_RFDIFFUSION_SEED", str(parameters.seed + index)),
                    ("FS2_RFDIFFUSION_HOME", "/opt/rfdiffusion"),
                    ("HF_HUB_OFFLINE", "1"),
                    ("TRANSFORMERS_OFFLINE", "1"),
                ),
                working_directory=workspace,
                consumes=(selected_input.logical_artifact_id,),
                produces=output,
                collector_id=COLLECTOR_ID,
                validator_id=VALIDATOR_ID,
                handoff_name=HANDOFF_NAME,
                max_output_artifacts=1,
                max_output_bytes=MAX_HANDOFF_ARCHIVE_BYTES,
                materializations=(
                    ArtifactMaterialization(
                        artifact_id=selected_input.logical_artifact_id,
                        destination=materialized_path,
                        mode=MaterializationMode.COPY_FILE,
                        compression=selected_input.compression,
                    ),
                ),
                runtime_artifacts=(CHECKPOINT_ARTIFACT,),
                runtime_mounts=(
                    RuntimeArtifactMount(
                        artifact_id=CHECKPOINT_ARTIFACT,
                        mount_path=CHECKPOINT_MOUNT,
                        supplemental_groups=(REFERENCE_DATA_SUPPLEMENTAL_GROUP,),
                    ),
                ),
                workspace_documents=(
                    StageWorkspaceDocument(".fs2/request.json", request_document),
                    StageWorkspaceDocument(".fs2/input-manifest.json", manifest_document),
                ),
            )
        )

    collect_workspace = run_workspace(MODEL_ID, operation_id, "collect-main")
    collect_request = _canonical_json(request.to_dict())
    collect_consumes = tuple(outputs) + ((target.logical_artifact_id,) if target is not None else ())
    collect_materializations = tuple(
        ArtifactMaterialization(
            artifact_id=output,
            destination=collect_workspace,
            mode=MaterializationMode.OVERLAY_TAR,
            compression="zstd",
        )
        for output in outputs
    )
    if target is not None:
        collect_materializations = (
            *collect_materializations,
            ArtifactMaterialization(
                artifact_id=target.logical_artifact_id,
                destination=f"{collect_workspace}/inputs/{target.artifact_id}",
                mode=MaterializationMode.COPY_FILE,
                compression=target.compression,
            ),
        )
    invocations.append(
        StageInvocation(
            stage_id="collect",
            shard_id="main",
            argv=wrap_stage_argv(collect_workspace, ("python", "--version")),
            environment=(
                ("FS2_RFDIFFUSION_DESIGN_COUNT", str(parameters.num_designs)),
                ("FS2_RFDIFFUSION_REQUEST_SHA256", hashlib.sha256(collect_request.encode()).hexdigest()),
            ),
            working_directory=collect_workspace,
            consumes=collect_consumes,
            produces=logical_stage_artifact(operation_id, "collect", "main"),
            collector_id=COLLECTOR_ID,
            validator_id=VALIDATOR_ID,
            max_output_artifacts=2 * parameters.num_designs,
            max_output_bytes=MAX_FINAL_BYTES,
            materializations=collect_materializations,
            workspace_documents=(
                StageWorkspaceDocument(".fs2/request.json", collect_request),
                StageWorkspaceDocument(".fs2/input-manifest.json", _canonical_json(_runtime_manifest(target))),
            ),
        )
    )
    return build_execution_plan(
        model_id=MODEL_ID,
        variant_id=VARIANT_ID,
        source_revision=SOURCE_REVISION,
        request=request,
        profile=profile,
        expansions=expansions,
        invocations=tuple(invocations),
        required_model_artifacts=(CHECKPOINT_ARTIFACT,),
    )


def _frozen_document(
    invocation: StageInvocation,
    workspace: Path,
    relative_path: str,
    *,
    label: str,
) -> bytes:
    documents = tuple(item for item in invocation.workspace_documents if item.relative_path == relative_path)
    if len(documents) != 1:
        raise ScientificAdapterError(f"{label} is absent from the frozen invocation")
    _path, content = contained_stable_file(
        workspace,
        relative_path,
        maximum_bytes=1024 * 1024,
        label=label,
    )
    if content != documents[0].canonical_json.encode():
        raise ScientificAdapterError(f"{label} differs from the frozen invocation")
    return content


def _request_from_collection(
    invocation: StageInvocation,
    workspace: Path,
) -> tuple[Mapping[str, object], Parameters]:
    content = _frozen_document(
        invocation,
        workspace,
        ".fs2/request.json",
        label="RFdiffusion frozen request",
    )
    request_value = parse_json_artifact(content, label="RFdiffusion frozen request")
    request = parse_public_request(request_value, maximum_input_bytes=MAX_INPUT_BYTES)
    parameters = Parameters.parse(request.parameters)
    if request.operation != parameters.operation:
        raise ScientificAdapterError("RFdiffusion collected request operation differs")
    return request.to_dict(), parameters


def _environment_int(invocation: StageInvocation, name: str, *, minimum: int, maximum: int) -> int:
    try:
        value = int(dict(invocation.environment)[name])
    except (KeyError, ValueError) as error:
        raise ScientificAdapterError(f"RFdiffusion invocation has no valid {name}") from error
    return bounded_int(value, minimum=minimum, maximum=maximum, label=name)


def _relative_output_path(value: object, *, prefix: str, label: str) -> str:
    if not isinstance(value, str):
        raise ScientificAdapterError(f"{label} path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or "\\" in value:
        raise ScientificAdapterError(f"{label} path escapes its shard output")
    return (PurePosixPath(prefix) / path).as_posix()


@dataclass(frozen=True, slots=True)
class PdbEvidence:
    residue_count: int
    atom_count: int
    chains: tuple[str, ...]
    residue_names: tuple[str, ...]
    residue_keys: frozenset[tuple[str, int]]


def _pdb_evidence(content: bytes, *, label: str) -> PdbEvidence:
    """Re-derive a physical complete backbone from bounded PDB bytes."""

    try:
        lines = content.decode("ascii").splitlines()
    except UnicodeError as error:
        raise ScientificAdapterError(f"{label} is not ASCII PDB") from error
    residues: dict[tuple[str, int, str], tuple[str, dict[str, tuple[float, float, float]]]] = {}
    order: list[tuple[str, int, str]] = []
    points: list[tuple[float, float, float]] = []
    for line in lines:
        if not line.startswith("ATOM"):
            continue
        if len(line) < 54:
            raise ScientificAdapterError(f"{label} contains a truncated ATOM record")
        chain = line[21:22].strip()
        residue_name = line[17:20].strip()
        atom_name = line[12:16].strip()
        try:
            residue_number = int(line[22:26])
            point = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
        except ValueError as error:
            raise ScientificAdapterError(f"{label} has invalid residue or coordinate data") from error
        if (
            not chain
            or residue_name not in _STANDARD_RESIDUES
            or not atom_name
            or not all(math.isfinite(value) for value in point)
        ):
            raise ScientificAdapterError(f"{label} has an invalid protein ATOM record")
        key = (chain, residue_number, line[26:27].strip())
        if key not in residues:
            residues[key] = (residue_name, {})
            order.append(key)
        recorded_name, atoms = residues[key]
        if recorded_name != residue_name or atom_name in atoms:
            raise ScientificAdapterError(f"{label} changes a residue or duplicates an atom")
        atoms[atom_name] = point
        points.append(point)
    if not residues:
        raise ScientificAdapterError(f"{label} contains no protein residues")
    if any(not {"N", "CA", "C"}.issubset(atoms) for _name, atoms in residues.values()):
        raise ScientificAdapterError(f"{label} lacks a complete N/CA/C backbone")
    extent = max(max(point[axis] for point in points) - min(point[axis] for point in points) for axis in range(3))
    if extent < 5.0:
        raise ScientificAdapterError(f"{label} coordinates are degenerate")
    for previous, current in zip(order, order[1:], strict=False):
        if previous[0] != current[0]:
            continue
        distance = math.dist(residues[previous][1]["CA"], residues[current][1]["CA"])
        if not 3.4 <= distance <= 4.2:
            raise ScientificAdapterError(f"{label} contains a non-physical CA-CA distance")
    return PdbEvidence(
        residue_count=len(residues),
        atom_count=len(points),
        chains=tuple(sorted({key[0] for key in residues})),
        residue_names=tuple(residues[key][0] for key in order),
        residue_keys=frozenset((key[0], key[1]) for key in residues),
    )


@dataclass(frozen=True, slots=True)
class MotifInputEvidence:
    artifact_id: str
    sha256: str
    size_bytes: int
    structure: PdbEvidence


def _motif_input(
    invocation: StageInvocation,
    workspace: Path,
    parameters: Parameters,
) -> MotifInputEvidence | None:
    """Verify the independently materialized motif source and requested sites."""

    if parameters.operation != "scaffold-motif":
        if any(item.artifact_id == TARGET_INPUT_ID for item in invocation.materializations):
            raise ScientificAdapterError("RFdiffusion backbone collection unexpectedly materializes a motif target")
        return None
    content = _frozen_document(
        invocation,
        workspace,
        ".fs2/input-manifest.json",
        label="RFdiffusion frozen input manifest",
    )
    manifest = strict_object(
        parse_json_artifact(content, label="RFdiffusion frozen input manifest"),
        required=frozenset({"schema", "manifest_id", "entries"}),
        label="RFdiffusion frozen input manifest",
    )
    entries = manifest["entries"]
    if (
        manifest["schema"] != "fs2-serve.nebius.ai/scientific-artifact-manifest/v1"
        or manifest["manifest_id"] != "manifest.rfdiffusion.controller-input"
        or not isinstance(entries, list)
        or len(entries) != 2
    ):
        raise ScientificAdapterError("RFdiffusion motif input manifest identity differs")
    by_name: dict[str, Mapping[str, object]] = {}
    for index, raw in enumerate(entries):
        entry = strict_object(
            raw,
            required=frozenset({"name", "semantic_type", "artifact"}),
            label=f"RFdiffusion input entries[{index}]",
        )
        name = entry["name"]
        if not isinstance(name, str) or name in by_name:
            raise ScientificAdapterError("RFdiffusion input manifest names are invalid")
        by_name[name] = entry
    if set(by_name) != {"base_checkpoint", TARGET_INPUT_ID}:
        raise ScientificAdapterError("RFdiffusion motif input manifest entries differ")
    if by_name["base_checkpoint"] != {
        "name": "base_checkpoint",
        "semantic_type": "rfdiffusion-checkpoint/v1",
        "artifact": {
            "artifact_id": CHECKPOINT_RUNTIME_ID,
            "sha256": CHECKPOINT_SHA256,
            "size_bytes": CHECKPOINT_BYTES,
            "media_type": "application/octet-stream",
            "compression": "none",
            "path": "Base_ckpt.pt",
        },
    }:
        raise ScientificAdapterError("RFdiffusion frozen checkpoint pointer differs")
    target_entry = by_name[TARGET_INPUT_ID]
    if target_entry["semantic_type"] != TARGET_INPUT_SEMANTIC_TYPE:
        raise ScientificAdapterError("RFdiffusion motif target semantic type differs")
    pointer = strict_object(
        target_entry["artifact"],
        required=frozenset({"artifact_id", "sha256", "size_bytes", "media_type", "compression", "path"}),
        label="RFdiffusion motif target pointer",
    )
    artifact_id = pointer["artifact_id"]
    sha256 = pointer["sha256"]
    size_bytes = pointer["size_bytes"]
    if (
        not isinstance(artifact_id, str)
        or _ARTIFACT_ID.fullmatch(artifact_id) is None
        or not isinstance(sha256, str)
        or re.fullmatch(r"[a-f0-9]{64}", sha256) is None
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 1 <= size_bytes <= MAX_INPUT_BYTES
        or pointer["media_type"] != "chemical/x-pdb"
        or pointer["compression"] != "none"
        or pointer["path"] != f"inputs/{artifact_id}"
    ):
        raise ScientificAdapterError("RFdiffusion motif target pointer differs")
    materializations = tuple(item for item in invocation.materializations if item.artifact_id == TARGET_INPUT_ID)
    if len(materializations) != 1 or materializations[0].destination != (
        f"{invocation.working_directory}/inputs/{artifact_id}"
    ):
        raise ScientificAdapterError("RFdiffusion motif target materialization differs")
    _path, target_content = contained_stable_file(
        workspace,
        f"inputs/{artifact_id}",
        maximum_bytes=MAX_INPUT_BYTES,
        label="RFdiffusion motif target",
    )
    if len(target_content) != size_bytes or hashlib.sha256(target_content).hexdigest() != sha256:
        raise ScientificAdapterError("RFdiffusion motif target bytes differ from the frozen pointer")
    structure = _pdb_evidence(target_content, label="RFdiffusion motif target")
    required_sites: set[tuple[str, int]] = set()
    for group in parameters.contigs:
        for piece in group.split("/"):
            motif = _MOTIF_SPAN.fullmatch(piece)
            if motif is not None:
                required_sites.update(
                    (motif.group(1).upper(), residue) for residue in range(int(motif.group(2)), int(motif.group(3)) + 1)
                )
    for hotspot in parameters.hotspots:
        match = _HOTSPOT.fullmatch(hotspot)
        assert match is not None
        required_sites.add((match.group(1).upper(), int(match.group(2))))
    if not required_sites.issubset(structure.residue_keys):
        raise ScientificAdapterError("RFdiffusion motif or hotspot site is absent from the verified target")
    return MotifInputEvidence(
        artifact_id=artifact_id,
        sha256=sha256,
        size_bytes=size_bytes,
        structure=structure,
    )


def _expected_upstream_argv(
    invocation: StageInvocation,
    *,
    index: int,
    parameters: Parameters,
    motif_input: MotifInputEvidence | None,
) -> tuple[str, ...]:
    producer = PurePosixPath(invocation.working_directory).parent / f"design-{index:03d}"
    shard = producer / "shards" / f"{index:03d}"
    result = shard / "result"
    scratch = shard / "scratch"
    argv = [
        "/opt/conda/bin/python",
        "/opt/rfdiffusion/scripts/run_inference.py",
        f"inference.output_prefix={result}/designs/design",
        f"inference.ckpt_override_path={CHECKPOINT_MOUNT}/Base_ckpt.pt",
        "inference.num_designs=1",
        f"inference.design_startnum={parameters.seed + index}",
        "inference.deterministic=True",
        f"diffuser.T={parameters.diffuser_t}",
        f"contigmap.contigs=[{','.join(parameters.contigs)}]",
        f"inference.schedule_directory_path={scratch}/schedules",
        f"hydra.run.dir={scratch}/hydra",
        "hydra.output_subdir=null",
    ]
    if parameters.length is not None:
        argv.append(f"contigmap.length={parameters.length}-{parameters.length}")
    if motif_input is not None:
        argv.append(f"inference.input_pdb={shard}/inputs/{motif_input.artifact_id}")
    if parameters.hotspots:
        argv.append("ppi.hotspot_res=[" + ",".join(parameters.hotspots) + "]")
    return tuple(argv)


def _validate_result(
    invocation: StageInvocation,
    workspace: Path,
    *,
    index: int,
    parameters: Parameters,
    motif_input: MotifInputEvidence | None,
) -> tuple[Path, bytes, Mapping[str, object]]:
    prefix = f"shards/{index:03d}/result"
    _path, content = contained_stable_file(
        workspace,
        f"{prefix}/result.json",
        maximum_bytes=16 * 1024 * 1024,
        label=f"RFdiffusion result {index}",
    )
    result = parse_json_artifact(content, label=f"RFdiffusion result {index}")
    expected_seed = parameters.seed + index
    if (
        result.get("schema") != "fs2-serve.nebius.ai/scientific-run-result/v1"
        or result.get("model_id") != MODEL_ID
        or result.get("adapter_id") != RUNTIME_ADAPTER_ID
        or result.get("status") != "succeeded"
        or result.get("operation") != parameters.operation
    ):
        raise ScientificAdapterError("RFdiffusion terminal result identity or status differs")
    checkpoint = result.get("checkpoint")
    if not isinstance(checkpoint, Mapping) or {
        "artifact_id": checkpoint.get("artifact_id"),
        "path": checkpoint.get("path"),
        "sha256": checkpoint.get("sha256"),
        "size_bytes": checkpoint.get("size_bytes"),
        "digest_verified": checkpoint.get("digest_verified"),
    } != {
        "artifact_id": CHECKPOINT_RUNTIME_ID,
        "path": f"{CHECKPOINT_MOUNT}/Base_ckpt.pt",
        "sha256": CHECKPOINT_SHA256,
        "size_bytes": CHECKPOINT_BYTES,
        "digest_verified": True,
    }:
        raise ScientificAdapterError("RFdiffusion checkpoint evidence differs")
    runtime_request = result.get("request")
    expected_request = {
        "operation": parameters.operation,
        "contigs": list(parameters.contigs),
        "num_designs": 1,
        "seed": expected_seed,
        "diffuser_T": parameters.diffuser_t,
        "length": parameters.length,
        "hotspot_residues": list(parameters.hotspots),
        "requested_residues": {
            "minimum": parameters.minimum_residues,
            "maximum": parameters.maximum_residues,
        },
    }
    if runtime_request != expected_request:
        raise ScientificAdapterError("RFdiffusion runtime request differs from the frozen shard")
    input_pdb = result.get("input_pdb")
    if motif_input is None:
        if input_pdb is not None:
            raise ScientificAdapterError("RFdiffusion backbone-only result claims a motif input")
    elif not isinstance(input_pdb, Mapping) or dict(input_pdb) != {
        "artifact_id": motif_input.artifact_id,
        "sha256": motif_input.sha256,
        "size_bytes": motif_input.size_bytes,
        "residue_count": motif_input.structure.residue_count,
    }:
        raise ScientificAdapterError("RFdiffusion motif input evidence differs")
    accelerator = result.get("accelerator")
    devices = accelerator.get("devices") if isinstance(accelerator, Mapping) else None
    if (
        not isinstance(accelerator, Mapping)
        or accelerator.get("cuda_execution_confirmed") is not True
        or not isinstance(devices, list)
        or not devices
        or any(
            not isinstance(device, str) or not device or len(device) > 256 or "cpu" in device.lower()
            for device in devices
        )
        or len(devices) != len(set(devices))
        or accelerator.get("evidence") != "upstream .trb run metadata records torch.cuda.get_device_name()"
    ):
        raise ScientificAdapterError("RFdiffusion result has no CUDA execution evidence")
    if result.get("shell_free") is not True or result.get("upstream_argv") != list(
        _expected_upstream_argv(
            invocation,
            index=index,
            parameters=parameters,
            motif_input=motif_input,
        )
    ):
        raise ScientificAdapterError("RFdiffusion upstream argv differs from the frozen shell-free projection")
    producer = PurePosixPath(invocation.working_directory).parent / f"design-{index:03d}"
    expected_log = producer / "shards" / f"{index:03d}" / "result" / "upstream.log"
    upstream = result.get("upstream")
    if not isinstance(upstream, Mapping) or set(upstream) != {
        "returncode",
        "log_path",
        "model_ready_seconds",
    }:
        raise ScientificAdapterError("RFdiffusion upstream completion evidence differs")
    ready_seconds = finite_number(
        upstream.get("model_ready_seconds"),
        minimum=0.0,
        maximum=7 * 24 * 3600,
        label="RFdiffusion model_ready_seconds",
    )
    if upstream.get("returncode") != 0 or upstream.get("log_path") != str(expected_log):
        raise ScientificAdapterError("RFdiffusion upstream completion evidence differs")
    cache = result.get("cache_level")
    if not isinstance(cache, Mapping) or {
        "declared": cache.get("declared"),
        "source": cache.get("source"),
        "gpu_snapshot_used": cache.get("gpu_snapshot_used"),
    } != {
        "declared": "artifact-local",
        "source": "submitter-declared",
        "gpu_snapshot_used": False,
    }:
        raise ScientificAdapterError("RFdiffusion cache execution evidence differs")
    cache_note = cache.get("note")
    if not isinstance(cache_note, str) or not 1 <= len(cache_note) <= 1024:
        raise ScientificAdapterError("RFdiffusion cache execution note is invalid")
    designs = result.get("designs")
    if not isinstance(designs, list) or len(designs) != 1 or not isinstance(designs[0], Mapping):
        raise ScientificAdapterError("RFdiffusion shard must contain exactly one design")
    design = cast(Mapping[str, object], designs[0])
    pdb = design.get("pdb")
    chains = design.get("chains")
    required_minimum = parameters.length if parameters.length is not None else parameters.minimum_residues
    required_maximum = parameters.length if parameters.length is not None else parameters.maximum_residues
    if (
        design.get("design_index") != expected_seed
        or design.get("seed") != expected_seed
        or not isinstance(pdb, Mapping)
        or design.get("device") not in devices
        or isinstance(design.get("residue_count"), bool)
        or not isinstance(design.get("residue_count"), int)
        or not required_minimum <= cast(int, design["residue_count"]) <= required_maximum
        or not isinstance(chains, list)
        or not chains
        or any(not isinstance(chain, str) or len(chain) != 1 or not chain.strip() for chain in chains)
        or len(chains) != len(set(chains))
    ):
        raise ScientificAdapterError("RFdiffusion design identity or structural cardinality differs")
    relative_pdb = _relative_output_path(pdb.get("path"), prefix=prefix, label="RFdiffusion PDB")
    pdb_path, pdb_content = contained_stable_file(
        workspace,
        relative_pdb,
        maximum_bytes=256 * 1024 * 1024,
        label=f"RFdiffusion structure {index}",
    )
    if pdb.get("sha256") != hashlib.sha256(pdb_content).hexdigest() or pdb.get("size_bytes") != len(pdb_content):
        raise ScientificAdapterError("RFdiffusion PDB differs from its result pointer")
    structure = _pdb_evidence(pdb_content, label=f"RFdiffusion structure {index}")
    if (
        structure.atom_count < cast(int, design["residue_count"]) * 3
        or structure.residue_count != design["residue_count"]
        or structure.chains != tuple(sorted(cast(list[str], chains)))
    ):
        raise ScientificAdapterError("RFdiffusion PDB lacks a complete atom set")
    rmsd = design.get("motif_ca_rmsd_angstrom")
    preserved = design.get("motif_positions_preserved")
    superposition = design.get("motif_superposition")
    if parameters.operation == "scaffold-motif":
        if not isinstance(superposition, Mapping):
            raise ScientificAdapterError("RFdiffusion motif preservation evidence differs")
        fit = strict_object(
            superposition,
            required=frozenset(
                {
                    "method",
                    "rmsd_angstrom",
                    "rmsd_unaligned_angstrom",
                    "rigid_body_rotation_degrees",
                    "rigid_body_translation_angstrom",
                    "note",
                }
            ),
            label="RFdiffusion motif superposition",
        )
        if (
            isinstance(preserved, bool)
            or not isinstance(preserved, int)
            or preserved != parameters.motif_residues
            or finite_number(rmsd, minimum=0.0, maximum=parameters.motif_rmsd_limit, label="motif RMSD")
            > parameters.motif_rmsd_limit
            or fit["method"] != "horn-quaternion-optimal-superposition"
            or finite_number(
                fit["rmsd_angstrom"],
                minimum=0.0,
                maximum=parameters.motif_rmsd_limit,
                label="motif superposition RMSD",
            )
            != rmsd
        ):
            raise ScientificAdapterError("RFdiffusion motif preservation evidence differs")
        finite_number(
            fit["rmsd_unaligned_angstrom"],
            minimum=0.0,
            maximum=1_000_000.0,
            label="motif unaligned RMSD",
        )
        finite_number(
            fit["rigid_body_rotation_degrees"],
            minimum=0.0,
            maximum=360.0,
            label="motif rigid-body rotation",
        )
        finite_number(
            fit["rigid_body_translation_angstrom"],
            minimum=0.0,
            maximum=1_000_000.0,
            label="motif rigid-body translation",
        )
        if not isinstance(fit["note"], str) or not 1 <= len(fit["note"]) <= 1024:
            raise ScientificAdapterError("RFdiffusion motif superposition note is invalid")
    elif rmsd is not None or preserved is not None or superposition is not None:
        raise ScientificAdapterError("RFdiffusion backbone-only result claims motif evidence")
    elif set(structure.residue_names) != {"GLY"}:
        raise ScientificAdapterError("RFdiffusion unconditional structure is not the expected glycine backbone")
    finite_number(
        design.get("upstream_seconds"),
        minimum=0.0,
        maximum=7 * 24 * 3600,
        label="RFdiffusion upstream_seconds",
    )
    phases = result.get("phases_seconds")
    expected_phases = {
        "validate_request",
        "resolve_checkpoint",
        "resolve_inputs",
        "upstream_execute",
        "verify_artifacts",
        "write_envelope",
    }
    if not isinstance(phases, Mapping) or set(phases) != expected_phases:
        raise ScientificAdapterError("RFdiffusion phase evidence differs")
    for phase, seconds in phases.items():
        finite_number(
            seconds,
            minimum=0.0,
            maximum=7 * 24 * 3600,
            label=f"RFdiffusion {phase} seconds",
        )
    total_seconds = result.get("total_seconds")
    parsed_total = finite_number(
        total_seconds,
        minimum=0.0,
        maximum=7 * 24 * 3600,
        label="RFdiffusion total_seconds",
    )
    if ready_seconds > parsed_total or sum(float(value) for value in phases.values()) > parsed_total + 0.01:
        raise ScientificAdapterError("RFdiffusion phase timings exceed the terminal duration")
    return pdb_path, pdb_content, result


def _sanitize_result(result: Mapping[str, object], *, index: int) -> bytes:
    checkpoint = cast(Mapping[str, object], result["checkpoint"])
    accelerator = cast(Mapping[str, object], result["accelerator"])
    upstream = cast(Mapping[str, object], result["upstream"])
    cache = cast(Mapping[str, object], result["cache_level"])
    design = cast(Mapping[str, object], cast(list[object], result["designs"])[0])
    pdb = cast(Mapping[str, object], design["pdb"])
    raw_fit = design.get("motif_superposition")
    fit = cast(Mapping[str, object], raw_fit) if isinstance(raw_fit, Mapping) else None
    value = {
        "schema": "fs2-serve.nebius.ai/rfdiffusion-design-result/v1",
        "model_id": MODEL_ID,
        "adapter_id": RUNTIME_ADAPTER_ID,
        "operation": result["operation"],
        "status": "succeeded",
        "request": result["request"],
        "checkpoint": {
            "artifact_id": checkpoint["artifact_id"],
            "sha256": checkpoint["sha256"],
            "size_bytes": checkpoint["size_bytes"],
            "digest_verified": checkpoint["digest_verified"],
        },
        "accelerator": {
            "devices": accelerator["devices"],
            "cuda_execution_confirmed": accelerator["cuda_execution_confirmed"],
        },
        "cache_level": {
            "declared": cache["declared"],
            "gpu_snapshot_used": cache["gpu_snapshot_used"],
        },
        "model_ready_seconds": upstream["model_ready_seconds"],
        "design": {
            "design_index": design["design_index"],
            "seed": design["seed"],
            "pdb": {
                "name": f"design-{index:03d}.pdb",
                "sha256": pdb["sha256"],
                "size_bytes": pdb["size_bytes"],
            },
            "residue_count": design["residue_count"],
            "chains": design["chains"],
            "device": design["device"],
            "upstream_seconds": design["upstream_seconds"],
            "motif_positions_preserved": design.get("motif_positions_preserved"),
            "motif_ca_rmsd_angstrom": design.get("motif_ca_rmsd_angstrom"),
            "motif_superposition": (
                None
                if fit is None
                else {
                    "method": fit["method"],
                    "rmsd_angstrom": fit["rmsd_angstrom"],
                    "rmsd_unaligned_angstrom": fit["rmsd_unaligned_angstrom"],
                    "rigid_body_rotation_degrees": fit["rigid_body_rotation_degrees"],
                    "rigid_body_translation_angstrom": fit["rigid_body_translation_angstrom"],
                }
            ),
        },
        "phases_seconds": result["phases_seconds"],
        "total_seconds": result["total_seconds"],
    }
    return _canonical_json(value).encode()


def _collect_final(invocation: StageInvocation, workspace: Path, *, completion_sha256: str) -> CollectedStageOutput:
    from . import CollectedArtifactFile, CollectedStageOutput

    if invocation.stage_id != "collect" or invocation.handoff_name is not None:
        raise ScientificAdapterError("RFdiffusion result collector received another stage contract")
    request, parameters = _request_from_collection(invocation, workspace)
    expected_request_sha256 = dict(invocation.environment).get("FS2_RFDIFFUSION_REQUEST_SHA256", "")
    if hashlib.sha256(_canonical_json(request).encode()).hexdigest() != expected_request_sha256:
        raise ScientificAdapterError("RFdiffusion collection request digest differs")
    count = _environment_int(invocation, "FS2_RFDIFFUSION_DESIGN_COUNT", minimum=1, maximum=MAX_DESIGNS)
    if count != parameters.num_designs:
        raise ScientificAdapterError("RFdiffusion invocation count differs from the request")
    motif_input = _motif_input(invocation, workspace, parameters)
    artifacts: list[CollectedArtifactFile] = []
    total_bytes = 0
    model_ready_seconds: list[float] = []
    for index in range(count):
        pdb_path, pdb_content, result = _validate_result(
            invocation,
            workspace,
            index=index,
            parameters=parameters,
            motif_input=motif_input,
        )
        sanitized = _sanitize_result(result, index=index)
        model_ready_seconds.append(
            float(cast(int | float, cast(Mapping[str, object], result["upstream"])["model_ready_seconds"]))
        )
        summary_path = workspace / ".fs2" / f"rfdiffusion-design-{index:03d}-result.json"
        atomic_publish(summary_path, sanitized, workspace=workspace, label="RFdiffusion result summary")
        total_bytes += len(pdb_content) + len(sanitized)
        if total_bytes > invocation.max_output_bytes:
            raise ScientificAdapterError("RFdiffusion final artifacts exceed the invocation bound")
        artifacts.extend(
            (
                CollectedArtifactFile(
                    name=f"design-{index:03d}-result",
                    semantic_type="rfdiffusion-design-result-json/v1",
                    path=summary_path,
                    media_type="application/json",
                ),
                CollectedArtifactFile(
                    name=f"design-{index:03d}-structure",
                    semantic_type="protein-structure-pdb/v1",
                    path=pdb_path,
                    media_type="chemical/x-pdb",
                ),
            )
        )
    if len(artifacts) != invocation.max_output_artifacts:
        raise ScientificAdapterError("RFdiffusion final artifact cardinality differs")
    validation = {
        "schema": "fs2-serve.nebius.ai/rfdiffusion-semantic-validation/v1",
        "validator_id": invocation.validator_id,
        "status": "passed",
        "request_sha256": expected_request_sha256,
        "operation": parameters.operation,
        "design_count": count,
        "maximum_model_ready_seconds": max(model_ready_seconds),
        "total_model_ready_seconds": sum(model_ready_seconds),
    }
    validation_payload = _canonical_json(validation).encode()
    receipt = workspace / ".fs2/rfdiffusion-semantic-validation.json"
    atomic_publish(receipt, validation_payload, workspace=workspace, label="RFdiffusion validation receipt")
    return CollectedStageOutput(
        artifacts=tuple(artifacts),
        validation={
            **validation,
            "completion_marker_sha256": completion_sha256,
            "validation_sha256": hashlib.sha256(validation_payload).hexdigest(),
        },
    )


def collect_companion_output(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    """Collect one exact RFdiffusion stage through the global companion."""

    if invocation.collector_id != COLLECTOR_ID or invocation.validator_id != VALIDATOR_ID:
        raise ScientificAdapterError("RFdiffusion collector received another execution identity")
    if invocation.stage_id == "inference":
        try:
            design_index = int(invocation.shard_id.removeprefix("design-"))
        except ValueError as error:
            raise ScientificAdapterError("RFdiffusion inference shard identity is invalid") from error
        bounded_int(design_index, minimum=0, maximum=MAX_DESIGNS - 1, label="RFdiffusion design index")
        if invocation.shard_id != f"design-{design_index:03d}":
            raise ScientificAdapterError("RFdiffusion inference shard identity is invalid")
        seed = _environment_int(
            invocation,
            "FS2_RFDIFFUSION_SEED",
            minimum=0,
            maximum=MAX_SEED,
        )
        return collect_workspace_handoff(
            invocation,
            workspace,
            label="RFdiffusion",
            name=HANDOFF_NAME,
            semantic_type=HANDOFF_SEMANTIC_TYPE,
            maximum_members=MAX_HANDOFF_MEMBERS,
            maximum_content_bytes=MAX_HANDOFF_CONTENT_BYTES,
            maximum_archive_bytes=MAX_HANDOFF_ARCHIVE_BYTES,
            included_paths=(
                f"shards/{design_index:03d}/result/result.json",
                f"shards/{design_index:03d}/result/designs/design_{seed}.pdb",
            ),
        )
    completion_sha256 = completion_marker(invocation, workspace, label="RFdiffusion")
    return _collect_final(invocation, workspace, completion_sha256=completion_sha256)


__all__ = [
    "COLLECTOR_ID",
    "MODEL_ID",
    "VALIDATOR_ID",
    "VARIANT_ID",
    "collect_companion_output",
    "compile_run",
]

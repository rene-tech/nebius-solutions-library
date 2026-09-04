"""Production controller adapter for the pinned Mosaic H100 candidate.

The public request selects only bounded scientific parameters.  Runtime image,
model artifacts, paths, direct argv, stage fan-out, collectors, and validators
remain controller-owned.  The profile remains route-disabled until this path
has its own public controller qualification receipt.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
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
    ArtifactPointer,
    LoadedArtifact,
    ScientificAdapterError,
    assert_profile_identity,
    bounded_int,
    build_execution_plan,
    finite_number,
    logical_stage_artifact,
    parse_json_artifact,
    parse_public_request,
    protein_sequence,
    run_workspace,
    strict_object,
    structure_atom_count,
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

MODEL_ID = "mosaic"
VARIANT_ID = "mosaic-boltz2-proteinmpnn-v1"
SOURCE_REPOSITORY = "escalante-bio/mosaic"
SOURCE_REVISION = "70fec525423f5f87156a1a957b4a4048f9f8e676"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/mosaic-boltz2-proteinmpnn-parameters/v1"
VALIDATOR_ID = VARIANT_ID
COLLECTOR_ID = VARIANT_ID
RECIPE_SHA256 = "cbfc7a88e6e7c2255730218bbdeaf6fc272d721b6c792231429a923309a8e0fe"
TARGET_INPUT_ID = "target_sequence"
TARGET_SEMANTIC_TYPE = "protein-sequence-fasta/v1"
TARGET_MEDIA_TYPE = "text/x-fasta"
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_SHARDS = 64
MAX_HANDOFF_MEMBERS = 32
MAX_HANDOFF_CONTENT_BYTES = 64 * 1024 * 1024
MAX_HANDOFF_ARCHIVE_BYTES = 80 * 1024 * 1024
MAX_FINAL_BYTES = 4 * 1024 * 1024 * 1024
HANDOFF_NAME = "stage-handoff"
HANDOFF_SEMANTIC_TYPE = "mosaic-design-workspace-handoff/v1"
RUNTIME_ENTRYPOINT = "/opt/fs2/bin/mosaic-batch"
RUNTIME_RECIPE = "/opt/fs2/mosaic/recipe.json"
MODEL_ARTIFACTS = (
    "mosaic-boltz2-conf",
    "boltzgen-inference-molecules",
    "mosaic-components",
)
MODEL_MOUNTS = {
    "mosaic-boltz2-conf": "/opt/fs2/artifacts/mosaic/boltz/boltz2_conf.ckpt",
    "boltzgen-inference-molecules": "/opt/fs2/artifacts/mosaic/boltz/mols",
    "mosaic-components": "/opt/fs2/artifacts/mosaic/proteinmpnn",
}
REFERENCE_DATA_SUPPLEMENTAL_GROUP = 1000
REQUIRES_VERIFIED_INPUT_ARTIFACTS = True


@dataclass(frozen=True, slots=True)
class Parameters:
    shard_count: int
    base_seed: int
    hotspots: tuple[int, ...]
    binder_length: int
    optimizer_steps: int

    @classmethod
    def parse(cls, value: object) -> Parameters:
        item = strict_object(
            value,
            required=frozenset({"schema", "shard_count", "base_seed", "hotspots", "binder_length", "optimizer_steps"}),
            label="Mosaic parameters",
        )
        if item["schema"] != PARAMETER_SCHEMA:
            raise ScientificAdapterError("Mosaic parameter schema selects another backend")
        shard_count = bounded_int(item["shard_count"], minimum=1, maximum=MAX_SHARDS, label="shard_count")
        base_seed = bounded_int(item["base_seed"], minimum=0, maximum=2**31 - 1, label="base_seed")
        if base_seed + shard_count - 1 > 2**31 - 1:
            raise ScientificAdapterError("Mosaic deterministic shard seeds overflow int32")
        raw_hotspots = item["hotspots"]
        if (
            not isinstance(raw_hotspots, list)
            or not 1 <= len(raw_hotspots) <= 32
            or any(isinstance(value, bool) or not isinstance(value, int) for value in raw_hotspots)
        ):
            raise ScientificAdapterError("Mosaic hotspots must be a bounded integer array")
        hotspots = tuple(cast(list[int], raw_hotspots))
        if hotspots != tuple(sorted(set(hotspots))) or any(not 1 <= value <= 1200 for value in hotspots):
            raise ScientificAdapterError("Mosaic hotspots must be sorted unique positions from 1 to 1200")
        return cls(
            shard_count=shard_count,
            base_seed=base_seed,
            hotspots=hotspots,
            binder_length=bounded_int(item["binder_length"], minimum=40, maximum=200, label="binder_length"),
            optimizer_steps=bounded_int(item["optimizer_steps"], minimum=20, maximum=500, label="optimizer_steps"),
        )


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _runtime_request(request: object) -> dict[str, object]:
    if not isinstance(request, Mapping):
        raise ScientificAdapterError("Mosaic request must be an object")
    return cast(dict[str, object], json.loads(json.dumps(request, allow_nan=False)))


def _runtime_manifest(target: ScientificInputArtifact) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
        "manifest_id": "manifest.mosaic.controller-input",
        "entries": [
            {
                "name": TARGET_INPUT_ID,
                "semantic_type": TARGET_SEMANTIC_TYPE,
                "artifact": {
                    "artifact_id": str(target.artifact_id),
                    "sha256": target.digest.removeprefix("sha256:"),
                    "size_bytes": target.size_bytes,
                    "media_type": target.media_type,
                    "compression": target.compression or "none",
                },
            }
        ],
    }
    return value


def _runtime_image_digest(profile: Mapping[str, object]) -> str:
    identity = strict_object(
        profile.get("execution_identity"),
        required=frozenset(
            {
                "model_revision",
                "runtime_image_digest",
                "runtime_recipe_sha256",
                "workload_recipe_sha256",
                "artifact_manifest_digest",
                "execution_identity_sha256",
            }
        ),
        label="Mosaic profile execution identity",
    )
    digest = identity["runtime_image_digest"]
    if (
        not isinstance(digest, str)
        or not digest.startswith("sha256:")
        or len(digest) != 71
        or any(character not in "0123456789abcdef" for character in digest[7:])
    ):
        raise ScientificAdapterError("Mosaic runtime image digest is invalid")
    return digest


def compile_run(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
    input_artifacts: tuple[ScientificInputArtifact, ...] | None = None,
) -> AdapterExecutionPlan:
    """Compile one bounded request into design fan-out and CPU aggregation."""

    request = parse_public_request(request_value, maximum_input_bytes=MAX_INPUT_BYTES)
    parameters = Parameters.parse(request.parameters)
    if request.operation != "design-binder":
        raise ScientificAdapterError("Mosaic supports only design-binder")
    target = verified_manifest_entry(
        request,
        input_artifacts,
        logical_artifact_id=TARGET_INPUT_ID,
        semantic_type=TARGET_SEMANTIC_TYPE,
        media_type=TARGET_MEDIA_TYPE,
        compressions=frozenset({None, "none"}),
        maximum_bytes=MAX_INPUT_BYTES,
        label="Mosaic",
    )
    assert_profile_identity(
        profile,
        model_id=MODEL_ID,
        repository=SOURCE_REPOSITORY,
        revision=SOURCE_REVISION,
        parameter_schema=PARAMETER_SCHEMA,
        request=request,
    )
    runtime_request = _runtime_request(request.to_dict())
    runtime_manifest = _runtime_manifest(target)
    request_document = _canonical_json(runtime_request)
    manifest_document = _canonical_json(runtime_manifest)
    runtime_request_sha256 = hashlib.sha256((request_document + "\n").encode()).hexdigest()
    runtime_image_digest = _runtime_image_digest(profile)
    shard_ids = tuple(f"shard-{index:03d}" for index in range(parameters.shard_count))
    expansions = {
        "design": ScientificStageExpansion(shard_ids=shard_ids),
        "aggregate": ScientificStageExpansion(shard_ids=("main",)),
    }
    invocations: list[StageInvocation] = []
    design_outputs: list[str] = []
    for index, shard_id in enumerate(shard_ids):
        workspace = run_workspace(MODEL_ID, operation_id, shard_id)
        output = logical_stage_artifact(operation_id, "design", shard_id)
        design_outputs.append(output)
        input_path = f"{workspace}/inputs/{target.artifact_id}"
        command = (
            RUNTIME_ENTRYPOINT,
            "run-shard",
            "--request",
            f"{workspace}/.fs2/request.json",
            "--input-manifest",
            f"{workspace}/.fs2/input-manifest.json",
            "--recipe",
            RUNTIME_RECIPE,
            "--recipe-sha256",
            RECIPE_SHA256,
            "--shard-index",
            str(index),
            "--seed",
            str(parameters.base_seed + index),
            "--output",
            f"{workspace}/shards/{index:03d}",
        )
        invocations.append(
            StageInvocation(
                stage_id="design",
                shard_id=shard_id,
                argv=wrap_stage_argv(workspace, command),
                environment=(
                    ("FS2_ARTIFACT_ROOT", "/opt/fs2/artifacts"),
                    # The pinned v6 runtime consumes model material and request
                    # inputs through independent roots.  Keep this explicit in
                    # the recipe identity while the public controller route is
                    # still closed pending platform qualification.
                    ("FS2_INPUT_ARTIFACT_ROOT", workspace),
                    ("HF_HUB_OFFLINE", "1"),
                    ("TRANSFORMERS_OFFLINE", "1"),
                ),
                working_directory=workspace,
                consumes=(target.logical_artifact_id,),
                produces=output,
                collector_id=COLLECTOR_ID,
                validator_id=VALIDATOR_ID,
                handoff_name=HANDOFF_NAME,
                max_output_artifacts=1,
                max_output_bytes=MAX_HANDOFF_ARCHIVE_BYTES,
                materializations=(
                    ArtifactMaterialization(
                        artifact_id=target.logical_artifact_id,
                        destination=input_path,
                        mode=MaterializationMode.COPY_FILE,
                        compression=target.compression,
                    ),
                ),
                runtime_artifacts=MODEL_ARTIFACTS,
                runtime_mounts=tuple(
                    RuntimeArtifactMount(
                        artifact_id=artifact_id,
                        mount_path=MODEL_MOUNTS[artifact_id],
                        supplemental_groups=(REFERENCE_DATA_SUPPLEMENTAL_GROUP,),
                    )
                    for artifact_id in MODEL_ARTIFACTS
                ),
                workspace_documents=(
                    StageWorkspaceDocument(".fs2/request.json", request_document),
                    StageWorkspaceDocument(".fs2/input-manifest.json", manifest_document),
                ),
            )
        )

    aggregate_workspace = run_workspace(MODEL_ID, operation_id, "aggregate-main")
    aggregate_output = logical_stage_artifact(operation_id, "aggregate", "main")
    aggregate_command = (
        RUNTIME_ENTRYPOINT,
        "aggregate",
        "--request",
        f"{aggregate_workspace}/.fs2/request.json",
        "--input-manifest",
        f"{aggregate_workspace}/.fs2/input-manifest.json",
        "--shards",
        f"{aggregate_workspace}/shards",
        "--expected-shards",
        str(parameters.shard_count),
        "--staging-manifest",
        f"{aggregate_workspace}/output-manifest.json.tmp",
        "--output-manifest",
        f"{aggregate_workspace}/output-manifest.json",
        "--atomic-rename",
    )
    invocations.append(
        StageInvocation(
            stage_id="aggregate",
            shard_id="main",
            argv=wrap_stage_argv(aggregate_workspace, aggregate_command),
            environment=(
                # The accepted runtime binds its atomic aggregate to this
                # exact environment key; the collector verifies the same
                # value instead of inventing a controller-only alias.
                ("FS2_RUNTIME_IMAGE_DIGEST", runtime_image_digest),
                ("FS2_MOSAIC_BASE_SEED", str(parameters.base_seed)),
                ("FS2_MOSAIC_BINDER_LENGTH", str(parameters.binder_length)),
                ("FS2_MOSAIC_REQUEST_SHA256", runtime_request_sha256),
                ("FS2_MOSAIC_SHARD_COUNT", str(parameters.shard_count)),
            ),
            working_directory=aggregate_workspace,
            consumes=tuple(design_outputs),
            produces=aggregate_output,
            collector_id=COLLECTOR_ID,
            validator_id=VALIDATOR_ID,
            max_output_artifacts=2 * parameters.shard_count + 1,
            max_output_bytes=MAX_FINAL_BYTES,
            materializations=tuple(
                ArtifactMaterialization(
                    artifact_id=output,
                    destination=aggregate_workspace,
                    mode=MaterializationMode.OVERLAY_TAR,
                    compression="zstd",
                )
                for output in design_outputs
            ),
            workspace_documents=(
                StageWorkspaceDocument(".fs2/request.json", request_document),
                StageWorkspaceDocument(".fs2/input-manifest.json", manifest_document),
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
        required_model_artifacts=MODEL_ARTIFACTS,
    )


def _environment_int(invocation: StageInvocation, name: str, *, minimum: int, maximum: int) -> int:
    try:
        raw = dict(invocation.environment)[name]
        value = int(raw)
    except (KeyError, ValueError) as error:
        raise ScientificAdapterError(f"Mosaic invocation has no valid {name}") from error
    return bounded_int(value, minimum=minimum, maximum=maximum, label=name)


def _manifest_entries(invocation: StageInvocation, workspace: Path) -> Mapping[str, Mapping[str, object]]:
    _path, content = contained_stable_file(
        workspace,
        "output-manifest.json",
        maximum_bytes=16 * 1024 * 1024,
        label="Mosaic output manifest",
    )
    manifest = strict_object(
        parse_json_artifact(content, label="Mosaic output manifest"),
        required=frozenset({"schema", "manifest_id", "entries"}),
        label="Mosaic output manifest",
    )
    if (
        manifest["schema"] != "fs2-serve.nebius.ai/scientific-artifact-manifest/v1"
        or manifest["manifest_id"] != "manifest.mosaic.output"
    ):
        raise ScientificAdapterError("Mosaic output manifest identity differs")
    raw_entries = manifest["entries"]
    if not isinstance(raw_entries, list) or len(raw_entries) != invocation.max_output_artifacts + _environment_int(
        invocation, "FS2_MOSAIC_SHARD_COUNT", minimum=1, maximum=MAX_SHARDS
    ):
        # The runtime records one private shard receipt per design in addition
        # to the customer-visible aggregate/metrics/structure artifacts.
        raise ScientificAdapterError("Mosaic output manifest cardinality differs")
    entries: dict[str, Mapping[str, object]] = {}
    for index, raw in enumerate(raw_entries):
        entry = strict_object(
            raw,
            required=frozenset({"name", "semantic_type", "artifact"}),
            label=f"Mosaic output entries[{index}]",
        )
        name = entry["name"]
        if not isinstance(name, str) or name in entries:
            raise ScientificAdapterError("Mosaic output entry names must be unique")
        entries[name] = entry
    return entries


def _artifact_index(workspace: Path) -> Mapping[str, str]:
    _path, content = contained_stable_file(
        workspace,
        "artifact-index.json",
        maximum_bytes=4 * 1024 * 1024,
        label="Mosaic artifact index",
    )
    value = parse_json_artifact(content, label="Mosaic artifact index")
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise ScientificAdapterError("Mosaic artifact index is invalid")
    return cast(Mapping[str, str], value)


def _entry_file(
    workspace: Path,
    entry: Mapping[str, object],
    index: Mapping[str, str],
    *,
    expected_name: str,
    semantic_type: str,
    media_type: str,
    maximum_bytes: int,
) -> tuple[Path, bytes]:
    if entry.get("name") != expected_name or entry.get("semantic_type") != semantic_type:
        raise ScientificAdapterError(f"Mosaic {expected_name} semantic identity differs")
    pointer = ArtifactPointer.parse(entry.get("artifact"), label=f"Mosaic {expected_name}", maximum_bytes=maximum_bytes)
    if pointer.media_type != media_type or pointer.compression not in {None, "none"}:
        raise ScientificAdapterError(f"Mosaic {expected_name} media type differs")
    indexed = index.get(pointer.artifact_id)
    if not isinstance(indexed, str):
        raise ScientificAdapterError(f"Mosaic {expected_name} is absent from the artifact index")
    try:
        root = workspace.resolve(strict=True)
        absolute = Path(indexed)
        relative = absolute.resolve(strict=True).relative_to(root).as_posix()
    except (OSError, ValueError) as error:
        raise ScientificAdapterError(f"Mosaic {expected_name} path escapes the workspace") from error
    path, content = contained_stable_file(
        workspace,
        relative,
        maximum_bytes=maximum_bytes,
        label=f"Mosaic {expected_name}",
    )
    if len(content) != pointer.size_bytes or hashlib.sha256(content).hexdigest() != pointer.sha256:
        raise ScientificAdapterError(f"Mosaic {expected_name} bytes differ from the committed pointer")
    return path, content


def _pdb_sequence(content: bytes, *, binder_length: int) -> tuple[str, int]:
    pointer = ArtifactPointer(
        artifact_id="validation.mosaic.structure",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        media_type="chemical/x-pdb",
    )
    atom_count = structure_atom_count(
        LoadedArtifact("candidate", "protein-structure-pdb/v1", pointer, content),
        require_two_chains=False,
    )
    residue_names = {
        "ALA": "A",
        "ARG": "R",
        "ASN": "N",
        "ASP": "D",
        "CYS": "C",
        "GLN": "Q",
        "GLU": "E",
        "GLY": "G",
        "HIS": "H",
        "ILE": "I",
        "LEU": "L",
        "LYS": "K",
        "MET": "M",
        "PHE": "F",
        "PRO": "P",
        "SER": "S",
        "THR": "T",
        "TRP": "W",
        "TYR": "Y",
        "VAL": "V",
    }
    try:
        lines = content.decode("ascii").splitlines()
    except UnicodeError as error:
        raise ScientificAdapterError("Mosaic structure is not ASCII PDB") from error
    residues: dict[tuple[str, str, str], str] = {}
    backbone: dict[tuple[str, str, str], set[str]] = {}
    coordinates: list[tuple[float, float, float]] = []
    for line in lines:
        if not line.startswith("ATOM"):
            continue
        if len(line) < 54 or line[17:20].strip() not in residue_names:
            raise ScientificAdapterError("Mosaic structure contains an invalid ATOM record")
        key = (line[21:22], line[22:26], line[26:27])
        residue = line[17:20].strip()
        if key in residues and residues[key] != residue:
            raise ScientificAdapterError("Mosaic structure changes a residue identity")
        residues.setdefault(key, residue)
        backbone.setdefault(key, set()).add(line[12:16].strip())
        try:
            point = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
        except ValueError as error:
            raise ScientificAdapterError("Mosaic structure contains an invalid coordinate") from error
        if not all(math.isfinite(value) for value in point):
            raise ScientificAdapterError("Mosaic structure contains a non-finite coordinate")
        coordinates.append(point)
    if len(residues) != binder_length:
        raise ScientificAdapterError("Mosaic structure residue count differs from binder_length")
    if any(not {"N", "CA", "C"}.issubset(atoms) for atoms in backbone.values()):
        raise ScientificAdapterError("Mosaic structure lacks a complete protein backbone")
    extent = max(
        max(point[axis] for point in coordinates) - min(point[axis] for point in coordinates) for axis in range(3)
    )
    if extent < 5.0:
        raise ScientificAdapterError("Mosaic structure coordinates are degenerate")
    composition = Counter(residues.values())
    if max(composition.values()) / len(residues) > 0.5:
        raise ScientificAdapterError("Mosaic structure is a degenerate homopolymer")
    return "".join(residue_names[name] for name in residues.values()), atom_count


def _collect_final(invocation: StageInvocation, workspace: Path, *, completion_sha256: str) -> CollectedStageOutput:
    from . import CollectedArtifactFile, CollectedStageOutput

    if invocation.stage_id != "aggregate" or invocation.handoff_name is not None:
        raise ScientificAdapterError("Mosaic result collector received another stage contract")
    count = _environment_int(invocation, "FS2_MOSAIC_SHARD_COUNT", minimum=1, maximum=MAX_SHARDS)
    base_seed = _environment_int(invocation, "FS2_MOSAIC_BASE_SEED", minimum=0, maximum=2**31 - 1)
    binder_length = _environment_int(invocation, "FS2_MOSAIC_BINDER_LENGTH", minimum=40, maximum=200)
    environment = dict(invocation.environment)
    expected_request_sha256 = environment.get("FS2_MOSAIC_REQUEST_SHA256", "")
    expected_image_digest = environment.get("FS2_RUNTIME_IMAGE_DIGEST", "")
    if len(expected_request_sha256) != 64 or not expected_image_digest.startswith("sha256:"):
        raise ScientificAdapterError("Mosaic frozen execution digests are invalid")
    entries = _manifest_entries(invocation, workspace)
    expected_names = {"aggregate"}
    expected_names.update(f"shard-{index:03d}" for index in range(count))
    expected_names.update(f"candidate-{index:03d}-metrics" for index in range(count))
    expected_names.update(f"candidate-{index:03d}-structure" for index in range(count))
    if set(entries) != expected_names:
        raise ScientificAdapterError("Mosaic output manifest contains missing or unexpected entries")
    index = _artifact_index(workspace)
    pointer_ids = tuple(
        raw.get("artifact_id")
        for entry in entries.values()
        for raw in (entry.get("artifact"),)
        if isinstance(raw, Mapping)
    )
    if (
        len(index) != len(entries)
        or len(pointer_ids) != len(entries)
        or any(not isinstance(artifact_id, str) for artifact_id in pointer_ids)
    ):
        raise ScientificAdapterError("Mosaic artifact index cardinality differs from the manifest")
    string_pointer_ids = cast(tuple[str, ...], pointer_ids)
    if (
        len(string_pointer_ids) != len(set(string_pointer_ids))
        or set(string_pointer_ids) != set(index)
        or len(index.values()) != len(set(index.values()))
    ):
        raise ScientificAdapterError("Mosaic artifact index cardinality differs from the manifest")

    aggregate_path, aggregate_content = _entry_file(
        workspace,
        entries["aggregate"],
        index,
        expected_name="aggregate",
        semantic_type="mosaic-aggregate-json/v1",
        media_type="application/json",
        maximum_bytes=1024 * 1024,
    )
    aggregate = parse_json_artifact(aggregate_content, label="Mosaic aggregate")
    expected_aggregate = {
        "backend_id": VARIANT_ID,
        "source_revision": SOURCE_REVISION,
        "recipe_sha256": RECIPE_SHA256,
        "request_sha256": expected_request_sha256,
        "runtime_image_digest": expected_image_digest,
        "expected_shards": count,
        "succeeded_shards": count,
        "atomic_commit": True,
    }
    if dict(aggregate) != expected_aggregate:
        raise ScientificAdapterError("Mosaic aggregate identity or completeness differs")

    artifacts = [
        CollectedArtifactFile(
            name="aggregate",
            semantic_type="mosaic-aggregate-json/v1",
            path=aggregate_path,
            media_type="application/json",
        )
    ]
    total_output_bytes = len(aggregate_content)
    total_atoms = 0
    for shard_index in range(count):
        shard_name = f"shard-{shard_index:03d}"
        _shard_path, shard_content = _entry_file(
            workspace,
            entries[shard_name],
            index,
            expected_name=shard_name,
            semantic_type="mosaic-shard-result-json/v1",
            media_type="application/json",
            maximum_bytes=1024 * 1024,
        )
        shard = parse_json_artifact(shard_content, label=f"Mosaic shard {shard_index}")
        if dict(shard) != {
            "backend_id": VARIANT_ID,
            "source_revision": SOURCE_REVISION,
            "recipe_sha256": RECIPE_SHA256,
            "index": shard_index,
            "seed": base_seed + shard_index,
            "status": "succeeded",
        }:
            raise ScientificAdapterError("Mosaic shard identity, seed, or status differs")

        metrics_name = f"candidate-{shard_index:03d}-metrics"
        metrics_path, metrics_content = _entry_file(
            workspace,
            entries[metrics_name],
            index,
            expected_name=metrics_name,
            semantic_type="mosaic-design-metrics-json/v1",
            media_type="application/json",
            maximum_bytes=1024 * 1024,
        )
        metrics = strict_object(
            parse_json_artifact(metrics_content, label=f"Mosaic metrics {shard_index}"),
            required=frozenset({"candidate_id", "shard_index", "seed", "sequence", "iptm", "mean_plddt", "objective"}),
            label=f"Mosaic metrics {shard_index}",
        )
        sequence = protein_sequence(metrics["sequence"], label=f"Mosaic sequence {shard_index}")
        if (
            metrics["candidate_id"] != f"design-{shard_index:03d}"
            or metrics["shard_index"] != shard_index
            or metrics["seed"] != base_seed + shard_index
            or len(sequence) != binder_length
        ):
            raise ScientificAdapterError("Mosaic candidate identity, seed, or sequence length differs")
        finite_number(metrics["iptm"], minimum=0.0, maximum=1.0, label="Mosaic iptm")
        finite_number(metrics["mean_plddt"], minimum=0.0, maximum=1.0, label="Mosaic mean_plddt")
        finite_number(metrics["objective"], minimum=-1e9, maximum=1e9, label="Mosaic objective")

        structure_name = f"candidate-{shard_index:03d}-structure"
        structure_path, structure_content = _entry_file(
            workspace,
            entries[structure_name],
            index,
            expected_name=structure_name,
            semantic_type="protein-structure-pdb/v1",
            media_type="chemical/x-pdb",
            maximum_bytes=128 * 1024 * 1024,
        )
        structure_sequence, atom_count = _pdb_sequence(structure_content, binder_length=binder_length)
        if structure_sequence != sequence:
            raise ScientificAdapterError("Mosaic designed sequence differs from its structure")
        total_output_bytes += len(metrics_content) + len(structure_content)
        if total_output_bytes > invocation.max_output_bytes:
            raise ScientificAdapterError("Mosaic customer-visible artifacts exceed the invocation bound")
        total_atoms += atom_count
        artifacts.extend(
            (
                CollectedArtifactFile(
                    name=metrics_name,
                    semantic_type="mosaic-design-metrics-json/v1",
                    path=metrics_path,
                    media_type="application/json",
                ),
                CollectedArtifactFile(
                    name=structure_name,
                    semantic_type="protein-structure-pdb/v1",
                    path=structure_path,
                    media_type="chemical/x-pdb",
                ),
            )
        )
    if len(artifacts) != invocation.max_output_artifacts:
        raise ScientificAdapterError("Mosaic customer-visible artifact cardinality differs")
    validation_payload = _canonical_json(
        {
            "schema": "fs2-serve.nebius.ai/mosaic-semantic-validation/v1",
            "validator_id": invocation.validator_id,
            "status": "passed",
            "request_sha256": expected_request_sha256,
            "runtime_image_digest": expected_image_digest,
            "candidate_count": count,
            "atom_count": total_atoms,
        }
    ).encode()
    receipt = workspace / ".fs2/mosaic-semantic-validation.json"
    atomic_publish(receipt, validation_payload, workspace=workspace, label="Mosaic validation receipt")
    return CollectedStageOutput(
        artifacts=tuple(artifacts),
        validation={
            "validator_id": invocation.validator_id,
            "status": "passed",
            "request_sha256": expected_request_sha256,
            "completion_marker_sha256": completion_sha256,
            "validation_sha256": hashlib.sha256(validation_payload).hexdigest(),
            "candidate_count": count,
            "atom_count": total_atoms,
        },
    )


def collect_companion_output(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    """Collect one exact Mosaic stage through the global companion protocol."""

    if invocation.collector_id != COLLECTOR_ID or invocation.validator_id != VALIDATOR_ID:
        raise ScientificAdapterError("Mosaic collector received another execution identity")
    if invocation.stage_id == "design":
        try:
            shard_index = int(invocation.shard_id.removeprefix("shard-"))
        except ValueError as error:
            raise ScientificAdapterError("Mosaic design shard identity is invalid") from error
        bounded_int(shard_index, minimum=0, maximum=MAX_SHARDS - 1, label="Mosaic shard index")
        if invocation.shard_id != f"shard-{shard_index:03d}":
            raise ScientificAdapterError("Mosaic design shard identity is invalid")
        return collect_workspace_handoff(
            invocation,
            workspace,
            label="Mosaic",
            name=HANDOFF_NAME,
            semantic_type=HANDOFF_SEMANTIC_TYPE,
            maximum_members=MAX_HANDOFF_MEMBERS,
            maximum_content_bytes=MAX_HANDOFF_CONTENT_BYTES,
            maximum_archive_bytes=MAX_HANDOFF_ARCHIVE_BYTES,
            included_paths=(
                f"shards/{shard_index:03d}/shard-result.json",
                f"shards/{shard_index:03d}/candidate-metrics.json",
                f"shards/{shard_index:03d}/candidate.pdb",
            ),
        )
    completion_sha256 = completion_marker(invocation, workspace, label="Mosaic")
    return _collect_final(invocation, workspace, completion_sha256=completion_sha256)


__all__ = [
    "COLLECTOR_ID",
    "MODEL_ID",
    "VALIDATOR_ID",
    "VARIANT_ID",
    "collect_companion_output",
    "compile_run",
]

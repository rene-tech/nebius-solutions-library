"""AlphaFold 3 v3.0.4 adapter reconciled with the current two-lane controller.

The public request freezes only bounded run controls.  The input artifact is an
ordinary AlphaFold 3 JSON document; academic authorization belongs to the
deployment profile and is never supplied by the caller.

The stage split is intentional and security relevant.  ``data-pipeline`` runs
on the reference-data CPU class, sees the entire published reference root, and
cannot see the licensed parameters.  ``inference`` sees the immutable CPU
handoff and the single private parameter file, but cannot see the reference
databases.  The stage identities, the 16 CPU / 64 GiB envelope, the whole-root
reference mount and the private parameter mount are the exact shapes the
controller's AlphaFold 3 gate (``execution.py::_verify_alphafold3_runtime``)
already enforces.

Every identity below is a restatement of an accepted contract elsewhere in the
tree: the r6 image lock, the academic parameter binding, and the published
reference-data terminal receipt.  Tests assert the restatements still agree.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from ..models import (
    AdapterExecutionPlan,
    ArtifactMaterialization,
    MaterializationMode,
    RuntimeArtifactMount,
    StageInvocation,
)
from .common import (
    ARTIFACT_MANIFEST_SCHEMA,
    ArtifactPointer,
    CollectedOutput,
    LoadedArtifact,
    PublicRunRequest,
    ScientificAdapterError,
    assert_profile_identity,
    build_execution_plan,
    collect_output_files,
    logical_stage_artifact,
    parse_public_request,
    run_workspace,
    strict_object,
    structure_atom_count,
)

MODEL_ID = "alphafold3"
VARIANT_ID = "upstream-v3-0-4"
DISPLAY_NAME = "AlphaFold 3 v3.0.4"
SOURCE_REPOSITORY = "google-deepmind/alphafold3"
SOURCE_REVISION = "85c4d20505fd5cef05eac22b534d4e793971ae69"
RELEASE_TAG = "v3.0.4"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/alphafold3-upstream-v3-0-4-parameters/v1"
VALIDATOR_ID = "alphafold3-upstream-v3-0-4"

# Immutable r6 runtime image, registry-verified read-only on 2026-09-04.
RUNTIME_IMAGE_REPOSITORY = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/alphafold3"
RUNTIME_IMAGE_TAG = "3.0.4-85c4d205-r6"
RUNTIME_IMAGE_DIGEST = "sha256:0cde199e8473a2d069c896c4f8d67a58b31e00bfb87c3660aed154693699e03e"
RUNTIME_IMAGE = f"{RUNTIME_IMAGE_REPOSITORY}@{RUNTIME_IMAGE_DIGEST}"
RUNTIME_COMMAND = ("/alphafold3_venv/bin/python3", "/opt/fs2/af3_runtime.py")

# Licensed parameter object: tenant-private, never embedded, never world readable.
PARAMETERS_ARTIFACT = "alphafold3-parameters"
PARAMETERS_FILENAME = "af3.bin.zst"
PARAMETERS_SHA256 = "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
PARAMETERS_SIZE_BYTES = 1_020_545_840
PARAMETERS_CLAIM = "academic-assets-runtime-rwx"
PARAMETERS_CLAIM_NAMESPACE = "fs2-academic-poc"
PARAMETERS_CLAIM_SUB_PATH = "alphafold3"
PARAMETERS_SOURCE_SUB_PATH = f"{PARAMETERS_CLAIM_SUB_PATH}/{PARAMETERS_FILENAME}"
PARAMETERS_SOURCE_MOUNT_PATH = "/models"
PARAMETERS_MOUNT_PATH = f"{PARAMETERS_SOURCE_MOUNT_PATH}/{PARAMETERS_FILENAME}"
PARAMETERS_SUPPLEMENTAL_GROUP = 65_532

# Public reference bundle: published, read-only, and bound by its terminal receipt.
REFERENCE_ARTIFACT = "alphafold3-public-databases-v3.0"
REFERENCE_REVISION = "v3.0-paper-snapshot-2022-09-28"
REFERENCE_HOST_ROOT = "/mnt/fs2-reference-data/data"
REFERENCE_MOUNT_PATH = "/reference-data"
REFERENCE_INVENTORY_MARKER = ".fs2-manifest-sha256"
REFERENCE_MANIFEST_ALGORITHM = "fs2-serve.nebius.ai/reference-data-manifest/v1"
REFERENCE_TREE_SHA256 = "d27b8956170b5b0cf0f7daadf53a34e38cbe725dafbe9c91af86c671b32dfaea"
REFERENCE_MANIFEST_SHA256 = "aa585259ce05393cd38db1693299ed9ec7f9c421aa4e1159f8d5aa0eb0ba9748"
REFERENCE_INVENTORY_SHA256 = "38af3baa89a66cd24dec785279670a2e37597f98d206f555a04c138c6be71579"
REFERENCE_RECEIPT_SHA256 = "b049e69846867caa75ef140e105a962fcf14e5c78ec8bfd97741cced32a8f6a6"
REFERENCE_FILE_COUNT = 195_867
REFERENCE_EXPANDED_BYTES = 672_435_030_513
REFERENCE_DATASET_SUB_PATH = f"datasets/{REFERENCE_ARTIFACT}/{REFERENCE_REVISION}/sha256/{REFERENCE_TREE_SHA256}"
REFERENCE_RECEIPT_PATH = f"{REFERENCE_MOUNT_PATH}/receipts/{REFERENCE_ARTIFACT}/{REFERENCE_REVISION}.json"
REFERENCE_DATA_GID = 1000

DATA_STAGE_ID = "data-pipeline"
INFERENCE_STAGE_ID = "inference"
EXECUTION_NAMESPACE = "fs2-academic-poc"
SERVICE_ACCOUNT_NAME = "fs2-academic-runner"
DATA_CPU_CLASS = "reference-data"
DATA_LOCAL_QUEUE = "academic-scientific-cpu"
DATA_CLUSTER_QUEUE = "reference-data-cpu"
INFERENCE_LOCAL_QUEUE = "academic-scientific"
INFERENCE_CLUSTER_QUEUE = "inference-accelerators"
DATA_CPU = "16"
DATA_MEMORY = "64Gi"
DATA_EPHEMERAL_STORAGE = "32Gi"
DATA_CPU_MILLIS = 16_000
DATA_MEMORY_BYTES = 64 * 1024**3
DATA_EPHEMERAL_STORAGE_BYTES = 32 * 1024**3
INFERENCE_CPU = "8"
INFERENCE_MEMORY = "96Gi"
INFERENCE_EPHEMERAL_STORAGE = "32Gi"
INFERENCE_LIMIT_CPU = "32"
INFERENCE_LIMIT_MEMORY = "256Gi"
INFERENCE_LIMIT_EPHEMERAL_STORAGE = "128Gi"
INFERENCE_CPU_MILLIS = 8_000
INFERENCE_MEMORY_BYTES = 96 * 1024**3
INFERENCE_EPHEMERAL_STORAGE_BYTES = 32 * 1024**3
INFERENCE_LIMIT_CPU_MILLIS = 32_000
INFERENCE_LIMIT_MEMORY_BYTES = 256 * 1024**3
INFERENCE_LIMIT_EPHEMERAL_STORAGE_BYTES = 128 * 1024**3

FOLD_INPUT_FILENAME = "fold_input.json"
DATA_OUTPUT_DIR = "data-output"
HANDOFF_DIR_NAME = "fs2-af3-handoff"
HANDOFF_MOUNT_DIR = "handoff"
HANDOFF_INDEX = "index.json"
HANDOFF_NAME = "processed-input"
HANDOFF_SCHEMA = "fs2-serve.nebius.ai/alphafold3-data-handoff/v1"
INFERENCE_OUTPUT_DIR = "outputs"
DATA_RECEIPT_FILENAME = "data-runtime-receipt.json"
INFERENCE_RECEIPT_FILENAME = "inference-runtime-receipt.json"
RUNTIME_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/alphafold3-runtime-receipt/v1"

DATA_COLLECTOR_ID = "alphafold3-data-collector-v1"
DATA_VALIDATOR_ID = "alphafold3-data-validator-v1"
RESULT_COLLECTOR_ID = "alphafold3-result-collector-v1"
STAGE_EXECUTION_CONTRACTS: Mapping[str, Mapping[str, object]] = MappingProxyType(
    {
        DATA_STAGE_ID: {
            "collector_id": DATA_COLLECTOR_ID,
            "validator_id": DATA_VALIDATOR_ID,
            "runtime_artifacts": (REFERENCE_ARTIFACT,),
        },
        INFERENCE_STAGE_ID: {
            "collector_id": RESULT_COLLECTOR_ID,
            "validator_id": VALIDATOR_ID,
            "runtime_artifacts": (PARAMETERS_ARTIFACT,),
        },
    }
)

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_HANDOFF_FILES = 64
MAX_HANDOFF_BYTES = 1024 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_CONFIDENCE_BYTES = 64 * 1024 * 1024
MAX_STRUCTURE_BYTES = 256 * 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024 * 1024
MAX_SAMPLES_PER_JOB = 256
FORBIDDEN_ARGV_TOKENS = (
    "fs2-run-alphafold3",
    "/databases",
    "--input-json",
    "--processed-json",
    "--handoff-tar",
)
ADMISSION_BLOCKER = "AcademicDeploymentAuthorizationMissing"

_RAW_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAMPLE_DIR = re.compile(r"^seed-(?P<seed>0|[1-9][0-9]{0,9})_sample-(?P<sample>0|[1-9][0-9]{0,4})$")
_SUMMARY_BOUNDS = {
    "ptm": (0.0, 1.0),
    "iptm": (0.0, 1.0),
    "ranking_score": (-100.0, 2.0),
    "fraction_disordered": (0.0, 1.0),
}


def reference_mount() -> RuntimeArtifactMount:
    """The whole-root, read-only reference plane binding of the CPU stage."""

    return RuntimeArtifactMount(
        artifact_id=REFERENCE_ARTIFACT,
        mount_path=REFERENCE_MOUNT_PATH,
        # There must be no subPath: the receipt, dataset marker, and sibling
        # manifests/sha256 document all resolve beneath this one root.
        sub_path=None,
        read_only=True,
        expected_content_sha256=REFERENCE_TREE_SHA256,
        expected_manifest_sha256=REFERENCE_MANIFEST_SHA256,
        supplemental_groups=(REFERENCE_DATA_GID,),
    )


def parameters_mount() -> RuntimeArtifactMount:
    """The single private parameter file binding of the GPU stage."""

    return RuntimeArtifactMount(
        artifact_id=PARAMETERS_ARTIFACT,
        mount_path=PARAMETERS_MOUNT_PATH,
        sub_path=PARAMETERS_FILENAME,
        read_only=True,
        expected_content_sha256=PARAMETERS_SHA256,
        supplemental_groups=(PARAMETERS_SUPPLEMENTAL_GROUP,),
    )


RUNTIME_MOUNTS: Mapping[str, RuntimeArtifactMount] = MappingProxyType(
    {DATA_STAGE_ID: reference_mount(), INFERENCE_STAGE_ID: parameters_mount()}
)


def mount_contract(stage_id: str) -> RuntimeArtifactMount:
    """Return the sole runtime mount for one AF3 stage."""

    try:
        return RUNTIME_MOUNTS[stage_id]
    except KeyError as error:
        raise ScientificAdapterError(f"AlphaFold 3 has no mount contract for stage {stage_id}") from error


@dataclass(frozen=True, slots=True)
class Parameters:
    input_mode: str
    fold_job: str | None

    @classmethod
    def parse(cls, value: object) -> Parameters:
        item = strict_object(
            value,
            required=frozenset({"input_mode"}),
            optional=frozenset({"fold_job"}),
            label="AlphaFold 3 parameters",
        )
        if item["input_mode"] != "raw":
            raise ScientificAdapterError(
                "input_mode must be raw until a separately scheduled enriched-input profile exists"
            )
        fold_job = item.get("fold_job")
        if fold_job is not None and not _is_fold_job_name(fold_job):
            raise ScientificAdapterError("fold_job must be a bounded AlphaFold 3 job name")
        return cls(input_mode="raw", fold_job=fold_job if isinstance(fold_job, str) else None)


def _is_fold_job_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and value.strip() == value
        and value not in {".", ".."}
        and not any(character in value for character in ("/", "\\"))
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _request(value: object) -> tuple[PublicRunRequest, Parameters]:
    request = parse_public_request(value, maximum_input_bytes=MAX_INPUT_BYTES)
    if request.input_manifest.media_type != "application/json":
        raise ScientificAdapterError("AlphaFold 3 input_manifest must point to application/json")
    if request.input_manifest.compression not in {None, "none"}:
        raise ScientificAdapterError("AlphaFold 3 fold input must be an uncompressed JSON artifact")
    return request, Parameters.parse(request.parameters)


def _assert_deployment_authorization(profile: Mapping[str, object]) -> None:
    """Require operator-owned authorization, never a caller receipt.

    A candidate profile carries no receipt digest; a promoted profile carries the
    deployment's own authorization receipt.  Either way the caller supplies
    nothing, and any other access shape fails closed.
    """

    access = profile.get("access")
    if not isinstance(access, Mapping) or set(access) != {"profile", "state", "receipt_digest", "credentials_embedded"}:
        raise ScientificAdapterError(ADMISSION_BLOCKER)
    receipt = access["receipt_digest"]
    if (
        access["profile"] != "academic"
        or access["state"] != "verified"
        or access["credentials_embedded"] is not False
        or not (receipt is None or (isinstance(receipt, str) and _RAW_SHA256.fullmatch(receipt) is not None))
    ):
        raise ScientificAdapterError(ADMISSION_BLOCKER)


def _assert_clean_argv(argv: tuple[str, ...]) -> None:
    for token in FORBIDDEN_ARGV_TOKENS:
        if any(value == token or value.startswith(f"{token}=") for value in argv):
            raise ScientificAdapterError(f"AlphaFold 3 argv uses retired command surface {token}")


def _load_json_file(path: Path, *, label: str, maximum_bytes: int) -> Mapping[str, object]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ScientificAdapterError(f"{label} is not readable") from error
    if not 1 <= len(payload) <= maximum_bytes:
        raise ScientificAdapterError(f"{label} is outside the byte bound")
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ScientificAdapterError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ScientificAdapterError(f"{label} must contain one object")
    return value


def _contained_regular_file(root: Path, candidate: Path, *, label: str) -> Path:
    """Resolve a file beneath *root* without accepting symlinked output paths."""

    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise ScientificAdapterError(f"{label} escapes its artifact root") from error
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ScientificAdapterError(f"{label} must not use symlinks")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ScientificAdapterError(f"{label} is missing") from error
    if root not in resolved.parents or not resolved.is_file():
        raise ScientificAdapterError(f"{label} is not a contained regular file")
    return resolved


def _contained_directory(root: Path, candidate: Path, *, label: str) -> Path:
    if candidate.is_symlink():
        raise ScientificAdapterError(f"{label} must not use symlinked directories")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ScientificAdapterError(f"{label} is not available") from error
    if root not in resolved.parents or not resolved.is_dir():
        raise ScientificAdapterError(f"{label} is not a contained directory")
    return resolved


def _assert_terminal_receipt(receipt: Mapping[str, object], *, mode: str) -> None:
    if (
        receipt.get("schema") != RUNTIME_RECEIPT_SCHEMA
        or receipt.get("mode") != mode
        or receipt.get("status") != "PASS"
    ):
        raise ScientificAdapterError(f"AlphaFold 3 {mode} receipt is not a terminal PASS")
    execution = receipt.get("execution")
    image = receipt.get("image")
    if (
        not isinstance(execution, Mapping)
        or execution.get("exit_code") != 0
        or execution.get("terminal_state") != "succeeded"
        or not isinstance(image, Mapping)
        or image.get("runtime_id") != MODEL_ID
        or image.get("upstream_commit") != SOURCE_REVISION
    ):
        raise ScientificAdapterError(f"AlphaFold 3 {mode} receipt execution identity is invalid")


def _canonical_tar(files: tuple[tuple[str, bytes], ...]) -> bytes:
    """Return a reproducible, path-free handoff archive."""

    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, content in files:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mode = 0o440
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def collect_data(workspace: Path) -> CollectedOutput:
    """Verify and package the runtime's portable CPU-to-GPU handoff."""

    try:
        root = workspace.resolve(strict=True)
    except OSError as error:
        raise ScientificAdapterError("AlphaFold 3 data workspace is not available") from error
    output_root = _contained_directory(root, root / DATA_OUTPUT_DIR, label="AlphaFold 3 data output")
    handoff_root = _contained_directory(output_root, output_root / HANDOFF_DIR_NAME, label="AlphaFold 3 data handoff")

    receipt_path = _contained_regular_file(root, root / DATA_RECEIPT_FILENAME, label="AlphaFold 3 data receipt")
    receipt = _load_json_file(receipt_path, label="AlphaFold 3 data receipt", maximum_bytes=MAX_METADATA_BYTES)
    _assert_terminal_receipt(receipt, mode="data")
    index_path = _contained_regular_file(handoff_root, handoff_root / HANDOFF_INDEX, label="AlphaFold 3 handoff index")
    index = _load_json_file(index_path, label="AlphaFold 3 handoff index", maximum_bytes=MAX_METADATA_BYTES)
    expected_index_fields = {"schema", "count", "fold_jobs", "entries", "paths_are_relative_to"}
    entries = index.get("entries")
    fold_jobs = index.get("fold_jobs")
    if (
        set(index) != expected_index_fields
        or index.get("schema") != HANDOFF_SCHEMA
        or index.get("paths_are_relative_to") != "the directory containing this index"
        or not isinstance(entries, list)
        or not 1 <= len(entries) <= MAX_HANDOFF_FILES
        or index.get("count") != len(entries)
        or not isinstance(fold_jobs, list)
        or fold_jobs != [entry.get("fold_job") if isinstance(entry, Mapping) else None for entry in entries]
        or len({str(value) for value in fold_jobs}) != len(fold_jobs)
    ):
        raise ScientificAdapterError("AlphaFold 3 handoff index identity or cardinality is invalid")

    archived: list[tuple[str, bytes]] = [(HANDOFF_INDEX, index_path.read_bytes())]
    total_bytes = len(archived[0][1])
    for position, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, Mapping) or set(raw_entry) != {"fold_job", "relative_path", "bytes", "sha256"}:
            raise ScientificAdapterError(f"AlphaFold 3 handoff entry {position} has an unexpected shape")
        fold_job = raw_entry["fold_job"]
        relative = raw_entry["relative_path"]
        if (
            not _is_fold_job_name(fold_job)
            or not isinstance(relative, str)
            or relative != f"{fold_job}/{fold_job}_data.json"
        ):
            raise ScientificAdapterError(f"AlphaFold 3 handoff entry {position} has an invalid relative path")
        path = PurePosixPath(relative)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ScientificAdapterError(f"AlphaFold 3 handoff entry {position} escapes its artifact root")
        payload_path = _contained_regular_file(
            handoff_root,
            handoff_root / relative,
            label=f"AlphaFold 3 handoff entry {position}",
        )
        content = payload_path.read_bytes()
        recorded_size = raw_entry["bytes"]
        recorded_digest = raw_entry["sha256"]
        if (
            isinstance(recorded_size, bool)
            or not isinstance(recorded_size, int)
            or recorded_size != len(content)
            or not isinstance(recorded_digest, str)
            or recorded_digest != hashlib.sha256(content).hexdigest()
        ):
            raise ScientificAdapterError(f"AlphaFold 3 handoff entry {position} digest or size is invalid")
        total_bytes += len(content)
        if total_bytes > MAX_HANDOFF_BYTES:
            raise ScientificAdapterError("AlphaFold 3 data handoff exceeds the byte bound")
        archived.append((relative, content))

    archive_bytes = _canonical_tar(tuple(archived))
    if len(archive_bytes) > MAX_HANDOFF_BYTES:
        raise ScientificAdapterError("AlphaFold 3 data handoff archive exceeds the byte bound")
    digest = hashlib.sha256(archive_bytes).hexdigest()
    artifact_id = f"af3.handoff.{digest}"
    return CollectedOutput(
        manifest={
            "schema": ARTIFACT_MANIFEST_SCHEMA,
            "manifest_id": f"af3.handoff.{digest[:32]}",
            "entries": [
                {
                    "name": HANDOFF_NAME,
                    "semantic_type": "alphafold3-data-handoff/v1",
                    "artifact": ArtifactPointer(
                        artifact_id=artifact_id,
                        sha256=digest,
                        size_bytes=len(archive_bytes),
                        media_type="application/x-tar",
                    ).to_dict(),
                }
            ],
        },
        blobs={artifact_id: archive_bytes},
    )


def _bounded_metric(summary: Mapping[str, object], name: str, *, nullable: bool = False) -> None:
    value = summary.get(name)
    if value is None and nullable:
        return
    minimum, maximum = _SUMMARY_BOUNDS[name]
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise ScientificAdapterError(f"AlphaFold 3 summary confidence {name} is invalid")


def _validate_summary(path: Path, *, label: str) -> None:
    summary = _load_json_file(path, label=label, maximum_bytes=MAX_CONFIDENCE_BYTES)
    if not {"ptm", "iptm", "ranking_score", "fraction_disordered", "has_clash"} <= set(summary):
        raise ScientificAdapterError(f"{label} lacks the upstream summary confidence fields")
    _bounded_metric(summary, "ptm")
    _bounded_metric(summary, "iptm", nullable=True)
    _bounded_metric(summary, "ranking_score")
    _bounded_metric(summary, "fraction_disordered")
    if summary["has_clash"] not in (0, 1, 0.0, 1.0, True, False):
        raise ScientificAdapterError(f"{label} has an invalid clash flag")


def _validate_structure(path: Path, *, label: str) -> int:
    content = path.read_bytes()
    if not 1 <= len(content) <= MAX_STRUCTURE_BYTES:
        raise ScientificAdapterError(f"{label} is outside the structure byte bound")
    loaded = LoadedArtifact(
        name=label,
        semantic_type="protein-structure-mmcif/v1",
        pointer=ArtifactPointer(
            artifact_id=f"structure.{hashlib.sha256(label.encode()).hexdigest()[:16]}",
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            media_type="chemical/x-mmcif",
        ),
        content=content,
    )
    atoms = structure_atom_count(loaded, require_two_chains=False)
    if atoms < 10:
        raise ScientificAdapterError(f"{label} must contain at least ten ATOM/HETATM records")
    return atoms


def collect_result(request_value: object, workspace: Path) -> CollectedOutput:
    """Validate the upstream AlphaFold 3 output layout and publish its closure.

    The runtime's terminal PASS receipt is required first.  Every fold job then
    needs the upstream top-level model, its summary and full confidences, the
    ranking table, and at least one ``seed-<s>_sample-<i>`` directory whose model
    and summary are themselves valid.  Metrics must sit inside their published
    ranges and every structure must carry real atom records, so an empty or
    truncated run can never be published as a result.
    """

    request, parameters = _request(request_value)
    try:
        root = workspace.resolve(strict=True)
    except OSError as error:
        raise ScientificAdapterError("AlphaFold 3 inference workspace is not available") from error
    receipt_path = _contained_regular_file(
        root, root / INFERENCE_RECEIPT_FILENAME, label="AlphaFold 3 inference receipt"
    )
    receipt = _load_json_file(receipt_path, label="AlphaFold 3 inference receipt", maximum_bytes=MAX_METADATA_BYTES)
    _assert_terminal_receipt(receipt, mode="inference")
    parameter_identity = receipt.get("parameters")
    if (
        not isinstance(parameter_identity, Mapping)
        or parameter_identity.get("artifact_id") != PARAMETERS_ARTIFACT
        or parameter_identity.get("sha256") != PARAMETERS_SHA256
        or parameter_identity.get("size_bytes") != PARAMETERS_SIZE_BYTES
        or parameter_identity.get("path") != PARAMETERS_MOUNT_PATH
    ):
        raise ScientificAdapterError("AlphaFold 3 inference receipt does not bind the exact licensed parameters")
    output_root = _contained_directory(root, root / INFERENCE_OUTPUT_DIR, label="AlphaFold 3 inference output")
    job_dirs = sorted(
        item
        for item in output_root.iterdir()
        if item.is_dir() and not item.is_symlink() and _is_fold_job_name(item.name)
    )
    if not 1 <= len(job_dirs) <= MAX_HANDOFF_FILES:
        raise ScientificAdapterError("AlphaFold 3 inference produced no bounded fold job output")
    if parameters.fold_job is not None and [item.name for item in job_dirs] != [parameters.fold_job]:
        raise ScientificAdapterError("AlphaFold 3 inference output does not match the selected fold job")

    entries: list[tuple[str, str, Path, bool]] = [
        ("inference-receipt", "alphafold3-runtime-receipt-json/v1", receipt_path, False)
    ]
    total_atoms = 0
    structure_count = 0
    for job_dir in job_dirs:
        job = job_dir.name
        model = _contained_regular_file(job_dir, job_dir / f"{job}_model.cif", label=f"{job} model")
        summary = _contained_regular_file(
            job_dir, job_dir / f"{job}_summary_confidences.json", label=f"{job} summary confidences"
        )
        confidences = _contained_regular_file(job_dir, job_dir / f"{job}_confidences.json", label=f"{job} confidences")
        ranking = _contained_regular_file(job_dir, job_dir / "ranking_scores.csv", label=f"{job} ranking scores")
        if confidences.stat().st_size > MAX_CONFIDENCE_BYTES:
            raise ScientificAdapterError(f"{job} confidences exceed the byte bound")
        total_atoms += _validate_structure(model, label=f"{job} model")
        structure_count += 1
        _validate_summary(summary, label=f"{job} summary confidences")
        entries.extend(
            (
                (f"{job}.model", "protein-structure-mmcif/v1", model, False),
                (f"{job}.summary-confidences", "alphafold3-summary-confidences-json/v1", summary, False),
                (f"{job}.confidences", "alphafold3-confidences-json/v1", confidences, False),
                (f"{job}.ranking-scores", "alphafold3-ranking-scores-csv/v1", ranking, True),
            )
        )
        samples = sorted(
            item
            for item in job_dir.iterdir()
            if item.is_dir() and not item.is_symlink() and _SAMPLE_DIR.fullmatch(item.name)
        )
        if not 1 <= len(samples) <= MAX_SAMPLES_PER_JOB:
            raise ScientificAdapterError(f"{job} has no bounded seed/sample outputs")
        for sample_dir in samples:
            sample_model = _contained_regular_file(
                sample_dir, sample_dir / "model.cif", label=f"{job} {sample_dir.name} model"
            )
            sample_summary = _contained_regular_file(
                sample_dir,
                sample_dir / "summary_confidences.json",
                label=f"{job} {sample_dir.name} summary confidences",
            )
            total_atoms += _validate_structure(sample_model, label=f"{job} {sample_dir.name} model")
            structure_count += 1
            _validate_summary(sample_summary, label=f"{job} {sample_dir.name} summary confidences")
            entries.extend(
                (
                    (f"{job}.{sample_dir.name}.model", "protein-structure-mmcif/v1", sample_model, False),
                    (
                        f"{job}.{sample_dir.name}.summary-confidences",
                        "alphafold3-summary-confidences-json/v1",
                        sample_summary,
                        False,
                    ),
                )
            )
    validation = {
        "validator_id": VALIDATOR_ID,
        "status": "passed",
        "model_revision": SOURCE_REVISION,
        "fold_jobs": [item.name for item in job_dirs],
        "structure_count": structure_count,
        "atom_count": total_atoms,
        "request_sha256": hashlib.sha256(
            json.dumps(request.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    validation_path = root / ".fs2" / "alphafold3-validation.json"
    validation_path.parent.mkdir(parents=True, exist_ok=True)
    validation_path.write_text(json.dumps(validation, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    entries.append(("semantic-validation", "alphafold3-semantic-validation-json/v1", validation_path, False))
    return collect_output_files(
        root,
        tuple(entries),
        manifest_id=f"alphafold3.results.{hashlib.sha256(SOURCE_REVISION.encode('utf-8')).hexdigest()[:24]}",
        maximum_total_bytes=MAX_RESULT_BYTES,
    )


def collect_stage_output(collector_id: str, request_value: object, workspace: Path) -> CollectedOutput:
    """Dispatch only collector identities frozen into this adapter's plan."""

    if collector_id == DATA_COLLECTOR_ID:
        return collect_data(workspace)
    if collector_id == RESULT_COLLECTOR_ID:
        return collect_result(request_value, workspace)
    raise ScientificAdapterError(f"unsupported AlphaFold 3 collector identity {collector_id!r}")


def compile_run(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
) -> AdapterExecutionPlan:
    """Compile one raw AF3 request into the current CPU and GPU scheduler stages."""

    request, parameters = _request(request_value)
    assert_profile_identity(
        profile,
        model_id=MODEL_ID,
        repository=SOURCE_REPOSITORY,
        revision=SOURCE_REVISION,
        parameter_schema=PARAMETER_SCHEMA,
        request=request,
    )
    _assert_deployment_authorization(profile)

    data_root = run_workspace(MODEL_ID, operation_id, f"{DATA_STAGE_ID}-main")
    inference_root = run_workspace(MODEL_ID, operation_id, f"{INFERENCE_STAGE_ID}-main")
    handoff_artifact = logical_stage_artifact(operation_id, DATA_STAGE_ID, "main")
    result_artifact = logical_stage_artifact(operation_id, INFERENCE_STAGE_ID, "main")

    data_input = f"{data_root}/input/{FOLD_INPUT_FILENAME}"
    data_output = f"{data_root}/{DATA_OUTPUT_DIR}"
    data_argv = (
        *RUNTIME_COMMAND,
        "data",
        "--json-path",
        data_input,
        "--output-dir",
        data_output,
        "--reference-receipt",
        REFERENCE_RECEIPT_PATH,
        "--threads",
        DATA_CPU,
        "--cpu-request",
        DATA_CPU,
        "--receipt",
        f"{data_root}/{DATA_RECEIPT_FILENAME}",
    )
    _assert_clean_argv(data_argv)
    data = StageInvocation(
        stage_id=DATA_STAGE_ID,
        shard_id="main",
        argv=data_argv,
        environment=(
            ("FS2_NETWORK_MODE", "offline"),
            ("FS2_AF3_REFERENCE_MOUNT", REFERENCE_MOUNT_PATH),
            ("FS2_SCIENTIFIC_COLLECTOR_ID", DATA_COLLECTOR_ID),
            ("FS2_SCIENTIFIC_VALIDATOR_ID", DATA_VALIDATOR_ID),
        ),
        working_directory=data_root,
        consumes=(request.input_manifest.artifact_id,),
        produces=handoff_artifact,
        collector_id=DATA_COLLECTOR_ID,
        validator_id=DATA_VALIDATOR_ID,
        handoff_name=HANDOFF_NAME,
        materializations=(
            ArtifactMaterialization(
                artifact_id=request.input_manifest.artifact_id,
                destination=data_input,
                mode=MaterializationMode.COPY_FILE,
                compression=request.input_manifest.compression,
            ),
        ),
        runtime_artifacts=(REFERENCE_ARTIFACT,),
        runtime_mounts=(mount_contract(DATA_STAGE_ID),),
    )

    inference_input = f"{inference_root}/{HANDOFF_MOUNT_DIR}"
    inference_argv_values = [
        *RUNTIME_COMMAND,
        "inference",
        "--handoff-dir",
        inference_input,
        "--output-dir",
        f"{inference_root}/{INFERENCE_OUTPUT_DIR}",
        "--receipt",
        f"{inference_root}/{INFERENCE_RECEIPT_FILENAME}",
    ]
    if parameters.fold_job is not None:
        inference_argv_values.extend(("--fold-job", parameters.fold_job))
    inference_argv = tuple(inference_argv_values)
    _assert_clean_argv(inference_argv)
    inference = StageInvocation(
        stage_id=INFERENCE_STAGE_ID,
        shard_id="main",
        argv=inference_argv,
        environment=(
            ("FS2_NETWORK_MODE", "offline"),
            ("HF_HUB_OFFLINE", "1"),
            ("TRANSFORMERS_OFFLINE", "1"),
            ("FS2_AF3_PARAMETER_PATH", PARAMETERS_MOUNT_PATH),
            ("FS2_SCIENTIFIC_COLLECTOR_ID", RESULT_COLLECTOR_ID),
            ("FS2_SCIENTIFIC_VALIDATOR_ID", VALIDATOR_ID),
        ),
        working_directory=inference_root,
        consumes=(handoff_artifact,),
        produces=result_artifact,
        collector_id=RESULT_COLLECTOR_ID,
        validator_id=VALIDATOR_ID,
        materializations=(
            ArtifactMaterialization(
                artifact_id=handoff_artifact,
                destination=inference_input,
                mode=MaterializationMode.EXTRACT_TAR,
                compression=None,
            ),
        ),
        runtime_artifacts=(PARAMETERS_ARTIFACT,),
        runtime_mounts=(mount_contract(INFERENCE_STAGE_ID),),
    )

    return build_execution_plan(
        model_id=MODEL_ID,
        variant_id=VARIANT_ID,
        source_revision=SOURCE_REVISION,
        request=request,
        profile=profile,
        expansions=None,
        invocations=(data, inference),
        required_model_artifacts=(REFERENCE_ARTIFACT, PARAMETERS_ARTIFACT),
    )


__all__ = [
    "ADMISSION_BLOCKER",
    "DATA_COLLECTOR_ID",
    "DATA_STAGE_ID",
    "DATA_VALIDATOR_ID",
    "HANDOFF_NAME",
    "INFERENCE_STAGE_ID",
    "MODEL_ID",
    "PARAMETER_SCHEMA",
    "PARAMETERS_ARTIFACT",
    "Parameters",
    "REFERENCE_ARTIFACT",
    "RESULT_COLLECTOR_ID",
    "RUNTIME_IMAGE",
    "RUNTIME_IMAGE_DIGEST",
    "RUNTIME_MOUNTS",
    "SOURCE_REPOSITORY",
    "SOURCE_REVISION",
    "STAGE_EXECUTION_CONTRACTS",
    "VALIDATOR_ID",
    "VARIANT_ID",
    "collect_data",
    "collect_result",
    "collect_stage_output",
    "compile_run",
    "mount_contract",
    "parameters_mount",
    "reference_mount",
]

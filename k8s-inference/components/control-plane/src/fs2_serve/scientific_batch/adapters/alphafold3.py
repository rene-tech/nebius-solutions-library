"""Native AlphaFold 3 v3.0.4 adapter for the production scientific controller.

The adapter deliberately separates the public reference-data plane from the
private academic parameter plane. ``data-pipeline`` gets the entire reference
root at ``/reference-data`` (never a Kubernetes subPath) and no parameters.
``inference`` gets the validated, relocatable handoff and the single private
parameter file, but no reference-data mount.

Academic execution authorization is deployment-owned. A caller supplies a
normal scientific input manifest and never a request-time licence receipt.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from ..models import (
    AdapterExecutionPlan,
    ArtifactAccessContext,
    ArtifactMaterialization,
    MaterializationMode,
    RuntimeArtifactMount,
    ScientificInputArtifact,
    StageInvocation,
)
from . import CollectedArtifactFile, CollectedStageOutput, CollectionPendingError
from .common import (
    PublicRunRequest,
    ScientificAdapterError,
    assert_profile_identity,
    build_execution_plan,
    logical_stage_artifact,
    parse_public_request,
    run_workspace,
    strict_object,
)

MODEL_ID = "alphafold3"
VARIANT_ID = "upstream-v3-0-4"
SOURCE_REPOSITORY = "google-deepmind/alphafold3"
SOURCE_REVISION = "85c4d20505fd5cef05eac22b534d4e793971ae69"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/alphafold3-upstream-v3-0-4-parameters/v1"
VALIDATOR_ID = "alphafold3-upstream-v3-0-4"

RUNTIME_IMAGE_DIGEST = "sha256:0cde199e8473a2d069c896c4f8d67a58b31e00bfb87c3660aed154693699e03e"
RUNTIME_COMMAND = ("/alphafold3_venv/bin/python3", "/opt/fs2/af3_runtime.py")

PARAMETERS_ARTIFACT = "alphafold3-parameters"
PARAMETERS_SHA256 = "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
PARAMETERS_SIZE_BYTES = 1_020_545_840
PARAMETERS_CLAIM = "academic-assets-runtime-rwx"
PARAMETERS_CLAIM_NAMESPACE = "fs2-academic-poc"
PARAMETERS_SOURCE_SUB_PATH = "alphafold3/af3.bin.zst"
PARAMETERS_MOUNT_PATH = "/models/af3.bin.zst"
PARAMETERS_SUPPLEMENTAL_GROUP = 65_532

REFERENCE_ARTIFACT = "alphafold3-public-databases-v3.0"
REFERENCE_REVISION = "v3.0-paper-snapshot-2022-09-28"
REFERENCE_HOST_ROOT = "/mnt/fs2-reference-data/data"
REFERENCE_MOUNT_PATH = "/reference-data"
REFERENCE_INVENTORY_MARKER = ".fs2-manifest-sha256"
REFERENCE_RECEIPT_PATH = f"{REFERENCE_MOUNT_PATH}/receipts/{REFERENCE_ARTIFACT}/{REFERENCE_REVISION}.json"

DATA_STAGE_ID = "data-pipeline"
INFERENCE_STAGE_ID = "inference"
EXECUTION_NAMESPACE = "fs2-academic-poc"
DATA_LOCAL_QUEUE = "academic-scientific-cpu"
DATA_CLUSTER_QUEUE = "reference-data-cpu"
INFERENCE_LOCAL_QUEUE = "academic-scientific"
DATA_CPU_MILLIS = 16_000
DATA_CPU = "16"
DATA_MEMORY_BYTES = 64 * 1024**3
DATA_MEMORY = "64Gi"
DATA_EPHEMERAL_STORAGE_BYTES = 32 * 1024**3
DATA_EPHEMERAL_STORAGE = "32Gi"

DATA_COLLECTOR_ID = "alphafold3-data-collector-v1"
DATA_VALIDATOR_ID = "alphafold3-data-validator-v1"
RESULT_COLLECTOR_ID = "alphafold3-result-collector-v1"

INPUT_MANIFEST_MEDIA_TYPE = "application/vnd.fs2.scientific-manifest+json"
FOLD_INPUT_ID = "fold-input"
FOLD_INPUT_SEMANTIC_TYPE = "alphafold3-fold-input/v1"
FOLD_INPUT_MEDIA_TYPE = "application/json"
REQUIRES_VERIFIED_INPUT_ARTIFACTS = True

FOLD_INPUT_FILENAME = "fold_input.json"
DATA_OUTPUT_DIR = "data-output"
HANDOFF_DIR_NAME = "fs2-af3-handoff"
HANDOFF_MOUNT_DIR = "handoff"
HANDOFF_INDEX = "index.json"
HANDOFF_SCHEMA = "fs2-serve.nebius.ai/alphafold3-data-handoff/v2"
HANDOFF_PACKAGE = "handoff.tar"
DATA_RECEIPT_FILENAME = "data-runtime-receipt.json"
INFERENCE_RECEIPT_FILENAME = "inference-runtime-receipt.json"
RUNTIME_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/alphafold3-runtime-receipt/v1"

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_HANDOFF_FILES = 64
# The companion materializer accepts at most 256 MiB compressed and expanded.
# Keeping the producer contract identical bounds the collector's two resident
# copies (validated members plus canonical tar) to about 512 MiB, below the
# 2 GiB artifact-sidecar memory envelope.
MAX_HANDOFF_BYTES = 256 * 1024 * 1024
MAX_HANDOFF_CONTENT_BYTES = MAX_HANDOFF_BYTES - 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024 * 1024
FORBIDDEN_ARGV_TOKENS = (
    "fs2-run-alphafold3",
    "/databases",
    "--input-json",
    "--processed-json",
    "--handoff-tar",
    "--runtime-localization-marker",
)
ADMISSION_BLOCKER = "AcademicDeploymentAuthorizationMissing"

RUNTIME_MOUNTS: Mapping[str, RuntimeArtifactMount] = MappingProxyType(
    {
        DATA_STAGE_ID: RuntimeArtifactMount(
            artifact_id=REFERENCE_ARTIFACT,
            mount_path=REFERENCE_MOUNT_PATH,
            read_only=True,
            # Receipt, dataset marker and sibling manifest all live below this
            # root. A subPath mount would hide two of those objects.
            sub_path=None,
        ),
        INFERENCE_STAGE_ID: RuntimeArtifactMount(
            artifact_id=PARAMETERS_ARTIFACT,
            mount_path=PARAMETERS_MOUNT_PATH,
            read_only=True,
            sub_path=PARAMETERS_SOURCE_SUB_PATH,
            expected_content_sha256=PARAMETERS_SHA256,
            supplemental_groups=(PARAMETERS_SUPPLEMENTAL_GROUP,),
        ),
    }
)


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
        if fold_job is not None and (
            not isinstance(fold_job, str)
            or not 1 <= len(fold_job) <= 128
            or fold_job.strip() != fold_job
            or fold_job in {".", ".."}
            or any(character in fold_job for character in ("/", "\\"))
            or any(ord(character) < 32 or ord(character) == 127 for character in fold_job)
        ):
            raise ScientificAdapterError("fold_job must be a bounded AlphaFold 3 job name")
        return cls(input_mode="raw", fold_job=fold_job if isinstance(fold_job, str) else None)


def _request(value: object) -> tuple[PublicRunRequest, Parameters]:
    request = parse_public_request(value, maximum_input_bytes=MAX_INPUT_BYTES)
    if request.input_manifest.media_type != INPUT_MANIFEST_MEDIA_TYPE:
        raise ScientificAdapterError("AlphaFold 3 input_manifest must identify a scientific manifest")
    if request.input_manifest.compression not in {None, "none"}:
        raise ScientificAdapterError("AlphaFold 3 input manifest must not be compressed")
    return request, Parameters.parse(request.parameters)


def _verified_fold_input(
    request: PublicRunRequest,
    input_artifacts: tuple[ScientificInputArtifact, ...],
) -> ScientificInputArtifact:
    if len(input_artifacts) != 1:
        raise ScientificAdapterError("AlphaFold 3 input manifest must contain exactly one fold-input")
    item = input_artifacts[0]
    if item.logical_artifact_id != FOLD_INPUT_ID or item.semantic_type != FOLD_INPUT_SEMANTIC_TYPE:
        raise ScientificAdapterError("AlphaFold 3 input manifest has no canonical fold-input entry")
    if item.media_type != FOLD_INPUT_MEDIA_TYPE or item.compression not in {None, "none"}:
        raise ScientificAdapterError("AlphaFold 3 fold-input must be uncompressed application/json")
    if not 1 <= item.size_bytes <= MAX_INPUT_BYTES:
        raise ScientificAdapterError("AlphaFold 3 fold-input size is outside the adapter bound")
    if str(item.artifact_id) == request.input_manifest.artifact_id:
        raise ScientificAdapterError("AlphaFold 3 fold input and its manifest must be distinct artifacts")
    return item


def _assert_deployment_authorization(profile: Mapping[str, object]) -> None:
    """Require operator-owned authorization, never a caller-provided receipt."""

    access = profile.get("access")
    if not isinstance(access, Mapping) or dict(access) != {
        "profile": "academic",
        "state": "verified",
        "receipt_digest": None,
        "credentials_embedded": False,
    }:
        raise ScientificAdapterError(ADMISSION_BLOCKER)


def _assert_parameter_requirement(profile: Mapping[str, object]) -> None:
    raw = profile.get("runtime_artifacts", [])
    if not isinstance(raw, list):
        raise ScientificAdapterError("AlphaFold 3 runtime artifact profile is invalid")
    matches = [item for item in raw if isinstance(item, Mapping) and item.get("artifact_id") == PARAMETERS_ARTIFACT]
    if len(matches) != 1:
        raise ScientificAdapterError("AlphaFold 3 profile has no exact private parameter artifact")
    identity = matches[0].get("content_identity")
    files = matches[0].get("file_manifest")
    if (
        not isinstance(identity, Mapping)
        or identity.get("digest_sha256") != PARAMETERS_SHA256
        or identity.get("size_bytes") != PARAMETERS_SIZE_BYTES
        or files
        != [
            {
                "path": "af3.bin.zst",
                "sha256": PARAMETERS_SHA256,
                "size_bytes": PARAMETERS_SIZE_BYTES,
            }
        ]
        or matches[0].get("required_files") != ["af3.bin.zst"]
    ):
        raise ScientificAdapterError("AlphaFold 3 private parameter identity differs from the reviewed asset")


def _cpu_count(profile: Mapping[str, object]) -> str:
    workload = profile.get("workload")
    stages = workload.get("stages") if isinstance(workload, Mapping) else None
    if not isinstance(stages, list):
        raise ScientificAdapterError("AlphaFold 3 workload stages are missing")
    stage = next(
        (item for item in stages if isinstance(item, Mapping) and item.get("id") == DATA_STAGE_ID),
        None,
    )
    placement = stage.get("placement") if isinstance(stage, Mapping) else None
    resources = stage.get("resources") if isinstance(stage, Mapping) else None
    if (
        not isinstance(placement, Mapping)
        or placement.get("class") != "reference-data"
        or not isinstance(resources, Mapping)
        or resources.get("cpu_millis") != DATA_CPU_MILLIS
        or resources.get("memory_bytes") != DATA_MEMORY_BYTES
        or resources.get("ephemeral_storage_bytes") != DATA_EPHEMERAL_STORAGE_BYTES
    ):
        raise ScientificAdapterError("AlphaFold 3 data-pipeline must use the 16 CPU/64Gi reference-data lane")
    return DATA_CPU


def _assert_clean_argv(argv: tuple[str, ...]) -> None:
    for token in FORBIDDEN_ARGV_TOKENS:
        if any(value == token or value.startswith(f"{token}=") for value in argv):
            raise ScientificAdapterError(f"AlphaFold 3 argv uses retired command surface {token}")


def _argument(invocation: StageInvocation, name: str) -> str:
    if invocation.argv.count(name) != 1 or invocation.argv.index(name) + 1 >= len(invocation.argv):
        raise ScientificAdapterError(f"AlphaFold 3 invocation has no exact {name} argument")
    return invocation.argv[invocation.argv.index(name) + 1]


def _load_json_file(path: Path, *, label: str, maximum_bytes: int) -> Mapping[str, object]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise CollectionPendingError(f"{label} is not available yet") from error
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
    """Resolve a file beneath *root* without accepting symlinked path parts."""

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
        raise CollectionPendingError(f"{label} is not available yet") from error
    if root not in resolved.parents or not resolved.is_file():
        raise ScientificAdapterError(f"{label} is not a contained regular file")
    return resolved


def _assert_terminal_receipt(receipt: Mapping[str, object], *, mode: str) -> None:
    execution = receipt.get("execution")
    image = receipt.get("image")
    if (
        receipt.get("schema") != RUNTIME_RECEIPT_SCHEMA
        or receipt.get("mode") != mode
        or receipt.get("status") != "PASS"
        or not isinstance(execution, Mapping)
        or execution.get("exit_code") != 0
        or execution.get("terminal_state") != "succeeded"
        or not isinstance(image, Mapping)
        or image.get("runtime_id") != MODEL_ID
        or image.get("upstream_commit") != SOURCE_REVISION
        or image.get("parameters_embedded") is not False
        or image.get("reference_databases_embedded") is not False
    ):
        raise ScientificAdapterError(f"AlphaFold 3 {mode} receipt is not an exact terminal PASS")


def _canonical_tar(files: tuple[tuple[str, bytes], ...]) -> bytes:
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


def collect_data(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    """Validate and package the runtime's relocatable CPU-to-GPU handoff."""

    if (
        invocation.stage_id != DATA_STAGE_ID
        or invocation.collector_id != DATA_COLLECTOR_ID
        or invocation.validator_id != DATA_VALIDATOR_ID
        or invocation.handoff_name is None
    ):
        raise ScientificAdapterError("AlphaFold 3 data collector received another stage contract")
    try:
        root = workspace.resolve(strict=True)
        output = root / DATA_OUTPUT_DIR
        handoff = output / HANDOFF_DIR_NAME
        if output.is_symlink() or handoff.is_symlink():
            raise ScientificAdapterError("AlphaFold 3 data handoff must not use symlinked directories")
        output_root = output.resolve(strict=True)
        handoff_root = handoff.resolve(strict=True)
    except OSError as error:
        raise CollectionPendingError("AlphaFold 3 data handoff is not available yet") from error
    if root not in output_root.parents or output_root not in handoff_root.parents or not handoff_root.is_dir():
        raise ScientificAdapterError("AlphaFold 3 data handoff is not a contained directory")

    receipt = _load_json_file(
        _contained_regular_file(root, root / DATA_RECEIPT_FILENAME, label="AlphaFold 3 data receipt"),
        label="AlphaFold 3 data receipt",
        maximum_bytes=MAX_METADATA_BYTES,
    )
    _assert_terminal_receipt(receipt, mode="data")
    expected_input_identity = {
        "artifact_id": _argument(invocation, "--raw-input-artifact-id"),
        "sha256": _argument(invocation, "--raw-input-sha256"),
    }
    if receipt.get("input_identity") != expected_input_identity:
        raise ScientificAdapterError("AlphaFold 3 data receipt lost the frozen fold-input identity")
    reference = receipt.get("reference_data")
    cpu = receipt.get("cpu_envelope")
    if (
        not isinstance(reference, Mapping)
        or reference.get("bundle_id") != REFERENCE_ARTIFACT
        or reference.get("revision") != REFERENCE_REVISION
        or reference.get("host_root") != REFERENCE_HOST_ROOT
        or reference.get("mount_path") != REFERENCE_MOUNT_PATH
        or reference.get("single_root_mount") is not True
        or reference.get("content_tree_sha256") == reference.get("manifest_sha256")
        or not isinstance(cpu, Mapping)
        or cpu.get("msa_threads") != 16
        or cpu.get("cpu_request") != 16
        or cpu.get("upstream_default_overridden") is not True
    ):
        raise ScientificAdapterError("AlphaFold 3 data receipt lost its reference or CPU identity")

    index_path = _contained_regular_file(handoff_root, handoff_root / HANDOFF_INDEX, label="AlphaFold 3 handoff index")
    index_bytes = index_path.read_bytes()
    index = _load_json_file(index_path, label="AlphaFold 3 handoff index", maximum_bytes=MAX_METADATA_BYTES)
    entries = index.get("entries")
    fold_jobs = index.get("fold_jobs")
    expected_index_fields = {
        "schema",
        "input_identity",
        "count",
        "fold_jobs",
        "entries",
        "paths_are_relative_to",
    }
    if (
        set(index) != expected_index_fields
        or index.get("schema") != HANDOFF_SCHEMA
        or index.get("input_identity") != expected_input_identity
        or index.get("paths_are_relative_to") != "the directory containing this index"
        or not isinstance(entries, list)
        or not 1 <= len(entries) <= MAX_HANDOFF_FILES
        or index.get("count") != len(entries)
        or not isinstance(fold_jobs, list)
        or fold_jobs != [entry.get("fold_job") if isinstance(entry, Mapping) else None for entry in entries]
        or len(set(str(value) for value in fold_jobs)) != len(fold_jobs)
    ):
        raise ScientificAdapterError("AlphaFold 3 handoff index identity or cardinality is invalid")
    receipt_handoff = receipt.get("handoff")
    if (
        not isinstance(receipt_handoff, Mapping)
        or receipt_handoff.get("schema") != HANDOFF_SCHEMA
        or receipt_handoff.get("input_identity") != expected_input_identity
        or receipt_handoff.get("count") != len(entries)
        or receipt_handoff.get("fold_jobs") != fold_jobs
        or receipt_handoff.get("entries") != entries
        or receipt_handoff.get("handoff_dirname") != HANDOFF_DIR_NAME
        or receipt_handoff.get("index_sha256") != hashlib.sha256(index_bytes).hexdigest()
    ):
        raise ScientificAdapterError("AlphaFold 3 data receipt does not bind the handoff index")

    archived: list[tuple[str, bytes]] = [(HANDOFF_INDEX, index_bytes)]
    total_bytes = len(index_bytes)
    for position, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, Mapping) or set(raw_entry) != {"fold_job", "relative_path", "bytes", "sha256"}:
            raise ScientificAdapterError(f"AlphaFold 3 handoff entry {position} has an unexpected shape")
        fold_job = raw_entry["fold_job"]
        relative = raw_entry["relative_path"]
        if (
            not isinstance(fold_job, str)
            or not 1 <= len(fold_job) <= 128
            or fold_job.strip() != fold_job
            or fold_job in {".", ".."}
            or any(character in fold_job for character in ("/", "\\"))
            or any(ord(character) < 32 or ord(character) == 127 for character in fold_job)
            or not isinstance(relative, str)
            or relative != f"{fold_job}/{fold_job}_data.json"
        ):
            raise ScientificAdapterError(f"AlphaFold 3 handoff entry {position} has an invalid relative path")
        path = PurePosixPath(relative)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ScientificAdapterError(f"AlphaFold 3 handoff entry {position} escapes its artifact root")
        payload = _contained_regular_file(
            handoff_root,
            handoff_root / relative,
            label=f"AlphaFold 3 handoff entry {position}",
        )
        content = payload.read_bytes()
        size = raw_entry["bytes"]
        digest = raw_entry["sha256"]
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size != len(content)
            or not isinstance(digest, str)
            or digest != hashlib.sha256(content).hexdigest()
        ):
            raise ScientificAdapterError(f"AlphaFold 3 handoff entry {position} digest or size is invalid")
        total_bytes += len(content)
        if total_bytes > MAX_HANDOFF_CONTENT_BYTES:
            raise ScientificAdapterError("AlphaFold 3 data handoff exceeds the byte bound")
        archived.append((relative, content))

    archive_bytes = _canonical_tar(tuple(archived))
    if len(archive_bytes) > MAX_HANDOFF_BYTES:
        raise ScientificAdapterError("AlphaFold 3 data handoff archive exceeds the byte bound")
    package = root / HANDOFF_PACKAGE
    pending = root / f".{HANDOFF_PACKAGE}.partial"
    try:
        with pending.open("wb") as stream:
            stream.write(archive_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, package)
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        pending.unlink(missing_ok=True)
    return CollectedStageOutput(
        artifacts=(
            CollectedArtifactFile(
                name=invocation.handoff_name,
                semantic_type="alphafold3-data-handoff/v1",
                path=package,
                media_type="application/x-tar",
            ),
        ),
        validation={
            "validator_id": invocation.validator_id,
            "status": "passed",
            "sha256": hashlib.sha256(archive_bytes).hexdigest(),
            "fold_jobs": len(entries),
        },
    )


def collect_result(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    """Collect the canonical top-ranked mmCIF and its AF3 confidence summary."""

    if (
        invocation.stage_id != INFERENCE_STAGE_ID
        or invocation.collector_id != RESULT_COLLECTOR_ID
        or invocation.validator_id != VALIDATOR_ID
    ):
        raise ScientificAdapterError("AlphaFold 3 result collector received another stage contract")
    try:
        root = workspace.resolve(strict=True)
    except OSError as error:
        raise CollectionPendingError("AlphaFold 3 inference workspace is not available yet") from error
    receipt = _load_json_file(
        _contained_regular_file(root, root / INFERENCE_RECEIPT_FILENAME, label="AlphaFold 3 inference receipt"),
        label="AlphaFold 3 inference receipt",
        maximum_bytes=MAX_METADATA_BYTES,
    )
    _assert_terminal_receipt(receipt, mode="inference")
    expected_input_identity = {
        "artifact_id": _argument(invocation, "--expected-raw-input-artifact-id"),
        "sha256": _argument(invocation, "--expected-raw-input-sha256"),
    }
    if receipt.get("input_identity") != expected_input_identity:
        raise ScientificAdapterError("AlphaFold 3 inference receipt lost the frozen fold-input identity")
    parameters = receipt.get("parameters")
    if (
        not isinstance(parameters, Mapping)
        or parameters.get("artifact_id") != PARAMETERS_ARTIFACT
        or parameters.get("path") != PARAMETERS_MOUNT_PATH
        or parameters.get("size_bytes") != PARAMETERS_SIZE_BYTES
        or parameters.get("sha256") != PARAMETERS_SHA256
        or parameters.get("identity_kind") != "file-digest"
    ):
        raise ScientificAdapterError("AlphaFold 3 inference receipt lost the private parameter identity")
    output_root = root / "outputs"
    if not output_root.is_dir() or output_root.is_symlink():
        raise CollectionPendingError("AlphaFold 3 inference outputs are not available yet")
    structures = sorted(output_root.rglob("*_model.cif"))
    summaries = sorted(output_root.rglob("*_summary_confidences.json"))
    if len(structures) != 1 or len(summaries) != 1:
        raise ScientificAdapterError("AlphaFold 3 result requires one top model and one confidence summary")
    structure = _contained_regular_file(root, structures[0], label="AlphaFold 3 top model")
    summary = _contained_regular_file(root, summaries[0], label="AlphaFold 3 confidence summary")
    structure_bytes = structure.read_bytes()
    summary_bytes = summary.read_bytes()
    if not structure_bytes.startswith(b"data_") or not any(
        line.startswith((b"ATOM ", b"HETATM ")) for line in structure_bytes.splitlines()
    ):
        raise ScientificAdapterError("AlphaFold 3 top model is not a non-empty mmCIF structure")
    summary_value = _load_json_file(summary, label="AlphaFold 3 confidence summary", maximum_bytes=MAX_METADATA_BYTES)
    ranking = summary_value.get("ranking_score")
    if isinstance(ranking, bool) or not isinstance(ranking, int | float) or not math.isfinite(float(ranking)):
        raise ScientificAdapterError("AlphaFold 3 confidence summary has no finite ranking_score")
    if len(structure_bytes) + len(summary_bytes) > MAX_RESULT_BYTES:
        raise ScientificAdapterError("AlphaFold 3 result exceeds the byte bound")
    return CollectedStageOutput(
        artifacts=(
            CollectedArtifactFile(
                name="structure",
                semantic_type="protein-structure-mmcif/v1",
                path=structure,
                media_type="chemical/x-mmcif",
            ),
            CollectedArtifactFile(
                name="summary-confidence",
                semantic_type="alphafold3-summary-confidence/v1",
                path=summary,
                media_type="application/json",
            ),
        ),
        validation={
            "validator_id": invocation.validator_id,
            "status": "passed",
            "ranking_score": float(ranking),
            "structure_sha256": hashlib.sha256(structure_bytes).hexdigest(),
            "summary_sha256": hashlib.sha256(summary_bytes).hexdigest(),
        },
    )


def mount_contract(stage_id: str) -> RuntimeArtifactMount:
    try:
        return RUNTIME_MOUNTS[stage_id]
    except KeyError as error:
        raise ScientificAdapterError(f"AlphaFold 3 has no mount contract for stage {stage_id}") from error


def compile_run(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
    variant_id: str,
    access_context: ArtifactAccessContext,
    input_artifacts: tuple[ScientificInputArtifact, ...],
) -> AdapterExecutionPlan:
    """Compile one authorized raw AF3 request into CPU then H100 stages."""

    del access_context  # Authorization is operator-owned profile state, not caller input.
    if variant_id != VARIANT_ID:
        raise ScientificAdapterError("route variant_id does not match the AlphaFold 3 adapter")
    request, parameters = _request(request_value)
    fold_input = _verified_fold_input(request, input_artifacts)
    raw_input_artifact_id = str(fold_input.artifact_id)
    raw_input_sha256 = fold_input.digest.removeprefix("sha256:")
    assert_profile_identity(
        profile,
        model_id=MODEL_ID,
        repository=SOURCE_REPOSITORY,
        revision=SOURCE_REVISION,
        parameter_schema=PARAMETER_SCHEMA,
        request=request,
    )
    _assert_deployment_authorization(profile)
    _assert_parameter_requirement(profile)
    cpu_count = _cpu_count(profile)

    data_root = run_workspace(MODEL_ID, operation_id, "data-pipeline-main")
    inference_root = run_workspace(MODEL_ID, operation_id, "inference-main")
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
        cpu_count,
        "--cpu-request",
        cpu_count,
        "--raw-input-artifact-id",
        raw_input_artifact_id,
        "--raw-input-sha256",
        raw_input_sha256,
        "--receipt",
        f"{data_root}/{DATA_RECEIPT_FILENAME}",
    )
    _assert_clean_argv(data_argv)
    data = StageInvocation(
        stage_id=DATA_STAGE_ID,
        shard_id="main",
        argv=data_argv,
        environment=(("FS2_NETWORK_MODE", "offline"),),
        working_directory=data_root,
        consumes=(fold_input.logical_artifact_id,),
        produces=handoff_artifact,
        collector_id=DATA_COLLECTOR_ID,
        validator_id=DATA_VALIDATOR_ID,
        handoff_name="processed-input",
        max_output_artifacts=1,
        max_output_bytes=MAX_HANDOFF_BYTES,
        materializations=(
            ArtifactMaterialization(
                artifact_id=fold_input.logical_artifact_id,
                destination=data_input,
                mode=MaterializationMode.COPY_FILE,
                compression=fold_input.compression,
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
        f"{inference_root}/outputs",
        "--receipt",
        f"{inference_root}/{INFERENCE_RECEIPT_FILENAME}",
        "--expected-raw-input-artifact-id",
        raw_input_artifact_id,
        "--expected-raw-input-sha256",
        raw_input_sha256,
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
        ),
        working_directory=inference_root,
        consumes=(handoff_artifact,),
        produces=result_artifact,
        collector_id=RESULT_COLLECTOR_ID,
        validator_id=VALIDATOR_ID,
        max_output_artifacts=2,
        max_output_bytes=MAX_RESULT_BYTES,
        materializations=(
            ArtifactMaterialization(
                artifact_id=handoff_artifact,
                destination=inference_input,
                mode=MaterializationMode.EXTRACT_TAR,
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
        required_model_artifacts=(PARAMETERS_ARTIFACT, REFERENCE_ARTIFACT),
    )


__all__ = [
    "ADMISSION_BLOCKER",
    "DATA_COLLECTOR_ID",
    "DATA_STAGE_ID",
    "DATA_VALIDATOR_ID",
    "INFERENCE_STAGE_ID",
    "MODEL_ID",
    "PARAMETER_SCHEMA",
    "RESULT_COLLECTOR_ID",
    "RUNTIME_IMAGE_DIGEST",
    "SOURCE_REVISION",
    "VALIDATOR_ID",
    "VARIANT_ID",
    "collect_data",
    "collect_result",
    "compile_run",
    "mount_contract",
]

"""Native academic AlphaFold 3 adapter for the two-stage CPU/GPU contract.

The argv this module composes is the ``af3-runtime`` command surface published
by the runtime image as ``contracts/af3-command-io-contract.json``. Nothing here
is hand-authored: :mod:`tests.test_alphafold3_adapter` binds every template
below to that document, so a runtime successor that changes the surface breaks
the test rather than the cluster.

Two properties of the reference-data plane drive the shape of the CPU stage.
The publisher writes three documents a preprocessing run has to reach - the
terminal receipt, the dataset tree, and the manifest that describes the tree -
and it writes them as siblings under one root. So the stage mounts the whole
read-only root at ``/reference-data`` with no Kubernetes ``subPath``; a subPath
mount of the dataset alone would hide the receipt and the manifest and the
runtime would fail closed. The second property is that the tree digest and the
manifest digest identify different objects, so they are carried separately and
are never allowed to be equal.

Stage separation is absolute. The CPU stage binds reference data and no
parameters; the GPU stage binds parameters and no reference data. The runtime
refuses a stage holding both, and this adapter never composes one.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..models import (
    AdapterExecutionPlan,
    ArtifactAccessContext,
    ArtifactMaterialization,
    MaterializationMode,
    RuntimeArtifactMount,
    ScientificInputArtifact,
    StageInvocation,
)
from . import (
    CollectedArtifactFile,
    CollectedStageOutput,
    CollectionPendingError,
    StageExecutionContract,
)
from .common import (
    ScientificAdapterError,
    assert_artifact_requirement,
    assert_profile_identity,
    bind_compiler_input,
    bounded_int,
    build_execution_plan,
    logical_stage_artifact,
    parse_public_request,
    run_workspace,
    strict_object,
)
from .secondary_structure import collect_confidence_envelope

MODEL_ID = "alphafold3"
VARIANT_ID = "upstream-v3-0-4"
SOURCE_REPOSITORY = "google-deepmind/alphafold3"
SOURCE_REVISION = "85c4d20505fd5cef05eac22b534d4e793971ae69"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/alphafold3-upstream-v3-0-4-parameters/v1"
VALIDATOR_ID = "alphafold3-upstream-v3-0-4"

PARAMETERS_ARTIFACT = "alphafold3-parameters"
PARAMETERS_SHA256 = "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
REFERENCE_ARTIFACT = "alphafold3-public-databases-v3.0"
REFERENCE_REVISION = "v3.0-paper-snapshot-2022-09-28"

DATA_STAGE_ID = "data-pipeline"
INFERENCE_STAGE_ID = "inference"

# The af3-runtime command surface. Templates, not literals with values baked in,
# so the drift test can compare them to the runtime's own contract document.
RUNTIME_COMMAND = ("/alphafold3_venv/bin/python3", "/opt/fs2/af3_runtime.py")
DATA_RUNTIME_ARGS = (
    "data",
    "--json-path",
    "{json_path}",
    "--output-dir",
    "{output_dir}",
    "--reference-receipt",
    "{reference_receipt}",
    "--threads",
    "{msa_threads}",
    "--cpu-request",
    "{cpu_request}",
)
INFERENCE_RUNTIME_ARGS = (
    "inference",
    "--handoff-dir",
    "{handoff_dir}",
    "--output-dir",
    "{output_dir}",
)

# The single read-only reference root. Both the dataset tree and the sibling
# manifest document resolve beneath it, which is why it is mounted whole.
REFERENCE_MOUNT_PATH = "/reference-data"
REFERENCE_MANIFEST_MARKER = ".fs2-manifest-sha256"
PARAMETER_MOUNT_PATH = "/models/af3.bin.zst"

# The runtime image has no flag for the controller's localization marker, but
# StageInvocation requires the marker path to appear in the argv of any stage
# that binds a runtime artifact. The adapter therefore emits this flag and the
# drift test records it as the one argument the runtime successor still owes us.
LOCALIZATION_MARKER_FLAG = "--runtime-localization-marker"
PENDING_RUNTIME_ARGS = frozenset({LOCALIZATION_MARKER_FLAG})

# Command surfaces this adapter must never resurrect. The first two belong to a
# wrapper that no longer exists; the rest are flags of that wrapper. --extra-arg
# is refused because it can override the flags that keep the stages separated.
FORBIDDEN_ARGV_TOKENS = (
    "fs2-run-alphafold3",
    "/databases",
    "--input-json",
    "--processed-json",
    "--handoff-tar",
    "--extra-arg",
)

ADMISSION_BLOCKER = "AcademicDeploymentAuthorizationMissing"

# The static stage closure the deployment execution map is built from. The two
# stages sit on two different storage planes on purpose: reference data is
# public and read-only, the licensed parameters are tenant-private, and no stage
# holds both.
STAGE_EXECUTION_CONTRACTS: Mapping[str, StageExecutionContract] = {
    DATA_STAGE_ID: StageExecutionContract(
        "alphafold3-data-collector-v1", "alphafold3-data-validator-v1", (REFERENCE_ARTIFACT,)
    ),
    INFERENCE_STAGE_ID: StageExecutionContract(
        "alphafold3-result-collector-v1", VALIDATOR_ID, (PARAMETERS_ARTIFACT,)
    ),
}

# The CPU stage writes its fold input, its data-pipeline output and the packaged
# handoff inside its own workspace. Nothing addresses an absolute /output or
# /handoff path: the runtime records only paths relative to the handoff
# directory, so the GPU pod reconstructs them under its own artifact mount.
FOLD_INPUT_FILENAME = "fold_input.json"
DATA_OUTPUT_DIR = "data-output"
HANDOFF_DIR_NAME = "fs2-af3-handoff"
HANDOFF_INDEX = "index.json"
HANDOFF_SCHEMA = "fs2-serve.nebius.ai/alphafold3-data-handoff/v1"
HANDOFF_PACKAGE = "handoff.tar"
HANDOFF_MOUNT_DIR = "handoff"

# The runtime processes one fold job per GPU run and requires --fold-job to
# select one once the handoff holds more than one. Selecting per run means one
# GPU work unit per fold job, and an execution plan admits exactly one terminal
# invocation, so a multi-job run is refused here rather than compiled into a
# stage the runtime would reject as ambiguous.
MAX_FOLD_JOBS_PER_RUN = 1

# The seed fan-out and sample count live in the fold input JSON, not in argv, so
# the collector reads them back from controller-owned stage environment rather
# than parsing flags the runtime does not accept.
SEEDS_ENV = "FS2_AF3_MODEL_SEEDS"
SAMPLES_ENV = "FS2_AF3_NUM_DIFFUSION_SAMPLES"
FOLD_JOBS_ENV = "FS2_AF3_FOLD_JOB_COUNT"

# Deployment-bound academic admission. The platform owner grants this once, for
# the deployment, and it is recorded in the profile the operator publishes. It
# is never a per-request or per-input field: an ordinary caller submits an
# ordinary request carrying no licence receipt at all.
_REQUIRED_DEPLOYMENT_ACCESS = {
    "profile": "academic",
    "credentials_embedded": False,
    "materialization": "restricted-quarantine-poc-authorized",
    "operational_activation": "user-authorized-academic-poc",
    "license_gate_scope": "production-promotion-only",
    "receipt_digest": None,
}


@dataclass(frozen=True, slots=True)
class ReferenceBinding:
    """The published reference identities a CPU stage is allowed to bind.

    Every path is derived from the publisher's own layout rather than assembled
    by hand, and the two digests are kept apart on purpose: ``tree_sha256``
    names the expanded database tree, ``manifest_sha256`` names the document
    that describes it.
    """

    bundle_id: str
    revision: str
    tree_sha256: str
    manifest_sha256: str

    @property
    def dataset_sub_path(self) -> str:
        return f"datasets/{self.bundle_id}/{self.revision}/sha256/{self.tree_sha256}"

    @property
    def database_root(self) -> str:
        return f"{REFERENCE_MOUNT_PATH}/{self.dataset_sub_path}"

    @property
    def marker_path(self) -> str:
        return f"{self.database_root}/{REFERENCE_MANIFEST_MARKER}"

    @property
    def manifest_path(self) -> str:
        return f"{REFERENCE_MOUNT_PATH}/manifests/sha256/{self.manifest_sha256}.json"

    @property
    def receipt_path(self) -> str:
        return f"{REFERENCE_MOUNT_PATH}/receipts/{self.bundle_id}/{self.revision}.json"

    def assert_single_root(self) -> None:
        """Prove every document the stage needs sits under one mounted root.

        This is the property that makes a subPath mount unusable, so it is
        asserted here rather than left as a comment.
        """
        root = PurePosixPath(REFERENCE_MOUNT_PATH)
        for path in (self.database_root, self.marker_path, self.manifest_path, self.receipt_path):
            resolved = PurePosixPath(path)
            if root not in resolved.parents or ".." in resolved.parts:
                raise ScientificAdapterError(
                    f"reference path {path} does not resolve under the read-only {REFERENCE_MOUNT_PATH} root"
                )


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ScientificAdapterError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _reference_binding(requirement: Mapping[str, object]) -> ReferenceBinding:
    """Bind the promoted reference bundle recorded in the deployment profile.

    The publisher's terminal receipt is the source of these identities. It is
    read by the runtime from the mounted root; the adapter re-derives the same
    paths from the promoted digests so a mismatch fails here, before a Job
    exists, instead of inside the container.
    """
    source = requirement.get("source")
    if not isinstance(source, Mapping) or source.get("release_id") != REFERENCE_REVISION:
        raise ScientificAdapterError("AlphaFold 3 reference bundle is not the qualified published revision")
    tree_sha256 = _sha256(requirement.get("content_digest_sha256"), "reference tree digest")
    manifest_sha256 = _sha256(requirement.get("localization_manifest_sha256"), "reference manifest digest")
    if tree_sha256 == manifest_sha256:
        raise ScientificAdapterError(
            "the reference tree digest and the manifest digest identify different objects "
            "and must never be equated"
        )
    binding = ReferenceBinding(
        bundle_id=REFERENCE_ARTIFACT,
        revision=REFERENCE_REVISION,
        tree_sha256=tree_sha256,
        manifest_sha256=manifest_sha256,
    )
    binding.assert_single_root()
    return binding


def _assert_deployment_authorization(profile: Mapping[str, object]) -> None:
    """Fail closed unless the deployment carries the granted academic authorization.

    The gate is deliberately on operator-owned deployment metadata. A caller
    cannot supply, forge or waive it, and a caller is never asked for a licence
    receipt to run an ordinary request.
    """
    access = profile.get("access")
    if not isinstance(access, Mapping):
        raise ScientificAdapterError(ADMISSION_BLOCKER)
    for field, expected in _REQUIRED_DEPLOYMENT_ACCESS.items():
        if access.get(field) != expected:
            raise ScientificAdapterError(ADMISSION_BLOCKER)


def _cpu_envelope(profile: Mapping[str, object]) -> int:
    """The whole CPU count the deployment gives the preprocessing stage.

    AlphaFold 3 derives its MSA thread default from the *node*, not the pod, so
    an unset thread count oversubscribes the cgroup. The frozen count therefore
    comes from the stage's own declared CPU request and the runtime rejects a
    count above it.
    """
    workload = profile.get("workload")
    if not isinstance(workload, Mapping):
        raise ScientificAdapterError("profile workload is missing")
    stages = workload.get("stages")
    if not isinstance(stages, list):
        raise ScientificAdapterError("profile workload stages are missing")
    for raw_stage in stages:
        if not isinstance(raw_stage, Mapping) or raw_stage.get("id") != DATA_STAGE_ID:
            continue
        resources = raw_stage.get("resources")
        if not isinstance(resources, Mapping):
            raise ScientificAdapterError("AlphaFold 3 data stage declares no resources")
        millis = bounded_int(
            resources.get("cpu_millis"), minimum=1000, maximum=128_000, label="data stage CPU request"
        )
        if millis % 1000:
            raise ScientificAdapterError(
                "the AlphaFold 3 data stage must request whole CPUs so the frozen MSA thread "
                "count cannot oversubscribe a fractional envelope"
            )
        return millis // 1000
    raise ScientificAdapterError(f"profile declares no {DATA_STAGE_ID} stage")


@dataclass(frozen=True, slots=True)
class Parameters:
    input_mode: str
    seeds: tuple[int, ...]
    samples: int
    raw_input_sha256: str | None
    fold_jobs: int

    @classmethod
    def parse(cls, value: object) -> Parameters:
        item = strict_object(
            value,
            required=frozenset({"input_mode", "model_seeds", "num_diffusion_samples"}),
            optional=frozenset({"raw_input_sha256", "fold_job_count"}),
            label="AlphaFold 3 parameters",
        )
        if item["input_mode"] not in {"raw", "enriched"}:
            raise ScientificAdapterError("input_mode must be raw or enriched")
        if item["input_mode"] == "enriched":
            # A single-stage plan drops data-pipeline, but the renderer requires
            # the plan's stages to equal the profile's stages in order, so such a
            # plan can never be rendered. Refusing here is truthful; silently
            # compiling an unrenderable plan is not.
            raise ScientificAdapterError(
                "enriched input needs its own single-stage profile; the canonical AlphaFold 3 "
                "profile declares both data-pipeline and inference and a plan must cover them"
            )
        raw = item["model_seeds"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 16:
            raise ScientificAdapterError("model_seeds must contain 1..16 values")
        seeds = tuple(bounded_int(seed, minimum=0, maximum=2**31 - 1, label="model seed") for seed in raw)
        if len(set(seeds)) != len(seeds):
            raise ScientificAdapterError("model_seeds must be unique")
        raw_input_digest = item.get("raw_input_sha256")
        if raw_input_digest is not None:
            _sha256(raw_input_digest, "raw_input_sha256")
        fold_jobs = bounded_int(
            item.get("fold_job_count", 1), minimum=1, maximum=64, label="fold_job_count"
        )
        if fold_jobs > MAX_FOLD_JOBS_PER_RUN:
            raise ScientificAdapterError(
                "a run carries one fold job. The runtime selects a single job per GPU "
                "invocation with --fold-job and rejects an ambiguous handoff, so several "
                "fold jobs need one run each until the terminal stage can fan out"
            )
        return cls(
            str(item["input_mode"]),
            seeds,
            bounded_int(item["num_diffusion_samples"], minimum=1, maximum=16, label="num_diffusion_samples"),
            raw_input_digest if isinstance(raw_input_digest, str) else None,
            fold_jobs,
        )


def _assert_argv_is_clean(argv: tuple[str, ...]) -> None:
    """Refuse a retired command surface even if a template is edited later."""
    for token in FORBIDDEN_ARGV_TOKENS:
        if any(token == value or value.startswith(f"{token}=") for value in argv):
            raise ScientificAdapterError(f"AlphaFold 3 argv must not carry the retired {token} surface")


def compile_run(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
    variant_id: str,
    access_context: ArtifactAccessContext,
    input_artifacts: tuple[ScientificInputArtifact, ...],
) -> AdapterExecutionPlan:
    del access_context  # Admission is deployment-bound, never per-request.
    request = parse_public_request(request_value, maximum_input_bytes=64 * 1024 * 1024)
    parameters = Parameters.parse(request.parameters)
    input_artifact = bind_compiler_input(
        request,
        variant_id=variant_id,
        expected_variant_id=VARIANT_ID,
        input_artifacts=input_artifacts,
        maximum_bytes=64 * 1024 * 1024,
        allowed_media_types=frozenset({"application/json"}),
        allowed_compressions=frozenset({None, "none"}),
    )
    assert_profile_identity(
        profile,
        model_id=MODEL_ID,
        variant_id=VARIANT_ID,
        repository=SOURCE_REPOSITORY,
        revision=SOURCE_REVISION,
        parameter_schema=PARAMETER_SCHEMA,
        request=request,
    )
    assert_artifact_requirement(
        profile,
        artifact_id=PARAMETERS_ARTIFACT,
        content_sha256=PARAMETERS_SHA256,
        required_file="af3.bin.zst",
    )
    reference_requirement = assert_artifact_requirement(
        profile, artifact_id=REFERENCE_ARTIFACT, content_sha256=None
    )
    _assert_deployment_authorization(profile)
    reference = _reference_binding(reference_requirement)
    cpu_request = _cpu_envelope(profile)

    data_root = run_workspace(MODEL_ID, operation_id, "data-pipeline-main")
    inference_root = run_workspace(MODEL_ID, operation_id, "inference-main")
    processed = logical_stage_artifact(operation_id, DATA_STAGE_ID, "main")
    result = logical_stage_artifact(operation_id, INFERENCE_STAGE_ID, "main")

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
        reference.receipt_path,
        # --database-root is deliberately not sent. The runtime derives it from
        # the receipt, and until the runtime successor is published only the
        # contract's required arguments are safe to depend on. The adapter still
        # derives the root itself so a wrong promotion fails before a Job runs.
        "--threads",
        str(cpu_request),
        "--cpu-request",
        str(cpu_request),
        LOCALIZATION_MARKER_FLAG,
        f"{data_root}/.fs2/runtime-localization.json",
    )
    _assert_argv_is_clean(data_argv)
    data = StageInvocation(
        stage_id=DATA_STAGE_ID,
        shard_id="main",
        argv=data_argv,
        environment=(("FS2_NETWORK_MODE", "offline"),),
        working_directory=data_root,
        consumes=(input_artifact.logical_artifact_id,),
        produces=processed,
        collector_id="alphafold3-data-collector-v1",
        validator_id="alphafold3-data-validator-v1",
        handoff_name="processed-input",
        max_output_artifacts=1,
        max_output_bytes=64 * 1024 * 1024,
        materializations=(
            ArtifactMaterialization(
                input_artifact.logical_artifact_id, data_input, MaterializationMode.COPY_FILE
            ),
        ),
        runtime_artifacts=(REFERENCE_ARTIFACT,),
        runtime_mounts=(
            # No sub_path: the dataset, its marker, the manifest and the receipt
            # are siblings under this one root and all four must resolve.
            RuntimeArtifactMount(
                artifact_id=REFERENCE_ARTIFACT,
                mount_path=REFERENCE_MOUNT_PATH,
                sub_path=None,
                read_only=True,
                expected_content_sha256=reference.tree_sha256,
                expected_manifest_sha256=reference.manifest_sha256,
            ),
        ),
    )

    # The handoff directory the CPU stage packaged, reconstructed under this
    # pod's own workspace. Never an absolute path recorded by the CPU pod.
    inference_input = f"{inference_root}/{HANDOFF_MOUNT_DIR}"
    inference_output = f"{inference_root}/outputs"
    inference_argv = (
        *RUNTIME_COMMAND,
        "inference",
        "--handoff-dir",
        inference_input,
        "--output-dir",
        inference_output,
        LOCALIZATION_MARKER_FLAG,
        f"{inference_root}/.fs2/runtime-localization.json",
    )
    _assert_argv_is_clean(inference_argv)
    inference = StageInvocation(
        stage_id=INFERENCE_STAGE_ID,
        shard_id="main",
        argv=inference_argv,
        environment=(
            ("FS2_NETWORK_MODE", "offline"),
            ("HF_HUB_OFFLINE", "1"),
            ("TRANSFORMERS_OFFLINE", "1"),
            (SEEDS_ENV, ",".join(str(seed) for seed in parameters.seeds)),
            (SAMPLES_ENV, str(parameters.samples)),
            (FOLD_JOBS_ENV, str(parameters.fold_jobs)),
        ),
        working_directory=inference_root,
        consumes=(processed,),
        produces=result,
        collector_id="alphafold3-result-collector-v1",
        validator_id=VALIDATOR_ID,
        handoff_name=None,
        max_output_artifacts=parameters.fold_jobs * len(parameters.seeds) * parameters.samples + 1,
        max_output_bytes=8 * 1024 * 1024 * 1024,
        materializations=(
            ArtifactMaterialization(processed, inference_input, MaterializationMode.EXTRACT_TAR),
        ),
        # No reference-data artifact here. The GPU stage consumes the immutable
        # CPU handoff and the licensed parameters, and the runtime refuses a
        # stage that can see a reference database.
        runtime_artifacts=(PARAMETERS_ARTIFACT,),
        runtime_mounts=(
            RuntimeArtifactMount(
                artifact_id=PARAMETERS_ARTIFACT,
                mount_path=PARAMETER_MOUNT_PATH,
                sub_path="alphafold3/af3.bin.zst",
                read_only=True,
                expected_content_sha256=PARAMETERS_SHA256,
            ),
        ),
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


def collect_data(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    """Package and verify the handoff directory the data stage wrote.

    The runtime writes ``<output_dir>/fs2-af3-handoff`` holding one data JSON per
    fold job and an ``index.json`` recording each entry by a path relative to
    that directory, a byte count and a SHA-256. Those relative paths are what
    make the handoff portable, so the package keeps them relative and the GPU
    stage reconstructs them under its own mount.
    """
    root = workspace.resolve()
    handoff = (workspace / DATA_OUTPUT_DIR / HANDOFF_DIR_NAME).resolve()
    index_path = handoff / HANDOFF_INDEX
    if root not in handoff.parents or not handoff.is_dir() or handoff.is_symlink():
        raise CollectionPendingError(f"data handoff directory is not available yet: {HANDOFF_DIR_NAME}")
    if not index_path.is_file() or index_path.is_symlink():
        raise CollectionPendingError(f"data handoff index is not available yet: {HANDOFF_INDEX}")
    if invocation.handoff_name is None:
        raise ScientificAdapterError("consumed stage output requires a handoff name")

    index = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(index, dict) or index.get("schema") != HANDOFF_SCHEMA:
        raise ScientificAdapterError(f"data handoff index is not a {HANDOFF_SCHEMA} document")
    entries = index.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ScientificAdapterError("data handoff index records no fold job")
    if len(entries) > MAX_FOLD_JOBS_PER_RUN:
        raise ScientificAdapterError(
            "data handoff holds more than one fold job, which the GPU stage could only "
            "run by selecting one with --fold-job"
        )

    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ScientificAdapterError("data handoff entry is not an object")
        relative = entry.get("path")
        expected_digest = entry.get("sha256")
        expected_bytes = entry.get("bytes")
        if not isinstance(relative, str) or not relative:
            raise ScientificAdapterError("data handoff entry records no relative path")
        parts = PurePosixPath(relative).parts
        if PurePosixPath(relative).is_absolute() or any(part in {"", ".", ".."} for part in parts):
            raise ScientificAdapterError(
                f"data handoff entry path {relative} is not contained and relative"
            )
        if relative in seen:
            raise ScientificAdapterError(f"data handoff repeats the entry {relative}")
        seen.add(relative)
        payload = (handoff / relative).resolve()
        if handoff not in payload.parents or not payload.is_file() or payload.is_symlink():
            raise ScientificAdapterError(f"data handoff entry {relative} is not a contained regular file")
        content = payload.read_bytes()
        if not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool) or expected_bytes != len(content):
            raise ScientificAdapterError(f"data handoff entry {relative} does not match its recorded size")
        if hashlib.sha256(content).hexdigest() != expected_digest:
            raise ScientificAdapterError(f"data handoff entry {relative} does not match its recorded digest")

    # Written to a temporary name and moved, so a partially written package is
    # never collectable.
    package = workspace / HANDOFF_PACKAGE
    pending = workspace / f".{HANDOFF_PACKAGE}.partial"
    with tarfile.open(pending, "w", format=tarfile.PAX_FORMAT) as archive:
        archive.add(handoff, arcname=".", recursive=True)
    pending.replace(package)
    return CollectedStageOutput(
        artifacts=(
            CollectedArtifactFile(
                name=invocation.handoff_name,
                semantic_type="alphafold3-data-handoff/v1",
                path=package,
                media_type="application/x-tar",
                compression=None,
            ),
        ),
        validation={
            "validator_id": invocation.validator_id,
            "status": "passed",
            "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
            "fold_jobs": len(entries),
        },
    )


def collect_result(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    environment = dict(invocation.environment)
    try:
        seeds = tuple(int(value) for value in environment[SEEDS_ENV].split(","))
        samples = int(environment[SAMPLES_ENV])
        fold_jobs = int(environment[FOLD_JOBS_ENV])
    except (KeyError, ValueError) as error:
        raise ScientificAdapterError(
            "AlphaFold 3 inference stage carries no frozen seed fan-out, so its outputs "
            "cannot be accounted for"
        ) from error
    return collect_confidence_envelope(
        invocation,
        workspace,
        expected_runtime_id=MODEL_ID,
        expected_model_revision=SOURCE_REVISION,
        expected_seeds=seeds,
        # Every fold job in the package runs the same seed fan-out, so each seed
        # accounts for one sample set per fold job.
        expected_samples_per_seed=samples * fold_jobs,
    )

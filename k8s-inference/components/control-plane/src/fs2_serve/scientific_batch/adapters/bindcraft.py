"""Native BindCraft v1.5.3 adapter with academic PyRosetta, on mixed planes.

The four trees this model reads do not share a plane. Three are public
generations and the licensed PyRosetta installed tree is tenant-private, so each
is named by its own contract identity -- archive provenance and extracted-tree
inventory kept as two separate digests -- and never by a single shared volume.
Treating them as one claim was the assumption the image gate rejected.

Admission is deployment-bound: the public request carries no licence receipt and
the public schema cannot express one, so only an absent or ungranted deployment
authorization rejects.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..catalog_adapter import ScientificStageExpansion
from ..models import (
    AdapterExecutionPlan,
    ArtifactMaterialization,
    MaterializationMode,
    RuntimeTreeBinding,
    StageInvocation,
)
from .common import (
    CollectedOutput,
    PublicRunRequest,
    ScientificAdapterError,
    assert_profile_identity,
    bounded_int,
    build_execution_plan,
    collect_output_files,
    finite_number,
    parse_public_request,
    protein_sequence,
    run_workspace,
    strict_object,
)

MODEL_ID = "bindcraft"
VARIANT_ID = "v1-5-3-pyrosetta-academic"
BACKEND_ID = "bindcraft-v1-5-3-pyrosetta-academic"
SOURCE_REPOSITORY = "martinpacesa/BindCraft"
SOURCE_REVISION = "7cd4ace1b7407adf66a50dfefa47de2270f5e4a9"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/bindcraft-v1-5-3-parameters/v1"

DESIGN_STAGE = "design"
AGGREGATE_STAGE = "aggregate"

# The reviewed outer entrypoint is the artifact gate. It verifies the AlphaFold2
# manifest and binds PyRosetta on every non-smoke command before it execs the
# wrapper, which is why even the CPU aggregation carries two of the four trees.
RUNTIME_GATE = ("python", "/opt/fs2/runtime_entrypoint.py")
BATCH_WRAPPER = "/opt/fs2/bin/bindcraft-batch"
SETTINGS_TEMPLATE = "/opt/bindcraft/settings_advanced/default_4stage_multimer.json"
SETTINGS_SHA256 = "4124733af9dff65fb23e6a5f52b2329fc0d7a4ce5c50b6df225422f77fe467d6"
FILTERS = "/opt/bindcraft/settings_filters/default_filters.json"
FILTERS_SHA256 = "4faeae2ed4a78b82ff8f9c3c763985ff0f0b97ebb9e10072d5d572424bb73206"

# Identities from catalog/runtime/contracts/scientific-artifact-localization.json.
# The archive digest says where the bytes came from and never qualifies a mount;
# the inventory digest is computed from the localized tree and is what a stage
# preflight verifies. A test pins every value below to that contract.
AF2_ARTIFACT = "alphafold2-params-bindcraft"
AF2_PATH = "/models/alphafold2"
AF2_ARCHIVE_SHA256 = "36d4b0220f3c735f3296d301152b738c9776d16981d054845a68a1370b26cfe3"
AF2_INVENTORY_SHA256 = "9e25d394b1a7296f7705a5be794c5e29b853beb967835db088069f7cc007aa4f"
AF2_ENTRY_COUNT = 17

MPNN_VANILLA_ARTIFACT = "colabdesign-mpnn-weights-vanilla"
MPNN_VANILLA_PATH = "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights"
MPNN_VANILLA_INVENTORY_SHA256 = "2602ff1e01c8bdfd5773334e5724fcf0bdfecb3963100f05ad67ad6a5824ee4f"

MPNN_SOLUBLE_ARTIFACT = "colabdesign-mpnn-weights-soluble"
MPNN_SOLUBLE_PATH = "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights_soluble"
MPNN_SOLUBLE_INVENTORY_SHA256 = "54da6672d5677ab27bea0939bbbc591f8877484175a182736ca79af045d0f146"

# Both ColabDesign trees are extracted from one upstream archive, so they share
# archive provenance and differ only in extracted-tree identity.
COLABDESIGN_ARCHIVE_SHA256 = "26c948e5e577c65d5b3e908cc11eece435eb0f05729b1e227926d671c463d37f"
MPNN_ENTRY_COUNT = 5

PYROSETTA_ARTIFACT = "bindcraft-pyrosetta-installed-tree"
PYROSETTA_PATH = "/opt/fs2/academic/pyrosetta-bindcraft/site-packages"
PYROSETTA_ARCHIVE_SHA256 = "4383d8d1a14fd3aff52983de936908791cc77bc6ac418e3bc53bb963a42c5242"
PYROSETTA_INVENTORY_SHA256 = "a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d"
PYROSETTA_ENTRY_COUNT = 8_697
# The only tenant-private tree of the four; the other three are public.
PYROSETTA_VISIBILITY = "tenant-private"

DESIGN_RUNTIME_ARTIFACTS = (
    AF2_ARTIFACT,
    MPNN_VANILLA_ARTIFACT,
    MPNN_SOLUBLE_ARTIFACT,
    PYROSETTA_ARTIFACT,
)
# The aggregate logic reads no tree, but the shared outer gate still verifies
# AlphaFold2 and binds PyRosetta, so two of four is the floor for that stage.
AGGREGATE_RUNTIME_ARTIFACTS = (AF2_ARTIFACT, PYROSETTA_ARTIFACT)

# Every bound below is the reviewed runtime's own, not the wider public one: a
# value the native document cannot express is work that can only fail on a GPU.
MAX_DESIGN_SHARDS = 32
MAX_DESIGNS_PER_SHARD = 10
EXECUTABLE_DESIGN_CEILING = MAX_DESIGN_SHARDS * MAX_DESIGNS_PER_SHARD
TRAJECTORY_BUDGET_PER_DESIGN = 20
MAX_TRAJECTORIES_PER_SHARD = 1000
MAX_SEED = 2**31 - 1
MIN_HOTSPOT_RESIDUE = 1
MAX_HOTSPOT_RESIDUE = 9_999
MAX_HOTSPOTS = 64
MIN_BINDER_LENGTH = 40
MAX_BINDER_LENGTH = 200
MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024 * 1024 * 1024

MPNN_LANES: Final[Mapping[str, str]] = {"vanilla": MPNN_VANILLA_ARTIFACT, "soluble": MPNN_SOLUBLE_ARTIFACT}
DEFAULT_MPNN_LANE = "vanilla"
# Upstream BindCraft's own setting value for each ColabDesign weight tree.
MPNN_WEIGHTS_SETTING: Final[Mapping[str, str]] = {"vanilla": "original", "soluble": "soluble"}


def tree_bindings() -> tuple[RuntimeTreeBinding, ...]:
    """Bind all four trees by contract identity, each on its own plane."""

    return (
        RuntimeTreeBinding(
            artifact_id=AF2_ARTIFACT,
            mount_path=AF2_PATH,
            archive_sha256=AF2_ARCHIVE_SHA256,
            tree_inventory_sha256=AF2_INVENTORY_SHA256,
            entry_count=AF2_ENTRY_COUNT,
        ),
        RuntimeTreeBinding(
            artifact_id=MPNN_VANILLA_ARTIFACT,
            mount_path=MPNN_VANILLA_PATH,
            archive_sha256=COLABDESIGN_ARCHIVE_SHA256,
            tree_inventory_sha256=MPNN_VANILLA_INVENTORY_SHA256,
            entry_count=MPNN_ENTRY_COUNT,
        ),
        RuntimeTreeBinding(
            artifact_id=MPNN_SOLUBLE_ARTIFACT,
            mount_path=MPNN_SOLUBLE_PATH,
            archive_sha256=COLABDESIGN_ARCHIVE_SHA256,
            tree_inventory_sha256=MPNN_SOLUBLE_INVENTORY_SHA256,
            entry_count=MPNN_ENTRY_COUNT,
        ),
        RuntimeTreeBinding(
            artifact_id=PYROSETTA_ARTIFACT,
            mount_path=PYROSETTA_PATH,
            archive_sha256=PYROSETTA_ARCHIVE_SHA256,
            tree_inventory_sha256=PYROSETTA_INVENTORY_SHA256,
            entry_count=PYROSETTA_ENTRY_COUNT,
        ),
    )


def _bindings_for(artifacts: tuple[str, ...]) -> tuple[RuntimeTreeBinding, ...]:
    return tuple(item for item in tree_bindings() if item.artifact_id in artifacts)


@dataclass(frozen=True, slots=True)
class BindCraftParameters:
    """Bounded caller parameters projected from the public parameter schema."""

    target_chain: str
    hotspot_residues: tuple[int, ...]
    minimum_length: int
    maximum_length: int
    designs: int
    mpnn_lane: str
    base_seed: int

    @classmethod
    def parse(cls, value: object) -> BindCraftParameters:
        item = strict_object(
            value,
            required=frozenset({"target", "binder_length", "designs"}),
            optional=frozenset({"mpnn_lane", "seed"}),
            label="BindCraft parameters",
        )
        target = strict_object(
            item["target"],
            required=frozenset({"chain", "hotspot_residues"}),
            label="BindCraft target",
        )
        chain = target["chain"]
        if not isinstance(chain, str) or len(chain) != 1 or not chain.isalnum():
            raise ScientificAdapterError(
                "BindCraft target chain must be one alphanumeric character; the runtime's chain identifier is "
                "a single PDB chain column"
            )
        raw_hotspots = target["hotspot_residues"]
        if not isinstance(raw_hotspots, list) or not 1 <= len(raw_hotspots) <= MAX_HOTSPOTS:
            raise ScientificAdapterError(f"BindCraft requires 1..{MAX_HOTSPOTS} hotspot residues")
        hotspots = tuple(
            bounded_int(
                residue,
                minimum=MIN_HOTSPOT_RESIDUE,
                maximum=MAX_HOTSPOT_RESIDUE,
                label="BindCraft hotspot residue",
            )
            for residue in raw_hotspots
        )
        if len(set(hotspots)) != len(hotspots):
            raise ScientificAdapterError("BindCraft hotspot residues must be unique")
        length = strict_object(
            item["binder_length"],
            required=frozenset({"minimum", "maximum"}),
            label="BindCraft binder_length",
        )
        minimum = bounded_int(
            length["minimum"], minimum=MIN_BINDER_LENGTH, maximum=MAX_BINDER_LENGTH, label="binder_length.minimum"
        )
        maximum = bounded_int(
            length["maximum"], minimum=MIN_BINDER_LENGTH, maximum=MAX_BINDER_LENGTH, label="binder_length.maximum"
        )
        if minimum > maximum:
            raise ScientificAdapterError("BindCraft binder_length minimum exceeds maximum")
        designs = bounded_int(item["designs"], minimum=1, maximum=1024, label="designs")
        if designs > EXECUTABLE_DESIGN_CEILING:
            # Refuse rather than truncate. The public schema admits more than the
            # profile's parallelism and the native per-shard cap leave
            # executable, and silently dropping designs is the worse failure.
            raise ScientificAdapterError(
                f"BindCraft can execute at most {EXECUTABLE_DESIGN_CEILING} designs "
                f"({MAX_DESIGN_SHARDS} shards x {MAX_DESIGNS_PER_SHARD} accepted designs per shard); "
                f"{designs} were requested"
            )
        lane = item.get("mpnn_lane", DEFAULT_MPNN_LANE)
        if not isinstance(lane, str) or lane not in MPNN_LANES:
            raise ScientificAdapterError("BindCraft mpnn_lane must be vanilla or soluble")
        base_seed = bounded_int(item.get("seed", 0), minimum=0, maximum=MAX_SEED, label="seed")
        return cls(chain, tuple(sorted(hotspots)), minimum, maximum, designs, lane, base_seed)

    @property
    def shard_count(self) -> int:
        return min(self.designs, MAX_DESIGN_SHARDS)

    @property
    def shard_ids(self) -> tuple[str, ...]:
        return tuple(f"design-{index:03d}" for index in range(self.shard_count))

    def accepted_designs(self, index: int) -> int:
        """Spread the requested designs over the shards without losing any."""

        base, remainder = divmod(self.designs, self.shard_count)
        return base + (1 if index < remainder else 0)

    def max_trajectories(self, index: int) -> int:
        return min(MAX_TRAJECTORIES_PER_SHARD, self.accepted_designs(index) * TRAJECTORY_BUDGET_PER_DESIGN)

    def seed(self, index: int) -> int:
        seed = self.base_seed + index
        if seed > MAX_SEED:
            raise ScientificAdapterError("BindCraft deterministic shard seeds overflow the runtime's seed bound")
        return seed


def _request(value: object) -> tuple[PublicRunRequest, BindCraftParameters]:
    request = parse_public_request(value, maximum_input_bytes=MAX_INPUT_BYTES)
    return request, BindCraftParameters.parse(request.parameters)


def assert_deployment_authorized(profile: Mapping[str, object]) -> Mapping[str, object]:
    """Fail closed unless the deployment itself carries the academic grant.

    The grant is deployment state. An ordinary request carries no licence
    receipt and none can be expressed, so only its absence rejects.
    """

    access = profile.get("access")
    if not isinstance(access, Mapping) or access.get("profile") != "academic":
        raise ScientificAdapterError("BindCraft requires the academic access profile")
    if access.get("request_time_license_receipt_required") is not False:
        raise ScientificAdapterError("BindCraft must not require a per-request licence receipt")
    authorization = access.get("authorization")
    if not isinstance(authorization, Mapping):
        raise ScientificAdapterError("BindCraft deployment academic authorization is absent")
    if authorization.get("use_authorization_status") != "Granted":
        raise ScientificAdapterError("BindCraft academic use authorization is not Granted")
    if authorization.get("execution_authorization_status") != "Authorized":
        raise ScientificAdapterError("BindCraft academic execution authorization is not Authorized")
    return authorization


def _environment(parameters: BindCraftParameters, accepted_designs: int) -> tuple[tuple[str, str], ...]:
    """Return the stage environment, naming each localized tree exactly once.

    A bound tree has to be reachable through some stage's argv or environment,
    and PYTHONPATH is a concatenation rather than a path, so every tree also
    gets a variable whose whole value is its mount path.
    """

    return (
        ("FS2_ARTIFACT_ROOT", AF2_PATH),
        ("FS2_BINDCRAFT_ACCEPTED_DESIGNS", str(accepted_designs)),
        ("FS2_BINDCRAFT_BINDER_LENGTH_MAX", str(parameters.maximum_length)),
        ("FS2_BINDCRAFT_BINDER_LENGTH_MIN", str(parameters.minimum_length)),
        ("FS2_BINDCRAFT_MPNN_SOLUBLE_TREE", MPNN_SOLUBLE_PATH),
        ("FS2_BINDCRAFT_MPNN_VANILLA_TREE", MPNN_VANILLA_PATH),
        ("FS2_BINDCRAFT_MPNN_WEIGHTS", MPNN_WEIGHTS_SETTING[parameters.mpnn_lane]),
        ("FS2_BINDCRAFT_PYROSETTA_TREE", PYROSETTA_PATH),
        ("FS2_SOURCE_REVISION", SOURCE_REVISION),
        # The gate rejects a PYTHONPATH that does not start with the licensed
        # tree, so the order here is contract, not preference.
        ("PYTHONPATH", f"{PYROSETTA_PATH}:/opt/bindcraft"),
    )


def _design_argv(workspace: str, index: int, seed: int) -> tuple[str, ...]:
    return (
        *RUNTIME_GATE,
        BATCH_WRAPPER,
        "run-trajectory",
        "--backend-id",
        BACKEND_ID,
        "--request",
        f"{workspace}/.fs2/request.json",
        "--input-manifest",
        f"{workspace}/.fs2/input-manifest.json",
        "--settings-template",
        SETTINGS_TEMPLATE,
        "--settings-sha256",
        SETTINGS_SHA256,
        "--filters",
        FILTERS,
        "--filters-sha256",
        FILTERS_SHA256,
        "--shard-index",
        str(index),
        "--seed",
        str(seed),
        "--pyrosetta-required",
        "--output",
        f"{workspace}/output",
        "--runtime-localization-marker",
        f"{workspace}/.fs2/runtime-localization.json",
    )


def _aggregate_argv(workspace: str, expected_shards: int) -> tuple[str, ...]:
    return (
        *RUNTIME_GATE,
        BATCH_WRAPPER,
        "aggregate",
        "--backend-id",
        BACKEND_ID,
        "--request",
        f"{workspace}/.fs2/request.json",
        "--input-manifest",
        f"{workspace}/.fs2/input-manifest.json",
        "--shards",
        f"{workspace}/shards",
        "--expected-shards",
        str(expected_shards),
        "--staging-manifest",
        f"{workspace}/output/output-manifest.json.tmp",
        "--output-manifest",
        f"{workspace}/output/output-manifest.json",
        "--atomic-rename",
        "--runtime-localization-marker",
        f"{workspace}/.fs2/runtime-localization.json",
    )


def compile_run(profile: Mapping[str, object], request_value: object, *, operation_id: str) -> AdapterExecutionPlan:
    """Compile one authorized academic request into an executable plan."""

    request, parameters = _request(request_value)
    assert_profile_identity(
        profile,
        model_id=MODEL_ID,
        repository=SOURCE_REPOSITORY,
        revision=SOURCE_REVISION,
        parameter_schema=PARAMETER_SCHEMA,
        request=request,
    )
    assert_deployment_authorized(profile)

    quotas = tuple(parameters.accepted_designs(index) for index in range(parameters.shard_count))
    if sum(quotas) != parameters.designs:
        raise ScientificAdapterError("BindCraft shard quotas do not account for every requested design")

    design_trees = _bindings_for(DESIGN_RUNTIME_ARTIFACTS)
    aggregate_trees = _bindings_for(AGGREGATE_RUNTIME_ARTIFACTS)
    expansions = {
        DESIGN_STAGE: ScientificStageExpansion(shard_ids=parameters.shard_ids),
        AGGREGATE_STAGE: ScientificStageExpansion(shard_ids=("main",), depends_on=(DESIGN_STAGE,)),
    }

    invocations: list[StageInvocation] = []
    design_outputs: list[str] = []
    for index, shard_id in enumerate(parameters.shard_ids):
        workspace = run_workspace(MODEL_ID, operation_id, shard_id)
        produces = f"bindcraft.{operation_id}.design.{shard_id}"
        design_outputs.append(produces)
        invocations.append(
            StageInvocation(
                stage_id=DESIGN_STAGE,
                shard_id=shard_id,
                argv=_design_argv(workspace, index, parameters.seed(index)),
                environment=_environment(parameters, parameters.accepted_designs(index)),
                working_directory=workspace,
                consumes=(request.input_manifest.artifact_id,),
                produces=produces,
                materializations=(
                    ArtifactMaterialization(
                        artifact_id=request.input_manifest.artifact_id,
                        destination=f"{workspace}/inputs/target_structure.pdb",
                        mode=MaterializationMode.COPY_FILE,
                        compression=request.input_manifest.compression,
                    ),
                ),
                runtime_artifacts=DESIGN_RUNTIME_ARTIFACTS,
                runtime_trees=design_trees,
            )
        )

    aggregate_workspace = run_workspace(MODEL_ID, operation_id, "aggregate")
    invocations.append(
        StageInvocation(
            stage_id=AGGREGATE_STAGE,
            shard_id="main",
            argv=_aggregate_argv(aggregate_workspace, len(design_outputs)),
            environment=_environment(parameters, parameters.designs),
            working_directory=aggregate_workspace,
            consumes=tuple(design_outputs),
            produces=f"bindcraft.{operation_id}.results",
            materializations=tuple(
                ArtifactMaterialization(
                    artifact_id=logical_id,
                    destination=f"{aggregate_workspace}/shards/{index:03d}",
                    mode=MaterializationMode.OVERLAY_TAR,
                    compression="zstd",
                )
                for index, logical_id in enumerate(design_outputs)
            ),
            runtime_artifacts=AGGREGATE_RUNTIME_ARTIFACTS,
            runtime_trees=aggregate_trees,
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
        required_model_artifacts=DESIGN_RUNTIME_ARTIFACTS,
    )


def collect_output(request_value: object, workspace: Path) -> CollectedOutput:
    """Collect one shard's accepted designs once the runtime publishes them."""

    _public, parameters = _request(request_value)
    artifacts = workspace / "output" / "artifacts"
    record = artifacts / "shard.json"
    if not record.is_file():
        raise ScientificAdapterError("BindCraft shard record has not been published yet")
    shard = _json_object(record, label="BindCraft shard record")
    if shard.get("status") != "succeeded" or shard.get("backend_id") != BACKEND_ID:
        raise ScientificAdapterError("BindCraft shard record does not report a succeeded run of this backend")
    if shard.get("source_revision") != SOURCE_REVISION:
        raise ScientificAdapterError("BindCraft shard record was produced by another source revision")

    metrics_files = sorted(artifacts.glob("candidate-*-metrics.json"))
    if not metrics_files:
        raise ScientificAdapterError("BindCraft shard published no accepted candidate")
    # The fourth element asks for upstream-CSV canonicalization; every BindCraft
    # output is JSON or PDB, so none of them wants it.
    entries: list[tuple[str, str, Path, bool]] = [("shard", "bindcraft-native-shard-result-json/v1", record, False)]
    seen: set[str] = set()
    for metrics_path in metrics_files:
        ordinal = metrics_path.name.removeprefix("candidate-").removesuffix("-metrics.json")
        if ordinal in seen:
            raise ScientificAdapterError("BindCraft shard published a duplicate candidate index")
        seen.add(ordinal)
        metrics = _json_object(metrics_path, label=f"BindCraft candidate {ordinal}")
        if metrics.get("scoring_engine") != "pyrosetta":
            raise ScientificAdapterError("BindCraft candidate was not scored by PyRosetta")
        sequence = protein_sequence(metrics.get("sequence"), label="BindCraft candidate sequence")
        if not parameters.minimum_length <= len(sequence) <= parameters.maximum_length:
            raise ScientificAdapterError("BindCraft candidate sequence violates the requested binder length")
        finite_number(metrics.get("iptm"), minimum=0.0, maximum=1.0, label="BindCraft iPTM")
        structure = artifacts / f"candidate-{ordinal}.pdb"
        relaxed = artifacts / f"candidate-{ordinal}-relaxed-complex.pdb"
        for path, label in ((structure, "structure"), (relaxed, "relaxed complex")):
            if not path.is_file() or not path.stat().st_size:
                raise ScientificAdapterError(f"BindCraft candidate has no {label}")
        entries.extend(
            (
                (f"candidate-{ordinal}-metrics", "bindcraft-native-design-metrics-json/v1", metrics_path, False),
                (f"candidate-{ordinal}-structure", "protein-structure-pdb/v1", structure, False),
                (
                    f"candidate-{ordinal}-relaxed-complex",
                    "bindcraft-native-relaxed-complex-pdb/v1",
                    relaxed,
                    False,
                ),
            )
        )
    index = shard.get("index")
    if not isinstance(index, int):
        raise ScientificAdapterError("BindCraft shard record carries no shard index")
    return collect_output_files(
        workspace,
        tuple(entries),
        manifest_id=f"bindcraft.shard.{index:03d}.results",
        maximum_total_bytes=MAX_OUTPUT_BYTES,
    )


def _json_object(path: Path, *, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ScientificAdapterError(f"{label} is not readable UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise ScientificAdapterError(f"{label} must be a JSON object")
    return value

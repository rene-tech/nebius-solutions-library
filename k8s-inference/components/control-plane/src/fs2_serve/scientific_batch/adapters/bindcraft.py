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

import hashlib
import io
import json
import os
import re
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final

import zstandard

from ..catalog_adapter import ScientificStageExpansion
from ..models import (
    AdapterExecutionPlan,
    ArtifactAccessContext,
    ArtifactMaterialization,
    MaterializationMode,
    RuntimeArtifactAdmissionRole,
    RuntimeArtifactAdmissionSpec,
    RuntimeArtifactMount,
    RuntimeTreeBinding,
    ScientificInputArtifact,
    StageInvocation,
    StageWorkspaceDocument,
)
from .common import (
    ARTIFACT_MANIFEST_SCHEMA,
    ArtifactPointer,
    CollectedOutput,
    LoadedArtifact,
    PublicRunRequest,
    ScientificAdapterError,
    assert_profile_identity,
    bounded_int,
    build_execution_plan,
    canonical_digest,
    collect_output_files,
    finite_number,
    parse_public_request,
    protein_sequence,
    run_workspace,
    strict_object,
    structure_atom_count,
)
from .staged_workspace import completion_marker, materialize_collected_output, wrap_stage_argv
from .verified_input import SCIENTIFIC_MANIFEST_MEDIA_TYPE, verified_manifest_entry

if TYPE_CHECKING:
    from . import CollectedStageOutput

MODEL_ID = "bindcraft"
VARIANT_ID = "v1-5-3-pyrosetta-academic"
BACKEND_ID = "bindcraft-v1-5-3-pyrosetta-academic"
SOURCE_REPOSITORY = "martinpacesa/BindCraft"
SOURCE_REVISION = "7cd4ace1b7407adf66a50dfefa47de2270f5e4a9"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/bindcraft-v1-5-3-parameters/v1"
NATIVE_PARAMETER_SCHEMA = "fs2-serve.nebius.ai/bindcraft-native-pyrosetta-parameters/v1"

ACADEMIC_ASSET_ID = "pyrosetta-bindcraft"
PYROSETTA_EXPECTED_VERSION = "2026.29+releasequarterly.80a0635615"

DESIGN_STAGE = "design"
AGGREGATE_STAGE = "aggregate"
DESIGN_COLLECTOR_ID = "bindcraft-design-output-v1"
AGGREGATE_COLLECTOR_ID = "bindcraft-aggregate-output-v1"
VALIDATOR_ID = "bindcraft-v1"
PUBLIC_REQUEST_DOCUMENT = ".fs2/public-request.json"
TARGET_INPUT_ID = "target_structure"
TARGET_SEMANTIC_TYPE = "protein-structure-pdb/v1"
TARGET_MEDIA_TYPE = "chemical/x-pdb"
NATIVE_INPUT_MANIFEST_ID = "manifest.bindcraft.controller-input"
REFERENCE_DATA_SUPPLEMENTAL_GROUP = 1000
ACADEMIC_ASSET_SUPPLEMENTAL_GROUP = 65532
REQUIRES_VERIFIED_INPUT_ARTIFACTS = True
REQUIRES_DEPLOYMENT_ACCESS_CONTEXT = True

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
MAX_MANIFEST_BYTES = 1 << 20
MAX_OUTPUT_BYTES = 32 * 1024 * 1024 * 1024
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_MEMBERS = 4_096
MAX_FINAL_ENTRIES = 1_024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_REFERENCE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENTRY_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_SEMANTIC_TYPE = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?/v[1-9][0-9]*$")
_CANDIDATE_NAME = re.compile(r"^candidate-([0-9]{3})-(metrics|structure|relaxed-complex)$")

MPNN_LANES: Final[Mapping[str, str]] = {"vanilla": MPNN_VANILLA_ARTIFACT, "soluble": MPNN_SOLUBLE_ARTIFACT}
DEFAULT_MPNN_LANE = "vanilla"
# Upstream BindCraft's own setting value for each ColabDesign weight tree.
MPNN_WEIGHTS_SETTING: Final[Mapping[str, str]] = {"vanilla": "original", "soluble": "soluble"}
EXTERNAL_TREE_ROLES: Final[Mapping[str, tuple[str, str, str]]] = {
    "alphafold2-params": (AF2_ARTIFACT, AF2_PATH, AF2_INVENTORY_SHA256),
    "colabdesign-mpnn-weights-vanilla": (
        MPNN_VANILLA_ARTIFACT,
        MPNN_VANILLA_PATH,
        MPNN_VANILLA_INVENTORY_SHA256,
    ),
    "colabdesign-mpnn-weights-soluble": (
        MPNN_SOLUBLE_ARTIFACT,
        MPNN_SOLUBLE_PATH,
        MPNN_SOLUBLE_INVENTORY_SHA256,
    ),
    "pyrosetta-site-packages": (PYROSETTA_ARTIFACT, PYROSETTA_PATH, PYROSETTA_INVENTORY_SHA256),
}


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


def _mounts_for(artifacts: tuple[str, ...]) -> tuple[RuntimeArtifactMount, ...]:
    roots = {artifact_id: root for _role, (artifact_id, root, _digest) in EXTERNAL_TREE_ROLES.items()}
    return tuple(
        RuntimeArtifactMount(
            artifact_id=artifact_id,
            mount_path=roots[artifact_id],
            supplemental_groups=(
                (ACADEMIC_ASSET_SUPPLEMENTAL_GROUP,)
                if artifact_id == PYROSETTA_ARTIFACT
                else (REFERENCE_DATA_SUPPLEMENTAL_GROUP,)
            ),
        )
        for artifact_id in artifacts
    )


def _admission_for(artifacts: tuple[str, ...]) -> RuntimeArtifactAdmissionSpec:
    by_artifact = {artifact_id: (role, root) for role, (artifact_id, root, _digest) in EXTERNAL_TREE_ROLES.items()}
    return RuntimeArtifactAdmissionSpec(
        schema="fs2.nebius.ai/bindcraft-external-tree-admission/v1",
        relative_path=".fs2/external-trees.json",
        roles=tuple(
            RuntimeArtifactAdmissionRole(
                role=by_artifact[artifact_id][0],
                artifact_id=artifact_id,
                mount_path=by_artifact[artifact_id][1],
                identity_field="inventory-digest",
            )
            for artifact_id in artifacts
        ),
    )


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
    request = parse_public_request(value, maximum_input_bytes=MAX_MANIFEST_BYTES)
    if request.input_manifest.media_type != SCIENTIFIC_MANIFEST_MEDIA_TYPE:
        raise ScientificAdapterError("BindCraft input_manifest must identify a scientific manifest")
    if request.input_manifest.compression not in {None, "none"}:
        raise ScientificAdapterError("BindCraft input manifest must not be compressed")
    return request, BindCraftParameters.parse(request.parameters)


def _canonical_bytes(value: object) -> bytes:
    """Use the exact canonical JSON form hashed by the r18 runtime."""

    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError) as error:
        raise ScientificAdapterError("BindCraft native document is not canonical JSON") from error


def _canonical_json(value: object) -> str:
    return _canonical_bytes(value).decode("ascii").removesuffix("\n")


def _workspace_documents(
    request: PublicRunRequest,
    parameters: BindCraftParameters,
    input_manifest: Mapping[str, object],
    *,
    shard_index: int,
) -> tuple[StageWorkspaceDocument, ...]:
    return (
        StageWorkspaceDocument(PUBLIC_REQUEST_DOCUMENT, _canonical_json(request.to_dict())),
        StageWorkspaceDocument(
            ".fs2/request.json",
            _canonical_json(native_request(request, parameters, input_manifest, shard_index=shard_index)),
        ),
        StageWorkspaceDocument(".fs2/input-manifest.json", _canonical_json(input_manifest)),
    )


def _load_public_request(workspace: Path) -> Mapping[str, object]:
    path = workspace.joinpath(*PurePosixPath(PUBLIC_REQUEST_DOCUMENT).parts)
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ScientificAdapterError("BindCraft collector request document is unavailable") from error
    try:
        value = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ScientificAdapterError("BindCraft collector request document is invalid") from error
    if not isinstance(value, Mapping) or _canonical_json(value).encode("ascii") != content:
        raise ScientificAdapterError("BindCraft collector request document is not canonical")
    return value


def native_input_manifest(target: ScientificInputArtifact) -> dict[str, object]:
    """Project the artifact-service-verified target into the manifest r18 consumes."""

    return {
        "schema": ARTIFACT_MANIFEST_SCHEMA,
        "manifest_id": NATIVE_INPUT_MANIFEST_ID,
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


def native_request(
    request: PublicRunRequest,
    parameters: BindCraftParameters,
    input_manifest: Mapping[str, object],
    *,
    shard_index: int,
) -> dict[str, object]:
    """Translate the public request into one exact r18 per-shard document.

    BindCraft's accepted-design and trajectory bounds are shard-local, so the
    translated document is deliberately attached to each invocation rather
    than pretending one run-level document can describe unequal shard quotas.
    The aggregate uses shard zero's document only as its immutable run identity;
    it does not execute the shard-local fields.
    """

    if not 0 <= shard_index < parameters.shard_count:
        raise ScientificAdapterError("BindCraft native request shard index is outside the plan")
    _native_manifest_target(input_manifest)
    manifest_bytes = _canonical_json(input_manifest).encode("ascii")
    value: dict[str, object] = {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": request.operation,
        "service_class": request.service_class.value,
        "input_manifest": {
            "artifact_id": input_manifest["manifest_id"],
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "size_bytes": len(manifest_bytes),
            "media_type": SCIENTIFIC_MANIFEST_MEDIA_TYPE,
            "compression": "none",
        },
        "parameters": {
            "schema": NATIVE_PARAMETER_SCHEMA,
            "shard_count": parameters.shard_count,
            "base_seed": parameters.base_seed,
            "target_chains": [parameters.target_chain],
            "hotspots": [
                {"chain": parameters.target_chain, "residue": residue} for residue in parameters.hotspot_residues
            ],
            "binder_length": {
                "minimum": parameters.minimum_length,
                "maximum": parameters.maximum_length,
            },
            "accepted_designs_per_shard": parameters.accepted_designs(shard_index),
            "max_trajectories_per_shard": parameters.max_trajectories(shard_index),
        },
    }
    if request.client_context is not None:
        value["client_context"] = dict(request.client_context)
    return value


def materialize_runtime_documents(invocation: StageInvocation, workspace: Path) -> tuple[Path, Path]:
    """Publish the canonical request pair into one attempt-local workspace.

    This is the executable controller boundary for the two JSON environment
    values. It is idempotent for identical bytes and refuses an existing file
    with different bytes, so a retry cannot silently execute another request.
    """

    environment = dict(invocation.environment)
    try:
        request_bytes = environment["FS2_BINDCRAFT_REQUEST_JSON"].encode("ascii")
        manifest_bytes = environment["FS2_BINDCRAFT_INPUT_MANIFEST_JSON"].encode("ascii")
    except (KeyError, UnicodeError) as error:
        raise ScientificAdapterError("BindCraft invocation has no canonical runtime documents") from error
    try:
        request_value = json.loads(request_bytes)
        manifest_value = json.loads(manifest_bytes)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ScientificAdapterError("BindCraft invocation runtime documents are unreadable") from error
    if (
        _canonical_json(request_value).encode("ascii") != request_bytes
        or _canonical_json(manifest_value).encode("ascii") != manifest_bytes
    ):
        raise ScientificAdapterError("BindCraft invocation runtime documents are not canonical")
    if not isinstance(request_value, Mapping) or not isinstance(manifest_value, Mapping):
        raise ScientificAdapterError("BindCraft invocation runtime documents must be JSON objects")
    _native_manifest_target(manifest_value)
    pointer = ArtifactPointer.parse(
        request_value.get("input_manifest"),
        label="BindCraft native input_manifest",
        maximum_bytes=1 << 20,
    )
    if (
        pointer.artifact_id != manifest_value.get("manifest_id")
        or pointer.sha256 != hashlib.sha256(manifest_bytes).hexdigest()
        or pointer.size_bytes != len(manifest_bytes)
        or pointer.media_type != SCIENTIFIC_MANIFEST_MEDIA_TYPE
        or pointer.compression not in {None, "none"}
    ):
        raise ScientificAdapterError("BindCraft native request does not bind its input manifest bytes")
    expected_request = f"{invocation.working_directory}/.fs2/request.json"
    expected_manifest = f"{invocation.working_directory}/.fs2/input-manifest.json"
    if expected_request not in invocation.argv or expected_manifest not in invocation.argv:
        raise ScientificAdapterError("BindCraft invocation argv does not consume its canonical runtime documents")

    root = workspace.resolve(strict=True)
    metadata = root / ".fs2"
    if metadata.exists() and (not metadata.is_dir() or metadata.is_symlink()):
        raise ScientificAdapterError("BindCraft runtime metadata root is not a regular directory")
    metadata.mkdir(mode=0o750, parents=False, exist_ok=True)

    def publish(name: str, content: bytes) -> Path:
        path = metadata / name
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise ScientificAdapterError(f"BindCraft runtime {name} already exists with different bytes")
            return path
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o444)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise ScientificAdapterError(f"BindCraft runtime {name} could not be published") from error
        return path

    return publish("request.json", request_bytes), publish("input-manifest.json", manifest_bytes)


def assert_deployment_authorized(
    profile: Mapping[str, object],
    *,
    access_context: ArtifactAccessContext | None = None,
) -> Mapping[str, object]:
    """Fail closed unless the deployment itself carries the academic grant.

    The grant is deployment state. An ordinary request carries no licence
    receipt and none can be expressed, so only its absence rejects.
    """

    access = profile.get("access")
    if not isinstance(access, Mapping) or access.get("profile") != "academic":
        raise ScientificAdapterError("BindCraft requires the academic access profile")
    if access.get("state") != "verified" or access.get("credentials_embedded") is not False:
        raise ScientificAdapterError("BindCraft deployment academic authorization is absent")

    # The public workload profile deliberately carries only the immutable
    # authorization receipt identity.  Tenant binding and the deployment
    # authorization handoff are resolved by ScientificExecutionMap before an
    # adapter runs; they must not be copied into a customer request or added as
    # schema-external profile fields.  Direct adapter callers from the legacy
    # qualification harness still exercise the older expanded profile below.
    if access_context is not None:
        receipt_digest = access.get("receipt_digest")
        if (
            not isinstance(receipt_digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", receipt_digest) is None
            or access_context.profile != "academic"
            or access_context.receipt_digest != f"sha256:{receipt_digest}"
            or access_context.tenant_id is None
        ):
            raise ScientificAdapterError("BindCraft deployment academic authorization is absent or mismatched")
        return {
            "authorization_id": access_context.receipt_digest,
            "tenant_id": access_context.tenant_id,
            "use_authorization_status": "Granted",
            "execution_authorization_status": "Authorized",
        }

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


def external_tree_roles() -> dict[str, object]:
    """Tell the controller which runtime role each verified mount satisfies."""

    return {
        "schema": "fs2-serve.nebius.ai/bindcraft-external-tree-roles/v1",
        "trees": [
            {"role": role, "artifact_id": artifact_id, "root": root}
            for role, (artifact_id, root, _digest) in sorted(EXTERNAL_TREE_ROLES.items())
        ],
    }


def _environment(
    request: PublicRunRequest,
    parameters: BindCraftParameters,
    input_manifest: Mapping[str, object],
    accepted_designs: int,
    *,
    shard_index: int,
    workspace: str,
    needs_target: bool,
    collector_id: str,
) -> tuple[tuple[str, str], ...]:
    """Return the stage environment and its controller-written documents.

    A bound tree has to be reachable through some stage's argv or environment,
    and PYTHONPATH is a concatenation rather than a path, so every tree also
    gets a variable whose whole value is its mount path. The two JSON values are
    already canonical bytes; the Kubernetes writer copies them byte-for-byte to
    the paths in argv before entering the image.
    """

    result = [
        ("FS2_ARTIFACT_ROOT", AF2_PATH),
        ("FS2_BINDCRAFT_ACCEPTED_DESIGNS", str(accepted_designs)),
        ("FS2_BINDCRAFT_BINDER_LENGTH_MAX", str(parameters.maximum_length)),
        ("FS2_BINDCRAFT_BINDER_LENGTH_MIN", str(parameters.minimum_length)),
        ("FS2_BINDCRAFT_EXTERNAL_TREE_ROLES", _canonical_bytes(external_tree_roles()).decode("ascii")),
        ("FS2_BINDCRAFT_EXTERNAL_TREES", f"{workspace}/.fs2/external-trees.json"),
        ("FS2_BINDCRAFT_INPUT_MANIFEST_JSON", _canonical_json(input_manifest)),
        ("FS2_BINDCRAFT_MPNN_SOLUBLE_TREE", MPNN_SOLUBLE_PATH),
        ("FS2_BINDCRAFT_MPNN_VANILLA_TREE", MPNN_VANILLA_PATH),
        ("FS2_BINDCRAFT_MPNN_WEIGHTS", MPNN_WEIGHTS_SETTING[parameters.mpnn_lane]),
        ("FS2_BINDCRAFT_PYROSETTA_TREE", PYROSETTA_PATH),
        (
            "FS2_BINDCRAFT_REQUEST_JSON",
            _canonical_json(native_request(request, parameters, input_manifest, shard_index=shard_index)),
        ),
        ("FS2_SCIENTIFIC_COLLECTOR_ID", collector_id),
        ("FS2_SCIENTIFIC_VALIDATOR_ID", VALIDATOR_ID),
        ("FS2_NETWORK_MODE", "offline"),
        ("FS2_SOURCE_REVISION", SOURCE_REVISION),
        # The gate rejects a PYTHONPATH that does not start with the licensed
        # tree, so the order here is contract, not preference.
        ("PYTHONPATH", f"{PYROSETTA_PATH}:/opt/bindcraft"),
    ]
    if needs_target:
        result.append(("FS2_BINDCRAFT_TARGET_PDB", f"{workspace}/inputs/target_structure.pdb"))
    return tuple(result)


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


def compile_run(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
    input_artifacts: tuple[ScientificInputArtifact, ...] | None = None,
    access_context: ArtifactAccessContext | None = None,
) -> AdapterExecutionPlan:
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
    assert_deployment_authorized(profile, access_context=access_context)
    target = verified_manifest_entry(
        request,
        input_artifacts,
        logical_artifact_id=TARGET_INPUT_ID,
        semantic_type=TARGET_SEMANTIC_TYPE,
        media_type=TARGET_MEDIA_TYPE,
        compressions=frozenset({None, "none"}),
        maximum_bytes=MAX_INPUT_BYTES,
        label="BindCraft",
    )
    input_manifest = native_input_manifest(target)

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
                argv=wrap_stage_argv(workspace, _design_argv(workspace, index, parameters.seed(index))),
                environment=_environment(
                    request,
                    parameters,
                    input_manifest,
                    parameters.accepted_designs(index),
                    shard_index=index,
                    workspace=workspace,
                    needs_target=True,
                    collector_id=DESIGN_COLLECTOR_ID,
                ),
                working_directory=workspace,
                consumes=(target.logical_artifact_id,),
                produces=produces,
                collector_id=DESIGN_COLLECTOR_ID,
                validator_id=VALIDATOR_ID,
                handoff_name=f"shard-{index:03d}-bundle",
                max_output_artifacts=1,
                max_output_bytes=MAX_BUNDLE_BYTES,
                materializations=(
                    ArtifactMaterialization(
                        artifact_id=target.logical_artifact_id,
                        destination=f"{workspace}/inputs/target_structure.pdb",
                        mode=MaterializationMode.COPY_FILE,
                        compression=target.compression,
                    ),
                ),
                runtime_artifacts=DESIGN_RUNTIME_ARTIFACTS,
                runtime_trees=design_trees,
                runtime_mounts=_mounts_for(DESIGN_RUNTIME_ARTIFACTS),
                workspace_documents=_workspace_documents(request, parameters, input_manifest, shard_index=index),
                runtime_admission=_admission_for(DESIGN_RUNTIME_ARTIFACTS),
            )
        )

    aggregate_workspace = run_workspace(MODEL_ID, operation_id, "aggregate")
    invocations.append(
        StageInvocation(
            stage_id=AGGREGATE_STAGE,
            shard_id="main",
            argv=wrap_stage_argv(
                aggregate_workspace,
                _aggregate_argv(aggregate_workspace, len(design_outputs)),
            ),
            environment=_environment(
                request,
                parameters,
                input_manifest,
                parameters.designs,
                shard_index=0,
                workspace=aggregate_workspace,
                needs_target=False,
                collector_id=AGGREGATE_COLLECTOR_ID,
            ),
            working_directory=aggregate_workspace,
            consumes=tuple(design_outputs),
            produces=f"bindcraft.{operation_id}.results",
            collector_id=AGGREGATE_COLLECTOR_ID,
            validator_id=VALIDATOR_ID,
            max_output_artifacts=MAX_FINAL_ENTRIES,
            max_output_bytes=MAX_OUTPUT_BYTES,
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
            runtime_mounts=_mounts_for(AGGREGATE_RUNTIME_ARTIFACTS),
            workspace_documents=_workspace_documents(request, parameters, input_manifest, shard_index=0),
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


def _json_object(path: Path, *, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ScientificAdapterError(f"{label} is not readable UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise ScientificAdapterError(f"{label} must be a JSON object")
    return value


def _entry(value: object, *, label: str, maximum_bytes: int) -> tuple[str, str, ArtifactPointer]:
    item = strict_object(
        value,
        required=frozenset({"name", "semantic_type", "artifact"}),
        label=label,
    )
    name = item["name"]
    semantic_type = item["semantic_type"]
    if not isinstance(name, str) or len(name) > 128 or _ENTRY_NAME.fullmatch(name) is None:
        raise ScientificAdapterError(f"{label}.name is invalid")
    if not isinstance(semantic_type, str) or _SEMANTIC_TYPE.fullmatch(semantic_type) is None:
        raise ScientificAdapterError(f"{label}.semantic_type is invalid")
    pointer = ArtifactPointer.parse(item["artifact"], label=f"{label}.artifact", maximum_bytes=maximum_bytes)
    return name, semantic_type, pointer


def _native_manifest_target(value: object) -> ArtifactPointer:
    """Validate the controller-written one-target manifest consumed by r18."""

    manifest = strict_object(
        value,
        required=frozenset({"schema", "manifest_id", "entries"}),
        label="BindCraft native input manifest",
    )
    if manifest["schema"] != ARTIFACT_MANIFEST_SCHEMA or manifest["manifest_id"] != NATIVE_INPUT_MANIFEST_ID:
        raise ScientificAdapterError("BindCraft native input manifest identity is unsupported")
    entries = manifest["entries"]
    if not isinstance(entries, list) or len(entries) != 1:
        raise ScientificAdapterError("BindCraft native input manifest must contain exactly one target")
    entry = strict_object(
        entries[0],
        required=frozenset({"name", "semantic_type", "artifact"}),
        label="BindCraft native target entry",
    )
    if entry["name"] != TARGET_INPUT_ID or entry["semantic_type"] != TARGET_SEMANTIC_TYPE:
        raise ScientificAdapterError("BindCraft native target entry identity is unsupported")
    pointer = ArtifactPointer.parse(
        entry["artifact"],
        label="BindCraft native target entry.artifact",
        maximum_bytes=MAX_INPUT_BYTES,
    )
    if pointer.media_type != TARGET_MEDIA_TYPE or pointer.compression not in {None, "none"}:
        raise ScientificAdapterError("BindCraft native target entry must be one uncompressed PDB")
    return pointer


def _contained_artifact(root: Path, relative: object, *, label: str) -> Path:
    if not isinstance(relative, str) or "\x00" in relative or "\\" in relative:
        raise ScientificAdapterError(f"{label} is not a safe relative path")
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ScientificAdapterError(f"{label} is not a safe relative path")
    resolved_root = root.resolve(strict=True)
    candidate = root.joinpath(*path.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ScientificAdapterError(f"{label} is unavailable") from error
    if resolved_root not in resolved.parents or not resolved.is_file() or candidate.is_symlink():
        raise ScientificAdapterError(f"{label} is not a contained regular file")
    return resolved


def _verified_content(path: Path, pointer: ArtifactPointer, *, label: str) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ScientificAdapterError(f"{label} is unreadable") from error
    if len(content) != pointer.size_bytes or hashlib.sha256(content).hexdigest() != pointer.sha256:
        raise ScientificAdapterError(f"{label} differs from its published content-addressed pointer")
    return content


def _validate_structure(name: str, pointer: ArtifactPointer, content: bytes, *, two_chains: bool) -> None:
    if pointer.media_type != "chemical/x-pdb" or pointer.compression not in {None, "none"}:
        raise ScientificAdapterError(f"BindCraft {name} is not an uncompressed PDB")
    structure_atom_count(
        LoadedArtifact(name=name, semantic_type="protein-structure-pdb/v1", pointer=pointer, content=content),
        require_two_chains=two_chains,
    )


def _validate_candidate_metrics(
    value: Mapping[str, Any],
    parameters: BindCraftParameters,
    *,
    shard_index: int,
    candidate_index: int,
) -> None:
    if value.get("candidate_id") != f"native-s{shard_index:03d}-c{candidate_index:03d}":
        raise ScientificAdapterError("BindCraft candidate identity disagrees with its shard and ordinal")
    if value.get("shard_index") != shard_index or value.get("scoring_engine") != "pyrosetta":
        raise ScientificAdapterError("BindCraft candidate was not scored by PyRosetta in its declared shard")
    if value.get("filter_set_sha256") != FILTERS_SHA256:
        raise ScientificAdapterError("BindCraft candidate did not pass the pinned production filter set")
    sequence = protein_sequence(value.get("sequence"), label="BindCraft candidate sequence")
    if not parameters.minimum_length <= len(sequence) <= parameters.maximum_length:
        raise ScientificAdapterError("BindCraft candidate sequence violates the requested binder length")
    finite_number(value.get("iptm"), minimum=0.0, maximum=1.0, label="BindCraft iPTM")
    interface_count = finite_number(
        value.get("interface_residue_count"),
        minimum=0.0,
        maximum=10_000.0,
        label="BindCraft average interface residue count",
    )
    if interface_count <= 0:
        raise ScientificAdapterError("BindCraft candidate has no interface residues")
    if (
        finite_number(
            value.get("buried_interface_area"),
            minimum=0.0,
            maximum=1_000_000.0,
            label="BindCraft buried interface area",
        )
        <= 0
    ):
        raise ScientificAdapterError("BindCraft candidate buries no interface area")
    if (
        finite_number(
            value.get("binder_energy_score"),
            minimum=-1_000_000.0,
            maximum=1_000_000.0,
            label="BindCraft binder energy score",
        )
        == 0
    ):
        raise ScientificAdapterError("BindCraft candidate has no PyRosetta energy score")
    geometry = strict_object(
        value.get("hotspot_geometry"),
        required=frozenset({"contact_cutoff_angstrom", "requested", "contacted"}),
        label="BindCraft hotspot geometry",
    )
    requested = geometry["requested"]
    if not isinstance(requested, list) or len(requested) != len(parameters.hotspot_residues):
        raise ScientificAdapterError("BindCraft hotspot geometry does not cover the requested site")
    observed: list[tuple[str, int]] = []
    for raw in requested:
        item = strict_object(
            raw,
            required=frozenset({"chain", "residue", "closest_binder_atom_angstrom", "in_contact"}),
            label="BindCraft hotspot contact",
        )
        chain = item["chain"]
        residue = item["residue"]
        if not isinstance(chain, str) or isinstance(residue, bool) or not isinstance(residue, int):
            raise ScientificAdapterError("BindCraft hotspot contact identity is invalid")
        finite_number(
            item["closest_binder_atom_angstrom"],
            minimum=0.0,
            maximum=1_000_000.0,
            label="BindCraft hotspot distance",
        )
        if not isinstance(item["in_contact"], bool):
            raise ScientificAdapterError("BindCraft hotspot contact flag is invalid")
        observed.append((chain, residue))
    expected = [(parameters.target_chain, residue) for residue in parameters.hotspot_residues]
    if observed != expected or geometry["contacted"] != sum(bool(item["in_contact"]) for item in requested):
        raise ScientificAdapterError("BindCraft hotspot geometry disagrees with the requested site")
    if not isinstance(geometry["contacted"], int) or geometry["contacted"] < 1:
        raise ScientificAdapterError("BindCraft candidate contacts none of the requested hotspots")


def _validate_external_tree_receipts(value: object) -> None:
    if not isinstance(value, Mapping) or value.get("schema") != "fs2.nebius.ai/bindcraft-external-tree-admission/v1":
        raise ScientificAdapterError("BindCraft shard has no verified external-tree admission")
    trees = value.get("trees")
    if not isinstance(trees, Mapping) or set(trees) != set(EXTERNAL_TREE_ROLES):
        raise ScientificAdapterError("BindCraft shard external-tree receipts do not cover all four roles")
    for role, (artifact_id, root, digest) in EXTERNAL_TREE_ROLES.items():
        receipt = trees[role]
        if not isinstance(receipt, Mapping) or receipt.get("artifact_id") != artifact_id or receipt.get("root") != root:
            raise ScientificAdapterError(f"BindCraft shard external-tree receipt for {role} has the wrong mount")
        identity_key = "tree_manifest_sha256" if role == "pyrosetta-site-packages" else "inventory_sha256"
        if receipt.get(identity_key) != digest:
            raise ScientificAdapterError(f"BindCraft shard external-tree receipt for {role} has the wrong identity")


def _validated_shard(
    request_value: object,
    workspace: Path,
) -> tuple[int, tuple[tuple[str, bytes], ...]]:
    """Validate one r18 shard and return exactly its handoff closure."""

    _public, parameters = _request(request_value)
    output = workspace / "output"
    descriptor_path = output / "shard-output.json"
    if not descriptor_path.is_file() or descriptor_path.is_symlink():
        raise ScientificAdapterError("BindCraft shard output has not been published yet")
    descriptor = _json_object(descriptor_path, label="BindCraft shard output")
    if descriptor.get("schema") != "fs2-serve.nebius.ai/bindcraft-native-shard-output/v1":
        raise ScientificAdapterError("BindCraft shard output schema is unsupported")
    raw_shard = descriptor.get("shard")
    raw_candidates = descriptor.get("candidates")
    raw_paths = descriptor.get("artifact_paths")
    if not isinstance(raw_candidates, list) or not isinstance(raw_paths, Mapping):
        raise ScientificAdapterError("BindCraft shard output has no candidate closure")
    shard_name, shard_semantic, shard_pointer = _entry(
        raw_shard,
        label="BindCraft shard entry",
        maximum_bytes=MAX_BUNDLE_BYTES,
    )
    if shard_semantic != "bindcraft-native-shard-result-json/v1" or shard_pointer.media_type != "application/json":
        raise ScientificAdapterError("BindCraft shard entry type is unsupported")

    parsed_entries = [
        (shard_name, shard_semantic, shard_pointer),
        *(
            _entry(raw, label=f"BindCraft candidate entry {index}", maximum_bytes=MAX_BUNDLE_BYTES)
            for index, raw in enumerate(raw_candidates)
        ),
    ]
    artifact_ids = [pointer.artifact_id for _name, _semantic, pointer in parsed_entries]
    if len(set(artifact_ids)) != len(artifact_ids) or set(raw_paths) != set(artifact_ids):
        raise ScientificAdapterError("BindCraft shard declarations and artifact path closure disagree")

    closure: list[tuple[str, bytes]] = [("shard-output.json", descriptor_path.read_bytes())]
    by_name: dict[str, tuple[str, ArtifactPointer, bytes]] = {}
    for name, semantic_type, pointer in parsed_entries:
        relative = raw_paths[pointer.artifact_id]
        path = _contained_artifact(output, relative, label=f"BindCraft artifact {pointer.artifact_id}")
        content = _verified_content(path, pointer, label=f"BindCraft artifact {pointer.artifact_id}")
        normalized = PurePosixPath(str(relative)).as_posix()
        closure.append((normalized, content))
        if name in by_name:
            raise ScientificAdapterError("BindCraft shard publishes a duplicate result name")
        by_name[name] = (semantic_type, pointer, content)

    shard_record = _json_object(
        _contained_artifact(output, raw_paths[shard_pointer.artifact_id], label="BindCraft shard record"),
        label="BindCraft shard record",
    )
    index = shard_record.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < parameters.shard_count:
        raise ScientificAdapterError("BindCraft shard record carries an invalid shard index")
    if shard_name != f"shard-{index:03d}":
        raise ScientificAdapterError("BindCraft shard entry name disagrees with its index")
    if (
        shard_record.get("status") != "succeeded"
        or shard_record.get("backend_id") != BACKEND_ID
        or shard_record.get("source_revision") != SOURCE_REVISION
        or shard_record.get("seed") != parameters.seed(index)
    ):
        raise ScientificAdapterError("BindCraft shard record does not identify this successful immutable run")

    candidate_entries: dict[int, dict[str, tuple[ArtifactPointer, bytes]]] = {}
    for name, (semantic_type, pointer, content) in by_name.items():
        match = _CANDIDATE_NAME.fullmatch(name)
        if match is None:
            if name != shard_name:
                raise ScientificAdapterError("BindCraft shard publishes an unexpected result name")
            continue
        ordinal = int(match.group(1))
        kind = match.group(2)
        expected_semantic = {
            "metrics": "bindcraft-native-design-metrics-json/v1",
            "structure": "protein-structure-pdb/v1",
            "relaxed-complex": "bindcraft-native-relaxed-complex-pdb/v1",
        }[kind]
        if semantic_type != expected_semantic:
            raise ScientificAdapterError("BindCraft candidate semantic type disagrees with its name")
        candidate_entries.setdefault(ordinal, {})[kind] = (pointer, content)
    expected_ordinals = set(range(parameters.accepted_designs(index)))
    if set(candidate_entries) != expected_ordinals:
        raise ScientificAdapterError("BindCraft shard did not publish its exact assigned candidate quota")
    for ordinal, parts in sorted(candidate_entries.items()):
        if set(parts) != {"metrics", "structure", "relaxed-complex"}:
            raise ScientificAdapterError("BindCraft candidate result closure is incomplete")
        metrics_pointer, metrics_content = parts["metrics"]
        if metrics_pointer.media_type != "application/json" or metrics_pointer.compression not in {None, "none"}:
            raise ScientificAdapterError("BindCraft candidate metrics are not uncompressed JSON")
        try:
            metrics = json.loads(metrics_content)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ScientificAdapterError("BindCraft candidate metrics are not readable JSON") from error
        if not isinstance(metrics, Mapping):
            raise ScientificAdapterError("BindCraft candidate metrics must be a JSON object")
        _validate_candidate_metrics(metrics, parameters, shard_index=index, candidate_index=ordinal)
        structure_pointer, structure_content = parts["structure"]
        _validate_structure("binder structure", structure_pointer, structure_content, two_chains=False)
        relaxed_pointer, relaxed_content = parts["relaxed-complex"]
        _validate_structure("relaxed complex", relaxed_pointer, relaxed_content, two_chains=True)

    pyrosetta = descriptor.get("pyrosetta")
    if not isinstance(pyrosetta, Mapping) or (
        pyrosetta.get("version") != PYROSETTA_EXPECTED_VERSION
        or pyrosetta.get("tree_manifest_sha256") != PYROSETTA_INVENTORY_SHA256
    ):
        raise ScientificAdapterError("BindCraft shard does not bind the authorized PyRosetta tree")
    _validate_external_tree_receipts(descriptor.get("external_trees"))
    return index, tuple(sorted(closure))


def _deterministic_zstd_tar(files: tuple[tuple[str, bytes], ...]) -> bytes:
    if not 1 <= len(files) <= MAX_BUNDLE_MEMBERS or sum(len(content) for _name, content in files) > MAX_BUNDLE_BYTES:
        raise ScientificAdapterError("BindCraft shard bundle exceeds the extraction bound")
    names = [name for name, _content in files]
    if len(set(names)) != len(names):
        raise ScientificAdapterError("BindCraft shard bundle contains duplicate paths")
    stream = io.BytesIO()
    try:
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, content in sorted(files):
                relative = PurePosixPath(name)
                if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                    raise ScientificAdapterError("BindCraft shard bundle path is unsafe")
                info = tarfile.TarInfo(relative.as_posix())
                info.size = len(content)
                info.mode = 0o444
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = 0
                archive.addfile(info, io.BytesIO(content))
        result = zstandard.ZstdCompressor(level=3, write_checksum=True, write_content_size=True).compress(
            stream.getvalue()
        )
    except (OSError, tarfile.TarError, zstandard.ZstdError) as error:
        raise ScientificAdapterError("BindCraft shard bundle could not be encoded deterministically") from error
    if len(result) > MAX_BUNDLE_BYTES:
        raise ScientificAdapterError("BindCraft shard bundle exceeds the compressed byte bound")
    return result


def collect_design_output(request_value: object, workspace: Path) -> CollectedOutput:
    """Publish one bounded deterministic archive the aggregate can overlay."""

    index, closure = _validated_shard(request_value, workspace)
    content = _deterministic_zstd_tar(closure)
    digest = hashlib.sha256(content).hexdigest()
    artifact_id = f"result.bindcraft.shard-{index:03d}.{digest[:32]}"
    pointer = ArtifactPointer(
        artifact_id=artifact_id,
        sha256=digest,
        size_bytes=len(content),
        media_type="application/octet-stream",
        compression="zstd",
    )
    return CollectedOutput(
        manifest={
            "schema": ARTIFACT_MANIFEST_SCHEMA,
            "manifest_id": f"bindcraft.shard.{index:03d}.handoff",
            "entries": [
                {
                    "name": f"shard-{index:03d}-bundle",
                    "semantic_type": "bindcraft-native-shard-bundle-tar/v1",
                    "artifact": pointer.to_dict(),
                }
            ],
        },
        blobs={artifact_id: content},
    )


def collect_output(request_value: object, workspace: Path) -> CollectedOutput:
    """Backward-compatible name for the design-stage collector."""

    return collect_design_output(request_value, workspace)


def _runtime_document(workspace: Path, relative: str, expected: bytes, *, label: str) -> bytes:
    path = _contained_artifact(workspace, relative, label=label)
    content = path.read_bytes()
    if content != expected:
        raise ScientificAdapterError(f"{label} differs from the adapter's canonical projection")
    return content


def _validated_runtime_documents(
    request: PublicRunRequest,
    parameters: BindCraftParameters,
    workspace: Path,
    *,
    shard_index: int,
) -> tuple[Mapping[str, object], dict[str, object]]:
    """Reload and verify the controller projection without trusting the public pointer."""

    manifest_path = _contained_artifact(
        workspace,
        ".fs2/input-manifest.json",
        label="BindCraft native input manifest",
    )
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest_value = json.loads(manifest_bytes)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ScientificAdapterError("BindCraft native input manifest is unreadable") from error
    if not isinstance(manifest_value, Mapping):
        raise ScientificAdapterError("BindCraft native input manifest must be a JSON object")
    _native_manifest_target(manifest_value)
    expected_manifest_bytes = _canonical_json(manifest_value).encode("ascii")
    if manifest_bytes != expected_manifest_bytes:
        raise ScientificAdapterError("BindCraft native input manifest is not canonical")
    projected_request = native_request(
        request,
        parameters,
        manifest_value,
        shard_index=shard_index,
    )
    _runtime_document(
        workspace,
        ".fs2/request.json",
        _canonical_json(projected_request).encode("ascii"),
        label="BindCraft native request",
    )
    return manifest_value, projected_request


def collect_aggregate_output(
    request_value: object,
    workspace: Path,
    *,
    runtime_image_digest: str,
) -> CollectedOutput:
    """Validate and collect the complete r18 CPU aggregation result."""

    public, parameters = _request(request_value)
    if _DIGEST_REFERENCE.fullmatch(runtime_image_digest) is None:
        raise ScientificAdapterError("BindCraft aggregate requires an immutable runtime image digest")
    _input_manifest, projected_request = _validated_runtime_documents(
        public,
        parameters,
        workspace,
        shard_index=0,
    )
    request_identity_bytes = _canonical_bytes(projected_request)
    marker_path = _contained_artifact(
        workspace,
        ".fs2/runtime-localization.json",
        label="BindCraft runtime localization marker",
    )
    marker_sha256 = hashlib.sha256(marker_path.read_bytes()).hexdigest()
    marker = _json_object(marker_path, label="BindCraft runtime localization marker")
    localization_generation = marker.get("generation", "")
    if not isinstance(localization_generation, str) or len(localization_generation) > 128:
        raise ScientificAdapterError("BindCraft runtime localization generation is invalid")

    manifest_path = _contained_artifact(
        workspace,
        "output/output-manifest.json",
        label="BindCraft aggregate output manifest",
    )
    sidecar_path = _contained_artifact(
        workspace,
        "output/output-manifest.json.artifact-paths.json",
        label="BindCraft aggregate artifact path sidecar",
    )
    manifest = _json_object(manifest_path, label="BindCraft aggregate output manifest")
    if (
        manifest.get("schema") != ARTIFACT_MANIFEST_SCHEMA
        or manifest.get("manifest_id") != "manifest.bindcraft.native.output"
    ):
        raise ScientificAdapterError("BindCraft aggregate output manifest identity is unsupported")
    raw_entries = manifest.get("entries")
    paths = _json_object(sidecar_path, label="BindCraft aggregate artifact path sidecar")
    if not isinstance(raw_entries, list) or not 1 <= len(raw_entries) <= MAX_FINAL_ENTRIES:
        raise ScientificAdapterError("BindCraft aggregate output entry count is outside the bound")

    parsed = [
        _entry(raw, label=f"BindCraft aggregate entry {index}", maximum_bytes=MAX_OUTPUT_BYTES)
        for index, raw in enumerate(raw_entries)
    ]
    names = [name for name, _semantic, _pointer in parsed]
    artifact_ids = [pointer.artifact_id for _name, _semantic, pointer in parsed]
    if len(set(names)) != len(names) or len(set(artifact_ids)) != len(artifact_ids) or set(paths) != set(artifact_ids):
        raise ScientificAdapterError("BindCraft aggregate manifest and artifact path closure disagree")

    expected_entries = parameters.shard_count + parameters.designs * 3 + 1
    if len(parsed) != expected_entries:
        raise ScientificAdapterError("BindCraft aggregate result count does not match the requested run")
    collector_entries: list[tuple[str, str, Path, bool]] = []
    candidate_parts: dict[tuple[int, int], dict[str, tuple[ArtifactPointer, bytes, Path, str]]] = {}
    aggregate_value: Mapping[str, Any] | None = None
    seen_shards: set[int] = set()
    for name, semantic_type, pointer in parsed:
        raw_path = paths[pointer.artifact_id]
        if not isinstance(raw_path, str):
            raise ScientificAdapterError("BindCraft aggregate artifact path is invalid")
        try:
            resolved = Path(raw_path).resolve(strict=True)
            root = workspace.resolve(strict=True)
        except OSError as error:
            raise ScientificAdapterError("BindCraft aggregate artifact path is unavailable") from error
        if root not in resolved.parents or not resolved.is_file() or Path(raw_path).is_symlink():
            raise ScientificAdapterError("BindCraft aggregate artifact escapes its workspace")
        content = _verified_content(resolved, pointer, label=f"BindCraft aggregate artifact {pointer.artifact_id}")
        shard_match = re.fullmatch(r"shard-([0-9]{3})", name)
        candidate_match = re.fullmatch(
            r"artifact\.bindcraft\.native\.s([0-9]{3})\.c([0-9]{3})\.(metrics|pdb|relaxed-complex)",
            pointer.artifact_id,
        )
        if shard_match is not None:
            if semantic_type != "bindcraft-native-shard-result-json/v1" or pointer.media_type != "application/json":
                raise ScientificAdapterError("BindCraft aggregate shard entry has the wrong type")
            shard = _json_object(resolved, label="BindCraft aggregate shard record")
            shard_index = int(shard_match.group(1))
            if (
                shard.get("index") != shard_index
                or shard.get("backend_id") != BACKEND_ID
                or shard.get("source_revision") != SOURCE_REVISION
                or shard.get("status") != "succeeded"
            ):
                raise ScientificAdapterError("BindCraft aggregate contains an invalid shard record")
            seen_shards.add(shard_index)
            collector_entries.append((name, semantic_type, resolved, False))
        elif candidate_match is not None:
            shard_index = int(candidate_match.group(1))
            local_index = int(candidate_match.group(2))
            kind = {
                "metrics": "metrics",
                "pdb": "structure",
                "relaxed-complex": "complex",
            }[candidate_match.group(3)]
            expected_semantic = {
                "metrics": "bindcraft-native-design-metrics-json/v1",
                "structure": "protein-structure-pdb/v1",
                "complex": "bindcraft-native-relaxed-complex-pdb/v1",
            }[kind]
            if not name.startswith("candidate-") or semantic_type != expected_semantic:
                raise ScientificAdapterError("BindCraft aggregate candidate type disagrees with its artifact identity")
            parts = candidate_parts.setdefault((shard_index, local_index), {})
            if kind in parts:
                raise ScientificAdapterError("BindCraft aggregate repeats one candidate result kind")
            parts[kind] = (pointer, content, resolved, semantic_type)
        elif name == "aggregate":
            if semantic_type != "bindcraft-native-aggregate-json/v1" or pointer.media_type != "application/json":
                raise ScientificAdapterError("BindCraft aggregate identity entry has the wrong type")
            aggregate_value = _json_object(resolved, label="BindCraft aggregate identity")
            collector_entries.append((name, semantic_type, resolved, False))
        else:
            raise ScientificAdapterError("BindCraft aggregate publishes an unexpected result name")

    expected_candidates = {
        (shard_index, candidate_index)
        for shard_index in range(parameters.shard_count)
        for candidate_index in range(parameters.accepted_designs(shard_index))
    }
    if seen_shards != set(range(parameters.shard_count)) or set(candidate_parts) != expected_candidates:
        raise ScientificAdapterError("BindCraft aggregate does not cover every expected shard and candidate")
    for ordinal, ((candidate_shard_index, candidate_index), parts) in enumerate(sorted(candidate_parts.items())):
        if set(parts) != {"metrics", "structure", "complex"}:
            raise ScientificAdapterError("BindCraft aggregate candidate closure is incomplete")
        metrics_pointer, metrics_content, metrics_path, metrics_semantic = parts["metrics"]
        if metrics_pointer.media_type != "application/json":
            raise ScientificAdapterError("BindCraft aggregate candidate metrics have the wrong media type")
        try:
            metrics = json.loads(metrics_content)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ScientificAdapterError("BindCraft aggregate candidate metrics are unreadable") from error
        if not isinstance(metrics, Mapping):
            raise ScientificAdapterError("BindCraft aggregate candidate metrics must be an object")
        recorded_shard_index = metrics.get("shard_index")
        candidate_id = metrics.get("candidate_id")
        match = re.fullmatch(r"native-s([0-9]{3})-c([0-9]{3})", str(candidate_id))
        if (
            isinstance(recorded_shard_index, bool)
            or not isinstance(recorded_shard_index, int)
            or match is None
            or recorded_shard_index != candidate_shard_index
            or int(match.group(1)) != candidate_shard_index
            or int(match.group(2)) != candidate_index
        ):
            raise ScientificAdapterError("BindCraft aggregate candidate identity is malformed")
        _validate_candidate_metrics(
            metrics,
            parameters,
            shard_index=candidate_shard_index,
            candidate_index=candidate_index,
        )
        structure_pointer, structure_content, structure_path, structure_semantic = parts["structure"]
        _validate_structure("binder structure", structure_pointer, structure_content, two_chains=False)
        complex_pointer, complex_content, complex_path, complex_semantic = parts["complex"]
        _validate_structure("relaxed complex", complex_pointer, complex_content, two_chains=True)
        collector_entries.extend(
            (
                (f"candidate-{ordinal:03d}-metrics", metrics_semantic, metrics_path, False),
                (f"candidate-{ordinal:03d}-structure", structure_semantic, structure_path, False),
                (f"candidate-{ordinal:03d}-relaxed-complex", complex_semantic, complex_path, False),
            )
        )

    if aggregate_value is None:
        raise ScientificAdapterError("BindCraft aggregate identity is absent")
    expected_identity: dict[str, object] = {
        "backend_id": BACKEND_ID,
        "source_revision": SOURCE_REVISION,
        "access_profile": "academic",
        "academic_asset_id": ACADEMIC_ASSET_ID,
        "academic_artifact_sha256": PYROSETTA_ARCHIVE_SHA256,
        "request_sha256": hashlib.sha256(request_identity_bytes).hexdigest(),
        "runtime_image_digest": runtime_image_digest,
        "runtime_localization_marker_sha256": marker_sha256,
        "localization_generation": localization_generation,
        "expected_shards": parameters.shard_count,
        "succeeded_shards": parameters.shard_count,
        "atomic_commit": True,
    }
    for key, expected in expected_identity.items():
        if aggregate_value.get(key) != expected:
            raise ScientificAdapterError(f"BindCraft aggregate immutable identity disagrees on {key}")

    collector_entries.append(
        (
            "output-manifest",
            "bindcraft-native-output-manifest-json/v1",
            manifest_path,
            False,
        )
    )
    return collect_output_files(
        workspace,
        tuple(collector_entries),
        manifest_id=f"bindcraft.results.{hashlib.sha256(request_identity_bytes).hexdigest()[:24]}",
        maximum_total_bytes=MAX_OUTPUT_BYTES,
    )


def collect_stage_output(
    collector_id: str,
    request_value: object,
    workspace: Path,
    *,
    runtime_image_digest: str | None = None,
) -> CollectedOutput:
    """Dispatch only the collector identity frozen into the stage invocation."""

    if collector_id == DESIGN_COLLECTOR_ID:
        return collect_design_output(request_value, workspace)
    if collector_id == AGGREGATE_COLLECTOR_ID:
        if runtime_image_digest is None:
            raise ScientificAdapterError("BindCraft aggregate collector requires an immutable execution image digest")
        return collect_aggregate_output(
            request_value,
            workspace,
            runtime_image_digest=runtime_image_digest,
        )
    raise ScientificAdapterError(f"unsupported BindCraft collector identity {collector_id!r}")


def collect_companion_output(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    """Collect one controller-bound BindCraft stage after atomic completion."""

    if invocation.validator_id != VALIDATOR_ID or invocation.collector_id not in {
        DESIGN_COLLECTOR_ID,
        AGGREGATE_COLLECTOR_ID,
    }:
        raise ScientificAdapterError("BindCraft collector received another execution identity")
    completion_sha256 = completion_marker(invocation, workspace, label="BindCraft")
    request = _load_public_request(workspace)
    public, parameters = _request(request)
    if invocation.collector_id == DESIGN_COLLECTOR_ID:
        if invocation.stage_id != DESIGN_STAGE or invocation.handoff_name is None:
            raise ScientificAdapterError("BindCraft design collector received another stage contract")
        match = re.fullmatch(r"design-([0-9]{3})", invocation.shard_id)
        if match is None:
            raise ScientificAdapterError("BindCraft design collector received an invalid shard identity")
        shard_index = int(match.group(1))
        if shard_index >= parameters.shard_count or parameters.shard_ids[shard_index] != invocation.shard_id:
            raise ScientificAdapterError("BindCraft design collector shard is outside the request plan")
        _validated_runtime_documents(
            public,
            parameters,
            workspace,
            shard_index=shard_index,
        )
        collected = collect_design_output(request, workspace)
        return materialize_collected_output(
            invocation,
            workspace,
            collected,
            label="BindCraftDesign",
            completion_sha256=completion_sha256,
            validation={
                "request_sha256": canonical_digest(public.to_dict()),
                "shard_id": invocation.shard_id,
            },
        )
    if invocation.stage_id != AGGREGATE_STAGE or invocation.handoff_name is not None:
        raise ScientificAdapterError("BindCraft aggregate collector received another stage contract")
    runtime_image_digest = os.environ.get("FS2_RUNTIME_IMAGE_DIGEST")
    if runtime_image_digest is None:
        raise ScientificAdapterError("BindCraft aggregate collector has no immutable execution image digest")
    collected = collect_aggregate_output(
        request,
        workspace,
        runtime_image_digest=runtime_image_digest,
    )
    return materialize_collected_output(
        invocation,
        workspace,
        collected,
        label="BindCraftAggregate",
        completion_sha256=completion_sha256,
        validation={
            "request_sha256": canonical_digest(public.to_dict()),
            "design_count": parameters.designs,
            "shard_count": parameters.shard_count,
            "runtime_image_digest": runtime_image_digest,
        },
    )

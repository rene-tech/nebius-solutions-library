"""Shared parsing and artifact helpers for scientific-batch model adapters.

The public envelope is the canonical ``scientific-run-request/v1`` contract.
Model adapters validate only its ``parameters`` object, select a bounded shard
expansion, and attach executable argv to the controller-owned stage plan.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from ..catalog_adapter import ScientificStageExpansion, scientific_plan_from_catalog_profile
from ..models import AdapterExecutionPlan, ServiceClass, StageInvocation

# Re-exported so every adapter keeps importing these from one place; the
# definitions live in the dependency-free primitives module because the same
# localization code also runs inside a model runtime image.
from .primitives import ScientificAdapterError as ScientificAdapterError
from .primitives import strict_object as strict_object

RUN_REQUEST_SCHEMA = "fs2-serve.nebius.ai/scientific-run-request/v1"
ARTIFACT_MANIFEST_SCHEMA = "fs2-serve.nebius.ai/scientific-artifact-manifest/v1"

_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_SEMANTIC_TYPE = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?/v[1-9][0-9]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+_-]*$")
_CONTROLLER_ROOT = PurePosixPath("/mnt/fs2-scientific")
_RUNTIME_ARTIFACT_ROOT = PurePosixPath("/opt/fs2/artifacts")
_RECIPE_SHARED_PATHS = (
    "components/control-plane/src/fs2_serve/scientific_batch/__init__.py",
    "components/control-plane/src/fs2_serve/scientific_batch/controller.py",
    "components/control-plane/src/fs2_serve/scientific_batch/models.py",
    "components/control-plane/src/fs2_serve/scientific_batch/catalog_adapter.py",
    "components/control-plane/src/fs2_serve/scientific_batch/protocols.py",
    "components/control-plane/src/fs2_serve/scientific_batch/adapters/__init__.py",
    "components/control-plane/src/fs2_serve/scientific_batch/adapters/common.py",
    "components/control-plane/src/fs2_serve/scientific_batch/adapters/primitives.py",
    "components/control-plane/src/fs2_serve/scientific_batch/adapters/materialization.py",
    "components/control-plane/src/fs2_serve/scientific_batch/adapters/localization.py",
    "catalog/runtime/schema/scientific-run-request.schema.json",
    "catalog/runtime/schema/scientific-run-result.schema.json",
    "catalog/runtime/schema/scientific-artifact-localization.schema.json",
    "catalog/runtime/contracts/scientific-artifact-localization.json",
)
_RECIPE_MODEL_PATHS = {
    "boltzgen": (
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/boltzgen.py",
        "catalog/runtime/schema/boltzgen-parameters.schema.json",
        "models/structure/batch-adapters/boltzgen/adapter.py",
        "models/structure/batch-adapters/boltzgen/contract.json",
    ),
    "proteina-complexa": (
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/proteina_complexa.py",
        "catalog/runtime/schema/proteina-complexa-parameters.schema.json",
        "models/structure/batch-adapters/proteina-complexa/adapter.py",
        "models/structure/batch-adapters/proteina-complexa/contract.json",
    ),
}


def load_json_request(payload: str | bytes) -> Mapping[str, object]:
    """Load one bounded request and reject duplicate keys at every depth."""

    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if not isinstance(raw, bytes) or len(raw) > 1_048_576:
        raise ScientificAdapterError("request JSON must be at most 1 MiB")

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ScientificAdapterError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=no_duplicates,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ScientificAdapterError(f"non-finite JSON number: {constant}")
            ),
        )
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ScientificAdapterError("request is not valid UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise ScientificAdapterError("request JSON must contain one object")
    return cast(Mapping[str, object], value)


def bounded_int(value: object, *, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ScientificAdapterError(f"{label} must be an integer in [{minimum}, {maximum}]")
    return value


def safe_name(value: object, *, label: str, maximum: int = 63) -> str:
    if not isinstance(value, str) or len(value) > maximum or _NAME.fullmatch(value) is None:
        raise ScientificAdapterError(f"{label} must be a bounded lowercase logical name")
    return value


def canonical_digest(value: object) -> str:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def runtime_recipe_sha256(solution_root: Path, model_id: str) -> str:
    """Hash the model adapter together with every shared execution contract."""

    try:
        paths = (*_RECIPE_SHARED_PATHS, *_RECIPE_MODEL_PATHS[model_id])
    except KeyError as error:
        raise ScientificAdapterError(f"no runtime recipe is registered for {model_id}") from error
    digest = hashlib.sha256()
    for relative in sorted(paths):
        path = solution_root / relative
        content = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ArtifactPointer:
    artifact_id: str
    sha256: str
    size_bytes: int
    media_type: str
    compression: str | None = None

    @classmethod
    def parse(cls, value: object, *, label: str, maximum_bytes: int) -> ArtifactPointer:
        item = strict_object(
            value,
            required=frozenset({"artifact_id", "sha256", "size_bytes", "media_type"}),
            optional=frozenset({"compression"}),
            label=label,
        )
        artifact_id = item["artifact_id"]
        if not isinstance(artifact_id, str) or _ARTIFACT_ID.fullmatch(artifact_id) is None:
            raise ScientificAdapterError(f"{label}.artifact_id must be a logical artifact ID")
        digest = item["sha256"]
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ScientificAdapterError(f"{label}.sha256 must be a lowercase SHA-256")
        size_bytes = bounded_int(item["size_bytes"], minimum=1, maximum=maximum_bytes, label=f"{label}.size_bytes")
        media_type = item["media_type"]
        if not isinstance(media_type, str) or _MEDIA_TYPE.fullmatch(media_type) is None:
            raise ScientificAdapterError(f"{label}.media_type is invalid")
        compression = item.get("compression")
        if compression is not None and compression not in {"gzip", "zstd", "none"}:
            raise ScientificAdapterError(f"{label}.compression is invalid")
        return cls(artifact_id, digest, size_bytes, media_type, cast(str | None, compression))

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "artifact_id": self.artifact_id,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
        }
        if self.compression is not None:
            value["compression"] = self.compression
        return value


@dataclass(frozen=True, slots=True)
class PublicRunRequest:
    operation: str
    service_class: ServiceClass
    input_manifest: ArtifactPointer
    parameters: Mapping[str, object]
    client_context: Mapping[str, object] | None

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": RUN_REQUEST_SCHEMA,
            "operation": self.operation,
            "service_class": self.service_class.value,
            "input_manifest": self.input_manifest.to_dict(),
            "parameters": dict(self.parameters),
        }
        if self.client_context is not None:
            value["client_context"] = dict(self.client_context)
        return value


def parse_public_request(value: object, *, maximum_input_bytes: int) -> PublicRunRequest:
    item = strict_object(
        value,
        required=frozenset({"schema", "operation", "service_class", "input_manifest", "parameters"}),
        optional=frozenset({"client_context"}),
        label="scientific run request",
    )
    if item["schema"] != RUN_REQUEST_SCHEMA:
        raise ScientificAdapterError("scientific run request schema is invalid")
    operation = safe_name(item["operation"], label="operation")
    try:
        service_class = ServiceClass(cast(str, item["service_class"]))
    except (TypeError, ValueError) as error:
        raise ScientificAdapterError("service_class is invalid") from error
    pointer = ArtifactPointer.parse(item["input_manifest"], label="input_manifest", maximum_bytes=maximum_input_bytes)
    parameters = item["parameters"]
    if not isinstance(parameters, Mapping) or not all(isinstance(key, str) for key in parameters):
        raise ScientificAdapterError("parameters must be an object")
    context_value = item.get("client_context")
    context: Mapping[str, object] | None = None
    if context_value is not None:
        context = strict_object(
            context_value,
            required=frozenset(),
            optional=frozenset({"batch_id", "correlation_id", "display_name"}),
            label="client_context",
        )
        for key, raw in context.items():
            if not isinstance(raw, str) or not raw or len(raw) > 128:
                raise ScientificAdapterError(f"client_context.{key} is invalid")
            if key != "display_name" and _ARTIFACT_ID.fullmatch(raw) is None:
                raise ScientificAdapterError(f"client_context.{key} must be an opaque logical ID")
    return PublicRunRequest(operation, service_class, pointer, cast(Mapping[str, object], parameters), context)


def profile_from_catalog(profile_set: object, model_id: str) -> Mapping[str, object]:
    root = strict_object(
        profile_set,
        required=frozenset({"schema", "profiles"}),
        label="scientific workload profile set",
    )
    if root["schema"] != "fs2-serve.nebius.ai/scientific-workload-profiles/v1":
        raise ScientificAdapterError("scientific workload profile set schema is invalid")
    values = root["profiles"]
    if not isinstance(values, list):
        raise ScientificAdapterError("scientific workload profiles must be an array")
    matches = [value for value in values if isinstance(value, Mapping) and value.get("model_id") == model_id]
    if len(matches) != 1:
        raise ScientificAdapterError(f"catalog must contain exactly one profile for {model_id}")
    return cast(Mapping[str, object], matches[0])


# A profile's lifecycle state and its route exposure must agree. A candidate is never
# routed; a dispatchable or qualified profile always is. Checking the pair rather than one
# fixed combination still stops an unvetted model being dispatched, while allowing a model
# that has earned dispatch to be served.
_ROUTED_PROFILE_STATES = frozenset({"active", "qualified"})


def profile_state_is_consistent(state: object, route_exposed: object) -> bool:
    if state == "candidate-unqualified":
        return route_exposed is False
    if state in _ROUTED_PROFILE_STATES:
        return route_exposed is True
    return False


def assert_profile_identity(
    profile: Mapping[str, object],
    *,
    model_id: str,
    repository: str,
    revision: str,
    parameter_schema: str,
    request: PublicRunRequest,
) -> None:
    if (
        profile.get("schema") != "fs2-serve.nebius.ai/scientific-workload-profile/v1"
        or profile.get("model_id") != model_id
        or not profile_state_is_consistent(profile.get("state"), profile.get("route_exposed"))
    ):
        raise ScientificAdapterError("catalog workload profile identity or candidate state is invalid")
    source = strict_object(
        profile.get("source"),
        required=frozenset({"kind", "repository", "revision", "review_url", "classification"}),
        label="catalog profile source",
    )
    if source["repository"] != repository or source["revision"] != revision:
        raise ScientificAdapterError("catalog workload profile source identity does not match the adapter")
    interface = strict_object(
        profile.get("interface"),
        required=frozenset(
            {
                "protocol",
                "submit_endpoint",
                "request_schema",
                "result_schema",
                "parameter_schema",
                "operations",
                "service_classes",
                "mcp",
            }
        ),
        label="catalog profile interface",
    )
    if interface["request_schema"] != RUN_REQUEST_SCHEMA or interface["parameter_schema"] != parameter_schema:
        raise ScientificAdapterError("catalog profile does not use the adapter's canonical request contracts")
    operations = interface["operations"]
    service_classes = interface["service_classes"]
    if not isinstance(operations, list) or not all(isinstance(value, str) for value in operations):
        raise ScientificAdapterError("catalog profile operations are invalid")
    if not isinstance(service_classes, list) or not all(isinstance(value, str) for value in service_classes):
        raise ScientificAdapterError("catalog profile service classes are invalid")
    if request.operation not in operations:
        raise ScientificAdapterError("operation is not allowed by the catalog profile")
    if request.service_class.value not in service_classes:
        raise ScientificAdapterError("service_class is not allowed by the catalog profile")
    resources = strict_object(
        profile.get("resources"),
        required=frozenset(
            {"gpu_count", "gpu_topology", "host_architectures", "compatible_pool_ids", "required_node_labels"}
        ),
        label="catalog profile resources",
    )
    if resources["gpu_count"] != 1 or resources["gpu_topology"] != "single-gpu":
        raise ScientificAdapterError("primary adapters require one GPU per GPU workload unit")


def input_root(artifact_id: str) -> str:
    if _ARTIFACT_ID.fullmatch(artifact_id) is None:
        raise ScientificAdapterError("artifact_id is not safe for controller localization")
    return str(_CONTROLLER_ROOT / "inputs" / artifact_id)


def model_root(artifact_id: str) -> str:
    """Return the image contract's controller-mounted artifact directory."""

    if _ARTIFACT_ID.fullmatch(artifact_id) is None:
        raise ScientificAdapterError("model artifact ID is invalid")
    return str(_RUNTIME_ARTIFACT_ROOT / artifact_id)


def model_file(artifact_id: str, filename: str) -> str:
    path = PurePosixPath(filename)
    if path.is_absolute() or len(path.parts) != 1 or path.name != filename or filename in {".", ".."}:
        raise ScientificAdapterError("model artifact filename is invalid")
    return str(PurePosixPath(model_root(artifact_id)) / filename)


def stage_workspace(stage_id: str, shard_id: str) -> str:
    safe_name(stage_id, label="stage_id")
    safe_name(shard_id, label="shard_id")
    return str(_CONTROLLER_ROOT / "work" / stage_id / shard_id)


def run_workspace(model_id: str, operation_id: str, shard_id: str) -> str:
    """Return an operation-isolated controller workspace.

    The operation identifier is hashed rather than exposed as a path segment so
    concurrent requests cannot collide and no caller-controlled path syntax is
    interpreted by the runtime.
    """

    safe_name(model_id, label="model_id")
    safe_name(operation_id, label="operation_id", maximum=127)
    safe_name(shard_id, label="shard_id")
    operation_key = hashlib.sha256(operation_id.encode("ascii")).hexdigest()[:20]
    return str(_CONTROLLER_ROOT / "work" / model_id / operation_key / shard_id)


def logical_stage_artifact(operation_id: str, stage_id: str, shard_id: str) -> str:
    safe_name(operation_id, label="operation_id", maximum=127)
    safe_name(stage_id, label="stage_id")
    safe_name(shard_id, label="shard_id")
    operation_key = hashlib.sha256(operation_id.encode("ascii")).hexdigest()[:20]
    return f"run.{operation_key}.{stage_id}.{shard_id}"


def build_execution_plan(
    *,
    model_id: str,
    variant_id: str,
    source_revision: str,
    request: PublicRunRequest,
    profile: Mapping[str, object],
    expansions: Mapping[str, ScientificStageExpansion] | None,
    invocations: tuple[StageInvocation, ...],
    required_model_artifacts: tuple[str, ...],
) -> AdapterExecutionPlan:
    controller_plan = scientific_plan_from_catalog_profile(profile, expansions=expansions)
    try:
        return AdapterExecutionPlan(
            model_id=model_id,
            variant_id=variant_id,
            source_revision=source_revision,
            request_sha256=canonical_digest(request.to_dict()),
            controller_plan=controller_plan,
            invocations=invocations,
            required_model_artifacts=required_model_artifacts,
        )
    except ValueError as error:
        raise ScientificAdapterError(str(error)) from error


@dataclass(frozen=True, slots=True)
class LoadedArtifact:
    name: str
    semantic_type: str
    pointer: ArtifactPointer
    content: bytes


ArtifactLoader = Callable[[str], bytes]


@dataclass(frozen=True, slots=True)
class CollectedOutput:
    """Canonical manifest plus bytes ready for the artifact-store commit."""

    manifest: Mapping[str, object]
    blobs: Mapping[str, bytes]


def parse_csv_artifact(
    content: bytes,
    *,
    label: str,
    maximum_rows: int,
    maximum_columns: int = 512,
) -> tuple[tuple[str, ...], tuple[Mapping[str, str], ...]]:
    if len(content) > 64 * 1024 * 1024 or b"\x00" in content:
        raise ScientificAdapterError(f"{label} exceeds the CSV byte bound")
    try:
        text = content.decode("utf-8")
    except UnicodeError as error:
        raise ScientificAdapterError(f"{label} is not UTF-8 CSV") from error
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fields = tuple(reader.fieldnames or ())
    if (
        not fields
        or len(fields) > maximum_columns
        or len(set(fields)) != len(fields)
        or any(not field for field in fields)
    ):
        raise ScientificAdapterError(f"{label} has invalid or duplicate CSV columns")
    rows: list[Mapping[str, str]] = []
    try:
        for index, row in enumerate(reader):
            if index >= maximum_rows:
                raise ScientificAdapterError(f"{label} exceeds the CSV row bound")
            if None in row or any(value is None or len(value) > 65_536 for value in row.values()):
                raise ScientificAdapterError(f"{label} contains an invalid CSV row")
            rows.append(cast(Mapping[str, str], row))
    except csv.Error as error:
        raise ScientificAdapterError(f"{label} is malformed CSV") from error
    if not rows:
        raise ScientificAdapterError(f"{label} contains no result rows")
    return fields, tuple(rows)


def canonicalize_upstream_csv(content: bytes, *, label: str, maximum_rows: int) -> bytes:
    """Drop upstream filesystem columns before committing customer-visible CSV."""

    fields, rows = parse_csv_artifact(content, label=label, maximum_rows=maximum_rows)
    forbidden_names = ("path", "url", "token", "secret", "password", "credential")
    retained = tuple(field for field in fields if not any(marker in field.lower() for marker in forbidden_names))
    if not retained:
        raise ScientificAdapterError(f"{label} has no safe result columns")
    for row in rows:
        for field in retained:
            value = row[field]
            if "://" in value or value.startswith(("/", "../")) or "\\" in value:
                raise ScientificAdapterError(f"{label} contains a non-public path or URL value")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=retained, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def collect_output_files(
    workspace: Path,
    entries: tuple[tuple[str, str, Path, bool], ...],
    *,
    manifest_id: str,
    maximum_total_bytes: int,
) -> CollectedOutput:
    """Read an allow-listed set of contained regular outputs into a manifest."""

    root = workspace.resolve(strict=True)
    blobs: dict[str, bytes] = {}
    manifest_entries: list[dict[str, object]] = []
    total = 0
    for name, semantic_type, path, sanitize_csv in entries:
        resolved = path.resolve(strict=True)
        if root not in resolved.parents or not resolved.is_file() or resolved.is_symlink():
            raise ScientificAdapterError("collector output must be a contained regular file")
        content = resolved.read_bytes()
        if sanitize_csv:
            content = canonicalize_upstream_csv(content, label=name, maximum_rows=100_000)
        total += len(content)
        if total > maximum_total_bytes:
            raise ScientificAdapterError("collected outputs exceed the byte bound")
        digest = hashlib.sha256(content).hexdigest()
        artifact_id = f"result.{hashlib.sha256(name.encode()).hexdigest()[:16]}.{digest[:32]}"
        if artifact_id in blobs:
            raise ScientificAdapterError("collector produced a duplicate artifact identity")
        blobs[artifact_id] = content
        suffix = resolved.suffix.lower()
        media_type = (
            "text/csv"
            if semantic_type.endswith("csv/v1")
            else "application/json"
            if semantic_type.endswith("json/v1") or suffix == ".json"
            else "chemical/x-mmcif"
            if suffix in {".cif", ".mmcif"}
            else "chemical/x-pdb"
        )
        manifest_entries.append(
            {
                "name": name,
                "semantic_type": semantic_type,
                "artifact": ArtifactPointer(artifact_id, digest, len(content), media_type).to_dict(),
            }
        )
    if not manifest_entries:
        raise ScientificAdapterError("collector found no final outputs")
    return CollectedOutput(
        manifest={"schema": ARTIFACT_MANIFEST_SCHEMA, "manifest_id": manifest_id, "entries": manifest_entries},
        blobs=blobs,
    )


def load_output_manifest(
    value: object,
    *,
    artifact_loader: ArtifactLoader,
    maximum_entries: int,
    maximum_total_bytes: int,
) -> tuple[LoadedArtifact, ...]:
    manifest = strict_object(
        value,
        required=frozenset({"schema", "manifest_id", "entries"}),
        label="scientific artifact manifest",
    )
    if manifest["schema"] != ARTIFACT_MANIFEST_SCHEMA:
        raise ScientificAdapterError("output uses a non-canonical artifact manifest schema")
    manifest_id = manifest["manifest_id"]
    if not isinstance(manifest_id, str) or _ARTIFACT_ID.fullmatch(manifest_id) is None:
        raise ScientificAdapterError("output manifest_id is invalid")
    entries = manifest["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= maximum_entries:
        raise ScientificAdapterError("output manifest entry count is outside the adapter bound")
    result: list[LoadedArtifact] = []
    names: set[str] = set()
    artifact_ids: set[str] = set()
    total_bytes = 0
    for index, raw in enumerate(entries):
        entry = strict_object(
            raw,
            required=frozenset({"name", "semantic_type", "artifact"}),
            label=f"output entries[{index}]",
        )
        name = entry["name"]
        semantic_type = entry["semantic_type"]
        if not isinstance(name, str) or _ARTIFACT_ID.fullmatch(name) is None:
            raise ScientificAdapterError("output entry name is invalid")
        if not isinstance(semantic_type, str) or _SEMANTIC_TYPE.fullmatch(semantic_type) is None:
            raise ScientificAdapterError("output semantic_type is invalid")
        pointer = ArtifactPointer.parse(
            entry["artifact"], label=f"output entries[{index}].artifact", maximum_bytes=maximum_total_bytes
        )
        if name in names or pointer.artifact_id in artifact_ids:
            raise ScientificAdapterError("output entry names and artifact IDs must be unique")
        names.add(name)
        artifact_ids.add(pointer.artifact_id)
        total_bytes += pointer.size_bytes
        if total_bytes > maximum_total_bytes:
            raise ScientificAdapterError("output artifacts exceed the adapter byte bound")
        content = artifact_loader(pointer.artifact_id)
        if not isinstance(content, bytes):
            raise ScientificAdapterError("artifact loader must return bytes")
        if len(content) != pointer.size_bytes or hashlib.sha256(content).hexdigest() != pointer.sha256:
            raise ScientificAdapterError("resolved artifact bytes do not match their public pointer")
        result.append(LoadedArtifact(name, semantic_type, pointer, content))
    return tuple(result)


def parse_json_artifact(content: bytes, *, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(
            content,
            object_pairs_hook=lambda pairs: _pairs_without_duplicates(pairs, label),
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ScientificAdapterError(f"{label} contains non-finite number {constant}")
            ),
        )
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ScientificAdapterError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise ScientificAdapterError(f"{label} must contain one object")
    return cast(Mapping[str, object], value)


def _pairs_without_duplicates(pairs: list[tuple[str, object]], label: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ScientificAdapterError(f"{label} contains duplicate field {key}")
        result[key] = value
    return result


def finite_number(value: object, *, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ScientificAdapterError(f"{label} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ScientificAdapterError(f"{label} must be finite in [{minimum}, {maximum}]")
    return parsed


def protein_sequence(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not 4 <= len(value) <= 2048 or not set(value) <= set("ACDEFGHIKLMNPQRSTVWY"):
        raise ScientificAdapterError(f"{label} must be a bounded canonical amino-acid sequence")
    return value


def structure_atom_count(artifact: LoadedArtifact, *, require_two_chains: bool) -> int:
    try:
        text = artifact.content.decode("ascii")
    except UnicodeDecodeError as error:
        raise ScientificAdapterError(f"{artifact.name} is not an ASCII structure") from error
    atoms = 0
    chains: set[str] = set()
    if artifact.pointer.media_type == "chemical/x-pdb":
        for line in text.splitlines():
            if not line.startswith(("ATOM  ", "HETATM")):
                continue
            if len(line) < 54:
                raise ScientificAdapterError(f"{artifact.name} has a short PDB atom record")
            try:
                coordinates = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
            except ValueError as error:
                raise ScientificAdapterError(f"{artifact.name} has invalid PDB coordinates") from error
            if not all(math.isfinite(value) for value in coordinates):
                raise ScientificAdapterError(f"{artifact.name} has non-finite PDB coordinates")
            chains.add(line[21:22].strip())
            atoms += 1
    elif artifact.pointer.media_type == "chemical/x-mmcif":
        atoms, chains = _mmcif_atom_summary(text, label=artifact.name)
    else:
        raise ScientificAdapterError(f"{artifact.name} has unsupported structure media type")
    if atoms < 4 or (require_two_chains and len(chains - {""}) < 2):
        raise ScientificAdapterError(f"{artifact.name} is a degenerate structure")
    return atoms


def _mmcif_atom_summary(text: str, *, label: str) -> tuple[int, set[str]]:
    """Parse the bounded atom-site loop needed for structural sanity checks.

    This deliberately rejects uncommon multiline atom rows instead of guessing
    column positions. The model outputs standard one-row-per-atom mmCIF.
    """

    lines = text.splitlines()
    for loop_index, raw_line in enumerate(lines):
        if raw_line.strip().lower() != "loop_":
            continue
        headers: list[str] = []
        row_index = loop_index + 1
        while row_index < len(lines) and lines[row_index].lstrip().startswith("_atom_site."):
            header = lines[row_index].strip().split(maxsplit=1)[0]
            headers.append(header)
            row_index += 1
        required = {
            "_atom_site.group_PDB",
            "_atom_site.Cartn_x",
            "_atom_site.Cartn_y",
            "_atom_site.Cartn_z",
        }
        if not required.issubset(headers):
            continue
        chain_header = next(
            (
                candidate
                for candidate in ("_atom_site.auth_asym_id", "_atom_site.label_asym_id")
                if candidate in headers
            ),
            None,
        )
        if chain_header is None:
            raise ScientificAdapterError(f"{label} mmCIF atom loop has no chain identity")
        column = {header: index for index, header in enumerate(headers)}
        atoms = 0
        chains: set[str] = set()
        while row_index < len(lines):
            line = lines[row_index].strip()
            row_index += 1
            if not line or line.startswith("#"):
                if atoms:
                    break
                continue
            if line == "loop_" or line.startswith("_") or line.lower().startswith("data_"):
                break
            try:
                tokens = shlex.split(line, comments=False, posix=True)
            except ValueError as error:
                raise ScientificAdapterError(f"{label} has an invalid mmCIF atom row") from error
            if len(tokens) != len(headers):
                raise ScientificAdapterError(f"{label} has a wrapped or incomplete mmCIF atom row")
            if tokens[column["_atom_site.group_PDB"]].upper() not in {"ATOM", "HETATM"}:
                continue
            try:
                coordinates = tuple(
                    float(tokens[column[header]])
                    for header in ("_atom_site.Cartn_x", "_atom_site.Cartn_y", "_atom_site.Cartn_z")
                )
            except ValueError as error:
                raise ScientificAdapterError(f"{label} has invalid mmCIF coordinates") from error
            if not all(math.isfinite(value) for value in coordinates):
                raise ScientificAdapterError(f"{label} has non-finite mmCIF coordinates")
            chain = tokens[column[chain_header]]
            if chain in {".", "?"}:
                raise ScientificAdapterError(f"{label} has an unresolved mmCIF chain identity")
            chains.add(chain)
            atoms += 1
        if atoms:
            return atoms, chains
    raise ScientificAdapterError(f"{label} has no usable mmCIF atom-site loop")

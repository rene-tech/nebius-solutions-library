"""Digest-bound localization of compressed runtime artifacts into usable trees.

A scientific runtime consumes a *directory*. Upstream publishes an *archive*.
This module keeps those two identities separate and independently verifiable:

* ``ArchiveProvenance`` records where bytes came from (filename, size, SHA-256,
  source URI, upstream revision, license). It never qualifies a runtime mount.
* ``TreeIdentity`` records what the runtime will actually read, as a digest
  computed from the localized filesystem. It is reproducible from the mount
  alone, with no reference to the archive that produced it.

``verify_localized_tree`` is the adapter preflight. It fails closed when a mount
still holds its source archive, when the tree is partial, when it is a different
tree, or when its identity digest does not match the contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import zipfile
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast

from .primitives import (
    ArtifactLocalizationError,
    ScientificAdapterError,
    TreeBoundExceededError,
    strict_object,
)

LOCALIZATION_SCHEMA = "fs2-serve.nebius.ai/scientific-artifact-localization/v1"
RECEIPT_SCHEMA = "fs2-serve.nebius.ai/scientific-localization-receipt/v1"
TREE_INVENTORY_ALGORITHM = "fs2-flat-tree-inventory/v1"

RUNTIME_ARTIFACT_ROOT = PurePosixPath("/opt/fs2/artifacts")

MAX_TREE_ENTRIES = 1_048_576
MAX_TREE_BYTES = 1_099_511_627_776
MAX_CONTRACT_BYTES = 4 * 1024 * 1024
_READ_CHUNK = 4 * 1024 * 1024
# Verification scans a little past the contracted size so one unexpected entry
# can be named precisely instead of surfacing as an opaque scan limit. The bound
# still stops a mount that has been filled with arbitrary content.
_SCAN_HEADROOM_BYTES = 1024 * 1024

_ARTIFACT_ID = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_ENTRY_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,254}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BINDING_NAME = re.compile(r"^(?:--[a-z][a-z0-9-]{0,62}|[A-Z][A-Z0-9_]{1,63})$")
_ABSOLUTE_PATH = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
_PACKAGE_PATH = re.compile(r"^[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*$")

_TRANSFORMS = frozenset({"safe-extract-zip", "safe-extract-tar", "safe-extract-tar-gz"})
_MEDIA_TYPES = {
    "safe-extract-zip": "application/zip",
    "safe-extract-tar": "application/x-tar",
    "safe-extract-tar-gz": "application/gzip",
}
_MEMBER_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*/$")

EXTERNAL_MANIFEST_SCHEMA = "fs2.nebius.ai/external-model-artifact-manifest/v1"
EXTERNAL_MANIFEST_GENERATOR = "external-model-artifact-manifest/v1"
_GENERATORS = frozenset({EXTERNAL_MANIFEST_GENERATOR})


# ---------------------------------------------------------------------------
# Canonical tree inventory
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One localized regular file, identified without its host path."""

    path: str
    size_bytes: int
    crc32: int

    def __post_init__(self) -> None:
        if _ENTRY_NAME.fullmatch(self.path) is None:
            raise ArtifactLocalizationError("localized tree entries must be safe flat-root names")
        if self.size_bytes < 0 or not 0 <= self.crc32 <= 0xFFFFFFFF:
            raise ArtifactLocalizationError("localized tree entry size or CRC-32 is invalid")


def tree_inventory_bytes(entries: Iterable[TreeEntry]) -> bytes:
    """Serialize a flat tree into its canonical, host-independent inventory.

    ``fs2-flat-tree-inventory/v1`` is a path-sorted JSON array of
    ``{"bytes", "crc32", "path"}`` objects with sorted keys, no whitespace, and
    one trailing newline. It contains no host path, mount root, timestamp,
    ownership, or permission, so the same tree hashes identically wherever it is
    localized.
    """

    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.path in seen:
            raise ArtifactLocalizationError("localized tree contains a duplicate entry path")
        seen.add(entry.path)
        rows.append({"bytes": entry.size_bytes, "crc32": f"{entry.crc32:08x}", "path": entry.path})
    rows.sort(key=lambda row: cast(str, row["path"]))
    return (json.dumps(rows, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def tree_inventory_sha256(entries: Iterable[TreeEntry]) -> str:
    return hashlib.sha256(tree_inventory_bytes(entries)).hexdigest()


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArchiveProvenance:
    """Immutable upstream identity of the compressed object."""

    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    source_uri: str
    source_revision: str
    license_id: str
    member_prefix: str | None = None

    @classmethod
    def parse(cls, value: object) -> ArchiveProvenance:
        item = strict_object(
            value,
            required=frozenset(
                {"filename", "media_type", "bytes", "sha256", "source_uri", "source_revision", "license_id"}
            ),
            optional=frozenset({"verified_at", "member_prefix"}),
            label="localization archive",
        )
        filename = item["filename"]
        if not isinstance(filename, str) or _ENTRY_NAME.fullmatch(filename) is None:
            raise ArtifactLocalizationError("archive filename must be a safe flat name")
        media_type = item["media_type"]
        if media_type not in set(_MEDIA_TYPES.values()):
            raise ArtifactLocalizationError("archive media type is unsupported")
        size_bytes = item["bytes"]
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or not 1 <= size_bytes <= MAX_TREE_BYTES:
            raise ArtifactLocalizationError("archive byte size is outside the bound")
        digest = item["sha256"]
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ArtifactLocalizationError("archive sha256 must be a lowercase SHA-256")
        strings = {}
        for key, maximum in (("source_uri", 1024), ("source_revision", 128), ("license_id", 64)):
            raw = item[key]
            if not isinstance(raw, str) or not 1 <= len(raw) <= maximum:
                raise ArtifactLocalizationError(f"archive {key} is invalid")
            strings[key] = raw
        member_prefix = item.get("member_prefix")
        if member_prefix is not None and (
            not isinstance(member_prefix, str)
            or len(member_prefix) > 256
            or _MEMBER_PREFIX.fullmatch(member_prefix) is None
        ):
            raise ArtifactLocalizationError("archive member_prefix must be a safe relative directory prefix")
        return cls(
            filename=filename,
            media_type=cast(str, media_type),
            size_bytes=size_bytes,
            sha256=digest,
            source_uri=strings["source_uri"],
            source_revision=strings["source_revision"],
            license_id=strings["license_id"],
            member_prefix=member_prefix,
        )

    def to_receipt(self, *, present_in_mount: bool) -> dict[str, object]:
        return {
            "filename": self.filename,
            "bytes": self.size_bytes,
            "sha256": self.sha256,
            "source_uri": self.source_uri,
            "source_revision": self.source_revision,
            "license_id": self.license_id,
            "present_in_mount": present_in_mount,
        }


@dataclass(frozen=True, slots=True)
class GeneratedEntry:
    """A tree entry the stager writes rather than lifting from the archive.

    A runtime can require a file upstream does not ship. The BindCraft image
    admits its AlphaFold2 mount only through an
    ``external-model-artifact-manifest/v1`` document, which upstream has no
    reason to publish. Declaring it here keeps it as verifiable as any other
    entry: the generator is named, its inputs are pinned, and the bytes it must
    produce are bound by their own digest, so a drifting generator fails closed
    instead of quietly changing what the runtime admits.
    """

    path: str
    size_bytes: int
    sha256: str
    generator: str
    generator_inputs: Mapping[str, str]

    @classmethod
    def parse(cls, value: object, *, index: int) -> GeneratedEntry:
        item = strict_object(
            value,
            required=frozenset({"path", "bytes", "sha256", "generator", "generator_inputs"}),
            label=f"tree generated_entries[{index}]",
        )
        path = item["path"]
        size_bytes = item["bytes"]
        digest = item["sha256"]
        generator = item["generator"]
        if (
            not isinstance(path, str)
            or _ENTRY_NAME.fullmatch(path) is None
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or not 1 <= size_bytes <= 16 * 1024 * 1024
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise ArtifactLocalizationError("tree generated entry is invalid")
        if generator not in _GENERATORS:
            raise ArtifactLocalizationError(f"tree generated entry generator {generator!r} is unsupported")
        raw_inputs = item["generator_inputs"]
        if not isinstance(raw_inputs, Mapping) or not all(
            isinstance(key, str) and isinstance(inner, str) and inner for key, inner in raw_inputs.items()
        ):
            raise ArtifactLocalizationError("tree generated entry inputs must be a string mapping")
        inputs = {str(key): str(inner) for key, inner in raw_inputs.items()}
        if generator == EXTERNAL_MANIFEST_GENERATOR and set(inputs) != {"artifact_kind", "source_revision"}:
            raise ArtifactLocalizationError(
                "an external model artifact manifest needs exactly artifact_kind and source_revision"
            )
        return cls(path, size_bytes, digest, cast(str, generator), inputs)


@dataclass(frozen=True, slots=True)
class ProbeEntry:
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class TreeIdentity:
    """Identity of the extracted directory the runtime mounts."""

    mount_paths: tuple[str, ...]
    entry_count: int
    total_bytes: int
    entry_path_pattern: str
    inventory_sha256: str
    probe_entries: tuple[ProbeEntry, ...]
    complete_entry_digests: bool
    generated_entries: tuple[GeneratedEntry, ...] = ()

    @classmethod
    def parse(cls, value: object) -> TreeIdentity:
        item = strict_object(
            value,
            required=frozenset(
                {
                    "mount_paths",
                    "entry_count",
                    "total_bytes",
                    "entry_path_pattern",
                    "inventory_algorithm",
                    "inventory_sha256",
                    "probe_entries",
                }
            ),
            optional=frozenset({"complete_entry_digests", "generated_entries"}),
            label="localization tree",
        )
        if item["inventory_algorithm"] != TREE_INVENTORY_ALGORITHM:
            raise ArtifactLocalizationError("tree inventory algorithm is unsupported")
        raw_mounts = item["mount_paths"]
        if not isinstance(raw_mounts, list) or not 1 <= len(raw_mounts) <= 8:
            raise ArtifactLocalizationError("tree mount_paths must contain 1..8 absolute paths")
        for candidate in raw_mounts:
            if not isinstance(candidate, str) or len(candidate) > 256 or _ABSOLUTE_PATH.fullmatch(candidate) is None:
                raise ArtifactLocalizationError("tree mount_paths must be safe absolute POSIX paths")
        mount_paths = tuple(cast(Sequence[str], raw_mounts))
        if len(set(mount_paths)) != len(mount_paths):
            raise ArtifactLocalizationError("tree mount_paths must be unique")
        entry_count = item["entry_count"]
        total_bytes = item["total_bytes"]
        if (
            isinstance(entry_count, bool)
            or not isinstance(entry_count, int)
            or not 1 <= entry_count <= MAX_TREE_ENTRIES
            or isinstance(total_bytes, bool)
            or not isinstance(total_bytes, int)
            or not 1 <= total_bytes <= MAX_TREE_BYTES
        ):
            raise ArtifactLocalizationError("tree entry count or total byte size is outside the bound")
        pattern = item["entry_path_pattern"]
        if not isinstance(pattern, str) or not 3 <= len(pattern) <= 256:
            raise ArtifactLocalizationError("tree entry_path_pattern is invalid")
        try:
            re.compile(pattern)
        except re.error as error:
            raise ArtifactLocalizationError("tree entry_path_pattern is not a valid regular expression") from error
        digest = item["inventory_sha256"]
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ArtifactLocalizationError("tree inventory_sha256 must be a lowercase SHA-256")
        raw_probes = item["probe_entries"]
        if not isinstance(raw_probes, list) or not 1 <= len(raw_probes) <= 64:
            raise ArtifactLocalizationError("tree probe_entries must contain 1..64 items")
        probes: list[ProbeEntry] = []
        for index, raw in enumerate(raw_probes):
            probe = strict_object(
                raw, required=frozenset({"path", "bytes", "sha256"}), label=f"tree probe_entries[{index}]"
            )
            probe_path = probe["path"]
            probe_bytes = probe["bytes"]
            probe_digest = probe["sha256"]
            if (
                not isinstance(probe_path, str)
                or _ENTRY_NAME.fullmatch(probe_path) is None
                or isinstance(probe_bytes, bool)
                or not isinstance(probe_bytes, int)
                or probe_bytes < 0
                or not isinstance(probe_digest, str)
                or _SHA256.fullmatch(probe_digest) is None
            ):
                raise ArtifactLocalizationError("tree probe entry is invalid")
            probes.append(ProbeEntry(probe_path, probe_bytes, probe_digest))
        if len({probe.path for probe in probes}) != len(probes):
            raise ArtifactLocalizationError("tree probe entry paths must be unique")
        if len(probes) > entry_count:
            raise ArtifactLocalizationError("tree probe entries cannot exceed the declared entry count")
        raw_generated = item.get("generated_entries", [])
        if not isinstance(raw_generated, list) or len(raw_generated) > 8:
            raise ArtifactLocalizationError("tree generated_entries must contain at most 8 items")
        generated = tuple(GeneratedEntry.parse(raw, index=index) for index, raw in enumerate(raw_generated))
        if len({entry.path for entry in generated}) != len(generated):
            raise ArtifactLocalizationError("tree generated entry paths must be unique")
        complete = item.get("complete_entry_digests", False)
        if not isinstance(complete, bool):
            raise ArtifactLocalizationError("tree complete_entry_digests must be a boolean")
        if complete and len(probes) != entry_count:
            raise ArtifactLocalizationError("a complete-digest tree must bind every entry")
        return cls(
            mount_paths=mount_paths,
            entry_count=entry_count,
            total_bytes=total_bytes,
            entry_path_pattern=pattern,
            inventory_sha256=digest,
            probe_entries=tuple(sorted(probes, key=lambda probe: probe.path)),
            complete_entry_digests=complete,
            generated_entries=tuple(sorted(generated, key=lambda entry: entry.path)),
        )

    @property
    def entry_matcher(self) -> re.Pattern[str]:
        return re.compile(self.entry_path_pattern)

    @property
    def canonical_mount_path(self) -> str:
        """The first declared mount path, used when a receipt names just one."""

        return self.mount_paths[0]


@dataclass(frozen=True, slots=True)
class RuntimeBinding:
    """How one model receives the localized tree path."""

    model_id: str
    binding_kind: str
    binding_name: str
    mount_path: str
    stages: tuple[str, ...]

    @classmethod
    def parse(cls, value: object) -> RuntimeBinding:
        item = strict_object(
            value,
            required=frozenset({"model_id", "binding_kind", "binding_name", "mount_path"}),
            optional=frozenset({"stages"}),
            label="localization consumer",
        )
        model_id = item["model_id"]
        if not isinstance(model_id, str) or _ARTIFACT_ID.fullmatch(model_id) is None:
            raise ArtifactLocalizationError("localization consumer model_id is invalid")
        kind = item["binding_kind"]
        if kind not in {"argv-option", "environment-variable", "installed-package-path"}:
            raise ArtifactLocalizationError("localization consumer binding_kind is unsupported")
        name = item["binding_name"]
        if kind == "installed-package-path":
            # The model imports the tree from its own installed location, so the
            # binding is the dotted package path rather than a flag or variable.
            if not isinstance(name, str) or _PACKAGE_PATH.fullmatch(name) is None:
                raise ArtifactLocalizationError("installed-package binding_name must be a dotted package path")
        elif not isinstance(name, str) or _BINDING_NAME.fullmatch(name) is None:
            raise ArtifactLocalizationError("localization consumer binding_name is invalid")
        elif (kind == "argv-option") != name.startswith("--"):
            raise ArtifactLocalizationError("localization consumer binding_kind does not match binding_name")
        mount_path = item["mount_path"]
        if not isinstance(mount_path, str) or _ABSOLUTE_PATH.fullmatch(mount_path) is None:
            raise ArtifactLocalizationError("localization consumer mount_path must be a safe absolute POSIX path")
        raw_stages = item.get("stages", ())
        if not isinstance(raw_stages, list | tuple) or not all(
            isinstance(stage, str) and _ARTIFACT_ID.fullmatch(stage) is not None for stage in raw_stages
        ):
            raise ArtifactLocalizationError("localization consumer stages are invalid")
        stages = tuple(cast(Sequence[str], raw_stages))
        if len(set(stages)) != len(stages):
            raise ArtifactLocalizationError("localization consumer stages must be unique")
        return cls(model_id, cast(str, kind), name, mount_path, stages)


@dataclass(frozen=True, slots=True)
class LocalizationContract:
    """One archive bound to the exact extracted tree a runtime consumes."""

    artifact_id: str
    transform: str
    archive: ArchiveProvenance
    tree: TreeIdentity
    consumers: tuple[RuntimeBinding, ...]

    def __post_init__(self) -> None:
        if _ARTIFACT_ID.fullmatch(self.artifact_id) is None:
            raise ArtifactLocalizationError("localization artifact_id is invalid")
        if self.transform not in _TRANSFORMS:
            raise ArtifactLocalizationError("localization transform is unsupported")
        if self.archive.media_type != _MEDIA_TYPES[self.transform]:
            raise ArtifactLocalizationError("localization transform does not match the archive media type")
        for candidate in self.tree.mount_paths:
            if ".." in PurePosixPath(candidate).parts:
                raise ArtifactLocalizationError("localized tree mount paths must not traverse")
        runtime_root_mounts = [
            candidate for candidate in self.tree.mount_paths if PurePosixPath(candidate).parent == RUNTIME_ARTIFACT_ROOT
        ]
        if runtime_root_mounts and any(
            PurePosixPath(candidate).name != self.artifact_id for candidate in runtime_root_mounts
        ):
            raise ArtifactLocalizationError(
                "a mount under the runtime artifact root must be this artifact's own directory"
            )
        if self.archive.sha256 == self.tree.inventory_sha256:
            raise ArtifactLocalizationError("archive provenance and extracted-tree identity must be distinct digests")
        if self.tree.entry_matcher.fullmatch(self.archive.filename) is not None:
            raise ArtifactLocalizationError("the source archive must not satisfy the localized tree entry pattern")
        if not self.consumers:
            raise ArtifactLocalizationError("a localization contract requires at least one consumer")
        if len({(item.model_id, item.binding_name) for item in self.consumers}) != len(self.consumers):
            raise ArtifactLocalizationError("localization consumers must be unique per model and binding")
        declared = set(self.tree.mount_paths)
        for consumer in self.consumers:
            if consumer.mount_path not in declared:
                raise ArtifactLocalizationError(
                    f"{consumer.model_id} reads {consumer.mount_path}, which the tree does not declare"
                )
        unused = declared - {consumer.mount_path for consumer in self.consumers}
        if unused:
            raise ArtifactLocalizationError(f"localized tree declares mount paths no consumer reads: {sorted(unused)}")

    @classmethod
    def parse(cls, value: object) -> LocalizationContract:
        item = strict_object(
            value,
            required=frozenset({"artifact_id", "transform", "archive", "tree", "consumers"}),
            optional=frozenset({"notes"}),
            label="localization artifact",
        )
        raw_consumers = item["consumers"]
        if not isinstance(raw_consumers, list) or not 1 <= len(raw_consumers) <= 16:
            raise ArtifactLocalizationError("localization consumers must contain 1..16 items")
        artifact_id = item["artifact_id"]
        transform = item["transform"]
        if not isinstance(artifact_id, str) or not isinstance(transform, str):
            raise ArtifactLocalizationError("localization artifact identity is invalid")
        return cls(
            artifact_id=artifact_id,
            transform=transform,
            archive=ArchiveProvenance.parse(item["archive"]),
            tree=TreeIdentity.parse(item["tree"]),
            consumers=tuple(RuntimeBinding.parse(raw) for raw in raw_consumers),
        )

    def binding_for(self, model_id: str) -> RuntimeBinding:
        matches = [item for item in self.consumers if item.model_id == model_id]
        if len(matches) != 1:
            raise ArtifactLocalizationError(f"{self.artifact_id} has no unique binding for {model_id}")
        return matches[0]


def load_localization_contracts(value: object) -> dict[str, LocalizationContract]:
    """Parse the canonical contract document into artifact-keyed contracts."""

    root = strict_object(
        value,
        required=frozenset({"schema", "generated_at", "artifacts"}),
        label="scientific artifact localization contract",
    )
    if root["schema"] != LOCALIZATION_SCHEMA:
        raise ArtifactLocalizationError("scientific artifact localization schema is invalid")
    raw = root["artifacts"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= 64:
        raise ArtifactLocalizationError("localization contract must declare 1..64 artifacts")
    contracts: dict[str, LocalizationContract] = {}
    for item in raw:
        contract = LocalizationContract.parse(item)
        if contract.artifact_id in contracts:
            raise ArtifactLocalizationError("localization contract declares a duplicate artifact")
        contracts[contract.artifact_id] = contract
    return contracts


def load_localization_contracts_from_path(path: Path) -> dict[str, LocalizationContract]:
    payload = path.read_bytes()
    if len(payload) > MAX_CONTRACT_BYTES:
        raise ArtifactLocalizationError("localization contract exceeds its byte bound")
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ArtifactLocalizationError("localization contract is not valid UTF-8 JSON") from error
    return load_localization_contracts(value)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocalizationReceipt:
    """Non-secret evidence about one runtime mount.

    ``archive_sha256`` and ``tree_inventory_sha256`` are deliberately separate
    fields: the first says where the bytes came from, the second says what the
    runtime will read. A caller must never substitute one for the other.

    Only a verified receipt asserts a tree identity. On a rejected receipt the
    tree fields carry what was observed where verification reached them, and
    zeros where it did not, so a rejection can be diagnosed without ever being
    mistaken for a qualification.
    """

    artifact_id: str
    mount_path: str
    state: str
    observed_at: datetime
    archive: ArchiveProvenance
    archive_present_in_mount: bool
    entry_count: int
    total_bytes: int
    tree_inventory_sha256: str
    probe_entries_verified: int
    runtime_bindings: tuple[tuple[str, str, str, str], ...] = ()
    rejection_reason: str | None = None
    observation: Mapping[str, object] | None = None

    @property
    def archive_sha256(self) -> str:
        return self.archive.sha256

    @property
    def verified(self) -> bool:
        return self.state == "verified"

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": RECEIPT_SCHEMA,
            "artifact_id": self.artifact_id,
            "observed_at": self.observed_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mount_path": self.mount_path,
            "state": self.state,
            "archive_provenance": self.archive.to_receipt(present_in_mount=self.archive_present_in_mount),
            "tree_identity": {
                "entry_count": self.entry_count,
                "total_bytes": self.total_bytes,
                "inventory_algorithm": TREE_INVENTORY_ALGORITHM,
                "inventory_sha256": self.tree_inventory_sha256,
                "probe_entries_verified": self.probe_entries_verified,
            },
        }
        if self.runtime_bindings:
            value["runtime_bindings"] = [
                {
                    "model_id": model_id,
                    "binding_kind": kind,
                    "binding_name": name,
                    "binding_value": binding_value,
                }
                for model_id, kind, name, binding_value in self.runtime_bindings
            ]
        if self.rejection_reason is not None:
            value["rejection_reason"] = self.rejection_reason
        if self.observation is not None:
            value["observation"] = dict(self.observation)
        return value


def _real_directory(root: Path, label: str) -> Path:
    if root.is_symlink():
        raise ArtifactLocalizationError(f"{label} must not be a symbolic link")
    try:
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise ArtifactLocalizationError(f"{label} does not exist") from error
    if not resolved.is_dir():
        raise ArtifactLocalizationError(f"{label} is not a directory")
    return resolved


def scan_localized_tree(
    root: Path,
    *,
    maximum_entries: int,
    maximum_bytes: int,
) -> tuple[TreeEntry, ...]:
    """Read a flat runtime mount, rejecting anything that is not a plain file.

    Directories, symbolic links, device nodes, and unsafe names all fail closed
    rather than being skipped, so a tampered mount can never hash as a clean one.
    """

    resolved = _real_directory(root, "localized tree root")
    entries: list[TreeEntry] = []
    total = 0
    with os.scandir(resolved) as scan:
        for item in scan:
            if len(entries) >= maximum_entries:
                raise TreeBoundExceededError("localized tree exceeds its declared entry bound")
            if item.is_symlink():
                raise ArtifactLocalizationError("localized tree contains a symbolic link")
            if item.is_dir(follow_symlinks=False):
                raise ArtifactLocalizationError("localized tree must be flat and contains a directory")
            if not item.is_file(follow_symlinks=False):
                raise ArtifactLocalizationError("localized tree contains a non-regular entry")
            if _ENTRY_NAME.fullmatch(item.name) is None:
                raise ArtifactLocalizationError("localized tree contains an unsafe entry name")
            size = 0
            crc = 0
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(item.path, flags)
            try:
                with os.fdopen(descriptor, "rb", closefd=True) as handle:
                    while True:
                        chunk = handle.read(_READ_CHUNK)
                        if not chunk:
                            break
                        size += len(chunk)
                        crc = zlib.crc32(chunk, crc)
                        total += len(chunk)
                        if total > maximum_bytes:
                            raise TreeBoundExceededError("localized tree exceeds its declared byte bound")
            except OSError as error:
                raise ArtifactLocalizationError("localized tree entry could not be read safely") from error
            entries.append(TreeEntry(item.name, size, crc & 0xFFFFFFFF))
    return tuple(sorted(entries, key=lambda entry: entry.path))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _rejected(
    contract: LocalizationContract,
    reason: str,
    *,
    now: datetime,
    archive_present: bool = False,
    entry_count: int = 0,
    total_bytes: int = 0,
    inventory: str = "0" * 64,
    observation: Mapping[str, object] | None = None,
) -> LocalizationReceipt:
    return LocalizationReceipt(
        artifact_id=contract.artifact_id,
        mount_path=contract.tree.canonical_mount_path,
        state="rejected",
        observed_at=now,
        archive=contract.archive,
        archive_present_in_mount=archive_present,
        entry_count=entry_count,
        total_bytes=total_bytes,
        tree_inventory_sha256=inventory,
        probe_entries_verified=0,
        rejection_reason=reason,
        observation=observation,
    )


def verify_localized_tree(
    root: Path,
    contract: LocalizationContract,
    *,
    now: datetime | None = None,
    verify_probes: bool = True,
    observation: Mapping[str, object] | None = None,
) -> LocalizationReceipt:
    """Adapter preflight: prove a mount is the contracted extracted tree.

    Returns a rejected receipt instead of raising for every condition a real
    mount can legitimately be in, so a controller can record exactly why a stage
    was refused. Structurally impossible input still raises.
    """

    moment = now or datetime.now(tz=UTC)
    tree = contract.tree
    try:
        resolved = _real_directory(root, "runtime artifact mount")
    except ArtifactLocalizationError as error:
        return _rejected(contract, f"unusable-runtime-mount: {error}", now=moment, observation=observation)

    archive_candidate = resolved / contract.archive.filename
    archive_present = archive_candidate.exists() or archive_candidate.is_symlink()
    if archive_present:
        return _rejected(
            contract,
            "archive-present-in-runtime-mount: the mount holds "
            f"{contract.archive.filename} instead of the extracted tree",
            now=moment,
            archive_present=True,
            observation=observation,
        )

    try:
        # A little headroom so an over-full mount is diagnosed as the wrong tree
        # rather than as a scanning limit, while a mount holding arbitrarily
        # many entries or bytes still stops at the bound.
        entries = scan_localized_tree(
            resolved,
            maximum_entries=tree.entry_count + 1,
            maximum_bytes=tree.total_bytes * 2 + _SCAN_HEADROOM_BYTES,
        )
    except TreeBoundExceededError as error:
        return _rejected(contract, f"unexpected-tree-content: {error}", now=moment, observation=observation)
    except ArtifactLocalizationError as error:
        return _rejected(contract, f"unsafe-tree-entry: {error}", now=moment, observation=observation)

    matcher = tree.entry_matcher
    offending = [entry.path for entry in entries if matcher.fullmatch(entry.path) is None]
    if offending:
        return _rejected(
            contract,
            f"entry-path-pattern-violation: {len(offending)} entries including {offending[0]}",
            now=moment,
            entry_count=len(entries),
            observation=observation,
        )

    observed_bytes = sum(entry.size_bytes for entry in entries)
    if len(entries) < tree.entry_count:
        return _rejected(
            contract,
            f"partial-tree: {len(entries)} of {tree.entry_count} entries are present",
            now=moment,
            entry_count=len(entries),
            total_bytes=observed_bytes,
            observation=observation,
        )
    if len(entries) > tree.entry_count or observed_bytes != tree.total_bytes:
        return _rejected(
            contract,
            f"unexpected-tree-content: {len(entries)} entries and {observed_bytes} bytes "
            f"do not match the contracted {tree.entry_count} entries and {tree.total_bytes} bytes",
            now=moment,
            entry_count=len(entries),
            total_bytes=observed_bytes,
            observation=observation,
        )

    inventory = tree_inventory_sha256(entries)
    if inventory != tree.inventory_sha256:
        return _rejected(
            contract,
            "tree-identity-mismatch: localized inventory digest does not match the contract",
            now=moment,
            entry_count=len(entries),
            total_bytes=observed_bytes,
            inventory=inventory,
            observation=observation,
        )

    verified_probes = 0
    if verify_probes:
        for probe in tree.probe_entries:
            candidate = resolved / probe.path
            if not candidate.is_file() or candidate.is_symlink() or candidate.stat().st_size != probe.size_bytes:
                return _rejected(
                    contract,
                    f"probe-entry-missing: {probe.path}",
                    now=moment,
                    entry_count=len(entries),
                    total_bytes=observed_bytes,
                    inventory=inventory,
                    observation=observation,
                )
            if _file_sha256(candidate) != probe.sha256:
                return _rejected(
                    contract,
                    f"probe-entry-digest-mismatch: {probe.path}",
                    now=moment,
                    entry_count=len(entries),
                    total_bytes=observed_bytes,
                    inventory=inventory,
                    observation=observation,
                )
            verified_probes += 1

    return LocalizationReceipt(
        artifact_id=contract.artifact_id,
        mount_path=contract.tree.canonical_mount_path,
        state="verified",
        observed_at=moment,
        archive=contract.archive,
        archive_present_in_mount=False,
        entry_count=len(entries),
        total_bytes=observed_bytes,
        tree_inventory_sha256=inventory,
        probe_entries_verified=verified_probes,
        runtime_bindings=tuple(
            (item.model_id, item.binding_kind, item.binding_name, item.mount_path) for item in contract.consumers
        ),
        observation=observation,
    )


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def fetch_archive(destination: Path, contract: LocalizationContract, *, timeout_seconds: float = 900.0) -> Path:
    """Download the contracted archive and prove its digest before anything reads it.

    Only the URI the contract declares is fetched, so a staging job cannot be
    pointed at a different object by its arguments.
    """

    import urllib.request

    if not contract.archive.source_uri.startswith("https://"):
        raise ArtifactLocalizationError("archive source_uri must be https")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    # A partial or wrong download must never be left behind for a later step to
    # pick up as if it were the contracted archive.
    destination.unlink(missing_ok=True)
    request = urllib.request.Request(  # noqa: S310 - scheme is checked above
        contract.archive.source_uri,
        headers={"User-Agent": "fs2-artifact-localization/1"},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        with destination.open("wb") as handle:
            while True:
                chunk = response.read(_READ_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > contract.archive.size_bytes:
                    raise ArtifactLocalizationError("archive download exceeded its contracted byte size")
                digest.update(chunk)
                handle.write(chunk)
    if written != contract.archive.size_bytes or digest.hexdigest() != contract.archive.sha256:
        destination.unlink(missing_ok=True)
        raise ArtifactLocalizationError("downloaded archive does not match its declared provenance")
    return destination


def verify_archive(path: Path, contract: LocalizationContract) -> None:
    """Fail closed before extracting anything from an unexpected archive."""

    if path.is_symlink() or not path.is_file():
        raise ArtifactLocalizationError("source archive must be a regular file")
    size = path.stat().st_size
    if size != contract.archive.size_bytes:
        raise ArtifactLocalizationError(
            f"source archive is {size} bytes and the contract requires {contract.archive.size_bytes}"
        )
    digest = _file_sha256(path)
    if digest != contract.archive.sha256:
        raise ArtifactLocalizationError("source archive SHA-256 does not match its declared provenance")


def _selected_member_name(name: str, contract: LocalizationContract) -> str | None:
    """Map an archive member onto a tree entry, or None when it is out of scope.

    With ``member_prefix`` set, only that one subtree is localized: the prefix is
    stripped and anything outside it, or nested deeper, is skipped rather than
    written, so an installed-package subtree can be lifted out of a source
    archive without ever materializing the rest of it.
    """

    prefix = contract.archive.member_prefix
    if prefix is None:
        return _safe_entry_name(name, contract)
    if not name.startswith(prefix):
        return None
    remainder = name[len(prefix) :]
    if not remainder or "/" in remainder:
        return None
    return _safe_entry_name(remainder, contract)


def _safe_entry_name(name: str, contract: LocalizationContract) -> str:
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or "/" in name
        or name in {".", ".."}
        or _ENTRY_NAME.fullmatch(name) is None
    ):
        raise ArtifactLocalizationError(f"archive member {name!r} is not a safe flat-root name")
    if contract.tree.entry_matcher.fullmatch(name) is None:
        raise ArtifactLocalizationError(f"archive member {name!r} violates the contracted entry pattern")
    return name


def _open_for_write(destination: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(destination, flags, 0o444)


def _archive_expectation(contract: LocalizationContract) -> tuple[int, int]:
    """How much of the contracted tree the archive itself has to supply."""

    generated = contract.tree.generated_entries
    return (
        contract.tree.entry_count - len(generated),
        contract.tree.total_bytes - sum(entry.size_bytes for entry in generated),
    )


def _extract_zip(archive: Path, destination: Path, contract: LocalizationContract) -> None:
    expected_members, expected_bytes = _archive_expectation(contract)
    with zipfile.ZipFile(archive) as bundle:
        infos = [info for info in bundle.infolist() if not info.is_dir()]
        if contract.archive.member_prefix is None and len(infos) != expected_members:
            raise ArtifactLocalizationError(
                f"archive holds {len(infos)} members and the contract requires {expected_members}"
            )
        seen: set[str] = set()
        total = 0
        for info in infos:
            name = _selected_member_name(info.filename, contract)
            if name is None:
                continue
            if name in seen:
                raise ArtifactLocalizationError("archive contains a duplicate member name")
            seen.add(name)
            total += info.file_size
            if total > expected_bytes:
                raise ArtifactLocalizationError("archive expands beyond its contracted byte bound")
            written = 0
            crc = 0
            handle = _open_for_write(destination / name)
            with bundle.open(info) as source, os.fdopen(handle, "wb") as target:
                while True:
                    chunk = source.read(_READ_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    crc = zlib.crc32(chunk, crc)
                    target.write(chunk)
            if written != info.file_size or (crc & 0xFFFFFFFF) != info.CRC:
                raise ArtifactLocalizationError(f"archive member {name} did not extract intact")
        if len(seen) != expected_members or total != expected_bytes:
            raise ArtifactLocalizationError("archive expanded content does not match the contracted tree")


def _extract_tar(archive: Path, destination: Path, contract: LocalizationContract) -> None:
    expected_members, expected_bytes = _archive_expectation(contract)
    opened = (
        tarfile.open(archive, mode="r:gz")
        if contract.transform == "safe-extract-tar-gz"
        else tarfile.open(archive, mode="r:")
    )
    with opened as bundle:
        members = bundle.getmembers()
        scoped = contract.archive.member_prefix is not None
        if not scoped and len(members) != expected_members:
            raise ArtifactLocalizationError(
                f"archive holds {len(members)} members and the contract requires {expected_members}"
            )
        seen: set[str] = set()
        total = 0
        for member in members:
            if scoped and not member.isfile():
                continue
            if not member.isfile():
                raise ArtifactLocalizationError("archive contains a non-regular member and the tree must be flat")
            name = _selected_member_name(member.name, contract)
            if name is None:
                continue
            if name in seen:
                raise ArtifactLocalizationError("archive contains a duplicate member name")
            seen.add(name)
            total += member.size
            if member.size < 0 or total > expected_bytes:
                raise ArtifactLocalizationError("archive expands beyond its contracted byte bound")
            source = bundle.extractfile(member)
            if source is None:
                raise ArtifactLocalizationError("archive regular member has no payload")
            written = 0
            handle = _open_for_write(destination / name)
            with source, os.fdopen(handle, "wb") as target:
                while True:
                    chunk = source.read(_READ_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    target.write(chunk)
            if written != member.size:
                raise ArtifactLocalizationError(f"archive member {name} did not extract intact")
        if len(seen) != expected_members or total != expected_bytes:
            raise ArtifactLocalizationError("archive expanded content does not match the contracted tree")


def render_external_model_artifact_manifest(
    root: Path,
    *,
    artifact_kind: str,
    source_revision: str,
    exclude: frozenset[str],
) -> bytes:
    """Render the admission manifest a runtime image reads before it starts.

    The shape is fixed by the consuming gate: every entry carries exactly
    ``path``, ``sha256`` and ``size_bytes``, and the document is serialized
    deterministically so the same tree always renders byte-identical bytes.
    """

    files: list[dict[str, object]] = []
    for name in sorted(item.name for item in root.iterdir()):
        if name in exclude:
            continue
        candidate = root / name
        if candidate.is_symlink() or not candidate.is_file():
            raise ArtifactLocalizationError("an artifact manifest can describe only contained regular files")
        size = candidate.stat().st_size
        if size < 1:
            raise ArtifactLocalizationError(f"{name} is empty and the consuming gate rejects zero-length entries")
        files.append({"path": name, "sha256": _file_sha256(candidate), "size_bytes": size})
    if not files:
        raise ArtifactLocalizationError("an artifact manifest cannot describe an empty tree")
    document = {
        "schema": EXTERNAL_MANIFEST_SCHEMA,
        "artifact_kind": artifact_kind,
        "source_revision": source_revision,
        "files": files,
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_generated_entries(destination: Path, contract: LocalizationContract) -> None:
    """Write each declared generated entry and prove its own digest."""

    generated_names = frozenset(entry.path for entry in contract.tree.generated_entries)
    for entry in contract.tree.generated_entries:
        if entry.generator != EXTERNAL_MANIFEST_GENERATOR:  # pragma: no cover - parse restricts this
            raise ArtifactLocalizationError(f"unsupported generator {entry.generator}")
        payload = render_external_model_artifact_manifest(
            destination,
            artifact_kind=entry.generator_inputs["artifact_kind"],
            source_revision=entry.generator_inputs["source_revision"],
            exclude=generated_names,
        )
        if len(payload) != entry.size_bytes or hashlib.sha256(payload).hexdigest() != entry.sha256:
            raise ArtifactLocalizationError(
                f"generated entry {entry.path} does not match its declared identity; "
                "the tree or the generator has drifted"
            )
        handle = _open_for_write(destination / entry.path)
        with os.fdopen(handle, "wb") as target:
            target.write(payload)


def localize_archive(
    archive: Path,
    destination: Path,
    contract: LocalizationContract,
    *,
    now: datetime | None = None,
    observation: Mapping[str, object] | None = None,
) -> LocalizationReceipt:
    """Verify an archive, expand it into an empty tree, then verify the tree.

    The archive is never written into ``destination``: the localized directory
    contains runtime content only, so its identity can never be confused with
    the provenance of the object it came from.
    """

    verify_archive(archive, contract)
    destination.mkdir(parents=True, exist_ok=True)
    resolved = _real_directory(destination, "localization destination")
    if any(resolved.iterdir()):
        raise ArtifactLocalizationError("localization destination must be empty before extraction")
    if resolved == archive.resolve(strict=True).parent:
        raise ArtifactLocalizationError("localization destination must not contain the source archive")
    try:
        if contract.transform == "safe-extract-zip":
            _extract_zip(archive, resolved, contract)
        else:
            _extract_tar(archive, resolved, contract)
        _write_generated_entries(resolved, contract)
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as error:
        if isinstance(error, ArtifactLocalizationError):
            raise
        raise ArtifactLocalizationError("source archive could not be expanded safely") from error
    return verify_localized_tree(resolved, contract, now=now, observation=observation)


__all__ = [
    "ArchiveProvenance",
    "ArtifactLocalizationError",
    "ScientificAdapterError",
    "LOCALIZATION_SCHEMA",
    "LocalizationContract",
    "LocalizationReceipt",
    "ProbeEntry",
    "RECEIPT_SCHEMA",
    "RuntimeBinding",
    "TREE_INVENTORY_ALGORITHM",
    "TreeBoundExceededError",
    "TreeEntry",
    "TreeIdentity",
    "load_localization_contracts",
    "load_localization_contracts_from_path",
    "localize_archive",
    "scan_localized_tree",
    "tree_inventory_bytes",
    "tree_inventory_sha256",
    "main",
    "fetch_archive",
    "verify_archive",
    "verify_localized_tree",
]


# ---------------------------------------------------------------------------
# Staging entry point
# ---------------------------------------------------------------------------


def _cli_observation(value: str | None) -> Mapping[str, object] | None:
    if not value:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ArtifactLocalizationError("observation must be a JSON object")
    allowed = {"cluster_context", "namespace", "node", "region", "duration_seconds"}
    unknown = sorted(set(parsed) - allowed)
    if unknown:
        raise ArtifactLocalizationError(f"observation contains unknown fields {unknown}")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    """Stage or verify one runtime artifact tree and emit its receipt.

    This module has no dependency beyond the standard library and its sibling
    ``primitives`` module so the exact same verification code can run inside a
    model runtime image, not just inside the control plane.
    """

    import argparse
    import time

    parser = argparse.ArgumentParser(prog="fs2-localize", description=__doc__)
    parser.add_argument("mode", choices=("stage", "verify"))
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--mount", required=True, type=Path)
    parser.add_argument("--archive", type=Path, help="local source archive, for stage")
    parser.add_argument(
        "--fetch-archive-to",
        type=Path,
        help="download the contract's declared archive URI here first, then stage from it",
    )
    parser.add_argument("--receipt", type=Path, help="write the receipt JSON here")
    parser.add_argument("--observation", help="JSON object of non-secret cluster observation fields")
    parser.add_argument(
        "--skip-probes",
        action="store_true",
        help="skip the content spot check; the inventory digest is still verified",
    )
    options = parser.parse_args(argv)

    started = time.monotonic()
    contracts = load_localization_contracts_from_path(options.contract)
    contract = contracts.get(options.artifact_id)
    if contract is None:
        raise SystemExit(f"no localization contract is registered for {options.artifact_id}")
    observation = dict(_cli_observation(options.observation) or {})

    try:
        if options.mode == "stage":
            archive = options.archive
            if options.fetch_archive_to is not None:
                archive = fetch_archive(options.fetch_archive_to, contract)
            if archive is None:
                raise SystemExit("stage requires --archive or --fetch-archive-to")
            receipt = localize_archive(archive, options.mount, contract, observation=observation or None)
        else:
            receipt = verify_localized_tree(
                options.mount,
                contract,
                verify_probes=not options.skip_probes,
                observation=observation or None,
            )
    except ArtifactLocalizationError as error:
        print(f"{options.artifact_id}: {error}")
        return 1

    observation["duration_seconds"] = round(time.monotonic() - started, 3)
    document = LocalizationReceipt(
        artifact_id=receipt.artifact_id,
        mount_path=receipt.mount_path,
        state=receipt.state,
        observed_at=receipt.observed_at,
        archive=receipt.archive,
        archive_present_in_mount=receipt.archive_present_in_mount,
        entry_count=receipt.entry_count,
        total_bytes=receipt.total_bytes,
        tree_inventory_sha256=receipt.tree_inventory_sha256,
        probe_entries_verified=receipt.probe_entries_verified,
        runtime_bindings=receipt.runtime_bindings,
        rejection_reason=receipt.rejection_reason,
        observation=observation,
    ).to_dict()
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if options.receipt is not None:
        options.receipt.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if receipt.verified else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a staging entry point
    raise SystemExit(main())

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

import contextlib
import errno
import hashlib
import json
import os
import re
import tarfile
import tempfile
import time
import zipfile
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:  # pragma: no cover - typing only
    import argparse

from .primitives import (
    ArtifactLocalizationError,
    ScientificAdapterError,
    TreeBoundExceededError,
    strict_object,
)

LOCALIZATION_SCHEMA = "fs2-serve.nebius.ai/scientific-artifact-localization/v1"
RECEIPT_SCHEMA = "fs2-serve.nebius.ai/scientific-localization-receipt/v1"
MARKER_SCHEMA = "fs2-serve.nebius.ai/scientific-localization-generation-marker/v1"
TREE_INVENTORY_ALGORITHM = "fs2-flat-tree-inventory/v1"
# The same canonical serialization over relative POSIX paths, for an installed
# tree that is legitimately nested. Flat trees keep v1 so their published
# identities never move. v2 also carries directories, because an installed
# package tree's empty directories are part of what makes it importable, and a
# files-only digest would call two different trees the same tree.
RECURSIVE_INVENTORY_ALGORITHM = "fs2-tree-inventory/v2"
# The academic-assets plane already identifies its installed trees, per file by
# SHA-256 and per symlink by target. That identity is authoritative for those
# trees: re-measuring PyRosetta under a different algorithm would publish a
# second, weaker name for bytes another plane has already named. This module
# therefore reproduces the producer's algorithm exactly rather than replacing
# it, and a cross-contract test holds the two implementations together.
TREE_MANIFEST_ALGORITHM = "fs2-tree-manifest/v1"
INVENTORY_ALGORITHMS = (TREE_INVENTORY_ALGORITHM, RECURSIVE_INVENTORY_ALGORITHM, TREE_MANIFEST_ALGORITHM)
_GENERATION = re.compile(r"^[0-9a-f]{64}$")
# A generation is published under <artifact_id>/sha256/<tree digest>, so the
# algorithm that produced the name is part of the path and a future digest can
# be introduced beside this one rather than over it.
GENERATION_DIGEST_DIRECTORY = "sha256"
# A partly written tree lives under this prefix until the rename that publishes
# it. The prefix is reserved so an interrupted run is recognizable, and skipped
# when a published generation is scanned.
STAGING_PREFIX = ".staging-"
# The marker lives inside the generation it describes, because a consumer that
# mounts only the generation sub-path cannot see a sibling file, and because the
# rename that publishes the tree then publishes the marker with it. It is
# excluded from the tree inventory by this one reserved name, deterministically,
# so the tree identity is exactly the contracted content and adding the marker
# never moves a published digest. No other dotfile is admitted: _ENTRY_NAME
# rejects a leading dot, so a marker at any other depth fails closed.
RUNTIME_MARKER_NAME = ".fs2-runtime-tree.json"

RUNTIME_ARTIFACT_ROOT = PurePosixPath("/opt/fs2/artifacts")

# This module is delivered into model runtime images to verify their own mounts,
# and those images are not all on the control plane's Python. Nothing here may
# use an interpreter feature newer than 3.10: `datetime.UTC`, for one, does not
# exist there, and importing it fails the verifier before it can report anything.
_UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+, this module targets 3.10

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
# An archive filename is a label recorded as provenance, never a member name this
# tool writes into a tree, so it admits the "+" a PEP 440 local version puts in a
# wheel name. Extracted entries keep the stricter name rule.
_ARCHIVE_FILENAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._+-]{0,254}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BINDING_NAME = re.compile(r"^(?:--[a-z][a-z0-9-]{0,62}|[A-Z][A-Z0-9_]{1,63})$")
_ABSOLUTE_PATH = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
_PACKAGE_PATH = re.compile(r"^[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*$")

# ``external-installed-tree`` is the one transform this tool never performs. The
# tree already exists because another plane installed it, and it stays where that
# plane put it; this contract only says how to recognize it and who reads it. Its
# archive is provenance for how the tree came to be, never something we expand.
EXTERNAL_TRANSFORM = "external-installed-tree"
_TRANSFORMS = frozenset({"safe-extract-zip", "safe-extract-tar", "safe-extract-tar-gz", EXTERNAL_TRANSFORM})
_MEDIA_TYPES = {
    "safe-extract-zip": "application/zip",
    "safe-extract-tar": "application/x-tar",
    "safe-extract-tar-gz": "application/gzip",
    EXTERNAL_TRANSFORM: "application/zip",
}
_MEMBER_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*/$")

EXTERNAL_MANIFEST_SCHEMA = "fs2.nebius.ai/external-model-artifact-manifest/v1"
EXTERNAL_MANIFEST_GENERATOR = "external-model-artifact-manifest/v1"
_GENERATORS = frozenset({EXTERNAL_MANIFEST_GENERATOR})


# ---------------------------------------------------------------------------
# Canonical tree inventory
# ---------------------------------------------------------------------------


def is_safe_relative_path(value: str) -> bool:
    """A relative POSIX path whose every segment is a safe name.

    One rule for flat and nested trees alike. It admits no absolute path, no
    empty segment, no ``.`` or ``..``, and no leading dot, which is what keeps
    the reserved marker name unforgeable at any depth.
    """

    if not value or value.startswith("/") or len(value) > 1024:
        return False
    return all(_ENTRY_NAME.fullmatch(segment) is not None for segment in value.split("/"))


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One localized regular file or directory, identified without its host path."""

    path: str
    size_bytes: int
    crc32: int
    kind: str = "file"

    def __post_init__(self) -> None:
        # A path may be nested for an installed tree, but every segment is still
        # a safe name and no segment may traverse. Flat trees keep single-segment
        # paths, so their v1 inventory digest is unchanged by this.
        if not is_safe_relative_path(self.path):
            raise ArtifactLocalizationError("localized tree entries must be safe relative POSIX paths")
        if self.kind not in {"file", "directory"}:
            raise ArtifactLocalizationError("localized tree entry kind must be file or directory")
        if self.kind == "directory" and (self.size_bytes or self.crc32):
            raise ArtifactLocalizationError("a directory entry carries no size or CRC-32")
        if self.size_bytes < 0 or not 0 <= self.crc32 <= 0xFFFFFFFF:
            raise ArtifactLocalizationError("localized tree entry size or CRC-32 is invalid")


def tree_inventory_bytes(entries: Iterable[TreeEntry]) -> bytes:
    """Serialize a flat tree into its canonical, host-independent inventory.

    ``fs2-flat-tree-inventory/v1`` is a path-sorted JSON array of
    ``{"bytes", "crc32", "path"}`` objects with sorted keys, no whitespace, and
    one trailing newline. It contains no host path, mount root, timestamp,
    ownership, or permission, so the same tree hashes identically wherever it is
    localized.

    Directories are refused rather than skipped: v1 describes a flat tree, and
    silently dropping a directory would let two different trees share a digest.
    """

    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.kind != "file":
            raise ArtifactLocalizationError(f"{TREE_INVENTORY_ALGORITHM} describes files only")
        if "/" in entry.path:
            raise ArtifactLocalizationError(f"{TREE_INVENTORY_ALGORITHM} describes a flat tree only")
        if entry.path in seen:
            raise ArtifactLocalizationError("localized tree contains a duplicate entry path")
        seen.add(entry.path)
        rows.append({"bytes": entry.size_bytes, "crc32": f"{entry.crc32:08x}", "path": entry.path})
    rows.sort(key=lambda row: cast(str, row["path"]))
    return (json.dumps(rows, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def recursive_inventory_bytes(entries: Iterable[TreeEntry]) -> bytes:
    """Serialize a recursive tree, directories included.

    ``fs2-tree-inventory/v2`` is the same canonical serialization as v1 over
    relative POSIX paths, with an explicit ``kind`` so a directory and a file can
    never be confused, and with directories carried as their own rows. An
    installed package tree depends on its directory structure, so a digest that
    ignored empty directories would admit a tree the runtime cannot import.
    """

    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.path in seen:
            raise ArtifactLocalizationError("localized tree contains a duplicate entry path")
        seen.add(entry.path)
        if entry.kind == "directory":
            rows.append({"kind": "directory", "path": entry.path})
        else:
            rows.append({"bytes": entry.size_bytes, "crc32": f"{entry.crc32:08x}", "kind": "file", "path": entry.path})
    rows.sort(key=lambda row: cast(str, row["path"]))
    return (json.dumps(rows, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def inventory_bytes(entries: Iterable[TreeEntry], algorithm: str) -> bytes:
    if algorithm == TREE_INVENTORY_ALGORITHM:
        return tree_inventory_bytes(entries)
    if algorithm == RECURSIVE_INVENTORY_ALGORITHM:
        return recursive_inventory_bytes(entries)
    raise ArtifactLocalizationError("tree inventory algorithm is unsupported")


def tree_inventory_sha256(entries: Iterable[TreeEntry]) -> str:
    return hashlib.sha256(tree_inventory_bytes(entries)).hexdigest()


def recursive_inventory_sha256(entries: Iterable[TreeEntry]) -> str:
    return hashlib.sha256(recursive_inventory_bytes(entries)).hexdigest()


def inventory_sha256(entries: Iterable[TreeEntry], algorithm: str) -> str:
    return hashlib.sha256(inventory_bytes(entries, algorithm)).hexdigest()


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
        if not isinstance(filename, str) or _ARCHIVE_FILENAME.fullmatch(filename) is None:
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
    inventory_algorithm: str = TREE_INVENTORY_ALGORITHM
    directory_count: int = 0

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
                }
            ),
            optional=frozenset({"complete_entry_digests", "generated_entries", "directory_count", "probe_entries"}),
            label="localization tree",
        )
        algorithm = item["inventory_algorithm"]
        if algorithm not in INVENTORY_ALGORITHMS:
            raise ArtifactLocalizationError("tree inventory algorithm is unsupported")
        algorithm = cast(str, algorithm)
        directory_count = item.get("directory_count", 0)
        if isinstance(directory_count, bool) or not isinstance(directory_count, int) or directory_count < 0:
            raise ArtifactLocalizationError("tree directory_count must be a non-negative integer")
        if directory_count and algorithm == TREE_INVENTORY_ALGORITHM:
            raise ArtifactLocalizationError(f"{TREE_INVENTORY_ALGORITHM} describes a flat tree with no directories")
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
        # A probe subset makes a cheap spot check possible for an algorithm whose
        # digest is a CRC-32 inventory. fs2-tree-manifest/v1 already binds every
        # file by SHA-256, so a subset of the same digests would add nothing and
        # is not required.
        fully_bound = algorithm == TREE_MANIFEST_ALGORITHM
        if not fully_bound and "probe_entries" not in item:
            raise ArtifactLocalizationError("tree probe_entries must contain 1..64 items")
        raw_probes = item.get("probe_entries", [])
        lower_bound = 0 if fully_bound else 1
        if not isinstance(raw_probes, list) or not lower_bound <= len(raw_probes) <= 64:
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
                or not is_safe_relative_path(probe_path)
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
        if complete and not fully_bound and len(probes) != entry_count:
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
            inventory_algorithm=algorithm,
            directory_count=directory_count,
        )

    @property
    def is_recursive(self) -> bool:
        return self.inventory_algorithm == RECURSIVE_INVENTORY_ALGORITHM

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
    visibility: str = "public"
    artifact_kind: str = ""
    source_sub_path: str = ""

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
            optional=frozenset({"notes", "visibility", "artifact_kind", "source_sub_path"}),
            label="localization artifact",
        )
        raw_consumers = item["consumers"]
        if not isinstance(raw_consumers, list) or not 1 <= len(raw_consumers) <= 16:
            raise ArtifactLocalizationError("localization consumers must contain 1..16 items")
        artifact_id = item["artifact_id"]
        transform = item["transform"]
        if not isinstance(artifact_id, str) or not isinstance(transform, str):
            raise ArtifactLocalizationError("localization artifact identity is invalid")
        visibility = item.get("visibility", "public")
        if visibility not in {"public", "tenant-private"}:
            raise ArtifactLocalizationError("localization visibility is invalid")
        visibility = cast(str, visibility)
        artifact_kind = item.get("artifact_kind", "")
        if not isinstance(artifact_kind, str) or len(artifact_kind) > 128:
            raise ArtifactLocalizationError("localization artifact_kind is invalid")
        source_sub_path = item.get("source_sub_path", "")
        if not isinstance(source_sub_path, str):
            raise ArtifactLocalizationError("localization source_sub_path must be a safe relative POSIX path")
        if source_sub_path and not is_safe_relative_path(source_sub_path):
            raise ArtifactLocalizationError("localization source_sub_path must be a safe relative POSIX path")
        if transform == EXTERNAL_TRANSFORM and not source_sub_path:
            raise ArtifactLocalizationError(f"a {EXTERNAL_TRANSFORM} artifact must declare its source_sub_path")
        if transform != EXTERNAL_TRANSFORM and source_sub_path:
            raise ArtifactLocalizationError("only an externally installed tree has a fixed source_sub_path")
        return cls(
            artifact_id=artifact_id,
            transform=transform,
            archive=ArchiveProvenance.parse(item["archive"]),
            tree=TreeIdentity.parse(item["tree"]),
            consumers=tuple(RuntimeBinding.parse(raw) for raw in raw_consumers),
            visibility=visibility,
            artifact_kind=artifact_kind or artifact_id,
            source_sub_path=source_sub_path,
        )

    @property
    def externally_installed(self) -> bool:
        """True when another plane owns these bytes and this tool only reads them."""

        return self.transform == EXTERNAL_TRANSFORM

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
    inventory_algorithm: str = TREE_INVENTORY_ALGORITHM
    directory_count: int = 0

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
            "observed_at": self.observed_at.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mount_path": self.mount_path,
            "state": self.state,
            "archive_provenance": self.archive.to_receipt(present_in_mount=self.archive_present_in_mount),
            "tree_identity": {
                "entry_count": self.entry_count,
                "total_bytes": self.total_bytes,
                "inventory_algorithm": self.inventory_algorithm,
                "inventory_sha256": self.tree_inventory_sha256,
                "probe_entries_verified": self.probe_entries_verified,
            },
        }
        if self.directory_count:
            cast(dict[str, object], value["tree_identity"])["directory_count"] = self.directory_count
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
            if item.name == RUNTIME_MARKER_NAME:
                continue
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
        inventory_algorithm=contract.tree.inventory_algorithm,
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

    moment = now or datetime.now(tz=_UTC)
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

    if tree.inventory_algorithm == TREE_MANIFEST_ALGORITHM:
        # An installed tree another plane already named. Reproduce that plane's
        # identity rather than measuring a second, weaker one of our own.
        try:
            observed = tree_manifest_identity(resolved)
        except (ArtifactLocalizationError, OSError) as error:
            return _rejected(contract, f"unsafe-tree-entry: {error}", now=moment, observation=observation)
        if observed.file_count != tree.entry_count or observed.total_bytes != tree.total_bytes:
            return _rejected(
                contract,
                f"unexpected-tree-content: {observed.file_count} files and {observed.total_bytes} bytes do not "
                f"match the contracted {tree.entry_count} files and {tree.total_bytes} bytes",
                now=moment,
                entry_count=observed.file_count,
                total_bytes=observed.total_bytes,
                observation=observation,
            )
        if observed.sha256 != tree.inventory_sha256:
            return _rejected(
                contract,
                "tree-identity-mismatch: installed tree manifest digest does not match the contract",
                now=moment,
                entry_count=observed.file_count,
                total_bytes=observed.total_bytes,
                inventory=observed.sha256,
                observation=observation,
            )
        return LocalizationReceipt(
            artifact_id=contract.artifact_id,
            mount_path=tree.canonical_mount_path,
            state="verified",
            observed_at=moment,
            archive=contract.archive,
            archive_present_in_mount=False,
            entry_count=observed.file_count,
            total_bytes=observed.total_bytes,
            tree_inventory_sha256=observed.sha256,
            probe_entries_verified=0,
            runtime_bindings=tuple(
                (item.model_id, item.binding_kind, item.binding_name, item.mount_path) for item in contract.consumers
            ),
            observation=observation,
            inventory_algorithm=TREE_MANIFEST_ALGORITHM,
            directory_count=tree.directory_count,
        )

    try:
        # A little headroom so an over-full mount is diagnosed as the wrong tree
        # rather than as a scanning limit, while a mount holding arbitrarily
        # many entries or bytes still stops at the bound.
        scan = scan_recursive_tree if tree.is_recursive else scan_localized_tree
        entries = scan(
            resolved,
            maximum_entries=tree.entry_count + tree.directory_count + 1,
            maximum_bytes=tree.total_bytes * 2 + _SCAN_HEADROOM_BYTES,
        )
    except TreeBoundExceededError as error:
        return _rejected(contract, f"unexpected-tree-content: {error}", now=moment, observation=observation)
    except ArtifactLocalizationError as error:
        return _rejected(contract, f"unsafe-tree-entry: {error}", now=moment, observation=observation)

    observed_files, observed_directories, observed_bytes = tree_counts(entries)

    matcher = tree.entry_matcher
    offending = [entry.path for entry in entries if matcher.fullmatch(entry.path) is None]
    if offending:
        return _rejected(
            contract,
            f"entry-path-pattern-violation: {len(offending)} entries including {offending[0]}",
            now=moment,
            entry_count=observed_files,
            observation=observation,
        )

    if observed_files < tree.entry_count:
        return _rejected(
            contract,
            f"partial-tree: {observed_files} of {tree.entry_count} files are present",
            now=moment,
            entry_count=observed_files,
            total_bytes=observed_bytes,
            observation=observation,
        )
    if (
        observed_files > tree.entry_count
        or observed_directories != tree.directory_count
        or observed_bytes != tree.total_bytes
    ):
        return _rejected(
            contract,
            f"unexpected-tree-content: {observed_files} files, {observed_directories} directories and "
            f"{observed_bytes} bytes do not match the contracted {tree.entry_count} files, "
            f"{tree.directory_count} directories and {tree.total_bytes} bytes",
            now=moment,
            entry_count=observed_files,
            total_bytes=observed_bytes,
            observation=observation,
        )

    inventory = inventory_sha256(entries, tree.inventory_algorithm)
    if inventory != tree.inventory_sha256:
        return _rejected(
            contract,
            "tree-identity-mismatch: localized inventory digest does not match the contract",
            now=moment,
            entry_count=observed_files,
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
                    entry_count=observed_files,
                    total_bytes=observed_bytes,
                    inventory=inventory,
                    observation=observation,
                )
            if _file_sha256(candidate) != probe.sha256:
                return _rejected(
                    contract,
                    f"probe-entry-digest-mismatch: {probe.path}",
                    now=moment,
                    entry_count=observed_files,
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
        entry_count=observed_files,
        total_bytes=observed_bytes,
        tree_inventory_sha256=inventory,
        probe_entries_verified=verified_probes,
        runtime_bindings=tuple(
            (item.model_id, item.binding_kind, item.binding_name, item.mount_path) for item in contract.consumers
        ),
        observation=observation,
        inventory_algorithm=tree.inventory_algorithm,
        directory_count=observed_directories,
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
    "GENERATION_DIGEST_DIRECTORY",
    "INVENTORY_ALGORITHMS",
    "MARKER_SCHEMA",
    "RECURSIVE_INVENTORY_ALGORITHM",
    "RUNTIME_MARKER_NAME",
    "STAGING_PREFIX",
    "count_generation",
    "generation_directory",
    "generation_marker",
    "interrupted_staging_directories",
    "inventory_bytes",
    "inventory_sha256",
    "is_safe_relative_path",
    "load_generation_marker",
    "load_localization_contracts",
    "load_localization_contracts_from_path",
    "localize_archive",
    "marker_bytes",
    "marker_sha256",
    "prepare_staging_directory",
    "promote_generation",
    "recursive_inventory_bytes",
    "recursive_inventory_sha256",
    "scan_localized_tree",
    "scan_recursive_tree",
    "tree_counts",
    "verify_generation_marker",
    "write_generation_marker",
    "tree_inventory_bytes",
    "tree_inventory_sha256",
    "main",
    "fetch_archive",
    "verify_archive",
    "verify_localized_tree",
]


# ---------------------------------------------------------------------------
# Immutable generations
# ---------------------------------------------------------------------------


def scan_recursive_tree(
    root: Path,
    *,
    maximum_entries: int,
    maximum_bytes: int,
) -> tuple[TreeEntry, ...]:
    """Read a nested tree, such as an installed package tree, the same way.

    Directories are walked rather than rejected, and are also recorded, because
    an installed tree's structure is part of what makes it importable. Every
    other rule from the flat scan still holds: no symbolic links, no device
    nodes, no unsafe names, and the reserved marker is skipped only at the root
    where a promotion writes it.
    """

    resolved = _real_directory(root, "installed tree root")
    entries: list[TreeEntry] = []
    total = 0
    stack: list[tuple[Path, str]] = [(resolved, "")]
    while stack:
        directory, prefix = stack.pop()
        with os.scandir(directory) as scan:
            for item in scan:
                relative = f"{prefix}{item.name}"
                if relative == RUNTIME_MARKER_NAME:
                    continue
                if item.name.startswith(STAGING_PREFIX) and not prefix:
                    # An interrupted promotion beside a published tree, never
                    # part of it.
                    continue
                if item.is_symlink():
                    raise ArtifactLocalizationError(f"installed tree contains a symbolic link: {relative}")
                if _ENTRY_NAME.fullmatch(item.name) is None:
                    raise ArtifactLocalizationError(f"installed tree contains an unsafe entry name: {relative}")
                if len(entries) >= maximum_entries:
                    raise TreeBoundExceededError("installed tree exceeds its declared entry bound")
                if item.is_dir(follow_symlinks=False):
                    entries.append(TreeEntry(relative, 0, 0, kind="directory"))
                    stack.append((Path(item.path), f"{relative}/"))
                    continue
                if not item.is_file(follow_symlinks=False):
                    raise ArtifactLocalizationError(f"installed tree contains a non-regular entry: {relative}")
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
                                raise TreeBoundExceededError("installed tree exceeds its declared byte bound")
                except OSError as error:
                    raise ArtifactLocalizationError(
                        f"installed tree entry could not be read safely: {relative}"
                    ) from error
                entries.append(TreeEntry(relative, size, crc & 0xFFFFFFFF))
    return tuple(sorted(entries, key=lambda entry: entry.path))


@dataclass(frozen=True, slots=True)
class TreeManifestIdentity:
    """What ``fs2-tree-manifest/v1`` says about one installed tree."""

    algorithm: str
    sha256: str
    total_bytes: int
    file_count: int
    symlink_count: int


def tree_manifest_identity(root: Path) -> TreeManifestIdentity:
    """Identify an installed tree the way the academic-assets plane does.

    This is a deliberate reimplementation of that plane's ``tree_manifest``, byte
    for byte, so a tree it already named keeps exactly one identity. Every
    regular file contributes its POSIX-relative path, byte size and SHA-256;
    every symlink contributes its path and target; directories contribute
    nothing. Entries are sorted by path and serialized as canonical JSON, and
    the digest is the SHA-256 of those bytes.

    Symlinks are described rather than refused here, unlike the flat and
    recursive scans, because an installed package tree legitimately contains
    them and the producer's identity already covers them by target.

    The one reserved marker name at the root is skipped, so promoting a producer
    tree into a content-addressed generation and sealing its marker inside still
    yields the digest the producer published. A producer tree never contains that
    name, so on the producing side the two implementations are identical.
    """

    resolved = _real_directory(root, "installed tree root")
    entries: list[dict[str, object]] = []
    total_bytes = 0
    for path in sorted(resolved.rglob("*"), key=lambda item: item.relative_to(resolved).as_posix()):
        relative = path.relative_to(resolved).as_posix()
        if relative == RUNTIME_MARKER_NAME:
            continue
        if path.is_symlink():
            entries.append({"path": relative, "kind": "symlink", "target": os.readlink(path)})
            continue
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
        entries.append({"path": relative, "kind": "file", "size_bytes": size, "sha256": digest.hexdigest()})
        total_bytes += size
    payload = {"algorithm": TREE_MANIFEST_ALGORITHM, "entries": entries}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return TreeManifestIdentity(
        algorithm=TREE_MANIFEST_ALGORITHM,
        sha256=hashlib.sha256(canonical).hexdigest(),
        total_bytes=total_bytes,
        file_count=sum(1 for entry in entries if entry["kind"] == "file"),
        symlink_count=sum(1 for entry in entries if entry["kind"] == "symlink"),
    )


def tree_counts(entries: Iterable[TreeEntry]) -> tuple[int, int, int]:
    """Files, directories, and total bytes, counted recursively."""

    files = directories = total = 0
    for entry in entries:
        if entry.kind == "directory":
            directories += 1
        else:
            files += 1
            total += entry.size_bytes
    return files, directories, total


def generation_marker(
    *,
    artifact_id: str,
    generation: str,
    entry_count: int,
    total_bytes: int,
    inventory_algorithm: str,
    sub_path: str,
    visibility: str,
    volume_kind: str = "persistent-volume-claim",
    namespace: str = "",
    claim: str = "",
    host_root: str = "",
    artifact_kind: str = "",
    directory_count: int = 0,
    archive: ArchiveProvenance | None = None,
    generated_entries: tuple[GeneratedEntry, ...] = (),
    consumer_paths: tuple[str, ...] = (),
    source: Mapping[str, object] | None = None,
    generator_identity: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """Describe one promoted generation cheaply enough to check at start-up.

    A verified tree at a mutable path can change after it was verified. A
    generation directory is named by the digest of its own content, so replacing
    the bytes means writing a different path, and this marker is the small
    document a consumer reads instead of rehashing gigabytes on every start.

    The document carries no timestamp and no host identity: no node name, no
    pod, no user, no duration, no run ID. Two promotions of the same tree, on
    different machines and on different days, must produce byte-identical
    markers, or the marker's own digest could not be pinned by a handoff and
    re-promotion would silently change identity. When something happened, on
    what, and for how long is recorded on the staging receipt, which is an
    event; this is an identity.
    """

    if _GENERATION.fullmatch(generation) is None:
        raise ArtifactLocalizationError("a generation must be named by a lowercase SHA-256")
    if visibility not in {"public", "tenant-private"}:
        raise ArtifactLocalizationError("generation visibility is invalid")
    if inventory_algorithm not in INVENTORY_ALGORITHMS:
        raise ArtifactLocalizationError("tree inventory algorithm is unsupported")
    if not is_safe_relative_path(sub_path):
        raise ArtifactLocalizationError("a generation sub-path must be a safe relative POSIX path")
    # A generation lives on exactly one kind of plane, and each kind is addressed
    # differently. Carrying a claim for a host directory, or a host root for a
    # claim, would describe a location that does not exist.
    if volume_kind == "persistent-volume-claim":
        if not namespace or not claim or host_root:
            raise ArtifactLocalizationError("a claim-backed generation is addressed by namespace and claim")
    elif volume_kind == "host-path":
        if not host_root or namespace or claim:
            raise ArtifactLocalizationError("a host-backed generation is addressed by its host root")
        if not host_root.startswith("/") or ".." in PurePosixPath(host_root).parts:
            raise ArtifactLocalizationError("a generation host root must be a safe absolute path")
    else:
        raise ArtifactLocalizationError("generation volume_kind is unsupported")
    document: dict[str, object] = {
        "schema": MARKER_SCHEMA,
        "artifact_id": artifact_id,
        "artifact_kind": artifact_kind or artifact_id,
        "generation": generation,
        "inventory_algorithm": inventory_algorithm,
        "inventory_sha256": generation,
        "entry_count": entry_count,
        "directory_count": directory_count,
        "total_bytes": total_bytes,
        "volume_kind": volume_kind,
        "namespace": namespace,
        "claim": claim,
        "host_root": host_root,
        "sub_path": sub_path,
        "visibility": visibility,
        "read_only": True,
    }
    if archive is not None:
        document.update(
            {
                "source_filename": archive.filename,
                "source_bytes": archive.size_bytes,
                "source_sha256": archive.sha256,
                "source_uri": archive.source_uri,
                "source_revision": archive.source_revision,
                "license_id": archive.license_id,
            }
        )
    elif source is not None:
        for key, value in source.items():
            document[key if key.startswith(("source_", "license")) else f"source_{key}"] = value
    identity: list[dict[str, object]] = [
        {
            "path": entry.path,
            "sha256": entry.sha256,
            "generator": entry.generator,
            "generator_inputs": dict(entry.generator_inputs),
        }
        for entry in generated_entries
    ]
    identity.extend(dict(item) for item in generator_identity)
    document["generator_identity"] = identity
    document["consumer_paths"] = list(consumer_paths)
    return document


def marker_bytes(document: Mapping[str, object]) -> bytes:
    """Serialize a marker deterministically, so its digest can be pinned.

    The same serialization the runtime admission manifest already uses, because
    a consumer that hashes exact bytes must get one answer for both documents.
    """

    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def marker_sha256(document: Mapping[str, object]) -> str:
    return hashlib.sha256(marker_bytes(document)).hexdigest()


def write_generation_marker(path: Path, document: Mapping[str, object]) -> str:
    """Write a marker once, and refuse to change one that already exists.

    A marker names an immutable generation, so it is itself immutable. Rewriting
    it, even with the same generation, would make marker identity mutable and a
    pinned marker digest meaningless. Re-promoting the same tree therefore has to
    produce the same bytes, and anything else fails closed.
    """

    payload = marker_bytes(document)
    digest = hashlib.sha256(payload).hexdigest()
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise ArtifactLocalizationError("generation marker path is not a regular file")
        existing = path.read_bytes()
        if existing != payload:
            raise ArtifactLocalizationError(
                "a generation marker already exists with different content; "
                "marker identity is immutable and must not be rewritten"
            )
        return digest
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.parent / f".{path.name}.{os.getpid()}.partial"
    handle = _open_for_write(staging)
    try:
        with os.fdopen(handle, "wb") as target:
            target.write(payload)
        os.link(staging, path)
    except FileExistsError:
        # Another writer won the race; its bytes must match ours.
        if path.read_bytes() != payload:
            raise ArtifactLocalizationError("a concurrent writer published a different marker") from None
    except OSError:
        staging.unlink(missing_ok=True)
        raise
    finally:
        staging.unlink(missing_ok=True)
    return digest


@dataclass(frozen=True, slots=True)
class LinkedTree:
    """How a generation was materialized from a tree that already existed."""

    files_linked: int
    files_copied: int
    bytes_linked: int
    bytes_copied: int
    directories: int
    symlinks: int


def link_tree_into(source: Path, destination: Path) -> LinkedTree:
    """Materialize a generation from an existing tree without copying its bytes.

    A hard link gives the generation its own immutable path into the very same
    data, so promoting a 3 GB installed tree costs directory entries rather than
    3 GB. That is only sound because these files are read-only and are replaced
    by rename rather than rewritten in place; a plane that edited a file under
    its own path would change both names at once, which is why this refuses a
    writable source file rather than linking it.

    Copying is the fallback for a source on another filesystem, and the counts
    come back so a caller can prove which happened rather than assume.
    """

    resolved = _real_directory(source, "promotion source")
    destination.mkdir(parents=True, exist_ok=True)
    files_linked = files_copied = symlinks = directories = 0
    bytes_linked = bytes_copied = 0
    for path in sorted(resolved.rglob("*"), key=lambda item: item.relative_to(resolved).as_posix()):
        relative = path.relative_to(resolved).as_posix()
        if relative == RUNTIME_MARKER_NAME:
            continue
        target = destination / relative
        if path.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(path), target)
            symlinks += 1
            continue
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            directories += 1
            continue
        if not path.is_file():
            raise ArtifactLocalizationError(f"promotion source contains a non-regular entry: {relative}")
        status = path.stat()
        if status.st_mode & 0o222:
            raise ArtifactLocalizationError(
                f"promotion source entry is writable and cannot be safely shared by link: {relative}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(path, target)
            files_linked += 1
            bytes_linked += status.st_size
        except OSError as error:
            if getattr(error, "errno", None) not in {errno.EXDEV, errno.EMLINK, errno.EPERM, errno.EOPNOTSUPP}:
                raise ArtifactLocalizationError(f"could not link {relative} into the generation") from error
            with path.open("rb") as handle, target.open("wb") as sink:
                while True:
                    chunk = handle.read(_READ_CHUNK)
                    if not chunk:
                        break
                    sink.write(chunk)
            files_copied += 1
            bytes_copied += status.st_size
    return LinkedTree(
        files_linked=files_linked,
        files_copied=files_copied,
        bytes_linked=bytes_linked,
        bytes_copied=bytes_copied,
        directories=directories,
        symlinks=symlinks,
    )


def load_generation_marker(path: Path) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ArtifactLocalizationError("generation marker is not a regular file")
    payload = path.read_bytes()
    if len(payload) > 1024 * 1024:
        raise ArtifactLocalizationError("generation marker exceeds its byte bound")
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ArtifactLocalizationError("generation marker is not valid UTF-8 JSON") from error
    document = strict_object(
        value,
        required=frozenset(
            {
                "schema",
                "artifact_id",
                "artifact_kind",
                "generation",
                "inventory_algorithm",
                "inventory_sha256",
                "entry_count",
                "directory_count",
                "total_bytes",
                "volume_kind",
                "namespace",
                "claim",
                "host_root",
                "sub_path",
                "visibility",
                "read_only",
                "generator_identity",
                "consumer_paths",
            }
        ),
        optional=frozenset(
            {
                "source_filename",
                "source_bytes",
                "source_sha256",
                "source_uri",
                "source_revision",
                "source_kind",
                "source_name",
                "source_version",
                "source_note",
                "license_id",
                "cross_reference",
            }
        ),
        label="generation marker",
    )
    if document["schema"] != MARKER_SCHEMA:
        raise ArtifactLocalizationError("generation marker schema is unsupported")
    return document


def verify_generation_marker(
    marker: Mapping[str, object],
    *,
    artifact_id: str,
    expected_generation: str,
    expected_sub_path: str,
) -> Mapping[str, object]:
    """Fail closed unless the marker describes exactly the mounted generation."""

    if marker.get("schema") != MARKER_SCHEMA:
        raise ArtifactLocalizationError("generation marker schema is unsupported")
    if marker["artifact_id"] != artifact_id:
        raise ArtifactLocalizationError("generation marker names a different artifact")
    if marker["generation"] != expected_generation or marker["inventory_sha256"] != expected_generation:
        raise ArtifactLocalizationError("generation marker does not describe the mounted generation")
    if marker["sub_path"] != expected_sub_path:
        raise ArtifactLocalizationError("generation marker sub-path does not match the mount")
    if marker["read_only"] is not True:
        raise ArtifactLocalizationError("generation marker does not assert a read-only mount")
    return marker


def generation_directory(artifact_root: Path, generation: str) -> Path:
    """Where one generation is published: ``<artifact_root>/sha256/<digest>``.

    The algorithm that produced the name is a path segment, so a future digest
    can be introduced beside this one instead of over it.
    """

    if _GENERATION.fullmatch(generation) is None:
        raise ArtifactLocalizationError("a generation must be named by a lowercase SHA-256")
    return artifact_root / GENERATION_DIGEST_DIRECTORY / generation


def interrupted_staging_directories(artifact_root: Path, *, older_than_seconds: float) -> list[Path]:
    """Temporary generations an earlier run left behind.

    Age is the discriminator, not existence, because a concurrent staging job
    holds a temporary directory that is doing exactly what it should. Only one
    old enough that no live run could still own it is treated as wreckage.
    """

    if not artifact_root.is_dir():
        return []
    cutoff = time.time() - older_than_seconds
    stale: list[Path] = []
    for candidate in sorted(artifact_root.iterdir()):
        if not candidate.name.startswith(STAGING_PREFIX) or candidate.is_symlink() or not candidate.is_dir():
            continue
        try:
            if candidate.stat().st_mtime < cutoff:
                stale.append(candidate)
        except OSError:  # pragma: no cover - vanished under us, which is the goal
            continue
    return stale


def _remove_tree(root: Path) -> None:
    """Delete a staged tree whose directories may already be read-only."""

    for path in sorted(root.rglob("*"), reverse=True):
        try:
            if path.is_dir() and not path.is_symlink():
                os.chmod(path, 0o700)  # noqa: S103 - reclaiming a sealed temporary
                path.rmdir()
            else:
                path.unlink()
        except OSError:  # pragma: no cover - best effort reclamation
            continue
    with contextlib.suppress(OSError):
        os.chmod(root, 0o700)  # noqa: S103 - reclaiming a sealed temporary
        root.rmdir()


def prepare_staging_directory(artifact_root: Path, *, reclaim_after_seconds: float = 6 * 3600.0) -> Path:
    """Open a private temporary generation beside where it will be published.

    Staging under the artifact root is what lets publication be a rename within
    one filesystem, and the reserved prefix is what lets an interrupted run be
    recognized and reclaimed rather than mistaken for a tree.
    """

    artifact_root.mkdir(parents=True, exist_ok=True)
    for stale in interrupted_staging_directories(artifact_root, older_than_seconds=reclaim_after_seconds):
        _remove_tree(stale)
    return Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=artifact_root))


def promote_generation(staged: Path, artifact_root: Path, generation: str) -> Path:
    """Make a verified tree read-only, then publish it under its own digest.

    The rename is the commit point: a consumer either sees no generation or sees
    the whole verified one, never a half-written directory. Promoting a
    generation that already exists is a no-op rather than an overwrite, so
    restaging can never destroy bytes another workload is already mounting, and
    an interrupted run leaves only a temporary directory the next run reclaims.
    """

    target = generation_directory(artifact_root, generation)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return target
    resolved = _real_directory(staged, "staged generation")
    # A generation is world-readable on purpose: several runtimes mount it under
    # their own accounts. It is never writable, which is the property that keeps
    # a content-addressed path honest.
    for path in sorted(resolved.rglob("*"), reverse=True):
        if path.is_symlink():
            # A symlink inside a generation is only ever one this tool recreated
            # from a source tree whose identity already covers it by target.
            continue
        if not path.is_file():
            continue
        status = path.stat()
        if not status.st_mode & 0o222:
            # Already read-only. A file shared by hard link with the tree it was
            # promoted from arrives in this state, and leaving it alone is what
            # keeps the owning plane's mode bits intact.
            continue
        if status.st_nlink > 1:
            # chmod follows the inode, so sealing a shared writable file would
            # rewrite every other name for it, including the producing plane's.
            # Refuse rather than reach outside this generation.
            raise ArtifactLocalizationError(
                "a writable file shared by hard link cannot be sealed, because chmod follows "
                "the inode and would rewrite the tree it was promoted from"
            )
        os.chmod(path, 0o444)
    try:
        # Renaming a directory rewrites its own parent link, so the directories
        # can only be sealed once they are already in their final place.
        os.rename(resolved, target)
    except OSError as error:
        if target.exists():
            # Another writer promoted the identical generation first, which is
            # the same bytes by construction.
            return target
        if getattr(error, "errno", None) == errno.EXDEV:
            raise ArtifactLocalizationError(
                "a generation must be staged on the same filesystem it is published to, "
                "because the rename is what makes publication atomic"
            ) from error
        raise ArtifactLocalizationError("could not publish the verified generation") from error
    for path in sorted(target.rglob("*"), reverse=True):
        if path.is_dir():
            os.chmod(path, 0o555)  # noqa: S103 - read-only for every reader
    os.chmod(target, 0o555)  # noqa: S103 - read-only for every reader
    return target


# ---------------------------------------------------------------------------
# Staging entry point
# ---------------------------------------------------------------------------


def count_generation(mount: Path, *, maximum_entries: int) -> tuple[int, int]:
    """Count a mounted generation's files and directories, recursively.

    This walks the tree but reads no file content, so a start-up gate can afford
    it on gigabytes where rehashing would be unaffordable. It is a structural
    cross-check that the mount is shaped like the generation the marker
    describes, never a substitute for the digest that named the directory.
    """

    resolved = _real_directory(mount, "mounted generation")
    files = directories = 0
    stack = [resolved]
    while stack:
        with os.scandir(stack.pop()) as scan:
            for item in scan:
                if files + directories > maximum_entries:
                    raise TreeBoundExceededError("mounted generation exceeds its declared entry bound")
                if item.name == RUNTIME_MARKER_NAME:
                    continue
                if item.is_symlink():
                    raise ArtifactLocalizationError("mounted generation contains a symbolic link")
                if item.is_dir(follow_symlinks=False):
                    directories += 1
                    stack.append(Path(item.path))
                else:
                    files += 1
    return files, directories


def _verify_marker(options: argparse.Namespace) -> int:
    """Admit a mounted generation from its own marker, without rehashing it."""

    expected = options.expect_generation
    mount = options.mount
    if not expected:
        raise SystemExit("marker requires --expect-generation")
    # The marker lives inside the generation by default, so mounting the
    # generation is enough to admit it; nothing else has to be mounted.
    marker_path = options.marker or (mount / RUNTIME_MARKER_NAME if mount is not None else None)
    if marker_path is None:
        raise SystemExit("marker requires --marker or --mount")
    try:
        marker = load_generation_marker(marker_path)
        identity = verify_generation_marker(
            marker,
            artifact_id=options.artifact_id,
            expected_generation=expected,
            expected_sub_path=options.sub_path,
        )
        if mount is not None:
            files, directories = count_generation(mount, maximum_entries=options.maximum_entries)
            declared_files = identity["entry_count"]
            declared_directories = identity.get("directory_count", 0)
            if files != declared_files or directories != declared_directories:
                raise ArtifactLocalizationError(
                    f"mount holds {files} files and {directories} directories, and the marker declares "
                    f"{declared_files} files and {declared_directories} directories"
                )
    except (ArtifactLocalizationError, ScientificAdapterError) as error:
        print(json.dumps({"state": "rejected", "reason": str(error)}, indent=2, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "state": "admitted",
                "marker": dict(marker),
                "marker_sha256": marker_sha256(marker),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _inventory(options: argparse.Namespace, started: float) -> int:
    """Record an identity for a tree this tool did not stage, without touching it."""

    mount = options.mount
    if mount is None:
        raise SystemExit("inventory requires --mount")
    algorithm = options.algorithm
    if algorithm == TREE_MANIFEST_ALGORITHM:
        manifest = tree_manifest_identity(mount)
        digest, files, total = manifest.sha256, manifest.file_count, manifest.total_bytes
        directories = sum(1 for path in mount.rglob("*") if path.is_dir() and not path.is_symlink())
    else:
        entries = scan_recursive_tree(
            mount,
            maximum_entries=options.maximum_entries,
            maximum_bytes=options.maximum_bytes,
        )
        digest = recursive_inventory_sha256(entries)
        files, directories, total = tree_counts(entries)
    expected_entries = options.expect_entries
    expected_bytes = options.expect_bytes
    if expected_entries is not None and files != expected_entries:
        print(f"tree holds {files} files and {expected_entries} were expected")
        return 1
    if expected_bytes is not None and total != expected_bytes:
        print(f"tree holds {total} bytes and {expected_bytes} were expected")
        return 1
    document = generation_marker(
        artifact_id=options.artifact_id,
        generation=digest,
        entry_count=files,
        directory_count=directories,
        total_bytes=total,
        inventory_algorithm=algorithm,
        sub_path=options.sub_path,
        volume_kind=options.volume_kind,
        namespace=options.namespace,
        claim=options.claim,
        host_root=options.host_root,
        visibility=options.visibility,
        source=_cli_source(options.source),
    )
    if options.cross_reference:
        # Another plane may already record an identity for this tree under its
        # own scheme. Carry it as a reference, never as our measurement.
        references: dict[str, str] = {}
        for item in options.cross_reference:
            key, separator, value = item.partition("=")
            if not separator or not key or not value:
                raise SystemExit("--cross-reference expects KEY=VALUE")
            references[key] = value
        document["cross_reference"] = references
    marker_path = options.marker
    digest_self = marker_sha256(document)
    if marker_path is not None:
        digest_self = write_generation_marker(marker_path, document)
    # The identity is the document; when it was measured is reported beside it
    # and never inside it, so re-running this produces the same marker bytes.
    print(
        json.dumps(
            {
                "marker": document,
                "marker_sha256": digest_self,
                "observation": {"duration_seconds": round(time.monotonic() - started, 3)},
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _cli_source(value: str | None) -> Mapping[str, object] | None:
    """Describe where a tree this tool did not extract actually came from."""

    if not value:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ArtifactLocalizationError("source must be a JSON object")
    allowed = {"kind", "name", "version", "source_uri", "source_revision", "license_id", "sha256", "bytes", "note"}
    unknown = sorted(set(parsed) - allowed)
    if unknown:
        raise ArtifactLocalizationError(f"source contains unknown fields {unknown}")
    return parsed


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

    parser = argparse.ArgumentParser(prog="fs2-localize", description=__doc__)
    parser.add_argument("mode", choices=("stage", "promote", "verify", "marker", "inventory"))
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--mount", type=Path)
    parser.add_argument(
        "--artifact-root",
        type=Path,
        help="stage: publish the verified tree at <artifact-root>/sha256/<tree digest>",
    )
    parser.add_argument("--source", help="inventory: JSON object describing where an unstaged tree came from")
    parser.add_argument(
        "--promote-from",
        type=Path,
        help="promote: an existing tree to publish as a generation, shared by link rather than copied",
    )
    parser.add_argument(
        "--algorithm",
        default=RECURSIVE_INVENTORY_ALGORITHM,
        choices=INVENTORY_ALGORITHMS,
        help="inventory: the identity algorithm to measure the tree with",
    )
    parser.add_argument("--marker", type=Path, help="the promotion marker to write or verify")
    parser.add_argument("--sub-path", default="", help="the exact volume sub-path the generation is published at")
    parser.add_argument(
        "--volume-kind",
        default="persistent-volume-claim",
        choices=("persistent-volume-claim", "host-path"),
        help="which kind of plane the generation is published on",
    )
    parser.add_argument("--namespace", default="")
    parser.add_argument("--claim", default="")
    parser.add_argument("--host-root", default="", help="host-path plane: the Terraform-managed host root")
    parser.add_argument("--visibility", default="public", choices=("public", "tenant-private"))
    parser.add_argument("--expect-generation", help="marker: the generation the caller believes it mounted")
    parser.add_argument("--expect-entries", type=int, help="inventory: fail unless the tree holds exactly this many")
    parser.add_argument("--expect-bytes", type=int, help="inventory: fail unless the tree holds exactly this many")
    parser.add_argument(
        "--cross-reference",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="inventory: another plane's recorded identity for the same tree, kept distinct from ours",
    )
    parser.add_argument("--maximum-entries", type=int, default=500_000)
    parser.add_argument("--maximum-bytes", type=int, default=64 * 1024 * 1024 * 1024)
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

    if options.mode == "marker":
        return _verify_marker(options)
    if options.mode == "inventory":
        return _inventory(options, started)

    if options.contract is None:
        raise SystemExit(f"{options.mode} requires --contract")
    if options.mount is None and options.artifact_root is None:
        raise SystemExit(f"{options.mode} requires --mount or --artifact-root")
    if options.mode == "promote" and (options.promote_from is None or options.artifact_root is None):
        raise SystemExit("promote requires --promote-from and --artifact-root")
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
            if options.mount is None:
                # Stage into a private temporary generation beside where it will
                # be published, so the rename that commits it stays within one
                # filesystem and an interrupted run leaves only a reclaimable
                # temporary directory, never a partial final tree.
                staging = prepare_staging_directory(options.artifact_root)
                staging.rmdir()
                options.mount = staging
            receipt = localize_archive(archive, options.mount, contract, observation=observation or None)
        elif options.mode == "promote":
            # The bytes already exist somewhere this tool does not own. Give them
            # an immutable content-addressed name without a second copy, then
            # verify the result before anything is published.
            staging = prepare_staging_directory(options.artifact_root)
            linked = link_tree_into(options.promote_from, staging)
            options.mount = staging
            observation["files_linked"] = linked.files_linked
            observation["files_copied"] = linked.files_copied
            observation["bytes_linked"] = linked.bytes_linked
            observation["bytes_copied"] = linked.bytes_copied
            receipt = verify_localized_tree(
                staging,
                contract,
                verify_probes=not options.skip_probes,
                observation=observation or None,
            )
            if not receipt.verified:
                _remove_tree(staging)
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

    if options.mode in {"stage", "promote"} and options.artifact_root is not None and receipt.verified:
        generation = receipt.tree_inventory_sha256
        sub_path = options.sub_path or f"{GENERATION_DIGEST_DIRECTORY}/{generation}"
        marker = generation_marker(
            artifact_id=contract.artifact_id,
            generation=generation,
            entry_count=receipt.entry_count,
            directory_count=receipt.directory_count,
            total_bytes=receipt.total_bytes,
            inventory_algorithm=contract.tree.inventory_algorithm,
            sub_path=sub_path,
            volume_kind=options.volume_kind,
            namespace=options.namespace,
            claim=options.claim,
            host_root=options.host_root,
            visibility=options.visibility,
            archive=contract.archive,
            generated_entries=contract.tree.generated_entries,
            consumer_paths=contract.tree.mount_paths,
        )
        # Write the marker into the tree before sealing it, so the rename that
        # publishes the generation publishes its marker with it and a consumer
        # that mounts only the generation can still admit it.
        marker_digest = write_generation_marker(options.mount / RUNTIME_MARKER_NAME, marker)
        published = promote_generation(options.mount, options.artifact_root, generation)
        observation["generation"] = published.name
        observation["generation_sub_path"] = sub_path
        observation["marker_sha256"] = marker_digest
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
        inventory_algorithm=receipt.inventory_algorithm,
        directory_count=receipt.directory_count,
    ).to_dict()
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if options.receipt is not None:
        options.receipt.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if receipt.verified else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a staging entry point
    raise SystemExit(main())

#!/usr/bin/env python3
"""Fail-closed entrypoint for the fs2 AlphaFold 3 v3.0.4 academic runtime.

This module is the only entrypoint of the runtime image. It exists because the
AlphaFold 3 model parameters are licensed, are obtained by the operator directly
from Google, and are never embedded in an image. Every execution therefore has
to prove, before any AlphaFold 3 code touches them, that the bytes mounted into
the container are exactly the authorized parameter object and nothing else.

It also enforces the two-stage execution contract of the platform. The data
pipeline is a CPU stage that reads the shared reference databases. Inference is
a GPU stage that reads the licensed parameters and the immutable handoff the CPU
stage produced. A single stage never carries both bindings at once, so a run can
never occupy a GPU while doing CPU-only database search.

Nothing here writes, copies, decompresses to disk, caches or exports a
parameter byte. The parameter mount is read-only and stays read-only.

Design notes
------------
* Identity values come from the contracts baked into the image. Environment
  variables can move a *path*, never an expected digest or size, so a
  misconfigured pod cannot weaken verification.
* Heavy dependencies (jax, zstandard, alphafold3) are imported lazily so this
  module stays importable, and therefore unit-testable, outside the image.
* The persistent directories under the cache root hold XLA and Triton
  compilation artefacts. They are an auxiliary compiler cache. They are not a
  GPU snapshot and this module never reports them as one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence
from urllib.parse import unquote, urlparse

RECEIPT_SCHEMA = "fs2-serve.nebius.ai/alphafold3-runtime-receipt/v1"
REFERENCE_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/reference-data-manifest/v1"

SOURCE_LOCK_PATH = Path(
    os.environ.get("FS2_AF3_SOURCE_LOCK", "/opt/fs2/af3-runtime-source-lock.json")
)
PARAMETER_BINDING_PATH = Path(
    os.environ.get("FS2_AF3_PARAMETER_BINDING", "/opt/fs2/af3-parameter-binding.json")
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HEX_READ_CHUNK = 8 * 1024 * 1024

# AlphaFold 3 selects its parameter object by scanning the model directory and
# raises if more than one model matches. These are the suffixes it accepts, so
# the same set is used here to prove exactly one candidate is present.
PARAMETER_SUFFIX_RE = re.compile(r"\.bin(\.zst)?(\.[0-9]+)?$|\.[0-9]+\.bin(\.zst)?$")

# Reference database objects of the AlphaFold 3 v3.0 public bundle. Used to
# prove that a GPU inference stage has no reference database bound to it.
REFERENCE_DB_FILENAMES = (
    "bfd-first_non_consensus_sequences.fasta",
    "mgy_clusters_2022_05.fa",
    "uniprot_all_2021_04.fa",
    "uniref90_2022_05.fa",
    "nt_rna_2023_02_23_clust_seq_id_90_cov_80_rep_seq.fasta",
    "rfam_14_9_clust_seq_id_90_cov_80_rep_seq.fasta",
    "rnacentral_active_seq_id_90_cov_80_linclust.fasta",
    "pdb_seqres_2022_09_28.fasta",
)


class ContractError(RuntimeError):
    """A binding, identity or stage-separation requirement was not met."""


# ---------------------------------------------------------------------------
# Contract loading
# ---------------------------------------------------------------------------


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            document = json.load(handle)
    except FileNotFoundError as error:
        raise ContractError(f"required contract is missing: {path}") from error
    except json.JSONDecodeError as error:
        raise ContractError(f"contract {path} is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise ContractError(f"contract {path} must be a JSON object")
    return document


@dataclass(frozen=True)
class ParameterExpectation:
    """The immutable identity of the authorized parameter object."""

    artifact_id: str
    filename: str
    sha256: str
    size_bytes: int
    magic_hex: str
    decompressed_sha256: str | None
    decompressed_size_bytes: int | None
    asset_gid: int
    expect_distribution_version: str
    expect_min_parameter_arrays: int

    @classmethod
    def from_contract(cls, document: dict[str, Any]) -> "ParameterExpectation":
        artifact = document.get("artifact")
        delivery = document.get("delivery")
        invocation = document.get("invocation")
        if not isinstance(artifact, dict) or not isinstance(delivery, dict):
            raise ContractError("parameter binding contract is missing artifact or delivery")
        if not isinstance(invocation, dict):
            raise ContractError("parameter binding contract is missing invocation")
        permissions = delivery.get("permissions")
        if not isinstance(permissions, dict):
            raise ContractError("parameter binding contract is missing delivery permissions")
        decompressed = artifact.get("decompressed") or {}
        digest = str(artifact.get("sha256", ""))
        if not SHA256_RE.fullmatch(digest):
            raise ContractError("parameter binding contract has no valid artifact sha256")
        size = artifact.get("size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ContractError("parameter binding contract has no valid artifact size_bytes")
        return cls(
            artifact_id=str(artifact.get("artifact_id", "")),
            filename=str(artifact.get("filename", "")),
            sha256=digest,
            size_bytes=size,
            magic_hex=str(artifact.get("magic_hex", "")),
            decompressed_sha256=(
                str(decompressed["sha256"]) if decompressed.get("sha256") else None
            ),
            decompressed_size_bytes=(
                int(decompressed["size_bytes"]) if decompressed.get("size_bytes") else None
            ),
            asset_gid=int(permissions.get("asset_gid", 0)),
            expect_distribution_version=str(invocation.get("expect_distribution_version", "")),
            expect_min_parameter_arrays=int(invocation.get("expect_min_parameter_arrays", 0)),
        )


# ---------------------------------------------------------------------------
# Parameter identity verification
# ---------------------------------------------------------------------------


def sha256_of_file(path: Path, chunk: int = HEX_READ_CHUNK) -> tuple[str, int]:
    """Return the SHA-256 and the byte count of ``path``, streaming it once."""
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
            total += len(block)
    return digest.hexdigest(), total


def verify_parameter_artifact(
    path: Path, expect: ParameterExpectation, *, deep: bool = False
) -> dict[str, Any]:
    """Prove the mounted object is exactly the authorized parameter artifact.

    Checks run cheapest-first so an obviously wrong mount fails without reading
    a gigabyte. The path itself is reported, but never any content byte.
    """
    if not path.exists():
        raise ContractError(
            f"AlphaFold 3 parameter object is not mounted at {path}. "
            "Bind the academic claim subPath alphafold3/af3.bin.zst read-only "
            "and grant supplemental group "
            f"{expect.asset_gid}."
        )
    if path.is_dir():
        raise ContractError(f"parameter path {path} is a directory, expected a single file")
    if not path.is_file():
        raise ContractError(f"parameter path {path} is not a regular file")

    stat = path.stat()
    if stat.st_size != expect.size_bytes:
        raise ContractError(
            f"parameter object size mismatch at {path}: "
            f"found {stat.st_size} bytes, authorized artifact is {expect.size_bytes} bytes"
        )

    if expect.magic_hex:
        want_magic = bytes.fromhex(expect.magic_hex)
        with path.open("rb") as handle:
            found_magic = handle.read(len(want_magic))
        if found_magic != want_magic:
            raise ContractError(
                f"parameter object at {path} does not start with the expected "
                f"{expect.magic_hex} zstd magic"
            )

    observed, counted = sha256_of_file(path)
    if counted != expect.size_bytes:
        raise ContractError(
            f"parameter object at {path} changed while being read: "
            f"read {counted} bytes, expected {expect.size_bytes}"
        )
    if observed != expect.sha256:
        raise ContractError(
            f"parameter object digest mismatch at {path}: "
            f"found sha256 {observed}, authorized artifact is sha256 {expect.sha256}"
        )

    report: dict[str, Any] = {
        "artifact_id": expect.artifact_id,
        "path": str(path),
        "size_bytes": counted,
        "sha256": observed,
        "identity_kind": "file-digest",
        "read_only_mount": not os.access(path, os.W_OK),
        "deep_verified": False,
    }

    if deep:
        report.update(_verify_decompressed_identity(path, expect))
    return report


def _verify_decompressed_identity(path: Path, expect: ParameterExpectation) -> dict[str, Any]:
    """Stream-decompress the artifact to verify its plaintext identity.

    The plaintext is hashed in memory as it streams and is never written to
    disk, so no unencrypted copy of the licensed parameters is ever created.
    """
    if not expect.decompressed_sha256 or not expect.decompressed_size_bytes:
        raise ContractError("deep verification requested but no decompressed identity is declared")
    try:
        import zstandard
    except ImportError as error:  # pragma: no cover - present in the image
        raise ContractError("deep verification requires the zstandard package") from error

    digest = hashlib.sha256()
    total = 0
    decompressor = zstandard.ZstdDecompressor()
    with path.open("rb") as handle, decompressor.stream_reader(handle) as stream:
        while True:
            block = stream.read(HEX_READ_CHUNK)
            if not block:
                break
            digest.update(block)
            total += len(block)

    if total != expect.decompressed_size_bytes:
        raise ContractError(
            "decompressed parameter size mismatch: "
            f"found {total} bytes, expected {expect.decompressed_size_bytes}"
        )
    observed = digest.hexdigest()
    if observed != expect.decompressed_sha256:
        raise ContractError(
            "decompressed parameter digest mismatch: "
            f"found sha256 {observed}, expected sha256 {expect.decompressed_sha256}"
        )
    return {
        "deep_verified": True,
        "decompressed_sha256": observed,
        "decompressed_size_bytes": total,
    }


def parameter_candidates(model_dir: Path) -> list[str]:
    """Names in ``model_dir`` that AlphaFold 3's model selector would match."""
    if not model_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in model_dir.iterdir()
        if entry.is_file() and PARAMETER_SUFFIX_RE.search(entry.name)
    )


def resolve_model_dir(parameter_path: Path) -> tuple[Path, list[str]]:
    """Return the directory to hand to ``--model_dir`` and its candidate list.

    AlphaFold 3 scans the whole directory and refuses to run when more than one
    model matches, so an ambiguous mount is rejected here with a clearer message
    than the upstream traceback.
    """
    model_dir = parameter_path.parent
    candidates = parameter_candidates(model_dir)
    if parameter_path.name not in candidates:
        raise ContractError(
            f"parameter object {parameter_path.name} is not selectable by AlphaFold 3 "
            f"inside {model_dir}"
        )
    if len(candidates) != 1:
        raise ContractError(
            f"model directory {model_dir} exposes {len(candidates)} parameter objects "
            f"({', '.join(candidates)}); AlphaFold 3 requires exactly one. Mount the "
            "single authorized object with a subPath instead of a whole directory."
        )
    return model_dir, candidates


# ---------------------------------------------------------------------------
# Reference-data binding
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> bytes:
    """Byte-exact canonical form used by the reference-data publisher.

    Identical to reference-data/reference_data.py canonical_json, because the
    manifest self-digest is recomputed here and must match the producer bit for
    bit. Any divergence would turn a real verification into a false one.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def manifest_self_digest(manifest: dict[str, Any]) -> str:
    """The manifest document's own SHA-256, recomputed from its content."""
    return hashlib.sha256(canonical_json(manifest)).hexdigest()


# The reference-data publisher's terminal receipt. This runtime cannot import the
# publisher's module, so the contract below mirrors
# reference-data/reference_data.py validate_terminal_receipt exactly, and the
# interoperability tests assert the two agree by validating the same fixture
# with the producer's own functions.
TERMINAL_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/reference-data-terminal-receipt/v1"
TERMINAL_RECEIPT_FIELDS = frozenset(
    {"schema", "bundle_id", "revision", "created_at", "storage", "content", "placement"}
)
TERMINAL_STORAGE_FIELDS = frozenset({"host_root", "mount_path", "dataset_sub_path", "read_only"})
TERMINAL_CONTENT_FIELDS = frozenset(
    {
        "tree_sha256",
        "manifest_sha256",
        "inventory_sha256",
        "inventory_marker",
        "file_count",
        "expanded_bytes",
        "inline_inventory",
    }
)

# Near-miss field names that do not exist in the published contract. Accepting
# one would let this runtime bind a digest nobody produced, so they are refused
# rather than ignored. Mirrors the publisher's own HANDOFF_ALIASES.
HANDOFF_ALIASES = {
    "published_manifest_sha256": "content.manifest_sha256",
    "source_sub_path": "storage.dataset_sub_path",
    "published_tree_sha256": "content.tree_sha256",
    "manifest_digest": "content.manifest_sha256",
    "shared_filesystem_uri": "storage.host_root with storage.dataset_sub_path",
}

MAX_INLINE_INVENTORY_FILES = 4096
GPU_SELECTOR_KEYS = frozenset(
    {"workload.fs2.nebius/gpu", "nebius.com/gpu", "accelerator.fs2.nebius/class"}
)

BUNDLE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
MANIFEST_MARKER = ".fs2-manifest-sha256"

# The reference-data plane's exact published locations. The host root is the
# shared filesystem the publisher writes to; the mount path is where a consumer
# mounts it read-only. Both are asserted so a receipt cannot redirect this
# runtime at some other filesystem.
REFERENCE_HOST_ROOT = "/mnt/fs2-reference-data/data"
REFERENCE_MOUNT_PATH = "/reference-data"


def assert_independent_identities(content_tree_sha256: str, manifest_sha256: str) -> None:
    """Refuse a pair in which the two reference identities have been conflated.

    The aggregate tree digest and the manifest digest identify two different
    objects, so they must never be equal, derived from one another, or defaulted
    to one another. The publisher enforces the same invariant when it builds a
    receipt; it is re-checked here so a hand-edited receipt cannot slip past.
    """
    if content_tree_sha256 == manifest_sha256:
        raise ContractError(
            "the content tree identity and the manifest identity are equal. They "
            "identify two different objects and must never be equated."
        )


def reject_handoff_aliases(*documents: dict[str, Any]) -> None:
    """Fail closed on an invented handoff field name."""
    for document in documents:
        for alias, actual in HANDOFF_ALIASES.items():
            if alias in document:
                raise ContractError(
                    f"handoff field {alias!r} does not exist; use {actual!r} from the "
                    f"{TERMINAL_RECEIPT_SCHEMA} contract"
                )


def _expect_exact_keys(document: dict[str, Any], expected: frozenset[str], label: str) -> None:
    present = set(document)
    if present != expected:
        missing = sorted(expected - present)
        extra = sorted(present - expected)
        raise ContractError(
            f"{label} must carry exactly {sorted(expected)}; missing {missing}, "
            f"unexpected {extra}"
        )


def validate_terminal_receipt(document: Any) -> dict[str, Any]:
    """Validate the publisher's bounded, content-addressed terminal receipt.

    A consumer binds a mount and a dataset sub-path from this document alone, so
    the receipt carries the aggregate tree digest, an independent manifest
    digest, and an inventory digest with counts, never a file list.
    """
    if not isinstance(document, dict):
        raise ContractError("terminal receipt must be a JSON object")
    _expect_exact_keys(document, TERMINAL_RECEIPT_FIELDS, "terminal receipt")

    if document["schema"] != TERMINAL_RECEIPT_SCHEMA:
        raise ContractError(f"terminal receipt schema must be {TERMINAL_RECEIPT_SCHEMA}")
    if not BUNDLE_ID_RE.fullmatch(str(document["bundle_id"])):
        raise ContractError("terminal receipt bundle id is invalid")
    revision = str(document["revision"])
    if not revision or len(revision) > 160:
        raise ContractError("terminal receipt revision is invalid")
    if not str(document["created_at"]):
        raise ContractError("terminal receipt created_at is invalid")

    storage = document["storage"]
    content = document["content"]
    if not isinstance(storage, dict) or not isinstance(content, dict):
        raise ContractError("terminal receipt storage and content must be objects")
    reject_handoff_aliases(document, storage, content)
    _expect_exact_keys(storage, TERMINAL_STORAGE_FIELDS, "terminal receipt storage")
    _expect_exact_keys(content, TERMINAL_CONTENT_FIELDS, "terminal receipt content")

    for field in ("host_root", "mount_path"):
        value = str(storage[field])
        if not value.startswith("/") or ".." in PurePosixPath(value).parts:
            raise ContractError(
                f"terminal receipt {field} must be an absolute path without traversal"
            )
    if storage["read_only"] is not True:
        raise ContractError("terminal receipt must mount the published tree read-only")
    if str(storage["host_root"]) != REFERENCE_HOST_ROOT:
        raise ContractError(
            f"terminal receipt host_root is {storage['host_root']!r}, but the shared "
            f"reference filesystem is {REFERENCE_HOST_ROOT!r}"
        )
    if str(storage["mount_path"]) != REFERENCE_MOUNT_PATH:
        raise ContractError(
            f"terminal receipt mount_path is {storage['mount_path']!r}, but the read-only "
            f"reference mount is {REFERENCE_MOUNT_PATH!r}"
        )

    for field in ("tree_sha256", "manifest_sha256", "inventory_sha256"):
        if not SHA256_RE.fullmatch(str(content[field])):
            raise ContractError(f"terminal receipt {field} must be a SHA-256 digest")
    assert_independent_identities(str(content["tree_sha256"]), str(content["manifest_sha256"]))
    if content["inventory_marker"] != MANIFEST_MARKER:
        raise ContractError("terminal receipt inventory marker is invalid")
    for field in ("file_count", "expanded_bytes"):
        value = content[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ContractError(f"terminal receipt {field} must be a positive integer")
    if not isinstance(content["inline_inventory"], bool):
        raise ContractError("terminal receipt inline_inventory must be a boolean")
    if content["inline_inventory"] != (content["file_count"] <= MAX_INLINE_INVENTORY_FILES):
        raise ContractError(
            "terminal receipt inline_inventory must agree with the "
            f"{MAX_INLINE_INVENTORY_FILES}-file bound a consumer can validate"
        )

    expected_sub_path = (
        f"datasets/{document['bundle_id']}/{revision}/sha256/{content['tree_sha256']}"
    )
    if str(storage["dataset_sub_path"]) != expected_sub_path:
        raise ContractError(
            "terminal receipt dataset sub-path must bind its /sha256/<tree> component "
            "to the exact aggregate tree digest"
        )

    # The reference stage is CPU-only. A receipt that reserves an accelerator
    # would mean preprocessing had been given a GPU it must never hold.
    placement = document["placement"]
    if not isinstance(placement, dict):
        raise ContractError("terminal receipt placement must be an object")
    if placement.get("resource_class") != "cpu":
        raise ContractError("the reference-data stage placement must stay CPU-only")
    if "accelerator" in placement:
        raise ContractError("the reference-data stage must not reserve an accelerator")
    selector = placement.get("node_selector") or {}
    if not isinstance(selector, dict):
        raise ContractError("terminal receipt node selector must be an object")
    if GPU_SELECTOR_KEYS & set(selector):
        raise ContractError(
            "the reference-data stage must not declare an accelerator node selector"
        )
    return document


def derive_database_root(receipt: dict[str, Any]) -> Path:
    """The read-only in-container path a consumer mounts for this revision.

    Derived only from published receipt fields, exactly as the publisher's own
    derive_database_root does, so the mounted dataset path and its
    ``/sha256/<tree>`` component always agree with the tree digest.
    """
    validated = validate_terminal_receipt(receipt)
    storage = validated["storage"]
    return Path(f"{str(storage['mount_path']).rstrip('/')}/{storage['dataset_sub_path']}")


@dataclass(frozen=True)
class ReferenceBinding:
    """A verified binding to the reference database tree actually mounted.

    Two independent identities are carried and neither is invented here. The
    aggregate tree digest must equal the full name of the mounted directory, and
    the receipt's manifest digest must equal the ``.fs2-manifest-sha256`` marker
    the publisher wrote inside that directory for a complete tree.
    """

    bundle_id: str
    revision: str
    content_tree_sha256: str
    manifest_sha256: str
    inventory_sha256: str
    database_root: Path
    host_root: str
    mount_path: str
    dataset_sub_path: str
    manifest_path: str
    file_count: int
    expanded_bytes: int
    manifest_document_verified: bool

    def as_receipt(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "revision": self.revision,
            "content_tree_sha256": self.content_tree_sha256,
            "manifest_sha256": self.manifest_sha256,
            "inventory_sha256": self.inventory_sha256,
            "identities_are_independent": True,
            "bound_by": "mounted-tree-name-and-marker",
            "marker_matched_receipt": True,
            "manifest_document_verified": self.manifest_document_verified,
            "database_root": str(self.database_root),
            "host_root": self.host_root,
            "mount_path": self.mount_path,
            "dataset_sub_path": self.dataset_sub_path,
            "manifest_path": self.manifest_path,
            "single_root_mount": True,
            "file_count": self.file_count,
            "expanded_bytes": self.expanded_bytes,
        }

    def preprocess_reference_data(self, manifest_uri: str) -> dict[str, Any]:
        """The controller preprocess-request ``reference_data`` object.

        The publisher never invents a manifest location, so the caller supplies
        the URI it published the manifest to and that URI must name the exact
        manifest digest. Mirrors the publisher's derive_preprocess_reference_data.
        """
        parsed = urlparse(manifest_uri)
        if parsed.scheme not in {"file", "s3"}:
            raise ContractError("reference manifest URI must use file or s3")
        if not parsed.path.endswith(f"/{self.manifest_sha256}.json"):
            raise ContractError(
                "reference manifest URI must name the published manifest digest "
                f"{self.manifest_sha256}"
            )
        return {
            "bundle_id": self.bundle_id,
            "revision": self.revision,
            "manifest_uri": manifest_uri,
            "manifest_sha256": self.manifest_sha256,
        }


def read_tree_manifest_marker(database_root: Path) -> str:
    """Read the manifest identity the publisher wrote inside the published tree.

    The marker exists only for a complete tree, so its presence is also the
    publisher's readiness signal.
    """
    marker = database_root / MANIFEST_MARKER
    if not marker.is_file():
        raise ContractError(
            f"mounted reference tree {database_root} has no {MANIFEST_MARKER} marker, so it "
            "is not a complete published tree and must not be used"
        )
    value = marker.read_text(encoding="utf-8").strip()
    if not SHA256_RE.fullmatch(value):
        raise ContractError(
            f"{MANIFEST_MARKER} in {database_root} does not contain a SHA-256 digest"
        )
    return value


def bind_reference_tree(
    receipt: dict[str, Any],
    *,
    mount_root: Path | None = None,
    database_root: Path | None = None,
    manifest_path: Path | None = None,
) -> ReferenceBinding:
    """Bind to the reference tree actually mounted for this receipt.

    The stage mounts **one** read-only root, the shared reference filesystem
    ``/mnt/fs2-reference-data/data`` at ``/reference-data``. Both the published
    dataset and its sibling manifest are resolved from that single root:

    * ``<root>/<dataset_sub_path>`` is the database root
    * ``<root>/manifests/sha256/<manifest_sha256>.json`` is the manifest

    Only the dataset being mounted is not a supported shape, because then the
    manifest could not be verified against the tree it describes. The binding
    rests on the mounted filesystem, not on any string in the receipt: the
    directory's full name must equal the aggregate tree digest, and the marker
    inside it must equal the receipt's independent manifest digest.
    """
    validated = validate_terminal_receipt(receipt)
    storage = validated["storage"]
    content = validated["content"]

    # The single read-only root. Tests and relocated mounts may host it
    # elsewhere; the sub-path, and therefore the content identity, still match.
    root = Path(mount_root) if mount_root is not None else Path(str(storage["mount_path"]))
    dataset_sub_path = str(storage["dataset_sub_path"])
    derived = Path(f"{str(root).rstrip('/')}/{dataset_sub_path}")

    if database_root is not None and Path(database_root) != derived:
        raise ContractError(
            f"database root {database_root} is not the published location for "
            f"{validated['bundle_id']}@{validated['revision']}; expected {derived}"
        )
    database_root = derived

    tree_sha256 = str(content["tree_sha256"])
    manifest_sha256 = str(content["manifest_sha256"])

    if ".." in database_root.parts:
        raise ContractError(f"database root {database_root} must not contain a traversal")
    if database_root.name != tree_sha256:
        raise ContractError(
            f"mounted database root {database_root} is not the published tree: its final "
            f"segment must equal the full aggregate tree digest {tree_sha256}"
        )
    if not database_root.is_dir():
        raise ContractError(
            f"database root {database_root} is not a mounted directory. Mount the whole "
            f"reference root read-only at {storage['mount_path']}, not just the dataset, so "
            "both the dataset sub-path and the sibling manifest resolve."
        )

    marker = read_tree_manifest_marker(database_root)
    if marker != manifest_sha256:
        raise ContractError(
            f"the {MANIFEST_MARKER} marker in {database_root} is {marker}, but the receipt "
            f"binds manifest {manifest_sha256}. The mounted tree is not ready for this "
            "published revision."
        )

    # The manifest is a sibling of the dataset under the same single root.
    resolved_manifest = (
        Path(manifest_path)
        if manifest_path is not None
        else root / "manifests" / "sha256" / f"{manifest_sha256}.json"
    )
    if not resolved_manifest.is_file():
        raise ContractError(
            f"the manifest the receipt binds is not readable at {resolved_manifest}. Mount the "
            f"whole reference root read-only at {storage['mount_path']} so "
            f"manifests/sha256/{manifest_sha256}.json resolves alongside the dataset; mounting "
            "only the dataset is not a supported shape."
        )

    manifest = load_json(resolved_manifest)
    if manifest.get("schema") != REFERENCE_RECEIPT_SCHEMA:
        raise ContractError(
            f"reference manifest {resolved_manifest} declares schema "
            f"{manifest.get('schema')!r}, expected {REFERENCE_RECEIPT_SCHEMA!r}"
        )
    recomputed = manifest_self_digest(manifest)
    if recomputed != manifest_sha256:
        raise ContractError(
            f"reference manifest {resolved_manifest} does not match the receipt identity: "
            f"recomputed {recomputed}, receipt binds {manifest_sha256}"
        )
    manifest_content = manifest.get("content")
    if not isinstance(manifest_content, dict):
        raise ContractError(f"reference manifest {resolved_manifest} has no content block")
    if str(manifest_content.get("tree_sha256")) != tree_sha256:
        raise ContractError(
            f"reference manifest {resolved_manifest} describes a different content tree "
            "than the receipt"
        )
    if str(manifest_content.get("inventory_sha256")) != str(content["inventory_sha256"]):
        raise ContractError(
            f"reference manifest {resolved_manifest} carries a different inventory identity "
            "than the receipt"
        )
    if str(manifest.get("bundle_id")) != str(validated["bundle_id"]) or (
        str(manifest.get("revision")) != str(validated["revision"])
    ):
        raise ContractError(
            f"reference manifest {resolved_manifest} does not bind "
            f"{validated['bundle_id']}@{validated['revision']}"
        )

    return ReferenceBinding(
        bundle_id=str(validated["bundle_id"]),
        revision=str(validated["revision"]),
        content_tree_sha256=tree_sha256,
        manifest_sha256=manifest_sha256,
        inventory_sha256=str(content["inventory_sha256"]),
        database_root=database_root,
        host_root=str(storage["host_root"]),
        mount_path=str(storage["mount_path"]),
        dataset_sub_path=dataset_sub_path,
        manifest_path=str(resolved_manifest),
        file_count=int(content["file_count"]),
        expanded_bytes=int(content["expanded_bytes"]),
        manifest_document_verified=True,
    )


# How deep a published dataset sits under a whole reference root:
# datasets/<bundle>/<revision>/sha256/<tree>.
DATASET_GLOB = "datasets/*/*/sha256/*"


def reference_databases_present(database_root: Path) -> list[str]:
    """Evidence that a reference-data tree is bound at or under ``database_root``.

    A GPU inference stage must carry no reference databases, and the canonical
    mount is the *whole* reference root, where the databases sit several levels
    down at ``datasets/<bundle>/<revision>/sha256/<tree>/``. Checking only the
    filenames directly under the mount would therefore miss the canonical shape
    entirely, so both the root layout and a directly mounted dataset are probed.

    The probes are bounded stats and one shallow glob, never a walk, because the
    published tree can be hundreds of gigabytes.
    """
    if not database_root.is_dir():
        return []

    found: list[str] = []

    # A whole reference root: its own directory layout is the giveaway.
    if (database_root / "datasets").is_dir():
        found.append("datasets/")
    if (database_root / "manifests" / "sha256").is_dir():
        found.append("manifests/sha256/")
    try:
        for candidate in sorted(database_root.glob(DATASET_GLOB)):
            if candidate.is_dir():
                found.append(f"datasets/.../{candidate.name}")
                break
    except OSError:
        pass

    # A directly mounted dataset tree.
    if (database_root / MANIFEST_MARKER).is_file():
        found.append(MANIFEST_MARKER)
    found.extend(name for name in REFERENCE_DB_FILENAMES if (database_root / name).exists())
    if (database_root / "mmcif_files").is_dir():
        found.append("mmcif_files")

    return sorted(set(found))


# ---------------------------------------------------------------------------
# Stage separation
# ---------------------------------------------------------------------------


@dataclass
class StageBindings:
    """What a single stage is actually allowed to touch."""

    stage: str
    parameters_bound: bool
    reference_bound: bool

    def enforce(self) -> None:
        if self.parameters_bound and self.reference_bound:
            raise ContractError(
                f"stage {self.stage!r} declares both the licensed parameter binding and a "
                "reference-database binding. The platform runs data preprocessing as a CPU "
                "stage and inference as a GPU stage, so a single stage must never hold both. "
                "Split the work into a CPU data Job and a GPU inference Job."
            )
        if self.stage == "data" and self.parameters_bound:
            raise ContractError(
                "the CPU data stage must not bind the licensed parameters; it does not run "
                "inference and would hold licensed bytes with no reason to read them"
            )
        if self.stage == "inference" and self.reference_bound:
            raise ContractError(
                "the GPU inference stage must not bind the reference databases; it consumes "
                "the immutable data-pipeline handoff and the published manifest digests only"
            )


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------


@dataclass
class CacheReport:
    """Truthful description of the auxiliary compiler cache state."""

    root: str
    jax_dir: str | None
    triton_dir: str | None
    xdg_dir: str | None
    writable: bool
    degraded_reason: str | None = None

    def as_receipt(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "jax_compilation_cache_dir": self.jax_dir,
            "triton_cache_dir": self.triton_dir,
            "xdg_cache_dir": self.xdg_dir,
            "writable": self.writable,
            "degraded_reason": self.degraded_reason,
            "kind": "xla-and-triton-compilation-cache",
            "is_gpu_snapshot": False,
            "cache_level": "auxiliary-compiler-cache",
            "note": (
                "Compilation artefacts only. This is not a GPU memory snapshot and must "
                "not be reported as one. Image-local content is the L1 level; no higher "
                "level is claimed without measured evidence."
            ),
        }


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        with tempfile.NamedTemporaryFile(dir=path, prefix=".fs2-probe-", delete=True):
            return True
    except OSError:
        return False


def prepare_caches(environ: dict[str, str] | None = None) -> CacheReport:
    """Prepare the auxiliary compiler cache, degrading truthfully if read-only.

    A non-writable compiler cache slows a run down but does not make it wrong,
    so the run continues without a cache rather than failing. The degradation is
    always reported so nobody can mistake an uncached run for a cached one.
    """
    env = os.environ if environ is None else environ
    root = env.get("FS2_AF3_CACHE_ROOT", "/cache/alphafold3")
    jax_dir = env.get("FS2_AF3_JAX_CACHE_DIR", f"{root}/jax")
    triton_dir = env.get("FS2_AF3_TRITON_CACHE_DIR", f"{root}/triton")
    xdg_dir = env.get("FS2_AF3_XDG_CACHE_DIR", f"{root}/xdg")

    unwritable = [name for name in (jax_dir, triton_dir, xdg_dir) if not _is_writable_dir(Path(name))]
    if unwritable:
        return CacheReport(
            root=root,
            jax_dir=None,
            triton_dir=None,
            xdg_dir=None,
            writable=False,
            degraded_reason=(
                "not writable: " + ", ".join(sorted(unwritable)) + ". Running without a "
                "persistent compilation cache; every kernel is recompiled in this pod."
            ),
        )
    return CacheReport(
        root=root, jax_dir=jax_dir, triton_dir=triton_dir, xdg_dir=xdg_dir, writable=True
    )


def cache_environment(report: CacheReport) -> dict[str, str]:
    """Environment additions that point the toolchain at the prepared cache."""
    if not report.writable:
        return {}
    return {
        "TRITON_CACHE_DIR": str(report.triton_dir),
        "XDG_CACHE_HOME": str(report.xdg_dir),
    }


# ---------------------------------------------------------------------------
# argv composition
# ---------------------------------------------------------------------------


@dataclass
class RunPlan:
    """A composed, inspectable AlphaFold 3 invocation."""

    stage: str
    argv: list[str]
    environment: dict[str, str] = field(default_factory=dict)

    def as_receipt(self) -> dict[str, Any]:
        return {"stage": self.stage, "argv": list(self.argv), "environment_additions": dict(self.environment)}


# The image's own interpreter. Composed argv must name it explicitly rather than
# inheriting whatever interpreter happens to be running, so a plan produced
# outside the image still describes exactly what the image would execute.
DEFAULT_INTERPRETER = "/alphafold3_venv/bin/python3"


def _interpreter() -> str:
    return os.environ.get("FS2_AF3_INTERPRETER") or DEFAULT_INTERPRETER


def _run_script() -> str:
    return os.environ.get("FS2_AF3_RUN_SCRIPT", "/app/alphafold/run_alphafold.py")


# Extra arguments are governed by a positive allowlist. A denylist cannot be
# complete: Abseil resolves --flagfile by reading flags out of a file, so any
# denied flag could be smuggled back in through one indirection, and new
# upstream flags would default to allowed.
#
# Every name below is a reviewed AlphaFold 3 tuning knob that changes how much
# work a stage does, never what the stage is or where it reads and writes.
ALLOWED_EXTRA_FLAGS = frozenset(
    {
        "buckets",
        "compress_large_output_files",
        "conformer_max_iterations",
        "fix_standalone_glycans",
        "gpu_device",
        "jackhmmer_max_parallel_shards",
        "max_template_date",
        "nhmmer_max_parallel_shards",
        "num_diffusion_samples",
        "num_recycles",
        "num_seeds",
        "resolve_msa_overlaps",
        "save_distogram",
        "save_embeddings",
        "save_terms_of_use",
    }
)

# Parser and meta indirections. These never reach the model; they change how the
# command line itself is interpreted, which is exactly how an allowlist would be
# bypassed. Named explicitly so the refusal says why.
PARSER_META_FLAGS = frozenset(
    {
        "flagfile",
        "undefok",
        "help",
        "helpfull",
        "helpshort",
        "helpxml",
        "only_check_args",
        "pdb",
        "pdb_post_mortem",
        "profile_file",
        "run_with_pdb",
        "run_with_profiling",
        "use_cprofile_for_profiling",
    }
)

# Flags the runtime composes itself, which decide what a stage is.
STAGE_CRITICAL_FLAGS = frozenset(
    {
        "run_inference",
        "run_data_pipeline",
        "model_dir",
        "db_dir",
        "json_path",
        "input_dir",
        "output_dir",
        "jax_compilation_cache_dir",
        "flash_attention_implementation",
        "jackhmmer_n_cpu",
        "nhmmer_n_cpu",
    }
)


def _flag_name(item: str) -> str:
    """The bare flag name of an argv item, with absl's ``no`` negation removed."""
    name = item.lstrip("-").split("=", 1)[0]
    if name.startswith("no") and name[2:] in (STAGE_CRITICAL_FLAGS | ALLOWED_EXTRA_FLAGS):
        return name[2:]
    return name


def validate_extra_args(extra: Sequence[str], composed: Sequence[str]) -> list[str]:
    """Admit only reviewed tuning flags, and only once each.

    Extra arguments exist so a caller can trade accuracy for time. They must
    never reach a flag the stage already set, never reintroduce a stage-critical
    flag, and never be able to pull more flags in from somewhere else.
    """
    already = {_flag_name(item) for item in composed if item.startswith("--")}
    seen: set[str] = set()
    for item in extra:
        stripped = item.lstrip("-")
        bare = stripped.split("=", 1)[0]
        if bare in PARSER_META_FLAGS:
            raise ContractError(
                f"extra argument {item!r} is a command-line parser directive, not a model "
                "option. It could introduce flags this runtime never reviewed, so it is "
                "refused outright."
            )
        if not item.startswith("--"):
            raise ContractError(
                f"extra argument {item!r} is not a flag; pass it as --name or --name=value"
            )
        name = _flag_name(item)
        if name in STAGE_CRITICAL_FLAGS:
            raise ContractError(
                f"extra argument {item!r} targets the stage-critical flag {name!r}, which "
                "decides what this stage is and is set by the runtime. It cannot be "
                "overridden."
            )
        if name not in ALLOWED_EXTRA_FLAGS:
            raise ContractError(
                f"extra argument {item!r} is not a reviewed tuning flag. Allowed flags are "
                f"{sorted(ALLOWED_EXTRA_FLAGS)}."
            )
        if name in already:
            raise ContractError(
                f"extra argument {item!r} duplicates the already composed flag {name!r}"
            )
        if name in seen:
            raise ContractError(f"extra argument {item!r} is passed more than once")
        seen.add(name)
    return list(extra)


# The producer's own bounds for a preprocessing thread count.
MIN_THREADS = 1
MAX_THREADS = 128


def resolve_msa_threads(threads: int, cpu_request: int | None) -> int:
    """Validate the controller-frozen MSA thread count against the CPU envelope.

    AlphaFold 3 defaults both MSA tools to ``min(cpu_count, 8)``, which is read
    from the *node* rather than from the pod's CPU request. On the canonical
    six-CPU preprocessing pod that silently oversubscribes the cgroup by eight
    threads per tool. The thread count is therefore always passed explicitly and
    is never allowed to exceed the requested CPUs.
    """
    if not isinstance(threads, int) or isinstance(threads, bool):
        raise ContractError("MSA thread count must be an integer")
    if not MIN_THREADS <= threads <= MAX_THREADS:
        raise ContractError(
            f"MSA thread count {threads} is outside the supported range "
            f"{MIN_THREADS}-{MAX_THREADS}"
        )
    if cpu_request is not None:
        if not isinstance(cpu_request, int) or isinstance(cpu_request, bool) or cpu_request < 1:
            raise ContractError("CPU request must be a positive integer")
        if threads > cpu_request:
            raise ContractError(
                f"MSA thread count {threads} exceeds the stage CPU request {cpu_request}. "
                "The data stage must not oversubscribe its cgroup; lower the frozen thread "
                "count or raise the CPU request."
            )
    return threads


def compose_data_argv(
    *,
    json_path: Path,
    output_dir: Path,
    database_root: Path,
    cache: CacheReport,
    threads: int,
    cpu_request: int | None = None,
    extra: Sequence[str] = (),
) -> RunPlan:
    """CPU stage: MSA and template search only, never inference.

    ``--norun_inference`` is not optional here. It is what makes this stage a
    CPU stage, and it is what lets the stage run with no parameters bound. Both
    MSA thread flags are always emitted so the stage cannot fall back to
    upstream's node-derived default.
    """
    resolved = resolve_msa_threads(threads, cpu_request)
    argv = [
        _interpreter(),
        _run_script(),
        f"--json_path={json_path}",
        f"--output_dir={output_dir}",
        f"--db_dir={database_root}",
        "--norun_inference",
        f"--jackhmmer_n_cpu={resolved}",
        f"--nhmmer_n_cpu={resolved}",
    ]
    argv.extend(validate_extra_args(extra, argv))
    return RunPlan(stage="data", argv=argv, environment=cache_environment(cache))


def compose_inference_argv(
    *,
    json_path: Path,
    output_dir: Path,
    model_dir: Path,
    cache: CacheReport,
    flash_attention: str = "triton",
    extra: Sequence[str] = (),
) -> RunPlan:
    """GPU stage: inference from the licensed parameters and the CPU handoff.

    ``--norun_data_pipeline`` is not optional here. The GPU stage consumes the
    immutable JSON the CPU stage produced, so it never performs a database
    search and never needs a reference database mounted.
    """
    argv = [
        _interpreter(),
        _run_script(),
        f"--json_path={json_path}",
        f"--output_dir={output_dir}",
        f"--model_dir={model_dir}",
        "--norun_data_pipeline",
        f"--flash_attention_implementation={flash_attention}",
    ]
    if cache.writable and cache.jax_dir:
        argv.append(f"--jax_compilation_cache_dir={cache.jax_dir}")
    argv.extend(validate_extra_args(extra, argv))
    return RunPlan(stage="inference", argv=argv, environment=cache_environment(cache))


# ---------------------------------------------------------------------------
# Image self-identity and semantic probes
# ---------------------------------------------------------------------------


def image_identity() -> dict[str, Any]:
    """What this image claims to be, read from its own baked-in lock."""
    lock = load_json(SOURCE_LOCK_PATH)
    upstream = lock.get("upstream", {})
    return {
        "runtime_id": lock.get("runtime_id"),
        "upstream_version": upstream.get("version"),
        "upstream_commit": upstream.get("commit"),
        "upstream_tree": upstream.get("tree"),
        "upstream_release_tag": upstream.get("release_tag"),
        "source_license": upstream.get("source_license"),
        "parameters_embedded": False,
        "reference_databases_embedded": False,
    }


def verify_image_hygiene(roots: Iterable[Path] | None = None) -> dict[str, Any]:
    """Prove at run time that the image carries no licensed or database payload."""
    search_roots = [Path(p) for p in (roots or ("/opt", "/app", "/alphafold3_venv", "/hmmer"))]
    offenders: list[str] = []
    for root in search_roots:
        if not root.exists():
            continue
        for candidate in root.rglob("*"):
            if not candidate.is_file():
                continue
            name = candidate.name
            if name.endswith(".bin.zst") or name.startswith("af3.bin") or name in REFERENCE_DB_FILENAMES:
                offenders.append(str(candidate))
    if offenders:
        raise ContractError(
            "image carries payload that must never be embedded: " + ", ".join(sorted(offenders))
        )
    return {"searched_roots": [str(p) for p in search_roots], "embedded_payload_found": False}


def probe_distribution(expect_version: str) -> dict[str, Any]:
    """Import the installed AlphaFold 3 distribution and assert its version."""
    import importlib.metadata as metadata

    version = metadata.version("alphafold3")
    if expect_version and version != expect_version:
        raise ContractError(
            f"installed alphafold3 distribution is {version}, expected {expect_version}"
        )
    from alphafold3.model import params as af3_params  # noqa: F401  (import is the check)

    return {
        "distribution_version": version,
        "params_module": "alphafold3.model.params",
        "params_entrypoint": "get_model_haiku_params",
        "entrypoint_present": hasattr(af3_params, "get_model_haiku_params"),
    }


def probe_devices() -> dict[str, Any]:
    """Report the JAX devices actually visible to this process."""
    import jax

    devices = jax.devices()
    return {
        "jax_version": getattr(jax, "__version__", None),
        "device_count": len(devices),
        "devices": [
            {
                "kind": device.platform,
                "device_kind": getattr(device, "device_kind", None),
                "id": device.id,
            }
            for device in devices
        ],
        "gpu_present": any(device.platform == "gpu" for device in devices),
    }


def load_parameters_semantically(model_dir: Path, expect: ParameterExpectation) -> dict[str, Any]:
    """Load the parameters through AlphaFold 3's own official loader.

    This is the semantic check: it uses the upstream entrypoint rather than a
    private reimplementation, so a pass means the real code path works.
    """
    from alphafold3.model import params as af3_params

    model_files, is_compressed = af3_params.select_model_files(str(model_dir))
    parameters = af3_params.get_model_haiku_params(model_dir=str(model_dir))

    scopes = len(parameters)
    arrays = 0
    elements = 0
    device_kinds: set[str] = set()
    for scope in parameters.values():
        for array in scope.values():
            arrays += 1
            elements += int(getattr(array, "size", 0))
            device = getattr(array, "device", None)
            if device is not None:
                device_kinds.add(str(device))

    if arrays < expect.expect_min_parameter_arrays:
        raise ContractError(
            f"parameter load produced {arrays} arrays, fewer than the required minimum "
            f"{expect.expect_min_parameter_arrays}; the parameters did not load correctly"
        )

    return {
        "selected_files": [Path(str(item)).name for item in model_files],
        "compressed": bool(is_compressed),
        "loaded_scopes": scopes,
        "loaded_parameter_arrays": arrays,
        "loaded_parameter_elements": elements,
        "parameter_devices": sorted(device_kinds),
    }


DATA_HANDOFF_SCHEMA = "fs2-serve.nebius.ai/alphafold3-data-handoff/v1"
DATA_HANDOFF_DIRNAME = "fs2-af3-handoff"
DATA_HANDOFF_INDEX = "index.json"
DATA_OUTPUT_SUFFIX = "_data.json"
MAX_DATA_HANDOFF_FILES = 64
# The controller reserves the final MiB of its 256 MiB materializer contract
# for deterministic tar headers, padding and end records (at most 65 members).
MAX_DATA_HANDOFF_BYTES = 255 * 1024 * 1024
MAX_DATA_HANDOFF_METADATA_BYTES = 1024 * 1024


def build_data_handoff(output_dir: Path) -> dict[str, Any]:
    """Package the data pipeline's outputs into one portable handoff directory.

    Upstream writes ``<output_dir>/<sanitized_name>/<sanitized_name>_data.json``
    and produces one such file per fold job, with names that depend on its own
    sanitization. Two things therefore have to happen before a GPU pod can use
    the result.

    First it is *packaged*: every produced payload is copied under a single
    handoff directory, so the GPU stage needs one artifact mount rather than the
    whole CPU output tree.

    Second it is made *portable*: the index records paths relative to the handoff
    directory only. A CPU pod's absolute ``/output`` path is meaningless in the
    GPU pod, so no absolute path is ever written into the index or reused later.
    """
    if not output_dir.is_dir():
        raise ContractError(f"data stage output directory {output_dir} does not exist")

    handoff_dir = output_dir / DATA_HANDOFF_DIRNAME
    produced = [
        path
        for path in sorted(output_dir.rglob(f"*{DATA_OUTPUT_SUFFIX}"))
        if path.is_file() and DATA_HANDOFF_DIRNAME not in path.parts
    ]
    if not produced:
        raise ContractError(
            f"the data stage produced no {DATA_OUTPUT_SUFFIX} output under {output_dir}; "
            "there is nothing for the inference stage to consume"
        )
    if len(produced) > MAX_DATA_HANDOFF_FILES:
        raise ContractError(
            f"the data stage produced {len(produced)} fold jobs; "
            f"the handoff limit is {MAX_DATA_HANDOFF_FILES}"
        )

    if handoff_dir.exists():
        shutil.rmtree(handoff_dir)
    handoff_dir.mkdir(parents=True)

    entries: list[dict[str, Any]] = []
    payload_bytes = 0
    for path in produced:
        fold_job = path.name[: -len(DATA_OUTPUT_SUFFIX)]
        relative = f"{fold_job}/{path.name}"
        destination = handoff_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_size = path.stat().st_size
        if source_size < 1 or payload_bytes + source_size > MAX_DATA_HANDOFF_BYTES:
            raise ContractError(
                f"the data handoff payload exceeds the {MAX_DATA_HANDOFF_BYTES}-byte bound"
            )
        shutil.copyfile(path, destination)
        digest, size = sha256_of_file(destination)
        if size != source_size:
            raise ContractError(f"data handoff payload changed while copied: {path}")
        payload_bytes += size
        entries.append(
            {
                "fold_job": fold_job,
                "relative_path": relative,
                "bytes": size,
                "sha256": digest,
            }
        )

    names = [entry["fold_job"] for entry in entries]
    if len(set(names)) != len(names):
        raise ContractError(
            "the data stage produced two fold jobs with the same sanitized name: "
            f"{sorted(names)}. The handoff cannot address them unambiguously."
        )

    index = {
        "schema": DATA_HANDOFF_SCHEMA,
        "count": len(entries),
        "fold_jobs": names,
        "entries": entries,
        "paths_are_relative_to": "the directory containing this index",
    }
    payload = json.dumps(index, indent=2, sort_keys=True) + "\n"
    payload_size = len(payload.encode("utf-8"))
    if payload_size > MAX_DATA_HANDOFF_METADATA_BYTES:
        raise ContractError("the data handoff index exceeds the metadata byte bound")
    if payload_bytes + payload_size > MAX_DATA_HANDOFF_BYTES:
        raise ContractError(
            f"the data handoff plus index exceeds the {MAX_DATA_HANDOFF_BYTES}-byte bound"
        )
    (handoff_dir / DATA_HANDOFF_INDEX).write_text(payload, encoding="utf-8")

    return {
        **index,
        "handoff_dirname": DATA_HANDOFF_DIRNAME,
        "index_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "note": (
            "Self-contained and relocatable. Mount this directory into the GPU stage and pass "
            "--handoff-dir; the inference stage reconstructs every path under its own mount "
            "and verifies each payload digest. No absolute path from the CPU pod is recorded."
        ),
    }


def load_data_handoff(handoff_dir: Path, fold_job: str | None = None) -> dict[str, Any]:
    """Resolve one fold job's payload under *this* pod's handoff mount.

    Paths are reconstructed from the mount the GPU stage was given, never from
    anything the CPU stage recorded, and the payload digest is verified before
    it is handed to AlphaFold 3.
    """
    index_path = handoff_dir / DATA_HANDOFF_INDEX
    if not index_path.is_file():
        raise ContractError(
            f"handoff directory {handoff_dir} has no {DATA_HANDOFF_INDEX}. Mount the "
            "directory the data stage packaged, not the raw output tree."
        )
    index = load_json(index_path)
    if index.get("schema") != DATA_HANDOFF_SCHEMA:
        raise ContractError(
            f"{index_path} declares schema {index.get('schema')!r}, expected "
            f"{DATA_HANDOFF_SCHEMA!r}"
        )
    entries = index.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ContractError(f"{index_path} lists no data-pipeline output")

    by_job = {str(entry.get("fold_job")): entry for entry in entries}
    if fold_job is None:
        if len(entries) != 1:
            raise ContractError(
                f"the handoff contains {len(entries)} fold jobs "
                f"({', '.join(sorted(by_job))}); pass --fold-job to choose one"
            )
        selected = entries[0]
    else:
        if fold_job not in by_job:
            raise ContractError(
                f"fold job {fold_job!r} is not in the handoff; available jobs are "
                f"{', '.join(sorted(by_job))}"
            )
        selected = by_job[fold_job]

    relative = str(selected.get("relative_path", ""))
    if not relative or relative.startswith("/") or ".." in PurePosixPath(relative).parts:
        raise ContractError(
            f"handoff entry {selected.get('fold_job')!r} has a non-relative or traversing "
            f"path {relative!r}; a handoff must never carry an absolute producer path"
        )

    resolved = handoff_dir / relative
    if not resolved.is_file():
        raise ContractError(
            f"handoff entry {selected.get('fold_job')!r} points at {relative}, which is not "
            f"present under {handoff_dir}. The packaged payload is incomplete."
        )
    digest, size = sha256_of_file(resolved)
    if digest != str(selected.get("sha256")):
        raise ContractError(
            f"handoff payload {relative} digest mismatch: found {digest}, index records "
            f"{selected.get('sha256')}"
        )
    if size != int(selected.get("bytes", -1)):
        raise ContractError(f"handoff payload {relative} size does not match the index")

    return {
        "fold_job": str(selected["fold_job"]),
        "json_path": resolved,
        "relative_path": relative,
        "sha256": digest,
        "bytes": size,
        "available_fold_jobs": sorted(by_job),
        "selected_from": str(index_path),
    }


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


def emit(document: dict[str, Any], destination: Path | None) -> None:
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(payload)
    if destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=f".{destination.name}.",
                suffix=".partial", dir=destination.parent, delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, destination)
            temporary_path = None
            directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def base_receipt(mode: str) -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "mode": mode,
        "image": image_identity(),
        "status": "PASS",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parameter_path(args: argparse.Namespace) -> Path:
    return Path(
        args.parameter_path
        or os.environ.get("FS2_AF3_PARAMETER_PATH", "/models/af3.bin.zst")
    )


def _database_root(args: argparse.Namespace) -> Path:
    """The reference mount to scan when proving a GPU stage has no tree bound."""
    return Path(
        args.database_root
        or os.environ.get("FS2_AF3_REFERENCE_MOUNT", REFERENCE_MOUNT_PATH)
    )


def _output_dir(args: argparse.Namespace) -> Path:
    return Path(args.output_dir or os.environ.get("FS2_AF3_OUTPUT_DIR", "/output"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="af3-runtime",
        description=(
            "fs2 AlphaFold 3 v3.0.4 academic runtime. Verifies the licensed parameter "
            "binding and the published reference-data identities before running, and keeps "
            "CPU data preprocessing and GPU inference as separate stages."
        ),
    )
    parser.add_argument(
        "mode",
        choices=("verify", "smoke", "params-load", "data", "inference", "plan"),
        help=(
            "verify: identity and hygiene only. smoke: import and CLI probe, no parameters. "
            "params-load: real semantic parameter load. data: CPU preprocessing stage. "
            "inference: GPU inference stage. plan: compose and print a stage's argv "
            "without running it."
        ),
    )
    parser.add_argument(
        "--stage",
        choices=("data", "inference"),
        help="Which stage to compose. Required by, and only used by, plan mode.",
    )
    parser.add_argument("--parameter-path", help="Path of the mounted parameter object")
    parser.add_argument(
        "--database-root",
        help=(
            "Optional explicit database_root. Must equal the location derived from the "
            "terminal receipt, so it can only confirm the mount, never redirect it."
        ),
    )
    parser.add_argument(
        "--reference-manifest",
        help=(
            "Optional localized published manifest document. When given, its canonical "
            "digest is recomputed and must equal the receipt's manifest identity."
        ),
    )
    parser.add_argument(
        "--reference-receipt",
        help=(
            "The reference-data worker's terminal publication receipt for the bundle"
        ),
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=(
            int(os.environ["FS2_AF3_THREADS"]) if os.environ.get("FS2_AF3_THREADS") else None
        ),
        help=(
            "Controller-frozen MSA thread count for the CPU stage. Drives both "
            "--jackhmmer_n_cpu and --nhmmer_n_cpu. Required by the data stage."
        ),
    )
    parser.add_argument(
        "--cpu-request",
        type=int,
        default=(
            int(os.environ["FS2_AF3_CPU_REQUEST"])
            if os.environ.get("FS2_AF3_CPU_REQUEST")
            else None
        ),
        help="The stage's CPU request, used to reject a thread count that oversubscribes it",
    )
    parser.add_argument(
        "--manifest-uri",
        help="The file or s3 URI the reference worker published the manifest to",
    )
    parser.add_argument(
        "--emit-preprocess-reference",
        action="store_true",
        help=(
            "Also emit the controller preprocess-request reference_data object derived "
            "from the receipt and the mounted tree"
        ),
    )
    parser.add_argument(
        "--json-path",
        help=(
            "AlphaFold 3 fold input JSON. For the GPU stage this is the direct alternative "
            "to --handoff-dir."
        ),
    )
    parser.add_argument(
        "--handoff-dir",
        help=(
            "Directory the data stage packaged, mounted into this pod. Paths are "
            "reconstructed under this mount and every payload digest is verified."
        ),
    )
    parser.add_argument(
        "--fold-job",
        help="Which fold job to run when the handoff contains more than one",
    )
    parser.add_argument("--output-dir", help="Directory for AlphaFold 3 output")
    parser.add_argument("--receipt", help="Also write the JSON receipt to this path")
    parser.add_argument(
        "--deep-verify",
        action="store_true",
        help="Additionally verify the decompressed parameter identity; reads 1.1 GB",
    )
    parser.add_argument(
        "--flash-attention",
        default=os.environ.get("FS2_AF3_FLASH_ATTENTION", "triton"),
        choices=("triton", "cudnn", "xla"),
        help="Flash attention implementation for the GPU stage",
    )
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="Extra flag passed verbatim to run_alphafold.py; repeatable",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Compose and report, but do not execute"
    )
    return parser


def _run(plan: RunPlan) -> int:
    environment = dict(os.environ)
    environment.update(plan.environment)
    if shutil.which(plan.argv[0]) is None and not Path(plan.argv[0]).exists():
        raise ContractError(f"interpreter {plan.argv[0]} is not present in this image")
    completed = subprocess.run(plan.argv, env=environment, check=False)
    return completed.returncode


def _plan_data_stage(args: argparse.Namespace, expect: ParameterExpectation) -> dict[str, Any]:
    """CPU preprocessing stage: reference databases in, immutable handoff out."""
    reference_source = args.reference_receipt or os.environ.get("FS2_AF3_REFERENCE_RECEIPT")
    if not reference_source:
        raise ContractError(
            "the CPU data stage requires --reference-receipt pointing at the terminal "
            "receipt the reference-data worker generated for the bundle. Until that worker "
            "publishes the bundle, this stage must not run."
        )
    manifest_override = args.reference_manifest or os.environ.get("FS2_AF3_REFERENCE_MANIFEST")
    binding = bind_reference_tree(
        load_json(Path(reference_source)),
        database_root=Path(args.database_root) if args.database_root else None,
        manifest_path=Path(manifest_override) if manifest_override else None,
    )
    database_root = binding.database_root
    parameter_path = _parameter_path(args)
    StageBindings(
        stage="data",
        parameters_bound=parameter_path.exists(),
        reference_bound=True,
    ).enforce()
    if not args.json_path:
        raise ContractError("the data stage requires --json-path")
    if args.threads is None:
        raise ContractError(
            "the CPU data stage requires --threads, the controller-frozen MSA thread count. "
            "Without it AlphaFold 3 would derive its own default from the node's CPU count "
            "and oversubscribe the pod."
        )
    cache = prepare_caches()
    plan = compose_data_argv(
        json_path=Path(args.json_path),
        output_dir=_output_dir(args),
        database_root=database_root,
        cache=cache,
        threads=args.threads,
        cpu_request=args.cpu_request,
        extra=args.extra_arg,
    )
    result = {
        "reference_data": binding.as_receipt(),
        "cache": cache.as_receipt(),
        "plan": plan.as_receipt(),
        "cpu_envelope": {
            "msa_threads": args.threads,
            "cpu_request": args.cpu_request,
            "jackhmmer_n_cpu": args.threads,
            "nhmmer_n_cpu": args.threads,
            "upstream_default_overridden": True,
            "upstream_default": "min(cpu_count, 8), derived from the node rather than the pod",
        },
    }
    if args.emit_preprocess_reference:
        if not args.manifest_uri:
            raise ContractError(
                "--emit-preprocess-reference requires --manifest-uri, because the publisher "
                "never invents a manifest location; the caller supplies the URI it published "
                "the manifest to"
            )
        result["preprocess_reference_data"] = binding.preprocess_reference_data(args.manifest_uri)
    return result


def _plan_inference_stage(
    args: argparse.Namespace, expect: ParameterExpectation
) -> dict[str, Any]:
    """GPU inference stage: licensed parameters plus the CPU handoff, no databases."""
    parameter_path = _parameter_path(args)
    database_root = _database_root(args)
    StageBindings(
        stage="inference",
        parameters_bound=True,
        reference_bound=bool(reference_databases_present(database_root)),
    ).enforce()
    handoff_source = args.handoff_dir or os.environ.get("FS2_AF3_HANDOFF_DIR")
    if bool(args.json_path) == bool(handoff_source):
        raise ContractError(
            "the GPU inference stage requires exactly one of --handoff-dir, the directory the "
            "data stage packaged, or --json-path for a fold input that already carries its MSAs"
        )

    handoff: dict[str, Any] | None = None
    if handoff_source:
        handoff = load_data_handoff(Path(handoff_source), args.fold_job)
        json_path = handoff["json_path"]
    else:
        json_path = Path(args.json_path)

    parameters = verify_parameter_artifact(parameter_path, expect, deep=args.deep_verify)
    model_dir, candidates = resolve_model_dir(parameter_path)
    cache = prepare_caches()
    plan = compose_inference_argv(
        json_path=json_path,
        output_dir=_output_dir(args),
        model_dir=model_dir,
        cache=cache,
        flash_attention=args.flash_attention,
        extra=args.extra_arg,
    )
    result = {
        "parameters": parameters,
        "model_dir": {"path": str(model_dir), "candidates": candidates},
        "cache": cache.as_receipt(),
        "plan": plan.as_receipt(),
    }
    if handoff is not None:
        result["handoff_input"] = {
            "fold_job": handoff["fold_job"],
            "relative_path": handoff["relative_path"],
            "sha256": handoff["sha256"],
            "bytes": handoff["bytes"],
            "available_fold_jobs": handoff["available_fold_jobs"],
            "resolved_under_mount": str(Path(handoff_source)),
            "reconstructed_locally": True,
        }
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    receipt_path = Path(args.receipt) if args.receipt else None

    try:
        binding_contract = load_json(PARAMETER_BINDING_PATH)
        expect = ParameterExpectation.from_contract(binding_contract)
        receipt = base_receipt(args.mode)

        if args.mode == "verify":
            receipt["hygiene"] = verify_image_hygiene()
            parameter_path = _parameter_path(args)
            if parameter_path.exists():
                receipt["parameters"] = verify_parameter_artifact(
                    parameter_path, expect, deep=args.deep_verify
                )
                model_dir, candidates = resolve_model_dir(parameter_path)
                receipt["model_dir"] = {"path": str(model_dir), "candidates": candidates}
            else:
                receipt["parameters"] = {"bound": False, "expected_path": str(parameter_path)}
            receipt["cache"] = prepare_caches().as_receipt()
            emit(receipt, receipt_path)
            return 0

        if args.mode == "smoke":
            receipt["hygiene"] = verify_image_hygiene()
            receipt["distribution"] = probe_distribution(expect.expect_distribution_version)
            receipt["devices"] = probe_devices()
            receipt["cache"] = prepare_caches().as_receipt()
            emit(receipt, receipt_path)
            return 0

        if args.mode == "params-load":
            parameter_path = _parameter_path(args)
            StageBindings(
                stage="inference",
                parameters_bound=True,
                reference_bound=bool(reference_databases_present(_database_root(args))),
            ).enforce()
            receipt["hygiene"] = verify_image_hygiene()
            receipt["distribution"] = probe_distribution(expect.expect_distribution_version)
            receipt["parameters"] = verify_parameter_artifact(
                parameter_path, expect, deep=args.deep_verify
            )
            model_dir, candidates = resolve_model_dir(parameter_path)
            receipt["model_dir"] = {"path": str(model_dir), "candidates": candidates}
            cache = prepare_caches()
            receipt["cache"] = cache.as_receipt()
            receipt["devices"] = probe_devices()
            receipt["semantic"] = load_parameters_semantically(model_dir, expect)
            emit(receipt, receipt_path)
            return 0

        stage = args.stage if args.mode == "plan" else args.mode
        if args.mode == "plan" and stage is None:
            raise ContractError("plan mode requires --stage data or --stage inference")

        if stage == "data":
            receipt.update(_plan_data_stage(args, expect))
        elif stage == "inference":
            receipt.update(_plan_inference_stage(args, expect))
        else:
            raise ContractError(f"unsupported mode {args.mode!r}")

        if args.mode == "plan" or args.dry_run:
            receipt["status"] = "PLANNED"
            emit(receipt, receipt_path)
            return 0

        # Run upstream first, then emit exactly one terminal receipt. Emitting a
        # PASS before run_alphafold.py has exited would leave a success receipt
        # behind for a run that failed.
        exit_code = _run(
            RunPlan(
                stage=stage,
                argv=receipt["plan"]["argv"],
                environment=receipt["plan"]["environment_additions"],
            )
        )
        receipt["execution"] = {
            "upstream": receipt["plan"]["argv"][1],
            "exit_code": exit_code,
            "terminal_state": "succeeded" if exit_code == 0 else "failed",
        }
        if exit_code != 0:
            receipt["status"] = "FAIL"
            receipt["error"] = (
                f"run_alphafold.py exited {exit_code}; the {stage} stage did not complete"
            )
            emit(receipt, receipt_path)
            return exit_code

        if stage == "data":
            receipt["handoff"] = build_data_handoff(_output_dir(args))

        receipt["status"] = "PASS"
        emit(receipt, receipt_path)
        return 0

    except ContractError as error:
        emit(
            {
                "schema": RECEIPT_SCHEMA,
                "mode": args.mode,
                "status": "FAIL",
                "error": str(error),
            },
            receipt_path,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

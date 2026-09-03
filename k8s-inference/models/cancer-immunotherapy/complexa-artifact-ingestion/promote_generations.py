#!/usr/bin/env python3
"""Promote task-private staged checkpoints into immutable public generations.

This module deliberately owns no promotion logic. Measuring a tree, building the
marker, writing it, and the rename that commits the generation all come from the
reviewed localization successor's own ``fs2_localization.localization``. A second
implementation of any of those would be a second answer to "what is this tree",
and the whole point of a content-addressed generation is that there is one.

What this module does own is the step before it: re-verifying, on the claim, that
every contracted file still hashes to its contracted SHA-256. Staging verified
the bytes as they arrived over the network. This verifies the bytes that are
actually on the volume, which is a different claim and the one a consumer cares
about.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

COPY_CHUNK = 8 * 1024 * 1024

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_artifacts import (  # noqa: E402  - sibling module, same directory
    IngestionError,
    hash_prefix,
    load_contract,
    selected,
)

RECEIPT_SCHEMA = "fs2-serve.nebius.ai/scientific-promotion-receipt/v1"


def load_localization(package_parent: Path) -> Any:
    """Import the successor's verifier exactly as its own jobs import it.

    It is delivered as the package ``fs2_localization`` so that the module's
    relative import of ``primitives`` resolves, which is the same shape the
    localization jobs mount through their ConfigMap.
    """

    sys.path.insert(0, str(package_parent))
    try:
        return importlib.import_module("fs2_localization.localization")
    except ImportError as error:  # pragma: no cover - a deployment mistake, not a logic one
        raise IngestionError(
            f"could not import fs2_localization.localization from {package_parent}: {error}"
        ) from error


def nearest_existing(path: Path) -> Path:
    for candidate in [path, *path.parents]:
        if candidate.exists():
            return candidate
    raise IngestionError(f"{path}: no existing ancestor to resolve a filesystem from")


def devices(staged: Path, generations_root: Path) -> tuple[int, int]:
    return staged.stat().st_dev, nearest_existing(generations_root).stat().st_dev


def copy_verified(source: Path, target: Path, artifact: dict[str, Any], log: Any) -> list[dict[str, Any]]:
    """Stream each contracted file once into the destination, hashing as it goes.

    Ingress and the canonical store are different filesystems, so the bytes have
    to be copied rather than renamed. Copying is the expensive step, so it is
    done exactly once and the digest is computed from the bytes actually written
    rather than by reading the result back. A file that does not match is
    removed immediately, because a wrong file left in a staging directory is a
    wrong file that a later run might promote.
    """

    contracted = {entry["path"] for entry in artifact["files"]}
    extra = sorted(item.name for item in source.iterdir() if item.name not in contracted)
    if extra:
        raise IngestionError(f"{source}: holds uncontracted entries {extra}")

    copied: list[dict[str, Any]] = []
    for entry in artifact["files"]:
        origin = source / entry["path"]
        if not origin.is_file() or origin.is_symlink():
            raise IngestionError(f"{origin}: contracted file is missing or is not a regular file")
        destination = target / entry["path"]
        digest = hashlib.sha256()
        written = 0
        started = time.monotonic()
        with origin.open("rb") as reader, destination.open("wb") as writer:
            while True:
                block = reader.read(COPY_CHUNK)
                if not block:
                    break
                writer.write(block)
                digest.update(block)
                written += len(block)
            writer.flush()
            os.fsync(writer.fileno())
        actual = digest.hexdigest()
        if written != entry["bytes"] or actual != entry["sha256"]:
            destination.unlink(missing_ok=True)
            raise IngestionError(
                f"{destination}: copied {written} bytes with sha256 {actual}, "
                f"contract says {entry['bytes']} bytes with sha256 {entry['sha256']}"
            )
        seconds = time.monotonic() - started
        rate = (written / seconds / 1e6) if seconds > 0 else 0.0
        log(f"  {entry['path']}: copied and verified {written} bytes in {seconds:.1f}s ({rate:.0f} MB/s)")
        copied.append({"path": entry["path"], "bytes": written, "sha256": actual,
                       "copy_seconds": round(seconds, 3)})
    return copied


def reverify(staged: Path, artifact: dict[str, Any], log: Any) -> list[dict[str, Any]]:
    """Re-hash every contracted file where it now lives, before it is sealed."""

    verified: list[dict[str, Any]] = []
    for entry in artifact["files"]:
        path = staged / entry["path"]
        if not path.is_file() or path.is_symlink():
            raise IngestionError(f"{path}: contracted file is missing or is not a regular file")
        size = path.stat().st_size
        if size != entry["bytes"]:
            raise IngestionError(f"{path}: holds {size} bytes, contract says {entry['bytes']}")
        started = time.monotonic()
        digest = hash_prefix(path, size).hexdigest()
        if digest != entry["sha256"]:
            raise IngestionError(f"{path}: sha256 {digest} does not match contracted {entry['sha256']}")
        seconds = round(time.monotonic() - started, 3)
        log(f"  {entry['path']}: {size} bytes re-verified on the claim in {seconds}s")
        verified.append({"path": entry["path"], "bytes": size, "sha256": digest, "rehash_seconds": seconds})
    extra = sorted(
        item.name
        for item in staged.iterdir()
        if item.name not in {entry["path"] for entry in artifact["files"]}
    )
    if extra:
        raise IngestionError(f"{staged}: holds uncontracted entries {extra}")
    return verified


def promote(
    loc: Any,
    artifact: dict[str, Any],
    staged: Path,
    generations_root: Path,
    *,
    tree_sub_path: str,
    volume_kind: str,
    namespace: str,
    claim: str,
    host_root: str,
    visibility: str,
    allow_cross_filesystem_copy: bool,
    log: Any,
) -> dict[str, Any]:
    artifact_id = artifact["artifact_id"]
    tree = artifact["tree"]
    algorithm = tree["inventory_algorithm"]
    artifact_root = generations_root / artifact_id
    source_device, target_device = devices(staged, generations_root)

    if source_device == target_device:
        log(f"{artifact_id}: source and generations share device {source_device}; promotion is a rename")
        log(f"{artifact_id}: re-verifying {tree['entry_count']} contracted file(s) in place")
        files = reverify(staged, artifact, log)
        tree_directory = staged
        commit: dict[str, Any] = {"method": "same-filesystem-rename", "device": source_device}
    elif not allow_cross_filesystem_copy:
        raise IngestionError(
            f"{staged} (device {source_device}) and {generations_root} (device {target_device}) "
            "are on different filesystems; pass --allow-cross-filesystem-copy to accept the "
            "cost of copying every byte, or promote from a source on the destination filesystem"
        )
    else:
        # Ingress and the canonical store are separate filesystems here, so the
        # copy is unavoidable. It lands in a reserved temporary directory beside
        # where it will be published, so the rename that commits it still stays
        # within the destination filesystem and an interrupted run leaves only a
        # directory the successor's own reclaim recognizes.
        tree_directory = loc.prepare_staging_directory(artifact_root)
        log(f"{artifact_id}: copying {tree['total_bytes']} bytes from device "
            f"{source_device} to {target_device} via {tree_directory}")
        files = copy_verified(staged, tree_directory, artifact, log)
        commit = {
            "method": "cross-filesystem-copy-then-rename",
            "source_device": source_device,
            "target_device": target_device,
        }

    entries = loc.scan_recursive_tree(tree_directory, maximum_entries=500_000,
                                      maximum_bytes=64 * 1024 * 1024 * 1024)
    generation = loc.inventory_sha256(entries, algorithm)
    entry_count, directory_count, total_bytes = loc.tree_counts(entries)
    if entry_count != tree["entry_count"]:
        raise IngestionError(f"{artifact_id}: tree holds {entry_count} files, contract says {tree['entry_count']}")
    if total_bytes != tree["total_bytes"]:
        raise IngestionError(f"{artifact_id}: tree holds {total_bytes} bytes, contract says {tree['total_bytes']}")
    if directory_count:
        raise IngestionError(f"{artifact_id}: a checkpoint tree must be flat, found {directory_count} directories")

    sub_path = "/".join(
        part for part in (tree_sub_path.strip("/"), artifact_id, loc.GENERATION_DIGEST_DIRECTORY, generation) if part
    )
    source = {
        "kind": "file-set",
        "source_uri": artifact["source"]["uri"],
        "source_revision": artifact["source"]["revision"],
        "license_id": artifact["source"]["license_id"],
    }
    marker = loc.generation_marker(
        artifact_id=artifact_id,
        generation=generation,
        entry_count=entry_count,
        directory_count=directory_count,
        total_bytes=total_bytes,
        inventory_algorithm=algorithm,
        sub_path=sub_path,
        volume_kind=volume_kind,
        namespace=namespace,
        claim=claim,
        host_root=host_root,
        visibility=visibility,
        artifact_kind="weights",
        consumer_paths=tuple(tree["mount_paths"]),
        source=source,
    )
    # Written inside the tree before the rename, so the commit that publishes the
    # generation publishes its terminal marker with it and a consumer that mounts
    # only the generation can still admit the bytes without rehashing them.
    marker_digest = loc.write_generation_marker(tree_directory / loc.RUNTIME_MARKER_NAME, marker)
    published, reused = loc.promote_generation(tree_directory, artifact_root, generation)
    if reused:
        # This run proved the bytes it staged. It has proved nothing about bytes
        # someone else published under the same name, so the existing generation
        # is measured rather than trusted, and only then is our now-redundant
        # copy released.
        existing = loc.scan_recursive_tree(published, maximum_entries=500_000,
                                           maximum_bytes=64 * 1024 * 1024 * 1024)
        existing_generation = loc.inventory_sha256(existing, algorithm)
        if existing_generation != generation:
            raise IngestionError(
                f"{published}: already-published tree measures {existing_generation}, "
                f"not the {generation} its path claims"
            )
        if tree_directory != published and tree_directory.is_dir():
            shutil.rmtree(tree_directory)
        log(f"{artifact_id}: generation {generation} was already published and re-measured intact")
    else:
        log(f"{artifact_id}: published generation {generation} at {published}")

    return {
        "artifact_id": artifact_id,
        "generation": generation,
        "inventory_algorithm": algorithm,
        "entry_count": entry_count,
        "directory_count": directory_count,
        "total_bytes": total_bytes,
        "published_path": str(published),
        "already_published": reused,
        "namespace": namespace,
        "claim": claim,
        "sub_path": sub_path,
        "marker_name": loc.RUNTIME_MARKER_NAME,
        "marker_sha256": marker_digest,
        "volume_kind": volume_kind,
        "host_root": host_root,
        "commit": commit,
        "source_root": str(staged),
        "visibility": visibility,
        "source": artifact["source"],
        "mount_paths": list(tree["mount_paths"]),
        "consumers": artifact.get("consumers", []),
        "files": files,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--artifact-id", action="append", default=[], dest="artifact_ids")
    parser.add_argument("--staging-root", type=Path, required=True)
    parser.add_argument("--generations-root", type=Path, required=True,
                        help="where generations are published; the rename into it is the commit")
    parser.add_argument("--allow-cross-filesystem-copy", action="store_true",
                        help="copy every byte when the source is on another filesystem, as it is "
                             "when ingress and the canonical store are different volumes")
    parser.add_argument("--localization-package-parent", type=Path, required=True,
                        help="directory containing the fs2_localization package")
    parser.add_argument("--tree-sub-path", default="",
                        help="the claim sub-path --generations-root corresponds to")
    # A generation lives on exactly one kind of plane and each is addressed
    # differently: a claim by namespace and claim, a Terraform-managed host
    # directory by its root. Carrying both would describe a location that does
    # not exist, and the successor's marker rejects that.
    parser.add_argument("--volume-kind", default="persistent-volume-claim",
                        choices=("persistent-volume-claim", "host-path"))
    parser.add_argument("--namespace", default="")
    parser.add_argument("--claim", default="")
    parser.add_argument("--host-root", default="", help="host-path plane: the Terraform-managed host root")
    parser.add_argument("--visibility", default="public", choices=("public", "tenant-private"))
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--continue-on-artifact-error", action="store_true")
    parser.add_argument("--prune-staging", action="store_true",
                        help="after the receipt is written, delete this task's own leftover .part files")
    options = parser.parse_args(argv)

    def log(message: str) -> None:
        print(message, flush=True)

    loc = load_localization(options.localization_package_parent)
    document = load_contract(options.contract)
    artifacts = selected(document, tuple(options.artifact_ids))

    promoted: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for artifact in artifacts:
        artifact_id = artifact["artifact_id"]
        staged = options.staging_root / artifact_id
        if not staged.is_dir():
            failures.append({"artifact_id": artifact_id, "error": f"nothing staged at {staged}"})
            log(f"{artifact_id}: SKIPPED, nothing staged at {staged}")
            if options.continue_on_artifact_error:
                continue
            break
        try:
            promoted.append(
                promote(
                    loc,
                    artifact,
                    staged,
                    options.generations_root,
                    tree_sub_path=options.tree_sub_path,
                    volume_kind=options.volume_kind,
                    namespace=options.namespace,
                    claim=options.claim,
                    host_root=options.host_root,
                    visibility=options.visibility,
                    allow_cross_filesystem_copy=options.allow_cross_filesystem_copy,
                    log=log,
                )
            )
        except Exception as error:  # noqa: BLE001 - the successor raises its own error type
            failures.append({"artifact_id": artifact_id, "error": str(error)})
            log(f"{artifact_id}: FAILED {error}")
            if not options.continue_on_artifact_error:
                break

    if options.receipt:
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "promoter": {
                "module": "fs2_localization.localization",
                "functions": [
                    "scan_recursive_tree",
                    "inventory_sha256",
                    "tree_counts",
                    "generation_marker",
                    "write_generation_marker",
                    "promote_generation",
                ],
            },
            "generations": promoted,
        }
        if failures:
            receipt["failures"] = failures
        options.receipt.parent.mkdir(parents=True, exist_ok=True)
        options.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        log(f"receipt written to {options.receipt}")

    if options.prune_staging:
        # A promoted artifact's staging directory was renamed into its
        # generation, so what can be left here is an empty directory. Removing
        # it is the whole of the cleanup, and it is deliberately the whole:
        #
        # - A ``.part`` file is an unfinished download, and deleting one throws
        #   away gigabytes this claim has no room to fetch twice. Resumability
        #   is worth more than the space, so partials are reported, not removed.
        # - Nothing outside --staging-root is examined. The PyRosetta,
        #   AlphaFold 3, and localization trees sharing this claim belong to
        #   other owners and are never walked, let alone deleted.
        for artifact in artifacts:
            artifact_id = artifact["artifact_id"]
            directory = options.staging_root / artifact_id
            if not directory.is_dir() or directory.is_symlink():
                continue
            remaining = sorted(item.name for item in directory.iterdir())
            if not remaining:
                directory.rmdir()
                log(f"{artifact_id}: removed empty staging directory")
            else:
                log(f"{artifact_id}: kept staging directory, still holds {remaining}")

    log(f"promoted {len(promoted)} generation(s); {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except IngestionError as error:
        print(f"error: {error}", file=sys.stderr, flush=True)
        sys.exit(2)

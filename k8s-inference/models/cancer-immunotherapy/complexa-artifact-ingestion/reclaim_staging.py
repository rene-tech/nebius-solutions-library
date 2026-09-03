#!/usr/bin/env python3
"""Reclaim ingress bytes, but only where the canonical generation is provably there.

Deleting the ingress copy is how the shared academic claim gets its space back,
and it is also the one irreversible step in this pipeline: the bytes cost hours
to fetch. So nothing is removed on the strength of a receipt alone. Each source
directory is released only after this process has looked at the published
generation itself and found the tree the receipt describes.

This runs as the account that owns the ingress directories, which is not the
account that writes the canonical store, so it is a separate entry point rather
than a flag on the promoter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

RECEIPT_SCHEMA = "fs2-serve.nebius.ai/scientific-reclaim-receipt/v1"


class ReclaimError(RuntimeError):
    """A safety gate refused to release source bytes."""


CHUNK = 8 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def expected_source_root(staging_root: Path, artifact_id: str) -> Path:
    """The only directory this task may release for a given artifact.

    Nothing is derived from the receipt here. A receipt is an input, and an
    input that can name the directory to delete is an input that can be made to
    name the wrong one, so the path is computed from the staging root and the
    artifact identity and the receipt's own value is then required to match it
    exactly. Containment alone is not enough: the staging root also holds the
    receipts directory and any sibling artifact's bytes.
    """

    if "/" in artifact_id or artifact_id in {"", ".", ".."} or artifact_id.startswith("."):
        raise ReclaimError(f"illegal artifact_id {artifact_id!r}")
    return staging_root / artifact_id


def hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


def confirm_published(entry: dict[str, Any], marker_name: str) -> tuple[int, list[dict[str, Any]]]:
    """Read the generation and prove it, byte for byte, before anything is deleted.

    The size of a file is not its identity. Corruption that preserves length --
    a partially rewritten block, a torn write, a silently truncated-and-padded
    replica -- passes a size check and a marker check alike, because the marker
    describes what *should* be there rather than what is. This is the last gate
    before bytes that cost hours to fetch are destroyed, so every file the
    receipt names is rehashed in full and compared against the digest the
    receipt recorded.
    """

    published = Path(entry["published_path"])
    if not published.is_dir() or published.is_symlink():
        raise ReclaimError(f"{published}: published generation is missing or is not a directory")
    if published.name != entry["generation"]:
        raise ReclaimError(f"{published}: does not sit under its own generation digest")

    marker = published / marker_name
    if not marker.is_file() or marker.is_symlink():
        raise ReclaimError(f"{marker}: terminal marker is missing")
    digest = hashlib.sha256(marker.read_bytes()).hexdigest()
    if digest != entry["marker_sha256"]:
        raise ReclaimError(f"{marker}: marker digest {digest} does not match the promoted {entry['marker_sha256']}")
    document = json.loads(marker.read_text())
    if document.get("generation") != entry["generation"]:
        raise ReclaimError(f"{marker}: names generation {document.get('generation')!r}")

    present = sorted(item.name for item in published.iterdir() if item.name != marker_name)
    expected = sorted(item["path"] for item in entry["files"])
    if present != expected:
        raise ReclaimError(f"{published}: holds {present}, the receipt promoted {expected}")

    total = 0
    verified: list[dict[str, Any]] = []
    for item in entry["files"]:
        recorded = item.get("sha256", "")
        if not SHA256.fullmatch(recorded):
            raise ReclaimError(f"{published}/{item['path']}: receipt carries no usable sha256")
        candidate = published / item["path"]
        if candidate.is_symlink() or not candidate.is_file():
            raise ReclaimError(f"{candidate}: is not a regular file in the published generation")
        size, actual = hash_file(candidate)
        if size != item["bytes"]:
            raise ReclaimError(f"{candidate}: holds {size} bytes, the receipt promoted {item['bytes']}")
        if actual != recorded:
            raise ReclaimError(
                f"{candidate}: reads back as sha256 {actual}, the receipt promoted {recorded}"
            )
        total += size
        verified.append({"path": item["path"], "bytes": size, "sha256": actual})
    if total != entry["total_bytes"]:
        raise ReclaimError(f"{published}: holds {total} bytes, the receipt promoted {entry['total_bytes']}")
    return total, verified


def directory_bytes(root: Path) -> int:
    return sum(item.stat().st_size for item in root.rglob("*") if item.is_file() and not item.is_symlink())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--promotion-receipt", type=Path, required=True)
    parser.add_argument("--staging-root", type=Path, required=True,
                        help="nothing outside this directory is ever removed")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(argv)

    def log(message: str) -> None:
        print(message, flush=True)

    promotion = json.loads(options.promotion_receipt.read_text())
    released: list[dict[str, Any]] = []
    refused: list[dict[str, str]] = []

    for entry in promotion.get("generations", []):
        artifact_id = entry["artifact_id"]
        try:
            required = expected_source_root(options.staging_root, artifact_id)
            declared = entry.get("source_root")
            # The receipt must agree with the path this task computed, exactly.
            # A broadened or rewritten source_root is the difference between
            # releasing one artifact's ingress and releasing the staging root.
            if declared is not None and Path(declared) != required:
                raise ReclaimError(
                    f"receipt names source_root {declared!r}, this artifact's ingress is {required}"
                )
            source = required
            total, verified = confirm_published(entry, entry.get("marker_name", ".fs2-runtime-tree.json"))
            if not source.exists():
                log(f"{artifact_id}: ingress already released")
                continue
            if source.is_symlink():
                raise ReclaimError(f"{source} is a symbolic link")
            freed = directory_bytes(source)
            if options.dry_run:
                log(f"{artifact_id}: would release {freed} bytes from {source}")
            else:
                shutil.rmtree(source)
                log(f"{artifact_id}: released {freed} bytes from {source}")
            released.append({
                "artifact_id": artifact_id,
                "source_root": str(source),
                "bytes_released": freed,
                "generation": entry["generation"],
                "published_path": entry["published_path"],
                "published_bytes": total,
                "published_files_rehashed": verified,
                "dry_run": bool(options.dry_run),
            })
        except (ReclaimError, OSError, KeyError, ValueError) as error:
            refused.append({"artifact_id": artifact_id, "error": str(error)})
            log(f"{artifact_id}: REFUSED to release ingress: {error}")

    if options.receipt:
        document: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "staging_root": str(options.staging_root),
            "released": released,
            "bytes_released": sum(item["bytes_released"] for item in released),
        }
        if refused:
            document["refused"] = refused
        options.receipt.parent.mkdir(parents=True, exist_ok=True)
        options.receipt.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
        log(f"receipt written to {options.receipt}")

    log(f"released {len(released)} ingress directory(ies); {len(refused)} refused")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())

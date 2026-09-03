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
import shutil
import sys
from pathlib import Path
from typing import Any

RECEIPT_SCHEMA = "fs2-serve.nebius.ai/scientific-reclaim-receipt/v1"


class ReclaimError(RuntimeError):
    """A safety gate refused to release source bytes."""


def within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def confirm_published(entry: dict[str, Any], marker_name: str) -> int:
    """Look at the generation, not at the claim that it exists.

    A published generation is named by the digest of its own content, so the
    cheap checks here are strong ones: the path is the digest, the marker inside
    it is byte-identical to the one the promoter recorded, and the file set still
    weighs exactly what the contract said. Rehashing gigabytes would add nothing
    the marker digest does not already pin.
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
    for item in entry["files"]:
        candidate = published / item["path"]
        if candidate.is_symlink() or not candidate.is_file():
            raise ReclaimError(f"{candidate}: is not a regular file in the published generation")
        size = candidate.stat().st_size
        if size != item["bytes"]:
            raise ReclaimError(f"{candidate}: holds {size} bytes, the receipt promoted {item['bytes']}")
        total += size
    if total != entry["total_bytes"]:
        raise ReclaimError(f"{published}: holds {total} bytes, the receipt promoted {entry['total_bytes']}")
    return total


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
        source = Path(entry.get("source_root") or (options.staging_root / artifact_id))
        try:
            if not within(source, options.staging_root):
                raise ReclaimError(f"{source} is outside the staging root {options.staging_root}")
            total = confirm_published(entry, entry.get("marker_name", ".fs2-runtime-tree.json"))
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

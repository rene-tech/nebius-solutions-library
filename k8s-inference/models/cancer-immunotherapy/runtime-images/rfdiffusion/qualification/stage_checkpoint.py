#!/usr/bin/env python3
"""Stage the official public RFdiffusion Base checkpoint as a content-addressed generation.

Runs inside the cluster, next to the artifact plane. It downloads the exact public
object, verifies its sha256 against the artifact catalog, and promotes it into

    <root>/generations/<artifact_id>/sha256/<generation>/

where ``generation`` is the tree inventory digest under ``fs2-flat-tree-inventory/v1``
-- the same algorithm the scientific-localization plane uses, so this staging binds to
that plane unchanged once it publishes.

Promotion is atomic: the tree is built in a sibling staging directory, sealed
read-only, then renamed. Re-staging an existing generation is a no-op, never an
overwrite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import zlib
from pathlib import Path
from typing import Any

INVENTORY_ALGORITHM = "fs2-flat-tree-inventory/v1"
MARKER_NAME = ".fs2-runtime-tree.json"
MARKER_SCHEMA = "fs2-serve.nebius.ai/scientific-localization-generation-marker/v1"

ARTIFACT_ID = "rfdiffusion-base-checkpoint"
ARTIFACT_KIND = "rfdiffusion-checkpoints"
SOURCE_REVISION = "9273ef67335acaf91df0150473a274759229cdf6"

# Exactly the catalog entry for the official public Base checkpoint.
CHECKPOINT = {
    "path": "Base_ckpt.pt",
    "url": "https://files.ipd.uw.edu/pub/RFdiffusion/6f5902ac237024bdd0c176cb93063dc4/Base_ckpt.pt",
    "sha256": "0fcf7d7c32b4848030aca3a051e6768de194616f96ba6c38186351a33bfc6eca",
    "bytes": 483616107,
}


def sha256_and_crc32(path: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    crc = 0
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            crc = zlib.crc32(chunk, crc)
            size += len(chunk)
    return digest.hexdigest(), crc & 0xFFFFFFFF, size


def inventory_digest(root: Path) -> tuple[str, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.name == MARKER_NAME:
            continue
        _, crc, size = sha256_and_crc32(path)
        rows.append(
            {"bytes": size, "crc32": crc, "path": str(path.relative_to(root))}
        )
    payload = json.dumps(rows, separators=(",", ":"), sort_keys=True) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), rows


def marker_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def download(url: str, destination: Path, expected_sha256: str, expected_bytes: int) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    request = urllib.request.Request(url, headers={"User-Agent": "fs2-rfdiffusion-stage/1"})
    with urllib.request.urlopen(request, timeout=1800) as response:  # noqa: S310 - pinned https source
        with destination.open("wb") as handle:
            while True:
                chunk = response.read(4 * 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                written += len(chunk)
                handle.write(chunk)
    actual = digest.hexdigest()
    if written != expected_bytes:
        raise SystemExit(f"{url}: expected {expected_bytes} bytes, received {written}")
    if actual != expected_sha256:
        raise SystemExit(f"{url}: sha256 mismatch\n  expected {expected_sha256}\n  actual   {actual}")
    return actual


def seal(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o444 if path.is_file() else 0o555)
    root.chmod(0o555)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("FS2_ARTIFACT_ROOT", "/artifacts"))
    parser.add_argument("--artifact-id", default=ARTIFACT_ID)
    parser.add_argument("--report", default="/dev/stdout")
    args = parser.parse_args(argv)

    root = Path(args.root)
    artifact_root = root / "generations" / args.artifact_id
    artifact_root.mkdir(parents=True, exist_ok=True)
    digest_root = artifact_root / "sha256"
    digest_root.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(artifact_root)))
    try:
        target = staging / CHECKPOINT["path"]
        print(f"downloading {CHECKPOINT['url']}", flush=True)
        actual_sha = download(
            CHECKPOINT["url"], target, CHECKPOINT["sha256"], CHECKPOINT["bytes"]
        )
        print(f"verified {CHECKPOINT['path']} sha256={actual_sha}", flush=True)

        generation, rows = inventory_digest(staging)
        final = digest_root / generation

        if final.is_dir():
            print(f"generation already present, no-op: {final}", flush=True)
            report = {
                "schema": "fs2-serve.nebius.ai/rfdiffusion-checkpoint-staging/v1",
                "state": "already-present",
                "artifact_id": args.artifact_id,
                "generation": generation,
                "sub_path": str(final.relative_to(root)),
                "absolute_path": str(final),
                "checkpoint_sha256": actual_sha,
            }
            Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0

        marker = {
            "schema": MARKER_SCHEMA,
            "artifact_id": args.artifact_id,
            "artifact_kind": ARTIFACT_KIND,
            "generation": generation,
            "inventory_algorithm": INVENTORY_ALGORITHM,
            "inventory_sha256": generation,
            "entry_count": len(rows),
            "directory_count": 0,
            "total_bytes": sum(r["bytes"] for r in rows),
            "volume_kind": "persistentVolumeClaim",
            "read_only": True,
            "visibility": "public",
            "source_uri": CHECKPOINT["url"],
            "source_sha256": CHECKPOINT["sha256"],
            "source_bytes": CHECKPOINT["bytes"],
            "source_revision": SOURCE_REVISION,
            "license_id": "BSD-3-Clause",
            "generator_identity": "fs2-rfdiffusion-final-image-h100-successor-r20260903",
            "sub_path": str(final.relative_to(root)),
            "consumer_paths": ["/models/rfdiffusion-checkpoints"],
        }
        (staging / MARKER_NAME).write_bytes(marker_bytes(marker))

        seal(staging)
        os.rename(staging, final)
        staging = None  # renamed; nothing to clean up
        print(f"promoted generation {generation} -> {final}", flush=True)

        report = {
            "schema": "fs2-serve.nebius.ai/rfdiffusion-checkpoint-staging/v1",
            "state": "promoted",
            "artifact_id": args.artifact_id,
            "generation": generation,
            "inventory_algorithm": INVENTORY_ALGORITHM,
            "sub_path": str(final.relative_to(root)),
            "absolute_path": str(final),
            "checkpoint_sha256": actual_sha,
            "checkpoint_bytes": CHECKPOINT["bytes"],
            "marker_sha256": hashlib.sha256(marker_bytes(marker)).hexdigest(),
            "entries": rows,
        }
        Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    finally:
        if staging is not None and Path(staging).is_dir():
            for path in sorted(Path(staging).rglob("*"), reverse=True):
                path.chmod(0o700)
            Path(staging).chmod(0o700)
            shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

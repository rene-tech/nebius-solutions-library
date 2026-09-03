#!/usr/bin/env python3
"""Resume, verify, and atomically publish the exact BoltzGen checkpoint tree.

This helper owns only its task-named staging directory.  Completed files from
the predecessor are reused after a full size/SHA-256 check; a partial file is
continued with an HTTP Range request when the server honors it.  The final
generation is addressed by the platform's fs2-flat-tree-inventory/v1 digest.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import time
import urllib.request
import zlib
from pathlib import Path
from typing import Any

CHUNK = 4 * 1024 * 1024
RANGE_CHUNK = 64 * 1024 * 1024
RANGE_WORKERS = 8
ARTIFACT_ID = "boltzgen-checkpoints"
MARKER = ".fs2-runtime-tree.json"
MARKER_SCHEMA = "fs2-serve.nebius.ai/scientific-localization-generation-marker/v1"
INVENTORY_ALGORITHM = "fs2-flat-tree-inventory/v1"
PLANE_PREFIX = "scientific-localization/public"


class LocalizationError(RuntimeError):
    """The exact immutable publication contract was not satisfied."""


def canonical_json(value: object, *, indent: int | None = None) -> bytes:
    if indent is None:
        return (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()
    return (json.dumps(value, indent=indent, sort_keys=True) + "\n").encode()


def digest_file(path: Path) -> tuple[int, str, int]:
    size = 0
    digest = hashlib.sha256()
    crc = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            size += len(chunk)
            digest.update(chunk)
            crc = zlib.crc32(chunk, crc)
    return size, digest.hexdigest(), crc & 0xFFFFFFFF


def inventory_generation(rows: list[dict[str, object]]) -> str:
    payload = [
        {"bytes": row["bytes"], "crc32": row["crc32"], "path": row["path"]}
        for row in sorted(rows, key=lambda item: str(item["path"]))
    ]
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def checkpoint_url(contract: dict[str, Any], filename: str) -> str:
    return contract["source_url_template"].format(
        revision=contract["source_revision"], name=filename
    )


def download_or_resume(
    target: Path, *, url: str, expected_bytes: int, expected_sha256: str
) -> tuple[dict[str, object], int]:
    """Return a verified inventory row and the number of bytes reused."""

    reused = 0
    if target.exists():
        observed_bytes, observed_sha256, observed_crc = digest_file(target)
        if observed_bytes == expected_bytes:
            if observed_sha256 != expected_sha256:
                raise LocalizationError(f"complete staged file has wrong digest: {target.name}")
            os.chmod(target, 0o444)
            return (
                {
                    "path": target.name,
                    "bytes": observed_bytes,
                    "sha256": observed_sha256,
                    "crc32": f"{observed_crc:08x}",
                    "transfer": "verified-existing",
                },
                observed_bytes,
            )
        if observed_bytes > expected_bytes:
            raise LocalizationError(f"partial staged file exceeds its contract: {target.name}")
        reused = observed_bytes

    started = time.monotonic()
    range_root = target.parent / f".ranges-{target.name}"
    range_root.mkdir(exist_ok=True)
    ranges = [
        (start, min(start + RANGE_CHUNK, expected_bytes) - 1)
        for start in range(reused, expected_bytes, RANGE_CHUNK)
    ]
    expected_segments = {
        f"{start:012d}-{end:012d}.part" for start, end in ranges
    }
    for child in range_root.iterdir():
        if child.is_file() and child.name not in expected_segments:
            child.unlink()

    def download_range(bounds: tuple[int, int]) -> Path:
        start, end = bounds
        destination = range_root / f"{start:012d}-{end:012d}.part"
        expected_length = end - start + 1
        if destination.is_file() and destination.stat().st_size == expected_length:
            return destination
        temporary = destination.with_suffix(".tmp")
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "fs2-boltzgen-checkpoint-localizer/1",
                "Range": f"bytes={start}-{end}",
            },
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            status = getattr(response, "status", response.getcode())
            content_range = response.headers.get("Content-Range", "")
            if status != 206 or not content_range.startswith(f"bytes {start}-{end}/"):
                raise LocalizationError(
                    f"server refused exact byte range {start}-{end} for {target.name}"
                )
            written = 0
            with temporary.open("wb") as handle:
                while chunk := response.read(CHUNK):
                    written += len(chunk)
                    if written > expected_length:
                        raise LocalizationError(f"range exceeded contract: {target.name}")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        if written != expected_length:
            raise LocalizationError(
                f"short range {start}-{end} for {target.name}: {written} bytes"
            )
        os.replace(temporary, destination)
        return destination

    with concurrent.futures.ThreadPoolExecutor(max_workers=RANGE_WORKERS) as executor:
        segments = list(executor.map(download_range, ranges))
    with target.open("ab") as handle:
        for (start, _end), segment in zip(ranges, segments, strict=True):
            if handle.tell() != start:
                raise LocalizationError(f"range assembly offset differs for {target.name}")
            with segment.open("rb") as source:
                while chunk := source.read(CHUNK):
                    handle.write(chunk)
        handle.flush()
        os.fsync(handle.fileno())
    written, observed_sha256, observed_crc = digest_file(target)
    if written != expected_bytes or observed_sha256 != expected_sha256:
        with target.open("r+b") as handle:
            handle.truncate(reused)
        raise LocalizationError(
            f"{target.name} delivered bytes={written} sha256={observed_sha256}"
        )
    for segment in segments:
        segment.unlink()
    range_root.rmdir()
    os.chmod(target, 0o444)
    return (
        {
            "path": target.name,
            "bytes": written,
            "sha256": observed_sha256,
            "crc32": f"{observed_crc:08x}",
            "transfer": "parallel-resumed" if reused else "parallel-downloaded",
            "range_workers": RANGE_WORKERS,
            "seconds": round(time.monotonic() - started, 3),
        },
        reused,
    )


def marker_document(
    *, contract: dict[str, Any], generation: str, physical_host_root: str
) -> dict[str, object]:
    sub_path = f"{PLANE_PREFIX}/generations/{ARTIFACT_ID}/sha256/{generation}"
    return {
        "schema": MARKER_SCHEMA,
        "artifact_id": ARTIFACT_ID,
        "artifact_kind": ARTIFACT_ID,
        "generation": generation,
        "inventory_algorithm": INVENTORY_ALGORITHM,
        "inventory_sha256": generation,
        "entry_count": len(contract["files"]),
        "directory_count": 0,
        "symlink_count": None,
        "total_bytes": contract["total_bytes"],
        "volume_kind": "host-path",
        "namespace": "",
        "claim": "",
        "host_root": physical_host_root,
        "sub_path": sub_path,
        "visibility": "public",
        "read_only": True,
        "source_uri": contract["source_uri"],
        "source_revision": contract["source_revision"],
        "license_id": contract["license"],
        "generator_identity": [],
        "consumer_paths": [contract["mount_path"]],
    }


def verify_published(
    root: Path, contract: dict[str, Any], *, physical_host_root: str
) -> dict[str, object]:
    rows = []
    for expected in contract["files"]:
        path = root / expected["path"]
        if path.is_symlink() or not path.is_file():
            raise LocalizationError(f"published generation lacks {expected['path']}")
        size, digest, crc = digest_file(path)
        if size != expected["bytes"] or digest != expected["sha256"]:
            raise LocalizationError(f"published generation differs at {expected['path']}")
        rows.append(
            {
                "path": expected["path"],
                "bytes": size,
                "sha256": digest,
                "crc32": f"{crc:08x}",
            }
        )
    generation = inventory_generation(rows)
    if root.name != generation:
        raise LocalizationError("published directory name is not its tree identity")
    marker_path = root / MARKER
    marker_bytes = marker_path.read_bytes()
    marker = json.loads(marker_bytes)
    if marker_bytes != canonical_json(marker, indent=2):
        raise LocalizationError("published marker is not canonically serialized")
    if marker != marker_document(
        contract=contract, generation=generation, physical_host_root=physical_host_root
    ):
        raise LocalizationError("published marker does not match the accepted plane")
    return {
        "generation": generation,
        "marker_sha256": hashlib.sha256(marker_bytes).hexdigest(),
        "files": rows,
    }


def localize(
    *, lock: dict[str, Any], host_root: Path, physical_host_root: str, staging_name: str
) -> dict[str, object]:
    contract = lock["artifacts"][ARTIFACT_ID]
    generation_parent = (
        host_root / PLANE_PREFIX / "generations" / ARTIFACT_ID / "sha256"
    )
    generation_parent.mkdir(parents=True, exist_ok=True)
    existing = sorted(path for path in generation_parent.iterdir() if path.is_dir())
    if existing:
        if len(existing) != 1:
            raise LocalizationError("checkpoint plane contains multiple generations")
        verified = verify_published(
            existing[0], contract, physical_host_root=physical_host_root
        )
        return {
            "schema": "fs2-serve.nebius.ai/boltzgen-checkpoint-localization/v1",
            "state": "already-published",
            **verified,
        }

    staging = host_root / ".staging" / staging_name
    staging.mkdir(parents=True, exist_ok=True)
    allowed = {record["path"] for record in contract["files"]}
    unexpected = sorted(
        child.name
        for child in staging.iterdir()
        if child.name not in allowed
        and not (child.name.startswith(".ranges-") and child.is_dir())
    )
    if unexpected:
        raise LocalizationError(f"task staging contains unexpected entries: {unexpected}")

    rows = []
    reused_bytes = 0
    for expected in contract["files"]:
        row, reused = download_or_resume(
            staging / expected["path"],
            url=checkpoint_url(contract, expected["path"]),
            expected_bytes=expected["bytes"],
            expected_sha256=expected["sha256"],
        )
        rows.append(row)
        reused_bytes += reused
        print(json.dumps({"checkpoint": row}, sort_keys=True), flush=True)

    generation = inventory_generation(rows)
    marker = marker_document(
        contract=contract,
        generation=generation,
        physical_host_root=physical_host_root,
    )
    marker_payload = canonical_json(marker, indent=2)
    marker_path = staging / MARKER
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    handle = os.open(marker_path, flags, 0o444)
    with os.fdopen(handle, "wb") as stream:
        stream.write(marker_payload)
        stream.flush()
        os.fsync(stream.fileno())
    destination = generation_parent / generation
    if destination.exists():
        raise LocalizationError("immutable generation appeared during publication")
    os.rename(staging, destination)
    # Overlayfs rejects renaming a directory after its owner write bit is
    # removed. The directory rename is still the publication boundary; seal the
    # task-owned destination immediately after that atomic transition.
    os.chmod(destination, 0o555)
    receipt = {
        "schema": "fs2-serve.nebius.ai/boltzgen-checkpoint-localization/v1",
        "state": "published",
        "artifact_id": ARTIFACT_ID,
        "generation": generation,
        "inventory_algorithm": INVENTORY_ALGORITHM,
        "marker_sha256": hashlib.sha256(marker_payload).hexdigest(),
        "entry_count": len(rows),
        "total_bytes": sum(int(row["bytes"]) for row in rows),
        "reused_bytes": reused_bytes,
        "host_root": physical_host_root,
        "sub_path": marker["sub_path"],
        "mount_path": contract["mount_path"],
        "source_uri": contract["source_uri"],
        "source_revision": contract["source_revision"],
        "files": rows,
    }
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--host-root", type=Path, required=True)
    parser.add_argument(
        "--physical-host-root", default="/mnt/fs2-reference-data/data"
    )
    parser.add_argument(
        "--staging-name", default="fs2-boltzgen-boltzgen-checkpoints-1"
    )
    arguments = parser.parse_args()
    try:
        receipt = localize(
            lock=json.loads(arguments.lock.read_text(encoding="utf-8")),
            host_root=arguments.host_root,
            physical_host_root=arguments.physical_host_root,
            staging_name=arguments.staging_name,
        )
    except (LocalizationError, OSError, ValueError) as error:
        print(json.dumps({"state": "failed", "reason": str(error)}, sort_keys=True))
        return 1
    print("FS2_LOCALIZATION_RECEIPT=" + json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

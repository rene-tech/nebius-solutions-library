#!/usr/bin/env python3
"""Prove a published generation is readable, and correct, from a consumer node.

Publication happened on a storage-capable CPU node. The models run on H100
nodes. Those are different machines reaching the reference plane through
different mounts, and "it was written correctly" is not the same claim as "the
node that will consume it can read exactly those bytes". This probe makes the
second claim, from the consumer node class, with no write access at all.

It re-hashes every contracted file in full rather than trusting sizes or the
marker, because a shared filesystem exposed to a second client is precisely
where a partially visible or stale file would show up.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

MARKER_NAME = ".fs2-runtime-tree.json"
CHUNK = 8 * 1024 * 1024
SCHEMA = "fs2-serve.nebius.ai/scientific-generation-visibility/v1"


def node_digest() -> str:
    """A stable, non-identifying stand-in for the node.

    These receipts are committed to a public repository and the downward API
    hands a pod the opaque instance ID, so the raw value stays out. A truncated
    digest still answers the only question this field was ever for: whether two
    receipts came from the same machine.
    """

    raw = os.environ.get("FS2_NODE_NAME") or socket.gethostname()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


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


def check(root: Path, artifact: dict[str, Any], generation: str) -> dict[str, Any]:
    artifact_id = artifact["artifact_id"]
    mount = root / artifact_id / "sha256" / generation
    problems: list[str] = []
    if mount.is_symlink() or not mount.is_dir():
        return {"artifact_id": artifact_id, "generation": generation, "mount": str(mount),
                "visible": False, "problems": ["generation directory is not visible from this node"]}

    if os.access(mount, os.W_OK):
        problems.append("generation directory is writable from a consumer node")

    marker_record: dict[str, Any] = {}
    marker = mount / MARKER_NAME
    if not marker.is_file() or marker.is_symlink():
        problems.append("terminal marker is missing")
    else:
        payload = marker.read_bytes()
        document = json.loads(payload)
        marker_record = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "generation": document.get("generation"),
            "artifact_id": document.get("artifact_id"),
            "source_uri": document.get("source_uri"),
            "source_revision": document.get("source_revision"),
            "inventory_algorithm": document.get("inventory_algorithm"),
            "host_root": document.get("host_root"),
            "total_bytes": document.get("total_bytes"),
            "entry_count": document.get("entry_count"),
        }
        if document.get("generation") != generation:
            problems.append(f"marker names generation {document.get('generation')!r}")
        if document.get("artifact_id") != artifact_id:
            problems.append(f"marker names artifact {document.get('artifact_id')!r}")
        if document.get("source_revision") != artifact["source"]["revision"]:
            problems.append("marker source revision disagrees with the contract")

    present = sorted(item.name for item in mount.iterdir() if item.name != MARKER_NAME)
    expected = sorted(entry["path"] for entry in artifact["files"])
    if present != expected:
        problems.append(f"holds {present}, contract declares {expected}")

    files: list[dict[str, Any]] = []
    for entry in artifact["files"]:
        path = mount / entry["path"]
        if path.is_symlink() or not path.is_file():
            problems.append(f"{entry['path']} is not a regular file")
            continue
        started = time.monotonic()
        size, digest = hash_file(path)
        seconds = time.monotonic() - started
        matched = size == entry["bytes"] and digest == entry["sha256"]
        if not matched:
            problems.append(f"{entry['path']} reads back as {size} bytes / {digest}")
        if os.access(path, os.W_OK):
            problems.append(f"{entry['path']} is writable from a consumer node")
        files.append({
            "path": entry["path"], "bytes": size, "sha256": digest, "matches_contract": matched,
            "read_seconds": round(seconds, 3),
            "read_mb_per_second": round(size / seconds / 1e6, 1) if seconds > 0 else None,
        })

    return {
        "artifact_id": artifact_id,
        "generation": generation,
        "mount": str(mount),
        "visible": True,
        "marker": marker_record,
        "files": files,
        "problems": problems,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--generations-root", type=Path, required=True)
    parser.add_argument("--expect", action="append", required=True, metavar="ARTIFACT_ID=GENERATION")
    parser.add_argument("--report", type=Path)
    options = parser.parse_args(argv)

    contract = json.loads(options.contract.read_text())
    by_id = {artifact["artifact_id"]: artifact for artifact in contract["artifacts"]}

    results: list[dict[str, Any]] = []
    for item in options.expect:
        artifact_id, separator, generation = item.partition("=")
        if not separator:
            raise SystemExit("--expect takes ARTIFACT_ID=GENERATION")
        if artifact_id not in by_id:
            raise SystemExit(f"contract declares no artifact {artifact_id}")
        result = check(options.generations_root, by_id[artifact_id], generation)
        results.append(result)
        state = "OK" if not result["problems"] else "PROBLEM"
        print(f"{artifact_id}: {state} {result['mount']}", flush=True)
        for problem in result["problems"]:
            print(f"  - {problem}", flush=True)
        for record in result.get("files", []):
            print(f"  {record['path']}: {record['bytes']} bytes, sha256 {record['sha256'][:16]}…, "
                  f"matches={record['matches_contract']}, {record['read_mb_per_second']} MB/s", flush=True)

    problems = sum(len(item["problems"]) for item in results)
    document = {
        "schema": SCHEMA,
        "node_digest": node_digest(),
        "generations_root": str(options.generations_root),
        "artifacts": results,
        "problems": problems,
    }
    if options.report:
        options.report.parent.mkdir(parents=True, exist_ok=True)
        options.report.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"problems": problems, "node_digest": document["node_digest"]}), flush=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

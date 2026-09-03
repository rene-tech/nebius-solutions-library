#!/usr/bin/env python3
"""Stage the exact pinned Complexa and RosettaFold3 artifacts, verifying bytes.

This runs *inside* a staging pod.  It exists because the qualification must be
reproducible on its own: the public-artifact ingestion successor owns the
canonical content-addressed generations, but that task is unmerged and still
active, and it removed its staging tree while this qualification was running.
Rather than depend on another task's in-flight scratch space, the same pinned
identities are staged into this task's own claim and verified byte for byte.

The identities are not re-derived here.  They are the file identities pinned in
``image-lock.json`` -> ``external_artifacts``, which are in turn the upstream
published identities recorded by the ingestion contract and its staging receipt.

Downloads resume: a partial ``.part`` file is continued with a Range request,
and a server that ignores Range is restarted from zero rather than producing a
corrupt concatenation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CHUNK = 8 * 1024 * 1024

HF_TEMPLATE = "https://huggingface.co/{repo}/resolve/{revision}/{path}"

SOURCES: dict[str, dict[str, Any]] = {
    "complexa-protein": {
        "kind": "huggingface",
        "repo": "nvidia/NV-Proteina-Complexa-Protein-Target-160M-v1",
        "revision": "ffed199e32612b98ffa04f4640d34d37b137fca5",
    },
    "complexa-ligand": {
        "kind": "huggingface",
        "repo": "nvidia/NV-Proteina-Complexa-Ligand-Target-160M-v1",
        "revision": "bc90c8b2c701ceb52d5faef72600b6b5be880244",
    },
    "complexa-ame": {
        "kind": "huggingface",
        "repo": "nvidia/NV-Proteina-Complexa-AME-160M-v1",
        "revision": "9743d749a8754080a32fda857d95579dfa4dabae",
    },
    "rosettafold3-checkpoint": {
        "kind": "direct",
        "base": "https://files.ipd.uw.edu/pub/rf3/",
    },
}


def _url(artifact_id: str, relative: str) -> str:
    source = SOURCES[artifact_id]
    if source["kind"] == "huggingface":
        return HF_TEMPLATE.format(
            repo=source["repo"], revision=source["revision"], path=relative
        )
    return source["base"] + relative


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(url: str, destination: Path, expected: dict[str, Any], retries: int) -> dict[str, Any]:
    partial = destination.with_suffix(destination.suffix + ".part")
    if destination.is_file() and destination.stat().st_size == expected["bytes"]:
        observed = _sha256(destination)
        if observed == expected["sha256"]:
            return {"state": "already-present", "bytes": expected["bytes"], "attempts": 0}
        destination.unlink()

    attempts = 0
    started = time.monotonic()
    while attempts < retries:
        attempts += 1
        offset = partial.stat().st_size if partial.is_file() else 0
        request = urllib.request.Request(url)
        if offset:
            request.add_header("Range", f"bytes={offset}-")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                if offset and response.status != 206:
                    # The server ignored the Range header; restart cleanly
                    # instead of appending a second full body to the partial.
                    partial.unlink(missing_ok=True)
                    continue
                mode = "ab" if offset and response.status == 206 else "wb"
                with partial.open(mode) as sink:
                    while True:
                        chunk = response.read(CHUNK)
                        if not chunk:
                            break
                        sink.write(chunk)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as failure:
            print(f"    attempt {attempts} failed: {failure}", flush=True)
            time.sleep(min(30, 2**attempts))
            continue

        size = partial.stat().st_size
        if size != expected["bytes"]:
            print(f"    attempt {attempts}: {size} of {expected['bytes']} bytes", flush=True)
            continue
        observed = _sha256(partial)
        if observed != expected["sha256"]:
            print(f"    attempt {attempts}: sha256 {observed} != {expected['sha256']}", flush=True)
            partial.unlink(missing_ok=True)
            continue
        partial.replace(destination)
        return {
            "state": "downloaded",
            "bytes": size,
            "attempts": attempts,
            "seconds": round(time.monotonic() - started, 3),
            "sha256": observed,
        }

    raise SystemExit(f"failed to stage {url} after {attempts} attempt(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True, help="path to image-lock.json")
    parser.add_argument("--root", required=True, help="staging root directory")
    parser.add_argument("--artifact-id", action="append", required=True)
    parser.add_argument("--retries", type=int, default=8)
    parser.add_argument("--receipt", default=None)
    arguments = parser.parse_args()

    lock = json.loads(Path(arguments.lock).read_text(encoding="utf-8"))
    catalogue = {item["artifact_id"]: item for item in lock["external_artifacts"]}
    root = Path(arguments.root)

    receipt: dict[str, Any] = {
        "schema": "fs2.nebius.ai/proteina-complexa-staging-receipt/v1",
        "owner_task": lock["owner_task"],
        "root": str(root),
        "artifacts": [],
    }
    for artifact_id in arguments.artifact_id:
        entry = catalogue.get(artifact_id)
        if entry is None or not entry.get("files"):
            raise SystemExit(f"{artifact_id} has no pinned files in the lock")
        target = root / artifact_id
        target.mkdir(parents=True, exist_ok=True)
        total = sum(item["bytes"] for item in entry["files"])
        print(
            f"{artifact_id}: {len(entry['files'])} file(s), {total} bytes -> {target}",
            flush=True,
        )
        files = []
        for item in entry["files"]:
            url = _url(artifact_id, item["path"])
            outcome = fetch(url, target / item["path"], item, arguments.retries)
            print(
                f"  {item['path']}: {outcome['state']} {outcome['bytes']} bytes"
                + (f" in {outcome['seconds']}s" if outcome.get("seconds") else ""),
                flush=True,
            )
            files.append({"path": item["path"], "url": url, **outcome})
        receipt["artifacts"].append(
            {
                "artifact_id": artifact_id,
                "source_uri": entry.get("source_uri"),
                "source_revision": entry.get("source_revision"),
                "license_id": entry.get("license_id"),
                "total_bytes": total,
                "files": files,
            }
        )

    if arguments.receipt:
        destination = Path(arguments.receipt)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        print(f"receipt written to {destination}", flush=True)
    print(
        f"staged {sum(len(item['files']) for item in receipt['artifacts'])} verified file(s)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

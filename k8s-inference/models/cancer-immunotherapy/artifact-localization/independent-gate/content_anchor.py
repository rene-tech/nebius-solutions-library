#!/usr/bin/env python3
"""Publish a collision-resistant anchor for the five public generations.

``fs2-flat-tree-inventory/v1`` identifies each file by length and CRC-32, and
CRC-32 is affine over GF(2): for any fixed length the kernel of the map is
non-trivial above four bytes, so a same-length, same-CRC-32, different-content
file can be constructed by elimination rather than found by search. The
generation name is a SHA-256, but of a document that carries only those CRC-32s,
so the name is strong against corruption and weak against construction.

This does not make the published trees wrong; a separate recompute already
showed they are exactly what the receipts describe. It means the generation name
alone cannot prove that to someone who does not trust the writer. So record the
real thing: a SHA-256 per file, and one SHA-256 over the canonical list of them.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

ROOT = os.environ.get("FS2_TREE_ROOT", "/refdata/scientific-localization/public/generations")
MARKER = ".fs2-runtime-tree.json"

GENERATIONS = [
    ("alphafold2-params", "cdbb7c7c475442712c73f8f8ea40b42fb5dd4fb5c1bf81fdb4642ca9e27f5ac4"),
    ("alphafold2-params-bindcraft", "9e25d394b1a7296f7705a5be794c5e29b853beb967835db088069f7cc007aa4f"),
    ("boltzgen-inference-molecules", "8ab1a59c72fc27a37dea61aab9408d7619f7a91fe32409f7a2b36fd59ebeecdc"),
    ("colabdesign-mpnn-weights-soluble", "54da6672d5677ab27bea0939bbbc591f8877484175a182736ca79af045d0f146"),
    ("colabdesign-mpnn-weights-vanilla", "2602ff1e01c8bdfd5773334e5724fcf0bdfecb3963100f05ad67ad6a5824ee4f"),
]

# Same canonical shape as the v1 inventory, with the checksum replaced by a
# cryptographic digest, so the two are directly comparable and obviously distinct.
ALGORITHM = "fs2-flat-tree-content-sha256/v1"


def content_rows(path):
    rows = []
    with os.scandir(path) as scanner:
        for item in scanner:
            if item.name == MARKER or not item.is_file(follow_symlinks=False):
                continue
            digest = hashlib.sha256()
            size = 0
            with open(item.path, "rb") as handle:
                while True:
                    chunk = handle.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    digest.update(chunk)
            rows.append({"bytes": size, "path": item.name, "sha256": digest.hexdigest()})
    rows.sort(key=lambda row: row["path"])
    return rows


def main() -> int:
    report = {
        "schema": "fs2-serve.nebius.ai/fable-gate-content-anchor/v1",
        "algorithm": ALGORITHM,
        "node_digest": hashlib.sha256(os.environ.get("FS2_NODE_NAME", "").encode()).hexdigest()[:16],
        "generations": [],
    }
    for artifact_id, generation in GENERATIONS:
        started = time.monotonic()
        path = os.path.join(ROOT, artifact_id, "sha256", generation)
        rows = content_rows(path)
        payload = (json.dumps(rows, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        entry = {
            "artifact_id": artifact_id,
            "generation": generation,
            "content_tree_sha256": hashlib.sha256(payload).hexdigest(),
            "entry_count": len(rows),
            "total_bytes": sum(row["bytes"] for row in rows),
            "read_seconds": round(time.monotonic() - started, 3),
        }
        # A large tree's per-file list is recorded as its own digest only; the
        # small trees carry every file, because a reader can check those by hand.
        if len(rows) <= 20:
            entry["files"] = rows
        report["generations"].append(entry)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

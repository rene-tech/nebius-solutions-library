#!/usr/bin/env python3
"""Independent re-derivation of every published public generation identity.

Written for the fs2-live-scientific-evidence-fable-gate review. It shares no code
with the candidate: it re-implements fs2-flat-tree-inventory/v1 from its written
definition and recomputes every digest from the bytes actually on the node.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import zlib

ROOT = os.environ.get("FS2_TREE_ROOT", "/refdata/scientific-localization/public/generations")
MARKER = ".fs2-runtime-tree.json"

EXPECTED = [
    {
        "artifact_id": "alphafold2-params",
        "generation": "cdbb7c7c475442712c73f8f8ea40b42fb5dd4fb5c1bf81fdb4642ca9e27f5ac4",
        "marker_sha256": "da4d1936b6bb9c83ea4dc046cdc05131b0b2caf92cda71e086837f0f786d176f",
        "entry_count": 16,
        "total_bytes": 5587956571,
    },
    {
        "artifact_id": "alphafold2-params-bindcraft",
        "generation": "9e25d394b1a7296f7705a5be794c5e29b853beb967835db088069f7cc007aa4f",
        "marker_sha256": "25cad364aa28e5cf282a877d123ad938ea048a957ad8185307b5542c301406e0",
        "entry_count": 17,
        "total_bytes": 5587959437,
    },
    {
        "artifact_id": "boltzgen-inference-molecules",
        "generation": "8ab1a59c72fc27a37dea61aab9408d7619f7a91fe32409f7a2b36fd59ebeecdc",
        "marker_sha256": "7f0e2c401abd73c1d4ff6deb6719e027db6ee9a75f7b7ed940b1e63ff54bbae4",
        "entry_count": 45227,
        "total_bytes": 1820698819,
    },
    {
        "artifact_id": "colabdesign-mpnn-weights-soluble",
        "generation": "54da6672d5677ab27bea0939bbbc591f8877484175a182736ca79af045d0f146",
        "marker_sha256": "471cd4bcd0964be0c2f462668d01885e9db268e14fed04ebe02b693491690660",
        "entry_count": 5,
        "total_bytes": 26601241,
    },
    {
        "artifact_id": "colabdesign-mpnn-weights-vanilla",
        "generation": "2602ff1e01c8bdfd5773334e5724fcf0bdfecb3963100f05ad67ad6a5824ee4f",
        "marker_sha256": "07ee17ecbc3c2a5e50327461f3cde311c35a7fad18f7d92e244e220e15329fc8",
        "entry_count": 5,
        "total_bytes": 26602793,
    },
]


def node_digest() -> str:
    node = os.environ.get("FS2_NODE_NAME", "")
    return hashlib.sha256(node.encode("utf-8")).hexdigest()[:16] if node else ""


def flat_inventory_sha256(rows):
    """fs2-flat-tree-inventory/v1, re-implemented from its written definition."""
    rows = sorted(rows, key=lambda row: row["path"])
    payload = (json.dumps(rows, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def scan(path):
    rows = []
    marker_bytes = None
    extra = []
    with os.scandir(path) as scanner:
        for item in scanner:
            if item.name == MARKER:
                with open(item.path, "rb") as handle:
                    marker_bytes = handle.read()
                continue
            if not item.is_file(follow_symlinks=False):
                extra.append({"name": item.name, "kind": "not-a-regular-file"})
                continue
            size = 0
            crc = 0
            with open(item.path, "rb") as handle:
                while True:
                    chunk = handle.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    crc = zlib.crc32(chunk, crc)
            rows.append({"bytes": size, "crc32": f"{crc & 0xFFFFFFFF:08x}", "path": item.name})
    return rows, marker_bytes, extra


def main() -> int:
    report = {
        "schema": "fs2-serve.nebius.ai/fable-gate-generation-recompute/v1",
        "node_digest": node_digest(),
        "tree_root": ROOT,
        "generations": [],
    }
    failures = 0
    for expected in EXPECTED:
        started = time.monotonic()
        path = os.path.join(ROOT, expected["artifact_id"], "sha256", expected["generation"])
        item = {"artifact_id": expected["artifact_id"], "generation": expected["generation"], "path": path}
        if not os.path.isdir(path):
            item["state"] = "failed"
            item["reason"] = "generation directory is absent on this node"
            report["generations"].append(item)
            failures += 1
            continue
        rows, marker_bytes, extra = scan(path)
        recomputed = flat_inventory_sha256(rows)
        total = sum(row["bytes"] for row in rows)
        item["recomputed_inventory_sha256"] = recomputed
        item["recomputed_entry_count"] = len(rows)
        item["recomputed_total_bytes"] = total
        item["non_regular_entries"] = extra
        item["marker_present"] = marker_bytes is not None
        checks = {
            "inventory_matches_generation_name": recomputed == expected["generation"],
            "entry_count_matches_receipt": len(rows) == expected["entry_count"],
            "total_bytes_matches_receipt": total == expected["total_bytes"],
            "no_non_regular_entries": not extra,
            "marker_present": marker_bytes is not None,
        }
        if marker_bytes is not None:
            digest = hashlib.sha256(marker_bytes).hexdigest()
            item["recomputed_marker_sha256"] = digest
            checks["marker_digest_matches_receipt"] = digest == expected["marker_sha256"]
            try:
                marker = json.loads(marker_bytes.decode("utf-8"))
            except Exception as error:
                item["marker_parse_error"] = str(error)
                checks["marker_parses"] = False
            else:
                item["marker"] = marker
                checks["marker_parses"] = True
                checks["marker_declares_same_generation"] = (
                    marker.get("generation") == expected["generation"]
                    or marker.get("inventory_sha256") == expected["generation"]
                    or marker.get("tree_identity", {}).get("inventory_sha256") == expected["generation"]
                )
        item["checks"] = checks
        item["state"] = "passed" if all(checks.values()) else "failed"
        item["duration_seconds"] = round(time.monotonic() - started, 3)
        if item["state"] != "passed":
            failures += 1
        report["generations"].append(item)

    report["failures"] = failures
    report["state"] = "passed" if failures == 0 else "failed"
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

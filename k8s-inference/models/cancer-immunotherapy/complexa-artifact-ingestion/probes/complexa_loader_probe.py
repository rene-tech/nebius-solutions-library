#!/usr/bin/env python3
"""Prove the runtime can actually load a published generation, not just mount it.

Verifying a tree proves the bytes. This proves the model's own loader accepts
them, which is a different claim and the one that was silently unproven before:
a checkpoint can be byte-perfect and still fail to open because it was saved by
an incompatible serializer or is not a checkpoint at all.

The probe refuses a mount without its terminal marker before it reads anything,
so a regression cannot pass by loading from somewhere unexpected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

MARKER_NAME = ".fs2-runtime-tree.json"


def read_marker(mount: Path, expect_generation: str | None) -> dict[str, Any]:
    marker = mount / MARKER_NAME
    if marker.is_symlink() or not marker.is_file():
        raise SystemExit(f"{mount}: no terminal marker; this is not a published generation")
    document = json.loads(marker.read_text())
    generation = document.get("generation")
    # The generation directory is named by the digest of its own content, so the
    # marker disagreeing with the path it sits in means one of them is lying.
    if mount.name != generation:
        raise SystemExit(f"{mount}: marker names generation {generation!r}, path says {mount.name!r}")
    if expect_generation and generation != expect_generation:
        raise SystemExit(f"{mount}: expected generation {expect_generation}, mounted {generation}")
    if not document.get("read_only", False):
        raise SystemExit(f"{mount}: marker does not describe a read-only generation")
    return document


def describe(value: Any, depth: int = 0) -> dict[str, Any]:
    """Summarize a loaded checkpoint without holding another copy of it."""

    import torch

    if isinstance(value, torch.Tensor):
        return {"kind": "tensor", "dtype": str(value.dtype), "shape": list(value.shape),
                "elements": int(value.numel())}
    if isinstance(value, dict):
        tensors = 0
        elements = 0
        for item in value.values():
            if isinstance(item, torch.Tensor):
                tensors += 1
                elements += int(item.numel())
            elif isinstance(item, dict) and depth < 3:
                nested = describe(item, depth + 1)
                tensors += int(nested.get("tensors", 0))
                elements += int(nested.get("elements", 0))
        return {"kind": "mapping", "keys": sorted(value)[:24], "key_count": len(value),
                "tensors": tensors, "elements": elements}
    return {"kind": type(value).__name__}


def load(path: Path, *, weights_only: bool, mmap: bool) -> tuple[Any, str]:
    import torch

    try:
        return torch.load(path, map_location="cpu", weights_only=weights_only, mmap=mmap), \
            f"weights_only={weights_only},mmap={mmap}"
    except Exception:  # noqa: BLE001 - the fallback below is the point
        if not weights_only:
            raise
        # A training checkpoint carries optimizer and hyper-parameter objects that
        # the restricted unpickler refuses. Falling back is safe here because the
        # bytes were already digest-verified before publication.
        return torch.load(path, map_location="cpu", weights_only=False, mmap=mmap), \
            f"weights_only=False,mmap={mmap}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mount", type=Path, action="append", required=True, dest="mounts")
    parser.add_argument("--expect-generation", action="append", default=[], dest="expected")
    parser.add_argument("--spot-check-bytes", type=int, default=8 * 1024 * 1024,
                        help="prefix hashed per file as a cheap content spot check")
    parser.add_argument("--report", type=Path)
    options = parser.parse_args(argv)

    import torch

    report: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/scientific-loader-probe/v1",
        "torch_version": torch.__version__,
        "artifacts": [],
    }
    failures = 0

    for index, mount in enumerate(options.mounts):
        expected = options.expected[index] if index < len(options.expected) else None
        marker = read_marker(mount, expected)
        entry: dict[str, Any] = {
            "mount": str(mount),
            "artifact_id": marker["artifact_id"],
            "generation": marker["generation"],
            "source_uri": marker.get("source_uri"),
            "source_revision": marker.get("source_revision"),
            "files": [],
        }
        for path in sorted(item for item in mount.iterdir() if item.name != MARKER_NAME):
            started = time.monotonic()
            try:
                value, mode = load(path, weights_only=True, mmap=True)
            except Exception as error:  # noqa: BLE001 - report, do not abort the run
                failures += 1
                entry["files"].append({"path": path.name, "loaded": False, "error": str(error)[:400]})
                print(f"{mount.name}/{path.name}: FAILED to load: {error}", flush=True)
                continue
            summary = describe(value)
            prefix = hashlib.sha256(path.open("rb").read(options.spot_check_bytes)).hexdigest()
            record = {
                "path": path.name,
                "bytes": path.stat().st_size,
                "loaded": True,
                "load_mode": mode,
                "load_seconds": round(time.monotonic() - started, 3),
                "prefix_sha256": prefix,
                "prefix_bytes": min(options.spot_check_bytes, path.stat().st_size),
                **summary,
            }
            entry["files"].append(record)
            print(f"{mount.name}/{path.name}: loaded {record['bytes']} bytes as {summary['kind']}"
                  f" with {summary.get('tensors', 0)} tensors / {summary.get('elements', 0)} elements"
                  f" in {record['load_seconds']}s ({mode})", flush=True)
            del value
        report["artifacts"].append(entry)

    report["failures"] = failures
    if options.report:
        options.report.parent.mkdir(parents=True, exist_ok=True)
        options.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"failures": failures,
                      "artifacts": [item["artifact_id"] for item in report["artifacts"]]}), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

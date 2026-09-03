#!/usr/bin/env python
"""Prove RFdiffusion can open the exact localized Base checkpoint.

The admission init container validates the immutable generation marker. This
model-side probe independently hashes the mounted object, checks that the raw
generation contains no second payload, and deserializes it with the same
PyTorch runtime RFdiffusion uses. It reports a node digest instead of the opaque
provider node identifier so its receipt is safe to publish.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path

CHECKPOINT_NAME = "Base_ckpt.pt"
CHECKPOINT_BYTES = 483_616_107
CHECKPOINT_SHA256 = "0fcf7d7c32b4848030aca3a051e6768de194616f96ba6c38186351a33bfc6eca"
GENERATION = "7f34c945e580dbf5ba96596dcd325150f6452f7a76ee06a3784b2891a9d4c03c"
RUNTIME_MARKER_NAME = ".fs2-runtime-tree.json"


def node_digest() -> str:
    node = os.environ.get("FS2_NODE_NAME", "")
    return hashlib.sha256(node.encode("utf-8")).hexdigest()[:16] if node else ""


def file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _emit(report: dict[str, object], path: Path | None) -> None:
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if path is not None:
        path.write_text(rendered + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint-root", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    options = parser.parse_args()

    started = time.monotonic()
    report: dict[str, object] = {
        "schema": "fs2-serve.nebius.ai/rfdiffusion-checkpoint-probe/v1",
        "node_digest": node_digest(),
        "checkpoint_root": str(options.checkpoint_root),
    }
    try:
        root = options.checkpoint_root
        if root.is_symlink() or not root.is_dir():
            raise ValueError("checkpoint root is not a real directory")
        entries = sorted(item.name for item in root.iterdir() if item.name != RUNTIME_MARKER_NAME)
        if entries != [CHECKPOINT_NAME]:
            raise ValueError(f"raw generation contains unexpected entries: {entries}")

        marker = json.loads((root / RUNTIME_MARKER_NAME).read_text(encoding="utf-8"))
        expected_marker = {
            "artifact_id": "rfdiffusion-base-checkpoint",
            "generation": GENERATION,
            "inventory_algorithm": "fs2-raw-file/v1",
            "source_kind": "file",
            "source_filename": CHECKPOINT_NAME,
            "source_sha256": CHECKPOINT_SHA256,
            "source_bytes": CHECKPOINT_BYTES,
            "source_present_in_mount": True,
        }
        mismatches = {
            key: {"expected": expected, "observed": marker.get(key)}
            for key, expected in expected_marker.items()
            if marker.get(key) != expected
        }
        if mismatches:
            raise ValueError(f"generation marker mismatch: {mismatches}")

        checkpoint = root / CHECKPOINT_NAME
        if checkpoint.is_symlink() or not checkpoint.is_file():
            raise ValueError("checkpoint is not a real regular file")
        size, digest = file_identity(checkpoint)
        if size != CHECKPOINT_BYTES or digest != CHECKPOINT_SHA256:
            raise ValueError(f"checkpoint identity mismatch: bytes={size}, sha256={digest}")

        import torch

        loaded = torch.load(checkpoint, map_location="cpu", weights_only=False)
        if not isinstance(loaded, Mapping) or not loaded:
            raise ValueError("PyTorch did not deserialize a non-empty checkpoint mapping")
        report.update(
            {
                "state": "passed",
                "checkpoint": {"filename": checkpoint.name, "bytes": size, "sha256": digest},
                "generation": GENERATION,
                "inventory_algorithm": marker["inventory_algorithm"],
                "torch_version": torch.__version__,
                "checkpoint_top_level_keys": sorted(str(key) for key in loaded),
                "duration_seconds": round(time.monotonic() - started, 3),
            }
        )
        _emit(report, options.report)
        return 0
    except Exception as error:  # pragma: no cover - live receipt carries the exact failure
        report["state"] = "failed"
        report["reason"] = str(error)
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        _emit(report, options.report)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

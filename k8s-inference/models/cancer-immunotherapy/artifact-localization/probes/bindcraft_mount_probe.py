#!/usr/bin/env python
"""Prove the published BindCraft image accepts the localized mounts.

Three claims are checked with the image's own code, not with a re-implementation:

1. `artifact_gate.verify_from_environment` admits `/models/alphafold2`. That gate
   reads `FS2_ARTIFACT_MANIFEST` and rejects a tree whose manifest is missing or
   whose `artifact_kind` and `source_revision` do not match the runtime, which is
   why the sixteen-file upstream parameter tree cannot run this image as-is.
2. ColabDesign resolves both MPNN weight directories from their installed
   package paths. The image deletes them at build time, so both are mounts.
3. Vanilla and soluble really are different weights, not one tree mounted twice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

MPNN_MODEL = "v_48_020"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    options = parser.parse_args()

    started = time.monotonic()
    report: dict[str, object] = {
        "schema": "fs2-serve.nebius.ai/bindcraft-mount-probe/v1",
        "node": os.environ.get("FS2_NODE_NAME", ""),
        "runtime_environment": {
            key: os.environ.get(key, "")
            for key in ("FS2_ARTIFACT_KIND", "FS2_ARTIFACT_ROOT", "FS2_ARTIFACT_MANIFEST", "FS2_SOURCE_REVISION")
        },
    }

    try:
        sys.path.insert(0, "/opt/fs2")
        import artifact_gate

        report["alphafold2_gate"] = artifact_gate.verify_from_environment()
    except Exception as error:  # pragma: no cover - reported, not raised
        report["state"] = "failed"
        report["reason"] = f"the image artifact gate rejected /models/alphafold2: {error}"
        _emit(report, options.report)
        return 1

    weights: dict[str, object] = {}
    digests: dict[str, str] = {}
    try:
        from colabdesign.mpnn import model as mpnn_model
    except Exception as error:  # pragma: no cover - reported, not raised
        report["state"] = "failed"
        report["reason"] = f"colabdesign is unavailable in this runtime: {error}"
        _emit(report, options.report)
        return 1

    for selector, module in (("original", "weights"), ("soluble", "weights_soluble")):
        try:
            package = __import__(f"colabdesign.mpnn.{module}", fromlist=["__file__"])
            directory = Path(package.__file__ or "").parent
            checkpoint = directory / f"{MPNN_MODEL}.pkl"
            payload = checkpoint.read_bytes()
            digests[selector] = hashlib.sha256(payload).hexdigest()
            loaded = mpnn_model.mk_mpnn_model(model_name=MPNN_MODEL, weights=selector)
            weights[selector] = {
                "directory": str(directory),
                "checkpoint": checkpoint.name,
                "bytes": len(payload),
                "sha256": digests[selector],
                "entries": sorted(item.name for item in directory.iterdir()),
                "model_type": type(loaded).__name__,
            }
        except Exception as error:  # pragma: no cover - reported, not raised
            report["mpnn_weights"] = weights
            report["state"] = "failed"
            report["reason"] = f"ColabDesign could not load {selector} MPNN weights: {error}"
            _emit(report, options.report)
            return 1

    report["mpnn_weights"] = weights
    if digests["original"] == digests["soluble"]:
        report["state"] = "failed"
        report["reason"] = "vanilla and soluble MPNN resolved to identical bytes; one tree is mounted twice"
        _emit(report, options.report)
        return 1

    report["mpnn_directories_are_distinct"] = True
    report["state"] = "passed"
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    _emit(report, options.report)
    return 0


def _emit(report: dict[str, object], path: Path | None) -> None:
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if path is not None:
        try:
            path.write_text(rendered + "\n", encoding="utf-8")
        except OSError as error:  # pragma: no cover - reported, not fatal
            print(f"could not write {path}: {error}")


if __name__ == "__main__":
    raise SystemExit(main())

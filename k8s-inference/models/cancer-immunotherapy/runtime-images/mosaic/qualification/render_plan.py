#!/usr/bin/env python3
"""Render the H100 qualification plan with the canonical mosaic batch adapter.

The adapter module is never vendored.  It is loaded straight out of the primary
adapter candidate commit so the argv that reaches the GPU is the argv the
canonical renderer produces, not a hand-written approximation.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
LOCK = json.loads((HERE.parent / "image-lock.json").read_text(encoding="utf-8"))
ADAPTER = LOCK["adapter"]


def _git_blob(repo: Path, revision: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{revision}:{path}"],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


def load_adapter(repo: Path):
    """Import the canonical batch adapter from its pinned commit."""
    payload = _git_blob(repo, ADAPTER["commit"], f"{ADAPTER['repository_path']}/batch_adapter.py")
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - lifetime is the process
        "wb", suffix="_mosaic_batch_adapter.py", delete=False
    )
    handle.write(payload)
    handle.close()
    spec = importlib.util.spec_from_file_location("mosaic_batch_adapter", handle.name)
    if spec is None or spec.loader is None:
        raise SystemExit("could not load the canonical mosaic batch adapter")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def artifact_loader(target_fasta: Path):
    payload = target_fasta.read_bytes()

    def load(artifact_id: str) -> bytes:
        if artifact_id != "artifact.mosaic.target.minibinder":
            raise KeyError(artifact_id)
        return payload

    return load


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[6])
    parser.add_argument("--runtime-image", required=True, help="repository@sha256:... reference")
    parser.add_argument("--tenant-id", default="cancer-immunotherapy")
    parser.add_argument("--operation-id", default="op.mosaic.h100.r20260903")
    parser.add_argument("--workload-id", default="mosaic-qualification-r20260903")
    parser.add_argument("--attempt-id", default="attempt-1")
    parser.add_argument("--local-queue", default="inference-models")
    parser.add_argument("--output", type=Path, default=HERE / "generated-plan.json")
    arguments = parser.parse_args()

    adapter = load_adapter(arguments.repo)
    request = json.loads((HERE / "mosaic-request.json").read_text(encoding="utf-8"))
    manifest = json.loads((HERE / "mosaic-input-manifest.json").read_text(encoding="utf-8"))
    target = HERE / "target-minibinder.fasta"

    plan = adapter.render_plan(
        request,
        manifest,
        artifact_loader=artifact_loader(target),
        runtime_image=arguments.runtime_image,
        operation_id=arguments.operation_id,
        workload_id=arguments.workload_id,
        attempt_id=arguments.attempt_id,
        tenant_id=arguments.tenant_id,
        local_queue=arguments.local_queue,
    )
    summary: dict[str, Any] = {
        "adapter_commit": ADAPTER["commit"],
        "adapter_module_sha256": hashlib.sha256(
            _git_blob(arguments.repo, ADAPTER["commit"], f"{ADAPTER['repository_path']}/batch_adapter.py")
        ).hexdigest(),
        "plan": plan,
        "argv": {node["id"]: node["job"]["spec"]["template"]["spec"]["containers"][0]["command"] for node in plan["nodes"]},
    }
    arguments.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"request_sha256": plan["request_sha256"], "nodes": [n["id"] for n in plan["nodes"]], "output": str(arguments.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

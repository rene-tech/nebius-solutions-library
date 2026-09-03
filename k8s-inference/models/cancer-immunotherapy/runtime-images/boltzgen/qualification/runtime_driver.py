#!/usr/bin/env python3
"""Run the exact adapter-generated BoltzGen configure/design argv on one GPU."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CHUNK = 4 * 1024 * 1024
MARKER = ".fs2-runtime-tree.json"
MODEL_READY_MARKERS = (
    "Restored all states from the checkpoint",
    "LOCAL_RANK:",
    "Predicting DataLoader",
    "Loaded weights from",
)


class QualificationError(RuntimeError):
    """The exact H100 qualification contract failed."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verify_marker(root: Path, contract: dict[str, Any]) -> dict[str, object]:
    marker_path = root / MARKER
    payload = marker_path.read_bytes()
    observed_sha256 = hashlib.sha256(payload).hexdigest()
    if observed_sha256 != contract["marker_sha256"]:
        raise QualificationError(f"{root.name} marker digest differs from the runtime lock")
    marker = json.loads(payload)
    if marker.get("generation") != contract["generation"]:
        raise QualificationError(f"{root.name} is not mounted at its pinned generation")
    expected = {
        "entry_count": contract["entry_count"],
        "total_bytes": contract["total_bytes"],
        "read_only": True,
        "visibility": "public",
        "inventory_algorithm": "fs2-flat-tree-inventory/v1",
    }
    for key, value in expected.items():
        if marker.get(key) != value:
            raise QualificationError(f"{root.name} marker {key} differs from the runtime lock")
    return {
        "generation": marker["generation"],
        "marker_sha256": observed_sha256,
        "entry_count": marker["entry_count"],
        "total_bytes": marker["total_bytes"],
        "host_root": marker["host_root"],
        "sub_path": marker["sub_path"],
        "mount_path": str(root),
        "read_only_mount": not os.access(root, os.W_OK),
    }


def verify_artifacts(lock: dict[str, Any], artifact_root: Path) -> dict[str, object]:
    started = time.monotonic()
    checkpoints = lock["artifacts"]["boltzgen-checkpoints"]
    checkpoint_root = artifact_root / "boltzgen-checkpoints"
    checkpoint_marker = verify_marker(checkpoint_root, checkpoints)
    files = []
    for expected in checkpoints["files"]:
        path = checkpoint_root / expected["path"]
        if path.is_symlink() or not path.is_file():
            raise QualificationError(f"checkpoint is absent or unsafe: {expected['path']}")
        observed = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
        if observed != {"bytes": expected["bytes"], "sha256": expected["sha256"]}:
            raise QualificationError(f"checkpoint bytes differ: {expected['path']}")
        files.append({"path": expected["path"], **observed})

    molecules = lock["artifacts"]["boltzgen-inference-molecules"]
    molecule_root = artifact_root / "boltzgen-inference-molecules"
    molecule_marker = verify_marker(molecule_root, molecules)
    # The immutable marker is the content identity. Recounting and statting every
    # regular file additionally detects a missing/truncated mount without hashing
    # 45,227 entries on every prepared run.
    count = 0
    total = 0
    for path in molecule_root.rglob("*"):
        if path.name == MARKER:
            continue
        if path.is_symlink():
            raise QualificationError(f"molecule generation has a non-regular entry: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise QualificationError(f"molecule generation has a non-regular entry: {path}")
        count += 1
        total += path.stat().st_size
    if count != molecules["entry_count"] or total != molecules["total_bytes"]:
        raise QualificationError(f"molecule generation has count={count}, bytes={total}")
    return {
        "verification_seconds": round(time.monotonic() - started, 3),
        "checkpoints": {**checkpoint_marker, "files": files},
        "molecules": {**molecule_marker, "observed_entry_count": count, "observed_total_bytes": total},
    }


def run_json(argv: list[str], *, cwd: Path | None = None) -> dict[str, object]:
    completed = subprocess.run(argv, cwd=cwd, check=False, capture_output=True, text=True)
    if completed.returncode:
        raise QualificationError(
            f"command failed ({completed.returncode}): {argv!r}: {completed.stdout}{completed.stderr}"
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise QualificationError(f"command emitted no JSON: {argv!r}")
    return json.loads(lines[-1])


def run_streaming(argv: list[str], *, cwd: Path, result_root: Path) -> dict[str, object]:
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if process.stdout is None:
        raise QualificationError("could not capture runtime output")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    model_ready: float | None = None
    model_ready_marker: str | None = None
    first_result: float | None = None
    log_lines = 0
    while process.poll() is None:
        for key, _mask in selector.select(timeout=0.2):
            line = key.fileobj.readline()
            if not line:
                continue
            log_lines += 1
            elapsed = time.monotonic() - started
            print(f"FS2_RUNTIME_LOG +{elapsed:.3f}s {line}", end="", flush=True)
            if model_ready is None and any(marker in line for marker in MODEL_READY_MARKERS):
                model_ready = elapsed
                model_ready_marker = line.strip()[:500]
        if first_result is None and any(
            path.is_file() and not path.name.endswith("_native.cif")
            for path in result_root.glob("*.cif")
        ):
            first_result = time.monotonic() - started
    for line in process.stdout:
        log_lines += 1
        elapsed = time.monotonic() - started
        print(f"FS2_RUNTIME_LOG +{elapsed:.3f}s {line}", end="", flush=True)
        if model_ready is None and any(marker in line for marker in MODEL_READY_MARKERS):
            model_ready = elapsed
            model_ready_marker = line.strip()[:500]
    completed = time.monotonic() - started
    if first_result is None and any(
        path.is_file() and not path.name.endswith("_native.cif")
        for path in result_root.glob("*.cif")
    ):
        first_result = completed
    if process.returncode:
        raise QualificationError(f"runtime argv exited {process.returncode}: {argv!r}")
    if first_result is None:
        raise QualificationError("runtime exited successfully without a generated mmCIF")
    return {
        "seconds": round(completed, 3),
        "model_ready_log_marker_seconds": None if model_ready is None else round(model_ready, 3),
        "model_ready_log_marker": model_ready_marker,
        "first_result_seconds": round(first_result, 3),
        "log_lines": log_lines,
    }


def gpu_identity() -> dict[str, object]:
    query = subprocess.run(
        ["nvidia-smi", "--query-gpu=uuid,name,driver_version", "--format=csv,noheader"],
        check=False,
        capture_output=True,
        text=True,
    )
    if query.returncode:
        raise QualificationError("nvidia-smi could not identify the allocated GPU")
    rows = [row.strip() for row in query.stdout.splitlines() if row.strip()]
    if len(rows) != 1:
        raise QualificationError(f"qualification must see exactly one GPU, found {len(rows)}")
    uuid, name, driver = (field.strip() for field in rows[0].split(",", maxsplit=2))
    if "H100" not in name:
        raise QualificationError(f"qualification requires H100, got {name}")
    return {"uuid": uuid, "name": name, "driver_version": driver}


def prepare_inputs(input_root: Path, workspace: Path) -> dict[str, object]:
    inputs = workspace / "inputs"
    specs = inputs / "design-specs"
    specs.mkdir(parents=True, exist_ok=False)
    target = inputs / "5J89-chain-A.cif"
    shutil.copyfile(input_root / "5J89-chain-A.cif", target)
    yaml_payload = (input_root / "pdl1-face.yaml").read_text(encoding="utf-8")
    rewritten = yaml_payload.replace("path: 5J89-chain-A.cif", f"path: {target}")
    if rewritten == yaml_payload:
        raise QualificationError("design YAML path was not rewritten during materialization")
    yaml_path = specs / "pdl1-face.yaml"
    yaml_path.write_text(rewritten, encoding="utf-8")
    return {
        "target_path": str(target),
        "target_sha256": sha256_file(target),
        "target_bytes": target.stat().st_size,
        "yaml_path": str(yaml_path),
        "yaml_sha256": sha256_file(yaml_path),
    }


def qualify(arguments: argparse.Namespace) -> dict[str, object]:
    total_started = time.monotonic()
    lock = json.loads(arguments.lock.read_text(encoding="utf-8"))
    plan = json.loads(arguments.plan.read_text(encoding="utf-8"))
    operation_id = plan["operation_id"]
    workspace = Path(plan["working_directory"])
    workspace.mkdir(parents=True, exist_ok=False)
    input_receipt = prepare_inputs(arguments.input_root, workspace)
    if (
        input_receipt["target_sha256"] != lock["input"]["projected_sha256"]
        or input_receipt["target_bytes"] != lock["input"]["projected_bytes"]
    ):
        raise QualificationError("mounted PD-L1 input differs from image-lock.json")

    artifacts = verify_artifacts(lock, arguments.artifact_root)
    gpu = gpu_identity()
    probe_started = time.monotonic()
    probe = run_json(
        ["python", "/opt/fs2/runtime_probe.py", "--model", "boltzgen", "--require-gpu"]
    )
    probe_seconds = time.monotonic() - probe_started
    if (probe.get("framework") or {}).get("device_name") != gpu["name"]:
        raise QualificationError("runtime probe and nvidia-smi report different GPUs")

    configure_started = time.monotonic()
    configure = subprocess.run(plan["configure_argv"], cwd=workspace, check=False)
    configure_seconds = time.monotonic() - configure_started
    if configure.returncode:
        raise QualificationError(f"adapter configure argv exited {configure.returncode}")
    config_path = workspace / "config" / "design.yaml"
    if not config_path.is_file():
        raise QualificationError("adapter configure argv did not create config/design.yaml")

    design = run_streaming(
        plan["design_argv"],
        cwd=workspace,
        result_root=workspace / "intermediate_designs",
    )
    validation = run_json(
        [
            "python",
            str(arguments.validator),
            "--workspace",
            str(workspace),
            "--target",
            input_receipt["target_path"],
        ]
    )
    if validation.get("status") != "passed":
        raise QualificationError("independent result validator did not pass")
    return {
        "schema": "fs2-serve.nebius.ai/boltzgen-h100-run-receipt/v1",
        "status": "passed",
        "finished_at": utc_now(),
        "scenario": plan["scenario"],
        "operation_id": operation_id,
        "batch_id": plan["batch_id"],
        "attempt_id": os.environ.get("FS2_ATTEMPT_ID"),
        "pod": {
            "namespace": os.environ.get("FS2_POD_NAMESPACE"),
            "name": os.environ.get("FS2_POD_NAME"),
            "uid": os.environ.get("FS2_POD_UID"),
            "node": os.environ.get("FS2_NODE_NAME"),
        },
        "gpu": gpu,
        "image": lock["image"],
        "artifacts": artifacts,
        "input": input_receipt,
        "adapter": {
            "source_sha256": lock["adapter"]["source_sha256"],
            "base_commit": lock["adapter"]["base_commit"],
            "configure_argv": plan["configure_argv"],
            "design_argv": plan["design_argv"],
            "request_sha256": plan["request_sha256"],
            "input_artifact_sha256": plan["input_artifact_sha256"],
        },
        "runtime_probe": probe,
        "timings": {
            "artifact_verification_seconds": artifacts["verification_seconds"],
            "gpu_probe_seconds": round(probe_seconds, 3),
            "configure_seconds": round(configure_seconds, 3),
            "model_ready_from_design_start_seconds": design["model_ready_log_marker_seconds"],
            "model_ready_log_marker": design["model_ready_log_marker"],
            "first_result_from_design_start_seconds": design["first_result_seconds"],
            "design_completion_seconds": design["seconds"],
            "container_work_completion_seconds": round(time.monotonic() - total_started, 3),
        },
        "validation": validation,
        "route_effect": "none-task-owned-job-only",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--validator", type=Path, required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, default=Path("/opt/fs2/artifacts"))
    arguments = parser.parse_args()
    try:
        receipt = qualify(arguments)
    except (OSError, ValueError, QualificationError, subprocess.SubprocessError) as error:
        receipt = {
            "schema": "fs2-serve.nebius.ai/boltzgen-h100-run-receipt/v1",
            "status": "failed",
            "finished_at": utc_now(),
            "reason": str(error),
            "pod": {
                "name": os.environ.get("FS2_POD_NAME"),
                "uid": os.environ.get("FS2_POD_UID"),
                "node": os.environ.get("FS2_NODE_NAME"),
            },
        }
    print("FS2_QUALIFICATION_RECEIPT=" + json.dumps(receipt, sort_keys=True), flush=True)
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())

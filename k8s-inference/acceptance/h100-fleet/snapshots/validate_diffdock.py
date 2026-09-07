#!/usr/bin/env python3
"""Run the pinned two-seed DiffDock validator inside an isolated serving Pod."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SOURCE = Path(__file__).resolve()
# The bridge is executed from a shallow temporary directory inside the serving
# Pod. Repository paths are host-only inputs used while building its archive.
ROOT = SOURCE.parents[3] if len(SOURCE.parents) > 3 else None
VALIDATOR_ROOT = (
    ROOT
    / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2/diffdock-native"
    if ROOT is not None
    else Path("/nonexistent/fs2-host-validator")
)
NATIVE_VALIDATOR = VALIDATOR_ROOT / "validate_diffdock.py"
FIXTURE = VALIDATOR_ROOT / "fixtures/1ubq-aspirin-request.json"
EXPECTED_VALIDATOR_SHA256 = (
    "245ae98a98db09c34924cd7a499b99da9eb35742667043aaee3e497c33268008"
)
EXPECTED_FIXTURE_SHA256 = (
    "f58c2b74f534529a3b7e5cdd1410e8df33a25cee64a988a62170c5c69ca80977"
)
SEEDS = (2370, 2371)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validator_archive() -> bytes:
    """Return only this bridge, the immutable validator, and its fixture."""
    if _sha256(NATIVE_VALIDATOR) != EXPECTED_VALIDATOR_SHA256:
        raise ValueError("DiffDock validator no longer matches its frozen contract")
    if _sha256(FIXTURE) != EXPECTED_FIXTURE_SHA256:
        raise ValueError(
            "DiffDock request fixture no longer matches its frozen contract"
        )
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for source, name in (
            (Path(__file__), "validate_diffdock_snapshot.py"),
            (NATIVE_VALIDATOR, "validate_diffdock_native.py"),
            (FIXTURE, "1ubq-aspirin-request.json"),
        ):
            content = source.read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o444
            archive.addfile(info, io.BytesIO(content))
    return payload.getvalue()


def _load_native(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("diffdock_native", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("pinned DiffDock validator cannot be imported")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_remote(base_url: str, fixture_path: Path, receipt_dir: Path) -> dict[str, Any]:
    native = _load_native(Path(__file__).with_name("validate_diffdock_native.py"))
    fixture = native._read_fixture(fixture_path)
    receipt_dir.mkdir(parents=True, exist_ok=False)
    ready_at = native._wait_ready(base_url, 60.0)
    cases = []
    started = time.monotonic()
    for index, seed in enumerate(SEEDS, 1):
        payload = {**fixture, "random_seed": seed}
        request = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        request_sha256 = hashlib.sha256(request).hexdigest()
        (receipt_dir / f"request-{index}.json").write_bytes(request)
        run_id = f"snapshot-diffdock-{seed}"
        raw, elapsed, request_started_at, response_received_at = native._post(
            base_url, payload, run_id, 900.0
        )
        (receipt_dir / f"response-{index}.json").write_bytes(raw)
        decoded = json.loads(raw)
        invariant = native._validate_response(decoded, payload)
        case = {
            "index": index,
            "input_id": run_id,
            "random_seed": seed,
            "request_sha256": request_sha256,
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "response_bytes": len(raw),
            "elapsed_seconds": elapsed,
            "request_started_at": request_started_at,
            "response_received_at": response_received_at,
            "invariant": invariant,
            "ok": True,
            "status": "PASS",
        }
        (receipt_dir / f"case-{index}.json").write_text(
            json.dumps(case, sort_keys=True, separators=(",", ":")) + "\n"
        )
        cases.append(case)
    passed = (
        len(cases) == 2
        and len({case["request_sha256"] for case in cases}) == 2
        and len({case["random_seed"] for case in cases}) == 2
    )
    summary = {
        "schema_version": 1,
        "validator": "diffdock-snapshot-semantic-v1",
        "native_validator_sha256": EXPECTED_VALIDATOR_SHA256,
        "fixture_sha256": EXPECTED_FIXTURE_SHA256,
        "base_url": base_url.rstrip("/"),
        "endpoint": base_url.rstrip("/") + native.ENDPOINT,
        "ready_at": ready_at,
        "request_count": len(cases),
        "passed_case_count": len(cases),
        "failed_case_count": 0,
        "cases": cases,
        "ok": passed,
        "status": "PASS" if passed else "FAIL",
        "validation_total_elapsed_seconds": round(time.monotonic() - started, 6),
        "finished_at": datetime.now(UTC).isoformat(),
    }
    (receipt_dir / "summary.json").write_text(
        json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n"
    )
    return summary


def project_summary(summary: dict[str, Any]) -> dict[str, Any]:
    cases = summary.get("cases")
    passed = (
        summary.get("validator") == "diffdock-snapshot-semantic-v1"
        and summary.get("native_validator_sha256") == EXPECTED_VALIDATOR_SHA256
        and summary.get("fixture_sha256") == EXPECTED_FIXTURE_SHA256
        and summary.get("ok") is True
        and summary.get("status") == "PASS"
        and summary.get("passed_case_count") == 2
        and isinstance(cases, list)
        and len(cases) == 2
        and {case.get("random_seed") for case in cases} == set(SEEDS)
        and len({case.get("request_sha256") for case in cases}) == 2
        and all(case.get("ok") is True for case in cases)
    )
    return {
        "passed": passed,
        "model": "diffdock",
        "requests": [
            {
                "passed": case.get("ok") is True,
                "input_id": case.get("input_id"),
                "random_seed": case.get("random_seed"),
                "request_sha256": case.get("request_sha256"),
                "response_sha256": case.get("response_sha256"),
                "elapsed_seconds": case.get("elapsed_seconds"),
                "invariant": case.get("invariant"),
            }
            for case in cases or []
        ],
        "canonical_summary": summary,
        "input_scope": (
            "the original pinned 1UBQ/aspirin payload at the two accepted public "
            "random seeds 2370 and 2371"
        ),
    }


def validate(
    kubeconfig: str,
    pod: str,
    output: Path,
    *,
    container: str = "runtime",
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    kube = [
        "kubectl",
        "--kubeconfig",
        kubeconfig,
        "--context",
        "k8s-inference-h100",
        "-n",
        "fs2-models",
    ]

    def call(command: list[str], *, payload: bytes | None = None, timeout: int = 60):
        return subprocess.run(
            [*kube, *command],
            input=payload,
            capture_output=True,
            check=True,
            timeout=timeout,
        )

    remote = (
        call(
            [
                "exec",
                pod,
                "-c",
                container,
                "--",
                "mktemp",
                "-d",
                "/tmp/fs2-diffdock-validator.XXXXXX",
            ]
        )
        .stdout.decode()
        .strip()
    )
    if not remote.startswith("/tmp/fs2-diffdock-validator.") or "/" in remote[5:]:
        raise RuntimeError("validator temporary directory was not safely created")
    try:
        call(
            ["exec", "-i", pod, "-c", container, "--", "tar", "-x", "-C", remote],
            payload=validator_archive(),
        )
        completed = subprocess.run(
            [
                *kube,
                "exec",
                pod,
                "-c",
                container,
                "--",
                "python3",
                remote + "/validate_diffdock_snapshot.py",
                "--remote",
                "--base-url",
                "http://127.0.0.1:8000",
                "--fixture",
                remote + "/1ubq-aspirin-request.json",
                "--receipt-dir",
                remote + "/receipt",
            ],
            capture_output=True,
            check=False,
            timeout=1900,
        )
        (output / "validator.stdout").write_bytes(completed.stdout)
        (output / "validator.stderr").write_bytes(completed.stderr)
        if completed.returncode:
            raise RuntimeError(
                "DiffDock validator process failed; retained stdout/stderr contain details"
            )
        summary = json.loads(completed.stdout.decode().strip().splitlines()[-1])
        for name in (
            "summary.json",
            "request-1.json",
            "request-2.json",
            "response-1.json",
            "response-2.json",
            "case-1.json",
            "case-2.json",
        ):
            content = call(
                ["exec", pod, "-c", container, "--", "cat", remote + "/receipt/" + name]
            ).stdout
            (output / name).write_bytes(content)
        result = project_summary(summary)
        (output / "semantics.json").write_text(json.dumps(result, indent=2) + "\n")
        if not result["passed"]:
            raise RuntimeError("frozen DiffDock semantic validator did not pass")
        return result
    finally:
        subprocess.run(
            [*kube, "exec", pod, "-c", container, "--", "rm", "-r", remote],
            capture_output=True,
            check=False,
            timeout=30,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--base-url")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--receipt-dir", type=Path)
    parser.add_argument("--kubeconfig")
    parser.add_argument("--pod")
    parser.add_argument("--container", default="runtime")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.remote:
        if args.base_url is None or args.fixture is None or args.receipt_dir is None:
            parser.error(
                "remote validation requires URL, fixture and receipt directory"
            )
        print(json.dumps(run_remote(args.base_url, args.fixture, args.receipt_dir)))
        return
    if args.kubeconfig is None or args.pod is None or args.output is None:
        parser.error("Pod validation requires kubeconfig, Pod and output")
    os.umask(0o077)
    print(
        json.dumps(
            validate(
                args.kubeconfig,
                args.pod,
                args.output,
                container=args.container,
            )
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run the frozen two-input GenMol validator inside an isolated serving Pod."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
VALIDATOR_ROOT = (
    ROOT
    / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2/genmol-native"
)
VALIDATOR = VALIDATOR_ROOT / "validate_genmol.py"
FIXTURE = VALIDATOR_ROOT / "fixtures/requests-qed-logp.json"
EXPECTED_FIXTURE_SHA256 = (
    "3065261de604f495a2fbae1e7fd92488546ee51f2729e5d40e9be5ee2c22f444"
)


def validator_archive() -> bytes:
    """Return only the immutable validator and its pinned request fixture."""
    if hashlib.sha256(FIXTURE.read_bytes()).hexdigest() != EXPECTED_FIXTURE_SHA256:
        raise ValueError("GenMol request fixture no longer matches its frozen contract")
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for source, name in (
            (VALIDATOR, "validate_genmol.py"),
            (FIXTURE, "requests-qed-logp.json"),
        ):
            info = tarfile.TarInfo(name)
            content = source.read_bytes()
            info.size = len(content)
            info.mode = 0o444
            archive.addfile(info, io.BytesIO(content))
    return payload.getvalue()


def project_summary(summary: dict[str, Any]) -> dict[str, Any]:
    cases = summary.get("cases")
    passed = (
        summary.get("validator") == "genmol-faststart-semantic-v1"
        and summary.get("ok") is True
        and summary.get("status") == "PASS"
        and summary.get("passed_case_count") == 2
        and isinstance(cases, list)
        and len(cases) == 2
        and all(case.get("ok") is True for case in cases)
    )
    return {
        "passed": passed,
        "model": "genmol",
        "requests": [
            {
                "passed": case.get("ok") is True,
                "name": case.get("name"),
                "input_id": case.get("input_id"),
                "request_sha256": case.get("request_sha256"),
                "response_sha256": case.get("response_sha256"),
                "elapsed_seconds": case.get("elapsed_seconds"),
                "invariant": case.get("invariant"),
            }
            for case in cases or []
        ],
        "canonical_summary": summary,
        "input_scope": (
            "original two pinned QED and LogP requests, also used before capture; "
            "not unseen-input evidence"
        ),
    }


def validate(
    kubeconfig: str,
    pod: str,
    output: Path,
    *,
    container: str = "genmol",
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

    remote = call(
        ["exec", pod, "-c", container, "--", "mktemp", "-d", "/tmp/fs2-genmol-validator.XXXXXX"]
    ).stdout.decode().strip()
    if not remote.startswith("/tmp/fs2-genmol-validator.") or "/" in remote[5:]:
        raise RuntimeError("validator temporary directory was not safely created")
    try:
        call(
            ["exec", "-i", pod, "-c", container, "--", "tar", "-x", "-C", remote],
            payload=validator_archive(),
        )
        suffix = hashlib.sha256(pod.encode()).hexdigest()[:10]
        command = [
            "exec",
            pod,
            "-c",
            container,
            "--",
            "python3",
            remote + "/validate_genmol.py",
            "--base-url",
            "http://127.0.0.1:8000",
            "--request-file",
            remote + "/requests-qed-logp.json",
            "--receipt-dir",
            remote + "/receipt",
            "--run-id",
            "snapshot-qed-" + suffix,
            "--run-id",
            "snapshot-logp-" + suffix,
            "--ready-timeout",
            "60",
            "--timeout",
            "600",
        ]
        completed = call(command, timeout=1300)
        summary = json.loads(completed.stdout.decode().strip().splitlines()[-1])
        for name in (
            "summary.json",
            "request-1-qed.json",
            "request-2-logp.json",
            "response-1-qed.json",
            "response-2-logp.json",
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
            raise RuntimeError("frozen GenMol semantic validator did not pass")
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
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--container", default="genmol")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
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

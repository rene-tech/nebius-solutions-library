#!/usr/bin/env python3
"""Emit the SAI-25 phase-zero boundary proposal without evaluating Terraform.

This command is deliberately source-only. It accepts no cluster identity,
signature, credential, TLS material, UID/resourceVersion, handoff, or receipt.
Platform Security supplies and verifies those values only after installing the
reviewed boundary package. The output is a deterministic digest of reviewed
source bytes, not an activation authorization.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts/security/fs2-platform-security-boundary"
SOURCE_PATHS = (
    "Chart.yaml",
    "values.yaml",
    "values.schema.json",
    "templates/guard.yaml",
)


def proposal() -> dict[str, object]:
    files = {
        path: hashlib.sha256((CHART / path).read_bytes()).hexdigest()
        for path in SOURCE_PATHS
    }
    canonical_files = json.dumps(
        files, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode() + b"\n"
    return {
        "schema": "fs2-serve.nebius.ai/nim-admission-boundary-prepare/v1",
        "source_only": True,
        "chart": "charts/security/fs2-platform-security-boundary",
        "chart_tree_sha256": hashlib.sha256(canonical_files).hexdigest(),
        "files": files,
        "fixed_release": {
            "name": "fs2-platform-security-boundary",
            "namespace": "fs2-system",
            "custody": "external-platform-security",
        },
        "external_inputs_required_after_phase_zero": [
            "cluster UID",
            "epoch-unique principal and credential-expiry evidence",
            "complete native provider IAM and Kubernetes authorization evidence",
            "dynamic owner lookup namespaces",
        ],
        "forbidden_phase_zero_inputs": [
            "signed authorization",
            "policy or binding UID/resourceVersion",
            "TLS Secret identity",
            "installation receipt",
            "Kubernetes API data",
        ],
    }


def main() -> None:
    print(json.dumps(proposal(), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()

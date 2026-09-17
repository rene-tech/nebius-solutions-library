#!/usr/bin/env python3
"""Single-argument local-exec entrypoint for the sealed Terraform apply gate."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if __name__ == "__main__" or __package__ != "security":
    print(
        "Terraform apply verification is an import-only external-capsule payload",
        file=sys.stderr,
    )
    raise SystemExit(1)

from .execution_toolchain import ToolchainError, validate_current_python  # noqa: E402
from .image_security_evidence import EvidenceError  # noqa: E402
from .release_image_closure import verify_live_terraform_apply_gate  # noqa: E402


def main() -> int:
    if os.environ.get("FS2_EXTERNAL_CAPSULE_ACTIVE") != "1":
        print("Terraform apply gate requires the external capsule", file=sys.stderr)
        return 1
    try:
        if len(sys.argv) != 2:
            raise EvidenceError("apply gate expects one canonical JSON argument")
        request = json.loads(sys.argv[1])
        if not isinstance(request, dict) or set(request) != {
            "closure_path",
            "external_trust_path",
            "plan_root",
            "source_root",
            "surfaces_path",
            "toolchain_path",
            "trust_path",
        }:
            raise EvidenceError("apply gate request fields are incomplete")
        toolchain = Path(request["toolchain_path"]).resolve()
        trust = Path(request["trust_path"]).resolve()
        external_trust = Path(request["external_trust_path"]).resolve()
        source_root_value = os.environ.get("FS2_CAPSULE_SOURCE_ROOT", "")
        if not source_root_value or not Path(source_root_value).is_absolute():
            raise EvidenceError("capsule read-only source root is absent")
        source_root = Path(source_root_value).resolve()
        if source_root != Path(
            request["source_root"]
        ).resolve():
            raise EvidenceError("apply source differs from capsule read-only source root")
        if Path(os.environ.get("FS2_EXTERNAL_CAPSULE_TRUST", "")).resolve() != external_trust:
            raise EvidenceError("external capsule trust differs from planned contract")
        if Path(os.environ.get("FS2_IMAGE_GATE_TOOLCHAIN", "")).resolve() != toolchain:
            raise EvidenceError("apply toolchain differs from planned contract")
        if Path(os.environ.get("FS2_IMAGE_GATE_TRUST", "")).resolve() != trust:
            raise EvidenceError("apply trust differs from planned contract")
        if Path(os.environ.get("FS2_IMAGE_GATE_AUTHORIZATION", "")).resolve() != Path(
            request["closure_path"]
        ).resolve():
            raise EvidenceError("apply closure differs from planned contract")
        validate_current_python(lock_path=toolchain, trust_path=trust)
        verify_live_terraform_apply_gate(
            root=source_root,
            manifest_path=Path(request["surfaces_path"]).resolve(),
            trust_path=trust,
            closure_path=Path(request["closure_path"]).resolve(),
            plan_root=request["plan_root"],
        )
    except (EvidenceError, ToolchainError, OSError, ValueError, TypeError) as exc:
        print(f"Terraform apply release gate: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

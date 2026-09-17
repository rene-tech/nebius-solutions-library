#!/usr/bin/env python3
"""Apply one saved Terraform plan through an FD-sealed execution capsule.

The launcher opens the plan, trust, closure, signature, verifier and tool
executables with ``O_NOFOLLOW`` and retains those descriptors until Terraform
exits.  Terraform receives the saved plan through ``/proc/self/fd``; the
in-root gate later walks its process ancestry and re-reads that same descriptor
instead of trusting a caller-selected JSON sidecar.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Retained only as historical source for the rejected repository-local design.
# The external bootstrap owns signed Terraform apply and never invokes this
# file. Refuse before importing any repository module if somebody runs it.
if __name__ == "__main__":
    print(
        "sealed Terraform apply moved to the externally anchored capsule bootstrap",
        file=sys.stderr,
    )
    raise SystemExit(1)

raise ImportError(
    "repository-local saved-plan execution is retired; use the external capsule bootstrap"
)

try:
    from .execution_toolchain import (
        ToolchainError,
        open_verified_file,
        validate_current_python,
        validate_source_file,
        validated_tool,
    )
    from .image_security_evidence import EvidenceError, validate_detached_signature
    from .release_image_closure import verify_terraform_apply_gate
except ImportError:
    from execution_toolchain import (
        ToolchainError,
        open_verified_file,
        validate_current_python,
        validate_source_file,
        validated_tool,
    )
    from image_security_evidence import EvidenceError, validate_detached_signature
    from release_image_closure import verify_terraform_apply_gate


ROOTS = {"stages/foundation", "stages/workloads"}


def _read_fd(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return b"".join(chunks)


def _open_source(
    relative: str,
    *,
    source_root: Path,
    toolchain: Path,
    trust: Path,
) -> int:
    expected = validate_source_file(
        relative,
        source_root=source_root,
        lock_path=toolchain,
        trust_path=trust,
    )
    return open_verified_file(source_root / relative, expected)


def main() -> int:
    print(
        "repository-local saved-plan execution is permanently disabled; use the external capsule",
        file=sys.stderr,
    )
    return 1

    parser = argparse.ArgumentParser()
    parser.add_argument("root", choices=sorted(ROOTS))
    parser.add_argument("saved_plan", type=Path)
    parser.add_argument("release_closure", type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--surfaces", required=True, type=Path)
    parser.add_argument("--trust", required=True, type=Path)
    parser.add_argument("--toolchain", required=True, type=Path)
    args = parser.parse_args()

    descriptors: list[int] = []
    try:
        source_root = args.source_root.resolve()
        trust = args.trust.resolve()
        toolchain = args.toolchain.resolve()
        validate_current_python(lock_path=toolchain, trust_path=trust)
        for relative in (
            "security/execution_toolchain.py",
            "security/image_security_evidence.py",
            "security/release_image_closure.py",
            "security/sealed_terraform_apply.py",
            "security/semantic_yaml_images.py",
            "security/terraform_apply_gate_entrypoint.py",
            "security/yaml_image_references.py",
            "security/release-image-surfaces.json",
        ):
            descriptors.append(
                _open_source(
                    relative,
                    source_root=source_root,
                    toolchain=toolchain,
                    trust=trust,
                )
            )
        terraform_path, terraform_sha256 = validated_tool(
            "terraform", lock_path=toolchain, trust_path=trust
        )
        terraform_fd = open_verified_file(terraform_path, terraform_sha256)
        descriptors.append(terraform_fd)
        plan_fd = os.open(
            args.saved_plan.resolve(),
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptors.append(plan_fd)
        closure_fd = os.open(
            args.release_closure.resolve(),
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptors.append(closure_fd)
        signature = Path(f"{args.release_closure.resolve()}.sig")
        signature_fd = os.open(
            signature,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptors.append(signature_fd)
        validate_detached_signature(args.release_closure.resolve(), signature, trust)

        executable = f"/proc/self/fd/{terraform_fd}"
        plan_argument = f"/proc/self/fd/{plan_fd}"
        pass_fds = tuple(descriptors)
        show = subprocess.run(
            [executable, f"-chdir={source_root / args.root}", "show", "-json", plan_argument],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=pass_fds,
            env={
                "HOME": os.environ.get("HOME", "/nonexistent"),
                "TF_IN_AUTOMATION": "1",
            },
        )
        if show.returncode != 0:
            raise EvidenceError("exact saved Terraform plan could not be rendered")
        verify_terraform_apply_gate(
            root=source_root,
            manifest_path=args.surfaces.resolve(),
            trust_path=trust,
            closure_path=args.release_closure.resolve(),
            plan_root=args.root,
            plan_json=show.stdout,
        )

        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PATH", "PYTHONPATH", "PYTHONHOME"}
        }
        environment.update(
            {
                "FS2_SIGNED_PLAN_FD": str(plan_fd),
                "FS2_SIGNED_PLAN_TERRAFORM_SHA256": terraform_sha256,
                "FS2_SIGNED_PLAN_ROOT": args.root,
                "FS2_IMAGE_GATE_TOOLCHAIN": str(toolchain),
                "FS2_IMAGE_GATE_TRUST": str(trust),
                "FS2_IMAGE_GATE_AUTHORIZATION": str(args.release_closure.resolve()),
                "TF_IN_AUTOMATION": "1",
            }
        )
        applied = subprocess.run(
            [
                executable,
                f"-chdir={source_root / args.root}",
                "apply",
                "-input=false",
                plan_argument,
            ],
            check=False,
            pass_fds=pass_fds,
            env=environment,
        )
        return applied.returncode
    except (EvidenceError, OSError, ToolchainError) as exc:
        print(f"sealed Terraform apply: {exc}", file=sys.stderr)
        return 1
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())

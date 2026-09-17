#!/usr/bin/env python3
"""RETAINED REJECTED v3 apply prototype; never use for an authorized apply.

This predecessor is preserved as negative design evidence. The canonical
source successor is ``run_sai07_authorized_apply_v4.py`` and remains blocked.
If this rejected prototype is inspected in isolation, it expects PID 1 in the exact
OCI image pinned by the active capsule contract.  It copies the caller's plan
and platform kubeconfig into write-sealed memfds, pins every executable and the
source archive by descriptor, runs the external executor and acknowledgement
verifier from that archive, and gives Terraform the same sealed plan descriptor
that was inspected and acknowledged.  It never accepts an apply-plan path from
the environment and never deletes, truncates, replaces, or renames an artifact.

The checked-in v3 capsule contract is permanently blocked and must not be
activated. A later independently reviewed v4 activation must fill authoritative
image/provenance/runtime facts before the successor can execute.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import sai07_saved_plan_contract as saved_plan


ROOT = Path(__file__).resolve().parents[1]
CAPSULE_CONTRACT = (
    ROOT / "stages" / "pod-security-custody" / "execution-capsule-contract-v3.json"
)
CAPSULE_RUNTIME_CONTRACT = Path(
    "/opt/fs2-sai07/contracts/execution-capsule-contract-v3.json"
)
TRUST_LOCK = ROOT / "stages" / "pod-security-custody" / "custody-trust-lock-v3.json"
SCHEMA = "fs2-serve.nebius.ai/sai07-execution-capsule-contract/v3"
OCI_DIGEST_PREFIX = "sha256:"
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_KUBECONFIG_BYTES = 4 * 1024 * 1024
MAX_PLAN_BYTES = saved_plan.MAX_PLAN_BYTES
REQUIRED_SEALS = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE


class AuthorizedApplyError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def read_regular(path: Path, label: str, maximum: int) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise AuthorizedApplyError(f"{label} path must be absolute without traversal")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > maximum:
            raise AuthorizedApplyError(f"{label} is not a bounded nonempty regular file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if remaining or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise AuthorizedApplyError(f"{label} changed during descriptor-fenced read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def load_canonical(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    payload = read_regular(path, label, MAX_JSON_BYTES)
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthorizedApplyError(f"{label} is not JSON") from error
    if not isinstance(document, dict) or payload != canonical(document) + b"\n":
        raise AuthorizedApplyError(f"{label} must be canonical JSON with one terminal LF")
    return payload, document


def install_sealed_memfd(source: Path, target_fd: int, label: str, maximum: int) -> str:
    payload = read_regular(source, label, maximum)
    descriptor = os.memfd_create(label, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise AuthorizedApplyError(f"{label} memfd write made no progress")
            offset += written
        os.fsync(descriptor)
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, REQUIRED_SEALS)
        if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & REQUIRED_SEALS != REQUIRED_SEALS:
            raise AuthorizedApplyError(f"{label} memfd is not write sealed")
        os.dup2(descriptor, target_fd, inheritable=True)
    finally:
        os.close(descriptor)
    if fcntl.fcntl(target_fd, fcntl.F_GET_SEALS) & REQUIRED_SEALS != REQUIRED_SEALS:
        raise AuthorizedApplyError(f"{label} fixed descriptor lost its seals")
    return hashlib.sha256(payload).hexdigest()


def open_pinned(path: Path, expected_sha256: str, target_fd: int, label: str) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise AuthorizedApplyError(f"{label} path must be absolute without traversal")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
            raise AuthorizedApplyError(f"{label} is not a nonempty regular file")
        digest = hashlib.sha256()
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if remaining or (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise AuthorizedApplyError(f"{label} changed during descriptor-fenced hashing")
        if digest.hexdigest() != expected_sha256:
            raise AuthorizedApplyError(f"{label} differs from the active capsule contract")
        os.dup2(descriptor, target_fd, inheritable=True)
    finally:
        os.close(descriptor)


def tree_sha256(root: Path) -> str:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise AuthorizedApplyError("provider bundle must be an absolute real directory")
    records: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise AuthorizedApplyError("provider bundle may not contain symlinks")
        if path.is_dir():
            records.append({"path": relative, "type": "directory"})
            continue
        if not path.is_file():
            raise AuthorizedApplyError("provider bundle contains a non-file entry")
        records.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(read_regular(path, "provider binary", 512 * 1024 * 1024)).hexdigest(),
                "type": "file",
            }
        )
    if not records:
        raise AuthorizedApplyError("provider bundle is empty")
    return hashlib.sha256(canonical(records)).hexdigest()


def active_contract() -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    capsule_bytes, capsule = load_canonical(CAPSULE_CONTRACT, "execution capsule contract")
    runtime_capsule_bytes, runtime_capsule = load_canonical(
        CAPSULE_RUNTIME_CONTRACT,
        "runtime execution capsule contract",
    )
    _trust_bytes, trust = load_canonical(TRUST_LOCK, "custody trust lock")
    if (
        capsule.get("schema") != SCHEMA
        or capsule.get("activation") != "active"
        or runtime_capsule != capsule
        or runtime_capsule_bytes != capsule_bytes
    ):
        raise AuthorizedApplyError("repository-pinned execution capsule is not active")
    executor = trust.get("executor")
    if not isinstance(executor, dict):
        raise AuthorizedApplyError("custody trust executor contract is malformed")
    if (
        trust.get("activation") != "active"
        or executor.get("execution_capsule_contract_path")
        != "stages/pod-security-custody/execution-capsule-contract-v3.json"
        or executor.get("execution_capsule_contract_sha256")
        != hashlib.sha256(capsule_bytes).hexdigest()
    ):
        raise AuthorizedApplyError("custody trust lock does not activate this exact capsule")
    return capsule_bytes, capsule, executor


def run_bounded(command: list[str], pass_fds: tuple[int, ...], environment: dict[str, str], label: str) -> bytes:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        env=environment,
        pass_fds=pass_fds,
        timeout=900,
    )
    if completed.returncode != 0:
        raise AuthorizedApplyError(f"{label} rejected or failed")
    if len(completed.stdout) > MAX_JSON_BYTES:
        raise AuthorizedApplyError(f"{label} output exceeded the bounded limit")
    return completed.stdout


def parse_executor_fds(arguments: list[str]) -> tuple[int, ...]:
    descriptors: list[int] = []
    for option in ("--owner-token-fd", "--signing-key-fd"):
        if arguments.count(option) != 1:
            raise AuthorizedApplyError(f"external executor arguments require exactly one {option}")
        index = arguments.index(option)
        if index + 1 >= len(arguments) or not arguments[index + 1].isdigit():
            raise AuthorizedApplyError(f"external executor {option} is malformed")
        descriptor = int(arguments[index + 1])
        if descriptor in descriptors or descriptor in {191, 192, 193, 194, 197, 198}:
            raise AuthorizedApplyError("external executor descriptor aliases a capsule descriptor")
        os.fstat(descriptor)
        descriptors.append(descriptor)
    return tuple(descriptors)


def execute(args: argparse.Namespace, executor_arguments: list[str]) -> dict[str, str]:
    if os.getpid() != 1:
        raise AuthorizedApplyError("authorized apply must be PID 1 in the retained capsule")
    capsule_bytes, capsule, _executor = active_contract()
    runtime = capsule.get("runtime")
    image = capsule.get("image")
    kubernetes = capsule.get("kubernetes")
    if (
        not isinstance(runtime, dict)
        or not isinstance(image, dict)
        or not isinstance(kubernetes, dict)
    ):
        raise AuthorizedApplyError("execution capsule runtime/image contract is malformed")
    expected_runtime = {
        "filesystem": "read-only-oci-rootfs",
        "pid": 1,
        "saved_plan_fd": 197,
        "platform_kubeconfig_fd": 198,
        "python_fd": 191,
        "openssl_fd": 192,
        "kubectl_fd": 193,
        "terraform_fd": 194,
    }
    if any(runtime.get(key) != value for key, value in expected_runtime.items()):
        raise AuthorizedApplyError("execution capsule fixed runtime differs from the reviewed contract")
    image_digest = image.get("digest")
    if (
        not isinstance(image_digest, str)
        or not image_digest.startswith(OCI_DIGEST_PREFIX)
        or len(image_digest) != len(OCI_DIGEST_PREFIX) + 64
        or image.get("reference") is None
        or image.get("provenance_sha256") is None
        or image.get("sbom_sha256") is None
        or not isinstance(kubernetes.get("runtime_image_id"), str)
        or not kubernetes["runtime_image_id"].endswith(f"@{image_digest}")
        or not all(
            isinstance(kubernetes.get(field), str) and kubernetes[field]
            for field in ("admission_policy_uid", "namespace", "pod_uid")
        )
    ):
        raise AuthorizedApplyError(
            "execution capsule image/provenance/admission pins are incomplete"
        )
    for key, target_fd in (
        ("python", 191),
        ("openssl", 192),
        ("kubectl", 193),
        ("terraform", 194),
    ):
        open_pinned(
            Path(runtime[f"{key}_path"]),
            runtime[f"{key}_sha256"],
            target_fd,
            f"capsule {key}",
        )
    if tree_sha256(Path(runtime["provider_bundle_path"])) != runtime["provider_bundle_sha256"]:
        raise AuthorizedApplyError("Terraform provider bundle differs from the capsule pin")
    if tree_sha256(Path(runtime["source_tree_path"])) != runtime["source_tree_sha256"]:
        raise AuthorizedApplyError("SAI-07 source tree differs from the capsule pin")
    plan_sha256 = install_sealed_memfd(args.saved_plan, 197, "saved Terraform plan", MAX_PLAN_BYTES)
    kubeconfig_sha256 = install_sealed_memfd(
        args.platform_kubeconfig,
        198,
        "platform kubeconfig",
        MAX_KUBECONFIG_BYTES,
    )
    plan_path = Path("/proc/1/fd/197")
    kubeconfig_path = "/proc/1/fd/198"
    plan_contract = saved_plan.inspect_saved_plan(
        plan_path,
        Path("/proc/1/fd/194"),
        runtime["terraform_sha256"],
        runtime["terraform_version"],
    )
    if (
        plan_contract["saved_plan_sha256"] != plan_sha256
        or plan_contract["platform_kubeconfig_path"] != kubeconfig_path
        or plan_contract["platform_kubeconfig_sha256"] != kubeconfig_sha256
        or plan_contract["execution_capsule_contract_sha256"]
        != hashlib.sha256(capsule_bytes).hexdigest()
    ):
        raise AuthorizedApplyError("sealed plan does not bind this exact capsule/kubeconfig")
    forbidden = {"--platform-saved-plan", "--ack-output"}
    if any(item in forbidden for item in executor_arguments):
        raise AuthorizedApplyError("caller may not select the executor plan or acknowledgement path")
    external_fds = parse_executor_fds(executor_arguments)
    common_fds = (191, 192, 193, 194, 197, 198, *external_fds)
    environment = {
        "FS2_SAI07_CAPSULE_CONTRACT_SHA256": hashlib.sha256(capsule_bytes).hexdigest(),
        "FS2_SAI07_CAPSULE_CONTRACT_PATH": str(CAPSULE_RUNTIME_CONTRACT),
        "FS2_SAI07_CAPSULE_IMAGE_DIGEST": image_digest,
        "FS2_SAI07_OPENSSL_PATH": "/proc/1/fd/192",
        "FS2_SAI07_KUBECTL_PATH": "/proc/1/fd/193",
        "PATH": "/nonexistent",
        "TF_IN_AUTOMATION": "1",
    }
    run_bounded(
        [
            "/proc/1/fd/191",
            runtime["external_executor_path"],
            *executor_arguments,
            "--platform-saved-plan",
            str(plan_path),
            "--ack-output",
            str(args.acknowledgement),
        ],
        common_fds,
        environment,
        "immutable external executor",
    )
    query_bytes = read_regular(args.acknowledgement_query, "acknowledgement query", MAX_JSON_BYTES)
    try:
        query = json.loads(query_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthorizedApplyError("acknowledgement query is not JSON") from error
    if (
        not isinstance(query, dict)
        or canonical(query) != query_bytes
        or query.get("ack_path") != str(args.acknowledgement)
        or query.get("actual_saved_plan_path") != str(plan_path)
        or query.get("platform_kubeconfig_path") != kubeconfig_path
        or query.get("execution_capsule_contract_sha256")
        != hashlib.sha256(capsule_bytes).hexdigest()
    ):
        raise AuthorizedApplyError("acknowledgement query differs from the sealed capsule session")
    verifier_command = [
        "/proc/1/fd/191",
        runtime["verifier_path"],
    ]
    verifier_environment = {
        **environment,
        "FS2_SAI07_APPLY_QUERY": query_bytes.decode(),
        "FS2_SAI07_CAPSULE_PLAN_PATH": str(plan_path),
    }
    run_bounded(
        verifier_command,
        common_fds,
        verifier_environment,
        "pre-apply acknowledgement verifier",
    )
    terraform = "/proc/1/fd/194"
    apply = subprocess.run(
        [
            terraform,
            f"-chdir={args.terraform_root}",
            "apply",
            "-input=false",
            "-auto-approve",
            str(plan_path),
        ],
        check=False,
        env={
            **verifier_environment,
        },
        pass_fds=common_fds,
    )
    if apply.returncode != 0:
        raise AuthorizedApplyError("Terraform apply of the sealed acknowledged plan failed")
    run_bounded(
        verifier_command,
        common_fds,
        verifier_environment,
        "post-apply acknowledgement verifier",
    )
    post_apply_plan_contract = saved_plan.inspect_saved_plan(
        plan_path,
        Path("/proc/1/fd/194"),
        runtime["terraform_sha256"],
        runtime["terraform_version"],
    )
    if post_apply_plan_contract != plan_contract:
        raise AuthorizedApplyError("sealed plan/config contract changed across Terraform apply")
    return {
        "capsule_contract_sha256": hashlib.sha256(capsule_bytes).hexdigest(),
        "plan_sha256": plan_sha256,
        "status": "applied-exact-sealed-plan",
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--saved-plan", required=True, type=Path)
    result.add_argument("--platform-kubeconfig", required=True, type=Path)
    result.add_argument("--acknowledgement", required=True, type=Path)
    result.add_argument("--acknowledgement-query", required=True, type=Path)
    result.add_argument("--terraform-root", required=True, type=Path)
    result.add_argument("executor_arguments", nargs=argparse.REMAINDER)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        executor_arguments = args.executor_arguments
        if executor_arguments[:1] == ["--"]:
            executor_arguments = executor_arguments[1:]
        if not executor_arguments:
            raise AuthorizedApplyError("external executor arguments are required")
        result = execute(args, executor_arguments)
    except (AuthorizedApplyError, saved_plan.SavedPlanError, OSError, ValueError) as error:
        print(f"SAI-07 authorized apply rejected: {error}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

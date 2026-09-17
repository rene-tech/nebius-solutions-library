#!/usr/bin/env python3
"""Apply only the exact externally approved additive Terraform plan.

This entry point deliberately has no plan-generation or cleanup mode.  An
external security owner must sign the exact saved-plan bytes plus both the
expected predecessor and successor remote-state identities.  Terraform then
executes the same descriptor-bound bytes that were verified.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_BYTES = 64 * 1024 * 1024
SAFE_ACTIONS = {("no-op",), ("read",), ("create",)}
STATE_VERSION_ADAPTER = Path("/usr/libexec/fs2-security/terraform-state-version")
EXECUTION_PUBLIC_KEY = Path(
    "/etc/fs2-security-ro/authority/additive-plan-execution-public-key.pem"
)
EXECUTION_PROFILE = Path(
    "/etc/fs2-security-ro/authority/additive-plan-execution-profile.json"
)
ACCEPTED_SAI10 = "057386a3e0c616d79735adb43a97c19c48046608"
ACCEPTED_SAI10_TREE = "a244a4264b1ad5ad782848eed04d9986524921b4"
PROFILE_SCHEMA = "fs2-serve.nebius.ai/additive-plan-execution-profile/v1"
RECEIPT_SCHEMA = "fs2-serve.nebius.ai/additive-plan-execution/v2"
PROFILE_ENVIRONMENT = {
    "PATH",
    "HOME",
    "XDG_CONFIG_HOME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NEBIUS_CONFIG",
    "KUBECONFIG",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "HELM_DRIVER",
}


def _open(path: Path, *, root_owned: bool, executable: bool = False) -> tuple[int, bytes]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("custody path is invalid")
    directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    metadata = os.fstat(descriptor)
    filesystem = os.fstatvfs(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_BYTES
        or (
            root_owned
            and (
                metadata.st_uid != 0
                or metadata.st_mode & 0o022
                or (not executable and metadata.st_mode & 0o077)
            )
        )
        or (root_owned and not filesystem.f_flag & getattr(os, "ST_RDONLY", 1))
        or (executable and not metadata.st_mode & 0o111)
    ):
        os.close(descriptor)
        raise ValueError("custody input is not a bounded protected regular file")
    payload = b""
    while len(payload) <= MAX_BYTES:
        chunk = os.read(descriptor, min(65536, MAX_BYTES + 1 - len(payload)))
        if not chunk:
            break
        payload += chunk
    after = os.fstat(descriptor)
    if (
        len(payload) != metadata.st_size
        or (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        os.close(descriptor)
        raise ValueError("custody input changed during its descriptor-bound read")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return descriptor, payload


def _object(payload: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"{label} contains a duplicate field")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} is not JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} is not a SHA-256 digest")
    return value


def _open_directory(path: Path, *, root_owned: bool) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("custody directory is invalid")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_fd
        metadata = os.fstat(descriptor)
        filesystem = os.fstatvfs(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (root_owned and (metadata.st_uid != 0 or metadata.st_mode & 0o022))
            or (root_owned and not filesystem.f_flag & getattr(os, "ST_RDONLY", 1))
        ):
            raise ValueError("custody directory is not root-owned and read-only")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_at(directory_fd: int, name: str) -> tuple[int, bytes, os.stat_result]:
    if not name or "/" in name or name in {".", ".."}:
        raise ValueError("custody child name is invalid")
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_BYTES:
        os.close(descriptor)
        raise ValueError("custody child is not a bounded regular file")
    payload = b""
    while len(payload) <= MAX_BYTES:
        chunk = os.read(descriptor, min(65536, MAX_BYTES + 1 - len(payload)))
        if not chunk:
            break
        payload += chunk
    after = os.fstat(descriptor)
    if (
        len(payload) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        os.close(descriptor)
        raise ValueError("custody child changed during its descriptor-bound read")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return descriptor, payload, before


def _unchanged(descriptor: int, before: os.stat_result, label: str) -> None:
    after = os.fstat(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError(f"{label} changed during custodied execution")


def _load_execution_profile() -> dict[str, Any]:
    profile_fd, profile_bytes = _open(EXECUTION_PROFILE, root_owned=True)
    key_fd = terraform_fd = git_fd = cli_fd = data_fd = -1
    try:
        profile = _object(profile_bytes, "execution profile")
        expected = {
            "schema",
            "terraform_binary_path",
            "terraform_binary_sha256",
            "git_binary_path",
            "git_binary_sha256",
            "terraform_cli_config_path",
            "terraform_cli_config_sha256",
            "terraform_data_dir",
            "environment",
            "environment_file_sha256",
        }
        if set(profile) != expected or profile.get("schema") != PROFILE_SCHEMA:
            raise ValueError("execution profile fields or schema differ")
        environment = profile.get("environment")
        environment_files = profile.get("environment_file_sha256")
        if (
            not isinstance(environment, dict)
            or not set(environment) <= PROFILE_ENVIRONMENT
            or any(
                not isinstance(name, str)
                or not isinstance(value, str)
                or not value
                for name, value in environment.items()
            )
            or environment.get("HELM_DRIVER") != "configmap"
            or not isinstance(environment.get("PATH"), str)
            or not environment["PATH"]
            or not isinstance(environment_files, dict)
            or set(environment_files)
            != set(environment)
            & {
                "KUBECONFIG",
                "NEBIUS_CONFIG",
                "SSL_CERT_FILE",
                "AWS_SHARED_CREDENTIALS_FILE",
                "AWS_CONFIG_FILE",
            }
        ):
            raise ValueError("execution profile environment is not the fixed allowlist")
        for name in (
            "terraform_binary_path",
            "git_binary_path",
            "terraform_cli_config_path",
            "terraform_data_dir",
        ):
            value = profile.get(name)
            if not isinstance(value, str) or not value.startswith("/") or ".." in Path(value).parts:
                raise ValueError("execution profile path is not absolute and canonical")
        terraform_fd, terraform_bytes = _open(
            Path(profile["terraform_binary_path"]), root_owned=True, executable=True
        )
        git_fd, git_bytes = _open(
            Path(profile["git_binary_path"]), root_owned=True, executable=True
        )
        cli_fd, cli_bytes = _open(
            Path(profile["terraform_cli_config_path"]), root_owned=True
        )
        data_fd = _open_directory(Path(profile["terraform_data_dir"]), root_owned=True)
        for name in ("HOME", "XDG_CONFIG_HOME", "SSL_CERT_DIR"):
            if name in environment:
                directory_fd = _open_directory(Path(environment[name]), root_owned=True)
                os.close(directory_fd)
        for path_entry in environment["PATH"].split(":"):
            if not path_entry or not path_entry.startswith("/") or ".." in Path(path_entry).parts:
                raise ValueError("execution profile PATH is not an absolute fixed set")
            path_fd = _open_directory(Path(path_entry), root_owned=True)
            os.close(path_fd)
        for name, expected_sha256 in environment_files.items():
            environment_fd, environment_bytes = _open(
                Path(environment[name]), root_owned=True
            )
            try:
                if hashlib.sha256(environment_bytes).hexdigest() != _digest(
                    expected_sha256, f"{name} custody file"
                ):
                    raise ValueError(f"{name} differs from the fixed execution profile")
            finally:
                os.close(environment_fd)
        if hashlib.sha256(terraform_bytes).hexdigest() != _digest(
            profile.get("terraform_binary_sha256"), "Terraform binary"
        ):
            raise ValueError("Terraform binary differs from the fixed execution profile")
        if hashlib.sha256(git_bytes).hexdigest() != _digest(
            profile.get("git_binary_sha256"), "Git binary"
        ):
            raise ValueError("Git binary differs from the fixed execution profile")
        if hashlib.sha256(cli_bytes).hexdigest() != _digest(
            profile.get("terraform_cli_config_sha256"), "Terraform CLI config"
        ):
            raise ValueError("Terraform CLI config differs from the fixed execution profile")
        key_fd, key_bytes = _open(EXECUTION_PUBLIC_KEY, root_owned=True)
        sanitized_environment = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TF_IN_AUTOMATION": "1",
            "CHECKPOINT_DISABLE": "1",
            "TF_CLI_CONFIG_FILE": profile["terraform_cli_config_path"],
            "TF_DATA_DIR": profile["terraform_data_dir"],
            **environment,
        }
        return {
            "profile": profile,
            "profile_sha256": hashlib.sha256(_canonical(profile)).hexdigest(),
            "profile_fd": profile_fd,
            "key_fd": key_fd,
            "key_bytes": key_bytes,
            "terraform_fd": terraform_fd,
            "git_fd": git_fd,
            "cli_fd": cli_fd,
            "data_fd": data_fd,
            "environment": sanitized_environment,
        }
    except Exception:
        for descriptor in (profile_fd, key_fd, terraform_fd, git_fd, cli_fd, data_fd):
            if descriptor >= 0:
                os.close(descriptor)
        raise


def _terraform(
    execution: dict[str, Any],
    module: Path,
    *arguments: str,
    pass_fds: tuple[int, ...] = (),
) -> bytes:
    descriptors = tuple(set((execution["terraform_fd"], *pass_fds)))
    result = subprocess.run(
        [f"/proc/self/fd/{execution['terraform_fd']}", f"-chdir={module}", *arguments],
        check=False,
        capture_output=True,
        env=execution["environment"],
        timeout=900,
        pass_fds=descriptors,
    )
    if result.returncode != 0 or len(result.stdout) > MAX_BYTES:
        raise ValueError("Terraform custody command failed")
    return result.stdout


def _backend(execution: dict[str, Any]) -> tuple[dict[str, Any], int, os.stat_result]:
    descriptor, payload, before = _open_at(
        execution["data_fd"], "terraform.tfstate"
    )
    try:
        metadata = _object(payload, "Terraform backend metadata")
        backend = metadata.get("backend")
        if not isinstance(backend, dict) or set(backend) != {"type", "config", "hash"}:
            raise ValueError("Terraform backend metadata fields differ")
        config = backend.get("config")
        if backend.get("type") != "s3" or not isinstance(config, dict):
            raise ValueError("custodied execution requires an S3 backend")
        for field in ("bucket", "key", "region", "endpoints"):
            if (
                field not in config
                or config[field] is None
                or config[field] == ""
                or config[field] == {}
            ):
                raise ValueError("Terraform backend identity is incomplete")
        if config.get("use_lockfile") is not True:
            raise ValueError("custodied execution requires native state locking")
        return {"type": backend["type"], "config": config}, descriptor, before
    except Exception:
        os.close(descriptor)
        raise


def _state_version(
    execution: dict[str, Any],
    backend: dict[str, Any],
    expected_adapter_sha256: str,
) -> str:
    descriptor, adapter = _open(STATE_VERSION_ADAPTER, root_owned=True, executable=True)
    try:
        if hashlib.sha256(adapter).hexdigest() != expected_adapter_sha256:
            raise ValueError("state-version adapter digest differs from execution approval")
        config = backend["config"]
        result = subprocess.run(
            [
                f"/proc/self/fd/{descriptor}",
                "--bucket", str(config["bucket"]),
                "--key", str(config["key"]),
                "--region", str(config["region"]),
                "--endpoints-json", json.dumps(config["endpoints"], sort_keys=True),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=execution["environment"],
            timeout=30,
            pass_fds=(descriptor,),
        )
        if result.returncode != 0 or len(result.stdout.encode()) > MAX_BYTES:
            raise ValueError("remote state version adapter failed")
        value = _object(result.stdout.encode(), "remote state version")
        if set(value) != {"version_id"} or not isinstance(value["version_id"], str) or not value["version_id"]:
            raise ValueError("remote state version is incomplete")
        return value["version_id"]
    finally:
        os.close(descriptor)


def _state(
    execution: dict[str, Any],
    module: Path,
    backend: dict[str, Any],
    expected_adapter_sha256: str,
) -> dict[str, Any]:
    version_before = _state_version(execution, backend, expected_adapter_sha256)
    payload = _terraform(execution, module, "state", "pull")
    state = _object(payload, "remote state")
    addresses = sorted(
        line.strip()
        for line in _terraform(execution, module, "state", "list").decode().splitlines()
        if line.strip()
    )
    if (
        not addresses
        or addresses != sorted(set(addresses))
        or not isinstance(state.get("lineage"), str)
        or not state["lineage"]
        or not isinstance(state.get("serial"), int)
        or state["serial"] < 1
        or not isinstance(state.get("version"), int)
        or state["version"] < 4
    ):
        raise ValueError("remote state has no canonical managed-address closure")
    version_after = _state_version(execution, backend, expected_adapter_sha256)
    if version_before != version_after:
        raise ValueError("remote state changed during its custodied read")
    return {
        "backend_config_sha256": hashlib.sha256(_canonical(backend)).hexdigest(),
        "lineage": state.get("lineage"),
        "serial": state.get("serial"),
        "version_id": version_after,
        "snapshot_sha256": hashlib.sha256(payload).hexdigest(),
        "managed_addresses": addresses,
    }


def _git(
    execution: dict[str, Any],
    repository: Path,
    *arguments: str,
    accepted: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [f"/proc/self/fd/{execution['git_fd']}", "-C", os.fspath(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=execution["environment"],
        timeout=10,
        pass_fds=(execution["git_fd"],),
    )
    if (
        result.returncode not in accepted
        or len(result.stdout.encode()) > MAX_BYTES
        or len(result.stderr.encode()) > MAX_BYTES
    ):
        raise ValueError("Git custody command failed")
    return result


def _source_identity(
    execution: dict[str, Any], module: Path
) -> tuple[Path, str, str]:
    root = _terraform(
        execution, module, "version"
    )  # Proves the fixed descriptor-bound binary is callable before Git custody.
    if not root:
        raise ValueError("Terraform binary identity is absent")
    repository_result = _git(execution, module, "rev-parse", "--show-toplevel")
    repository_path = Path(repository_result.stdout.strip())
    status = _git(
        execution,
        repository_path,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    identities = _git(execution, repository_path, "rev-parse", "HEAD", "HEAD^{tree}")
    values = identities.stdout.splitlines()
    if status.stdout or len(values) != 2:
        raise ValueError("execution requires an exact clean Git commit and tree")
    return repository_path, values[0], values[1]


def _is_ancestor(
    execution: dict[str, Any], repository: Path, ancestor: str, descendant: str
) -> bool:
    result = _git(
        execution,
        repository,
        "merge-base",
        "--is-ancestor",
        ancestor,
        descendant,
        accepted=frozenset({0, 1}),
    )
    if result.stdout:
        raise ValueError("Git ancestry check returned unexpected output")
    return result.returncode == 0


def _verify_sai10_ancestry(
    execution: dict[str, Any], repository: Path, receipt: dict[str, Any]
) -> None:
    commit = receipt.get("accepted_sai10_commit")
    tree = receipt.get("accepted_sai10_tree")
    if commit != ACCEPTED_SAI10 or tree != ACCEPTED_SAI10_TREE:
        raise ValueError("execution approval lacks accepted SAI-10 commit/tree custody")
    observed_tree = _git(
        execution, repository, "show", "-s", "--format=%T", commit
    ).stdout.strip()
    if observed_tree != tree:
        raise ValueError("accepted SAI-10 commit does not have the approved tree")
    if not _is_ancestor(execution, repository, ACCEPTED_SAI10, "HEAD"):
        raise ValueError("accepted SAI-10 custody is not an ancestor of executing source")


def _verify_receipt(receipt: dict[str, Any], public_key: bytes) -> None:
    body = {key: value for key, value in receipt.items() if key not in {"payload_sha256", "signature"}}
    payload = _canonical(body)
    if receipt.get("payload_sha256") != hashlib.sha256(payload).hexdigest():
        raise ValueError("execution receipt payload digest differs")
    key = serialization.load_pem_public_key(public_key)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("execution approval key is not Ed25519")
    try:
        key.verify(base64.b64decode(receipt["signature"], validate=True), payload)
    except (InvalidSignature, KeyError, TypeError, ValueError) as exc:
        raise ValueError("execution receipt signature is invalid") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args()
    if not args.module.is_absolute() or ".." in args.module.parts:
        parser.error("--module must be an absolute path without traversal")
    plan_fd = receipt_fd = lock_fd = backend_fd = module_fd = -1
    execution: dict[str, Any] | None = None
    try:
        module_fd = _open_directory(args.module, root_owned=False)
        execution = _load_execution_profile()
        plan_fd, plan_bytes = _open(args.plan, root_owned=True)
        receipt_fd, receipt_bytes = _open(args.receipt, root_owned=True)
        receipt = _object(receipt_bytes, "execution receipt")
        _verify_receipt(receipt, execution["key_bytes"])
        expected_fields = {
            "schema", "execution_profile_sha256", "module_repository_path",
            "source_commit", "source_tree", "accepted_sai10_commit",
            "accepted_sai10_tree", "accepted_sai10_review_receipt_sha256",
            "terraform_lock_sha256", "plan_sha256", "plan_json_sha256",
            "state_version_adapter_sha256",
            "prior_state", "successor_state_contract", "payload_sha256", "signature",
        }
        if set(receipt) != expected_fields or receipt.get("schema") != RECEIPT_SCHEMA:
            raise ValueError("execution receipt fields or schema differ")
        if receipt.get("execution_profile_sha256") != execution["profile_sha256"]:
            raise ValueError("fixed execution profile differs from the signed approval")
        _digest(
            receipt.get("accepted_sai10_review_receipt_sha256"),
            "accepted SAI-10 review receipt",
        )
        adapter_sha256 = receipt.get("state_version_adapter_sha256")
        _digest(adapter_sha256, "state-version adapter")
        if hashlib.sha256(plan_bytes).hexdigest() != receipt["plan_sha256"]:
            raise ValueError("saved-plan bytes differ from approval")
        repository, source_commit, source_tree = _source_identity(execution, args.module)
        if (source_commit, source_tree) != (receipt["source_commit"], receipt["source_tree"]):
            raise ValueError("Git source identity differs from execution approval")
        try:
            module_relative = args.module.relative_to(repository).as_posix()
        except ValueError as exc:
            raise ValueError("module is outside the approved Git repository") from exc
        if module_relative != receipt.get("module_repository_path"):
            raise ValueError("module path differs from execution approval")
        _verify_sai10_ancestry(execution, repository, receipt)
        lock_fd, lock_bytes = _open(repository / ".terraform.lock.hcl", root_owned=False)
        if hashlib.sha256(lock_bytes).hexdigest() != _digest(
            receipt.get("terraform_lock_sha256"), "Terraform dependency lock"
        ):
            raise ValueError("Terraform dependency lock differs from execution approval")
        lock_before = os.fstat(lock_fd)
        backend, backend_fd, backend_before = _backend(execution)
        prior_state = _state(execution, args.module, backend, adapter_sha256)
        if prior_state != receipt["prior_state"]:
            raise ValueError("remote predecessor state differs from execution approval")
        plan_json = _terraform(
            execution,
            args.module,
            "show",
            "-json",
            f"/proc/self/fd/{plan_fd}",
            pass_fds=(plan_fd,),
        )
        if hashlib.sha256(plan_json).hexdigest() != receipt["plan_json_sha256"]:
            raise ValueError("saved-plan JSON differs from execution approval")
        plan = _object(plan_json, "saved plan")
        actions = {
            tuple(change.get("change", {}).get("actions", []))
            for change in plan.get("resource_changes", [])
        }
        if not actions <= SAFE_ACTIONS:
            raise ValueError("saved plan contains a non-additive action")
        successor_contract = receipt["successor_state_contract"]
        if (
            not isinstance(successor_contract, dict)
            or set(successor_contract) != {"backend_config_sha256", "lineage", "minimum_serial", "managed_addresses"}
            or successor_contract.get("backend_config_sha256") != prior_state["backend_config_sha256"]
            or successor_contract.get("lineage") != prior_state["lineage"]
            or successor_contract.get("minimum_serial") != prior_state["serial"] + 1
            or not isinstance(successor_contract.get("managed_addresses"), list)
            or not successor_contract["managed_addresses"]
            or successor_contract["managed_addresses"]
            != sorted(set(successor_contract["managed_addresses"]))
        ):
            raise ValueError("successor state contract is not canonical")
        _unchanged(backend_fd, backend_before, "Terraform backend descriptor")
        _terraform(
            execution,
            args.module,
            "apply",
            "-input=false",
            f"/proc/self/fd/{plan_fd}",
            pass_fds=(plan_fd,),
        )
        _unchanged(backend_fd, backend_before, "Terraform backend descriptor")
        successor_state = _state(execution, args.module, backend, adapter_sha256)
        _unchanged(backend_fd, backend_before, "Terraform backend descriptor")
        _unchanged(lock_fd, lock_before, "Terraform dependency lock")
        if (
            successor_state["lineage"] != successor_contract["lineage"]
            or successor_state["serial"] < successor_contract["minimum_serial"]
            or successor_state["version_id"] == prior_state["version_id"]
            or successor_state["managed_addresses"]
            != successor_contract["managed_addresses"]
        ):
            raise ValueError("post-apply remote state differs from execution approval")
        print(
            json.dumps(
                {
                    "applied": True,
                    "plan_sha256": receipt["plan_sha256"],
                    "successor_state": successor_state,
                },
                sort_keys=True,
            )
        )
        return 0
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"custodied additive plan rejected: {exc}", file=sys.stderr)
        return 2
    finally:
        for descriptor in (plan_fd, receipt_fd, lock_fd, backend_fd, module_fd):
            if descriptor >= 0:
                os.close(descriptor)
        if execution is not None:
            for name in (
                "profile_fd",
                "key_fd",
                "terraform_fd",
                "git_fd",
                "cli_fd",
                "data_fd",
            ):
                descriptor = execution[name]
                if descriptor >= 0:
                    os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())

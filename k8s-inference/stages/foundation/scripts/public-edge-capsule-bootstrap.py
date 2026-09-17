#!/usr/bin/env python3
"""Enter one source-enrolled public-edge execution capsule.

The static setgid launcher opens this file and the accepted release manifest
before Python starts.  This bootstrap accepts neither path nor digest from the
caller: it verifies the inherited, root-owned manifest; pins every accepted
source/tool/provider file by descriptor; then executes only the named source
snapshot.  The capsule group deliberately has no members, so an ordinary
caller cannot manufacture the effective-GID plus unreadable-manifest proof by
setting environment variables and invoking Python directly.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Mapping


MANIFEST_SCHEMA = "fs2-serve.nebius.ai/public-edge-execution-capsule/v1"
SOURCE_IDS = {
    "edge-client-identity-verifier",
    "inference-stack",
    "jobset-api-gate",
    "jobset-chart-materializer",
    "jobset-crd-upgrade",
    "jobset-release-verifier",
    "public-edge-verifier",
    "kueue-materializer",
    "kueue-admission-gate",
    "kueue-destroy-cleanup",
}
SOURCE_MODES = {
    "edge-client-identity-verifier": {"external"},
    "inference-stack": {"operator"},
    "jobset-api-gate": {"local-exec"},
    "jobset-chart-materializer": {"external"},
    "jobset-crd-upgrade": {"local-exec"},
    "jobset-release-verifier": {"local-exec"},
    "public-edge-verifier": {"external", "local-exec", "receipt-contract"},
    "kueue-materializer": {"local-exec"},
    "kueue-admission-gate": {"local-exec"},
    "kueue-destroy-cleanup": {"local-exec"},
}
SOURCE_PATHS = {
    "edge-client-identity-verifier": "stages/workloads/scripts/verify-edge-client-identity-receipt.py",
    "inference-stack": "inference-stack",
    "jobset-api-gate": "modules/jobset-controller/scripts/wait-for-jobset-api.sh",
    "jobset-chart-materializer": "modules/jobset-controller/scripts/materialize-chart.sh",
    "jobset-crd-upgrade": "modules/jobset-controller/scripts/apply-jobset-crd.sh",
    "jobset-release-verifier": "modules/jobset-controller/scripts/verify-jobset-release.sh",
    "public-edge-verifier": "stages/foundation/scripts/verify-public-edge-node-eligibility.py",
    "kueue-materializer": "stages/foundation/scripts/materialize-kueue-release.sh",
    "kueue-admission-gate": "stages/foundation/scripts/wait-for-kueue-deployment-admission.sh",
    "kueue-destroy-cleanup": "stages/foundation/scripts/cleanup-kueue-aggregate-roles.sh",
}
REQUIRED_TOOLS = {
    "awk",
    "bash",
    "cat",
    "crane",
    "date",
    "find",
    "git",
    "grep",
    "helm",
    "id",
    "install",
    "jq",
    "kubectl",
    "mktemp",
    "nebius",
    "openssl",
    "python3",
    "realpath",
    "rm",
    "sed",
    "sha256sum",
    "sleep",
    "stat",
    "tar",
    "terraform",
    "timeout",
    "tr",
    "wc",
}
REQUIRED_PROVIDER_ADDRESSES = {
    "registry.terraform.io/hashicorp/external",
    "registry.terraform.io/hashicorp/helm",
    "registry.terraform.io/hashicorp/kubernetes",
    "registry.terraform.io/hashicorp/random",
    "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius",
}
HEX_40 = re.compile(r"^[a-f0-9]{40}$")
HEX_64 = re.compile(r"^[a-f0-9]{64}$")
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_PINNED_FILE_BYTES = 1024 * 1024 * 1024


class CapsuleError(RuntimeError):
    """The installed capsule is not the accepted immutable release."""


def fail(message: str) -> None:
    raise CapsuleError(message)


def exact_object(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        fail(f"{label} must contain exactly {sorted(keys)}")
    return value


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def lower_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or HEX_64.fullmatch(value) is None:
        fail(f"{label} must be a lowercase SHA-256 digest")
    if value == "0" * 64:
        fail(f"{label} cannot be the zero digest")
    return value


def inherited_fd(variable: str) -> int:
    value = os.environ.get(variable, "")
    if not value.isdecimal():
        fail(f"{variable} is not an inherited descriptor")
    descriptor = int(value)
    if descriptor < 3:
        fail(f"{variable} cannot name a standard descriptor")
    return descriptor


def require_capsule_process() -> tuple[int, int]:
    real_gid = os.getgid()
    effective_gid = os.getegid()
    if effective_gid == real_gid or effective_gid in os.getgroups():
        fail("capsule process lacks the no-member setgid launcher proof")
    if os.environ.get("FS2_CAPSULE_LAUNCHER") != "fs2-public-edge-capsule-v1":
        fail("capsule launcher marker is absent")
    if sys.flags.isolated != 1 or not sys.dont_write_bytecode:
        fail("capsule Python must use isolated/no-bytecode mode")
    return real_gid, effective_gid


def read_inherited_file(
    descriptor: int,
    *,
    label: str,
    owner_uid: int,
    owner_gid: int,
    exact_mode: int,
    maximum_bytes: int,
) -> bytes:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != owner_uid
        or before.st_gid != owner_gid
        or stat.S_IMODE(before.st_mode) != exact_mode
    ):
        fail(f"{label} is not the exact protected regular file")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum_bytes:
            fail(f"{label} exceeds its accepted size bound")
    after = os.fstat(descriptor)
    stable = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_mode,
        before.st_uid,
        before.st_gid,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_mode,
        after.st_uid,
        after.st_gid,
    )
    if not stable or after.st_size != total:
        fail(f"{label} changed while it was read")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return b"".join(chunks)


def decode_canonical_manifest(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapsuleError("accepted capsule manifest is not UTF-8 JSON") from exc
    if canonical_bytes(value) + b"\n" != raw:
        fail("accepted capsule manifest must be canonical JSON plus one newline")
    manifest = exact_object(
        value,
        {
            "schema",
            "accepted_commit",
            "accepted_tree",
            "installed_source_root",
            "capsule_group",
            "launcher_sha256",
            "bootstrap_sha256",
            "sources",
            "tools",
            "terraform",
            "release_files",
            "installation_receipt",
        },
        "accepted capsule manifest",
    )
    if manifest["schema"] != MANIFEST_SCHEMA:
        fail("accepted capsule manifest has an unsupported schema")
    if not isinstance(manifest["accepted_commit"], str) or HEX_40.fullmatch(
        manifest["accepted_commit"]
    ) is None:
        fail("accepted capsule commit is malformed")
    if not isinstance(manifest["accepted_tree"], str) or HEX_40.fullmatch(
        manifest["accepted_tree"]
    ) is None:
        fail("accepted capsule tree is malformed")
    lower_digest(manifest["launcher_sha256"], "accepted launcher digest")
    lower_digest(manifest["bootstrap_sha256"], "accepted bootstrap digest")
    receipt = exact_object(
        manifest["installation_receipt"],
        {"issuer", "key_id", "payload_sha256", "signature", "reviewed_at"},
        "capsule installation receipt",
    )
    if not all(isinstance(receipt[key], str) and receipt[key] for key in receipt):
        fail("capsule installation receipt fields must be non-empty strings")
    payload_digest = lower_digest(
        receipt["payload_sha256"], "installation receipt payload digest"
    )
    signed_payload = {
        key: manifest[key] for key in manifest if key != "installation_receipt"
    }
    if hashlib.sha256(canonical_bytes(signed_payload)).hexdigest() != payload_digest:
        fail("installation receipt does not bind the accepted capsule payload")
    return manifest


def protected_parent_chain(path: Path, root: Path, capsule_gid: int, label: str) -> None:
    if not path.is_absolute() or not root.is_absolute():
        fail(f"{label} paths must be absolute")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise CapsuleError(f"{label} escapes the accepted capsule root") from exc
    candidates = [root]
    ancestor = root.parent
    while True:
        candidates.append(ancestor)
        if ancestor == ancestor.parent:
            break
        ancestor = ancestor.parent
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        candidates.append(current)
    for current in dict.fromkeys(candidates):
        details = os.stat(current, follow_symlinks=False)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != 0
            or details.st_gid not in {0, capsule_gid}
            or stat.S_IMODE(details.st_mode) & 0o022
        ):
            fail(f"{label} parent chain is not root-owned and protected")


def pin_manifest_file(
    root: Path,
    record: object,
    capsule_gid: int,
    label: str,
) -> tuple[int, str]:
    entry = exact_object(record, {"relative_path", "sha256"}, label)
    relative = entry["relative_path"]
    if (
        not isinstance(relative, str)
        or not relative
        or relative.startswith("/")
        or ".." in Path(relative).parts
    ):
        fail(f"{label} has an unsafe relative path")
    expected = lower_digest(entry["sha256"], f"{label} digest")
    path = root / relative
    protected_parent_chain(path, root, capsule_gid, label)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CapsuleError(f"cannot open accepted {label}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_gid not in {0, capsule_gid}
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size > MAX_PINNED_FILE_BYTES
        ):
            fail(f"{label} is not a protected regular file")
        hasher = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            fail(f"{label} changed while it was pinned")
        if hasher.hexdigest() != expected:
            fail(f"{label} differs from the accepted capsule digest")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.set_inheritable(descriptor, True)
        return descriptor, f"/proc/self/fd/{descriptor}"
    except BaseException:
        os.close(descriptor)
        raise


def sealed_memfd(name: str, content: bytes) -> int:
    if not hasattr(os, "memfd_create"):
        fail("sealed anonymous file descriptors are unavailable")
    descriptor = os.memfd_create(
        name, os.MFD_CLOEXEC | getattr(os, "MFD_ALLOW_SEALING", 0)
    )
    try:
        os.write(descriptor, content)
        os.lseek(descriptor, 0, os.SEEK_SET)
        seals = (
            fcntl.F_SEAL_SEAL
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_WRITE
        )
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
        if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & seals != seals:
            fail("Terraform CLI configuration memfd is not fully sealed")
        os.set_inheritable(descriptor, True)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def pin_capsule(manifest: Mapping[str, Any], capsule_gid: int) -> tuple[dict[str, str], tuple[int, ...]]:
    source_root_text = manifest["installed_source_root"]
    if not isinstance(source_root_text, str) or not source_root_text.startswith("/opt/fs2/"):
        fail("accepted source root must be under /opt/fs2")
    source_root = Path(source_root_text)
    root_details = os.stat(source_root, follow_symlinks=False)
    if (
        not stat.S_ISDIR(root_details.st_mode)
        or root_details.st_uid != 0
        or root_details.st_gid not in {0, capsule_gid}
        or stat.S_IMODE(root_details.st_mode) & 0o022
    ):
        fail("accepted source root is not root-owned and protected")

    release_files = manifest["release_files"]
    if not isinstance(release_files, list) or not release_files:
        fail("accepted capsule release file inventory must be non-empty")
    expected_files: list[str] = []
    for index, record in enumerate(release_files):
        entry = exact_object(
            record, {"relative_path", "sha256"}, f"release file {index}"
        )
        relative = entry["relative_path"]
        if not isinstance(relative, str):
            fail("release file path must be a string")
        expected_files.append(relative)
        descriptor, _path = pin_manifest_file(
            source_root, entry, capsule_gid, f"release file {index}"
        )
        os.close(descriptor)
    if expected_files != sorted(set(expected_files)):
        fail("accepted release file inventory must be sorted and unique")
    observed_files: list[str] = []
    for directory, directories, files in os.walk(source_root, followlinks=False):
        directory_path = Path(directory)
        for name in directories:
            candidate = directory_path / name
            if candidate.is_symlink():
                fail("accepted release tree cannot contain a directory symlink")
        for name in files:
            candidate = directory_path / name
            if candidate.is_symlink() or not candidate.is_file():
                fail("accepted release tree may contain only regular files")
            observed_files.append(candidate.relative_to(source_root).as_posix())
    if sorted(observed_files) != expected_files:
        fail("installed release file set differs from the accepted manifest")

    source_records = exact_object(manifest["sources"], SOURCE_IDS, "capsule sources")
    tool_records = exact_object(manifest["tools"], REQUIRED_TOOLS, "capsule tools")
    descriptors: list[int] = []
    paths: dict[str, str] = {}
    for name in sorted(SOURCE_IDS):
        record = source_records[name]
        if (
            not isinstance(record, Mapping)
            or record.get("relative_path") != SOURCE_PATHS[name]
        ):
            fail(f"source {name} is not mapped to its canonical release path")
        descriptor, path = pin_manifest_file(
            source_root, record, capsule_gid, f"source {name}"
        )
        descriptors.append(descriptor)
        paths[f"source:{name}"] = path
    for name in sorted(REQUIRED_TOOLS):
        descriptor, path = pin_manifest_file(
            source_root, tool_records[name], capsule_gid, f"tool {name}"
        )
        descriptors.append(descriptor)
        paths[name] = path

    terraform = exact_object(
        manifest["terraform"],
        {
            "provider_files",
            "provider_mirror_relative_path",
            "provider_overrides",
            "tool_bin_relative_path",
        },
        "capsule Terraform contract",
    )
    provider_files = terraform["provider_files"]
    if not isinstance(provider_files, list) or not provider_files:
        fail("capsule Terraform provider set must be non-empty")
    for index, record in enumerate(provider_files):
        descriptor, path = pin_manifest_file(
            source_root,
            record,
            capsule_gid,
            f"Terraform provider file {index}",
        )
        descriptors.append(descriptor)
        paths[f"provider:{index}"] = path
    mirror_relative = terraform["provider_mirror_relative_path"]
    if (
        not isinstance(mirror_relative, str)
        or not mirror_relative
        or mirror_relative.startswith("/")
        or ".." in Path(mirror_relative).parts
    ):
        fail("Terraform provider mirror path is unsafe")
    mirror_path = source_root / mirror_relative
    protected_parent_chain(
        mirror_path / "sentinel", source_root, capsule_gid, "provider mirror"
    )
    mirror_details = os.stat(mirror_path, follow_symlinks=False)
    if (
        not stat.S_ISDIR(mirror_details.st_mode)
        or mirror_details.st_uid != 0
        or stat.S_IMODE(mirror_details.st_mode) & 0o022
    ):
        fail("Terraform provider mirror is not root-owned and protected")
    overrides = exact_object(
        terraform["provider_overrides"],
        REQUIRED_PROVIDER_ADDRESSES,
        "Terraform provider overrides",
    )
    override_lines: list[str] = []
    for address in sorted(REQUIRED_PROVIDER_ADDRESSES):
        relative = overrides[address]
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or ".." in Path(relative).parts
        ):
            fail("Terraform provider override path is unsafe")
        override_path = source_root / relative
        protected_parent_chain(
            override_path / "sentinel",
            source_root,
            capsule_gid,
            f"provider override {address}",
        )
        details = os.stat(override_path, follow_symlinks=False)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != 0
            or stat.S_IMODE(details.st_mode) & 0o022
        ):
            fail("Terraform provider override is not root-owned and protected")
        override_lines.append(
            f"    {json.dumps(address)} = {json.dumps(str(override_path))}"
        )
    cli_config_text = "\n".join(
        [
            "provider_installation {",
            "  dev_overrides {",
            *override_lines,
            "  }",
            "  filesystem_mirror {",
            f"    path = {json.dumps(str(mirror_path))}",
            '    include = ["*/*", "*/*/*"]',
            "  }",
            "}",
            "",
        ]
    )
    if "direct" in cli_config_text or "network_mirror" in cli_config_text:
        fail("generated Terraform CLI configuration enabled network discovery")
    cli_config = cli_config_text.encode("utf-8")
    cli_config_fd = sealed_memfd("fs2-terraform-cli-config", cli_config)
    descriptors.append(cli_config_fd)
    paths["terraform_cli_config"] = f"/proc/self/fd/{cli_config_fd}"
    tool_bin_relative = terraform["tool_bin_relative_path"]
    if (
        not isinstance(tool_bin_relative, str)
        or not tool_bin_relative
        or tool_bin_relative.startswith("/")
        or ".." in Path(tool_bin_relative).parts
    ):
        fail("capsule tool-bin path is unsafe")
    tool_bin = source_root / tool_bin_relative
    protected_parent_chain(tool_bin / "sentinel", source_root, capsule_gid, "tool bin")
    details = os.stat(tool_bin, follow_symlinks=False)
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != 0 or stat.S_IMODE(details.st_mode) & 0o022:
        fail("capsule tool bin is not root-owned and protected")
    for name in REQUIRED_TOOLS:
        record = tool_records[name]
        if Path(record["relative_path"]).parent != Path(tool_bin_relative):
            fail(f"tool {name} is outside the accepted tool bin")
    paths["tool_bin"] = str(tool_bin)
    return paths, tuple(sorted(descriptors))


def main() -> int:
    _real_gid, capsule_gid = require_capsule_process()
    manifest_fd = inherited_fd("FS2_CAPSULE_MANIFEST_FD")
    python_fd = inherited_fd("FS2_CAPSULE_PYTHON_FD")
    launcher_fd = inherited_fd("FS2_CAPSULE_LAUNCHER_FD")
    bootstrap_fd = inherited_fd("FS2_CAPSULE_BOOTSTRAP_FD")
    manifest_raw = read_inherited_file(
        manifest_fd,
        label="accepted capsule manifest",
        owner_uid=0,
        owner_gid=capsule_gid,
        exact_mode=0o440,
        maximum_bytes=MAX_MANIFEST_BYTES,
    )
    manifest = decode_canonical_manifest(manifest_raw)
    if manifest["capsule_group"] != os.environ.get("FS2_CAPSULE_GROUP"):
        fail("accepted capsule group differs from the launcher identity")
    launcher_bytes = read_inherited_file(
        launcher_fd,
        label="capsule launcher",
        owner_uid=0,
        owner_gid=capsule_gid,
        exact_mode=0o2755,
        maximum_bytes=MAX_PINNED_FILE_BYTES,
    )
    bootstrap_bytes = read_inherited_file(
        bootstrap_fd,
        label="capsule bootstrap",
        owner_uid=0,
        owner_gid=capsule_gid,
        exact_mode=0o440,
        maximum_bytes=MAX_PINNED_FILE_BYTES,
    )
    if hashlib.sha256(launcher_bytes).hexdigest() != manifest["launcher_sha256"]:
        fail("running launcher differs from the accepted manifest")
    if hashlib.sha256(bootstrap_bytes).hexdigest() != manifest["bootstrap_sha256"]:
        fail("running bootstrap differs from the accepted manifest")
    python_bytes = read_inherited_file(
        python_fd,
        label="capsule Python interpreter",
        owner_uid=0,
        owner_gid=capsule_gid,
        exact_mode=0o550,
        maximum_bytes=MAX_PINNED_FILE_BYTES,
    )
    tool_records = manifest["tools"]
    if not isinstance(tool_records, Mapping):
        fail("capsule tool records are malformed")
    python_record = exact_object(
        tool_records.get("python3"), {"relative_path", "sha256"}, "Python tool"
    )
    if hashlib.sha256(python_bytes).hexdigest() != lower_digest(
        python_record["sha256"], "accepted Python digest"
    ):
        fail("running Python interpreter differs from the accepted tool digest")
    if len(sys.argv) < 3:
        fail("capsule bootstrap requires a logical source and mode")
    source_id, mode, *arguments = sys.argv[1:]
    if source_id not in SOURCE_IDS or mode not in SOURCE_MODES[source_id]:
        fail("capsule bootstrap source/mode pair is unsupported")
    paths, pinned_fds = pin_capsule(manifest, capsule_gid)
    pass_fds = tuple(
        sorted({manifest_fd, python_fd, launcher_fd, bootstrap_fd, *pinned_fds})
    )
    source_fd_path = paths[f"source:{source_id}"]
    source_fd = int(source_fd_path.rsplit("/", 1)[1])
    source_bytes = read_inherited_file(
        source_fd,
        label=f"accepted source {source_id}",
        owner_uid=0,
        owner_gid=os.fstat(source_fd).st_gid,
        exact_mode=stat.S_IMODE(os.fstat(source_fd).st_mode),
        maximum_bytes=MAX_PINNED_FILE_BYTES,
    )

    os.environ["FS2_CAPSULE_MANIFEST_SHA256"] = hashlib.sha256(manifest_raw).hexdigest()
    os.environ["FS2_CAPSULE_ACCEPTED_COMMIT"] = manifest["accepted_commit"]
    os.environ["FS2_CAPSULE_ACCEPTED_TREE"] = manifest["accepted_tree"]
    os.environ["FS2_CAPSULE_SOURCE_ROOT"] = manifest["installed_source_root"]
    os.environ["FS2_CAPSULE_SOURCE_ID"] = source_id
    os.environ["FS2_CAPSULE_SOURCE_SHA256"] = hashlib.sha256(source_bytes).hexdigest()
    os.environ["FS2_CAPSULE_TOOL_PATHS_JSON"] = json.dumps(paths, sort_keys=True)
    os.environ["FS2_CAPSULE_PASS_FDS"] = ",".join(str(item) for item in pass_fds)
    os.environ["TF_CLI_CONFIG_FILE"] = paths["terraform_cli_config"]
    os.environ["FS2_CAPSULE_TOOL_BIN"] = paths["tool_bin"]
    os.environ["PATH"] = paths["tool_bin"]

    if source_id == "inference-stack":
        sys.argv = [str(Path(manifest["installed_source_root"]) / "inference-stack"), *arguments]
    elif source_id == "public-edge-verifier" and mode == "local-exec":
        sys.argv = [source_id]
    elif source_id == "public-edge-verifier":
        sys.argv = [source_id, f"--{mode}"]
    elif source_id == "edge-client-identity-verifier":
        sys.argv = [source_id]
    else:
        os.execve(
            paths["bash"],
            [paths["bash"], source_fd_path],
            dict(os.environ),
        )
        fail("accepted shell source could not be executed")
    scope = {
        "__name__": "__main__",
        "__file__": str(Path(manifest["installed_source_root"]) / source_records_path(manifest, source_id)),
        "__package__": None,
    }
    exec(
        compile(
            source_bytes,
            f"capsule:{manifest['accepted_commit']}:{source_id}",
            "exec",
        ),
        scope,
        scope,
    )
    return 0


def source_records_path(manifest: Mapping[str, Any], source_id: str) -> str:
    sources = manifest["sources"]
    if not isinstance(sources, Mapping):
        fail("capsule sources are malformed")
    record = sources[source_id]
    if not isinstance(record, Mapping) or not isinstance(record.get("relative_path"), str):
        fail("capsule source record is malformed")
    return record["relative_path"]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CapsuleError as exc:
        print(f"public-edge capsule: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

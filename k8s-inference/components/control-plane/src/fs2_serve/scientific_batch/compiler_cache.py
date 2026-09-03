"""Identity-scoped compiler-cache preparation for scientific GPU stages."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

CACHE_IDENTITY_SCHEMA: Final = "fs2-serve.nebius.ai/scientific-compiler-cache-instance/v1"
CACHE_INSTANCE_PREFIX: Final = PurePosixPath("scientific-cache/v1")
CACHE_RUN_AS_USER: Final = 10001
CACHE_RUN_AS_GROUP: Final = 10001
CACHE_MARKER: Final = ".fs2-cache-instance.json"
CACHE_PROBE: Final = ".fs2-cache-nonroot-probe"
CACHE_SUB_PATH_LAYOUT: Final = (
    "scientific-cache/v1/{model_id}/{stage_id}/{variant_id}/"
    "{runtime_image_sha256}/{artifact_set_sha256}/{accelerator_sm}"
)
CACHE_PREPARATION: Final = "root-init-chown-and-nonroot-write-read-probe"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_KUBERNETES_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_ACCELERATOR_SM = re.compile(r"^sm[0-9]{2,3}[a-z]?$")
_ACCELERATOR_CLASS_TO_SM: Final = {
    "nvidia-h100-sxm5-80gb": "sm90",
}


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def artifact_set_sha256(artifacts: list[dict[str, object]]) -> str:
    """Hash a sorted stage-local closure of content and manifest identities."""

    normalized: list[dict[str, object]] = []
    for artifact in artifacts:
        artifact_id = artifact.get("artifact_id")
        content_sha256 = str(artifact.get("content_sha256", "")).removeprefix("sha256:")
        manifest_sha256 = str(artifact.get("manifest_sha256", "")).removeprefix("sha256:")
        if (
            not isinstance(artifact_id, str)
            or len(artifact_id) > 128
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", artifact_id) is None
            or _SHA256.fullmatch(content_sha256) is None
            or _SHA256.fullmatch(manifest_sha256) is None
        ):
            raise ValueError("compiler-cache artifact identity is invalid")
        normalized.append(
            {
                "artifact_id": artifact_id,
                "content_sha256": content_sha256,
                "manifest_sha256": manifest_sha256,
            }
        )
    if not normalized or len({item["artifact_id"] for item in normalized}) != len(normalized):
        raise ValueError("compiler-cache artifact closure is empty or duplicated")
    return sha256_json(sorted(normalized, key=lambda item: str(item["artifact_id"])))


def accelerator_sm(node_selector: dict[str, str]) -> str:
    """Resolve only reviewed deployment-owned accelerator classes to CUDA SM."""

    try:
        return _ACCELERATOR_CLASS_TO_SM[node_selector["accelerator.fs2.nebius/class"]]
    except KeyError as error:
        raise ValueError("scientific compiler cache has no reviewed accelerator-SM binding") from error


@dataclass(frozen=True, slots=True)
class CompilerCacheIdentity:
    model_id: str
    stage_id: str
    variant_id: str
    runtime_image_digest: str
    artifact_set_sha256: str
    accelerator_sm: str
    run_as_user: int = CACHE_RUN_AS_USER
    run_as_group: int = CACHE_RUN_AS_GROUP

    def __post_init__(self) -> None:
        image_sha256 = self.runtime_image_digest.removeprefix("sha256:")
        if (
            any(_KUBERNETES_NAME.fullmatch(value) is None for value in (self.model_id, self.stage_id, self.variant_id))
            or _SHA256.fullmatch(image_sha256) is None
            or _SHA256.fullmatch(self.artifact_set_sha256) is None
            or _ACCELERATOR_SM.fullmatch(self.accelerator_sm) is None
            or self.run_as_user != CACHE_RUN_AS_USER
            or self.run_as_group != CACHE_RUN_AS_GROUP
        ):
            raise ValueError("compiler-cache identity is invalid")
        object.__setattr__(self, "runtime_image_digest", f"sha256:{image_sha256}")

    def subject(self) -> dict[str, object]:
        return {
            "schema": CACHE_IDENTITY_SCHEMA,
            "model_id": self.model_id,
            "stage_id": self.stage_id,
            "variant_id": self.variant_id,
            "runtime_image_digest": self.runtime_image_digest,
            "artifact_set_sha256": self.artifact_set_sha256,
            "accelerator_sm": self.accelerator_sm,
            "run_as_user": self.run_as_user,
            "run_as_group": self.run_as_group,
        }

    @property
    def identity_sha256(self) -> str:
        return sha256_json(self.subject())

    @property
    def sub_path(self) -> str:
        image_sha256 = self.runtime_image_digest.removeprefix("sha256:")
        return (
            CACHE_INSTANCE_PREFIX
            / self.model_id
            / self.stage_id
            / self.variant_id
            / image_sha256
            / self.artifact_set_sha256
            / self.accelerator_sm
        ).as_posix()

    def marker(self) -> dict[str, object]:
        return {**self.subject(), "identity_sha256": self.identity_sha256, "sub_path": self.sub_path}

    @classmethod
    def from_mapping(cls, value: object) -> CompilerCacheIdentity:
        if not isinstance(value, dict) or set(value) != {
            "schema",
            "model_id",
            "stage_id",
            "variant_id",
            "runtime_image_digest",
            "artifact_set_sha256",
            "accelerator_sm",
            "run_as_user",
            "run_as_group",
            "identity_sha256",
            "sub_path",
        }:
            raise ValueError("compiler-cache identity fields differ")
        if value["schema"] != CACHE_IDENTITY_SCHEMA:
            raise ValueError("compiler-cache identity schema is unsupported")
        identity = cls(
            model_id=str(value["model_id"]),
            stage_id=str(value["stage_id"]),
            variant_id=str(value["variant_id"]),
            runtime_image_digest=str(value["runtime_image_digest"]),
            artifact_set_sha256=str(value["artifact_set_sha256"]),
            accelerator_sm=str(value["accelerator_sm"]),
            run_as_user=value["run_as_user"] if isinstance(value["run_as_user"], int) else -1,
            run_as_group=value["run_as_group"] if isinstance(value["run_as_group"], int) else -1,
        )
        if value["identity_sha256"] != identity.identity_sha256 or value["sub_path"] != identity.sub_path:
            raise ValueError("compiler-cache identity digest or subPath differs")
        return identity


def _open_or_create_directory(parent_fd: int, name: str, *, mode: int) -> int:
    try:
        os.mkdir(name, mode=mode, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)


def _probe_nonroot(directory_fd: int, *, uid: int, gid: int) -> None:
    """Create/read/remove a real file after irreversibly dropping to the workload identity."""

    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no branch - child always exits
        os.close(read_fd)
        try:
            if os.geteuid() == 0:
                os.setgroups([])
                os.setgid(gid)
                os.setuid(uid)
            elif (os.geteuid(), os.getegid()) != (uid, gid):
                raise PermissionError("cache probe cannot assume the required non-root identity")
            token = os.urandom(32)
            probe_fd = os.open(
                CACHE_PROBE,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                os.write(probe_fd, token)
                os.fsync(probe_fd)
                os.lseek(probe_fd, 0, os.SEEK_SET)
                if os.read(probe_fd, len(token) + 1) != token:
                    raise OSError("compiler-cache write/read probe differed")
            finally:
                os.close(probe_fd)
                os.unlink(CACHE_PROBE, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except BaseException as error:  # noqa: BLE001 - transmit child failure without traceback leakage
            os.write(write_fd, f"{type(error).__name__}: {error}".encode()[:2048])
            os._exit(1)
        os._exit(0)
    os.close(write_fd)
    message = os.read(read_fd, 2048)
    os.close(read_fd)
    _pid, status = os.waitpid(child, 0)
    if status != 0:
        raise PermissionError(message.decode(errors="replace") or "compiler-cache non-root probe failed")


def prepare_compiler_cache(root: Path, *, sub_path: str, identity: CompilerCacheIdentity) -> None:
    """Prepare one symlink-safe identity subtree and prove its non-root reuse contract."""

    if not root.is_absolute() or sub_path != identity.sub_path:
        raise ValueError("compiler-cache root or identity subPath differs")
    relative = PurePosixPath(sub_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("compiler-cache subPath is unsafe")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    opened = [root_fd]
    try:
        parent_fd = root_fd
        for part in relative.parts:
            child_fd = _open_or_create_directory(parent_fd, part, mode=0o755)
            opened.append(child_fd)
            parent_fd = child_fd
        os.fchown(parent_fd, identity.run_as_user, identity.run_as_group)
        os.fchmod(parent_fd, 0o700)
        expected = canonical_json(identity.marker()) + b"\n"
        try:
            marker_fd = os.open(CACHE_MARKER, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except FileNotFoundError:
            marker_fd = os.open(
                CACHE_MARKER,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o444,
                dir_fd=parent_fd,
            )
            try:
                os.write(marker_fd, expected)
                os.fsync(marker_fd)
            finally:
                os.close(marker_fd)
            os.fsync(parent_fd)
        else:
            try:
                actual = os.read(marker_fd, len(expected) + 1)
            finally:
                os.close(marker_fd)
            if actual != expected:
                raise ValueError("compiler-cache instance marker differs from the execution identity")
        _probe_nonroot(parent_fd, uid=identity.run_as_user, gid=identity.run_as_group)
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)

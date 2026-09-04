#!/usr/bin/env python3
"""Host-memory residency agent for the ``host-memory-residency`` mechanism.

This process is the mechanism's explicit price tag.  It holds the exact
immutable model payload in host RAM on one node and publishes a receipt that
the model Pod's init container verifies before the runtime starts.  Its
container memory request and limit are both the declared reservation, so the
node RAM the mechanism costs is scheduled and attributable instead of being an
incidental page-cache effect another workload can evict.

Two residency modes are supported and the receipt always says which one is in
force:

``locked-payload-residency``
    Every payload page is ``mmap``-ed and ``mlock``-ed, so the kernel may not
    reclaim it.  This needs ``CAP_IPC_LOCK``.  Residency is guaranteed.

``mapped-payload-residency``
    Every payload page is mapped and faulted in, then re-touched on each
    refresh.  Residency is best effort and the receipt says so, because the
    kernel may still reclaim a mapped page under pressure.

The agent never claims residency it did not achieve: if locking fails it
reports the failure and exits non-zero rather than silently degrading to the
weaker mode.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import errno
import fcntl
import hashlib
import json
import mmap
import os
import re
import resource
import stat
import sys
import time
from pathlib import Path
from typing import Any

RECEIPT_SCHEMA = "fs2-serve.nebius.ai/fast-start-host-memory-residency-receipt/v2"
LOCKED_MODE = "locked-payload-residency"
MAPPED_MODE = "mapped-payload-residency"
SUPPORTED_MODES = (LOCKED_MODE, MAPPED_MODE)
TOUCH_STRIDE = mmap.PAGESIZE
CAP_IPC_LOCK_BIT = 1 << 14
INCARNATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ResidencyError(RuntimeError):
    """A residency requirement could not be met."""


PROT_READ = 0x01
MAP_SHARED = 0x01
MAP_FAILED = ctypes.c_void_p(-1).value


def _libc() -> ctypes.CDLL:
    name = ctypes.util.find_library("c")
    if name is None:  # pragma: no cover - present on every supported image
        raise ResidencyError("libc is unavailable")
    library = ctypes.CDLL(name, use_errno=True)
    library.mmap.restype = ctypes.c_void_p
    library.mmap.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_long,
    ]
    library.munmap.restype = ctypes.c_int
    library.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    library.mlock.restype = ctypes.c_int
    library.mlock.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    return library


class _Residency:
    """One node's held payload.

    The payload is mapped through libc rather than through Python's ``mmap``
    because a read-only Python mapping does not expose an address that can be
    handed to ``mlock``.  Mapping directly also keeps both residency modes on
    one code path, so the only difference between them is the lock syscall.
    """

    def __init__(self, *, root: Path, mode: str) -> None:
        if mode not in SUPPORTED_MODES:
            raise ResidencyError(f"unsupported residency mode: {mode}")
        self._root = root
        self._mode = mode
        self._libc = _libc()
        self._held: list[tuple[Path, int, int]] = []
        self.resident_bytes = 0
        self.file_count = 0

    @staticmethod
    def locking_available() -> tuple[bool, str]:
        """Report whether this process can actually lock pages, and why not.

        ``capabilities.add`` only reaches the *bounding* set for a non-root
        container; without ambient capabilities the effective set stays empty
        and ``mlock`` is limited by ``RLIMIT_MEMLOCK``, which is a few
        megabytes. Checking up front turns a 16 GiB mapping that fails at the
        end into one precise sentence.
        """

        effective = 0
        bounding = 0
        try:
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("CapEff:"):
                    effective = int(line.split()[1], 16)
                elif line.startswith("CapBnd:"):
                    bounding = int(line.split()[1], 16)
        except OSError:  # pragma: no cover - /proc is always present on Linux
            return False, "capability state is unreadable"
        if effective & CAP_IPC_LOCK_BIT:
            return True, "CAP_IPC_LOCK is effective"
        soft, _hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)
        held = "bounding only" if bounding & CAP_IPC_LOCK_BIT else "absent"
        return False, (
            f"CAP_IPC_LOCK is {held} rather than effective and RLIMIT_MEMLOCK is {soft} bytes; "
            "a non-root container does not receive added capabilities in its effective set"
        )

    def acquire(self) -> None:
        if self._mode == LOCKED_MODE:
            available, detail = self.locking_available()
            if not available:
                raise ResidencyError(
                    f"{LOCKED_MODE} cannot guarantee residency here: {detail}. "
                    f"Declare {MAPPED_MODE} instead, which reports residency as best effort."
                )
        paths = sorted(path for path in self._root.rglob("*") if path.is_file() and not path.is_symlink())
        if not paths:
            raise ResidencyError("the retained payload contains no regular files")
        for path in paths:
            size = path.stat().st_size
            if size == 0:
                continue
            handle = os.open(path, os.O_RDONLY)
            try:
                address = self._libc.mmap(None, size, PROT_READ, MAP_SHARED, handle, 0)
            finally:
                os.close(handle)
            if not address or address == MAP_FAILED:
                raise ResidencyError(f"mmap of {path.name} failed with errno {ctypes.get_errno()}")
            if self._mode == LOCKED_MODE:
                if self._libc.mlock(ctypes.c_void_p(address), ctypes.c_size_t(size)) != 0:
                    errno = ctypes.get_errno()
                    self._libc.munmap(ctypes.c_void_p(address), ctypes.c_size_t(size))
                    raise ResidencyError(
                        f"mlock of {path.name} failed with errno {errno}; the holder needs "
                        "CAP_IPC_LOCK and a memory limit that covers the payload"
                    )
            else:
                self._touch(address, size)
            self._held.append((path, address, size))
            self.resident_bytes += size
            self.file_count += 1

    def refresh(self) -> None:
        """Re-touch mapped pages; locked pages need nothing."""

        if self._mode == LOCKED_MODE:
            return
        for _path, address, size in self._held:
            self._touch(address, size)

    @staticmethod
    def _touch(address: int, size: int) -> None:
        pointer = ctypes.cast(address, ctypes.POINTER(ctypes.c_ubyte))
        total = 0
        for offset in range(0, size, TOUCH_STRIDE):
            total += pointer[offset]
        if total < 0:  # pragma: no cover - defensive
            raise ResidencyError("payload touch failed")

    @property
    def guaranteed(self) -> bool:
        return self._mode == LOCKED_MODE


def localized_content_digest(root: Path) -> str:
    """Recompute the catalog artifact-manifest/v1 digest from localized bytes."""

    if not root.is_dir() or root.is_symlink():
        raise ResidencyError("the retained payload root must be a non-symlink directory")
    inventory: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise ResidencyError(f"the retained payload contains a non-regular entry: {relative}")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(8 * 1024 * 1024):
                digest.update(chunk)
        inventory.append({"path": relative, "bytes": info.st_size, "sha256": digest.hexdigest()})
    if not inventory:
        raise ResidencyError("the retained payload contains no regular files")
    canonical = (
        json.dumps(
            inventory,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _receipt_path(receipt_root: Path, holder_id: str, node_name: str) -> Path:
    """Keep one receipt per holder and node on the shared RWX claim."""

    return receipt_root / holder_id / node_name / "receipt.json"


def _coordination_lock_path(receipt_root: Path, holder_id: str, node_name: str) -> Path:
    return receipt_root / holder_id / node_name / "holder.lock"


def _incarnation_lock_path(receipt_root: Path, holder_id: str, node_name: str, incarnation: str) -> Path:
    if INCARNATION_PATTERN.fullmatch(incarnation) is None:
        raise ResidencyError("the holder incarnation is not a safe Kubernetes identity")
    return receipt_root / holder_id / node_name / "incarnations" / f"{incarnation}.lock"


def _acquire_lock(path: Path) -> int:
    """Acquire one process-lifetime lock and fail closed when locking is unsupported."""

    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ResidencyError(f"the residency holder lock is unavailable: {path.name}") from exc
    return descriptor


def _incarnation_is_live(path: Path) -> bool:
    """Prove the exact receipt-producing holder still owns its process lock."""

    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            return exc.errno in {errno.EACCES, errno.EAGAIN}
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")
    os.replace(temporary, path)


def _invalidate_receipt(path: Path) -> None:
    """Remove a prior holder epoch before hashing or acquiring any payload page."""

    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ResidencyError("the previous residency receipt could not be invalidated") from exc


def _environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ResidencyError(f"{name} is required")
    return value


def check(argv_max_age: float | None = None) -> int:
    """Readiness probe: the published receipt must be current and complete."""

    model_ref = _environment("FS2_RESIDENCY_MODEL_REF")
    holder_id = _environment("FS2_RESIDENCY_HOLDER_ID")
    receipt_root = Path(_environment("FS2_RESIDENCY_RECEIPT_ROOT"))
    expected_bytes = int(_environment("FS2_RESIDENCY_PAYLOAD_BYTES"))
    expected_digest = _environment("FS2_RESIDENCY_PAYLOAD_DIGEST")
    config_digest = _environment("FS2_RESIDENCY_CONFIG_DIGEST")
    node_name = _environment("FS2_NODE_NAME")
    expected_incarnation = _environment("FS2_RESIDENCY_HOLDER_INCARNATION")
    refresh_seconds = float(os.environ.get("FS2_RESIDENCY_REFRESH_SECONDS", "30"))
    max_age = argv_max_age if argv_max_age is not None else refresh_seconds * 4
    try:
        receipt = json.loads(_receipt_path(receipt_root, holder_id, node_name).read_text(encoding="ascii"))
    except (OSError, ValueError):
        sys.stderr.write("residency receipt is unavailable\n")
        return 1
    problems = []
    if receipt.get("schema") != RECEIPT_SCHEMA:
        problems.append("schema")
    if receipt.get("model_ref") != model_ref:
        problems.append("model_ref")
    if receipt.get("holder_id") != holder_id:
        problems.append("holder_id")
    if receipt.get("node_name") != node_name:
        problems.append("node")
    if receipt.get("holder_incarnation") != expected_incarnation:
        problems.append("holder_incarnation")
    if receipt.get("config_digest") != config_digest:
        problems.append("config_digest")
    if receipt.get("payload_digest") != expected_digest:
        problems.append("payload_digest")
    if receipt.get("content_digest_verified") is not True:
        problems.append("content_digest_verified")
    if int(receipt.get("resident_bytes", -1)) < expected_bytes:
        problems.append("resident_bytes")
    if time.time() - float(receipt.get("refreshed_at_epoch", 0.0)) > max_age:
        problems.append("freshness")
    try:
        incarnation_lock = _incarnation_lock_path(receipt_root, holder_id, node_name, expected_incarnation)
    except ResidencyError:
        problems.append("holder_incarnation")
    else:
        if not _incarnation_is_live(incarnation_lock):
            problems.append("holder_incarnation_liveness")
    if problems:
        sys.stderr.write(f"residency receipt is not admissible: {','.join(problems)}\n")
        return 1
    return 0


def hold() -> int:
    """Acquire residency, publish the receipt, and keep refreshing it."""

    model_ref = _environment("FS2_RESIDENCY_MODEL_REF")
    holder_id = _environment("FS2_RESIDENCY_HOLDER_ID")
    mode = _environment("FS2_RESIDENCY_MODE")
    payload_root = Path(_environment("FS2_RESIDENCY_PAYLOAD_ROOT"))
    payload_digest = _environment("FS2_RESIDENCY_PAYLOAD_DIGEST")
    payload_bytes = int(_environment("FS2_RESIDENCY_PAYLOAD_BYTES"))
    reserved_bytes = int(_environment("FS2_RESIDENCY_RESERVED_BYTES"))
    config_digest = _environment("FS2_RESIDENCY_CONFIG_DIGEST")
    receipt_root = Path(_environment("FS2_RESIDENCY_RECEIPT_ROOT"))
    node_name = _environment("FS2_NODE_NAME")
    holder_incarnation = _environment("FS2_RESIDENCY_HOLDER_INCARNATION")
    refresh_seconds = float(os.environ.get("FS2_RESIDENCY_REFRESH_SECONDS", "30"))
    receipt_path = _receipt_path(receipt_root, holder_id, node_name)
    coordination_lock = _acquire_lock(_coordination_lock_path(receipt_root, holder_id, node_name))
    incarnation_lock = _acquire_lock(_incarnation_lock_path(receipt_root, holder_id, node_name, holder_incarnation))
    try:
        _invalidate_receipt(receipt_path)

        started = time.monotonic()
        localized_digest = localized_content_digest(payload_root)
        if localized_digest != payload_digest:
            raise ResidencyError(
                f"localized payload digest {localized_digest} does not match declared {payload_digest}"
            )
        residency = _Residency(root=payload_root, mode=mode)
        residency.acquire()
        acquire_seconds = time.monotonic() - started
        if residency.resident_bytes < payload_bytes:
            raise ResidencyError(f"held {residency.resident_bytes} of the declared {payload_bytes} payload bytes")
        while True:
            residency.refresh()
            _write_receipt(
                receipt_path,
                {
                    "schema": RECEIPT_SCHEMA,
                    "model_ref": model_ref,
                    "holder_id": holder_id,
                    "holder_incarnation": holder_incarnation,
                    "node_name": node_name,
                    "config_digest": config_digest,
                    "payload_digest": localized_digest,
                    "content_digest_verified": True,
                    "payload_root": str(payload_root),
                    "residency_mode": mode,
                    "residency_guaranteed": residency.guaranteed,
                    "resident_bytes": residency.resident_bytes,
                    "resident_files": residency.file_count,
                    "reserved_bytes": reserved_bytes,
                    "acquire_seconds": round(acquire_seconds, 3),
                    "refreshed_at_epoch": round(time.time(), 3),
                    "refresh_seconds": refresh_seconds,
                },
            )
            time.sleep(refresh_seconds)
    finally:
        os.close(incarnation_lock)
        os.close(coordination_lock)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="readiness probe over the published receipt")
    parser.add_argument("--max-age-seconds", type=float, default=None)
    arguments = parser.parse_args(argv)
    try:
        if arguments.check:
            return check(arguments.max_age_seconds)
        return hold()
    except ResidencyError as error:
        sys.stderr.write(f"{error}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

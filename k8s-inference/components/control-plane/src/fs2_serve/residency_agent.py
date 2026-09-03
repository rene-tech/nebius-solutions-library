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
import json
import mmap
import os
import sys
import time
from pathlib import Path
from typing import Any

RECEIPT_SCHEMA = "fs2-serve.nebius.ai/fast-start-host-memory-residency-receipt/v1"
LOCKED_MODE = "locked-payload-residency"
MAPPED_MODE = "mapped-payload-residency"
SUPPORTED_MODES = (LOCKED_MODE, MAPPED_MODE)
TOUCH_STRIDE = mmap.PAGESIZE


class ResidencyError(RuntimeError):
    """A residency requirement could not be met."""


def _libc() -> ctypes.CDLL:
    name = ctypes.util.find_library("c")
    if name is None:  # pragma: no cover - present on every supported image
        raise ResidencyError("libc is unavailable")
    return ctypes.CDLL(name, use_errno=True)


class _Residency:
    """One node's held payload."""

    def __init__(self, *, root: Path, mode: str) -> None:
        if mode not in SUPPORTED_MODES:
            raise ResidencyError(f"unsupported residency mode: {mode}")
        self._root = root
        self._mode = mode
        self._libc = _libc() if mode == LOCKED_MODE else None
        self._held: list[tuple[Path, mmap.mmap]] = []
        self.resident_bytes = 0
        self.file_count = 0

    def acquire(self) -> None:
        paths = sorted(path for path in self._root.rglob("*") if path.is_file() and not path.is_symlink())
        if not paths:
            raise ResidencyError("the retained payload contains no regular files")
        for path in paths:
            size = path.stat().st_size
            if size == 0:
                continue
            handle = os.open(path, os.O_RDONLY)
            try:
                mapping = mmap.mmap(handle, size, prot=mmap.PROT_READ, flags=mmap.MAP_SHARED)
            finally:
                os.close(handle)
            if self._mode == LOCKED_MODE:
                assert self._libc is not None
                address = ctypes.addressof(ctypes.c_char.from_buffer(mapping))
                if self._libc.mlock(ctypes.c_void_p(address), ctypes.c_size_t(size)) != 0:
                    errno = ctypes.get_errno()
                    mapping.close()
                    raise ResidencyError(
                        f"mlock of {path.name} failed with errno {errno}; "
                        "the holder needs CAP_IPC_LOCK and a large enough memory limit"
                    )
            else:
                self._touch(mapping, size)
            self._held.append((path, mapping))
            self.resident_bytes += size
            self.file_count += 1

    def refresh(self) -> None:
        """Re-touch mapped pages; locked pages need nothing."""

        if self._mode == LOCKED_MODE:
            return
        for _path, mapping in self._held:
            self._touch(mapping, len(mapping))

    @staticmethod
    def _touch(mapping: mmap.mmap, size: int) -> None:
        total = 0
        for offset in range(0, size, TOUCH_STRIDE):
            total += mapping[offset]
        # Keep the read result observable so the loop cannot be optimised away.
        if total < 0:  # pragma: no cover - defensive
            raise ResidencyError("payload touch failed")

    @property
    def guaranteed(self) -> bool:
        return self._mode == LOCKED_MODE


def _receipt_path(receipt_root: Path, model_ref: str) -> Path:
    return receipt_root / model_ref / "receipt.json"


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")
    os.replace(temporary, path)


def _environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ResidencyError(f"{name} is required")
    return value


def check(argv_max_age: float | None = None) -> int:
    """Readiness probe: the published receipt must be current and complete."""

    model_ref = _environment("FS2_RESIDENCY_MODEL_REF")
    receipt_root = Path(_environment("FS2_RESIDENCY_RECEIPT_ROOT"))
    expected_bytes = int(_environment("FS2_RESIDENCY_PAYLOAD_BYTES"))
    expected_digest = _environment("FS2_RESIDENCY_PAYLOAD_DIGEST")
    config_digest = _environment("FS2_RESIDENCY_CONFIG_DIGEST")
    node_name = _environment("FS2_NODE_NAME")
    refresh_seconds = float(os.environ.get("FS2_RESIDENCY_REFRESH_SECONDS", "30"))
    max_age = argv_max_age if argv_max_age is not None else refresh_seconds * 4
    try:
        receipt = json.loads(_receipt_path(receipt_root, model_ref).read_text(encoding="ascii"))
    except (OSError, ValueError):
        sys.stderr.write("residency receipt is unavailable\n")
        return 1
    problems = []
    if receipt.get("schema") != RECEIPT_SCHEMA:
        problems.append("schema")
    if receipt.get("node_name") != node_name:
        problems.append("node")
    if receipt.get("config_digest") != config_digest:
        problems.append("config_digest")
    if receipt.get("payload_digest") != expected_digest:
        problems.append("payload_digest")
    if int(receipt.get("resident_bytes", -1)) < expected_bytes:
        problems.append("resident_bytes")
    if time.time() - float(receipt.get("refreshed_at_epoch", 0.0)) > max_age:
        problems.append("freshness")
    if problems:
        sys.stderr.write(f"residency receipt is not admissible: {','.join(problems)}\n")
        return 1
    return 0


def hold() -> int:
    """Acquire residency, publish the receipt, and keep refreshing it."""

    model_ref = _environment("FS2_RESIDENCY_MODEL_REF")
    mode = _environment("FS2_RESIDENCY_MODE")
    payload_root = Path(_environment("FS2_RESIDENCY_PAYLOAD_ROOT"))
    payload_digest = _environment("FS2_RESIDENCY_PAYLOAD_DIGEST")
    payload_bytes = int(_environment("FS2_RESIDENCY_PAYLOAD_BYTES"))
    reserved_bytes = int(_environment("FS2_RESIDENCY_RESERVED_BYTES"))
    config_digest = _environment("FS2_RESIDENCY_CONFIG_DIGEST")
    receipt_root = Path(_environment("FS2_RESIDENCY_RECEIPT_ROOT"))
    node_name = _environment("FS2_NODE_NAME")
    refresh_seconds = float(os.environ.get("FS2_RESIDENCY_REFRESH_SECONDS", "30"))

    started = time.monotonic()
    residency = _Residency(root=payload_root, mode=mode)
    residency.acquire()
    acquire_seconds = time.monotonic() - started
    if residency.resident_bytes < payload_bytes:
        raise ResidencyError(f"held {residency.resident_bytes} of the declared {payload_bytes} payload bytes")
    receipt_path = _receipt_path(receipt_root, model_ref)
    while True:
        residency.refresh()
        _write_receipt(
            receipt_path,
            {
                "schema": RECEIPT_SCHEMA,
                "model_ref": model_ref,
                "node_name": node_name,
                "config_digest": config_digest,
                "payload_digest": payload_digest,
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

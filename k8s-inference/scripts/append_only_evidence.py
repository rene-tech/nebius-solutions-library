#!/usr/bin/env python3
"""Owner-only, append-only, hash-chained evidence streams.

Each state transition is a new immutable file.  There is deliberately no
mutable head file, temporary file, rename, replace, truncate, or cleanup path.
The lexically last dense sequence is the head only after every preceding event
has been re-read and its filename/content/previous-hash binding has verified.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EVENT_NAME = re.compile(r"^(?P<sequence>[0-9]{12})-(?P<digest>[0-9a-f]{64})\.json$")
ZERO_HASH = "0" * 64


class EvidenceError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _require_owner_directory(path: Path, *, create: bool) -> Path:
    path = path.absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise EvidenceError("evidence path must contain no symlink")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise EvidenceError("evidence stream must be a real directory")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise EvidenceError("evidence stream must be owner-owned mode 0700")
    return path


def _stable_private_read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
        ):
            raise EvidenceError("evidence event must be an owner-only single-link file")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        stable = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
            "st_nlink",
        )
        if any(getattr(before, field) != getattr(after, field) for field in stable):
            raise EvidenceError("evidence event changed while read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def load_stream(path: Path, *, stream: str) -> list[dict[str, Any]]:
    path = _require_owner_directory(path, create=False)
    events: list[dict[str, Any]] = []
    previous = ZERO_HASH
    names = sorted(entry.name for entry in path.iterdir())
    for expected_sequence, name in enumerate(names, start=1):
        match = EVENT_NAME.fullmatch(name)
        if match is None or int(match.group("sequence")) != expected_sequence:
            raise EvidenceError("evidence stream is not a dense immutable sequence")
        encoded = _stable_private_read(path / name)
        digest = sha256_bytes(encoded)
        if digest != match.group("digest"):
            raise EvidenceError("evidence event filename does not bind its bytes")
        try:
            event = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise EvidenceError("evidence event is not valid JSON") from error
        required = {
            "schema",
            "stream",
            "sequence",
            "previous_sha256",
            "recorded_at",
            "event",
            "state_sha256",
            "state",
        }
        if not isinstance(event, dict) or set(event) != required:
            raise EvidenceError("evidence event has an invalid schema")
        if (
            event["schema"] != "fs2-serve.nebius.ai/append-only-event/v1"
            or event["stream"] != stream
            or event["sequence"] != expected_sequence
            or event["previous_sha256"] != previous
            or not isinstance(event["event"], str)
            or not event["event"]
            or not isinstance(event["recorded_at"], str)
            or event["state_sha256"] != sha256_bytes(canonical_bytes(event["state"]))
        ):
            raise EvidenceError("evidence event chain or state binding is invalid")
        events.append(event)
        previous = digest
    return events


def append_event(path: Path, *, stream: str, event: str, state: Any) -> dict[str, Any]:
    path = _require_owner_directory(path, create=True)
    events = load_stream(path, stream=stream)
    previous = ZERO_HASH if not events else sha256_bytes(canonical_bytes(events[-1]))
    sequence = len(events) + 1
    document = {
        "schema": "fs2-serve.nebius.ai/append-only-event/v1",
        "stream": stream,
        "sequence": sequence,
        "previous_sha256": previous,
        "recorded_at": utc_timestamp(),
        "event": event,
        "state_sha256": sha256_bytes(canonical_bytes(state)),
        "state": state,
    }
    encoded = canonical_bytes(document)
    digest = sha256_bytes(encoded)
    destination = path / f"{sequence:012d}-{digest}.json"
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        written = 0
        while written < len(encoded):
            written += os.write(descriptor, encoded[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    verified = load_stream(path, stream=stream)
    if verified[-1] != document:
        raise EvidenceError("published evidence event did not re-verify")
    return {"sequence": sequence, "sha256": digest, "state": state}


def latest_state(path: Path, *, stream: str) -> Any:
    events = load_stream(path, stream=stream)
    if not events:
        raise EvidenceError("evidence stream is empty")
    return events[-1]["state"]

#!/usr/bin/env python3
"""Owner-only, append-only, crash-recoverable evidence streams.

An event is written to a preserved unique fragment and fsynced before that
inode is atomically hard-linked into the dense event namespace.  A crash can
therefore leave an uncommitted fragment, but can never expose a partial event
as the stream head.  Fragments are evidence: they are never removed, renamed,
truncated, or reused.  This local stream is an operational journal; externally
anchored signatures provide the independent trust root.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EVENT_NAME = re.compile(r"^(?P<sequence>[0-9]{12})-(?P<digest>[0-9a-f]{64})\.json$")
FRAGMENT_NAME = re.compile(
    r"^(?P<sequence>[0-9]{12})-(?P<fragment>[0-9a-f]{32})\.fragment$"
)
ZERO_HASH = "0" * 64
LOCK_NAME = ".append.lock"


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


def _stable_private_read(path: Path, *, expected_links: frozenset[int]) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink not in expected_links
        ):
            raise EvidenceError("evidence file ownership, mode, or link count is unsafe")
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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _append_lock(path: Path):
    """Serialize writers without a mutable head file or cleanup operation."""

    import fcntl

    descriptor = os.open(
        path / LOCK_NAME,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise EvidenceError("evidence append lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def load_stream(path: Path, *, stream: str) -> list[dict[str, Any]]:
    path = _require_owner_directory(path, create=False)
    events: list[dict[str, Any]] = []
    previous = ZERO_HASH
    names: list[str] = []
    for entry in path.iterdir():
        if entry.name == LOCK_NAME:
            _stable_private_read(entry, expected_links=frozenset({1}))
            continue
        if FRAGMENT_NAME.fullmatch(entry.name) is not None:
            # A fragment may be incomplete after a crash.  Preserve it and
            # verify its metadata, but do not make it a committed chain node.
            descriptor = os.open(
                entry, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_nlink not in (1, 2)
                ):
                    raise EvidenceError("evidence fragment is unsafe")
            finally:
                os.close(descriptor)
            continue
        names.append(entry.name)
    names.sort()
    for expected_sequence, name in enumerate(names, start=1):
        match = EVENT_NAME.fullmatch(name)
        if match is None or int(match.group("sequence")) != expected_sequence:
            raise EvidenceError("evidence stream is not a dense immutable sequence")
        encoded = _stable_private_read(
            path / name,
            # Link-one is accepted for historical v1 events.  New events keep
            # their preserved fragment as the second hard link.
            expected_links=frozenset({1, 2}),
        )
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


def _recover_complete_head_fragment(
    path: Path, *, stream: str, events: list[dict[str, Any]]
) -> bool:
    """Publish one fully durable next event left between fsync and link.

    Partial fragments remain preserved and ignored. More than one valid but
    different next event is an ambiguity and fails closed; no fragment is ever
    renamed, truncated, or removed during recovery.
    """

    sequence = len(events) + 1
    previous = ZERO_HASH if not events else sha256_bytes(canonical_bytes(events[-1]))
    candidates: list[tuple[Path, bytes, dict[str, Any]]] = []
    for entry in sorted(path.iterdir(), key=lambda value: value.name):
        match = FRAGMENT_NAME.fullmatch(entry.name)
        if match is None or int(match.group("sequence")) != sequence:
            continue
        try:
            encoded = _stable_private_read(entry, expected_links=frozenset({1, 2}))
            document = json.loads(encoded)
        except (EvidenceError, json.JSONDecodeError, UnicodeDecodeError):
            continue
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
        if (
            not isinstance(document, dict)
            or set(document) != required
            or document.get("schema")
            != "fs2-serve.nebius.ai/append-only-event/v1"
            or document.get("stream") != stream
            or document.get("sequence") != sequence
            or document.get("previous_sha256") != previous
            or document.get("state_sha256")
            != sha256_bytes(canonical_bytes(document.get("state")))
            or canonical_bytes(document) != encoded
        ):
            continue
        candidates.append((entry, encoded, document))
    digests = {sha256_bytes(encoded) for _entry, encoded, _document in candidates}
    if len(digests) > 1:
        raise EvidenceError("evidence recovery found conflicting complete head fragments")
    if not candidates:
        return False
    fragment, encoded, _document = candidates[0]
    digest = sha256_bytes(encoded)
    destination = path / f"{sequence:012d}-{digest}.json"
    if destination.exists() or destination.is_symlink():
        committed = _stable_private_read(destination, expected_links=frozenset({2}))
        if committed != encoded or destination.stat().st_ino != fragment.stat().st_ino:
            raise EvidenceError("evidence recovery destination conflicts with fragment")
        return False
    os.link(fragment, destination, follow_symlinks=False)
    _fsync_directory(path)
    if _stable_private_read(destination, expected_links=frozenset({2})) != encoded:
        raise EvidenceError("recovered evidence event did not verify")
    return True


def append_event(path: Path, *, stream: str, event: str, state: Any) -> dict[str, Any]:
    path = _require_owner_directory(path, create=True)
    with _append_lock(path):
        events = load_stream(path, stream=stream)
        if _recover_complete_head_fragment(path, stream=stream, events=events):
            events = load_stream(path, stream=stream)
        previous = (
            ZERO_HASH if not events else sha256_bytes(canonical_bytes(events[-1]))
        )
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
        fragment = path / f"{sequence:012d}-{uuid.uuid4().hex}.fragment"
        descriptor = os.open(
            fragment,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            written = 0
            while written < len(encoded):
                count = os.write(descriptor, encoded[written:])
                if count <= 0:
                    raise EvidenceError("evidence fragment write made no progress")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if _stable_private_read(fragment, expected_links=frozenset({1})) != encoded:
            raise EvidenceError("evidence fragment write did not verify")
        destination = path / f"{sequence:012d}-{digest}.json"
        os.link(fragment, destination, follow_symlinks=False)
        _fsync_directory(path)
        if _stable_private_read(destination, expected_links=frozenset({2})) != encoded:
            raise EvidenceError("committed evidence event did not verify")
        verified = load_stream(path, stream=stream)
        if verified[-1] != document:
            raise EvidenceError("published evidence event did not re-verify")
        return {"sequence": sequence, "sha256": digest, "state": state}


def latest_state(path: Path, *, stream: str) -> Any:
    events = load_stream(path, stream=stream)
    if not events:
        raise EvidenceError("evidence stream is empty")
    return events[-1]["state"]

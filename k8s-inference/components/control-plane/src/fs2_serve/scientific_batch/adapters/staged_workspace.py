"""Trusted completion and bounded workspace handoffs for staged adapters.

The model process does not get to declare that a stage completed.  Every
staged adapter runs behind the companion-injected runner, then this module
binds the runner's atomic marker to the frozen ``StageInvocation`` before it
reads any output.  Intermediate workspaces are encoded as deterministic,
symlink-free zstd tar archives that the existing companion materializer can
round-trip without a model-specific transport.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import zstandard

from ..models import StageInvocation
from .primitives import ScientificAdapterError

if TYPE_CHECKING:
    from . import CollectedStageOutput

STAGE_RUNNER_RELATIVE_PATH = ".fs2/stage-runner.py"
STAGE_COMPLETION_RELATIVE_PATH = ".fs2/stage-complete.json"
STAGE_COMPLETION_SCHEMA = "fs2-serve.nebius.ai/scientific-stage-completion/v1"
_MAX_COMPLETION_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    """One deterministic regular file or directory in a stage handoff."""

    name: str
    is_directory: bool
    content: bytes = b""


def wrap_stage_argv(workspace: str, command: tuple[str, ...]) -> tuple[str, ...]:
    """Place an exec-form model command behind the trusted completion runner."""

    if not command or any(not value or "\x00" in value for value in command):
        raise ScientificAdapterError("stage command contains an invalid argument")
    return ("python", f"{workspace}/{STAGE_RUNNER_RELATIVE_PATH}", "--", *command)


def unwrapped_stage_argv(invocation: StageInvocation, *, label: str) -> tuple[str, ...]:
    expected = f"{invocation.working_directory}/{STAGE_RUNNER_RELATIVE_PATH}"
    if invocation.argv[:3] != ("python", expected, "--") or len(invocation.argv) < 4:
        raise ScientificAdapterError(f"{label} stage does not use the trusted completion runner")
    return invocation.argv[3:]


def _read_stable_file(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    """Read one regular file without following a symlink or accepting a race."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ScientificAdapterError(f"{label} is unavailable or unsafe") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= maximum_bytes:
            raise ScientificAdapterError(f"{label} size or type is outside the bound")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(maximum_bytes + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        current = path.lstat()
    except OSError as error:
        raise ScientificAdapterError(f"{label} changed while it was read") from error
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if (
        len(content) != before.st_size
        or identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or identity != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        or stat.S_ISLNK(current.st_mode)
    ):
        raise ScientificAdapterError(f"{label} changed while it was read")
    return content


def completion_marker(invocation: StageInvocation, workspace: Path, *, label: str) -> str:
    """Validate and digest the atomic marker for the frozen invocation."""

    from . import CollectionPendingError

    marker = workspace.joinpath(*PurePosixPath(STAGE_COMPLETION_RELATIVE_PATH).parts)
    if not marker.exists():
        raise CollectionPendingError(f"{label} stage has not atomically published completion")
    try:
        root = workspace.resolve(strict=True)
        resolved = marker.resolve(strict=True)
    except OSError as error:
        raise ScientificAdapterError(f"{label} completion marker is unavailable") from error
    if root not in resolved.parents or marker.is_symlink():
        raise ScientificAdapterError(f"{label} completion marker escapes the stage workspace")
    payload = _read_stable_file(marker, maximum_bytes=_MAX_COMPLETION_BYTES, label=f"{label} completion marker")
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ScientificAdapterError(f"{label} completion marker is invalid JSON") from error
    command = unwrapped_stage_argv(invocation, label=label)
    argv_payload = json.dumps(command, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    expected = {
        "schema": STAGE_COMPLETION_SCHEMA,
        "status": "passed",
        "stage_id": invocation.stage_id,
        "shard_id": invocation.shard_id,
        "logical_output_id": invocation.produces,
        "collector_id": invocation.collector_id,
        "validator_id": invocation.validator_id,
        "argv_sha256": hashlib.sha256(argv_payload).hexdigest(),
    }
    if value != expected:
        raise ScientificAdapterError(f"{label} completion marker differs from the frozen invocation")
    return hashlib.sha256(payload).hexdigest()


def atomic_publish(path: Path, content: bytes, *, workspace: Path, label: str) -> None:
    """Durably replace one controller-owned output beneath ``workspace/.fs2``."""

    root = workspace.resolve(strict=True)
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as error:
        raise ScientificAdapterError(f"{label} publication directory is unavailable") from error
    if root not in resolved_parent.parents or parent.is_symlink():
        raise ScientificAdapterError(f"{label} publication escapes the stage workspace")
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=resolved_parent,
        prefix=f".{path.name}.",
        suffix=".partial",
        delete=False,
    ) as output:
        temporary = Path(output.name)
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    try:
        os.chmod(temporary, 0o400)
        os.replace(temporary, path)
        directory = os.open(resolved_parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def snapshot_workspace(
    workspace: Path,
    *,
    label: str,
    maximum_members: int,
    maximum_content_bytes: int,
) -> tuple[SnapshotEntry, ...]:
    """Capture one bounded symlink-free workspace after terminal completion."""

    try:
        root = workspace.resolve(strict=True)
    except OSError as error:
        raise ScientificAdapterError(f"{label} workspace is unavailable") from error
    if workspace.is_symlink() or not root.is_dir():
        raise ScientificAdapterError(f"{label} workspace is unsafe")
    entries: list[SnapshotEntry] = []
    total = 0
    files = 0
    try:
        candidates = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    except OSError as error:
        raise ScientificAdapterError(f"{label} workspace changed during collection") from error
    for path in candidates:
        relative = path.relative_to(root)
        if relative.parts[0] == ".fs2":
            continue
        name = PurePosixPath(*relative.parts).as_posix()
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise ScientificAdapterError(f"{label} workspace changed during collection") from error
        if root not in resolved.parents or stat.S_ISLNK(metadata.st_mode):
            raise ScientificAdapterError(f"{label} handoff contains an escaping or symbolic-link entry")
        if stat.S_ISDIR(metadata.st_mode):
            entries.append(SnapshotEntry(name=name, is_directory=True))
        elif stat.S_ISREG(metadata.st_mode):
            remaining = maximum_content_bytes - total
            if remaining < 1:
                raise ScientificAdapterError(f"{label} handoff exceeds the extracted byte bound")
            content = _read_stable_file(path, maximum_bytes=remaining, label=f"{label} handoff file")
            total += len(content)
            files += 1
            entries.append(SnapshotEntry(name=name, is_directory=False, content=content))
        else:
            raise ScientificAdapterError(f"{label} handoff contains an unsupported entry")
        if len(entries) > maximum_members:
            raise ScientificAdapterError(f"{label} handoff exceeds the member bound")
    if not entries or files == 0:
        raise ScientificAdapterError(f"{label} handoff contains no regular files")
    return tuple(entries)


def encode_handoff(
    entries: tuple[SnapshotEntry, ...],
    *,
    label: str,
    maximum_archive_bytes: int,
) -> bytes:
    """Create a reproducible PAX tar and bounded zstd frame."""

    stream = io.BytesIO()
    try:
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for entry in entries:
                member = tarfile.TarInfo(f"{entry.name}/" if entry.is_directory else entry.name)
                member.uid = 0
                member.gid = 0
                member.uname = ""
                member.gname = ""
                member.mtime = 0
                member.mode = 0o755 if entry.is_directory else 0o400
                if entry.is_directory:
                    member.type = tarfile.DIRTYPE
                    archive.addfile(member)
                else:
                    member.size = len(entry.content)
                    archive.addfile(member, io.BytesIO(entry.content))
        raw = stream.getvalue()
        if len(raw) > maximum_archive_bytes:
            raise ScientificAdapterError(f"{label} handoff exceeds the framed tar bound")
        content = zstandard.ZstdCompressor(level=3, write_checksum=True, write_content_size=True).compress(raw)
    except (OSError, tarfile.TarError, zstandard.ZstdError) as error:
        raise ScientificAdapterError(f"{label} handoff could not be encoded") from error
    if not 1 <= len(content) <= maximum_archive_bytes:
        raise ScientificAdapterError(f"{label} handoff exceeds the compressed byte bound")
    return content


def validate_handoff(
    content: bytes,
    entries: tuple[SnapshotEntry, ...],
    *,
    label: str,
    maximum_archive_bytes: int,
    maximum_content_bytes: int,
) -> None:
    """Re-read the encoded handoff before exposing it to the artifact service."""

    expected = {entry.name: (entry.is_directory, entry.content) for entry in entries}
    try:
        raw = zstandard.ZstdDecompressor().decompress(content, max_output_size=maximum_archive_bytes)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            observed: dict[str, tuple[bool, bytes]] = {}
            total = 0
            for member in archive.getmembers():
                name = member.name.rstrip("/")
                path = PurePosixPath(name)
                if (
                    not name
                    or path.is_absolute()
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or path.parts[0] == ".fs2"
                    or name in observed
                    or not (member.isdir() or member.isfile())
                ):
                    raise ScientificAdapterError(f"{label} handoff archive contains an unsafe entry")
                if member.isdir():
                    observed[name] = (True, b"")
                    continue
                total += member.size
                if member.size < 1 or total > maximum_content_bytes:
                    raise ScientificAdapterError(f"{label} handoff archive exceeds the content bound")
                source = archive.extractfile(member)
                if source is None:
                    raise ScientificAdapterError(f"{label} handoff archive member has no payload")
                payload = source.read(maximum_content_bytes + 1)
                if len(payload) != member.size:
                    raise ScientificAdapterError(f"{label} handoff archive member size differs")
                observed[name] = (False, payload)
    except (tarfile.TarError, zstandard.ZstdError) as error:
        raise ScientificAdapterError(f"{label} handoff archive failed validation") from error
    if observed != expected:
        raise ScientificAdapterError(f"{label} handoff differs from the completed workspace")


def collect_workspace_handoff(
    invocation: StageInvocation,
    workspace: Path,
    *,
    label: str,
    name: str,
    semantic_type: str,
    maximum_members: int,
    maximum_content_bytes: int,
    maximum_archive_bytes: int,
) -> CollectedStageOutput:
    """Validate completion and publish one materializer-compatible handoff."""

    from . import CollectedArtifactFile, CollectedStageOutput

    if invocation.handoff_name != name:
        raise ScientificAdapterError(f"{label} collector received another handoff contract")
    completion_sha256 = completion_marker(invocation, workspace, label=label)
    entries = snapshot_workspace(
        workspace,
        label=label,
        maximum_members=maximum_members,
        maximum_content_bytes=maximum_content_bytes,
    )
    content = encode_handoff(entries, label=label, maximum_archive_bytes=maximum_archive_bytes)
    validate_handoff(
        content,
        entries,
        label=label,
        maximum_archive_bytes=maximum_archive_bytes,
        maximum_content_bytes=maximum_content_bytes,
    )
    output = workspace / ".fs2" / f"{label.lower()}-stage-handoff.tar.zst"
    atomic_publish(output, content, workspace=workspace, label=f"{label} handoff")
    return CollectedStageOutput(
        artifacts=(
            CollectedArtifactFile(
                name=name,
                semantic_type=semantic_type,
                path=output,
                media_type="application/octet-stream",
                compression="zstd",
            ),
        ),
        validation={
            "validator_id": invocation.validator_id,
            "status": "passed",
            "completion_marker_sha256": completion_sha256,
            "handoff_sha256": hashlib.sha256(content).hexdigest(),
            "handoff_size_bytes": len(content),
            "member_count": len(entries),
            "expanded_bytes": sum(len(entry.content) for entry in entries if not entry.is_directory),
        },
    )


__all__ = [
    "STAGE_COMPLETION_RELATIVE_PATH",
    "STAGE_COMPLETION_SCHEMA",
    "STAGE_RUNNER_RELATIVE_PATH",
    "SnapshotEntry",
    "atomic_publish",
    "collect_workspace_handoff",
    "completion_marker",
    "encode_handoff",
    "snapshot_workspace",
    "unwrapped_stage_argv",
    "validate_handoff",
    "wrap_stage_argv",
]

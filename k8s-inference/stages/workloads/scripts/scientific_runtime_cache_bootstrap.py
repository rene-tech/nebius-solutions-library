"""Prepare exact model-owned directories on the shared scientific runtime cache.

Terraform runs this program once, before the control plane may launch a
scientific workload.  The PVC root remains provider-owned: only the bounded
first-level directories declared by the execution map are created or migrated.
The dual-access phase keeps the legacy group on every existing entry, mirrors
owner permissions to that group, and moves ownership to the exact tenant/model
runtime UID. The immutable journal records the former UID/GID/mode, so rollback
remains possible. It never deletes, truncates, or follows a symlink.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import secrets
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, cast


CONTRACT_ENV = "FS2_SCIENTIFIC_RUNTIME_CACHE_OWNERSHIP_JSON"
CONTRACT_SCHEMA = "fs2-serve.nebius.ai/scientific-runtime-cache-ownership/v3"
JOURNAL_SCHEMA = "fs2-serve.nebius.ai/scientific-runtime-cache-migration-journal/v2"
COMPLETION_SCHEMA = "fs2-serve.nebius.ai/scientific-runtime-cache-migration-completion/v2"
CACHE_ROOT = Path("/cache")
DIRECTORY_MODE = 0o2770
DIRECTORY_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
AUTHORIZATION_ID = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,126}[a-z0-9])?$")
MIGRATION_PHASE = "journaled-dual-access-legacy-group"
WRITER_LOCK_NAME = ".fs2-cache-writer-admission.lock"
WRITER_LOCK_SCHEMA = "fs2-serve.nebius.ai/scientific-runtime-cache-writer-lock/v1"
QUIESCENCE_SCHEMA = "fs2-serve.nebius.ai/scientific-runtime-cache-quiescence/v2"


class CacheOwnershipError(ValueError):
    """The ownership contract or mounted cache does not satisfy the contract."""


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CacheOwnershipError(f"{label} must be an object")
    return value


def _identity(value: object, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= 2_147_483_647
    ):
        raise CacheOwnershipError(f"{label} must be a positive POSIX identity")
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CacheOwnershipError(f"{label} must be whole-second UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise CacheOwnershipError(f"{label} is invalid") from error
    if parsed.tzinfo is None or parsed.microsecond:
        raise CacheOwnershipError(f"{label} must be whole-second UTC")
    return parsed.astimezone(UTC)


def _require_active_quiescence(quiescence: Mapping[str, Any]) -> None:
    """Refuse every mutation once the independently signed fence expires."""

    observed_at = _timestamp(quiescence["observed_at"], "writer quiescence observed_at")
    expires_at = _timestamp(quiescence["expires_at"], "writer quiescence expires_at")
    now = datetime.now(UTC).replace(microsecond=0)
    if (
        expires_at <= observed_at
        or (expires_at - observed_at).total_seconds() > 900
        or observed_at > now
        or expires_at <= now
    ):
        raise CacheOwnershipError("runtime cache writer quiescence is not freshly active")


def _writer_lock_bytes(quiescence: Mapping[str, Any]) -> bytes:
    """Return the only bytes an independently observed writer lock may contain."""

    return _canonical(
        {
            "schema": WRITER_LOCK_SCHEMA,
            "lease_name": WRITER_LOCK_NAME,
            "lease_uid": quiescence["lease_uid"],
            "activation_id": quiescence["activation_id"],
        }
    ) + b"\n"


def _read_exact_descriptor(descriptor: int, *, maximum: int) -> bytes:
    """Read one bounded regular file through the descriptor that is flocked."""

    os.lseek(descriptor, 0, os.SEEK_SET)
    value = os.read(descriptor, maximum + 1)
    if len(value) > maximum or os.read(descriptor, 1):
        raise CacheOwnershipError("runtime cache migration lease content is unbounded")
    return value


def _open_relative(target_fd: int, relative_path: str, *, directory: bool) -> int:
    """Open every path component without following a symlink."""

    if relative_path == ".":
        return os.dup(target_fd)
    current_fd = os.dup(target_fd)
    try:
        parts = relative_path.split("/")
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1 or directory:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _tree_inventory(
    target_fd: int,
    *,
    boundary: str,
    current_uid: int,
    legacy_uid: int,
    legacy_gid: int,
    allow_new_root: bool = False,
) -> list[dict[str, Any]]:
    """Return a complete immutable preflight record without changing metadata."""

    allowed_uids = {current_uid, legacy_uid}
    result: list[dict[str, Any]] = []
    for walk_root, directory_names, file_names, directory_fd in os.fwalk(
        ".",
        topdown=True,
        follow_symlinks=False,
        dir_fd=target_fd,
    ):
        entries = [(name, True) for name in directory_names]
        entries.extend((name, False) for name in file_names)
        if walk_root == ".":
            root_status = os.fstat(directory_fd)
            entries.insert(0, (".", True))
        for name, is_directory in entries:
            if name == ".":
                entry = root_status
                relative_path = "."
                entry_fd = os.dup(directory_fd)
            else:
                relative_path = (
                    name if walk_root == "." else f"{walk_root.removeprefix('./')}/{name}"
                )
                flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
                if is_directory:
                    flags |= os.O_DIRECTORY
                try:
                    entry_fd = os.open(name, flags, dir_fd=directory_fd)
                except OSError as error:
                    raise CacheOwnershipError(
                        "runtime cache tree entry could not be opened without following links"
                    ) from error
                entry = os.fstat(entry_fd)
            try:
                if is_directory != stat.S_ISDIR(entry.st_mode):
                    raise CacheOwnershipError("runtime cache tree changed during preflight")
                if not (stat.S_ISDIR(entry.st_mode) or stat.S_ISREG(entry.st_mode)):
                    raise CacheOwnershipError(
                        "runtime cache tree contains a non-directory, non-regular entry"
                    )
                if stat.S_ISREG(entry.st_mode) and entry.st_nlink != 1:
                    raise CacheOwnershipError(
                        "runtime cache tree contains a hard-linked regular file"
                    )
                root_created_boundary = (
                    allow_new_root
                    and relative_path == "."
                    and stat.S_ISDIR(entry.st_mode)
                    and entry.st_uid == os.geteuid()
                )
                if entry.st_uid not in allowed_uids and not root_created_boundary:
                    raise CacheOwnershipError(
                        "runtime cache tree contains an entry owned by a foreign UID"
                    )
                current_mode = stat.S_IMODE(entry.st_mode)
                if current_mode & stat.S_IRWXO:
                    raise CacheOwnershipError(
                        "runtime cache tree contains an entry accessible to other users"
                    )
                desired_mode = current_mode | ((current_mode & stat.S_IRWXU) >> 3)
                if stat.S_ISDIR(entry.st_mode):
                    desired_mode |= stat.S_ISGID
                result.append(
                    {
                        "boundary": boundary,
                        "path": relative_path,
                        "kind": "directory" if stat.S_ISDIR(entry.st_mode) else "regular",
                        "device": entry.st_dev,
                        "inode": entry.st_ino,
                        "links": entry.st_nlink,
                        "uid": entry.st_uid,
                        "gid": entry.st_gid,
                        "mode": current_mode,
                        "desired_uid": current_uid,
                        "desired_gid": legacy_gid,
                        "desired_mode": desired_mode,
                    }
                )
            finally:
                os.close(entry_fd)
    return sorted(result, key=lambda item: (item["boundary"], item["path"]))


def _write_once(root_fd: int, name: str, document: Mapping[str, Any]) -> None:
    """Publish one fully fsynced content-addressed blob, then commit it atomically.

    Interrupted staging inodes are deliberately retained and ignored. Neither
    retry nor rollback removes or overwrites cache or receipt bytes.
    """

    encoded = _canonical(document) + b"\n"
    digest = hashlib.sha256(encoded).hexdigest()
    payload_name = f"{name}.payload.{digest}"
    marker_name = f"{name}.committed.{digest}"
    existing = _read_once(root_fd, name)
    if existing is not None:
        if existing != dict(document):
            raise CacheOwnershipError("runtime cache migration receipt differs")
        return
    staging_name = f"{name}.staging.{secrets.token_hex(16)}"
    try:
        receipt_fd = os.open(
            staging_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o400,
            dir_fd=root_fd,
        )
    except FileExistsError as error:
        raise CacheOwnershipError("runtime cache receipt staging identity collided") from error
    try:
        written = 0
        while written < len(encoded):
            written += os.write(receipt_fd, encoded[written:])
        os.fsync(receipt_fd)
    finally:
        os.close(receipt_fd)
    try:
        os.link(
            staging_name,
            payload_name,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
            follow_symlinks=False,
        )
    except FileExistsError:
        payload_fd = os.open(
            payload_name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            with os.fdopen(os.dup(payload_fd), "rb") as source:
                if source.read() != encoded:
                    raise CacheOwnershipError("runtime cache sealed receipt payload differs")
        finally:
            os.close(payload_fd)
    os.fsync(root_fd)
    try:
        os.mkdir(marker_name, mode=0o500, dir_fd=root_fd)
    except FileExistsError:
        pass
    marker_fd = os.open(
        marker_name,
        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=root_fd,
    )
    marker_status = os.fstat(marker_fd)
    os.close(marker_fd)
    if not stat.S_ISDIR(marker_status.st_mode) or stat.S_IMODE(marker_status.st_mode) != 0o500:
        raise CacheOwnershipError("runtime cache receipt commit marker metadata differs")
    os.fsync(root_fd)


def _read_once(root_fd: int, name: str) -> dict[str, Any] | None:
    marker_names = sorted(
        item for item in os.listdir(root_fd) if item.startswith(f"{name}.committed.")
    )
    if not marker_names:
        return None
    if len(marker_names) != 1 or re.fullmatch(
        re.escape(f"{name}.committed.") + r"[a-f0-9]{64}", marker_names[0]
    ) is None:
        raise CacheOwnershipError("runtime cache receipt commit marker is ambiguous")
    marker_fd = os.open(
        marker_names[0],
        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=root_fd,
    )
    marker_status = os.fstat(marker_fd)
    os.close(marker_fd)
    if not stat.S_ISDIR(marker_status.st_mode) or stat.S_IMODE(marker_status.st_mode) != 0o500:
        raise CacheOwnershipError("runtime cache receipt commit marker metadata differs")
    expected_digest = marker_names[0].rsplit(".", 1)[-1]
    payload_name = f"{name}.payload.{expected_digest}"
    try:
        receipt_fd = os.open(
            payload_name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
    except FileNotFoundError as error:
        raise CacheOwnershipError("runtime cache committed receipt payload is absent") from error
    try:
        status = os.fstat(receipt_fd)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 2
            or stat.S_IMODE(status.st_mode) != 0o400
        ):
            raise CacheOwnershipError("runtime cache migration receipt is not a sealed staged payload")
        with os.fdopen(os.dup(receipt_fd), "rb") as source:
            raw = source.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise CacheOwnershipError("runtime cache migration receipt exceeds its bound")
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            raise CacheOwnershipError("runtime cache migration receipt content digest differs")
        value = json.loads(raw)
    except (OSError, ValueError) as error:
        raise CacheOwnershipError("runtime cache migration receipt is invalid") from error
    finally:
        os.close(receipt_fd)
    return _object(value, "runtime cache migration receipt")


def _set_entry_metadata(target_fd: int, entry: Mapping[str, Any], *, desired: bool) -> None:
    entry_fd = _open_relative(
        target_fd,
        str(entry["path"]),
        directory=entry["kind"] == "directory",
    )
    try:
        current = os.fstat(entry_fd)
        if (
            current.st_dev != entry["device"]
            or current.st_ino != entry["inode"]
            or current.st_uid not in {entry["uid"], entry["desired_uid"]}
            or (stat.S_ISREG(current.st_mode) and current.st_nlink != 1)
        ):
            raise CacheOwnershipError("runtime cache entry identity changed after preflight")
        current_metadata = (current.st_uid, current.st_gid, stat.S_IMODE(current.st_mode))
        # fchown and fchmod are separate syscalls. Recovery must recognize the
        # exact UID/GID/mode Cartesian product, including set-ID clearing.
        journaled_modes = {int(entry["mode"]), int(entry["desired_mode"])}
        # POSIX permits fchown to clear set-ID bits before the following
        # fchmod restores the journaled mode. Those cleared modes are expected
        # crash states too; no other mode is accepted on recovery.
        intermediate_modes = journaled_modes | {
            mode & ~(stat.S_ISUID | stat.S_ISGID) for mode in journaled_modes
        }
        allowed_metadata = {
            (uid, gid, mode)
            for uid in (int(entry["uid"]), int(entry["desired_uid"]))
            for gid in (int(entry["gid"]), int(entry["desired_gid"]))
            for mode in intermediate_modes
        }
        if current_metadata not in allowed_metadata:
            raise CacheOwnershipError(
                "runtime cache interrupted state is outside the journaled metadata transaction"
            )
        uid = int(entry["desired_uid"] if desired else entry["uid"])
        gid = int(entry["desired_gid"] if desired else entry["gid"])
        mode = int(entry["desired_mode"] if desired else entry["mode"])
        if desired:
            # Establish group-equivalent access before changing the owning UID.
            # A crash or fchown failure therefore leaves the legacy writer able
            # to use the entry. fchown may clear set-ID bits, so restore the
            # exact journaled target mode after ownership changes.
            os.fchmod(entry_fd, mode)
            os.fchown(entry_fd, uid, gid)
            os.fchmod(entry_fd, mode)
        else:
            # Rollback restores the legacy owner before narrowing permissions.
            os.fchown(entry_fd, uid, gid)
            os.fchmod(entry_fd, mode)
        verified = os.fstat(entry_fd)
        if (
            verified.st_uid != uid
            or verified.st_gid != gid
            or stat.S_IMODE(verified.st_mode) != mode
        ):
            raise CacheOwnershipError("runtime cache metadata transaction verification failed")
    finally:
        os.close(entry_fd)


def _metadata_matches_journaled_state(
    current: Mapping[str, Any], journaled: Mapping[str, Any]
) -> bool:
    journaled_modes = {int(journaled["mode"]), int(journaled["desired_mode"])}
    intermediate_modes = journaled_modes | {
        mode & ~(stat.S_ISUID | stat.S_ISGID) for mode in journaled_modes
    }
    return (
        current["device"] == journaled["device"]
        and current["inode"] == journaled["inode"]
        and current["links"] == journaled["links"]
        and current["uid"] in {journaled["uid"], journaled["desired_uid"]}
        and current["gid"] in {journaled["gid"], journaled["desired_gid"]}
        and current["mode"] in intermediate_modes
    )


def _apply_transaction(
    target_fds: Mapping[str, int],
    entries: list[dict[str, Any]],
    quiescence: Mapping[str, Any],
) -> None:
    """Apply all metadata or restore every changed entry from the journal."""

    changed: list[dict[str, Any]] = []
    try:
        for entry in entries:
            _require_active_quiescence(quiescence)
            # Record the entry before the first syscall so an fchmod failure
            # after a successful fchown cannot escape rollback.
            changed.append(entry)
            _set_entry_metadata(target_fds[str(entry["boundary"])], entry, desired=True)
    except BaseException as error:
        rollback_errors: list[str] = []
        for entry in reversed(changed):
            try:
                _set_entry_metadata(target_fds[str(entry["boundary"])], entry, desired=False)
            except BaseException as rollback_error:
                rollback_errors.append(str(rollback_error))
        detail = "" if not rollback_errors else f"; rollback errors: {'; '.join(rollback_errors)}"
        raise CacheOwnershipError(f"runtime cache metadata transaction aborted{detail}") from error


def _validated_journal_entries(
    value: object, specs_by_name: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise CacheOwnershipError("runtime cache migration journal entries are absent")
    required = {
        "boundary",
        "path",
        "kind",
        "device",
        "inode",
        "links",
        "uid",
        "gid",
        "mode",
        "desired_uid",
        "desired_gid",
        "desired_mode",
    }
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw_entry in value:
        entry = _object(raw_entry, "runtime cache migration journal entry")
        boundary = entry.get("boundary")
        relative_path = entry.get("path")
        kind = entry.get("kind")
        if (
            set(entry) != required
            or not isinstance(boundary, str)
            or boundary not in specs_by_name
            or not isinstance(relative_path, str)
            or relative_path != "."
            and (
                relative_path.startswith("/")
                or any(part in {"", ".", ".."} for part in relative_path.split("/"))
            )
            or kind not in {"directory", "regular"}
            or any(
                not isinstance(entry.get(field), int)
                or isinstance(entry.get(field), bool)
                or int(entry[field]) < 0
                for field in (
                    "device",
                    "inode",
                    "links",
                    "uid",
                    "gid",
                    "mode",
                    "desired_uid",
                    "desired_gid",
                    "desired_mode",
                )
            )
        ):
            raise CacheOwnershipError("runtime cache migration journal entry fields differ")
        spec = specs_by_name[boundary]
        original_mode = int(entry["mode"])
        expected_mode = original_mode | ((original_mode & stat.S_IRWXU) >> 3)
        if kind == "directory":
            expected_mode |= stat.S_ISGID
        if (
            int(entry["inode"]) == 0
            or int(entry["links"]) == 0
            or kind == "regular"
            and int(entry["links"]) != 1
            or (
                int(entry["uid"]) not in {int(spec["uid"]), int(spec["legacy_uid"])}
                and not (
                    spec["origin"] == "new-empty"
                    and relative_path == "."
                    and kind == "directory"
                    and int(entry["uid"]) == os.geteuid()
                )
            )
            or int(entry["desired_uid"]) != int(spec["uid"])
            or int(entry["desired_gid"]) != int(spec["legacy_gid"])
            or int(entry["desired_mode"]) != expected_mode
            or original_mode & stat.S_IRWXO
            or original_mode > 0o7777
        ):
            raise CacheOwnershipError("runtime cache migration journal metadata differs")
        key = (boundary, relative_path)
        if key in seen:
            raise CacheOwnershipError("runtime cache migration journal entry is duplicated")
        seen.add(key)
        result.append(entry)
    if {boundary for boundary, _ in seen} != set(specs_by_name):
        raise CacheOwnershipError("runtime cache migration journal omits a boundary root")
    return sorted(result, key=lambda item: (item["boundary"], item["path"]))


def prepare(contract: object, *, expected_root: Path = CACHE_ROOT) -> tuple[str, ...]:
    """Create or dual-access migrate the contract's exact model boundaries."""

    if expected_root == CACHE_ROOT and os.geteuid() != 0:
        raise CacheOwnershipError("runtime cache bootstrap requires its dedicated root identity")
    document = _object(contract, "runtime cache ownership contract")
    if set(document) != {"schema", "root", "writer_quiescence", "directories"}:
        raise CacheOwnershipError("runtime cache ownership contract fields differ")
    if document["schema"] != CONTRACT_SCHEMA:
        raise CacheOwnershipError("runtime cache ownership contract schema differs")
    if document["root"] != expected_root.as_posix():
        raise CacheOwnershipError(
            "runtime cache ownership root differs from the mounted root"
        )
    quiescence = _object(document["writer_quiescence"], "runtime cache writer quiescence")
    if (
        set(quiescence) != {
            "lease_name",
            "lease_uid",
            "lock_device",
            "lock_inode",
            "lock_content_sha256",
            "zero_writers",
            "writer_admission_fenced",
            "active_writer_count",
            "activation_id",
            "admission_policy_name",
            "admission_policy_uid",
            "admission_policy_resource_version",
            "admission_policy_sha256",
            "admission_binding_name",
            "observed_at",
            "expires_at",
            "evidence_sha256",
            "authorization_id",
            "quiescence_sha256",
        }
        or not isinstance(quiescence["lease_name"], str)
        or quiescence["lease_name"] != WRITER_LOCK_NAME
        or not isinstance(quiescence["lease_uid"], str)
        or re.fullmatch(r"[a-f0-9]{64}", quiescence["lease_uid"]) is None
        or not isinstance(quiescence["lock_device"], int)
        or isinstance(quiescence["lock_device"], bool)
        or quiescence["lock_device"] < 0
        or not isinstance(quiescence["lock_inode"], int)
        or isinstance(quiescence["lock_inode"], bool)
        or quiescence["lock_inode"] < 1
        or not isinstance(quiescence["lock_content_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", quiescence["lock_content_sha256"]) is None
        or quiescence["zero_writers"] is not True
        or quiescence["writer_admission_fenced"] is not True
        or quiescence["active_writer_count"] != 0
        or isinstance(quiescence["active_writer_count"], bool)
        or not isinstance(quiescence["activation_id"], str)
        or re.fullmatch(r"[a-f0-9]{64}", quiescence["activation_id"]) is None
        or quiescence["admission_policy_name"] != "fs2-scientific-runtime-cache-writer-fence"
        or not isinstance(quiescence["admission_policy_uid"], str)
        or re.fullmatch(r"[a-f0-9-]{36}", quiescence["admission_policy_uid"]) is None
        or not isinstance(quiescence["admission_policy_resource_version"], str)
        or re.fullmatch(r"[1-9][0-9]*", quiescence["admission_policy_resource_version"]) is None
        or not isinstance(quiescence["admission_policy_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", quiescence["admission_policy_sha256"]) is None
        or quiescence["admission_binding_name"]
        != "fs2-scientific-runtime-cache-writer-fence"
        or not isinstance(quiescence["evidence_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", quiescence["evidence_sha256"]) is None
        or not isinstance(quiescence["authorization_id"], str)
        or AUTHORIZATION_ID.fullmatch(quiescence["authorization_id"]) is None
        or not isinstance(quiescence["quiescence_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", quiescence["quiescence_sha256"]) is None
        or quiescence["quiescence_sha256"]
        != hashlib.sha256(
            _canonical(
                {
                    "schema": QUIESCENCE_SCHEMA,
                    "lease_name": quiescence["lease_name"],
                    "lease_uid": quiescence["lease_uid"],
                    "lock_device": quiescence["lock_device"],
                    "lock_inode": quiescence["lock_inode"],
                    "lock_content_sha256": quiescence["lock_content_sha256"],
                    "zero_writers": quiescence["zero_writers"],
                    "writer_admission_fenced": quiescence["writer_admission_fenced"],
                    "active_writer_count": quiescence["active_writer_count"],
                    "activation_id": quiescence["activation_id"],
                    "admission_policy_name": quiescence["admission_policy_name"],
                    "admission_policy_uid": quiescence["admission_policy_uid"],
                    "admission_policy_resource_version": quiescence[
                        "admission_policy_resource_version"
                    ],
                    "admission_policy_sha256": quiescence["admission_policy_sha256"],
                    "admission_binding_name": quiescence["admission_binding_name"],
                    "observed_at": quiescence["observed_at"],
                    "expires_at": quiescence["expires_at"],
                    "evidence_sha256": quiescence["evidence_sha256"],
                }
            )
        ).hexdigest()
    ):
        raise CacheOwnershipError("runtime cache migration lacks exact zero-writer lease evidence")
    _require_active_quiescence(quiescence)

    root = expected_root.resolve(strict=True)
    root_status = root.stat()
    if not stat.S_ISDIR(root_status.st_mode):
        raise CacheOwnershipError("runtime cache root is not a directory")

    raw_directories = document["directories"]
    if not isinstance(raw_directories, list) or not 1 <= len(raw_directories) <= 512:
        raise CacheOwnershipError(
            "runtime cache ownership directories must be a bounded array"
        )

    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_directory in raw_directories:
        directory = _object(raw_directory, "runtime cache directory")
        if set(directory) != {
            "name",
            "uid",
            "gid",
            "legacy_uid",
            "legacy_gid",
            "tenant_id",
            "model_id",
            "origin",
            "boundary_sha256",
            "migration_phase",
            "mode",
        }:
            raise CacheOwnershipError("runtime cache directory fields differ")
        name = directory["name"]
        if not isinstance(name, str) or DIRECTORY_NAME.fullmatch(name) is None or name in seen:
            raise CacheOwnershipError("runtime cache directory name is invalid or duplicated")
        uid = _identity(directory["uid"], f"runtime cache directory {name} uid")
        gid = _identity(directory["gid"], f"runtime cache directory {name} gid")
        legacy_uid = _identity(directory["legacy_uid"], f"runtime cache directory {name} legacy uid")
        legacy_gid = _identity(directory["legacy_gid"], f"runtime cache directory {name} legacy gid")
        if uid == legacy_uid or gid == legacy_gid:
            raise CacheOwnershipError("runtime cache current and legacy identities must differ")
        if directory["origin"] not in {"legacy-existing", "new-empty"}:
            raise CacheOwnershipError("runtime cache directory origin differs")
        if directory["migration_phase"] != MIGRATION_PHASE or directory["mode"] != "2770":
            raise CacheOwnershipError("runtime cache migration phase or directory mode differs")
        if not isinstance(directory["boundary_sha256"], str) or re.fullmatch(
            r"[a-f0-9]{64}", directory["boundary_sha256"]
        ) is None:
            raise CacheOwnershipError("runtime cache boundary digest is invalid")
        specs.append({**directory, "uid": uid, "gid": gid, "legacy_uid": legacy_uid, "legacy_gid": legacy_gid})
        seen.add(name)
    specs_by_name = {str(spec["name"]): spec for spec in specs}

    contract_sha256 = hashlib.sha256(_canonical(document)).hexdigest()
    journal_name = f".fs2-cache-migration-{contract_sha256}.journal.json"
    completion_name = f".fs2-cache-migration-{contract_sha256}.complete.json"
    prepared = [str(spec["name"]) for spec in specs]
    root_fd = os.open(root, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    lease_fd = os.open(
        WRITER_LOCK_NAME,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=root_fd,
    )
    lease_status = os.fstat(lease_fd)
    expected_lock = _writer_lock_bytes(quiescence)
    if (
        not stat.S_ISREG(lease_status.st_mode)
        or lease_status.st_nlink != 1
        or lease_status.st_uid != os.geteuid()
        or lease_status.st_gid != os.getegid()
        or stat.S_IMODE(lease_status.st_mode) != 0o444
        or lease_status.st_dev != quiescence["lock_device"]
        or lease_status.st_ino != quiescence["lock_inode"]
        or hashlib.sha256(expected_lock).hexdigest()
        != quiescence["lock_content_sha256"]
        or _read_exact_descriptor(lease_fd, maximum=4096) != expected_lock
    ):
        os.close(lease_fd)
        os.close(root_fd)
        raise CacheOwnershipError("runtime cache migration lease is not one regular inode")
    try:
        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(lease_fd)
        os.close(root_fd)
        raise CacheOwnershipError("runtime cache migration lease is already held") from error
    target_fds: dict[str, int] = {}
    try:
        known_completion = _read_once(root_fd, completion_name)
        # Phase 1 is read-only across every pre-existing boundary. No metadata
        # change occurs until the whole claim has passed this preflight.
        inventories: dict[str, list[dict[str, Any]]] = {}
        absent: list[dict[str, Any]] = []
        for spec in specs:
            name = str(spec["name"])
            try:
                target_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
            except FileNotFoundError:
                if spec["origin"] != "new-empty":
                    raise CacheOwnershipError(f"legacy runtime cache target {name} is absent")
                absent.append(spec)
                continue
            except OSError as error:
                raise CacheOwnershipError(f"runtime cache target {name} is not a real directory") from error
            target_fds[name] = target_fd
            inventories[name] = _tree_inventory(
                target_fd,
                boundary=name,
                current_uid=int(spec["uid"]),
                legacy_uid=int(spec["legacy_uid"]),
                legacy_gid=int(spec["legacy_gid"]),
                allow_new_root=spec["origin"] == "new-empty" and known_completion is None,
            )
            if (
                spec["origin"] == "new-empty"
                and known_completion is None
                and len(inventories[name]) != 1
            ):
                raise CacheOwnershipError(f"new runtime cache boundary {name} is not empty")

        existing_journal = _read_once(root_fd, journal_name)
        journal_was_existing = existing_journal is not None
        if existing_journal is None:
            # New tenant boundaries are additive. If interrupted before the
            # journal write they remain empty/root-only and are re-preflighted;
            # no customer cache byte is removed or replaced.
            for spec in absent:
                name = str(spec["name"])
                os.mkdir(name, mode=0o700, dir_fd=root_fd)
                target_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
                target_fds[name] = target_fd
                inventories[name] = _tree_inventory(
                    target_fd,
                    boundary=name,
                    current_uid=int(spec["uid"]),
                    legacy_uid=os.fstat(target_fd).st_uid,
                    legacy_gid=int(spec["legacy_gid"]),
                    allow_new_root=True,
                )
            journal = {
                "schema": JOURNAL_SCHEMA,
                "contract_sha256": contract_sha256,
                "entries": sorted(
                    [entry for inventory in inventories.values() for entry in inventory],
                    key=lambda item: (item["boundary"], item["path"]),
                ),
            }
            journal["entries"] = _validated_journal_entries(
                journal["entries"], specs_by_name
            )
            _write_once(root_fd, journal_name, journal)
        else:
            journal = existing_journal
            if (
                set(journal) != {"schema", "contract_sha256", "entries"}
                or journal["schema"] != JOURNAL_SCHEMA
                or journal["contract_sha256"] != contract_sha256
                or not isinstance(journal["entries"], list)
            ):
                raise CacheOwnershipError("runtime cache migration journal differs from its contract")
            journal["entries"] = _validated_journal_entries(
                journal["entries"], specs_by_name
            )
            journal_identities = {
                (entry["boundary"], entry["path"]): entry for entry in journal["entries"]
            }
            current_identities = {
                (entry["boundary"], entry["path"]): entry
                for inventory in inventories.values()
                for entry in inventory
            }
            if known_completion is None and (
                set(current_identities) != set(journal_identities)
                or any(
                    not _metadata_matches_journaled_state(
                        current_identities[key], journal_identities[key]
                    )
                    for key in journal_identities
                )
            ):
                raise CacheOwnershipError("runtime cache tree identity differs from its migration journal")

        entries = cast(list[dict[str, Any]], journal["entries"])
        completion = {
            "schema": COMPLETION_SCHEMA,
            "contract_sha256": contract_sha256,
            "journal_sha256": hashlib.sha256(_canonical(journal)).hexdigest(),
        }
        existing_completion = known_completion
        if existing_completion is None:
            # Close the preflight-to-mutation interval while holding the same
            # exclusive lease: no extra unjournaled entry may appear.
            immediate_identities = {
                (entry["boundary"], entry["path"]): entry
                for spec in specs
                for entry in _tree_inventory(
                    target_fds[str(spec["name"])],
                    boundary=str(spec["name"]),
                    current_uid=int(spec["uid"]),
                    legacy_uid=int(spec["legacy_uid"]),
                    legacy_gid=int(spec["legacy_gid"]),
                    allow_new_root=spec["origin"] == "new-empty",
                )
            }
            journal_identities = {
                (entry["boundary"], entry["path"]): entry
                for entry in entries
            }
            if set(immediate_identities) != set(journal_identities) or any(
                not _metadata_matches_journaled_state(
                    immediate_identities[key], journal_identities[key]
                )
                for key in journal_identities
            ):
                raise CacheOwnershipError(
                    "runtime cache tree changed after journal publication under the migration lease"
                )
            # An interrupted predecessor may have changed only a prefix. The
            # immutable journal makes the next attempt restore that prefix
            # before applying the complete transaction again.
            if journal_was_existing:
                try:
                    for entry in reversed(entries):
                        _require_active_quiescence(quiescence)
                        _set_entry_metadata(
                            target_fds[str(entry["boundary"])],
                            entry,
                            desired=False,
                        )
                except BaseException as error:
                    raise CacheOwnershipError(
                        "runtime cache interrupted transaction could not be restored"
                    ) from error
            _apply_transaction(target_fds, entries, quiescence)
            _require_active_quiescence(quiescence)
            _write_once(root_fd, completion_name, completion)
        elif existing_completion != completion:
            raise CacheOwnershipError("runtime cache migration completion receipt differs")

        for entry in entries:
            entry_fd = _open_relative(
                target_fds[str(entry["boundary"])],
                str(entry["path"]),
                directory=entry["kind"] == "directory",
            )
            try:
                verified = os.fstat(entry_fd)
                if (
                    verified.st_uid != entry["desired_uid"]
                    or verified.st_gid != entry["desired_gid"]
                    or stat.S_IMODE(verified.st_mode) != entry["desired_mode"]
                    or (stat.S_ISREG(verified.st_mode) and verified.st_nlink != 1)
                ):
                    raise CacheOwnershipError("runtime cache completed metadata differs")
            finally:
                os.close(entry_fd)
    finally:
        for target_fd in target_fds.values():
            os.close(target_fd)
        fcntl.flock(lease_fd, fcntl.LOCK_UN)
        os.close(lease_fd)
        os.close(root_fd)

    return tuple(prepared)


def main() -> int:
    raw_contract = os.environ.get(CONTRACT_ENV)
    if raw_contract is None:
        raise CacheOwnershipError(f"{CONTRACT_ENV} is required")
    try:
        contract: object = json.loads(raw_contract)
    except json.JSONDecodeError as error:
        raise CacheOwnershipError(
            "runtime cache ownership contract is not valid JSON"
        ) from error
    prepared = prepare(contract)
    print(
        json.dumps(
            {"schema": CONTRACT_SCHEMA, "prepared": list(prepared)}, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

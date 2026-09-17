"""Prepare exact model-owned directories on the shared scientific runtime cache.

Terraform runs this program once, before the control plane may launch a
scientific workload.  The PVC root remains provider-owned: only the bounded
first-level directories declared by the execution map are created or migrated.
The dual-access phase keeps the legacy group on every existing entry, mirrors
owner permissions to that group, and gives the new runtime only that model's
legacy group. It never deletes, truncates, follows a symlink, or changes an
existing entry's owning UID, so the prior runtime identity remains usable for
rollback while new entries inherit the same legacy group through setgid.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any


CONTRACT_ENV = "FS2_SCIENTIFIC_RUNTIME_CACHE_OWNERSHIP_JSON"
CONTRACT_SCHEMA = "fs2-serve.nebius.ai/scientific-runtime-cache-ownership/v2"
CACHE_ROOT = Path("/cache")
DIRECTORY_MODE = 0o2770
DIRECTORY_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
MIGRATION_PHASE = "dual-access-legacy-group"


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


def _migrate_tree(
    target_fd: int,
    *,
    current_uid: int,
    legacy_uid: int,
    legacy_gid: int,
) -> None:
    """Add model-local legacy-group access without following or deleting entries."""

    allowed_uids = {current_uid, legacy_uid}
    for _, directory_names, file_names, directory_fd in os.fwalk(
        ".",
        topdown=True,
        follow_symlinks=False,
        dir_fd=target_fd,
    ):
        for name in [*directory_names, *file_names]:
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            if name in directory_names:
                flags |= os.O_DIRECTORY
            try:
                entry_fd = os.open(name, flags, dir_fd=directory_fd)
            except OSError as error:
                raise CacheOwnershipError(
                    "runtime cache tree entry could not be opened without following links"
                ) from error
            try:
                entry = os.fstat(entry_fd)
                if not (stat.S_ISDIR(entry.st_mode) or stat.S_ISREG(entry.st_mode)):
                    raise CacheOwnershipError(
                        "runtime cache tree contains a non-directory, non-regular entry"
                    )
                if entry.st_uid not in allowed_uids:
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
                os.fchown(entry_fd, -1, legacy_gid)
                os.fchmod(entry_fd, desired_mode)
            finally:
                os.close(entry_fd)


def prepare(contract: object, *, expected_root: Path = CACHE_ROOT) -> tuple[str, ...]:
    """Create or dual-access migrate the contract's exact model boundaries."""

    document = _object(contract, "runtime cache ownership contract")
    if set(document) != {"schema", "root", "directories"}:
        raise CacheOwnershipError("runtime cache ownership contract fields differ")
    if document["schema"] != CONTRACT_SCHEMA:
        raise CacheOwnershipError("runtime cache ownership contract schema differs")
    if document["root"] != expected_root.as_posix():
        raise CacheOwnershipError(
            "runtime cache ownership root differs from the mounted root"
        )

    root = expected_root.resolve(strict=True)
    root_status = root.stat()
    if not stat.S_ISDIR(root_status.st_mode):
        raise CacheOwnershipError("runtime cache root is not a directory")

    raw_directories = document["directories"]
    if not isinstance(raw_directories, list) or not 1 <= len(raw_directories) <= 512:
        raise CacheOwnershipError(
            "runtime cache ownership directories must be a bounded array"
        )

    prepared: list[str] = []
    seen: set[str] = set()
    root_fd = os.open(root, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for raw_directory in raw_directories:
            directory = _object(raw_directory, "runtime cache directory")
            if set(directory) != {
                "name",
                "uid",
                "gid",
                "legacy_uid",
                "legacy_gid",
                "migration_phase",
                "mode",
            }:
                raise CacheOwnershipError("runtime cache directory fields differ")
            name = directory["name"]
            if (
                not isinstance(name, str)
                or DIRECTORY_NAME.fullmatch(name) is None
                or name in seen
            ):
                raise CacheOwnershipError(
                    "runtime cache directory name is invalid or duplicated"
                )
            uid = _identity(directory["uid"], f"runtime cache directory {name} uid")
            gid = _identity(directory["gid"], f"runtime cache directory {name} gid")
            legacy_uid = _identity(
                directory["legacy_uid"], f"runtime cache directory {name} legacy uid"
            )
            legacy_gid = _identity(
                directory["legacy_gid"], f"runtime cache directory {name} legacy gid"
            )
            if uid == legacy_uid or gid == legacy_gid:
                raise CacheOwnershipError(
                    "runtime cache current and legacy identities must differ"
                )
            if directory["migration_phase"] != MIGRATION_PHASE:
                raise CacheOwnershipError("runtime cache migration phase differs")
            if directory["mode"] != "2770":
                raise CacheOwnershipError("runtime cache directory mode must be 2770")

            created = False
            try:
                os.mkdir(name, mode=0o770, dir_fd=root_fd)
                created = True
            except FileExistsError:
                pass
            try:
                target_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
            except OSError as error:
                raise CacheOwnershipError(
                    f"runtime cache target {name} is not a real directory"
                ) from error
            try:
                target_status = os.fstat(target_fd)
                if created:
                    os.fchown(target_fd, legacy_uid, legacy_gid)
                elif target_status.st_uid not in {uid, legacy_uid}:
                    raise CacheOwnershipError(
                        f"runtime cache target {name} is owned by a foreign UID"
                    )
                elif stat.S_IMODE(target_status.st_mode) & stat.S_IRWXO:
                    raise CacheOwnershipError(
                        f"runtime cache target {name} is accessible to other users"
                    )

                # Retain the existing owning UID and the legacy GID. The new
                # runtime receives only this legacy GID under Strict groups.
                os.fchown(target_fd, -1, legacy_gid)
                os.fchmod(target_fd, DIRECTORY_MODE)
                _migrate_tree(
                    target_fd,
                    current_uid=uid,
                    legacy_uid=legacy_uid,
                    legacy_gid=legacy_gid,
                )
                verified = os.fstat(target_fd)
                if (
                    verified.st_uid not in {uid, legacy_uid}
                    or verified.st_gid != legacy_gid
                    or stat.S_IMODE(verified.st_mode) != DIRECTORY_MODE
                ):
                    raise CacheOwnershipError(
                        f"runtime cache target {name} ownership verification failed"
                    )
            finally:
                os.close(target_fd)
            seen.add(name)
            prepared.append(name)
    finally:
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

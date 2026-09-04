"""Prepare exact model-owned directories on the shared scientific runtime cache.

Terraform runs this program once, before the control plane may launch a
scientific workload.  The PVC root remains provider-owned: only the bounded
first-level directories declared by the execution map are created or repaired.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any


CONTRACT_ENV = "FS2_SCIENTIFIC_RUNTIME_CACHE_OWNERSHIP_JSON"
CONTRACT_SCHEMA = "fs2-serve.nebius.ai/scientific-runtime-cache-ownership/v1"
CACHE_ROOT = Path("/cache")
DIRECTORY_MODE = 0o770
DIRECTORY_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")


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


def prepare(contract: object, *, expected_root: Path = CACHE_ROOT) -> tuple[str, ...]:
    """Create and verify the contract's exact non-recursive directory set."""

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
    for raw_directory in raw_directories:
        directory = _object(raw_directory, "runtime cache directory")
        if set(directory) != {"name", "uid", "gid", "mode"}:
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
        if directory["mode"] != "0770":
            raise CacheOwnershipError("runtime cache directory mode must be 0770")

        target = root / name
        try:
            target.mkdir(mode=0o770)
        except FileExistsError:
            pass
        target_status = target.lstat()
        if stat.S_ISLNK(target_status.st_mode) or not stat.S_ISDIR(
            target_status.st_mode
        ):
            raise CacheOwnershipError(
                f"runtime cache target {name} is not a real directory"
            )

        # Deliberately non-recursive. Existing cache entries remain untouched;
        # only the model-owned boundary gets the exact execution-map identity.
        os.chown(target, uid, gid, follow_symlinks=False)
        os.chmod(target, DIRECTORY_MODE, follow_symlinks=False)
        verified = target.lstat()
        if (
            verified.st_uid != uid
            or verified.st_gid != gid
            or stat.S_IMODE(verified.st_mode) != DIRECTORY_MODE
        ):
            raise CacheOwnershipError(
                f"runtime cache target {name} ownership verification failed"
            )
        seen.add(name)
        prepared.append(name)

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

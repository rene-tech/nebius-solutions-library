#!/usr/bin/env python3
"""Fail closed on destructive key migration plans and residual plaintext state."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from typing import Any, Sequence


LEGACY_ADDRESSES = frozenset(
    {
        "random_password.admin_token",
        "random_password.bootstrap_access_token_secret",
        "random_password.key_material[\"payload\"]",
        "random_password.key_material[\"ledger\"]",
        "random_password.key_material[\"pepper\"]",
        "random_password.key_material[\"attestor\"]",
        "kubernetes_secret_v1.admin",
        "kubernetes_secret_v1.payload_keyring",
        "kubernetes_secret_v1.ledger_keyring",
        "kubernetes_secret_v1.token_pepper",
        "kubernetes_secret_v1.route_attestors",
    }
)
LEGACY_ADDRESS_PREFIXES = (
    'random_password.database["',
    'kubernetes_secret_v1.database_account["',
)
SENSITIVE_ARTIFACT_SUFFIXES = (".tfstate", ".tfplan", ".backup")


class GuardError(RuntimeError):
    pass


def inspect_plan(document: dict[str, Any]) -> dict[str, int]:
    protected_changes = 0
    for change in document.get("resource_changes", []):
        address = change.get("address")
        actions = change.get("change", {}).get("actions", [])
        protected = address in LEGACY_ADDRESSES or (
            isinstance(address, str) and address.startswith(LEGACY_ADDRESS_PREFIXES)
        )
        if protected and any(action in {"delete", "replace"} for action in actions):
            raise GuardError(f"plan would replace or delete protected legacy address {address}")
        if protected and actions != ["no-op"]:
            protected_changes += 1
    return {"protected_addresses": len(LEGACY_ADDRESSES), "protected_changes": protected_changes}


def inspect_run_root(root: Path, *, retired: bool) -> dict[str, int | str]:
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise GuardError("run root must be a real directory")
    artifacts = 0
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(directory)
        if current.is_symlink() or stat.S_IMODE(current.stat().st_mode) & 0o077:
            raise GuardError(f"run-root directory is not owner-only: {current}")
        for name in directory_names:
            if (current / name).is_symlink():
                raise GuardError(f"run root contains a directory symlink: {current / name}")
        for name in file_names:
            path = current / name
            if path.is_symlink():
                raise GuardError(f"run root contains a file symlink: {path}")
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise GuardError(f"run-root file is not owner-only: {path}")
            if name.endswith(SENSITIVE_ARTIFACT_SUFFIXES) or ".tfstate." in name or ".tfplan." in name:
                artifacts += 1
                if retired:
                    raise GuardError(f"plaintext Terraform artifact remains after retirement: {path}")
    return {"phase": "retired" if retired else "migration", "plaintext_artifacts": artifacts}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("plan_json", type=Path)
    root = subparsers.add_parser("run-root")
    root.add_argument("path", type=Path)
    root.add_argument("--retired", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "plan":
        result = inspect_plan(json.loads(args.plan_json.read_text(encoding="utf-8")))
    else:
        result = inspect_run_root(args.path, retired=args.retired)
    print(json.dumps({"status": "pass", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GuardError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=os.sys.stderr)
        raise SystemExit(2) from error

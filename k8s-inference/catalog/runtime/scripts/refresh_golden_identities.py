#!/usr/bin/env python3
"""Refresh the deterministic catalog identity projection used by CI and review."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from fs2_serve_catalog.consumer import identity_map
from fs2_serve_catalog.loader import load_catalog


def _render(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _replace(path: Path, payload: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        stream.write(payload)
        temporary = Path(stream.name)
    os.replace(temporary, path)


def refresh(catalog_root: Path, *, check: bool) -> bool:
    output = catalog_root / "contracts" / "golden-identities.json"
    payload = _render(identity_map(load_catalog(catalog_root)))
    changed = output.read_bytes() != payload
    if changed and not check:
        _replace(output, payload)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--catalog-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    changed = refresh(args.catalog_root.resolve(), check=args.check)
    if args.check and changed:
        print("golden identities require refresh")
        return 1
    print(
        "golden identities are current"
        if not changed
        else "golden identities refreshed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

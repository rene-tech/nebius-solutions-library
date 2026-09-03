#!/usr/bin/env python3
"""Stage a motif-scaffolding target structure into the artifact plane.

The target is small enough to be committed alongside the contract fixture, so this
copies it from a local path rather than downloading, and verifies its sha256 against
the identity the fixture declares. Written next to the checkpoint generation so a
scaffold-motif request resolves both artifacts under one ``--artifact-root``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# The 1UBQ fixture already carried on main as the RFdiffusion validator input.
TARGET_SHA256 = "d4a6812d8951cf6594e6a0763f089e35f5a80b62acb3c117b2c5565228a7b161"
TARGET_BYTES = 78570
RELATIVE_PATH = "targets/1UBQ.pdb"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--root", default=os.environ.get("FS2_ARTIFACT_ROOT", "/artifacts"))
    parser.add_argument("--relative-path", default=RELATIVE_PATH)
    parser.add_argument("--sha256", default=TARGET_SHA256)
    parser.add_argument("--report", default="/dev/stdout")
    args = parser.parse_args(argv)

    source = args.source
    if not source.is_file():
        raise SystemExit(f"target structure is absent: {source}")
    actual = sha256_file(source)
    if actual != args.sha256:
        raise SystemExit(
            f"{source}: sha256 mismatch\n  expected {args.sha256}\n  actual   {actual}"
        )

    destination = Path(args.root) / args.relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.is_file() and sha256_file(destination) == actual:
        state = "already-present"
    else:
        # Same-directory temp plus rename, so a reader never sees a partial file.
        handle, staging = tempfile.mkstemp(dir=str(destination.parent), prefix=".staging-")
        os.close(handle)
        shutil.copyfile(source, staging)
        os.chmod(staging, 0o444)
        os.replace(staging, destination)
        state = "staged"

    report = {
        "schema": "fs2-serve.nebius.ai/rfdiffusion-target-staging/v1",
        "state": state,
        "artifact_id": "artifact.rfdiffusion.target.1ubq",
        "relative_path": args.relative_path,
        "absolute_path": str(destination),
        "sha256": actual,
        "size_bytes": destination.stat().st_size,
    }
    Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Place the Base checkpoint where a qualification run can read it.

This is **run-input plumbing, not localization.** Artifact localization for raw
single-file checkpoints is owned by
``fs2-single-file-model-artifact-localization-r20260903``, and this runtime makes no
localization claim: it publishes no generation, writes no ``.fs2-runtime-tree.json``
marker, emits no localization receipt, and nothing it writes is admissible as a
localized tree. An earlier revision of this script did mimic the plane's marker
schema; that was a task-local substitute for someone else's contract and has been
removed rather than left to be mistaken for the real thing.

What it does: fetch the exact public object, refuse it unless both its sha256 and its
byte count match the accepted catalog, and put it at a path named by the object's own
digest so a rerun cannot silently pick up different bytes. That is the whole job.

The path is ``<root>/inputs/sha256/<object sha256>/Base_ckpt.pt``. The digest in the
path is the *object's* digest from the catalog, not a tree-inventory generation, and
carries no meaning beyond "these are the bytes that hash to this".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

# Exactly the accepted catalog entry for the official public Base checkpoint.
CHECKPOINT = {
    "filename": "Base_ckpt.pt",
    "url": "https://files.ipd.uw.edu/pub/RFdiffusion/6f5902ac237024bdd0c176cb93063dc4/Base_ckpt.pt",
    "sha256": "0fcf7d7c32b4848030aca3a051e6768de194616f96ba6c38186351a33bfc6eca",
    "bytes": 483616107,
    "license_id": "BSD-3-Clause",
    "source_revision": "9273ef67335acaf91df0150473a274759229cdf6",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path, expected_sha256: str, expected_bytes: int) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    request = urllib.request.Request(url, headers={"User-Agent": "fs2-rfdiffusion-stage/2"})
    with urllib.request.urlopen(request, timeout=1800) as response:  # noqa: S310 - pinned https source
        with destination.open("wb") as handle:
            while True:
                chunk = response.read(4 * 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                written += len(chunk)
                handle.write(chunk)
    actual = digest.hexdigest()
    # Size is checked too, but identity is the digest: ActiveSite_ckpt.pt is exactly
    # the same number of bytes as Base_ckpt.pt.
    if written != expected_bytes:
        raise SystemExit(f"{url}: expected {expected_bytes} bytes, received {written}")
    if actual != expected_sha256:
        raise SystemExit(f"{url}: sha256 mismatch\n  expected {expected_sha256}\n  actual   {actual}")
    return actual


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("FS2_ARTIFACT_ROOT", "/artifacts"))
    parser.add_argument("--report", default="/dev/stdout")
    args = parser.parse_args(argv)

    directory = Path(args.root) / "inputs" / "sha256" / CHECKPOINT["sha256"]
    target = directory / CHECKPOINT["filename"]

    if target.is_file() and sha256_file(target) == CHECKPOINT["sha256"]:
        state = "already-present"
    else:
        directory.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(directory)))
        try:
            downloaded = staging / CHECKPOINT["filename"]
            print(f"fetching {CHECKPOINT['url']}", flush=True)
            download(CHECKPOINT["url"], downloaded, CHECKPOINT["sha256"], CHECKPOINT["bytes"])
            downloaded.chmod(0o444)
            os.replace(downloaded, target)
            state = "staged"
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    report = {
        "schema": "fs2-serve.nebius.ai/rfdiffusion-run-input-staging/v1",
        "state": state,
        "is_localization": False,
        "note": (
            "Run-input staging for semantic qualification only. Not a localized "
            "generation, not admissible as one, and not a substitute for the contract "
            "owned by fs2-single-file-model-artifact-localization-r20260903."
        ),
        "filename": CHECKPOINT["filename"],
        "sha256": CHECKPOINT["sha256"],
        "size_bytes": target.stat().st_size,
        "license_id": CHECKPOINT["license_id"],
        "source_uri": CHECKPOINT["url"],
        "source_revision": CHECKPOINT["source_revision"],
        "relative_path": str(target.relative_to(Path(args.root))),
        "absolute_path": str(target),
    }
    Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

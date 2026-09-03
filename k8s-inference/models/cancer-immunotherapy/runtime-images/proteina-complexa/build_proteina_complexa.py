#!/usr/bin/env python3
"""Build and publish the digest-pinned Proteina-Complexa scientific-batch image.

Three rules are enforced before anything is pushed:

1. **Non-overwriting publication.**  An existing target tag aborts the run.  A
   new image needs a new ``tag_suffix`` in ``image-lock.json``.
2. **Clean-commit provenance.**  SLSA provenance is attached at push in
   ``max`` mode, and its VCS revision must name the exact commit that carries
   these build inputs.  A dirty working tree is refused outright, because
   BuildKit would then record a ``-dirty`` revision that names no reviewable
   commit.
3. **Verified source identity.**  The upstream archive digest pinned in the
   lock is re-checked against the live archive before the build, so the build
   cannot silently consume a different tree than the one that was reviewed.

The published digest and the provenance revision are written back into
``image-lock.json`` only with ``--record``, so a build can be rehearsed without
mutating the lock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
IMAGE_LOCK = HERE / "image-lock.json"


def _run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(command), flush=True)
    return subprocess.run(command, check=True, text=True, **kwargs)


def _capture(command: list[str]) -> str:
    return _run(command, stdout=subprocess.PIPE).stdout.strip()


def _lock() -> dict[str, Any]:
    return json.loads(IMAGE_LOCK.read_text(encoding="utf-8"))


def _write_lock(lock: dict[str, Any]) -> None:
    IMAGE_LOCK.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")


def repository_root() -> Path:
    return Path(
        subprocess.run(
            ["git", "-C", str(HERE), "rev-parse", "--show-toplevel"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
    )


def clean_revision(repo: Path) -> str:
    """Return HEAD, refusing to proceed while anything is uncommitted."""
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    if status:
        raise SystemExit(
            "refusing to build: the working tree is not clean, so the attached SLSA\n"
            "provenance would name a '-dirty' revision instead of a reviewable commit.\n"
            "Commit the build inputs first. Outstanding paths:\n" + status
        )
    return _capture(["git", "-C", str(repo), "rev-parse", "HEAD"])


def verify_source_identity(source: dict[str, Any]) -> int:
    """Re-fetch the pinned upstream archive and check its digest."""
    print(f"verifying upstream archive {source['archive_url']}", flush=True)
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(source["archive_url"], timeout=180) as response:
        while True:
            chunk = response.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    observed = digest.hexdigest()
    if observed != source["archive_sha256"]:
        raise SystemExit(
            f"upstream archive is sha256:{observed}, "
            f"but the lock pins sha256:{source['archive_sha256']}"
        )
    print(f"  archive verified: {total} bytes, sha256:{observed}", flush=True)
    return total


def tag_exists(target: str) -> bool:
    result = subprocess.run(
        ["crane", "digest", target], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    return result.returncode == 0 and result.stdout.strip().startswith("sha256:")


def build(lock: dict[str, Any], revision: str, *, push: bool, load: bool) -> str | None:
    image = lock["image"]
    target = image["target_tag"]
    if push and tag_exists(target):
        raise SystemExit(
            f"refusing to overwrite an existing published tag: {target}. "
            "Publication is non-overwriting; choose a new tag_suffix."
        )
    context = (HERE / image["build_context"]).resolve()
    dockerfile = (HERE / image["dockerfile"]).resolve()
    source = lock["source"]

    command = [
        "docker",
        "buildx",
        "build",
        "--platform",
        image["platform"],
        "--file",
        str(dockerfile),
        "--build-arg",
        f"SOURCE_REVISION={source['revision']}",
        "--build-arg",
        f"SOURCE_ARCHIVE_URL={source['archive_url']}",
        "--build-arg",
        f"SOURCE_ARCHIVE_SHA256={source['archive_sha256']}",
        "--build-arg",
        f"SOURCE_DATE_EPOCH={source['source_date_epoch']}",
        # BuildKit reads the build context's git metadata for the provenance
        # VCS fields; the clean-tree check above is what makes them meaningful.
        "--build-arg",
        f"BUILDKIT_CONTEXT_KEEP_GIT_DIR=1",
        "--provenance=mode=max",
        "--sbom=true",
        "--tag",
        target,
        "--metadata-file",
        str(HERE / "evidence" / "build-metadata.json"),
    ]
    if push:
        command.append("--push")
    elif load:
        command.append("--load")
    command.append(str(context))
    _run(command)

    if not push:
        for smoke in image["smoke"]:
            _run(["docker", "run", "--rm", "--entrypoint", smoke[0], target, *smoke[1:]])
        return None

    digest = _capture(["crane", "digest", target])
    if not digest.startswith("sha256:"):
        raise SystemExit(f"registry did not return a digest for {target}")
    return digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-push", action="store_true", help="build and smoke only")
    parser.add_argument("--load", action="store_true", help="with --no-push, load into the daemon")
    parser.add_argument("--record", action="store_true", help="write results into image-lock.json")
    parser.add_argument(
        "--skip-source-verify",
        action="store_true",
        help="skip the pre-build upstream archive digest check (the Dockerfile still verifies it)",
    )
    arguments = parser.parse_args()

    for tool in ("docker", "crane", "git"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} is required")

    lock = _lock()
    repo = repository_root()
    revision = clean_revision(repo)
    print(f"building from clean revision {revision}", flush=True)

    entrypoint = HERE / lock["image"]["runtime_entrypoint"]["path"]
    entrypoint_digest = hashlib.sha256(entrypoint.read_bytes()).hexdigest()
    pinned = lock["image"]["runtime_entrypoint"]["sha256"]
    if pinned not in (None, entrypoint_digest):
        raise SystemExit(
            f"{entrypoint.name} is sha256:{entrypoint_digest} but the lock pins sha256:{pinned}"
        )

    archive_bytes = None
    if not arguments.skip_source_verify:
        archive_bytes = verify_source_identity(lock["source"])

    (HERE / "evidence").mkdir(exist_ok=True)
    digest = build(lock, revision, push=not arguments.no_push, load=arguments.load)

    outcome = {
        "status": "published" if digest else "built",
        "digest": digest,
        "vcs_revision": revision,
        "entrypoint_sha256": entrypoint_digest,
        "archive_bytes": archive_bytes,
    }
    print(json.dumps(outcome, indent=2))

    if arguments.record:
        lock["image"]["runtime_entrypoint"]["sha256"] = entrypoint_digest
        if archive_bytes is not None:
            lock["source"]["archive_bytes"] = archive_bytes
        if digest:
            lock["image"]["published_digest"] = digest
            lock["image"]["provenance"]["vcs_revision"] = revision
        _write_lock(lock)
        print(f"recorded into {IMAGE_LOCK.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

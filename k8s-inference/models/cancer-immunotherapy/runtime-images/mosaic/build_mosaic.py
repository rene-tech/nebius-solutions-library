#!/usr/bin/env python3
"""Build and publish the digest-pinned Mosaic scientific-batch runtime image.

The canonical ``mosaic-batch`` wrapper contract (``bin/mosaic-batch`` and
``recipe.json``) is never copied into this tree.  It is materialised straight
out of the primary adapter candidate commit and byte-verified against the
pinned SHA-256 identities before it can enter the image, so the image cannot
drift away from the adapter that renders its argv.

Publication is non-overwriting: an existing target tag aborts the run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
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


def _git_blob(repo: Path, revision: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{revision}:{path}"],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


def materialise_adapter_context(lock: dict[str, Any], repo: Path, destination: Path) -> None:
    """Extract the canonical adapter contract files and verify their identity."""
    adapter = lock["adapter"]
    revision = adapter["commit"]
    for relative, expected in adapter["files"].items():
        payload = _git_blob(repo, revision, f"{adapter['repository_path']}/{relative}")
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            raise SystemExit(
                f"canonical adapter file {relative} is sha256:{actual}, "
                f"expected sha256:{expected} at {revision}"
            )
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        print(f"  adapter {relative} verified sha256:{actual}", flush=True)


def tag_exists(target: str) -> bool:
    result = subprocess.run(
        ["crane", "digest", target], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    return result.returncode == 0 and result.stdout.strip().startswith("sha256:")


def build(lock: dict[str, Any], repo: Path, *, push: bool) -> str | None:
    image = lock["image"]
    target = image["target_tag"]
    if tag_exists(target):
        raise SystemExit(
            f"refusing to overwrite an existing published tag: {target}. "
            "Publication is non-overwriting; choose a new tag_suffix."
        )
    context = (HERE / image["build_context"]).resolve()
    dockerfile = (HERE / image["dockerfile"]).resolve()
    source = lock["source"]

    with tempfile.TemporaryDirectory(prefix="fs2-mosaic-adapter-") as handle:
        adapter_context = Path(handle)
        materialise_adapter_context(lock, repo, adapter_context)
        command = [
            "docker",
            "build",
            "--platform",
            image["platform"],
            "--file",
            str(dockerfile),
            "--build-context",
            f"adapter={adapter_context}",
            "--build-arg",
            f"SOURCE_REVISION={source['revision']}",
            "--build-arg",
            f"SOURCE_ARCHIVE_URL={source['archive_url']}",
            "--build-arg",
            f"SOURCE_ARCHIVE_SHA256={source['archive_sha256']}",
            "--build-arg",
            f"SOURCE_DATE_EPOCH={source['source_date_epoch']}",
            "--tag",
            target,
            str(context),
        ]
        _run(command)

    for smoke in image["smoke"]:
        _run(["docker", "run", "--rm", "--entrypoint", smoke[0], target, *smoke[1:]])

    if not push:
        return None
    _run(["docker", "push", target])
    digest = _capture(["crane", "digest", target])
    if not digest.startswith("sha256:"):
        raise SystemExit(f"registry did not return a digest for {target}")
    return digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[5],
        help="repository root that contains the primary adapter candidate commit",
    )
    parser.add_argument("--no-push", action="store_true", help="build and smoke only")
    parser.add_argument(
        "--record", action="store_true", help="write the published digest into image-lock.json"
    )
    arguments = parser.parse_args()

    if shutil.which("docker") is None or shutil.which("crane") is None:
        raise SystemExit("docker and crane are required")

    lock = _lock()
    digest = build(lock, arguments.repo, push=not arguments.no_push)
    if digest is None:
        print(json.dumps({"status": "built", "published": False}, sort_keys=True))
        return
    print(json.dumps({"status": "published", "digest": digest}, sort_keys=True))
    if arguments.record:
        lock["image"]["published_digest"] = digest
        IMAGE_LOCK.write_text(json.dumps(lock, indent=2, sort_keys=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())

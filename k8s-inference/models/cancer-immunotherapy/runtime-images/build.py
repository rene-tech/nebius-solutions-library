#!/usr/bin/env python3
"""Build, smoke, and publish immutable scientific runtime images safely."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CATALOG_PATH = ROOT / "catalog.json"
EVIDENCE_PATH = ROOT / "evidence" / "publish-receipt.json"
ARTIFACT_DIR = ROOT / ".runtime-image-artifacts"


def _run(command: list[str], *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(command), flush=True)
    return subprocess.run(
        command,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def _catalog() -> dict[str, Any]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _select(catalog: dict[str, Any], image_id: str) -> dict[str, Any]:
    for image in catalog["images"]:
        if image["id"] == image_id:
            return image
    raise SystemExit(f"unknown image id: {image_id}")


def inspect_target(image: dict[str, Any]) -> dict[str, str | None]:
    completed = _run(["crane", "digest", image["target_tag"]], capture=True, check=False)
    output = (completed.stdout or "").strip()
    if completed.returncode == 0:
        return {"state": "present", "digest": output, "detail": None}
    lower = output.lower()
    if "401 unauthorized" in lower or "403 forbidden" in lower or "denied" in lower:
        raise RuntimeError(f"registry authorization failed for {image['target_tag']}: {output[-500:]}")
    if "404 not found" in lower or "manifest_unknown" in lower or "name_unknown" in lower:
        return {"state": "absent", "digest": None, "detail": output[-500:]}
    raise RuntimeError(f"unable to establish target state for {image['target_tag']}: {output[-500:]}")


def build_image(image: dict[str, Any], no_cache: bool) -> None:
    source = image["source"]
    command = [
        "docker",
        "buildx",
        "build",
        "--platform",
        "linux/amd64",
        "--load",
        "--tag",
        image["target_tag"],
        "--build-arg",
        f"SOURCE_REVISION={source['revision']}",
        "--build-arg",
        f"SOURCE_ARCHIVE_URL={source['archive_url']}",
        "--build-arg",
        f"SOURCE_ARCHIVE_SHA256={source['archive_sha256']}",
        "--build-arg",
        f"SOURCE_DATE_EPOCH={source['source_date_epoch']}",
        "--file",
        str(ROOT / image["dockerfile"]),
    ]
    if image.get("adapter_context"):
        command.extend(["--build-context", f"adapter={ROOT / image['adapter_context']}"])
    if no_cache:
        command.append("--no-cache")
    command.append(str(ROOT / image["build_context"]))
    _run(command)


def smoke_image(image: dict[str, Any]) -> None:
    for smoke in image["smoke"]:
        _run(["docker", "run", "--rm", "--network", "none", image["target_tag"], *smoke])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_receipt(image: dict[str, Any], prior: dict[str, str | None], digest: str) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    sbom = ARTIFACT_DIR / f"{image['id']}.spdx.json"
    _run(["syft", f"docker:{image['target_tag']}", "-o", f"spdx-json={sbom}"])
    local_id = _run(
        ["docker", "image", "inspect", image["target_tag"], "--format", "{{.Id}}"],
        capture=True,
    ).stdout.strip()
    lock_path = ROOT / image["dependency_lock"]["path"]

    if EVIDENCE_PATH.exists():
        receipt = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    else:
        receipt = {
            "schema": "fs2.nebius.ai/scientific-runtime-image-publish-receipt/v1",
            "registry": _catalog()["registry"],
            "images": [],
        }
    receipt["checked_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    record = {
        "id": image["id"],
        "source": image["source"],
        "base": image["base"],
        "accelerator_runtime": image["accelerator_runtime"],
        "dependency_lock_sha256": _sha256(lock_path),
        "target_tag": image["target_tag"],
        "target_pre_push": prior,
        "digest": digest,
        "digest_reference": image["target_tag"].split(":", 1)[0] + "@" + digest,
        "local_image_id": local_id,
        "weight_policy": image["weight_policy"],
        "compatibility_overrides": image.get("compatibility_overrides", []),
        "supersedes": image.get("supersedes"),
        "local_smoke": {"state": "passed", "commands": image["smoke"]},
        "sbom": {"format": "spdx-json", "sha256": _sha256(sbom), "retained_path": str(sbom)},
        "deployment": "not-performed",
    }
    previous = [item for item in receipt["images"] if item["id"] == image["id"]]
    if previous:
        superseded = receipt.setdefault("superseded_images", [])
        superseded.extend(previous)
    receipt["images"] = [item for item in receipt["images"] if item["id"] != image["id"]]
    receipt["images"].append(record)
    receipt["images"].sort(key=lambda item: item["id"])
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def publish_image(image: dict[str, Any]) -> None:
    prior = inspect_target(image)
    if prior["state"] == "present":
        raise RuntimeError(
            f"refusing to overwrite existing immutable target {image['target_tag']} at {prior['digest']}"
        )
    smoke_image(image)
    _run(["docker", "push", image["target_tag"]])
    observed = inspect_target(image)
    if observed["state"] != "present" or not observed["digest"]:
        raise RuntimeError(f"pushed image is not readable from registry: {image['target_tag']}")
    _write_receipt(image, prior, observed["digest"])
    print(f"published {image['target_tag']}@{observed['digest']}")


def validate_catalog(catalog: dict[str, Any]) -> None:
    seen: set[str] = set()
    for image in catalog["images"]:
        if image["id"] in seen:
            raise RuntimeError(f"duplicate image id: {image['id']}")
        seen.add(image["id"])
        expected_tag = image["source"]["revision"]
        if image.get("tag_suffix"):
            expected_tag += "-" + image["tag_suffix"]
        if not image["target_tag"].endswith(":" + expected_tag):
            raise RuntimeError(f"target tag is not the source revision for {image['id']}")
        for key in ("builder", "runtime"):
            if "@sha256:" not in image["base"][key]:
                raise RuntimeError(f"unpinned {key} base for {image['id']}")
        lock = ROOT / image["dependency_lock"]["path"]
        if _sha256(lock) != image["dependency_lock"]["sha256"]:
            raise RuntimeError(f"dependency lock digest mismatch for {image['id']}")
        if image["weight_policy"]["embedded"] is not False:
            raise RuntimeError(f"default image may not embed weights: {image['id']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "inspect", "build", "smoke", "publish"])
    parser.add_argument("image", nargs="?", choices=["proteina-complexa", "boltzgen", "mosaic"])
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()
    catalog = _catalog()
    validate_catalog(catalog)
    if args.action == "check":
        print(json.dumps({"valid": True, "images": [item["id"] for item in catalog["images"]]}))
        return
    if args.image is None:
        parser.error("image is required for this action")
    image = _select(catalog, args.image)
    if args.action == "inspect":
        print(json.dumps(inspect_target(image), sort_keys=True))
    elif args.action == "build":
        build_image(image, args.no_cache)
    elif args.action == "smoke":
        smoke_image(image)
    elif args.action == "publish":
        publish_image(image)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

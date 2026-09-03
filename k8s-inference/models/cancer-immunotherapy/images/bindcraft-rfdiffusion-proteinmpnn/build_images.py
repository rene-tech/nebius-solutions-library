#!/usr/bin/env python3
"""Build and publish exact scientific runtime images without overwriting tags."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = ROOT.parents[3]
ADAPTER_CONTEXT = REPOSITORY_ROOT / "models" / "structure" / "runtime"
LOCK_PATH = ROOT / "image-lock.json"
RECEIPT_PATH = ROOT / "evidence" / "published-images.json"
PUBLISH_BUILDER = os.environ.get("FS2_BUILDX_BUILDER", "fs2-cancer-runtime-publisher")
ABSENT_MARKERS = (
    "manifest unknown",
    "name unknown",
    "repository name not known",
    "entity folder not found",
)
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class BuildError(RuntimeError):
    """The image build or immutable publication contract failed."""


def run(command: list[str], *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=check,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        env={**os.environ, "DOCKER_BUILDKIT": "1"},
    )


def load_lock() -> dict[str, Any]:
    value = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    if value.get("schema") != "fs2.nebius.ai/cancer-runtime-image-lock/v1":
        raise BuildError("unsupported image lock schema")
    if value.get("platform") != "linux/amd64":
        raise BuildError("H100 image lock must target linux/amd64")
    if not FULL_COMMIT.fullmatch(str(value.get("adapter_commit", ""))):
        raise BuildError("adapter commit must be a full immutable Git revision")
    base = value.get("base", {})
    if not isinstance(base.get("reference"), str) or "@sha256:" not in base["reference"]:
        raise BuildError("base image must be digest pinned")
    images = value.get("images")
    if not isinstance(images, list) or not images:
        raise BuildError("image lock is empty")
    ids: set[str] = set()
    targets: set[str] = set()
    for image in images:
        image_id = image.get("id")
        target = image.get("target")
        source = image.get("source", {})
        if not isinstance(image_id, str) or image_id in ids:
            raise BuildError("image IDs must be unique strings")
        if not isinstance(target, str) or target in targets or "@" in target:
            raise BuildError("publication targets must be unique tags")
        if not FULL_COMMIT.fullmatch(str(source.get("revision", ""))):
            raise BuildError(f"{image_id}: source revision is not a full commit")
        if not HEX_SHA256.fullmatch(str(source.get("archive_sha256", ""))):
            raise BuildError(f"{image_id}: source archive has no SHA-256")
        build_tag_suffix = image.get("build_tag_suffix", "")
        if not isinstance(build_tag_suffix, str) or (build_tag_suffix and not build_tag_suffix.startswith("-")):
            raise BuildError(f"{image_id}: build tag suffix must be empty or start with '-'")
        if not target.endswith(":" + source["revision"] + build_tag_suffix):
            raise BuildError(f"{image_id}: target tag does not encode source and build revisions")
        dockerfile = ROOT / str(image.get("dockerfile", ""))
        if not dockerfile.is_file():
            raise BuildError(f"{image_id}: Dockerfile is missing")
        ids.add(image_id)
        targets.add(target)
    return value


def selected_images(lock: dict[str, Any], names: Iterable[str]) -> list[dict[str, Any]]:
    requested = list(names)
    if not requested or requested == ["all"]:
        return list(lock["images"])
    by_id = {image["id"]: image for image in lock["images"]}
    unknown = sorted(set(requested) - set(by_id))
    if unknown:
        raise BuildError("unknown image IDs: " + ", ".join(unknown))
    return [by_id[name] for name in requested]


def local_reference(image: dict[str, Any]) -> str:
    return f"fs2-cancer-runtime/{image['id']}:{image['source']['revision']}"


def inspect_target(target: str) -> str | None:
    completed = run(
        ["skopeo", "inspect", "--format", "{{.Digest}}", f"docker://{target}"],
        capture=True,
        check=False,
    )
    if completed.returncode == 0:
        digest = completed.stdout.strip()
        if SHA256.fullmatch(digest) is None:
            raise BuildError(f"registry returned a malformed digest for {target}")
        return digest
    message = (completed.stderr + completed.stdout).lower()
    if any(marker in message for marker in ABSENT_MARKERS):
        return None
    raise BuildError(f"registry inspection failed for {target}; authenticate or repair access before retrying")


def ensure_absent(image: dict[str, Any]) -> None:
    digest = inspect_target(image["target"])
    if digest is not None:
        raise BuildError(f"refusing to overwrite existing target {image['target']}@{digest}")


def ensure_publish_builder() -> None:
    completed = run(["docker", "buildx", "inspect", PUBLISH_BUILDER], capture=True, check=False)
    if completed.returncode != 0:
        run(
            [
                "docker",
                "buildx",
                "create",
                "--name",
                PUBLISH_BUILDER,
                "--driver",
                "docker-container",
            ],
            capture=True,
        )
    inspected = run(
        ["docker", "buildx", "inspect", "--bootstrap", PUBLISH_BUILDER],
        capture=True,
    )
    if not re.search(r"(?m)^Driver:\s+docker-container\s*$", inspected.stdout):
        raise BuildError(f"publication builder {PUBLISH_BUILDER!r} cannot emit OCI attestations")


def docker_build(image: dict[str, Any], *, push: bool) -> str:
    reference = image["target"] if push else local_reference(image)
    command = [
        "docker",
        "buildx",
        "build",
        "--platform",
        "linux/amd64",
        "--file",
        str(ROOT / image["dockerfile"]),
        "--build-context",
        f"fs2-adapters={ADAPTER_CONTEXT}",
        "--tag",
        reference,
    ]
    if push:
        ensure_publish_builder()
        command[3:3] = ["--builder", PUBLISH_BUILDER]
        command += ["--provenance=mode=max", "--sbom=true", "--push"]
    else:
        command += ["--provenance=false", "--sbom=false", "--load"]
    command.append(str(ROOT))
    run(command)
    return reference


def smoke_commands(image_id: str, reference: str) -> list[list[str]]:
    common = ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none"]
    commands = [common + [reference, "--fs2-image-smoke"]]
    cli = {
        "freebindcraft-open-fallback": [
            "/opt/fs2/bin/freebindcraft-batch",
            "run-trajectory",
            "--help",
        ],
        "proteinmpnn": ["python", "/opt/proteinmpnn/protein_mpnn_run.py", "--help"],
    }.get(image_id)
    if cli:
        commands.append(common + ["--entrypoint", cli[0], reference, *cli[1:]])
    return commands


def smoke(image: dict[str, Any], reference: str) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for command in smoke_commands(image["id"], reference):
        completed = run(command, capture=True)
        output = completed.stdout.strip()
        evidence.append(
            {
                "command": command,
                "stdout_sha256": hashlib.sha256(output.encode()).hexdigest(),
                "last_line": output.splitlines()[-1][:1000] if output else "",
            }
        )
    return evidence


def raw_manifest(target: str) -> tuple[str, int, list[str]]:
    completed = run(["skopeo", "inspect", "--raw", f"docker://{target}"], capture=True)
    raw = completed.stdout.encode()
    value = json.loads(raw)
    manifests = value.get("manifests", []) if isinstance(value, dict) else []
    attestation_descriptors = [
        descriptor
        for descriptor in manifests
        if isinstance(descriptor, dict)
        and descriptor.get("annotations", {}).get("vnd.docker.reference.type") == "attestation-manifest"
    ]
    predicate_types: set[str] = set()
    repository = target.rsplit(":", 1)[0]
    for descriptor in attestation_descriptors:
        digest = descriptor.get("digest")
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            raise BuildError(f"{target}: malformed attestation descriptor")
        attestation = run(
            ["skopeo", "inspect", "--raw", f"docker://{repository}@{digest}"],
            capture=True,
        )
        manifest = json.loads(attestation.stdout)
        for layer in manifest.get("layers", []):
            predicate = layer.get("annotations", {}).get("in-toto.io/predicate-type")
            if isinstance(predicate, str):
                predicate_types.add(predicate)
    return hashlib.sha256(raw).hexdigest(), len(attestation_descriptors), sorted(predicate_types)


def verify_published(lock: dict[str, Any], image: dict[str, Any]) -> dict[str, Any]:
    digest = inspect_target(image["target"])
    if digest is None:
        raise BuildError(f"published target did not resolve: {image['target']}")
    digest_reference = image["target"].rsplit(":", 1)[0] + "@" + digest
    pulled = run(["docker", "pull", "--platform", lock["platform"], digest_reference], capture=True)
    smoke_evidence = smoke(image, digest_reference)
    manifest_sha256, attestation_manifests, predicate_types = raw_manifest(image["target"])
    if "https://spdx.dev/Document" not in predicate_types:
        raise BuildError(f"{image['id']}: SPDX SBOM attestation was not published")
    if not any(value.startswith("https://slsa.dev/provenance/") for value in predicate_types):
        raise BuildError(f"{image['id']}: SLSA provenance attestation was not published")
    return {
        "id": image["id"],
        "source": image["source"],
        "tag": image["target"],
        "digest": digest,
        "digest_reference": digest_reference,
        "manifest_sha256": manifest_sha256,
        "attestation_manifests": attestation_manifests,
        "attestation_predicate_types": predicate_types,
        "pull_stdout_sha256": hashlib.sha256(pulled.stdout.encode()).hexdigest(),
        "smoke": smoke_evidence,
    }


def write_receipt(lock: dict[str, Any], records: list[dict[str, Any]]) -> None:
    by_id: dict[str, dict[str, Any]] = {}
    if RECEIPT_PATH.is_file():
        existing = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
        if existing.get("schema") != "fs2.nebius.ai/cancer-runtime-image-publication/v1":
            raise BuildError("existing publication receipt has an unsupported schema")
        for record in existing.get("images", []):
            if isinstance(record, dict) and isinstance(record.get("id"), str):
                by_id[record["id"]] = record
    for record in records:
        by_id[record["id"]] = record
    receipt = {
        "schema": "fs2.nebius.ai/cancer-runtime-image-publication/v1",
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "platform": lock["platform"],
        "base": lock["base"],
        "images": [by_id[image["id"]] for image in lock["images"] if image["id"] in by_id],
    }
    RECEIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = RECEIPT_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(RECEIPT_PATH)


def command_plan(lock: dict[str, Any], images: list[dict[str, Any]]) -> None:
    print(
        json.dumps(
            {
                "platform": lock["platform"],
                "base": lock["base"]["reference"],
                "images": [
                    {
                        "id": image["id"],
                        "source_revision": image["source"]["revision"],
                        "target": image["target"],
                        "external_artifacts": image["external_artifacts"],
                    }
                    for image in images
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("plan", "check-targets", "build", "smoke", "verify-published", "push"),
    )
    parser.add_argument("images", nargs="*", default=["all"])
    args = parser.parse_args()
    lock = load_lock()
    images = selected_images(lock, args.images)
    if args.action == "plan":
        command_plan(lock, images)
        return
    if args.action == "check-targets":
        for image in images:
            digest = inspect_target(image["target"])
            print(json.dumps({"id": image["id"], "target": image["target"], "digest": digest}))
        return
    if args.action == "build":
        for image in images:
            docker_build(image, push=False)
        return
    if args.action == "smoke":
        for image in images:
            print(json.dumps({"id": image["id"], "smoke": smoke(image, local_reference(image))}, sort_keys=True))
        return
    if args.action == "verify-published":
        for image in images:
            record = verify_published(lock, image)
            write_receipt(lock, [record])
            print(json.dumps(record, sort_keys=True))
        return

    for image in images:
        ensure_absent(image)
        docker_build(image, push=True)
        record = verify_published(lock, image)
        write_receipt(lock, [record])


if __name__ == "__main__":
    try:
        main()
    except (BuildError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

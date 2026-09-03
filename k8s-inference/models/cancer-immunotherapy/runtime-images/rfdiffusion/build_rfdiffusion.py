#!/usr/bin/env python3
"""Build and publish the digest-pinned RFdiffusion runtime image.

Publication is non-overwriting and provenance-attested. Three gates run before an
image can be handed to anyone:

1. **Clean source.** The build refuses to start from a dirty tree. The whole point
   of the attestation is that it names a commit someone else can check out, so a
   ``-dirty`` revision is worthless.
2. **Tag is free.** An existing target tag aborts the run. A registry error that is
   not an unambiguous "absent" also aborts: an authentication failure must never be
   mistaken for a free tag.
3. **Attestations match the source.** After the push, the SLSA provenance is read
   back out of the registry and its recorded VCS revision must equal the commit the
   build ran from. An image whose provenance points at different source is refused,
   not published-with-a-note.

Gate 3 is the one that matters for this task. The images this runtime supersedes
carry provenance with no VCS revision at all and an adapter label naming a commit
that exists on no branch, which is exactly what this script makes impossible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
IMAGE_LOCK = HERE / "image-lock.json"
REPO_ROOT = HERE.parents[4]

PUBLISH_BUILDER = os.environ.get("FS2_BUILDX_BUILDER", "fs2-rfdiffusion-publisher")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SLSA_PREFIX = "https://slsa.dev/provenance/"
SPDX_PREDICATE = "https://spdx.dev/Document"

# Registry errors that unambiguously mean "this tag does not exist". Anything else
# is treated as an unknown state and fails closed.
ABSENT_MARKERS = (
    "manifest unknown",
    "name unknown",
    "repository name not known",
    "entity folder not found",
    "not found",
)


class BuildError(RuntimeError):
    pass


def run(command: list[str], *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(command), flush=True)
    return subprocess.run(  # noqa: S603 - argv vector, shell=False
        command,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def capture(command: list[str]) -> str:
    return run(command, capture=True).stdout.strip()


def lock() -> dict[str, Any]:
    return json.loads(IMAGE_LOCK.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------
# Gate 1: clean source
# --------------------------------------------------------------------------------


def source_state(repo: Path) -> dict[str, str]:
    commit = capture(["git", "-C", str(repo), "rev-parse", "HEAD"])
    if not COMMIT_RE.match(commit):
        raise BuildError(f"could not resolve a commit for {repo}")
    status = capture(["git", "-C", str(repo), "status", "--porcelain"])
    if status:
        raise BuildError(
            "refusing to build from a dirty tree. The published provenance names a "
            "commit, so uncommitted changes would make it unverifiable.\n" + status
        )
    branch = capture(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"])
    return {"commit": commit, "branch": branch}


def runtime_file_digests(entries: dict[str, str]) -> dict[str, str]:
    actual: dict[str, str] = {}
    for relative in entries:
        payload = (HERE / relative).read_bytes()
        actual[relative] = hashlib.sha256(payload).hexdigest()
    return actual


def verify_runtime_inputs(document: dict[str, Any]) -> None:
    """The lock records the sha256 of every file that enters the image, so the
    published layers can be tied back to the checked-in runtime."""
    declared = document["image"]["runtime_inputs"]
    actual = runtime_file_digests(declared)
    for relative, expected in declared.items():
        if actual[relative] != expected:
            raise BuildError(
                f"runtime input {relative} is sha256:{actual[relative]}, "
                f"but image-lock.json pins sha256:{expected}. "
                "Update the lock deliberately, or restore the file."
            )
        print(f"  runtime input {relative} verified sha256:{expected}", flush=True)


# --------------------------------------------------------------------------------
# Gate 2: the tag must be free
# --------------------------------------------------------------------------------


def inspect_target(target: str) -> str | None:
    result = run(
        ["skopeo", "inspect", "--format", "{{.Digest}}", f"docker://{target}"],
        capture=True,
        check=False,
    )
    if result.returncode == 0:
        digest = result.stdout.strip()
        if not SHA256_RE.match(digest):
            raise BuildError(f"registry returned a malformed digest for {target}: {digest!r}")
        return digest
    message = (result.stderr + result.stdout).lower()
    if any(marker in message for marker in ABSENT_MARKERS):
        return None
    raise BuildError(
        f"registry inspection for {target} failed in an unknown way; "
        "authenticate or repair access before retrying.\n" + result.stderr.strip()
    )


def ensure_absent(target: str) -> None:
    digest = inspect_target(target)
    if digest is not None:
        raise BuildError(
            f"refusing to overwrite the existing published tag {target}@{digest}. "
            "Publication is non-overwriting; choose a new tag suffix."
        )
    print(f"  target tag is free: {target}", flush=True)


# --------------------------------------------------------------------------------
# Gate 3: attestations
# --------------------------------------------------------------------------------


def ensure_publish_builder() -> None:
    inspected = run(["docker", "buildx", "inspect", PUBLISH_BUILDER], capture=True, check=False)
    if inspected.returncode != 0:
        run(
            [
                "docker", "buildx", "create",
                "--name", PUBLISH_BUILDER,
                "--driver", "docker-container",
                "--bootstrap",
            ]
        )
        inspected = run(["docker", "buildx", "inspect", PUBLISH_BUILDER], capture=True)
    if not re.search(r"(?m)^Driver:\s+docker-container\s*$", inspected.stdout):
        raise BuildError(
            f"builder {PUBLISH_BUILDER!r} is not a docker-container builder and "
            "cannot emit OCI attestations"
        )


def attestation_predicates(target: str) -> dict[str, dict[str, Any]]:
    """Pull every in-toto predicate attached to the pushed image."""
    index = json.loads(capture(["crane", "manifest", target]))
    predicates: dict[str, dict[str, Any]] = {}
    repository = target.split(":")[0].split("@")[0]

    for descriptor in index.get("manifests", []):
        annotations = descriptor.get("annotations") or {}
        if annotations.get("vnd.docker.reference.type") != "attestation-manifest":
            continue
        manifest = json.loads(capture(["crane", "manifest", f"{repository}@{descriptor['digest']}"]))
        for layer in manifest.get("layers", []):
            predicate_type = (layer.get("annotations") or {}).get("in-toto.io/predicate-type")
            if not predicate_type:
                continue
            blob = capture(["crane", "blob", f"{repository}@{layer['digest']}"])
            try:
                predicates[predicate_type] = json.loads(blob)
            except json.JSONDecodeError:
                predicates[predicate_type] = {}
    return predicates


def provenance_vcs_revision(predicate: dict[str, Any]) -> str | None:
    """Find the source revision BuildKit recorded, across predicate shapes."""
    body = predicate.get("predicate", predicate)

    definition = body.get("buildDefinition") or {}
    external = definition.get("externalParameters") or {}
    for key in ("source_revision", "vcs:revision"):
        value = external.get(key)
        if isinstance(value, str) and COMMIT_RE.match(value):
            return value

    metadata = body.get("metadata") or {}
    for container in (external, metadata, body):
        for key, value in (container or {}).items():
            if key.endswith("vcs:revision") and isinstance(value, str) and COMMIT_RE.match(value):
                return value

    for dependency in definition.get("resolvedDependencies", []) or []:
        digest = dependency.get("digest") or {}
        commit = digest.get("gitCommit")
        if isinstance(commit, str) and COMMIT_RE.match(commit):
            return commit

    # BuildKit also stashes the git metadata under a nested buildkit_ key.
    for value in (metadata or {}).values():
        if isinstance(value, dict):
            candidate = value.get("vcs:revision") or value.get("revision")
            if isinstance(candidate, str) and COMMIT_RE.match(candidate):
                return candidate

    invocation = body.get("invocation") or {}
    config_source = invocation.get("configSource") or {}
    sha1 = (config_source.get("digest") or {}).get("sha1")
    if isinstance(sha1, str) and COMMIT_RE.match(sha1):
        return sha1
    return None


def verify_attestations(target_digest_ref: str, expected_commit: str) -> dict[str, Any]:
    predicates = attestation_predicates(target_digest_ref)
    types = sorted(predicates)
    print(f"  attestation predicate types: {types}", flush=True)

    if SPDX_PREDICATE not in predicates:
        raise BuildError("the SPDX SBOM attestation was not published")

    slsa_type = next((t for t in types if t.startswith(SLSA_PREFIX)), None)
    if slsa_type is None:
        raise BuildError("no SLSA provenance attestation was published")

    recorded = provenance_vcs_revision(predicates[slsa_type])
    if recorded is None:
        raise BuildError(
            "the SLSA provenance records no VCS revision. Refusing to publish an "
            "image whose provenance cannot be tied to a source commit. Ensure the "
            "build context is a clean git checkout."
        )
    if recorded != expected_commit:
        raise BuildError(
            f"the SLSA provenance records source revision {recorded}, but the build "
            f"ran at {expected_commit}. Refusing to hand back an image whose "
            "provenance points at different source."
        )
    print(f"  SLSA provenance vcs revision matches source commit {recorded}", flush=True)
    return {
        "attestation_predicate_types": types,
        "attestation_vcs_revision": recorded,
        "slsa_predicate_type": slsa_type,
    }


# --------------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------------


def build(document: dict[str, Any], *, push: bool, repo: Path) -> dict[str, Any]:
    image = document["image"]
    source = document["source"]
    target = image["target_tag"]

    state = source_state(repo)
    print(f"source commit {state['commit']} on {state['branch']}", flush=True)
    verify_runtime_inputs(document)

    if push:
        ensure_absent(target)

    command = [
        "docker", "buildx", "build",
        "--platform", image["platform"],
        "--file", str(HERE / image["dockerfile"]),
        "--build-arg", f"BASE_IMAGE={image['base']['runtime']}",
        "--build-arg", f"UPSTREAM_REVISION={source['revision']}",
        "--build-arg", f"UPSTREAM_ARCHIVE_URL={source['archive_url']}",
        "--build-arg", f"UPSTREAM_ARCHIVE_SHA256={source['archive_sha256']}",
        "--build-arg", f"DGL_WHEEL_URL={image['dgl_wheel']['url']}",
        "--build-arg", f"DGL_WHEEL_SHA256={image['dgl_wheel']['sha256']}",
        "--tag", target,
    ]
    if push:
        ensure_publish_builder()
        command[3:3] = ["--builder", PUBLISH_BUILDER]
        command += ["--provenance=mode=max", "--sbom=true", "--push"]
    else:
        command += ["--provenance=false", "--sbom=false", "--load"]
    command.append(str(HERE))
    run(command)

    if not push:
        return {"status": "built", "published": False, "source_commit": state["commit"]}

    # The tree must not have moved while the build ran.
    after = source_state(repo)
    if after["commit"] != state["commit"]:
        raise BuildError(
            f"the build context moved from {state['commit']} to {after['commit']} "
            "during the build; refusing to record provenance"
        )

    digest = capture(["crane", "digest", target])
    if not SHA256_RE.match(digest):
        raise BuildError(f"registry did not return a digest for {target}: {digest!r}")

    repository = target.split(":")[0]
    attestations = verify_attestations(f"{repository}@{digest}", state["commit"])

    return {
        "status": "published",
        "published": True,
        "target_tag": target,
        "digest": digest,
        "reference": f"{repository}@{digest}",
        "source_commit": state["commit"],
        "source_branch": state["branch"],
        **attestations,
    }


def record(document: dict[str, Any], result: dict[str, Any]) -> None:
    document["image"]["published_digest"] = result["digest"]
    document["image"]["published_reference"] = result["reference"]
    document["source"]["build_commit"] = result["source_commit"]
    document["source"]["attestation_vcs_revision"] = result["attestation_vcs_revision"]
    document["image"]["attestation_predicate_types"] = result["attestation_predicate_types"]
    IMAGE_LOCK.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"recorded published digest in {IMAGE_LOCK}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--no-push", action="store_true", help="build and load locally only")
    parser.add_argument("--record", action="store_true", help="write the digest into image-lock.json")
    parser.add_argument("--check", action="store_true", help="validate the lock and inputs, build nothing")
    args = parser.parse_args()

    document = lock()

    if args.check:
        verify_runtime_inputs(document)
        image = document["image"]
        expected = (
            f"{image['registry']}/{image['repository']}"
            f":{document['source']['revision']}{image['tag_suffix']}"
        )
        if image["target_tag"] != expected:
            raise BuildError(f"target_tag is {image['target_tag']}, expected {expected}")
        print(json.dumps({"valid": True, "target_tag": image["target_tag"]}, sort_keys=True))
        return 0

    for tool in ("docker", "crane", "skopeo"):
        if shutil.which(tool) is None:
            raise BuildError(f"{tool} is required")

    result = build(document, push=not args.no_push, repo=args.repo)
    print(json.dumps(result, indent=2, sort_keys=True))
    if result.get("published") and args.record:
        record(document, result)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

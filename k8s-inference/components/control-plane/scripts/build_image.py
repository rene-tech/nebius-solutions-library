#!/usr/bin/env python3
"""Build an exact committed fs2-serve image and emit local provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any

SOLUTION_REL = Path("k8s-inference")
CONTROL_REL = SOLUTION_REL / "components/control-plane"
CATALOG_REL = SOLUTION_REL / "catalog/runtime"
DOCKERFILE_REL = CONTROL_REL / "Dockerfile"
CONTEXT_POLICY_REL = CONTROL_REL / "Dockerfile.dockerignore"
LOCK_REL = CONTROL_REL / "uv.lock"
CONTEXT_INPUTS = (
    CONTROL_REL / "pyproject.toml",
    LOCK_REL,
    CONTROL_REL / "README.md",
    CONTROL_REL / "src",
    CONTROL_REL / "migrations",
    CONTROL_REL / "contracts",
    CATALOG_REL / "fs2_serve_catalog",
    CATALOG_REL / "pyproject.toml",
    CATALOG_REL / "uv.lock",
    CATALOG_REL / "catalog.json",
    CATALOG_REL / "contracts",
    CATALOG_REL / "kubernetes",
    CATALOG_REL / "models",
    CATALOG_REL / "schema",
    CATALOG_REL / "sql",
    CATALOG_REL / "validators",
    CATALOG_REL / "packaged-repository",
)
LABEL_KEYS = {
    "commit": "org.opencontainers.image.revision",
    "tree": "ai.nebius.fs2-serve.source-tree",
    "lock_sha256": "ai.nebius.fs2-serve.uv-lock-sha256",
    "dockerfile_sha256": "ai.nebius.fs2-serve.dockerfile-sha256",
    "context_policy_sha256": "ai.nebius.fs2-serve.context-policy-sha256",
}
SBOM_GENERATOR = (
    "docker.io/docker/buildkit-syft-scanner@sha256:ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9"
)
EXPECTED_ATTESTATIONS = {
    "https://spdx.dev/Document",
    "https://slsa.dev/provenance/v1",
}
SUPPORTED_STATEMENT_TYPES = {
    "https://in-toto.io/Statement/v0.1",
    "https://in-toto.io/Statement/v1",
}


def _run(
    command: list[str],
    *,
    cwd: Path,
    capture_output: bool = False,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - every executable and argument is locally derived.
        command,
        cwd=cwd,
        check=True,
        capture_output=capture_output,
        input=input_bytes,
    )


def _git(repo: Path, *arguments: str) -> str:
    result = _run(["git", *arguments], cwd=repo, capture_output=True)
    return result.stdout.decode("utf-8").strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive(repo: Path, commit: str, destination: Path) -> None:
    archive = _run(["git", "archive", "--format=tar", commit], cwd=repo, capture_output=True).stdout
    _run(["tar", "-xf", "-", "-C", str(destination)], cwd=repo, input_bytes=archive)


def _expected_context(repo: Path, commit: str) -> set[str]:
    output = _git(repo, "ls-tree", "-r", "--name-only", commit, "--", *(str(path) for path in CONTEXT_INPUTS))
    return {line for line in output.splitlines() if line}


def _actual_context(root: Path) -> set[str]:
    actual: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("filtered build context contains a symlink")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    return actual


def _builder_arguments(builder: str | None) -> list[str]:
    return ["--builder", builder] if builder else []


def verify_context(
    repo: Path,
    commit: str,
    archive_root: Path,
    audit_root: Path,
    builder: str | None,
) -> set[str]:
    _run(
        [
            "docker",
            "buildx",
            "build",
            *_builder_arguments(builder),
            "--progress=plain",
            "--target",
            "context-audit",
            "--output",
            f"type=local,dest={audit_root}",
            "--file",
            str(archive_root / DOCKERFILE_REL),
            str(archive_root),
        ],
        cwd=repo,
    )
    expected = _expected_context(repo, commit)
    actual = _actual_context(audit_root)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise RuntimeError(
            f"filtered build context differs from committed allowlist; missing={missing!r}, unexpected={unexpected!r}"
        )
    return actual


def _tar_json(bundle: tarfile.TarFile, member_name: str) -> dict[str, Any]:
    try:
        member = bundle.getmember(member_name)
    except KeyError as error:
        raise RuntimeError(f"OCI archive is missing {member_name}") from error
    if not member.isfile() or member.size > 2 * 1024 * 1024:
        raise RuntimeError(f"OCI JSON member is invalid: {member_name}")
    handle = bundle.extractfile(member)
    if handle is None:
        raise RuntimeError(f"OCI JSON member is unreadable: {member_name}")
    value = json.load(handle)
    if not isinstance(value, dict):
        raise RuntimeError(f"OCI JSON member is not an object: {member_name}")
    return value


def _digest_member(digest: object) -> str:
    if not isinstance(digest, str) or not digest.startswith("sha256:") or len(digest) != 71:
        raise RuntimeError("OCI descriptor has an invalid digest")
    return f"blobs/sha256/{digest.removeprefix('sha256:')}"


def _verify_attestations(archive: Path) -> dict[str, Any]:
    with tarfile.open(archive, mode="r") as bundle:
        outer_index = _tar_json(bundle, "index.json")
        outer_manifests = outer_index.get("manifests")
        if not isinstance(outer_manifests, list) or len(outer_manifests) != 1:
            raise RuntimeError("OCI archive must contain exactly one image index")
        outer_descriptor = outer_manifests[0]
        if not isinstance(outer_descriptor, dict):
            raise RuntimeError("OCI image index descriptor is invalid")
        image_index = _tar_json(bundle, _digest_member(outer_descriptor.get("digest")))
        manifests = image_index.get("manifests")
        if not isinstance(manifests, list):
            raise RuntimeError("OCI image index has no manifests")
        image_descriptors: list[dict[str, Any]] = []
        attestation_descriptors: list[dict[str, Any]] = []
        for descriptor in manifests:
            if not isinstance(descriptor, dict):
                raise RuntimeError("OCI manifest descriptor is invalid")
            annotations = descriptor.get("annotations")
            if isinstance(annotations, dict) and annotations.get("vnd.docker.reference.type") == "attestation-manifest":
                attestation_descriptors.append(descriptor)
                continue
            platform = descriptor.get("platform")
            if isinstance(platform, dict) and platform.get("os") == "linux" and platform.get("architecture") == "amd64":
                image_descriptors.append(descriptor)
        if len(image_descriptors) != 1 or len(attestation_descriptors) != 1:
            raise RuntimeError("OCI archive must contain one linux/amd64 image and one attestation manifest")
        image_digest = image_descriptors[0].get("digest")
        if not isinstance(image_digest, str):
            raise RuntimeError("OCI image digest is invalid")
        attestation_descriptor = attestation_descriptors[0]
        annotations = attestation_descriptor.get("annotations")
        if not isinstance(annotations, dict) or annotations.get("vnd.docker.reference.digest") != image_digest:
            raise RuntimeError("OCI attestation does not reference the image manifest")
        attestation = _tar_json(bundle, _digest_member(attestation_descriptor.get("digest")))
        subject = attestation.get("subject")
        # BuildKit 0.29 emits the Docker reference descriptor and statement
        # subjects but no OCI 1.1 manifest-level subject. If a manifest subject
        # exists it must still match; otherwise the already-verified descriptor
        # plus every in-toto statement remains the binding authority.
        if subject is not None and (not isinstance(subject, dict) or subject.get("digest") != image_digest):
            raise RuntimeError("OCI attestation subject does not match the image manifest")
        layers = attestation.get("layers")
        if not isinstance(layers, list):
            raise RuntimeError("OCI attestation manifest has no layers")
        predicate_types: set[str] = set()
        statement_types: set[str] = set()
        for layer in layers:
            if not isinstance(layer, dict):
                raise RuntimeError("OCI attestation layer is invalid")
            layer_annotations = layer.get("annotations")
            predicate_type = (
                layer_annotations.get("in-toto.io/predicate-type") if isinstance(layer_annotations, dict) else None
            )
            if not isinstance(predicate_type, str):
                raise RuntimeError("OCI attestation layer has no predicate type")
            statement = _tar_json(bundle, _digest_member(layer.get("digest")))
            statement_subject = statement.get("subject")
            if (
                statement.get("_type") not in SUPPORTED_STATEMENT_TYPES
                or statement.get("predicateType") != predicate_type
                or not isinstance(statement_subject, list)
                or not any(
                    isinstance(item, dict)
                    and isinstance(item.get("digest"), dict)
                    and item["digest"].get("sha256") == image_digest.removeprefix("sha256:")
                    for item in statement_subject
                )
            ):
                raise RuntimeError("OCI attestation statement is not bound to the image manifest")
            predicate_types.add(predicate_type)
            statement_types.add(statement["_type"])
        if predicate_types != EXPECTED_ATTESTATIONS:
            raise RuntimeError(f"OCI attestation predicates differ from policy: {sorted(predicate_types)!r}")
        return {
            "image_manifest_digest": image_digest,
            "attestation_manifest_digest": attestation_descriptor.get("digest"),
            "attestation_predicates": sorted(predicate_types),
            "attestation_statement_types": sorted(statement_types),
        }


def _source(repo: Path, ref: str) -> tuple[str, str]:
    commit = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    if len(commit) not in {40, 64} or len(tree) not in {40, 64}:
        raise RuntimeError("source commit or tree identity is malformed")
    return commit, tree


def _labels(archive_root: Path, commit: str, tree: str) -> dict[str, str]:
    return {
        "commit": commit,
        "tree": tree,
        "lock_sha256": _sha256(archive_root / LOCK_REL),
        "dockerfile_sha256": _sha256(archive_root / DOCKERFILE_REL),
        "context_policy_sha256": _sha256(archive_root / CONTEXT_POLICY_REL),
    }


def _inspect_image(repo: Path, image: str, expected: dict[str, str]) -> dict[str, Any]:
    result = _run(["docker", "image", "inspect", image], cwd=repo, capture_output=True)
    values = json.loads(result.stdout)
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise RuntimeError("local image inspection returned an invalid document")
    inspected: dict[str, Any] = values[0]
    config = inspected.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict):
        raise RuntimeError("local image has no provenance labels")
    for field, label in LABEL_KEYS.items():
        if labels.get(label) != expected[field]:
            raise RuntimeError(f"local image provenance label mismatch: {label}")
    return inspected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify-context", "build"))
    parser.add_argument("--ref", default="HEAD", help="Committed Git ref to archive and build")
    parser.add_argument("--builder", help="Attestation-capable Buildx builder name")
    parser.add_argument("--image", help="Local image tag; required by build")
    parser.add_argument("--provenance-file", type=Path, help="Output JSON path; required by build")
    parser.add_argument("--oci-file", type=Path, help="OCI archive with SBOM/provenance; required by build")
    args = parser.parse_args()

    control_root = Path(__file__).resolve().parents[1]
    repo = Path(_git(control_root, "rev-parse", "--show-toplevel"))
    commit, tree = _source(repo, args.ref)
    with tempfile.TemporaryDirectory(prefix="fs2-serve-image-") as temporary:
        temporary_root = Path(temporary)
        archive_root = temporary_root / "source"
        audit_root = temporary_root / "context"
        archive_root.mkdir()
        audit_root.mkdir()
        _archive(repo, commit, archive_root)
        context_files = verify_context(repo, commit, archive_root, audit_root, args.builder)
        expected = _labels(archive_root, commit, tree)
        if args.command == "verify-context":
            print(
                json.dumps(
                    {
                        "schema": "fs2-serve.nebius.ai/container-context/v1",
                        **expected,
                        "files": len(context_files),
                        "status": "PASS",
                    },
                    sort_keys=True,
                )
            )
            return
        if not args.image or args.provenance_file is None or args.oci_file is None:
            parser.error("build requires --image, --provenance-file, and --oci-file")
        if args.provenance_file.exists() or args.oci_file.exists():
            parser.error("refusing to overwrite an existing provenance or OCI file")
        metadata_file = temporary_root / "build-metadata.json"
        _run(
            [
                "docker",
                "buildx",
                "build",
                *_builder_arguments(args.builder),
                "--platform",
                "linux/amd64",
                "--provenance=mode=max",
                "--attest",
                f"type=sbom,generator={SBOM_GENERATOR}",
                "--output",
                f"type=oci,dest={args.oci_file}",
                "--metadata-file",
                str(metadata_file),
                "--tag",
                args.image,
                "--build-arg",
                f"FS2_SOURCE_COMMIT={expected['commit']}",
                "--build-arg",
                f"FS2_SOURCE_TREE={expected['tree']}",
                "--build-arg",
                f"FS2_UV_LOCK_SHA256={expected['lock_sha256']}",
                "--build-arg",
                f"FS2_DOCKERFILE_SHA256={expected['dockerfile_sha256']}",
                "--build-arg",
                f"FS2_CONTEXT_POLICY_SHA256={expected['context_policy_sha256']}",
                "--file",
                str(archive_root / DOCKERFILE_REL),
                str(archive_root),
            ],
            cwd=repo,
        )
        attestation = _verify_attestations(args.oci_file)
        provenance_metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        build_provenance = provenance_metadata.get("buildx.build.provenance")
        if (
            not isinstance(build_provenance, dict)
            or build_provenance.get("buildType") != "https://mobyproject.org/buildkit@v1"
            or not isinstance(build_provenance.get("materials"), list)
            or len(build_provenance["materials"]) < 3
        ):
            raise RuntimeError("Buildx metadata has no max-mode provenance materials")
        _run(
            [
                "skopeo",
                "copy",
                "--override-os",
                "linux",
                "--override-arch",
                "amd64",
                f"oci-archive:{args.oci_file}",
                f"docker-daemon:{args.image}",
            ],
            cwd=repo,
        )
        inspected = _inspect_image(repo, args.image, expected)
        provenance = {
            "schema": "fs2-serve.nebius.ai/container-build-provenance/v1",
            **expected,
            "context_files": len(context_files),
            "image": args.image,
            "image_id": inspected.get("Id"),
            "platform": "linux/amd64",
            "oci_archive_sha256": _sha256(args.oci_file),
            **attestation,
            "buildx_metadata": provenance_metadata,
        }
        args.provenance_file.parent.mkdir(parents=True, exist_ok=True)
        args.provenance_file.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({key: provenance[key] for key in ("schema", "commit", "tree", "image_id")}, sort_keys=True))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Sign, verify, and allow-list fs2-serve platform image digests (SAI-09).

Every digest published for the platform namespaces must be cosign-signed with
the operator release key and recorded in the admission allow-list ConfigMap
before it is deployed. The transparency log is intentionally disabled: image
repositories and digests of a private deployment must not be published to a
public Rekor instance.

Subcommands:
  render-allowlist  Render the fs2-image-provenance-allowlist ConfigMap.
  sign              cosign-sign one or more digest references with a key file.
  verify            cosign-verify one or more digest references with a public key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
DIGEST_REFERENCE_PATTERN = re.compile(r"^[a-z0-9.\-]+(?::\d+)?/\S+@sha256:[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ALLOWLIST_NAME = "fs2-image-provenance-allowlist"
ALLOWLIST_NAMESPACE = "fs2-system"
RECEIPT_SCHEMA = "fs2-serve.nebius.ai/release-receipt/v1"


class ProvenanceError(RuntimeError):
    """A bounded, operator-actionable provenance failure."""


def validate_digest(digest: str) -> str:
    if not DIGEST_PATTERN.match(digest):
        raise ProvenanceError(
            f"not an exact sha256 digest: {digest!r}; expected sha256:<64 hex>"
        )
    return digest


def validate_digest_reference(reference: str) -> str:
    if not DIGEST_REFERENCE_PATTERN.match(reference):
        raise ProvenanceError(
            f"not a digest-pinned image reference: {reference!r}; "
            "expected <registry>/<repository>@sha256:<64 hex>"
        )
    return reference


def validate_registry_prefix(prefix: str) -> str:
    if not prefix or "@" in prefix or " " in prefix or "\n" in prefix:
        raise ProvenanceError(f"invalid registry prefix: {prefix!r}")
    if not prefix.endswith("/"):
        raise ProvenanceError(
            f"registry prefix must end with '/' so it cannot match a longer "
            f"host or repository name by accident: {prefix!r}"
        )
    return prefix


def render_allowlist(
    registry_prefixes: Sequence[str],
    platform_repository_prefix: str,
    platform_digests: Sequence[str],
    deploy_principals: Sequence[str] = (),
) -> dict:
    """Render the admission allow-list ConfigMap consumed by policy.yaml."""
    if not registry_prefixes:
        raise ProvenanceError("at least one --registry-prefix is required")
    if not platform_digests:
        raise ProvenanceError("at least one --platform-digest is required")
    if not deploy_principals:
        raise ProvenanceError(
            "at least one --deploy-principal is required; Helm release writes "
            "fail closed without a governed deploy principal list"
        )
    for principal in deploy_principals:
        if not principal.strip() or "\n" in principal:
            raise ProvenanceError(f"invalid deploy principal: {principal!r}")
    prefixes = [validate_registry_prefix(prefix) for prefix in registry_prefixes]
    platform_prefix = validate_registry_prefix(platform_repository_prefix)
    digests = sorted({validate_digest(digest) for digest in platform_digests})
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": ALLOWLIST_NAME,
            "namespace": ALLOWLIST_NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": "fs2-image-provenance",
                "app.kubernetes.io/part-of": "fs2-serve",
                "security.fs2.nebius.ai/finding": "sai-09",
            },
        },
        "data": {
            "registry-prefixes": "\n".join(prefixes),
            "platform-repository-prefix": platform_prefix,
            "platform-digests": "\n".join(digests),
            "deploy-principals": "\n".join(sorted(set(deploy_principals))),
        },
    }


def receipt_path(run_root: Path, digest: str) -> Path:
    return run_root / "release-receipts" / (validate_digest(digest).split(":", 1)[1] + ".json")


def _run_capture(command: Sequence[str]) -> str:
    result = subprocess.run(
        list(command), check=True, capture_output=True, text=True
    )
    return result.stdout


def _git_capture(repository: Path, *arguments: str) -> str:
    return _run_capture(["git", "-C", str(repository), *arguments])


SPDX_PREDICATE = "https://spdx.dev/Document"
SLSA_PREDICATE_PREFIX = "https://slsa.dev/provenance"
IN_TOTO_MEDIA_TYPE = "application/vnd.in-toto+json"


def _validated_attestation_evidence(reference: str, capture) -> dict | None:
    """Validate the BuildKit attestation structure, not just its annotation.

    Returns SBOM evidence only after: the index names an attestation manifest
    whose subject is an image manifest of the same signed index; the fetched
    attestation manifest carries an in-toto layer with the SPDX predicate; and
    the fetched in-toto statement names that exact subject digest.
    """
    repository = reference.rsplit("@", 1)[0]
    try:
        index = json.loads(capture(["crane", "manifest", reference]))
    except subprocess.CalledProcessError:
        return None
    attestation_digest = None
    subject_manifest_digest = None
    for entry in index.get("manifests", []):
        annotations = entry.get("annotations", {})
        if annotations.get("vnd.docker.reference.type") == "attestation-manifest":
            attestation_digest = entry["digest"]
            subject_manifest_digest = annotations.get("vnd.docker.reference.digest")
            break
    if attestation_digest is None:
        return None
    image_manifests = {
        entry["digest"]
        for entry in index.get("manifests", [])
        if entry.get("annotations", {}).get("vnd.docker.reference.type")
        != "attestation-manifest"
    }
    if subject_manifest_digest not in image_manifests:
        raise ProvenanceError(
            f"attestation manifest of {reference} names subject "
            f"{subject_manifest_digest}, which is not an image manifest of the "
            "same signed index"
        )
    attestation_manifest = json.loads(
        capture(["crane", "manifest", f"{repository}@{attestation_digest}"])
    )
    spdx_layer_digest = None
    slsa_layer_digest = None
    for layer in attestation_manifest.get("layers", []):
        if layer.get("mediaType") != IN_TOTO_MEDIA_TYPE:
            continue
        predicate = layer.get("annotations", {}).get("in-toto.io/predicate-type", "")
        if predicate == SPDX_PREDICATE and spdx_layer_digest is None:
            spdx_layer_digest = layer.get("digest")
        elif predicate.startswith(SLSA_PREDICATE_PREFIX) and slsa_layer_digest is None:
            slsa_layer_digest = layer.get("digest")
    if spdx_layer_digest is None:
        raise ProvenanceError(
            f"attestation manifest {attestation_digest} of {reference} carries "
            f"no {IN_TOTO_MEDIA_TYPE} layer with predicate {SPDX_PREDICATE}"
        )
    statement = json.loads(
        capture(["crane", "blob", f"{repository}@{spdx_layer_digest}"])
    )
    statement_subjects = {
        f"sha256:{value}"
        for subject in statement.get("subject", [])
        for algorithm, value in (subject.get("digest") or {}).items()
        if algorithm == "sha256"
    }
    if subject_manifest_digest not in statement_subjects:
        raise ProvenanceError(
            f"SPDX attestation of {reference} names subjects "
            f"{sorted(statement_subjects)}, not the image manifest "
            f"{subject_manifest_digest}"
        )
    return {
        "attestation_manifest_digest": attestation_digest,
        "subject_manifest_digest": subject_manifest_digest,
        "spdx_layer_digest": spdx_layer_digest,
        "slsa_layer_digest": slsa_layer_digest,
        "spdx_sha256": None,
        "spdx_subject_digest": None,
    }


def _validated_spdx_document(digest: str, sbom_path: Path) -> dict:
    """Parse and subject-check a standalone SPDX document (no byte-searching)."""
    try:
        document = json.loads(sbom_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProvenanceError(f"unreadable SPDX document: {sbom_path}") from error
    if not str(document.get("spdxVersion", "")).startswith("SPDX-"):
        raise ProvenanceError(f"{sbom_path} is not an SPDX JSON document")
    digest_hex = digest.split(":", 1)[1]
    structured_values = [
        str(document.get("name", "")),
        str(document.get("documentNamespace", "")),
    ]
    for package in document.get("packages", []):
        for checksum in package.get("checksums", []):
            structured_values.append(str(checksum.get("checksumValue", "")))
        for external in package.get("externalRefs", []):
            structured_values.append(str(external.get("referenceLocator", "")))
    if not any(digest_hex in value for value in structured_values):
        raise ProvenanceError(
            f"SPDX document {sbom_path} does not name the image digest "
            f"{digest} in its name, namespace, checksums, or external refs"
        )
    return {
        "attestation_manifest_digest": None,
        "subject_manifest_digest": None,
        "spdx_layer_digest": None,
        "slsa_layer_digest": None,
        "spdx_sha256": hashlib.sha256(sbom_path.read_bytes()).hexdigest(),
        "spdx_subject_digest": digest,
    }


def create_release_receipt(
    reference: str,
    run_root: Path,
    repository: Path,
    anchor_tag: str,
    key_path: str,
    sbom_path: Path | None = None,
    capture=_run_capture,
) -> dict:
    """Bind an image digest to its source, durable anchor, and SBOM evidence.

    The chain is content-addressed end to end: the signed image digest covers
    the OCI config whose labels carry the source commit/tree; the commit must
    be reachable from the durable anchor tag whose restore-tested bundle hash
    is recorded in the run root; the label tree must equal the Git tree of the
    label commit; SBOM evidence must name this exact image as its subject.
    Deep checks run here, then the receipt itself is cosign-signed so its
    claims are tamper-evident at every later load.
    """
    reference = validate_digest_reference(reference)
    digest = reference.rsplit("@", 1)[1]
    if not anchor_tag.startswith("refs/tags/"):
        anchor_tag = f"refs/tags/{anchor_tag}"
    anchors_file = run_root / "release-anchors.json"
    try:
        anchors = json.loads(anchors_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProvenanceError(
            f"missing or unreadable release anchor store: {anchors_file}"
        ) from error
    anchor = anchors.get(anchor_tag)
    if not isinstance(anchor, dict) or not anchor.get("restore_tested"):
        raise ProvenanceError(
            f"anchor {anchor_tag} has no restore-tested bundle receipt; run "
            "inference-stack anchor-release first"
        )
    bundle = Path(str(anchor.get("bundle_path", "")))
    if not bundle.is_file():
        raise ProvenanceError(f"anchor bundle is missing: {bundle}")
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != anchor.get("sha256"):
        raise ProvenanceError(
            f"anchor bundle no longer matches its recorded SHA-256: {bundle}"
        )
    tag_target = _git_capture(repository, "rev-parse", anchor_tag).strip()
    anchor_commit = _git_capture(
        repository, "rev-parse", f"{anchor_tag}^{{commit}}"
    ).strip()
    if anchor_commit != anchor.get("commit"):
        raise ProvenanceError(
            f"anchor {anchor_tag} moved: bundle receipt binds {anchor.get('commit')}, "
            f"the tag now resolves to {anchor_commit}"
        )

    config = json.loads(
        capture(["crane", "config", "--platform", "linux/amd64", reference])
    )
    labels = (config.get("config", {}) or {}).get("Labels") or {}
    source_commit = labels.get("org.opencontainers.image.revision", "")
    source_tree = labels.get("ai.nebius.fs2-serve.source-tree", "")
    if not COMMIT_PATTERN.match(source_commit):
        raise ProvenanceError(
            f"image {reference} does not carry an exact 40-hex "
            "org.opencontainers.image.revision label; rebuild it from an "
            "anchored commit before receipting"
        )
    if not COMMIT_PATTERN.match(source_tree):
        raise ProvenanceError(
            f"image {reference} does not carry an exact 40-hex "
            "ai.nebius.fs2-serve.source-tree label; rebuild it with the "
            "release tooling before receipting"
        )
    # Prove commit/tree/anchor ancestry against a fresh clone restored from
    # the durable bundle itself, so the recovery evidence is self-contained
    # rather than trusting the current working repository's object store.
    with tempfile.TemporaryDirectory(dir=run_root) as scratch:
        restore = Path(scratch) / "restore.git"
        _run_capture(["git", "clone", "--quiet", "--bare", str(bundle), str(restore)])
        restored_anchor_commit = _git_capture(
            restore, "rev-parse", f"{anchor_tag}^{{commit}}"
        ).strip()
        if restored_anchor_commit != anchor_commit:
            raise ProvenanceError(
                f"bundle restore resolves {anchor_tag} to "
                f"{restored_anchor_commit}, expected {anchor_commit}"
            )
        ancestry = subprocess.run(
            [
                "git",
                "-C",
                str(restore),
                "merge-base",
                "--is-ancestor",
                source_commit,
                restored_anchor_commit,
            ],
            capture_output=True,
            text=True,
        )
        if ancestry.returncode != 0:
            raise ProvenanceError(
                f"image source commit {source_commit} is not reachable from "
                f"anchor {anchor_tag} ({anchor_commit}) in the restored "
                "bundle; anchor the lineage first"
            )
        restored_tree = _git_capture(
            restore, "rev-parse", f"{source_commit}^{{tree}}"
        ).strip()
    if source_tree != restored_tree:
        raise ProvenanceError(
            f"image {reference} source-tree label {source_tree} does not match "
            f"the Git tree {restored_tree} of its revision in the restored bundle"
        )

    sbom_evidence = _validated_attestation_evidence(reference, capture)
    if sbom_evidence is None and sbom_path is not None:
        sbom_evidence = _validated_spdx_document(digest, Path(sbom_path))
    if sbom_evidence is None:
        raise ProvenanceError(
            f"image {reference} carries no validated in-toto SPDX attestation "
            "and no --sbom document was provided; generate an SPDX SBOM first"
        )

    receipt = {
        "schema": RECEIPT_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "image": reference,
        "digest": digest,
        "source": {"commit": source_commit, "tree": source_tree},
        "anchor": {
            "mode": "bundle",
            "tag": anchor_tag,
            "tag_target": tag_target,
            "commit": anchor_commit,
            "restored_commit": anchor_commit,
            "bundle_path": str(bundle),
            "bundle_sha256": anchor["sha256"],
        },
        "sbom": sbom_evidence,
    }
    path = receipt_path(run_root, digest)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    path.chmod(0o600)
    signature = path.parent / (path.name + ".sig")
    capture(
        [
            "cosign",
            "sign-blob",
            "--key",
            key_path,
            "--use-signing-config=false",
            "--tlog-upload=false",
            "--yes",
            "--output-file",
            str(signature),
            str(path),
        ]
    )
    signature.chmod(0o600)
    return receipt


def receipt_verify_blob_command(
    public_key_path: str, receipt: Path, signature: Path
) -> list[str]:
    return [
        "cosign",
        "verify-blob",
        "--key",
        public_key_path,
        "--signature",
        str(signature),
        "--insecure-ignore-tlog=true",
        str(receipt),
    ]


def validate_receipt_binding(receipt: dict, reference: str) -> None:
    """Refuse signing/allow-listing unless the receipt fully binds the digest.

    Deep source/subject checks happened at creation and are tamper-evident via
    the receipt signature; this validation re-verifies everything that can
    drift afterwards: the digest identity, the durable bundle artifact (it
    must exist, hash-match, and still carry the anchor tag at its recorded
    target), the restore evidence, and the SBOM evidence shape.
    """
    reference = validate_digest_reference(reference)
    digest = reference.rsplit("@", 1)[1]
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise ProvenanceError(f"unsupported release receipt schema for {reference}")
    if receipt.get("image") != reference or receipt.get("digest") != digest:
        raise ProvenanceError(
            f"release receipt does not bind {reference}; it names "
            f"{receipt.get('image')!r}"
        )
    source = receipt.get("source", {})
    if not COMMIT_PATTERN.match(str(source.get("commit", ""))) or not COMMIT_PATTERN.match(
        str(source.get("tree", ""))
    ):
        raise ProvenanceError(
            f"release receipt for {reference} lacks an exact source commit and tree"
        )
    anchor = receipt.get("anchor", {})
    if anchor.get("mode") != "bundle" or not str(anchor.get("tag", "")).startswith(
        "refs/tags/"
    ):
        raise ProvenanceError(
            f"release receipt for {reference} lacks a durable bundle anchor"
        )
    if not COMMIT_PATTERN.match(str(anchor.get("restored_commit", ""))) or anchor.get(
        "restored_commit"
    ) != anchor.get("commit"):
        raise ProvenanceError(
            f"release receipt for {reference} lacks restore evidence matching "
            "the anchor commit"
        )
    if not SHA256_PATTERN.match(str(anchor.get("bundle_sha256", ""))):
        raise ProvenanceError(
            f"release receipt for {reference} lacks the anchor bundle SHA-256"
        )
    bundle = Path(str(anchor.get("bundle_path", "")))
    if not bundle.is_file():
        raise ProvenanceError(
            f"anchor bundle for {reference} is missing: {bundle}; a receipt "
            "without its durable artifact does not authorize anything"
        )
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != anchor["bundle_sha256"]:
        raise ProvenanceError(
            f"anchor bundle for {reference} no longer matches the receipt: {bundle}"
        )
    heads = _run_capture(["git", "bundle", "list-heads", str(bundle)])
    expected = f"{anchor.get('tag_target')} {anchor.get('tag')}"
    if expected not in {line.strip() for line in heads.splitlines()}:
        raise ProvenanceError(
            f"anchor bundle for {reference} does not carry {anchor.get('tag')} "
            f"at {anchor.get('tag_target')}"
        )
    sbom = receipt.get("sbom", {})
    attestation = str(sbom.get("attestation_manifest_digest") or "")
    subject = str(sbom.get("subject_manifest_digest") or "")
    spdx_layer = str(sbom.get("spdx_layer_digest") or "")
    spdx = str(sbom.get("spdx_sha256") or "")
    spdx_subject = str(sbom.get("spdx_subject_digest") or "")

    def _is_digest(value: str) -> bool:
        return value.startswith("sha256:") and bool(
            SHA256_PATTERN.match(value.split(":", 1)[1])
        )

    attestation_bound = (
        _is_digest(attestation) and _is_digest(subject) and _is_digest(spdx_layer)
    )
    spdx_bound = bool(SHA256_PATTERN.match(spdx)) and spdx_subject == digest
    if not attestation_bound and not spdx_bound:
        raise ProvenanceError(
            f"release receipt for {reference} lacks subject-bound SBOM evidence "
            "(validated in-toto SPDX layer or subject-checked SPDX document)"
        )


def load_bound_receipt(
    run_root: Path,
    reference: str,
    public_key_path: str,
    verifier=None,
) -> dict:
    """Load a receipt, verify its cosign signature, then verify its bindings."""
    reference = validate_digest_reference(reference)
    path = receipt_path(run_root, reference.rsplit("@", 1)[1])
    signature = path.parent / (path.name + ".sig")
    if not path.is_file() or not signature.is_file():
        raise ProvenanceError(
            f"no signed release receipt for {reference} at {path}; create one "
            "with provenance.py receipt before signing or allow-listing"
        )
    run_verifier = verifier or (
        lambda command: subprocess.run(list(command), check=True, capture_output=True)
    )
    try:
        run_verifier(receipt_verify_blob_command(public_key_path, path, signature))
    except subprocess.CalledProcessError as error:
        raise ProvenanceError(
            f"release receipt signature verification failed for {reference}; "
            "the receipt is not trustworthy"
        ) from error
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProvenanceError(f"unreadable release receipt at {path}") from error
    validate_receipt_binding(receipt, reference)
    return receipt


def verified_allowlist(
    public_key_path: str,
    references: Sequence[str],
    registry_prefixes: Sequence[str],
    platform_repository_prefix: str,
    receipts_root: Path,
    deploy_principals: Sequence[str] = (),
    verifier=None,
) -> dict:
    """Render the allow-list only from receipted references that cosign-verify.

    Every digest that enters the admission allow-list must carry both a valid
    signature by the release key and a bound release receipt (source commit,
    durable restore-tested anchor bundle, SBOM evidence); anything less aborts
    rendering, so the ConfigMap can never drift ahead of the release evidence.
    """
    if not references:
        raise ProvenanceError("at least one --image digest reference is required")
    run_verifier = verifier or (
        lambda command: subprocess.run(list(command), check=True)
    )
    digests = []
    for reference in references:
        reference = validate_digest_reference(reference)
        load_bound_receipt(receipts_root, reference, public_key_path, verifier)
        try:
            run_verifier(cosign_verify_command(public_key_path, reference))
        except subprocess.CalledProcessError as error:
            raise ProvenanceError(
                f"signature verification failed for {reference}; sign it "
                "before allow-listing"
            ) from error
        digests.append(reference.rsplit("@", 1)[1])
    manifest = render_allowlist(
        registry_prefixes, platform_repository_prefix, digests, deploy_principals
    )
    manifest["metadata"].setdefault("annotations", {})[
        "security.fs2.nebius.ai/verified-with-key-sha256"
    ] = hashlib.sha256(Path(public_key_path).read_bytes()).hexdigest()
    return manifest


def cosign_sign_command(key_path: str, reference: str) -> list[str]:
    """Key-based signing without publishing to a public transparency log.

    The signing config and new bundle format are disabled: the first fetches
    public sigstore services, and the regional registry rejects the bundle
    media type, so signatures use the classic per-digest `.sig` tag format.
    """
    return [
        "cosign",
        "sign",
        "--key",
        key_path,
        "--use-signing-config=false",
        "--new-bundle-format=false",
        "--tlog-upload=false",
        "--yes",
        validate_digest_reference(reference),
    ]


def cosign_verify_command(public_key_path: str, reference: str) -> list[str]:
    return [
        "cosign",
        "verify",
        "--key",
        public_key_path,
        "--private-infrastructure=true",
        validate_digest_reference(reference),
    ]


def run_commands(commands: Sequence[Sequence[str]]) -> None:
    for command in commands:
        print("+ " + " ".join(command), file=sys.stderr)
        subprocess.run(list(command), check=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    render = subcommands.add_parser(
        "render-allowlist",
        help=(
            "cosign-verify platform digest references, then render the "
            "admission allow-list ConfigMap as JSON"
        ),
    )
    render.add_argument(
        "--public-key",
        required=True,
        help="cosign public key used to verify every digest before rendering",
    )
    render.add_argument(
        "--registry-prefix",
        action="append",
        default=[],
        help="allowed image reference prefix, must end with '/'; repeatable",
    )
    render.add_argument(
        "--platform-repository-prefix",
        required=True,
        help="repository prefix whose images require an allow-listed digest",
    )
    render.add_argument(
        "--image",
        action="append",
        default=[],
        help="signed platform image (<registry>/<repo>@sha256:<hex>); repeatable",
    )
    render.add_argument(
        "--run-root",
        required=True,
        type=Path,
        help="private run root holding release-receipts/ for every image",
    )
    render.add_argument(
        "--deploy-principal",
        action="append",
        default=[],
        help="Kubernetes username allowed to write Helm release Secrets; repeatable",
    )

    receipt = subcommands.add_parser(
        "receipt",
        help=(
            "bind an image digest to its source commit, durable anchor bundle "
            "and SBOM evidence; signing and allow-listing require this receipt"
        ),
    )
    receipt.add_argument("--image", required=True, help="<registry>/<repo>@sha256:<hex>")
    receipt.add_argument("--run-root", required=True, type=Path)
    receipt.add_argument(
        "--repository",
        required=True,
        type=Path,
        help="Git repository used to prove the image commit is anchored",
    )
    receipt.add_argument(
        "--anchor-tag", required=True, help="release/* or deploy/* anchor tag"
    )
    receipt.add_argument(
        "--sbom",
        type=Path,
        help="SPDX document for images without a BuildKit attestation manifest",
    )
    receipt.add_argument(
        "--key",
        required=True,
        help="cosign private key; the receipt itself is signed so it is tamper-evident",
    )

    sign = subcommands.add_parser(
        "sign",
        help=(
            "cosign-sign digest references with the release key; every "
            "reference must already have a bound release receipt"
        ),
    )
    sign.add_argument("--key", required=True, help="cosign private key path")
    sign.add_argument(
        "--public-key",
        required=True,
        help="cosign public key used to verify each release receipt signature",
    )
    sign.add_argument(
        "--run-root",
        required=True,
        type=Path,
        help="private run root holding release-receipts/ for every reference",
    )
    sign.add_argument("reference", nargs="+", help="<registry>/<repo>@sha256:<hex>")

    verify = subcommands.add_parser(
        "verify", help="cosign-verify digest references with the public key"
    )
    verify.add_argument("--public-key", required=True, help="cosign public key path")
    verify.add_argument("reference", nargs="+", help="<registry>/<repo>@sha256:<hex>")

    args = parser.parse_args(argv)
    if args.command == "render-allowlist":
        manifest = verified_allowlist(
            args.public_key,
            args.image,
            args.registry_prefix,
            args.platform_repository_prefix,
            args.run_root,
            args.deploy_principal,
        )
        print(json.dumps(manifest, indent=2, sort_keys=True))
    elif args.command == "receipt":
        written = create_release_receipt(
            args.image,
            args.run_root,
            args.repository,
            args.anchor_tag,
            args.key,
            args.sbom,
        )
        print(json.dumps(written, indent=2, sort_keys=True))
    elif args.command == "sign":
        for reference in args.reference:
            load_bound_receipt(args.run_root, reference, args.public_key)
        run_commands(
            [cosign_sign_command(args.key, ref) for ref in args.reference]
        )
    elif args.command == "verify":
        run_commands(
            [cosign_verify_command(args.public_key, ref) for ref in args.reference]
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProvenanceError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

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
import os
import re
import stat as stat_module
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
RECEIPT_SCHEMA = "fs2-serve.nebius.ai/release-receipt/v2"
IN_TOTO_STATEMENT_TYPES = (
    "https://in-toto.io/Statement/v0.1",
    "https://in-toto.io/Statement/v1",
)


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
    # One directory per digest so the receipt and its signature publish
    # together in a single atomic directory rename.
    return (
        run_root
        / "release-receipts"
        / validate_digest(digest).split(":", 1)[1]
        / "receipt.json"
    )


def _read_evidence_bytes(path: Path) -> bytes:
    """Read evidence via dirfd + O_NOFOLLOW and refuse filesystem anomalies.

    The directory and the file are both opened without following symlinks, and
    the OPEN DESCRIPTOR is fstat-checked: it must be a regular file with link
    count 1, owned by the caller, with no group/other access. The bytes
    returned are read from that descriptor exactly once, so what is verified
    is what is parsed.
    """
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as error:
            raise ProvenanceError(
                f"cannot open evidence file safely (symlink or missing): {path}"
            ) from error
    finally:
        os.close(parent_fd)
    try:
        status = os.fstat(fd)
        if not stat_module.S_ISREG(status.st_mode):
            raise ProvenanceError(f"evidence path is not a regular file: {path}")
        if status.st_nlink != 1:
            raise ProvenanceError(
                f"evidence file has link count {status.st_nlink}: {path}; "
                "hardlinked evidence is refused"
            )
        if status.st_uid != os.getuid():
            raise ProvenanceError(f"evidence file has a foreign owner: {path}")
        if status.st_mode & 0o077:
            raise ProvenanceError(
                f"evidence file is group/other accessible: {path}; require mode 0600"
            )
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _verify_blob_bytes(
    public_key_path: str,
    payload: bytes,
    signature: bytes,
    verifier,
    context: str,
) -> None:
    """Verify a signature over EXACTLY the bytes the caller will parse.

    cosign re-reads files, so the already-read bytes are written to private
    scratch copies and verified there; the caller then parses the same byte
    string it passed in, eliminating any verify/parse divergence.
    """
    run_verifier = verifier or (
        lambda command: subprocess.run(list(command), check=True, capture_output=True)
    )
    with tempfile.TemporaryDirectory(prefix=".fs2-verify-") as scratch:
        payload_copy = Path(scratch) / "payload"
        signature_copy = Path(scratch) / "payload.sig"
        payload_copy.write_bytes(payload)
        payload_copy.chmod(0o600)
        signature_copy.write_bytes(signature)
        signature_copy.chmod(0o600)
        try:
            run_verifier(
                receipt_verify_blob_command(
                    public_key_path, payload_copy, signature_copy
                )
            )
        except subprocess.CalledProcessError as error:
            raise ProvenanceError(
                f"signature verification failed for {context}"
            ) from error


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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


def _resolve_amd64_image(reference: str, capture) -> tuple[dict, str, str, dict]:
    """Resolve the exact linux/amd64 image manifest, config, and labels.

    Returns (top_manifest, amd64_manifest_digest, config_digest, labels). For a
    multi-platform index the linux/amd64 image manifest must exist exactly
    once; the config blob is fetched, hash-verified against its digest, and
    must itself declare linux/amd64, so the labels provably belong to the
    manifest the SBOM subject names.
    """
    repository = reference.rsplit("@", 1)[0]
    top_text = capture(["crane", "manifest", reference])
    top_sha = hashlib.sha256(top_text.encode("utf-8")).hexdigest()
    if f"sha256:{top_sha}" != reference.rsplit("@", 1)[1]:
        raise ProvenanceError(
            f"fetched top manifest of {reference} hashes to sha256:{top_sha}, "
            "not the reference digest"
        )
    top = json.loads(top_text)
    if "manifests" in top:
        image_entries = [
            entry
            for entry in top.get("manifests", [])
            if entry.get("annotations", {}).get("vnd.docker.reference.type")
            != "attestation-manifest"
        ]
        amd64_entries = [
            entry
            for entry in image_entries
            if entry.get("platform", {}).get("architecture") == "amd64"
            and entry.get("platform", {}).get("os") == "linux"
        ]
        if len(amd64_entries) != 1:
            raise ProvenanceError(
                f"index {reference} must contain exactly one linux/amd64 image "
                f"manifest, found {len(amd64_entries)}"
            )
        amd64_digest = amd64_entries[0]["digest"]
        amd64_text = capture(["crane", "manifest", f"{repository}@{amd64_digest}"])
        amd64_sha = hashlib.sha256(amd64_text.encode("utf-8")).hexdigest()
        if f"sha256:{amd64_sha}" != amd64_digest:
            raise ProvenanceError(
                f"fetched linux/amd64 manifest of {reference} hashes to "
                f"sha256:{amd64_sha}, not its descriptor digest {amd64_digest}"
            )
        amd64_manifest = json.loads(amd64_text)
    else:
        amd64_digest = reference.rsplit("@", 1)[1]
        amd64_manifest = top
    config_digest = (amd64_manifest.get("config") or {}).get("digest", "")
    if not str(config_digest).startswith("sha256:"):
        raise ProvenanceError(
            f"image manifest {amd64_digest} of {reference} has no config digest"
        )
    config_text = capture(["crane", "blob", f"{repository}@{config_digest}"])
    config_sha = hashlib.sha256(config_text.encode("utf-8")).hexdigest()
    if f"sha256:{config_sha}" != config_digest:
        raise ProvenanceError(
            f"fetched config of {reference} hashes to sha256:{config_sha}, "
            f"not its digest {config_digest}"
        )
    config = json.loads(config_text)
    if config.get("architecture") != "amd64" or config.get("os") != "linux":
        raise ProvenanceError(
            f"config {config_digest} of {reference} declares "
            f"{config.get('os')}/{config.get('architecture')}, expected linux/amd64"
        )
    labels = (config.get("config", {}) or {}).get("Labels") or {}
    return top, amd64_digest, config_digest, labels


def _validated_attestation_evidence(
    reference: str, top: dict, amd64_digest: str, capture
) -> dict | None:
    """Validate the BuildKit attestation structure, not just its annotation.

    Selects the attestation whose subject is exactly the linux/amd64 image
    manifest (never merely the first index entry); the fetched attestation
    manifest must carry an in-toto layer with the SPDX predicate, and the
    fetched, hash-verified in-toto statement must name that exact subject.
    """
    repository = reference.rsplit("@", 1)[0]
    attestation_entries = [
        entry
        for entry in top.get("manifests", [])
        if entry.get("annotations", {}).get("vnd.docker.reference.type")
        == "attestation-manifest"
    ]
    if not attestation_entries:
        return None
    matching = [
        entry
        for entry in attestation_entries
        if entry.get("annotations", {}).get("vnd.docker.reference.digest")
        == amd64_digest
    ]
    if not matching:
        raise ProvenanceError(
            f"index {reference} carries attestation manifests, but none whose "
            f"subject is the linux/amd64 image manifest {amd64_digest}"
        )
    if len(matching) > 1:
        raise ProvenanceError(
            f"index {reference} carries {len(matching)} attestation manifests "
            f"for the linux/amd64 image manifest {amd64_digest}; ambiguous "
            "attestations are refused"
        )
    attestation_digest = matching[0]["digest"]
    subject_manifest_digest = amd64_digest
    attestation_text = capture(
        ["crane", "manifest", f"{repository}@{attestation_digest}"]
    )
    attestation_sha = hashlib.sha256(attestation_text.encode("utf-8")).hexdigest()
    if f"sha256:{attestation_sha}" != attestation_digest:
        raise ProvenanceError(
            f"fetched attestation manifest of {reference} hashes to "
            f"sha256:{attestation_sha}, not its descriptor digest "
            f"{attestation_digest}"
        )
    attestation_manifest = json.loads(attestation_text)
    spdx_layers = []
    slsa_layer_digest = None
    for layer in attestation_manifest.get("layers", []):
        if layer.get("mediaType") != IN_TOTO_MEDIA_TYPE:
            continue
        predicate = layer.get("annotations", {}).get("in-toto.io/predicate-type", "")
        if predicate == SPDX_PREDICATE:
            spdx_layers.append(layer.get("digest"))
        elif predicate.startswith(SLSA_PREDICATE_PREFIX) and slsa_layer_digest is None:
            slsa_layer_digest = layer.get("digest")
    if not spdx_layers:
        raise ProvenanceError(
            f"attestation manifest {attestation_digest} of {reference} carries "
            f"no {IN_TOTO_MEDIA_TYPE} layer with predicate {SPDX_PREDICATE}"
        )
    if len(spdx_layers) > 1:
        raise ProvenanceError(
            f"attestation manifest {attestation_digest} of {reference} carries "
            f"{len(spdx_layers)} SPDX predicate layers; ambiguous attestations "
            "are refused"
        )
    spdx_layer_digest = spdx_layers[0]
    # The layer annotation alone proves nothing: fetch the blob, prove it is
    # the content the layer digest names, and validate it as a real in-toto
    # Statement carrying an SPDX document about this exact image.
    statement_text = capture(["crane", "blob", f"{repository}@{spdx_layer_digest}"])
    statement_sha256 = hashlib.sha256(statement_text.encode("utf-8")).hexdigest()
    if f"sha256:{statement_sha256}" != spdx_layer_digest:
        raise ProvenanceError(
            f"fetched SPDX statement of {reference} hashes to "
            f"sha256:{statement_sha256}, not the layer digest {spdx_layer_digest}"
        )
    try:
        statement = json.loads(statement_text)
    except json.JSONDecodeError as error:
        raise ProvenanceError(
            f"SPDX attestation layer of {reference} is not valid JSON"
        ) from error
    if not isinstance(statement, dict) or statement.get("_type") not in IN_TOTO_STATEMENT_TYPES:
        found = statement.get("_type") if isinstance(statement, dict) else type(statement).__name__
        raise ProvenanceError(
            f"SPDX attestation layer of {reference} is not an in-toto "
            f"Statement (got _type={found!r})"
        )
    if statement.get("predicateType") != SPDX_PREDICATE:
        raise ProvenanceError(
            f"attestation statement of {reference} carries predicateType "
            f"{statement.get('predicateType')!r}, expected {SPDX_PREDICATE}"
        )
    subject_hex = subject_manifest_digest.split(":", 1)[1]
    matching_subjects = [
        subject
        for subject in statement.get("subject", []) or []
        if isinstance(subject, dict)
        and (subject.get("digest") or {}).get("sha256") == subject_hex
    ]
    if not matching_subjects:
        raise ProvenanceError(
            f"SPDX attestation of {reference} does not name the image manifest "
            f"{subject_manifest_digest} as a subject"
        )
    if len(matching_subjects) > 1:
        raise ProvenanceError(
            f"SPDX attestation of {reference} names the image manifest "
            f"{subject_manifest_digest} as {len(matching_subjects)} subjects; "
            "ambiguous subjects are refused"
        )
    if not str(matching_subjects[0].get("name", "")).strip():
        raise ProvenanceError(
            f"SPDX attestation subject for {reference} has no name"
        )
    predicate = statement.get("predicate")
    if not isinstance(predicate, dict) or not predicate:
        raise ProvenanceError(
            f"SPDX attestation of {reference} has an empty or non-object predicate"
        )
    _validate_spdx_shape(predicate, f"SPDX predicate of {reference}")
    return {
        "attestation_manifest_digest": attestation_digest,
        "subject_manifest_digest": subject_manifest_digest,
        "spdx_layer_digest": spdx_layer_digest,
        "statement_sha256": statement_sha256,
        "slsa_layer_digest": slsa_layer_digest,
        "spdx_sha256": None,
        "spdx_subject_digest": None,
    }


SPDXID_PATTERN = re.compile(r"^SPDXRef-[A-Za-z0-9.\-]+$")


def _validate_spdx_shape(document: dict, context: str) -> list[dict]:
    """Require a real SPDX 2.x document shape and return the described packages.

    Beyond field presence this enforces valid and unique package SPDXIDs, and
    that the document describes (via `documentDescribes` or a
    SPDXRef-DOCUMENT → package DESCRIBES relationship) only packages that
    actually exist in the document.
    """
    if str(document.get("spdxVersion", "")) not in ("SPDX-2.2", "SPDX-2.3"):
        raise ProvenanceError(
            f"{context} has unsupported spdxVersion {document.get('spdxVersion')!r}"
        )
    if document.get("SPDXID") != "SPDXRef-DOCUMENT":
        raise ProvenanceError(f"{context} lacks SPDXID SPDXRef-DOCUMENT")
    if document.get("dataLicense") != "CC0-1.0":
        raise ProvenanceError(f"{context} lacks the required dataLicense CC0-1.0")
    if not str(document.get("name", "")).strip():
        raise ProvenanceError(f"{context} lacks a document name")
    if not str(document.get("documentNamespace", "")).strip():
        raise ProvenanceError(f"{context} lacks a documentNamespace")
    creation = document.get("creationInfo")
    if (
        not isinstance(creation, dict)
        or not str(creation.get("created", "")).strip()
        or not isinstance(creation.get("creators"), list)
        or not creation["creators"]
    ):
        raise ProvenanceError(
            f"{context} lacks creationInfo with created and creators"
        )
    packages = document.get("packages")
    if not isinstance(packages, list) or not packages:
        raise ProvenanceError(f"{context} describes no packages")
    package_ids: dict[str, dict] = {}
    for package in packages:
        if not isinstance(package, dict):
            raise ProvenanceError(f"{context} contains a non-object package")
        spdx_id = str(package.get("SPDXID", ""))
        if not SPDXID_PATTERN.match(spdx_id):
            raise ProvenanceError(
                f"{context} contains a package with invalid SPDXID {spdx_id!r}"
            )
        if spdx_id in package_ids:
            raise ProvenanceError(
                f"{context} contains duplicate package SPDXID {spdx_id}"
            )
        if not str(package.get("name", "")).strip():
            raise ProvenanceError(
                f"{context} package {spdx_id} has no name"
            )
        if not str(package.get("downloadLocation", "")).strip():
            raise ProvenanceError(
                f"{context} package {spdx_id} has no downloadLocation"
            )
        package_ids[spdx_id] = package
    described_ids = [
        str(identifier) for identifier in document.get("documentDescribes") or []
    ]
    for relationship in document.get("relationships", []) or []:
        if (
            isinstance(relationship, dict)
            and relationship.get("relationshipType") == "DESCRIBES"
            and relationship.get("spdxElementId") == "SPDXRef-DOCUMENT"
        ):
            described_ids.append(str(relationship.get("relatedSpdxElement", "")))
    if not described_ids:
        raise ProvenanceError(
            f"{context} has no SPDXRef-DOCUMENT DESCRIBES relationship or "
            "documentDescribes entry"
        )
    if len(described_ids) != len(set(described_ids)):
        raise ProvenanceError(
            f"{context} describes the same element more than once; ambiguous "
            "descriptions are refused"
        )
    described_packages = []
    for identifier in described_ids:
        if identifier not in package_ids:
            raise ProvenanceError(
                f"{context} describes {identifier}, which is not a package in "
                "the document"
            )
        described_packages.append(package_ids[identifier])
    return described_packages


def _package_names_exact_digest(package: dict, digest_hex: str) -> bool:
    """Exact identity only: a SHA256 checksum value or a purl version part.

    Substring matches anywhere else are deliberately rejected; the digest must
    appear as the package's declared SHA256 checksum or as the exact
    `@sha256:<hex>` version of a defined external subject locator.
    """
    for checksum in package.get("checksums") or []:
        if (
            isinstance(checksum, dict)
            and checksum.get("algorithm") == "SHA256"
            and checksum.get("checksumValue") == digest_hex
        ):
            return True
    for external in package.get("externalRefs") or []:
        if not isinstance(external, dict):
            continue
        if external.get("referenceType") not in ("purl", "locator"):
            continue
        locator = str(external.get("referenceLocator", ""))
        if "@" not in locator:
            continue
        version = locator.rsplit("@", 1)[1].split("?", 1)[0]
        if version == f"sha256:{digest_hex}":
            return True
    return False


def _validated_spdx_document(digest: str, sbom_path: Path) -> dict:
    """Parse, shape-check, and exactly subject-bind a standalone SPDX document."""
    try:
        document = json.loads(sbom_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProvenanceError(f"unreadable SPDX document: {sbom_path}") from error
    if not isinstance(document, dict) or not str(
        document.get("spdxVersion", "")
    ).startswith("SPDX-"):
        raise ProvenanceError(f"{sbom_path} is not an SPDX JSON document")
    described = _validate_spdx_shape(document, f"SPDX document {sbom_path}")
    digest_hex = digest.split(":", 1)[1]
    if not any(
        _package_names_exact_digest(package, digest_hex) for package in described
    ):
        raise ProvenanceError(
            f"SPDX document {sbom_path} does not bind the image digest "
            f"{digest} as an exact SHA256 checksum or subject locator on a "
            "described package; substring mentions do not count"
        )
    return {
        "attestation_manifest_digest": None,
        "subject_manifest_digest": None,
        "spdx_layer_digest": None,
        "statement_sha256": None,
        "slsa_layer_digest": None,
        "spdx_sha256": hashlib.sha256(sbom_path.read_bytes()).hexdigest(),
        "spdx_subject_digest": digest,
    }


def _read_anchor_store_strict(run_root: Path) -> dict:
    """Read the anchor store fail-closed: a malformed store is a refusal."""
    anchors_file = run_root / "release-anchors.json"
    if anchors_file.is_symlink():
        raise ProvenanceError(f"anchor store must not be a symlink: {anchors_file}")
    try:
        anchors = json.loads(anchors_file.read_text(encoding="utf-8"))
    except OSError as error:
        raise ProvenanceError(
            f"missing release anchor store: {anchors_file}"
        ) from error
    except json.JSONDecodeError as error:
        raise ProvenanceError(
            f"release anchor store is malformed and fails closed: {anchors_file}"
        ) from error
    if not isinstance(anchors, dict):
        raise ProvenanceError(
            f"release anchor store is malformed and fails closed: {anchors_file}"
        )
    return anchors


def create_release_receipt(
    reference: str,
    run_root: Path,
    repository: Path,
    anchor_tag: str,
    key_path: str,
    public_key_path: str,
    sbom_path: Path | None = None,
    capture=_run_capture,
) -> dict:
    """Bind an image digest to its source, durable anchor, and SBOM evidence.

    The chain is content-addressed end to end: the signed image digest covers
    the exact linux/amd64 manifest whose hash-verified config carries the
    source commit/tree labels; the commit must be reachable from the durable
    anchor tag — whose current annotated tag OBJECT must equal the recorded
    target and be present in the verified bundle — and the label tree must
    equal the Git tree of the label commit inside a clone restored from that
    bundle; SBOM evidence must name this exact manifest as its subject. Deep
    checks run here; the receipt and its verified cosign signature are then
    published together by one atomic directory rename, so a crash can never
    leave partial evidence at the published path.
    """
    reference = validate_digest_reference(reference)
    digest = reference.rsplit("@", 1)[1]
    if not anchor_tag.startswith("refs/tags/"):
        anchor_tag = f"refs/tags/{anchor_tag}"
    anchors = _read_anchor_store_strict(run_root)
    anchor = anchors.get(anchor_tag)
    if not isinstance(anchor, dict) or not anchor.get("restore_tested"):
        raise ProvenanceError(
            f"anchor {anchor_tag} has no restore-tested bundle receipt; run "
            "inference-stack anchor-release first"
        )
    bundle = Path(str(anchor.get("bundle_path", "")))
    if bundle.is_symlink():
        raise ProvenanceError(f"anchor bundle path is a symlink: {bundle}")
    if not bundle.is_file():
        raise ProvenanceError(f"anchor bundle is missing: {bundle}")
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != anchor.get("sha256"):
        raise ProvenanceError(
            f"anchor bundle no longer matches its recorded SHA-256: {bundle}"
        )
    tag_target = _git_capture(repository, "rev-parse", anchor_tag).strip()
    if _git_capture(repository, "cat-file", "-t", tag_target).strip() != "tag":
        raise ProvenanceError(
            f"anchor {anchor_tag} is not an annotated tag object; lightweight "
            "tags are never receipted"
        )
    anchor_commit = _git_capture(
        repository, "rev-parse", f"{anchor_tag}^{{commit}}"
    ).strip()
    # The exact annotated tag OBJECT must be unchanged, not merely its peeled
    # commit: a deleted-and-recreated tag at the same commit is a different
    # object and is refused before anything is signed.
    if tag_target != anchor.get("tag_target"):
        raise ProvenanceError(
            f"anchor {anchor_tag} tag object changed: recorded "
            f"{anchor.get('tag_target')}, the tag now resolves to {tag_target}; "
            "a recreated tag is never receipted — anchor a new tag"
        )
    if anchor_commit != anchor.get("commit"):
        raise ProvenanceError(
            f"anchor {anchor_tag} moved: bundle receipt binds {anchor.get('commit')}, "
            f"the tag now resolves to {anchor_commit}"
        )
    bundle_heads = _run_capture(["git", "bundle", "list-heads", str(bundle)])
    if f"{tag_target} {anchor_tag}" not in {
        line.strip() for line in bundle_heads.splitlines()
    }:
        raise ProvenanceError(
            f"verified bundle {bundle} does not carry {anchor_tag} at tag "
            f"object {tag_target}"
        )

    top, amd64_digest, config_digest, labels = _resolve_amd64_image(
        reference, capture
    )
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
        restored_tag_target = _git_capture(restore, "rev-parse", anchor_tag).strip()
        if restored_tag_target != tag_target:
            raise ProvenanceError(
                f"bundle restore resolves {anchor_tag} to tag object "
                f"{restored_tag_target}, expected {tag_target}"
            )
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

    sbom_evidence = _validated_attestation_evidence(
        reference, top, amd64_digest, capture
    )
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
        "image_manifest": {
            "amd64_manifest_digest": amd64_digest,
            "config_digest": config_digest,
        },
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
    return _publish_receipt(
        run_root, reference, receipt, key_path, public_key_path, capture
    )


def _existing_receipt_or_conflict(
    run_root: Path,
    final_dir: Path,
    reference: str,
    receipt: dict,
    public_key_path: str,
    capture,
) -> dict:
    """Idempotence with full revalidation: bytes equal or refuse.

    The existing signature is cryptographically re-verified over the exact
    read bytes — never trusted on existence — and the recorded bindings are
    revalidated before the identity comparison decides between idempotent
    return and refusal.
    """
    path = final_dir / "receipt.json"
    signature = final_dir / "receipt.json.sig"
    if (
        path.is_symlink()
        or signature.is_symlink()
        or not path.is_file()
        or not signature.is_file()
    ):
        raise ProvenanceError(
            f"partial receipt evidence already exists for {reference} at "
            f"{final_dir}; refusing to overwrite — restore or archive the "
            "existing evidence first"
        )
    receipt_bytes = _read_evidence_bytes(path)
    signature_bytes = _read_evidence_bytes(signature)
    try:
        _verify_blob_bytes(
            public_key_path,
            receipt_bytes,
            signature_bytes,
            lambda command: capture(command),
            f"existing receipt of {reference}",
        )
    except ProvenanceError as error:
        raise ProvenanceError(
            f"existing receipt signature for {reference} fails verification; "
            "receipts are immutable — refusing to overwrite; investigate the "
            "tamper"
        ) from error
    try:
        existing = json.loads(receipt_bytes)
    except json.JSONDecodeError as error:
        raise ProvenanceError(
            f"existing receipt for {reference} is unreadable; receipts are "
            "immutable — refusing to overwrite"
        ) from error
    validate_receipt_binding(existing, reference, run_root)
    comparable_existing = {k: v for k, v in existing.items() if k != "created_at"}
    comparable_new = {k: v for k, v in receipt.items() if k != "created_at"}
    if comparable_existing != comparable_new:
        raise ProvenanceError(
            f"a different release receipt already exists for {reference} at "
            f"{path}; receipts are immutable — investigate the conflict and, "
            "only if superseding is intended, archive the old receipt and "
            "signature before creating a new one"
        )
    return existing


def _publish_receipt(
    run_root: Path,
    reference: str,
    receipt: dict,
    key_path: str,
    public_key_path: str,
    capture,
) -> dict:
    """Publish receipt+signature atomically; existing evidence is write-once.

    The receipt is written to a same-filesystem staging directory, signed,
    the signature is VERIFIED, both files are fsynced, and only then is the
    directory renamed into place. A crash leaves at most an inert staging
    directory, never partial published evidence; a concurrent publisher loses
    the rename race and falls back to the idempotence/conflict check.
    """
    path = receipt_path(run_root, receipt["digest"])
    final_dir = path.parent
    parent = final_dir.parent
    _assert_receipt_tree_safe(run_root, final_dir)
    if final_dir.exists():
        return _existing_receipt_or_conflict(
            run_root, final_dir, reference, receipt, public_key_path, capture
        )
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_receipt_tree_safe(run_root, final_dir)
    staging = Path(
        tempfile.mkdtemp(dir=parent, prefix=f".tmp-{receipt['digest'][7:19]}-")
    )
    try:
        staged_receipt = staging / "receipt.json"
        staged_signature = staging / "receipt.json.sig"
        staged_receipt.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        staged_receipt.chmod(0o600)
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
                str(staged_signature),
                str(staged_receipt),
            ]
        )
        staged_signature.chmod(0o600)
        # The complete artifact pair is verified before publication.
        capture(
            receipt_verify_blob_command(
                public_key_path, staged_receipt, staged_signature
            )
        )
        _fsync_file(staged_receipt)
        _fsync_file(staged_signature)
        _fsync_dir(staging)
        try:
            os.rename(staging, final_dir)
        except OSError:
            # Lost a publication race; the winner's evidence governs.
            return _existing_receipt_or_conflict(
                run_root, final_dir, reference, receipt, public_key_path, capture
            )
        _fsync_dir(parent)
        return receipt
    finally:
        if staging.exists():
            for leftover in staging.iterdir():
                leftover.unlink()
            staging.rmdir()


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


def validate_receipt_binding(receipt: dict, reference: str, run_root: Path) -> None:
    """Refuse signing/allow-listing unless the receipt fully binds the digest.

    Nothing recorded is taken on faith: the digest identity, the SBOM subject
    == the recorded linux/amd64 manifest, the durable bundle artifact (exists,
    hash-matches, still carries the anchor tag at its recorded target), and —
    via a fresh clone restored from the bundle — the tag object, the anchor
    commit, the source-commit ancestry, and the source tree are all
    re-proven on every load.
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
    if bundle.is_symlink() or not bundle.is_file():
        raise ProvenanceError(
            f"anchor bundle for {reference} is missing or a symlink: {bundle}; "
            "a receipt without its durable artifact does not authorize anything"
        )
    if hashlib.sha256(_read_evidence_bytes(bundle)).hexdigest() != anchor[
        "bundle_sha256"
    ]:
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
    # Re-prove the recorded source identity against the bundle itself: a
    # receipt naming a commit, tree, or tag object the durable artifact does
    # not actually contain is refused, whatever its other fields claim.
    with tempfile.TemporaryDirectory(dir=run_root) as scratch:
        restore = Path(scratch) / "revalidate.git"
        _run_capture(["git", "clone", "--quiet", "--bare", str(bundle), str(restore)])
        restored_target = _git_capture(restore, "rev-parse", anchor["tag"]).strip()
        restored_commit = _git_capture(
            restore, "rev-parse", f"{anchor['tag']}^{{commit}}"
        ).strip()
        if restored_target != anchor.get("tag_target") or restored_commit != anchor.get(
            "commit"
        ):
            raise ProvenanceError(
                f"anchor bundle for {reference} restores {anchor['tag']} to "
                f"{restored_target} ({restored_commit}), not the recorded "
                f"{anchor.get('tag_target')} ({anchor.get('commit')})"
            )
        ancestry = subprocess.run(
            [
                "git",
                "-C",
                str(restore),
                "merge-base",
                "--is-ancestor",
                source["commit"],
                restored_commit,
            ],
            capture_output=True,
            text=True,
        )
        if ancestry.returncode != 0:
            raise ProvenanceError(
                f"receipt source commit {source['commit']} for {reference} is "
                "not reachable from the anchor in the restored bundle"
            )
        restored_tree = _git_capture(
            restore, "rev-parse", f"{source['commit']}^{{tree}}"
        ).strip()
        if restored_tree != source["tree"]:
            raise ProvenanceError(
                f"receipt source tree {source['tree']} for {reference} does "
                f"not match the tree {restored_tree} of its commit in the "
                "restored bundle"
            )
    image_manifest = receipt.get("image_manifest", {})
    amd64_digest = str(image_manifest.get("amd64_manifest_digest") or "")
    config_digest = str(image_manifest.get("config_digest") or "")

    sbom = receipt.get("sbom", {})
    attestation = str(sbom.get("attestation_manifest_digest") or "")
    subject = str(sbom.get("subject_manifest_digest") or "")
    spdx_layer = str(sbom.get("spdx_layer_digest") or "")
    statement_sha = str(sbom.get("statement_sha256") or "")
    spdx = str(sbom.get("spdx_sha256") or "")
    spdx_subject = str(sbom.get("spdx_subject_digest") or "")

    def _is_digest(value: str) -> bool:
        return value.startswith("sha256:") and bool(
            SHA256_PATTERN.match(value.split(":", 1)[1])
        )

    if not _is_digest(amd64_digest) or not _is_digest(config_digest):
        raise ProvenanceError(
            f"release receipt for {reference} lacks the exact linux/amd64 "
            "manifest and config digests"
        )
    attestation_bound = (
        _is_digest(attestation)
        and _is_digest(subject)
        and subject == amd64_digest
        and _is_digest(spdx_layer)
        and bool(SHA256_PATTERN.match(statement_sha))
        and spdx_layer == f"sha256:{statement_sha}"
    )
    spdx_bound = bool(SHA256_PATTERN.match(spdx)) and spdx_subject == digest
    if not attestation_bound and not spdx_bound:
        raise ProvenanceError(
            f"release receipt for {reference} lacks SBOM evidence bound to the "
            "exact linux/amd64 manifest (in-toto subject == recorded amd64 "
            "manifest) or a subject-checked SPDX document"
        )


def _assert_receipt_tree_safe(run_root: Path, final_dir: Path) -> None:
    """Refuse symlinks at every component of the receipt publication path."""
    receipts_parent = final_dir.parent
    for component in (run_root, receipts_parent, final_dir):
        if component.is_symlink():
            raise ProvenanceError(f"receipt path component is a symlink: {component}")
    if receipts_parent.exists():
        expected = run_root.resolve(strict=True) / receipts_parent.name
        if receipts_parent.resolve() != expected:
            raise ProvenanceError(
                f"receipt store escapes the run root: {receipts_parent}"
            )


def load_bound_receipt(
    run_root: Path,
    reference: str,
    public_key_path: str,
    verifier=None,
) -> dict:
    """Load a receipt, verify its cosign signature, then verify its bindings.

    The receipt and signature are read once through dirfd/O_NOFOLLOW with
    anomaly checks, the signature is verified over exactly those bytes, the
    same bytes are parsed, and the parsed bindings are then fully revalidated
    against the durable bundle artifact.
    """
    reference = validate_digest_reference(reference)
    path = receipt_path(run_root, reference.rsplit("@", 1)[1])
    _assert_receipt_tree_safe(run_root, path.parent)
    signature = path.parent / (path.name + ".sig")
    if not path.parent.is_dir() or not path.is_file() or not signature.is_file():
        raise ProvenanceError(
            f"no signed release receipt for {reference} at {path}; create one "
            "with provenance.py receipt before signing or allow-listing"
        )
    receipt_bytes = _read_evidence_bytes(path)
    signature_bytes = _read_evidence_bytes(signature)
    try:
        _verify_blob_bytes(
            public_key_path,
            receipt_bytes,
            signature_bytes,
            verifier,
            f"release receipt of {reference}",
        )
    except ProvenanceError as error:
        raise ProvenanceError(
            f"release receipt signature verification failed for {reference}; "
            "the receipt is not trustworthy"
        ) from error
    try:
        receipt = json.loads(receipt_bytes)
    except json.JSONDecodeError as error:
        raise ProvenanceError(f"unreadable release receipt at {path}") from error
    validate_receipt_binding(receipt, reference, run_root)
    return receipt


INVENTORY_SCHEMA = "fs2-serve.nebius.ai/release-inventory/v2"
INVENTORY_SOURCES = (
    "live_workloads",
    "helm_rollback_window",
    "frozen_scientific_bindings",
)
DRAIN_REASON_PATTERN = re.compile(
    r"^(incident|change|ticket|task):[A-Za-z0-9][A-Za-z0-9._/-]{1,63}"
    r"( [\x20-\x7e]{1,160})?$"
)
INVENTORY_MAX_AGE_HOURS = 24
_CLOCK_SKEW_SECONDS = 300


def _parse_rfc3339(value: str, context: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ProvenanceError(f"{context} has an invalid timestamp {value!r}") from error
    if parsed.tzinfo is None:
        raise ProvenanceError(f"{context} timestamp {value!r} lacks a timezone")
    return parsed


def load_signed_inventory(
    inventory_path: Path,
    public_key_path: str,
    verifier=None,
    max_age_hours: float = INVENTORY_MAX_AGE_HOURS,
) -> dict:
    """Load and verify the signed, complete platform-image inventory.

    The inventory is the acceptance-gate document enumerating every platform
    image reference from the live workloads, the Helm rollback window, and the
    frozen scientific-stage bindings. Each source carries its observation
    snapshot (observed_at plus the resource identities it was read from) and
    the document carries the cluster identity and capture time, which must be
    fresh — a stale or future-dated inventory is refused, bounding replay.

    Drains can never remove an ACTIVE image: a drained reference must not
    appear in live_workloads and must come from a non-live source (rollback
    window or frozen bindings), with a reason bound to a tracking identifier.
    Owner scope decisions about live sibling programs belong in the admission
    policy's match scope, never in inventory falsification. platform_images
    must equal the source union minus those audited non-live drains.
    """
    signature = inventory_path.parent / (inventory_path.name + ".sig")
    if (
        inventory_path.is_symlink()
        or signature.is_symlink()
        or not inventory_path.is_file()
        or not signature.is_file()
    ):
        raise ProvenanceError(
            f"a signed release inventory is required: {inventory_path} and "
            f"{signature} must both exist (and not be symlinks); assemble it "
            "from live workloads, the Helm rollback window, and frozen "
            "scientific bindings, then sign it with the release key"
        )
    inventory_bytes = _read_evidence_bytes(inventory_path)
    signature_bytes = _read_evidence_bytes(signature)
    try:
        _verify_blob_bytes(
            public_key_path,
            inventory_bytes,
            signature_bytes,
            verifier,
            f"inventory {inventory_path}",
        )
    except ProvenanceError as error:
        raise ProvenanceError(
            f"inventory signature verification failed for {inventory_path}"
        ) from error
    try:
        inventory = json.loads(inventory_bytes)
    except json.JSONDecodeError as error:
        raise ProvenanceError(f"unreadable inventory: {inventory_path}") from error
    if not isinstance(inventory, dict) or inventory.get("schema") != INVENTORY_SCHEMA:
        raise ProvenanceError(
            f"{inventory_path} is not a {INVENTORY_SCHEMA} document"
        )
    if not str(inventory.get("cluster", "")).strip():
        raise ProvenanceError(
            f"{inventory_path} lacks the cluster identity it was captured from"
        )
    now = datetime.now(UTC)
    captured_at = _parse_rfc3339(
        inventory.get("captured_at", ""), f"{inventory_path} captured_at"
    )
    age_seconds = (now - captured_at).total_seconds()
    if age_seconds < -_CLOCK_SKEW_SECONDS:
        raise ProvenanceError(f"{inventory_path} is dated in the future")
    if age_seconds > max_age_hours * 3600:
        raise ProvenanceError(
            f"{inventory_path} is stale: captured {captured_at.isoformat()}, "
            f"older than {max_age_hours}h; re-capture the inventory"
        )
    sources = inventory.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(INVENTORY_SOURCES):
        raise ProvenanceError(
            f"{inventory_path} must enumerate exactly these sources: "
            + ", ".join(INVENTORY_SOURCES)
        )
    union: set[str] = set()
    per_source: dict[str, set[str]] = {}
    for name in INVENTORY_SOURCES:
        source = sources.get(name) or {}
        refs = source.get("refs")
        if not isinstance(refs, list):
            raise ProvenanceError(f"{inventory_path} source {name} lacks a refs list")
        observed_at = _parse_rfc3339(
            source.get("observed_at", ""), f"{inventory_path} source {name}"
        )
        if abs((now - observed_at).total_seconds()) > max_age_hours * 3600:
            raise ProvenanceError(
                f"{inventory_path} source {name} observation is stale or "
                "future-dated"
            )
        resource_ids = source.get("resource_ids")
        if not isinstance(resource_ids, list) or (refs and not resource_ids):
            raise ProvenanceError(
                f"{inventory_path} source {name} lacks the resource identities "
                "its refs were observed on"
            )
        per_source[name] = {
            validate_digest_reference(str(reference)) for reference in refs
        }
        union |= per_source[name]
    live = per_source["live_workloads"]
    non_live = per_source["helm_rollback_window"] | per_source[
        "frozen_scientific_bindings"
    ]
    drained: set[str] = set()
    for removal in inventory.get("drained_removals") or []:
        if not isinstance(removal, dict):
            raise ProvenanceError(
                f"{inventory_path} drained removals must be objects"
            )
        image = validate_digest_reference(str(removal.get("image", "")))
        reason = str(removal.get("reason", ""))
        if not DRAIN_REASON_PATTERN.match(reason):
            raise ProvenanceError(
                f"{inventory_path} drain of {image} needs a reason bound to a "
                "tracking identifier (incident:/change:/ticket:/task:)"
            )
        if image in live:
            raise ProvenanceError(
                f"{inventory_path} drains {image}, which is STILL LIVE in "
                "live_workloads; an active image can never be drained — scope "
                "the admission policy instead, or receipt and sign the image"
            )
        if image not in non_live:
            raise ProvenanceError(
                f"{inventory_path} drains {image}, which no non-live source "
                "lists; there is nothing to drain"
            )
        drained.add(image)
    platform_images = inventory.get("platform_images")
    if not isinstance(platform_images, list) or not platform_images:
        raise ProvenanceError(f"{inventory_path} lists no platform images")
    listed = [validate_digest_reference(str(ref)) for ref in platform_images]
    if len(listed) != len(set(listed)):
        raise ProvenanceError(f"{inventory_path} lists duplicate platform images")
    expected = union - drained
    if set(listed) != expected:
        missing = sorted(expected - set(listed))
        extras = sorted(set(listed) - expected)
        raise ProvenanceError(
            f"{inventory_path} platform_images do not equal sources minus "
            f"drained removals; missing: {missing or 'none'}; extras: "
            f"{extras or 'none'}"
        )
    return inventory


def verified_allowlist(
    public_key_path: str,
    references: Sequence[str],
    registry_prefixes: Sequence[str],
    platform_repository_prefix: str,
    receipts_root: Path,
    inventory_path: Path,
    deploy_principals: Sequence[str] = (),
    verifier=None,
    max_age_hours: float = INVENTORY_MAX_AGE_HOURS,
) -> dict:
    """Render the allow-list only from the signed, complete inventory.

    The reference set comes from the verified inventory — never from an
    arbitrary operator-chosen subset. If explicit --image references are also
    given they must equal the inventory set exactly (extras and missing are
    both refused). Every inventory digest must then carry a bound release
    receipt and a valid cosign signature, so the ConfigMap can never drift
    ahead of the release evidence or silently drop coverage.
    """
    inventory = load_signed_inventory(
        inventory_path, public_key_path, verifier, max_age_hours
    )
    inventory_references = sorted(
        validate_digest_reference(str(ref)) for ref in inventory["platform_images"]
    )
    if references:
        given = {validate_digest_reference(ref) for ref in references}
        if given != set(inventory_references):
            missing = sorted(set(inventory_references) - given)
            extras = sorted(given - set(inventory_references))
            raise ProvenanceError(
                "--image references must equal the signed inventory exactly; "
                f"missing: {missing or 'none'}; extras: {extras or 'none'}"
            )
    run_verifier = verifier or (
        lambda command: subprocess.run(list(command), check=True)
    )
    digests = []
    for reference in inventory_references:
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
    annotations = manifest["metadata"].setdefault("annotations", {})
    annotations["security.fs2.nebius.ai/verified-with-key-sha256"] = hashlib.sha256(
        Path(public_key_path).read_bytes()
    ).hexdigest()
    annotations["security.fs2.nebius.ai/inventory-sha256"] = hashlib.sha256(
        inventory_path.read_bytes()
    ).hexdigest()
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
    render.add_argument(
        "--inventory",
        required=True,
        type=Path,
        help=(
            "signed release-inventory JSON (with .sig) enumerating live "
            "workloads, the Helm rollback window, frozen scientific bindings, "
            "and audited non-live drained removals; the allow-list renders "
            "only from it"
        ),
    )
    render.add_argument(
        "--max-inventory-age-hours",
        type=float,
        default=INVENTORY_MAX_AGE_HOURS,
        help="refuse inventories captured longer ago than this (freshness bound)",
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
    receipt.add_argument(
        "--public-key",
        required=True,
        help="cosign public key; the receipt signature is verified before publication",
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
            args.inventory,
            args.deploy_principal,
            max_age_hours=args.max_inventory_age_hours,
        )
        print(json.dumps(manifest, indent=2, sort_keys=True))
    elif args.command == "receipt":
        written = create_release_receipt(
            args.image,
            args.run_root,
            args.repository,
            args.anchor_tag,
            args.key,
            args.public_key,
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

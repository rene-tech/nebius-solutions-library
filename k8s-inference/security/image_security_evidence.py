#!/usr/bin/env python3
"""Fail-closed validators and receipt writer for SAI-24 scan evidence.

This module intentionally uses only the Python standard library so the release
gate does not need another package-manager bootstrap. Receipt creation is used
by CI after an image/filesystem scan; validation never treats a tag as an image
identity and never treats an absent or malformed report as a pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DIGEST_REFERENCE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
TAG_REFERENCE = re.compile(r"^[^\s@]+:[^\s:@/]+$")
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_OBJECT = re.compile(r"^[0-9a-f]{40,64}$")
BLOCKING_SEVERITIES = {"CRITICAL", "HIGH"}


class EvidenceError(ValueError):
    """Raised when evidence is incomplete, mutable, or unsafe to promote."""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{path}: unreadable JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{path}: JSON root must be an object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise EvidenceError(f"{path}: cannot hash: {exc}") from exc
    return digest.hexdigest()


def _git_identity(repository: Path) -> tuple[str, str]:
    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise EvidenceError(f"git {' '.join(arguments)} failed")
        return completed.stdout.strip()

    if git("status", "--porcelain"):
        raise EvidenceError("source repository is not clean")
    return git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")


def validate_detached_signature(
    subject: Path, signature: Path, trust_path: Path
) -> None:
    trust = _load_object(trust_path)
    if trust.get("schema") != "fs2-serve.nebius.ai/image-attestation-trust/v1":
        raise EvidenceError(f"{trust_path}: unsupported trust schema")
    if trust.get("state") != "trusted":
        raise EvidenceError(f"{trust_path}: attestation trust is not active")
    public_key_value = trust.get("public_key_path")
    public_key_sha256 = trust.get("public_key_sha256")
    if not isinstance(public_key_value, str) or not public_key_value:
        raise EvidenceError(f"{trust_path}: public key path is missing")
    if not isinstance(public_key_sha256, str) or not HEX_SHA256.fullmatch(
        public_key_sha256
    ):
        raise EvidenceError(f"{trust_path}: public key fingerprint is invalid")
    public_key = (trust_path.parent / public_key_value).resolve()
    try:
        public_key.relative_to(trust_path.parent.resolve())
    except ValueError as exc:
        raise EvidenceError(f"{trust_path}: public key escapes trust root") from exc
    if _sha256(public_key) != public_key_sha256:
        raise EvidenceError(f"{trust_path}: public key fingerprint mismatch")
    completed = subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(public_key),
            "-signature",
            str(signature),
            str(subject),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise EvidenceError(f"{subject}: detached attestation signature is invalid")


def validate_attestation_identity(
    identity: Any, trust_path: Path, *, purpose: str
) -> dict[str, str]:
    trust = _load_object(trust_path)
    policy = trust.get("oidc_release_attestation")
    if not isinstance(policy, dict):
        raise EvidenceError(f"{trust_path}: OIDC attestation policy is missing")
    if not isinstance(identity, dict):
        raise EvidenceError(f"{purpose}: OIDC attestation identity is missing")
    expected = {
        "issuer": policy.get("issuer"),
        "audience": policy.get("audience"),
        "repository": policy.get("repository"),
        "environment": policy.get("protected_environment"),
    }
    for field, value in expected.items():
        if not isinstance(value, str) or not value or identity.get(field) != value:
            raise EvidenceError(f"{purpose}: OIDC {field} is not authorized")
    expected_subject = (
        f"repo:{expected['repository']}:environment:{expected['environment']}"
    )
    if identity.get("subject") != expected_subject:
        raise EvidenceError(f"{purpose}: OIDC subject is not the protected environment")
    workflow_ref = identity.get("workflow_ref")
    allowed_workflows = policy.get("allowed_workflow_refs")
    if (
        not isinstance(workflow_ref, str)
        or not isinstance(allowed_workflows, list)
        or workflow_ref not in allowed_workflows
    ):
        raise EvidenceError(f"{purpose}: workflow ref is not authorized")
    for field in ("run_id", "run_attempt"):
        if not isinstance(identity.get(field), str) or not identity[field]:
            raise EvidenceError(f"{purpose}: OIDC {field} is missing")
    return identity


def _repository_and_tag(reference: str) -> tuple[str, str]:
    repository, separator, tag = reference.rpartition(":")
    if not separator or not repository or not tag or "/" in tag:
        raise EvidenceError(f"invalid exact tag reference: {reference}")
    return repository, tag


def _validate_registry_resolution(
    image: dict[str, Any], inventory_path: Path, trust_path: Path
) -> None:
    identifier = image["id"]
    source_reference = image["source_reference"]
    digest_reference = image["digest_reference"]
    repository, tag = _repository_and_tag(source_reference)
    registry = repository.split("/", 1)[0]
    manifest_digest = digest_reference.rsplit("@", 1)[1]
    provenance = image["resolution_provenance"]
    receipt_value = provenance.get("resolution_receipt_path")
    signature_value = provenance.get("resolution_receipt_signature_path")
    signature_sha256 = provenance.get("resolution_receipt_signature_sha256")
    for value, label in (
        (receipt_value, "receipt"),
        (signature_value, "receipt signature"),
    ):
        if not isinstance(value, str) or not value or Path(value).is_absolute():
            raise EvidenceError(
                f"{inventory_path}: {identifier} resolution {label} path is invalid"
            )
    if not isinstance(signature_sha256, str) or not HEX_SHA256.fullmatch(
        signature_sha256
    ):
        raise EvidenceError(
            f"{inventory_path}: {identifier} resolution signature hash is invalid"
        )
    receipt_path = (inventory_path.parent / receipt_value).resolve()
    signature_path = (inventory_path.parent / signature_value).resolve()
    for resolved, label in (
        (receipt_path, "receipt"),
        (signature_path, "receipt signature"),
    ):
        try:
            resolved.relative_to(inventory_path.parent.resolve())
        except ValueError as exc:
            raise EvidenceError(
                f"{inventory_path}: {identifier} resolution {label} escapes inventory root"
            ) from exc
    if _sha256(receipt_path) != provenance["resolution_receipt_sha256"]:
        raise EvidenceError(
            f"{inventory_path}: {identifier} resolution receipt hash mismatch"
        )
    if _sha256(signature_path) != signature_sha256:
        raise EvidenceError(
            f"{inventory_path}: {identifier} resolution signature hash mismatch"
        )
    validate_detached_signature(receipt_path, signature_path, trust_path)
    receipt = _load_object(receipt_path)
    if receipt.get("schema") != "fs2-serve.nebius.ai/registry-resolution-receipt/v1":
        raise EvidenceError(f"{receipt_path}: unsupported resolution receipt schema")
    expected_scalars = {
        "source_reference": source_reference,
        "resolved_reference": digest_reference,
        "registry": registry,
        "repository": repository,
        "tag": tag,
    }
    for field, expected in expected_scalars.items():
        if receipt.get(field) != expected:
            raise EvidenceError(f"{receipt_path}: {field} differs from inventory")
    manifest = receipt.get("manifest")
    if not isinstance(manifest, dict):
        raise EvidenceError(f"{receipt_path}: manifest evidence is missing")
    if manifest.get("digest") != manifest_digest:
        raise EvidenceError(f"{receipt_path}: manifest digest differs from subject")
    if manifest.get("sha256") != manifest_digest.removeprefix("sha256:"):
        raise EvidenceError(f"{receipt_path}: manifest bytes do not hash to digest")
    if manifest.get("media_type") != provenance.get("manifest_media_type"):
        raise EvidenceError(f"{receipt_path}: manifest media type differs")
    resolution = receipt.get("resolution")
    resolver = resolution.get("resolver") if isinstance(resolution, dict) else None
    if not isinstance(resolver, dict):
        raise EvidenceError(f"{receipt_path}: resolver identity is missing")
    trust = _load_object(trust_path)
    resolver_policy = trust.get("registry_resolution")
    allowed = (
        resolver_policy.get("authorized_resolver_identities")
        if isinstance(resolver_policy, dict)
        else None
    )
    if not isinstance(allowed, list) or resolver.get("identity") not in allowed:
        raise EvidenceError(f"{receipt_path}: resolver identity is not authorized")
    if resolver.get("identity") != provenance.get("resolver_identity"):
        raise EvidenceError(f"{receipt_path}: resolver identity differs from inventory")
    if resolution.get("resolved_at") != provenance.get("resolved_at"):
        raise EvidenceError(f"{receipt_path}: resolution time differs from inventory")
    command_contract = resolution.get("command_contract")
    if not isinstance(command_contract, str) or not command_contract:
        raise EvidenceError(f"{receipt_path}: resolution command contract is missing")
    validate_attestation_identity(
        receipt.get("attestation"), trust_path, purpose=str(receipt_path)
    )


def validate_inventory(
    path: Path, trust_path: Path | None = None
) -> list[dict[str, Any]]:
    inventory = _load_object(path)
    if inventory.get("schema") != "fs2-serve.nebius.ai/third-party-image-lock/v1":
        raise EvidenceError(f"{path}: unsupported schema")
    images = inventory.get("images")
    if not isinstance(images, list) or not images:
        raise EvidenceError(f"{path}: images must be a non-empty array")

    identifiers: set[str] = set()
    digest_references: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, image in enumerate(images):
        if not isinstance(image, dict):
            raise EvidenceError(f"{path}: images[{index}] must be an object")
        identifier = image.get("id")
        source_reference = image.get("source_reference")
        digest_reference = image.get("digest_reference")
        consumers = image.get("consumers")
        if not isinstance(identifier, str) or not identifier:
            raise EvidenceError(f"{path}: images[{index}].id is required")
        if identifier in identifiers:
            raise EvidenceError(f"{path}: duplicate image id {identifier}")
        identifiers.add(identifier)
        if not isinstance(source_reference, str) or "@sha256:" in source_reference:
            raise EvidenceError(
                f"{path}: {identifier} needs its original tag in source_reference"
            )
        if not TAG_REFERENCE.fullmatch(source_reference):
            raise EvidenceError(f"{path}: {identifier} needs one exact source tag")
        if not isinstance(digest_reference, str) or not DIGEST_REFERENCE.fullmatch(
            digest_reference
        ):
            raise EvidenceError(
                f"{path}: {identifier} is not bound to an exact sha256 digest"
            )
        if source_reference.rsplit(":", 1)[0] != digest_reference.split("@", 1)[0]:
            raise EvidenceError(
                f"{path}: {identifier} source and digest repositories differ"
            )
        if digest_reference in digest_references:
            raise EvidenceError(
                f"{path}: duplicate digest reference {digest_reference}"
            )
        digest_references.add(digest_reference)
        if not isinstance(consumers, list) or not consumers or not all(
            isinstance(value, str) and value for value in consumers
        ):
            raise EvidenceError(f"{path}: {identifier} needs at least one consumer")
        provenance = image.get("resolution_provenance")
        if not isinstance(provenance, dict):
            raise EvidenceError(
                f"{path}: {identifier} resolution provenance is missing"
            )
        if provenance.get("source_reference") != source_reference:
            raise EvidenceError(f"{path}: {identifier} provenance source differs")
        if provenance.get("digest_reference") != digest_reference:
            raise EvidenceError(f"{path}: {identifier} provenance digest differs")
        for field in ("manifest_media_type", "resolved_at", "resolver_identity"):
            if not isinstance(provenance.get(field), str) or not provenance[field]:
                raise EvidenceError(
                    f"{path}: {identifier} provenance {field} is missing"
                )
        for field in ("manifest_sha256", "resolution_receipt_sha256"):
            if not isinstance(provenance.get(field), str) or not HEX_SHA256.fullmatch(
                provenance[field]
            ):
                raise EvidenceError(
                    f"{path}: {identifier} provenance {field} is invalid"
                )
        if provenance["manifest_sha256"] != digest_reference.rsplit(":", 1)[1]:
            raise EvidenceError(
                f"{path}: {identifier} manifest hash differs from OCI digest"
            )
        if trust_path is None:
            raise EvidenceError(
                f"{path}: {identifier} resolution trust policy is required"
            )
        _validate_registry_resolution(image, path, trust_path)
        validated.append(image)
    return validated


def validate_first_party_inventory(
    path: Path, trust_path: Path
) -> list[dict[str, Any]]:
    inventory = _load_object(path)
    if inventory.get("schema") != "fs2-serve.nebius.ai/first-party-image-lock/v1":
        raise EvidenceError(f"{path}: unsupported first-party image lock schema")
    if inventory.get("inventory_state") != "protected-build-attested":
        raise EvidenceError(f"{path}: first-party image lock is not accepted")
    images = inventory.get("images")
    if not isinstance(images, list) or not images:
        raise EvidenceError(f"{path}: first-party images must be non-empty")
    identifiers: set[str] = set()
    references: set[str] = set()
    for index, image in enumerate(images):
        if not isinstance(image, dict):
            raise EvidenceError(f"{path}: images[{index}] must be an object")
        identifier = image.get("id")
        digest_reference = image.get("digest_reference")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise EvidenceError(f"{path}: invalid or duplicate first-party image id")
        identifiers.add(identifier)
        if not isinstance(digest_reference, str) or not DIGEST_REFERENCE.fullmatch(
            digest_reference
        ):
            raise EvidenceError(f"{path}: {identifier} is not digest-bound")
        if digest_reference in references:
            raise EvidenceError(f"{path}: duplicate first-party digest reference")
        references.add(digest_reference)
        repository_suffix = image.get("repository_suffix")
        repository = digest_reference.split("@", 1)[0]
        if (
            not isinstance(repository_suffix, str)
            or not repository_suffix.startswith("/")
            or not repository.endswith(repository_suffix)
            or repository.startswith("registry.example.invalid/")
        ):
            raise EvidenceError(f"{path}: {identifier} production repository is invalid")
        build_source = image.get("build_source")
        if not isinstance(build_source, dict) or not all(
            GIT_OBJECT.fullmatch(str(build_source.get(field, "")))
            for field in ("commit", "tree")
        ):
            raise EvidenceError(f"{path}: {identifier} build source is invalid")
        attestation = image.get("build_attestation")
        if not isinstance(attestation, dict):
            raise EvidenceError(f"{path}: {identifier} build attestation is missing")
        attestation_value = attestation.get("path")
        expected_sha256 = attestation.get("sha256")
        signature_value = attestation.get("signature_path")
        signature_sha256 = attestation.get("signature_sha256")
        if (
            not isinstance(attestation_value, str)
            or Path(attestation_value).is_absolute()
            or not isinstance(expected_sha256, str)
            or not HEX_SHA256.fullmatch(expected_sha256)
            or signature_value != f"{attestation_value}.sig"
            or not isinstance(signature_sha256, str)
            or not HEX_SHA256.fullmatch(signature_sha256)
        ):
            raise EvidenceError(f"{path}: {identifier} build attestation binding is invalid")
        attestation_path = (path.parent / attestation_value).resolve()
        signature_path = (path.parent / signature_value).resolve()
        try:
            attestation_path.relative_to(path.parent.resolve())
            signature_path.relative_to(path.parent.resolve())
        except ValueError as exc:
            raise EvidenceError(f"{path}: {identifier} build attestation escapes lock root") from exc
        if _sha256(attestation_path) != expected_sha256:
            raise EvidenceError(f"{path}: {identifier} build attestation hash mismatch")
        if _sha256(signature_path) != signature_sha256:
            raise EvidenceError(f"{path}: {identifier} build signature hash mismatch")
        validate_detached_signature(attestation_path, signature_path, trust_path)
        document = _load_object(attestation_path)
        if document.get("schema") != "fs2-serve.nebius.ai/first-party-build-attestation/v1":
            raise EvidenceError(f"{attestation_path}: unsupported build attestation schema")
        if document.get("source") != build_source:
            raise EvidenceError(f"{path}: {identifier} build source differs from attestation")
        if document.get("subject") != digest_reference:
            raise EvidenceError(f"{path}: {identifier} build subject differs from lock")
        dockerfile = document.get("dockerfile")
        if not isinstance(dockerfile, dict) or dockerfile.get("path") != image.get(
            "dockerfile"
        ) or not HEX_SHA256.fullmatch(str(dockerfile.get("sha256", ""))):
            raise EvidenceError(f"{path}: {identifier} Dockerfile attestation is invalid")
        retained = document.get("retained_evidence")
        if not isinstance(retained, dict):
            raise EvidenceError(f"{path}: {identifier} retained evidence is missing")
        for field in (
            "buildkit_provenance_sha256",
            "oci_archive_sha256",
            "sbom_sha256",
            "report_sha256",
            "scan_receipt_sha256",
            "evidence_archive_sha256",
        ):
            if not HEX_SHA256.fullmatch(str(retained.get(field, ""))):
                raise EvidenceError(
                    f"{path}: {identifier} retained evidence {field} is invalid"
                )
        if retained.get("oci_manifest_sha256") != digest_reference.rsplit(":", 1)[1]:
            raise EvidenceError(f"{path}: {identifier} manifest hash differs")
        if not isinstance(retained.get("artifact_id"), str) or not retained[
            "artifact_id"
        ].isdigit() or not isinstance(retained.get("retention_until"), str) or not retained[
            "retention_until"
        ]:
            raise EvidenceError(f"{path}: {identifier} retained artifact identity is invalid")
        validate_attestation_identity(
            document.get("attestation"), trust_path, purpose=str(attestation_path)
        )
    return images


def validate_catalog_image_map(
    path: Path, trust_path: Path
) -> dict[str, str]:
    document = _load_object(path)
    if document.get("schema") != "fs2-serve.nebius.ai/catalog-image-map/v1":
        raise EvidenceError(f"{path}: unsupported catalog image map schema")
    if document.get("mapping_state") != "production-mapped":
        raise EvidenceError(f"{path}: catalog image map is not accepted")
    placeholders = document.get("placeholder_registries")
    mappings = document.get("mappings")
    if not isinstance(placeholders, list) or not placeholders:
        raise EvidenceError(f"{path}: placeholder registries are missing")
    if not isinstance(mappings, list) or not mappings:
        raise EvidenceError(f"{path}: catalog mappings must be non-empty")
    result: dict[str, str] = {}
    for index, mapping in enumerate(mappings):
        if not isinstance(mapping, dict):
            raise EvidenceError(f"{path}: mappings[{index}] must be an object")
        source = mapping.get("source_reference")
        production = mapping.get("production_reference")
        if (
            not isinstance(source, str)
            or not DIGEST_REFERENCE.fullmatch(source)
            or source.split("/", 1)[0] not in placeholders
        ):
            raise EvidenceError(f"{path}: mappings[{index}] source is not a placeholder digest")
        if (
            not isinstance(production, str)
            or not DIGEST_REFERENCE.fullmatch(production)
            or production.split("/", 1)[0] in placeholders
            or production.rsplit("@", 1)[1] != source.rsplit("@", 1)[1]
        ):
            raise EvidenceError(f"{path}: mappings[{index}] production digest is invalid")
        if source in result:
            raise EvidenceError(f"{path}: duplicate mapping for {source}")
        receipt = mapping.get("mapping_receipt")
        if not isinstance(receipt, dict):
            raise EvidenceError(f"{path}: mappings[{index}] receipt is missing")
        receipt_value = receipt.get("path")
        signature_value = receipt.get("signature_path")
        if not all(
            isinstance(value, str) and value and not Path(value).is_absolute()
            for value in (receipt_value, signature_value)
        ):
            raise EvidenceError(f"{path}: mappings[{index}] receipt paths are invalid")
        receipt_path = (path.parent / receipt_value).resolve()
        signature_path = (path.parent / signature_value).resolve()
        for resolved in (receipt_path, signature_path):
            try:
                resolved.relative_to(path.parent.resolve())
            except ValueError as exc:
                raise EvidenceError(
                    f"{path}: mappings[{index}] receipt escapes lock root"
                ) from exc
        expected_receipt = receipt.get("sha256")
        expected_signature = receipt.get("signature_sha256")
        if (
            not isinstance(expected_receipt, str)
            or not HEX_SHA256.fullmatch(expected_receipt)
            or not isinstance(expected_signature, str)
            or not HEX_SHA256.fullmatch(expected_signature)
            or _sha256(receipt_path) != expected_receipt
            or _sha256(signature_path) != expected_signature
        ):
            raise EvidenceError(f"{path}: mappings[{index}] receipt hash mismatch")
        validate_detached_signature(receipt_path, signature_path, trust_path)
        receipt_document = _load_object(receipt_path)
        if receipt_document.get("schema") != "fs2-serve.nebius.ai/catalog-image-mapping-receipt/v1":
            raise EvidenceError(f"{receipt_path}: unsupported mapping receipt schema")
        if receipt_document.get("source_reference") != source or receipt_document.get(
            "production_reference"
        ) != production:
            raise EvidenceError(f"{receipt_path}: mapping subject differs from lock")
        production_repository = production.split("@", 1)[0]
        production_registry = production_repository.split("/", 1)[0]
        if receipt_document.get("production_repository") != production_repository:
            raise EvidenceError(f"{receipt_path}: production repository differs")
        if receipt_document.get("production_registry") != production_registry:
            raise EvidenceError(f"{receipt_path}: production registry differs")
        manifest = receipt_document.get("manifest")
        production_digest = production.rsplit("@", 1)[1]
        if not isinstance(manifest, dict) or manifest != {
            "digest": production_digest,
            "sha256": production_digest.removeprefix("sha256:"),
        }:
            raise EvidenceError(f"{receipt_path}: mapped manifest identity differs")
        resolver = receipt_document.get("resolver")
        trust = _load_object(trust_path)
        resolver_policy = trust.get("registry_resolution")
        authorized_resolvers = (
            resolver_policy.get("authorized_resolver_identities")
            if isinstance(resolver_policy, dict)
            else None
        )
        if (
            not isinstance(resolver, dict)
            or not isinstance(authorized_resolvers, list)
            or resolver.get("identity") not in authorized_resolvers
        ):
            raise EvidenceError(f"{receipt_path}: mapping resolver is not authorized")
        validate_attestation_identity(
            receipt_document.get("attestation"), trust_path, purpose=str(receipt_path)
        )
        result[source] = production
    return result


def alpine_package_constraint(path: Path, package_name: str) -> str:
    lock = _load_object(path)
    if lock.get("schema") != "fs2-serve.nebius.ai/alpine-runtime-package-lock/v1":
        raise EvidenceError(f"{path}: unsupported Alpine package lock schema")
    if lock.get("state") != "version-pinned":
        raise EvidenceError(f"{path}: Alpine package lock is not version-pinned")
    if not DIGEST_REFERENCE.fullmatch(str(lock.get("base_image", ""))):
        raise EvidenceError(f"{path}: Alpine package base is not digest-bound")
    packages = lock.get("packages")
    constraint = packages.get(package_name) if isinstance(packages, dict) else None
    if not isinstance(constraint, str) or not re.fullmatch(
        rf"{re.escape(package_name)}=[A-Za-z0-9._+~-]+", constraint
    ):
        raise EvidenceError(f"{path}: {package_name} is not exactly version-pinned")
    return constraint


def scan_summary(path: Path) -> dict[str, int]:
    report = _load_object(path)
    results = report.get("Results")
    if not isinstance(results, list):
        raise EvidenceError(f"{path}: Trivy Results array is missing")

    vulnerabilities = 0
    fixable_high_critical = 0
    secrets = 0
    for result in results:
        if not isinstance(result, dict):
            raise EvidenceError(f"{path}: malformed Trivy result")
        findings = result.get("Vulnerabilities") or []
        secret_findings = result.get("Secrets") or []
        if not isinstance(findings, list) or not isinstance(secret_findings, list):
            raise EvidenceError(f"{path}: malformed finding arrays")
        vulnerabilities += len(findings)
        secrets += len(secret_findings)
        for finding in findings:
            if not isinstance(finding, dict):
                raise EvidenceError(f"{path}: malformed vulnerability")
            severity = str(finding.get("Severity", "")).upper()
            fixed_version = finding.get("FixedVersion")
            if severity in BLOCKING_SEVERITIES and isinstance(
                fixed_version, str
            ) and fixed_version.strip():
                fixable_high_critical += 1
    return {
        "vulnerabilities": vulnerabilities,
        "fixable_high_critical": fixable_high_critical,
        "secrets": secrets,
    }


def scanner_identity(path: Path, expected_release: str) -> dict[str, Any]:
    identity = _load_object(path)
    if identity.get("Version") != expected_release:
        raise EvidenceError(f"{path}: scanner version differs from release contract")
    database = identity.get("VulnerabilityDB")
    if not isinstance(database, dict):
        raise EvidenceError(f"{path}: vulnerability database identity is missing")
    for field in ("Version", "UpdatedAt", "DownloadedAt"):
        if database.get(field) in (None, ""):
            raise EvidenceError(f"{path}: vulnerability database {field} is missing")
    return database


def enforce_report(path: Path) -> dict[str, int]:
    summary = scan_summary(path)
    if summary["fixable_high_critical"]:
        raise EvidenceError(
            f"{path}: {summary['fixable_high_critical']} fixable HIGH/CRITICAL findings"
        )
    if summary["secrets"]:
        raise EvidenceError(f"{path}: {summary['secrets']} secret findings")
    return summary


def create_package_inventory(
    sbom_path: Path, package_names: list[str], output: Path
) -> None:
    sbom = _load_object(sbom_path)
    packages = sbom.get("packages")
    if not isinstance(packages, list):
        raise EvidenceError(f"{sbom_path}: SPDX packages array is missing")
    requested = set(package_names)
    installed: dict[str, set[str]] = {name: set() for name in requested}
    for package in packages:
        if not isinstance(package, dict):
            raise EvidenceError(f"{sbom_path}: malformed SPDX package")
        name = package.get("name")
        version = package.get("versionInfo")
        if name in requested and isinstance(version, str) and version:
            installed[name].add(version)
    missing = sorted(name for name, versions in installed.items() if not versions)
    if missing:
        raise EvidenceError(
            f"{sbom_path}: required packages missing exact versions: "
            f"{', '.join(missing)}"
        )
    output.write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/installed-package-inventory/v1",
                "source_sbom_sha256": _sha256(sbom_path),
                "packages": [
                    {"name": name, "versions": sorted(installed[name])}
                    for name in sorted(installed)
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def create_receipt(args: argparse.Namespace) -> None:
    if not GIT_OBJECT.fullmatch(args.source_commit):
        raise EvidenceError("source commit must be a full Git object id")
    if not GIT_OBJECT.fullmatch(args.source_tree):
        raise EvidenceError("source tree must be a full Git object id")
    actual_commit, actual_tree = _git_identity(args.repository.resolve())
    if (args.source_commit, args.source_tree) != (actual_commit, actual_tree):
        raise EvidenceError("source commit/tree differs from the clean checkout")
    if args.kind in {"image", "release-image"} and not DIGEST_REFERENCE.fullmatch(
        args.subject
    ):
        raise EvidenceError("image receipt subject must be an exact digest reference")
    if not HEX_SHA256.fullmatch(args.scanner_archive_sha256):
        raise EvidenceError("scanner archive SHA-256 is invalid")

    report = args.report.resolve()
    sbom = args.sbom.resolve()
    scanner_version = args.scanner_version.resolve()
    database_identity = scanner_identity(scanner_version, args.scanner_release)
    summary = scan_summary(report)
    artifacts: dict[str, dict[str, str]] = {
        "report": {"path": report.name, "sha256": _sha256(report)},
        "sbom": {"path": sbom.name, "sha256": _sha256(sbom)},
        "scanner_version": {
            "path": scanner_version.name,
            "sha256": _sha256(scanner_version),
        },
    }
    if args.package_inventory is not None:
        packages = args.package_inventory.resolve()
        artifacts["package_inventory"] = {
            "path": packages.name,
            "sha256": _sha256(packages),
        }

    provenance: dict[str, Any]
    if args.kind == "image":
        if args.build_metadata is None or args.oci_archive is None:
            raise EvidenceError(
                "first-party image receipt requires build metadata and OCI archive"
            )
        build_metadata = _load_object(args.build_metadata)
        built_digest = build_metadata.get("containerimage.digest")
        if not isinstance(built_digest, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", built_digest
        ):
            raise EvidenceError("build metadata has no exact containerimage.digest")
        if not args.subject.endswith(f"@{built_digest}"):
            raise EvidenceError("receipt subject differs from build output digest")
        descriptor = build_metadata.get("containerimage.descriptor")
        if not isinstance(descriptor, dict) or descriptor.get("digest") != built_digest:
            raise EvidenceError("build descriptor differs from output digest")
        build_provenance = build_metadata.get("buildx.build.provenance")
        if not isinstance(build_provenance, (dict, str)) or not build_provenance:
            raise EvidenceError("BuildKit provenance is missing")
        serialized_provenance = json.dumps(build_provenance, sort_keys=True)
        if (
            args.source_commit not in serialized_provenance
            or args.source_tree not in serialized_provenance
        ):
            raise EvidenceError("BuildKit provenance does not bind source commit/tree")
        artifacts["build_metadata"] = {
            "path": args.build_metadata.name,
            "sha256": _sha256(args.build_metadata),
        }
        artifacts["oci_archive"] = {
            "path": args.oci_archive.name,
            "sha256": _sha256(args.oci_archive),
        }
        provenance = {
            "kind": "local-oci-build",
            "manifest_digest": built_digest,
            "buildkit_provenance_sha256": hashlib.sha256(
                serialized_provenance.encode("utf-8")
            ).hexdigest(),
        }
    elif args.kind == "release-image":
        if args.release_closure is None or args.trust is None:
            raise EvidenceError(
                "release image receipt requires an attested release closure and trust"
            )
        closure = _load_object(args.release_closure)
        subject_provenance = closure.get("subject_provenance")
        if (
            closure.get("schema") != "fs2-serve.nebius.ai/release-image-closure/v2"
            or args.subject not in closure.get("subjects", [])
            or not isinstance(subject_provenance, dict)
            or not isinstance(subject_provenance.get(args.subject), dict)
        ):
            raise EvidenceError("release closure does not bind the scan subject")
        validate_attestation_identity(
            closure.get("attestation"), args.trust, purpose=str(args.release_closure)
        )
        closure_signature = Path(f"{args.release_closure}.sig")
        validate_detached_signature(
            args.release_closure, closure_signature, args.trust
        )
        artifacts["release_closure"] = {
            "path": args.release_closure.name,
            "sha256": _sha256(args.release_closure),
        }
        artifacts["release_closure_signature"] = {
            "path": closure_signature.name,
            "sha256": _sha256(closure_signature),
        }
        provenance = {
            "kind": "attested-release-closure",
            "subject": subject_provenance[args.subject],
        }
    else:
        provenance = {"kind": "git-filesystem"}

    attestation = None
    if args.attestation_identity is not None:
        attestation = _load_object(args.attestation_identity.resolve())

    receipt = {
        "schema": "fs2-serve.nebius.ai/image-scan-receipt/v2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {"commit": args.source_commit, "tree": args.source_tree},
        "subject": {"kind": args.kind, "identity": args.subject},
        "provenance": provenance,
        "scanner": {
            "name": "trivy",
            "version": args.scanner_release,
            "archive_sha256": args.scanner_archive_sha256,
            "vulnerability_database": database_identity,
        },
        "command_contract": args.command_contract,
        "artifacts": artifacts,
        "results": summary,
        "gate": {
            "block_fixable_severities": sorted(BLOCKING_SEVERITIES),
            "maximum_secret_findings": 0,
            "passed": not summary["fixable_high_critical"] and not summary["secrets"],
        },
    }
    if attestation is not None:
        receipt["attestation"] = attestation
    args.output.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def validate_receipt(path: Path, trust_path: Path | None = None) -> None:
    receipt = _load_object(path)
    if receipt.get("schema") != "fs2-serve.nebius.ai/image-scan-receipt/v2":
        raise EvidenceError(f"{path}: unsupported receipt schema")
    source = receipt.get("source")
    subject = receipt.get("subject")
    scanner = receipt.get("scanner")
    artifacts = receipt.get("artifacts")
    gate = receipt.get("gate")
    if not isinstance(source, dict) or not all(
        GIT_OBJECT.fullmatch(str(source.get(key, ""))) for key in ("commit", "tree")
    ):
        raise EvidenceError(f"{path}: invalid source binding")
    if not isinstance(subject, dict) or (
        subject.get("kind") in {"image", "release-image"}
        and not DIGEST_REFERENCE.fullmatch(str(subject.get("identity", "")))
    ):
        raise EvidenceError(f"{path}: image subject is not digest-bound")
    if not isinstance(scanner, dict) or not HEX_SHA256.fullmatch(
        str(scanner.get("archive_sha256", ""))
    ):
        raise EvidenceError(f"{path}: invalid scanner binding")
    if not isinstance(scanner.get("vulnerability_database"), dict):
        raise EvidenceError(f"{path}: vulnerability database binding is missing")
    if not isinstance(artifacts, dict) or not artifacts:
        raise EvidenceError(f"{path}: artifact hashes are missing")
    for name, artifact in artifacts.items():
        artifact_name = artifact.get("path") if isinstance(artifact, dict) else None
        expected_sha256 = artifact.get("sha256") if isinstance(artifact, dict) else None
        if (
            not isinstance(artifact_name, str)
            or not artifact_name
            or Path(artifact_name).name != artifact_name
            or not isinstance(expected_sha256, str)
            or not HEX_SHA256.fullmatch(expected_sha256)
        ):
            raise EvidenceError(f"{path}: invalid {name} artifact hash")
        artifact_path = path.parent / artifact_name
        if _sha256(artifact_path) != expected_sha256:
            raise EvidenceError(f"{path}: {name} artifact hash mismatch")
    if not isinstance(gate, dict) or gate.get("passed") is not True:
        raise EvidenceError(f"{path}: scan gate did not pass")
    provenance = receipt.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("kind") not in {
        "local-oci-build",
        "attested-release-closure",
        "git-filesystem",
    }:
        raise EvidenceError(f"{path}: build/resolution provenance is missing")
    if trust_path is None:
        raise EvidenceError(f"{path}: attestation trust policy is required")
    validate_attestation_identity(
        receipt.get("attestation"), trust_path, purpose=str(path)
    )
    validate_detached_signature(path, Path(f"{path}.sig"), trust_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    inventory = commands.add_parser("inventory")
    inventory.add_argument("path", type=Path)
    inventory.add_argument("--trust", type=Path)
    inventory.add_argument("--print-digest-references", action="store_true")

    report = commands.add_parser("report")
    report.add_argument("paths", nargs="+", type=Path)

    alpine_package = commands.add_parser("alpine-package")
    alpine_package.add_argument("path", type=Path)
    alpine_package.add_argument("--package", required=True)

    packages = commands.add_parser("package-inventory")
    packages.add_argument("--sbom", required=True, type=Path)
    packages.add_argument("--package", action="append", required=True)
    packages.add_argument("--output", required=True, type=Path)

    receipt = commands.add_parser("create-receipt")
    receipt.add_argument("--repository", required=True, type=Path)
    receipt.add_argument("--source-commit", required=True)
    receipt.add_argument("--source-tree", required=True)
    receipt.add_argument(
        "--kind", choices=("image", "release-image", "filesystem"), required=True
    )
    receipt.add_argument("--subject", required=True)
    receipt.add_argument("--scanner-release", required=True)
    receipt.add_argument("--scanner-archive-sha256", required=True)
    receipt.add_argument("--scanner-version", required=True, type=Path)
    receipt.add_argument("--command-contract", required=True)
    receipt.add_argument("--report", required=True, type=Path)
    receipt.add_argument("--sbom", required=True, type=Path)
    receipt.add_argument("--package-inventory", type=Path)
    receipt.add_argument("--build-metadata", type=Path)
    receipt.add_argument("--oci-archive", type=Path)
    receipt.add_argument("--release-closure", type=Path)
    receipt.add_argument("--trust", type=Path)
    receipt.add_argument("--attestation-identity", type=Path)
    receipt.add_argument("--output", required=True, type=Path)

    validate = commands.add_parser("receipt")
    validate.add_argument("paths", nargs="+", type=Path)
    validate.add_argument("--trust", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "inventory":
            images = validate_inventory(args.path, args.trust)
            if args.print_digest_references:
                for image in images:
                    print(image["digest_reference"])
        elif args.command == "report":
            for path in args.paths:
                enforce_report(path)
        elif args.command == "alpine-package":
            print(alpine_package_constraint(args.path, args.package))
        elif args.command == "package-inventory":
            create_package_inventory(args.sbom, args.package, args.output)
        elif args.command == "create-receipt":
            create_receipt(args)
        elif args.command == "receipt":
            for path in args.paths:
                validate_receipt(path, args.trust)
    except EvidenceError as exc:
        print(f"image security gate: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

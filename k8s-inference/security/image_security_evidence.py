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


def validate_inventory(path: Path) -> list[dict[str, Any]]:
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
        for field in (
            "registry",
            "manifest_media_type",
            "resolved_at",
            "resolver_identity",
        ):
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
        receipt_value = provenance.get("resolution_receipt_path")
        if (
            not isinstance(receipt_value, str)
            or not receipt_value
            or Path(receipt_value).is_absolute()
        ):
            raise EvidenceError(
                f"{path}: {identifier} resolution receipt path is invalid"
            )
        receipt_path = (path.parent / receipt_value).resolve()
        try:
            receipt_path.relative_to(path.parent.resolve())
        except ValueError as exc:
            raise EvidenceError(
                f"{path}: {identifier} resolution receipt escapes inventory root"
            ) from exc
        if _sha256(receipt_path) != provenance["resolution_receipt_sha256"]:
            raise EvidenceError(
                f"{path}: {identifier} resolution receipt hash mismatch"
            )
        validated.append(image)
    return validated


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
    if args.kind in {"image", "third-party-image"} and not DIGEST_REFERENCE.fullmatch(
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
    elif args.kind == "third-party-image":
        if args.resolution_receipt is None:
            raise EvidenceError(
                "third-party image receipt requires a resolution receipt"
            )
        artifacts["resolution_receipt"] = {
            "path": args.resolution_receipt.name,
            "sha256": _sha256(args.resolution_receipt),
        }
        provenance = {"kind": "registry-resolution"}
    else:
        provenance = {"kind": "git-filesystem"}

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
        subject.get("kind") in {"image", "third-party-image"}
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
        "registry-resolution",
        "git-filesystem",
    }:
        raise EvidenceError(f"{path}: build/resolution provenance is missing")
    if trust_path is None:
        raise EvidenceError(f"{path}: attestation trust policy is required")
    validate_detached_signature(path, Path(f"{path}.sig"), trust_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    inventory = commands.add_parser("inventory")
    inventory.add_argument("path", type=Path)
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
        "--kind", choices=("image", "third-party-image", "filesystem"), required=True
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
    receipt.add_argument("--resolution-receipt", type=Path)
    receipt.add_argument("--output", required=True, type=Path)

    validate = commands.add_parser("receipt")
    validate.add_argument("paths", nargs="+", type=Path)
    validate.add_argument("--trust", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "inventory":
            images = validate_inventory(args.path)
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

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
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DIGEST_REFERENCE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
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


def validate_inventory(path: Path) -> list[dict[str, Any]]:
    inventory = _load_object(path)
    if inventory.get("schema") != "fs2-serve.nebius.ai/third-party-image-lock/v1":
        raise EvidenceError(f"{path}: unsupported schema")
    if inventory.get("rendered_inventory_complete") is not True:
        raise EvidenceError(f"{path}: rendered inventory is not complete")
    if inventory.get("inventory_state") != "digest-pinned":
        raise EvidenceError(f"{path}: inventory_state must be digest-pinned")

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
        if not isinstance(digest_reference, str) or not DIGEST_REFERENCE.fullmatch(
            digest_reference
        ):
            raise EvidenceError(
                f"{path}: {identifier} is not bound to an exact sha256 digest"
            )
        if digest_reference in digest_references:
            raise EvidenceError(f"{path}: duplicate digest reference {digest_reference}")
        digest_references.add(digest_reference)
        if not isinstance(consumers, list) or not consumers or not all(
            isinstance(value, str) and value for value in consumers
        ):
            raise EvidenceError(f"{path}: {identifier} needs at least one consumer")
        validated.append(image)
    return validated


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
            f"{sbom_path}: required packages missing exact versions: {', '.join(missing)}"
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
    if args.kind == "image" and not DIGEST_REFERENCE.fullmatch(args.subject):
        raise EvidenceError("image receipt subject must be an exact digest reference")
    if not HEX_SHA256.fullmatch(args.scanner_archive_sha256):
        raise EvidenceError("scanner archive SHA-256 is invalid")

    report = args.report.resolve()
    sbom = args.sbom.resolve()
    scanner_version = args.scanner_version.resolve()
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

    receipt = {
        "schema": "fs2-serve.nebius.ai/image-scan-receipt/v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {"commit": args.source_commit, "tree": args.source_tree},
        "subject": {"kind": args.kind, "identity": args.subject},
        "scanner": {
            "name": "trivy",
            "version": args.scanner_release,
            "archive_sha256": args.scanner_archive_sha256,
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


def validate_receipt(path: Path) -> None:
    receipt = _load_object(path)
    if receipt.get("schema") != "fs2-serve.nebius.ai/image-scan-receipt/v1":
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
        subject.get("kind") == "image"
        and not DIGEST_REFERENCE.fullmatch(str(subject.get("identity", "")))
    ):
        raise EvidenceError(f"{path}: image subject is not digest-bound")
    if not isinstance(scanner, dict) or not HEX_SHA256.fullmatch(
        str(scanner.get("archive_sha256", ""))
    ):
        raise EvidenceError(f"{path}: invalid scanner binding")
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    inventory = commands.add_parser("inventory")
    inventory.add_argument("path", type=Path)
    inventory.add_argument("--print-digest-references", action="store_true")

    report = commands.add_parser("report")
    report.add_argument("paths", nargs="+", type=Path)

    packages = commands.add_parser("package-inventory")
    packages.add_argument("--sbom", required=True, type=Path)
    packages.add_argument("--package", action="append", required=True)
    packages.add_argument("--output", required=True, type=Path)

    receipt = commands.add_parser("create-receipt")
    receipt.add_argument("--source-commit", required=True)
    receipt.add_argument("--source-tree", required=True)
    receipt.add_argument("--kind", choices=("image", "filesystem"), required=True)
    receipt.add_argument("--subject", required=True)
    receipt.add_argument("--scanner-release", required=True)
    receipt.add_argument("--scanner-archive-sha256", required=True)
    receipt.add_argument("--scanner-version", required=True, type=Path)
    receipt.add_argument("--command-contract", required=True)
    receipt.add_argument("--report", required=True, type=Path)
    receipt.add_argument("--sbom", required=True, type=Path)
    receipt.add_argument("--package-inventory", type=Path)
    receipt.add_argument("--output", required=True, type=Path)

    validate = commands.add_parser("receipt")
    validate.add_argument("paths", nargs="+", type=Path)
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
        elif args.command == "package-inventory":
            create_package_inventory(args.sbom, args.package, args.output)
        elif args.command == "create-receipt":
            create_receipt(args)
        elif args.command == "receipt":
            for path in args.paths:
                validate_receipt(path)
    except EvidenceError as exc:
        print(f"image security gate: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

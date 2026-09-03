#!/usr/bin/env python3
"""Validate a published reference-data revision in place, read-only.

Checks the status document, the manifest and its digest, the mounted tree
path, the readiness marker and the aggregate identity, then derives the exact
terminal handoff receipt the bounded contract requires. It writes nothing, so
it is safe to run against a live shared filesystem while other workers hold
the bundle lock.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import stat
import sys
from urllib import parse

REFERENCE_DATA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REFERENCE_DATA))

import reference_data  # noqa: E402


def _check(findings: list[dict[str, object]], name: str, ok: bool, detail: object) -> bool:
    findings.append({"check": name, "ok": bool(ok), "detail": detail})
    return bool(ok)


def validate(root: Path, bundle_id: str, *, deep: bool, host_root: str) -> dict[str, object]:
    findings: list[dict[str, object]] = []
    report: dict[str, object] = {
        "schema": "fs2-serve.nebius.ai/reference-data-publication-validation/v1",
        "bundle_id": bundle_id,
        "root": str(root),
        "findings": findings,
    }

    status_path = root / "status" / f"{bundle_id}.json"
    if not _check(findings, "status document exists", status_path.is_file(), str(status_path)):
        report["published"] = False
        report["valid"] = False
        return report
    status = reference_data.load_json(status_path)
    report["published"] = status.get("ready") is True
    _check(findings, "status reports ready", status.get("ready") is True, status.get("ready"))
    report["status"] = status

    manifest_sha256 = str(status.get("manifest_sha256", ""))
    manifest_path = root / "manifests" / "sha256" / f"{manifest_sha256}.json"
    if not _check(findings, "manifest exists", manifest_path.is_file(), str(manifest_path)):
        report["valid"] = False
        return report
    manifest = reference_data.load_json(manifest_path)
    observed = reference_data.sha256_bytes(reference_data.canonical_json(manifest))
    _check(findings, "manifest digest equals its filename", observed == manifest_sha256,
           {"recorded": manifest_sha256, "computed": observed})
    content = manifest.get("content", {})
    storage = manifest.get("storage", {})
    report["manifest"] = {
        "sha256": manifest_sha256,
        "created_at": manifest.get("created_at"),
        "tree_sha256": content.get("tree_sha256"),
        "file_count": content.get("file_count"),
        "expanded_bytes": content.get("expanded_bytes"),
        "inventory_sha256": content.get("inventory_sha256"),
        "inline_inventory_entries": len(content["files"]) if isinstance(content.get("files"), list) else None,
        "source_objects": len(manifest.get("source_objects", [])),
    }

    legacy = "inventory_sha256" not in content or "dataset_sub_path" not in storage
    report["bounded_contract"] = not legacy
    _check(findings, "manifest already uses the bounded contract", not legacy,
           "legacy shape: run upgrade-publication" if legacy else "bounded")

    parsed = parse.urlparse(str(storage.get("shared_filesystem_uri", "")))
    published = Path(parse.unquote(parsed.path)) if parsed.scheme == "file" else None
    if not _check(findings, "published tree resolves", published is not None and published.is_dir(),
                  str(published)):
        report["valid"] = False
        return report

    tree_sha256 = str(content.get("tree_sha256", ""))
    _check(findings, "tree directory name binds the aggregate digest",
           published.name == tree_sha256, {"directory": published.name, "tree_sha256": tree_sha256})
    expected_sub_path = f"datasets/{bundle_id}/{status.get('revision')}/sha256/{tree_sha256}"
    actual_sub_path = published.relative_to(root).as_posix()
    _check(findings, "dataset sub-path is canonical", actual_sub_path == expected_sub_path,
           {"expected": expected_sub_path, "actual": actual_sub_path})

    marker = published / ".fs2-manifest-sha256"
    marker_value = marker.read_text(encoding="utf-8").strip() if marker.is_file() else None
    _check(findings, "readiness marker equals the manifest digest",
           marker_value == manifest_sha256, {"marker": marker_value, "manifest": manifest_sha256})

    mode = stat.S_IMODE(published.stat().st_mode)
    _check(findings, "published tree is read-only", not mode & 0o222, oct(mode))
    report["mount"] = {
        "host_root": host_root,
        "container_mount_path": "/reference-data",
        "dataset_sub_path": actual_sub_path,
        "host_path": f"{host_root.rstrip('/')}/{actual_sub_path}",
        "directory_mode": oct(mode),
        "marker": ".fs2-manifest-sha256",
    }

    summary = reference_data.tree_stat_summary(published)
    total_bytes = sum(int(entry["bytes"]) for entry in summary)
    _check(findings, "tree file count matches the manifest",
           len(summary) == content.get("file_count"),
           {"walked": len(summary), "manifest": content.get("file_count")})
    _check(findings, "tree byte total matches the manifest",
           total_bytes == content.get("expanded_bytes"),
           {"walked": total_bytes, "manifest": content.get("expanded_bytes")})

    if deep:
        _manifest, _digest = reference_data.verify_manifest(manifest_path, verify_tree=True)
        _check(findings, "every published file re-hashes to its recorded digest", True, "deep verify")

    receipt_path = root / "receipts" / bundle_id / f"{status.get('revision')}.json"
    if receipt_path.is_file():
        receipt = reference_data.validate_terminal_receipt(reference_data.load_json(receipt_path))
        _check(findings, "published receipt binds the published manifest",
               receipt["content"]["manifest_sha256"] == manifest_sha256, receipt_path.name)
        report["receipt"] = receipt
        report["receipt_source"] = "published"
    elif not legacy:
        _check(findings, "terminal receipt exists", False, str(receipt_path))
        report["receipt_source"] = "missing"
    else:
        # Derive, without writing, the receipt the bounded contract requires.
        report["receipt"] = reference_data.build_terminal_receipt(
            bundle_id=bundle_id,
            revision=str(status.get("revision")),
            tree_sha256=tree_sha256,
            manifest_sha256=manifest_sha256,
            inventory_sha256=reference_data.sha256_bytes(reference_data.canonical_json({
                "schema": reference_data.INVENTORY_SCHEMA,
                "bundle_id": bundle_id,
                "revision": str(status.get("revision")),
                "tree_sha256": tree_sha256,
                "expanded_bytes": content["expanded_bytes"],
                "file_count": content["file_count"],
                "files": content["files"],
            })) if isinstance(content.get("files"), list) else "0" * 64,
            file_count=int(content["file_count"]),
            expanded_bytes=int(content["expanded_bytes"]),
            created_at=str(manifest.get("created_at")),
            host_root=host_root,
        )
        report["receipt_source"] = "derived-not-written"

    report["valid"] = all(item["ok"] for item in findings)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--host-root", default=reference_data.CANONICAL_HOST_ROOT)
    parser.add_argument("--deep", action="store_true", help="re-hash every published file")
    arguments = parser.parse_args()
    report = validate(arguments.root, arguments.bundle, deep=arguments.deep,
                      host_root=arguments.host_root)
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())

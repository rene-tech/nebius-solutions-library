#!/usr/bin/env python3
"""Derive the release image closure from source and exact Helm render evidence.

The surface list cannot declare itself complete. This validator independently
discovers every Terraform Helm resource in the governed roots, requires the
post-render gate on each resource and direct installer, then requires one
hash-bound exact chart+values render for every declared release surface.
Catalog runtime images are read directly from their authoritative JSON inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

try:
    from .image_security_evidence import (
        DIGEST_REFERENCE,
        EvidenceError,
        validate_detached_signature,
        validate_inventory,
    )
except ImportError:
    from image_security_evidence import (
        DIGEST_REFERENCE,
        EvidenceError,
        validate_detached_signature,
        validate_inventory,
    )


HELM_RESOURCE = re.compile(r'resource\s+"helm_release"\s+"([^"]+)"\s*\{')
IMAGE_LINE = re.compile(r"^[ \t]*(?:-[ \t]*)?image:[ \t]*['\"]?([^'\"# \t]+)", re.M)
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _load(path: Path) -> dict[str, Any]:
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


def _resolve(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise EvidenceError(f"path escapes source root: {relative}") from exc
    return candidate


def _resource_block(source: str, start: int) -> str:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(source)):
        character = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise EvidenceError("unterminated helm_release resource")


def validate_source_surfaces(root: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = _load(manifest_path)
    if manifest.get("schema") != "fs2-serve.nebius.ai/release-image-surfaces/v1":
        raise EvidenceError(f"{manifest_path}: unsupported schema")
    discovered: set[str] = set()
    for path in sorted(root.rglob("*.tf")):
        if ".terraform" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        for match in HELM_RESOURCE.finditer(source):
            relative = path.relative_to(root).as_posix()
            identifier = f"{relative}::{match.group(1)}"
            discovered.add(identifier)
            block = _resource_block(source, match.end() - 1)
            for required in (
                "postrender",
                "helm_image_postrenderer.py",
                "third-party-images.lock.json",
            ):
                if required not in block:
                    raise EvidenceError(f"{identifier}: missing {required} consumer")
    expected = set(manifest.get("terraform_helm_releases", []))
    if discovered != expected:
        raise EvidenceError(
            "Terraform Helm surface mismatch: "
            f"missing={sorted(discovered - expected)} "
            f"stale={sorted(expected - discovered)}"
        )
    for relative in manifest.get("direct_installer_scripts", []):
        source = _resolve(root, relative).read_text(encoding="utf-8")
        for required in (
            "--post-renderer",
            "helm_image_postrenderer.py",
            "third-party-images.lock.json",
        ):
            if required not in source:
                raise EvidenceError(f"{relative}: missing {required} consumer")
    discovered_installers = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.sh")
        if "upgrade --install" in path.read_text(encoding="utf-8")
    }
    expected_installers = set(manifest.get("direct_installer_scripts", []))
    if discovered_installers != expected_installers:
        raise EvidenceError(
            "direct Helm installer mismatch: "
            f"missing={sorted(discovered_installers - expected_installers)} "
            f"stale={sorted(expected_installers - discovered_installers)}"
        )
    anchors = manifest.get("direct_surface_anchors")
    if not isinstance(anchors, list):
        raise EvidenceError(f"{manifest_path}: direct surface anchors are missing")
    anchor_ids = {
        item.get("id") for item in anchors if isinstance(item, dict)
    }
    if anchor_ids != set(manifest.get("direct_render_surfaces", [])):
        raise EvidenceError(f"{manifest_path}: direct surface IDs and anchors differ")
    for relative in manifest.get("direct_installer_scripts", []):
        source = _resolve(root, relative).read_text(encoding="utf-8")
        expected_anchors = [
            item["anchor"]
            for item in anchors
            if isinstance(item, dict) and item.get("script") == relative
        ]
        if relative.endswith("bootstrap/bootstrap.sh"):
            actual_count = sum(
                line.lstrip().startswith("install_chart ")
                for line in source.splitlines()
            )
        else:
            actual_count = sum(
                "upgrade --install" in line for line in source.splitlines()
            )
        if actual_count != len(expected_anchors):
            raise EvidenceError(
                f"{relative}: discovered {actual_count} install calls but "
                f"{len(expected_anchors)} render surfaces are declared"
            )
        for anchor in expected_anchors:
            if source.count(anchor) != 1:
                raise EvidenceError(
                    f"{relative}: install anchor is absent or ambiguous: {anchor}"
                )
    static_roots = [
        _resolve(root, relative)
        for relative in manifest.get("static_manifest_roots", [])
    ]
    for path in sorted(root.rglob("*.y*ml")):
        if not IMAGE_LINE.search(path.read_text(encoding="utf-8")):
            continue
        relative_parts = path.relative_to(root).parts
        rendered_input = relative_parts[0] == "charts" or "values" in relative_parts
        static_input = any(
            path.is_relative_to(static_root) for static_root in static_roots
        )
        if not rendered_input and not static_input:
            raise EvidenceError(
                f"{path.relative_to(root)}: image-bearing manifest has no closure owner"
            )
    return manifest


def _git_identity(root: Path) -> tuple[str, str]:
    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise EvidenceError(f"git {' '.join(arguments)} failed")
        return completed.stdout.strip()

    return git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")


def _artifact(packet_dir: Path, value: Any, label: str) -> Path:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label}: artifact binding is missing")
    relative = value.get("path")
    expected = value.get("sha256")
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise EvidenceError(f"{label}: path must be relative")
    if not isinstance(expected, str) or not HEX_SHA256.fullmatch(expected):
        raise EvidenceError(f"{label}: invalid SHA-256")
    path = _resolve(packet_dir, relative)
    if _sha256(path) != expected:
        raise EvidenceError(f"{label}: SHA-256 mismatch")
    return path


def _catalog_files(root: Path, sources: list[str]) -> Iterator[Path]:
    for relative in sources:
        path = _resolve(root, relative)
        if path.is_dir():
            yield from sorted(path.glob("*.json"))
        else:
            yield path


def _catalog_images(path: Path) -> set[str]:
    document = _load(path)
    images: set[str] = set()

    def visit(value: Any, location: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_location = f"{location}.{key}"
                if key == "image":
                    if isinstance(child, str):
                        if not DIGEST_REFERENCE.fullmatch(child):
                            raise EvidenceError(
                                f"{path}:{child_location}: image is not digest-bound"
                            )
                        images.add(child)
                    elif isinstance(child, dict) and "reference" in child:
                        reference = child.get("reference")
                        if not isinstance(
                            reference, str
                        ) or not DIGEST_REFERENCE.fullmatch(reference):
                            raise EvidenceError(
                                f"{path}:{child_location}: image reference is not "
                                "digest-bound"
                            )
                        images.add(reference)
                visit(child, child_location)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{location}[{index}]")

    visit(document, "$")
    return images


def _planned_images(path: Path) -> set[str]:
    plan = _load(path)
    images: set[str] = set()

    def accept(reference: Any, location: str) -> None:
        if reference is None:
            return
        if not isinstance(reference, str) or not DIGEST_REFERENCE.fullmatch(reference):
            raise EvidenceError(
                f"{path}:{location}: planned image is not digest-bound"
            )
        images.add(reference)

    def visit(value: Any, location: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_location = f"{location}.{key}"
                normalized = key.lower().replace("-", "_")
                if normalized in {"image", "image_ref", "image_reference"}:
                    if isinstance(child, dict):
                        if "reference" in child:
                            accept(child.get("reference"), child_location)
                        elif "repository" in child and "digest" in child:
                            repository = child.get("repository")
                            digest = child.get("digest")
                            accept(
                                f"{repository}@{digest}"
                                if isinstance(repository, str)
                                and isinstance(digest, str)
                                else None,
                                child_location,
                            )
                    else:
                        accept(child, child_location)
                visit(child, child_location)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{location}[{index}]")

    visit(plan, "$")
    if not images:
        raise EvidenceError(f"{path}: Terraform plan contains no resolved images")
    return images


def _static_manifest_images(root: Path, roots: list[str]) -> set[str]:
    images: set[str] = set()
    for relative in roots:
        scan_root = _resolve(root, relative)
        paths = (
            [scan_root]
            if scan_root.is_file()
            else sorted(scan_root.rglob("*.y*ml"))
        )
        for path in paths:
            references = set(IMAGE_LINE.findall(path.read_text(encoding="utf-8")))
            invalid = sorted(
                ref for ref in references if not DIGEST_REFERENCE.fullmatch(ref)
            )
            if invalid:
                raise EvidenceError(
                    f"{path}: non-digest static manifest images: {invalid}"
                )
            images.update(references)
    return images


def derive_closure(
    root: Path,
    manifest_path: Path,
    packet_path: Path,
    trust_path: Path,
    inventory_path: Path,
) -> dict[str, Any]:
    manifest = validate_source_surfaces(root, manifest_path)
    packet = _load(packet_path)
    if packet.get("schema") != "fs2-serve.nebius.ai/release-render-packet/v1":
        raise EvidenceError(f"{packet_path}: unsupported schema")
    if packet.get("status") != "attested-exact-render":
        raise EvidenceError(f"{packet_path}: exact render packet is not accepted")
    attestation = packet.get("attestation")
    if not isinstance(attestation, dict):
        raise EvidenceError(f"{packet_path}: render attestation is missing")
    signature_value = attestation.get("signature_path")
    if not isinstance(signature_value, str) or not signature_value:
        raise EvidenceError(f"{packet_path}: render signature path is missing")
    if attestation.get("trust_policy_sha256") != _sha256(trust_path):
        raise EvidenceError(f"{packet_path}: trust policy hash mismatch")
    signature = _resolve(packet_path.parent.resolve(), signature_value)
    validate_detached_signature(packet_path, signature, trust_path)
    commit, tree = _git_identity(root)
    if packet.get("source") != {"commit": commit, "tree": tree}:
        raise EvidenceError(f"{packet_path}: source identity differs from checkout")
    if packet.get("surface_manifest_sha256") != _sha256(manifest_path):
        raise EvidenceError(f"{packet_path}: surface manifest hash mismatch")
    if packet.get("inventory_sha256") != _sha256(inventory_path):
        raise EvidenceError(f"{packet_path}: image inventory hash mismatch")

    expected_surfaces = set(manifest["terraform_helm_releases"]) | set(
        manifest["direct_render_surfaces"]
    )
    renders = packet.get("renders")
    if not isinstance(renders, list):
        raise EvidenceError(f"{packet_path}: renders must be an array")
    actual_surfaces: set[str] = set()
    subjects: set[str] = set()
    rendered_subjects: set[str] = set()
    packet_dir = packet_path.parent.resolve()
    terraform_plan = _artifact(
        packet_dir, packet.get("terraform_plan"), "exact Terraform plan JSON"
    )
    subjects.update(_planned_images(terraform_plan))
    for render in renders:
        if not isinstance(render, dict) or not isinstance(
            render.get("surface_id"), str
        ):
            raise EvidenceError(f"{packet_path}: malformed render entry")
        surface_id = render["surface_id"]
        if surface_id in actual_surfaces:
            raise EvidenceError(f"{packet_path}: duplicate render {surface_id}")
        actual_surfaces.add(surface_id)
        chart = render.get("chart")
        if not isinstance(chart, dict):
            raise EvidenceError(f"{surface_id}: chart provenance is missing")
        for field in ("reference", "version", "repository"):
            if not isinstance(chart.get(field), str) or not chart[field]:
                raise EvidenceError(f"{surface_id}: chart {field} is missing")
        _artifact(packet_dir, chart.get("artifact"), f"{surface_id} chart")
        values = render.get("values")
        if not isinstance(values, list):
            raise EvidenceError(f"{surface_id}: values provenance is missing")
        for index, binding in enumerate(values):
            _artifact(packet_dir, binding, f"{surface_id} values[{index}]")
        rendered = _artifact(
            packet_dir, render.get("rendered_manifest"), f"{surface_id} render"
        )
        references = set(IMAGE_LINE.findall(rendered.read_text(encoding="utf-8")))
        if not references:
            raise EvidenceError(f"{surface_id}: render contains no images")
        invalid = sorted(
            ref for ref in references if not DIGEST_REFERENCE.fullmatch(ref)
        )
        if invalid:
            raise EvidenceError(f"{surface_id}: non-digest images: {invalid}")
        rendered_subjects.update(references)
    if actual_surfaces != expected_surfaces:
        raise EvidenceError(
            "exact render closure mismatch: "
            f"missing={sorted(expected_surfaces - actual_surfaces)} "
            f"unexpected={sorted(actual_surfaces - expected_surfaces)}"
        )
    accepted_render_digests = {
        image["digest_reference"] for image in validate_inventory(inventory_path)
    }
    if rendered_subjects != accepted_render_digests:
        raise EvidenceError(
            "render/inventory closure mismatch: "
            f"unaccepted={sorted(rendered_subjects - accepted_render_digests)} "
            f"unconsumed={sorted(accepted_render_digests - rendered_subjects)}"
        )
    subjects.update(rendered_subjects)
    for path in _catalog_files(root, manifest["catalog_image_sources"]):
        subjects.update(_catalog_images(path))
    subjects.update(
        _static_manifest_images(root, manifest.get("static_manifest_roots", []))
    )
    return {
        "schema": "fs2-serve.nebius.ai/release-image-closure/v1",
        "source": {"commit": commit, "tree": tree},
        "surface_manifest_sha256": _sha256(manifest_path),
        "inventory_sha256": _sha256(inventory_path),
        "render_packet_sha256": _sha256(packet_path),
        "render_packet_signature_sha256": _sha256(signature),
        "terraform_plan_sha256": _sha256(terraform_plan),
        "subjects": sorted(subjects),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--surfaces", required=True, type=Path)
    parser.add_argument("--render-packet", type=Path)
    parser.add_argument("--trust", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        root = args.root.resolve()
        if args.render_packet is None:
            validate_source_surfaces(root, args.surfaces.resolve())
        else:
            if (
                args.output is None
                or args.trust is None
                or args.inventory is None
            ):
                raise EvidenceError(
                    "--output, --trust, and --inventory are required with "
                    "--render-packet"
                )
            closure = derive_closure(
                root,
                args.surfaces.resolve(),
                args.render_packet.resolve(),
                args.trust.resolve(),
                args.inventory.resolve(),
            )
            args.output.write_text(
                json.dumps(closure, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    except EvidenceError as exc:
        print(f"release image closure gate: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
import tarfile
from pathlib import Path
from typing import Any, Iterator

try:
    from .image_security_evidence import (
        DIGEST_REFERENCE,
        EvidenceError,
        validate_attestation_identity,
        validate_catalog_image_map,
        validate_detached_signature,
        validate_first_party_inventory,
        validate_inventory,
    )
except ImportError:
    from image_security_evidence import (
        DIGEST_REFERENCE,
        EvidenceError,
        validate_attestation_identity,
        validate_catalog_image_map,
        validate_detached_signature,
        validate_first_party_inventory,
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
                "first-party-images.lock.json",
                "image-attestation-trust.json",
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
            "first-party-images.lock.json",
            "image-attestation-trust.json",
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


def _release_declaration_hashes(
    root: Path, manifest: dict[str, Any]
) -> dict[str, str]:
    declarations: dict[str, str] = {}
    for path in sorted(root.rglob("*.tf")):
        if ".terraform" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        for match in HELM_RESOURCE.finditer(source):
            identifier = f"{path.relative_to(root).as_posix()}::{match.group(1)}"
            block = _resource_block(source, match.end() - 1)
            declarations[identifier] = hashlib.sha256(
                block.encode("utf-8")
            ).hexdigest()
    for anchor in manifest["direct_surface_anchors"]:
        script = _resolve(root, anchor["script"])
        source = script.read_bytes()
        declarations[anchor["id"]] = hashlib.sha256(
            source + b"\0" + anchor["anchor"].encode("utf-8")
        ).hexdigest()
    return declarations


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


def _chart_metadata(chart_path: Path, label: str) -> dict[str, str]:
    if not chart_path.is_file():
        raise EvidenceError(f"{label}: chart artifact must be one exact archive")
    try:
        with tarfile.open(chart_path, mode="r:*") as archive:
            members = [
                member
                for member in archive.getmembers()
                if member.name.endswith("/Chart.yaml")
                and member.isfile()
                and not member.issym()
                and not member.islnk()
            ]
            if len(members) != 1 or members[0].size > 1024 * 1024:
                raise EvidenceError(f"{label}: chart archive has ambiguous metadata")
            stream = archive.extractfile(members[0])
            if stream is None:
                raise EvidenceError(f"{label}: Chart.yaml cannot be read")
            source = stream.read().decode("utf-8")
    except (OSError, tarfile.TarError, UnicodeDecodeError) as exc:
        raise EvidenceError(f"{label}: invalid chart archive: {exc}") from exc
    metadata: dict[str, str] = {}
    for line in source.splitlines():
        match = re.match(r"^(name|version):\s*['\"]?([^'\"#\s]+)", line)
        if match:
            metadata[match.group(1)] = match.group(2)
    if set(metadata) != {"name", "version"}:
        raise EvidenceError(f"{label}: Chart.yaml name/version are missing")
    return metadata


def _validate_render_provenance(
    provenance_path: Path,
    *,
    surface_id: str,
    source: dict[str, str],
    chart: dict[str, Any],
    chart_sha256: str,
    values_sha256: list[str],
    rendered_sha256: str,
    release_declaration_sha256: str,
    post_renderer_sha256: str,
    inventory_sha256: str,
    first_party_inventory_sha256: str,
    trust_policy_sha256: str,
    trust_path: Path,
) -> dict[str, Any]:
    provenance = _load(provenance_path)
    if provenance.get("schema") != "fs2-serve.nebius.ai/helm-render-provenance/v1":
        raise EvidenceError(f"{provenance_path}: unsupported render provenance schema")
    if provenance.get("surface_id") != surface_id or provenance.get("source") != source:
        raise EvidenceError(f"{provenance_path}: render subject differs from packet")
    builder = provenance.get("builder")
    trust = _load(trust_path)
    render_policy = trust.get("render_provenance")
    allowed_builders = (
        render_policy.get("authorized_builder_ids")
        if isinstance(render_policy, dict)
        else None
    )
    if (
        not isinstance(builder, dict)
        or not isinstance(allowed_builders, list)
        or builder.get("id") not in allowed_builders
        or not isinstance(builder.get("helm_version"), str)
        or not builder["helm_version"]
        or not isinstance(builder.get("helm_binary_sha256"), str)
        or not HEX_SHA256.fullmatch(builder["helm_binary_sha256"])
    ):
        raise EvidenceError(f"{provenance_path}: render builder is not authorized")
    invocation = provenance.get("invocation")
    expected_invocation = {
        "tool": "helm",
        "action": "template",
        "release_name": chart["release_name"],
        "namespace": chart["namespace"],
        "chart_sha256": chart_sha256,
        "release_declaration_sha256": release_declaration_sha256,
        "ordered_values_sha256": values_sha256,
        "post_renderer": {
            "script_sha256": post_renderer_sha256,
            "third_party_inventory_sha256": inventory_sha256,
            "first_party_inventory_sha256": first_party_inventory_sha256,
            "trust_policy_sha256": trust_policy_sha256,
        },
        "exit_code": 0,
    }
    if invocation != expected_invocation:
        raise EvidenceError(
            f"{provenance_path}: invocation does not bind exact chart, values, and gate"
        )
    subject = provenance.get("subject")
    if subject != {
        "name": "rendered-manifest.yaml",
        "sha256": rendered_sha256,
    }:
        raise EvidenceError(f"{provenance_path}: render output hash differs")
    validate_attestation_identity(
        provenance.get("attestation"), trust_path, purpose=str(provenance_path)
    )
    return provenance


def _catalog_files(root: Path, sources: list[str]) -> Iterator[Path]:
    for relative in sources:
        path = _resolve(root, relative)
        if path.is_dir():
            yield from sorted(path.rglob("*.json"))
        else:
            yield path


def _production_reference(
    reference: str,
    mappings: dict[str, str],
    placeholder_registries: set[str],
    used_mappings: set[str],
    label: str,
) -> str:
    registry = reference.split("/", 1)[0]
    if registry in placeholder_registries:
        mapped = mappings.get(reference)
        if mapped is None:
            raise EvidenceError(f"{label}: placeholder image has no production mapping")
        used_mappings.add(reference)
        return mapped
    return reference


def _catalog_images(
    path: Path,
    mappings: dict[str, str],
    placeholder_registries: set[str],
    used_mappings: set[str],
) -> set[str]:
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
                        images.add(
                            _production_reference(
                                child,
                                mappings,
                                placeholder_registries,
                                used_mappings,
                                f"{path}:{child_location}",
                            )
                        )
                    elif isinstance(child, dict) and "reference" in child:
                        reference = child.get("reference")
                        if not isinstance(
                            reference, str
                        ) or not DIGEST_REFERENCE.fullmatch(reference):
                            raise EvidenceError(
                                f"{path}:{child_location}: image reference is not "
                                "digest-bound"
                            )
                        images.add(
                            _production_reference(
                                reference,
                                mappings,
                                placeholder_registries,
                                used_mappings,
                                f"{path}:{child_location}",
                            )
                        )
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


def _static_manifest_images(
    root: Path,
    roots: list[str],
    mappings: dict[str, str],
    placeholder_registries: set[str],
    used_mappings: set[str],
) -> set[str]:
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
            images.update(
                _production_reference(
                    ref, mappings, placeholder_registries, used_mappings, str(path)
                )
                for ref in references
            )
    return images


def derive_closure(
    root: Path,
    manifest_path: Path,
    packet_path: Path,
    trust_path: Path,
    inventory_path: Path,
    first_party_inventory_path: Path,
    catalog_image_map_path: Path,
    attestation_identity_path: Path,
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
    validate_attestation_identity(
        attestation.get("identity"), trust_path, purpose=str(packet_path)
    )
    signature = _resolve(packet_path.parent.resolve(), signature_value)
    validate_detached_signature(packet_path, signature, trust_path)
    commit, tree = _git_identity(root)
    if packet.get("source") != {"commit": commit, "tree": tree}:
        raise EvidenceError(f"{packet_path}: source identity differs from checkout")
    if packet.get("surface_manifest_sha256") != _sha256(manifest_path):
        raise EvidenceError(f"{packet_path}: surface manifest hash mismatch")
    if packet.get("inventory_sha256") != _sha256(inventory_path):
        raise EvidenceError(f"{packet_path}: image inventory hash mismatch")
    if packet.get("first_party_inventory_sha256") != _sha256(
        first_party_inventory_path
    ):
        raise EvidenceError(f"{packet_path}: first-party inventory hash mismatch")
    if packet.get("catalog_image_map_sha256") != _sha256(catalog_image_map_path):
        raise EvidenceError(f"{packet_path}: catalog image map hash mismatch")

    third_party_images = validate_inventory(inventory_path, trust_path)
    first_party_images = validate_first_party_inventory(
        first_party_inventory_path, trust_path
    )
    catalog_mappings = validate_catalog_image_map(catalog_image_map_path, trust_path)
    catalog_map = _load(catalog_image_map_path)
    placeholder_registries = set(catalog_map["placeholder_registries"])
    used_catalog_mappings: set[str] = set()
    closure_attestation = _load(attestation_identity_path)
    validate_attestation_identity(
        closure_attestation, trust_path, purpose="release image closure"
    )

    expected_surfaces = set(manifest["terraform_helm_releases"]) | set(
        manifest["direct_render_surfaces"]
    )
    declaration_hashes = _release_declaration_hashes(root, manifest)
    if set(declaration_hashes) != expected_surfaces:
        raise EvidenceError("release declaration hash closure differs from surfaces")
    renders = packet.get("renders")
    if not isinstance(renders, list):
        raise EvidenceError(f"{packet_path}: renders must be an array")
    actual_surfaces: set[str] = set()
    subjects: set[str] = set()
    rendered_subjects: set[str] = set()
    subject_provenance: dict[str, dict[str, Any]] = {}
    packet_dir = packet_path.parent.resolve()
    terraform_plan = _artifact(
        packet_dir, packet.get("terraform_plan"), "exact Terraform plan JSON"
    )
    planned_subjects = _planned_images(terraform_plan)
    placeholder_plans = sorted(
        reference
        for reference in planned_subjects
        if reference.split("/", 1)[0] in placeholder_registries
    )
    if placeholder_plans:
        raise EvidenceError(
            f"{terraform_plan}: production plan contains placeholder images: {placeholder_plans}"
        )
    subjects.update(planned_subjects)
    for reference in planned_subjects:
        subject_provenance[reference] = {
            "kind": "terraform-production-plan",
            "terraform_plan_sha256": _sha256(terraform_plan),
        }
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
        for field in (
            "name",
            "version",
            "repository",
            "release_name",
            "namespace",
            "digest",
        ):
            if not isinstance(chart.get(field), str) or not chart[field]:
                raise EvidenceError(f"{surface_id}: chart {field} is missing")
        chart_path = _artifact(
            packet_dir, chart.get("artifact"), f"{surface_id} chart"
        )
        declaration_sha256 = render.get("release_declaration_sha256")
        if declaration_sha256 != declaration_hashes[surface_id]:
            raise EvidenceError(
                f"{surface_id}: render is not bound to the declared release"
            )
        chart_sha256 = _sha256(chart_path)
        if chart["digest"] != f"sha256:{chart_sha256}":
            raise EvidenceError(f"{surface_id}: chart digest differs from archive")
        metadata = _chart_metadata(chart_path, f"{surface_id} chart")
        if metadata != {"name": chart["name"], "version": chart["version"]}:
            raise EvidenceError(f"{surface_id}: chart metadata differs from declaration")
        values = render.get("values")
        if not isinstance(values, list):
            raise EvidenceError(f"{surface_id}: values provenance is missing")
        value_hashes: list[str] = []
        for index, binding in enumerate(values):
            value_hashes.append(
                _sha256(
                    _artifact(packet_dir, binding, f"{surface_id} values[{index}]")
                )
            )
        rendered = _artifact(
            packet_dir, render.get("rendered_manifest"), f"{surface_id} render"
        )
        provenance = _artifact(
            packet_dir, render.get("render_provenance"), f"{surface_id} provenance"
        )
        _validate_render_provenance(
            provenance,
            surface_id=surface_id,
            source={"commit": commit, "tree": tree},
            chart=chart,
            chart_sha256=chart_sha256,
            values_sha256=value_hashes,
            rendered_sha256=_sha256(rendered),
            release_declaration_sha256=declaration_sha256,
            post_renderer_sha256=_sha256(root / "security/helm_image_postrenderer.py"),
            inventory_sha256=_sha256(inventory_path),
            first_party_inventory_sha256=_sha256(first_party_inventory_path),
            trust_policy_sha256=_sha256(trust_path),
            trust_path=trust_path,
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
        for reference in references:
            provenance_entry = subject_provenance.setdefault(
                reference, {"kind": "helm-render"}
            )
            provenance_entry.setdefault("surfaces", []).append(surface_id)
    if actual_surfaces != expected_surfaces:
        raise EvidenceError(
            "exact render closure mismatch: "
            f"missing={sorted(expected_surfaces - actual_surfaces)} "
            f"unexpected={sorted(actual_surfaces - expected_surfaces)}"
        )
    accepted_render_digests = {
        image["digest_reference"] for image in third_party_images + first_party_images
    }
    if rendered_subjects != accepted_render_digests:
        raise EvidenceError(
            "render/inventory closure mismatch: "
            f"unaccepted={sorted(rendered_subjects - accepted_render_digests)} "
            f"unconsumed={sorted(accepted_render_digests - rendered_subjects)}"
        )
    subjects.update(rendered_subjects)
    declared_builds = manifest.get("first_party_builds")
    if not isinstance(declared_builds, list) or not all(
        isinstance(item, dict) for item in declared_builds
    ):
        raise EvidenceError(f"{manifest_path}: first-party build declarations are invalid")
    builds_by_id = {item.get("id"): item for item in declared_builds}
    images_by_id = {item["id"]: item for item in first_party_images}
    if set(builds_by_id) != set(images_by_id):
        raise EvidenceError("first-party build declarations and accepted lock differ")
    for identifier, build in builds_by_id.items():
        image = images_by_id[identifier]
        if build.get("dockerfile") != image.get("dockerfile"):
            raise EvidenceError(f"first-party {identifier}: Dockerfile binding differs")
        dockerfile = _resolve(root, build["dockerfile"])
        if not dockerfile.is_file():
            raise EvidenceError(f"first-party {identifier}: Dockerfile is missing")
        expected_surfaces_for_build = set(build.get("render_surfaces", []))
        if expected_surfaces_for_build != set(image.get("render_surfaces", [])):
            raise EvidenceError(f"first-party {identifier}: render surface binding differs")
        digest_reference = image["digest_reference"]
        actual_surfaces_for_build = set(
            subject_provenance.get(digest_reference, {}).get("surfaces", [])
        )
        if not expected_surfaces_for_build.issubset(actual_surfaces_for_build):
            raise EvidenceError(
                f"first-party {identifier}: production render does not consume accepted digest"
            )
        subject_provenance[digest_reference] = {
            "kind": "first-party-build-and-render",
            "build_id": identifier,
            "dockerfile_sha256": _sha256(dockerfile),
            "build_receipt_sha256": image["build_receipt"]["sha256"],
            "surfaces": sorted(actual_surfaces_for_build),
        }
    for path in _catalog_files(root, manifest["catalog_image_sources"]):
        for reference in _catalog_images(
            path, catalog_mappings, placeholder_registries, used_catalog_mappings
        ):
            subjects.add(reference)
            provenance_entry = subject_provenance.setdefault(
                reference, {"kind": "catalog-production-mapping"}
            )
            provenance_entry.setdefault("sources", []).append(
                path.relative_to(root).as_posix()
            )
    for reference in _static_manifest_images(
        root,
        manifest.get("static_manifest_roots", []),
        catalog_mappings,
        placeholder_registries,
        used_catalog_mappings,
    ):
        subjects.add(reference)
        subject_provenance.setdefault(
            reference, {"kind": "static-manifest-production-mapping"}
        )
    if used_catalog_mappings != set(catalog_mappings):
        raise EvidenceError(
            "catalog production mapping closure mismatch: "
            f"missing={sorted(used_catalog_mappings - set(catalog_mappings))} "
            f"stale={sorted(set(catalog_mappings) - used_catalog_mappings)}"
        )
    third_party_by_reference = {
        image["digest_reference"]: image for image in third_party_images
    }
    for reference, image in third_party_by_reference.items():
        entry = subject_provenance.setdefault(
            reference, {"kind": "third-party-registry-resolution", "surfaces": []}
        )
        entry["resolution_receipt_sha256"] = image["resolution_provenance"][
            "resolution_receipt_sha256"
        ]
        entry["resolver_identity"] = image["resolution_provenance"][
            "resolver_identity"
        ]
    if set(subject_provenance) != subjects:
        raise EvidenceError(
            "release subjects lack exact provenance: "
            f"missing={sorted(subjects - set(subject_provenance))} "
            f"stale={sorted(set(subject_provenance) - subjects)}"
        )
    return {
        "schema": "fs2-serve.nebius.ai/release-image-closure/v2",
        "source": {"commit": commit, "tree": tree},
        "attestation": closure_attestation,
        "surface_manifest_sha256": _sha256(manifest_path),
        "inventory_sha256": _sha256(inventory_path),
        "first_party_inventory_sha256": _sha256(first_party_inventory_path),
        "catalog_image_map_sha256": _sha256(catalog_image_map_path),
        "render_packet_sha256": _sha256(packet_path),
        "render_packet_signature_sha256": _sha256(signature),
        "terraform_plan_sha256": _sha256(terraform_plan),
        "subjects": sorted(subjects),
        "subject_provenance": {
            reference: subject_provenance[reference] for reference in sorted(subjects)
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--surfaces", required=True, type=Path)
    parser.add_argument("--render-packet", type=Path)
    parser.add_argument("--trust", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--first-party-inventory", type=Path)
    parser.add_argument("--catalog-image-map", type=Path)
    parser.add_argument("--attestation-identity", type=Path)
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
                or args.first_party_inventory is None
                or args.catalog_image_map is None
                or args.attestation_identity is None
            ):
                raise EvidenceError(
                    "--output, --trust, --inventory, --first-party-inventory, "
                    "--catalog-image-map, and --attestation-identity are required "
                    "with --render-packet"
                )
            closure = derive_closure(
                root,
                args.surfaces.resolve(),
                args.render_packet.resolve(),
                args.trust.resolve(),
                args.inventory.resolve(),
                args.first_party_inventory.resolve(),
                args.catalog_image_map.resolve(),
                args.attestation_identity.resolve(),
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

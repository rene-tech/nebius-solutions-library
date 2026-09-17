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
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Iterator

if __name__ == "__main__" or __package__ != "security":
    print(
        "release closure verification is an import-only external-capsule payload",
        file=sys.stderr,
    )
    raise SystemExit(1)

from .execution_toolchain import (  # noqa: E402
    ToolchainError,
    environment_toolchain,
    open_verified_file,
    validate_current_python,
    validated_tool,
)
from .image_security_evidence import (  # noqa: E402
    DIGEST_REFERENCE,
    EvidenceError,
    validate_attestation_identity,
    validate_catalog_image_map,
    validate_detached_signature,
    validate_first_party_inventory,
    validate_image_gate_authorization,
    validate_inventory,
)
from .semantic_yaml_images import (  # noqa: E402
    SemanticYamlError,
    independently_validated_image_scalars,
)
from .yaml_image_references import YamlImageError, image_key_lines  # noqa: E402


HELM_RESOURCE = re.compile(r'resource\s+"helm_release"\s+"([^"]+)"\s*\{')
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
HELM_INSTALL = re.compile(
    r"(?:^|\s)(?:helm|hctl|\"?\$\{reviewed_helm\[@\]\}\"?)\s+"
    r"(?:install(?:\s|$)|upgrade(?:\s+--install)?(?:\s|$))"
)
HELM_ARRAY_ALIAS = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)=\(\s*helm(?:\s|\))")
DOCUMENTED_HELM_INSTALL = re.compile(
    r"^\s*(?:\$\s*)?(?:helm|hctl)\s+(?:install|upgrade)(?:\s|$)", re.MULTILINE
)
TEXT_ENTRYPOINT_SUFFIXES = {
    ".sh", ".bash", ".yml", ".yaml", ".md", ".py", ".js", ".ts", ".tsx"
}
PROGRAMMATIC_HELM_INSTALL = re.compile(
    r"['\"]helm['\"]\s*,\s*['\"](?:install|upgrade)['\"]|"
    r"\bhelm\.(?:install|upgrade)\b"
)
IGNORED_SOURCE_PARTS = {".git", ".terraform", "node_modules", "vendor"}


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


def _logical_shell_commands(source: str) -> list[str]:
    commands: list[str] = []
    pending = ""
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        continued = stripped.endswith("\\")
        part = stripped[:-1].rstrip() if continued else stripped
        pending = f"{pending} {part}".strip()
        if not continued:
            commands.append(pending)
            pending = ""
    if pending:
        raise EvidenceError("shell installer ends with an unterminated continuation")
    return commands


def _source_text_entrypoints(root: Path) -> dict[str, str]:
    """Read every potential shell, workflow, Makefile, and operator-doc entrypoint."""

    result: dict[str, str] = {}
    repository_workflows = root.parent / ".github/workflows"
    candidates = list(root.rglob("*"))
    if repository_workflows.is_dir():
        candidates.extend(repository_workflows.rglob("*"))
    for path in sorted(candidates):
        if not path.is_file() or any(part in IGNORED_SOURCE_PARTS for part in path.parts):
            continue
        try:
            with path.open("rb") as stream:
                first_line = stream.readline(256)
        except OSError as exc:
            raise EvidenceError(f"{path}: cannot inspect governed entrypoint") from exc
        shell_shebang = first_line.startswith(b"#!") and any(
            shell in first_line for shell in (b"/sh", b"/bash", b"/dash", b"/ksh")
        )
        if (
            path.suffix not in TEXT_ENTRYPOINT_SUFFIXES
            and path.name != "Makefile"
            and not shell_shebang
        ):
            continue
        try:
            if path.is_relative_to(root):
                relative = path.relative_to(root).as_posix()
            else:
                relative = (
                    "repository/.github/workflows/"
                    + path.relative_to(repository_workflows).as_posix()
                )
            result[relative] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise EvidenceError(f"{path}: governed entrypoint is not UTF-8") from exc
    return result


def _script_helm_installs(source: str) -> list[str]:
    commands = _logical_shell_commands(source)
    aliases = {
        match.group("name")
        for command in commands
        for match in [HELM_ARRAY_ALIAS.match(command)]
        if match is not None
    }
    result: list[str] = []
    for command in commands:
        alias_install = any(
            re.search(
                rf"(?:^|\s)\"?\$\{{{re.escape(alias)}\[@\]\}}\"?\s+"
                r"(?:install(?:\s|$)|upgrade(?:\s+--install)?(?:\s|$))",
                command,
            )
            for alias in aliases
        )
        if HELM_INSTALL.search(command) or alias_install:
            result.append(command)
    return result


def _is_shell_entrypoint(relative: str, source: str) -> bool:
    first_line = source.splitlines()[0] if source.splitlines() else ""
    return relative.endswith((".sh", ".bash")) or (
        first_line.startswith("#!")
        and any(shell in first_line for shell in ("/sh", "/bash", "/dash", "/ksh"))
    )


def _anchor_strings(binding: dict[str, Any], manifest_path: Path) -> list[str]:
    value = binding.get("anchors")
    if value is None and isinstance(binding.get("anchor"), str):
        value = [binding["anchor"]]
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(anchor, str) and anchor for anchor in value)
    ):
        raise EvidenceError(f"{manifest_path}: direct surface anchors are invalid")
    return value


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
                "--authorization",
                "--toolchain",
                "release_image_contract.bootstrap_path",
                "release_image_contract.external_trust_path",
                "registry_auth_receipt_path",
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
    owners = manifest.get("terraform_plan_owners")
    if (
        not isinstance(owners, dict)
        or set(owners) != expected
        or set(owners.values()) != {"stages/foundation", "stages/workloads"}
    ):
        raise EvidenceError(
            f"{manifest_path}: every Terraform Helm release needs one production plan owner"
        )
    for plan_root in sorted(set(owners.values())):
        gate_source = _resolve(root, f"{plan_root}/release_image_gate.tf").read_text(
            encoding="utf-8"
        )
        cluster_source = _resolve(root, f"{plan_root}/cluster_contract.tf").read_text(
            encoding="utf-8"
        )
        for required in (
            'resource "terraform_data" "release_image_closure_gate"',
            "triggers_replace = [timestamp()]",
            "terraform_apply_gate_entrypoint.py",
            "release_image_contract.bootstrap_path",
            "release_image_contract.external_trust_path",
            "release_image_contract.toolchain_path",
        ):
            if required not in gate_source:
                raise EvidenceError(f"{plan_root}: apply root omits {required}")
        if "depends_on = [terraform_data.release_image_closure_gate]" not in cluster_source:
            raise EvidenceError(
                f"{plan_root}: cluster mutations do not depend on the image closure gate"
            )
    apply_wrapper = _resolve(root, "security/apply_signed_terraform_plan.sh").read_text(
        encoding="utf-8"
    )
    for required in (
        "signed-terraform-apply",
        "FS2_IMAGE_GATE_BOOTSTRAP",
        "FS2_EXTERNAL_CAPSULE_TRUST",
        "FS2_IMAGE_GATE_TOOLCHAIN",
        "FS2_EXTERNAL_CAPSULE_ACTIVE",
        "FS2_CAPSULE_SOURCE_ROOT",
        "--registry-refresh-registration",
    ):
        if required not in apply_wrapper:
            raise EvidenceError(f"saved-plan apply wrapper omits {required}")
    for relative in manifest.get("direct_installer_scripts", []):
        source = _resolve(root, relative).read_text(encoding="utf-8")
        for required in (
            "--post-renderer",
            "helm_image_postrenderer.py",
            "third-party-images.lock.json",
            "first-party-images.lock.json",
            "image-attestation-trust.json",
            "--authorization",
            "--toolchain",
            "FS2_EXTERNAL_CAPSULE_ACTIVE",
            "FS2_CAPSULE_SOURCE_ROOT",
        ):
            if required not in source:
                raise EvidenceError(f"{relative}: missing {required} consumer")
    stack_source = _resolve(root, "inference-stack").read_text(encoding="utf-8")
    for required in (
        'DEPLOY_ROOT: "."',
        'INFRA_ROOT: "stages/infrastructure"',
        'FOUNDATION_ROOT: "stages/foundation"',
        'WORKLOADS_ROOT: "stages/workloads"',
        "signed-terraform-apply",
        "FS2_EXTERNAL_CAPSULE_ACTIVE",
        "FS2_CAPSULE_SOURCE_ROOT",
        "FS2_CAPSULE_TOOL_DIR",
        "signed-terraform-plan",
        "receive_capsule_bindings",
        "SO_PEERCRED",
        "SOCK_SEQPACKET",
        "acquire-workload-registry-credential",
        "MINIMUM_WORKLOAD_CREDENTIAL_TTL_SECONDS",
        "MAXIMUM_REFRESH_READINESS_AGE_SECONDS",
        "REGISTRY_COPY_OPERATION_TIMEOUT_SECONDS",
        "--broker-workload-credential-at-provider-rpc",
        "workload-registry-secret-admission-contract.json",
        'gate_environment.pop("TF_VAR_nvcrio_dockerconfigjson", None)',
        "direct inference-stack execution is disabled",
        "registry_refresh_registration_path",
        'terraform_capsule_command("terraform-init"',
    ):
        if required not in stack_source:
            raise EvidenceError(f"inference-stack omits capsule invariant {required}")
    entrypoints = _source_text_entrypoints(root)
    discovered_installers = {
        relative
        for relative, source in entrypoints.items()
        if _is_shell_entrypoint(relative, source) and _script_helm_installs(source)
    }
    expected_installers = set(manifest.get("direct_installer_scripts", []))
    exclusions = manifest.get("non_release_helm_scripts")
    if not isinstance(exclusions, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and isinstance(item.get("purpose"), str)
        and item["purpose"]
        for item in exclusions
    ):
        raise EvidenceError(f"{manifest_path}: non-release Helm script ledger is invalid")
    excluded_installers = {item["path"] for item in exclusions}
    if discovered_installers != expected_installers | excluded_installers:
        raise EvidenceError(
            "direct Helm installer mismatch: "
            f"unclassified={sorted(discovered_installers - expected_installers - excluded_installers)} "
            f"stale={sorted((expected_installers | excluded_installers) - discovered_installers)}"
        )
    documented_bypasses = sorted(
        relative
        for relative, source in entrypoints.items()
        if relative.endswith(".md") and DOCUMENTED_HELM_INSTALL.search(source)
    )
    if documented_bypasses:
        raise EvidenceError(
            "operator documentation contains raw Helm install/upgrade bypasses: "
            f"{documented_bypasses}"
        )
    workflow_or_make_bypasses = sorted(
        relative
        for relative, source in entrypoints.items()
        if (
            relative.endswith((".yml", ".yaml"))
            or Path(relative).name == "Makefile"
        )
        and DOCUMENTED_HELM_INSTALL.search(source)
    )
    if workflow_or_make_bypasses:
        raise EvidenceError(
            "workflow or Makefile contains an ungoverned Helm install/upgrade: "
            f"{workflow_or_make_bypasses}"
        )
    programmatic_bypasses = sorted(
        relative
        for relative, source in entrypoints.items()
        if relative.endswith((".py", ".js", ".ts", ".tsx"))
        and PROGRAMMATIC_HELM_INSTALL.search(source)
    )
    if programmatic_bypasses:
        raise EvidenceError(
            "programmatic Helm install/upgrade bypasses the governed entrypoints: "
            f"{programmatic_bypasses}"
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
        bindings = [
            item
            for item in anchors
            if isinstance(item, dict) and item.get("script") == relative
        ]
        expected_anchors = [
            anchor
            for item in bindings
            for anchor in _anchor_strings(item, manifest_path)
        ]
        commands = _logical_shell_commands(source)
        if relative.endswith("bootstrap/bootstrap.sh"):
            install_commands = [
                command for command in commands if command.startswith("install_chart ")
            ]
        else:
            install_commands = _script_helm_installs(source)
        if len(install_commands) != len(expected_anchors):
            raise EvidenceError(
                f"{relative}: discovered {len(install_commands)} install calls but "
                f"{len(expected_anchors)} render surfaces are declared"
            )
        for binding in bindings:
            gate_marker = binding.get("gate_marker")
            if not isinstance(gate_marker, str) or not gate_marker:
                raise EvidenceError(
                    f"{relative}: {binding.get('id')} has no post-render argument binding"
                )
            gate_scope = binding.get("gate_scope", "command")
            if gate_scope not in {"command", "script-wrapper"}:
                raise EvidenceError(
                    f"{relative}: {binding.get('id')} has an invalid gate scope"
                )
            for anchor in _anchor_strings(binding, manifest_path):
                matched = [command for command in install_commands if anchor in command]
                if len(matched) != 1:
                    raise EvidenceError(
                        f"{relative}: install anchor is absent or ambiguous: {anchor}"
                    )
                if gate_scope == "command" and gate_marker not in matched[0]:
                    raise EvidenceError(
                        f"{relative}: {anchor} bypasses post-render arguments"
                    )
            if gate_scope == "script-wrapper" and gate_marker not in source:
                raise EvidenceError(
                    f"{relative}: installer wrapper bypasses post-render arguments"
                )
            if source.count(gate_marker) < 1:
                raise EvidenceError(
                    f"{relative}: post-render argument binding is absent: {gate_marker}"
                )
    static_roots = [
        _resolve(root, relative)
        for relative in manifest.get("static_manifest_roots", [])
    ]
    for path in sorted(root.rglob("*.y*ml")):
        try:
            key_lines = image_key_lines(path.read_text(encoding="utf-8"))
        except YamlImageError as exc:
            raise EvidenceError(f"{path}: {exc}") from exc
        if not key_lines:
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
            source
            + b"\0"
            + b"\0".join(
                value.encode("utf-8")
                for value in _anchor_strings(anchor, root / "security/release-image-surfaces.json")
            )
            + b"\0"
            + anchor["gate_marker"].encode("utf-8")
        ).hexdigest()
    return declarations


def _direct_execution_bindings(
    root: Path, manifest: dict[str, Any], manifest_path: Path
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for binding in manifest["direct_surface_anchors"]:
        script = _resolve(root, binding["script"])
        source = script.read_text(encoding="utf-8")
        commands = _logical_shell_commands(source)
        matched: list[str] = []
        for anchor in _anchor_strings(binding, manifest_path):
            candidates = [command for command in commands if anchor in command]
            if len(candidates) != 1:
                raise EvidenceError(
                    f"{binding['id']}: direct invocation anchor is ambiguous"
                )
            matched.append(candidates[0])
        execution: dict[str, Any] = {
            "kind": "direct-installer",
            "script": binding["script"],
            "script_sha256": _sha256(script),
            "commands_sha256": hashlib.sha256(
                json.dumps(matched, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }
        modes = binding.get("modes")
        if modes is not None:
            if (
                not isinstance(modes, dict)
                or set(modes) != {"install", "upgrade"}
                or not all(isinstance(value, str) and value for value in modes.values())
            ):
                raise EvidenceError(f"{binding['id']}: direct modes are invalid")
            command_by_mode: dict[str, str] = {}
            for mode, anchor in modes.items():
                candidates = [command for command in commands if anchor in command]
                if len(candidates) != 1:
                    raise EvidenceError(
                        f"{binding['id']}: {mode} invocation is absent or ambiguous"
                    )
                command_by_mode[mode] = hashlib.sha256(
                    candidates[0].encode("utf-8")
                ).hexdigest()
            chart_source = binding.get("chart_source")
            release_name_variable = binding.get("release_name_variable")
            namespace_variable = binding.get("namespace_variable")
            if not all(
                isinstance(value, str) and value
                for value in (
                    chart_source,
                    release_name_variable,
                    namespace_variable,
                )
            ):
                raise EvidenceError(
                    f"{binding['id']}: release, namespace, or chart binding is missing"
                )
            execution.update(
                {
                    "release_name_variable": release_name_variable,
                    "namespace_variable": namespace_variable,
                    "chart_source": chart_source,
                    "chart_source_sha256": _source_tree_sha256(
                        _resolve(root, chart_source)
                    ),
                    "commands_sha256_by_mode": command_by_mode,
                }
            )
        result[binding["id"]] = execution
    return result


def _source_tree_sha256(path: Path) -> str:
    if not path.is_dir():
        raise EvidenceError(f"{path}: chart source must be a directory")
    entries: list[dict[str, str]] = []
    for candidate in sorted(path.rglob("*")):
        if candidate.is_symlink():
            raise EvidenceError(f"{candidate}: chart source symlinks are not accepted")
        if candidate.is_file():
            entries.append(
                {
                    "path": candidate.relative_to(path).as_posix(),
                    "sha256": _sha256(candidate),
                }
            )
    if not entries:
        raise EvidenceError(f"{path}: chart source is empty")
    return hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _git_identity(root: Path, trust_path: Path) -> tuple[str, str]:
    lock_path, environment_trust = environment_toolchain()
    if environment_trust.resolve() != trust_path.resolve():
        raise EvidenceError("execution trust differs from requested release trust")
    validate_current_python(lock_path=lock_path, trust_path=trust_path)
    git_path, _ = validated_tool(
        "git", lock_path=lock_path, trust_path=trust_path
    )

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            [str(git_path), "-C", str(root), *arguments],
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
    materials_authorization_sha256: str,
    trust_policy_sha256: str,
    source_execution: dict[str, Any],
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
            "materials_authorization_sha256": materials_authorization_sha256,
            "trust_policy_sha256": trust_policy_sha256,
        },
        "source_execution": source_execution,
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


def _terraform_helm_resources(path: Path) -> dict[str, dict[str, Any]]:
    return _terraform_helm_resources_document(_load(path), str(path))


def _terraform_helm_resources_document(
    plan: dict[str, Any], label: str
) -> dict[str, dict[str, Any]]:
    planned = plan.get("planned_values")
    root_module = planned.get("root_module") if isinstance(planned, dict) else None
    if not isinstance(root_module, dict):
        raise EvidenceError(f"{label}: Terraform planned root module is missing")
    result: dict[str, dict[str, Any]] = {}

    def visit(module: dict[str, Any]) -> None:
        for resource in module.get("resources", []):
            if not isinstance(resource, dict) or resource.get("type") != "helm_release":
                continue
            address = resource.get("address")
            values = resource.get("values")
            if not isinstance(address, str) or not isinstance(values, dict):
                raise EvidenceError(f"{label}: malformed planned Helm resource")
            if address in result:
                raise EvidenceError(f"{label}: duplicate planned address {address}")
            result[address] = values
        for child in module.get("child_modules", []):
            if not isinstance(child, dict):
                raise EvidenceError(f"{label}: malformed planned child module")
            visit(child)

    visit(root_module)
    if not result:
        raise EvidenceError(f"{label}: Terraform plan contains no Helm resources")
    return result


def _terraform_execution_binding(
    *,
    surface_id: str,
    plan_root: str,
    resources: dict[str, dict[str, Any]],
    chart: dict[str, Any],
    values_sha256: list[str],
) -> dict[str, Any]:
    resource_name = surface_id.rsplit("::", 1)[1]
    matches = [
        (address, values)
        for address, values in resources.items()
        if re.search(
            rf"(?:^|\.)helm_release\.{re.escape(resource_name)}(?:\[.+\])?$",
            address,
        )
    ]
    if len(matches) != 1:
        raise EvidenceError(
            f"{surface_id}: production plan has {len(matches)} matching Helm resources"
        )
    address, planned = matches[0]
    planned_chart = planned.get("chart")
    if not isinstance(planned_chart, str) or not planned_chart:
        raise EvidenceError(f"{surface_id}: planned Helm chart is missing")
    if chart.get("planned_source") != planned_chart:
        raise EvidenceError(f"{surface_id}: render chart differs from planned chart")
    for field, expected in (
        ("name", chart["release_name"]),
        ("namespace", chart["namespace"]),
    ):
        if planned.get(field) != expected:
            raise EvidenceError(f"{surface_id}: planned Helm {field} differs from render")
    planned_values = planned.get("values", [])
    if not isinstance(planned_values, list) or not all(
        isinstance(value, str) for value in planned_values
    ):
        raise EvidenceError(f"{surface_id}: planned Helm values are not exact strings")
    actual_value_hashes = [
        hashlib.sha256(value.encode("utf-8")).hexdigest() for value in planned_values
    ]
    if actual_value_hashes != values_sha256:
        raise EvidenceError(
            f"{surface_id}: rendered values differ from the exact Terraform plan"
        )
    for field in ("repository", "version"):
        planned_value = planned.get(field)
        if planned_value not in (None, "") and planned_value != chart[field]:
            raise EvidenceError(f"{surface_id}: planned chart {field} differs from render")
    postrender = planned.get("postrender")
    serialized_postrender = json.dumps(postrender, sort_keys=True)
    for required in (
        "helm_image_postrenderer.py",
        "third-party-images.lock.json",
        "first-party-images.lock.json",
        "image-attestation-trust.json",
        "--authorization",
    ):
        if required not in serialized_postrender:
            raise EvidenceError(
                f"{surface_id}: planned Helm post-renderer omits {required}"
            )
    return {
        "kind": "terraform-plan",
        "plan_root": plan_root,
        "resource_address": address,
        "planned_chart": planned_chart,
        "planned_repository": planned.get("repository"),
        "planned_version": planned.get("version"),
        "planned_postrender_sha256": hashlib.sha256(
            serialized_postrender.encode("utf-8")
        ).hexdigest(),
        "planned_resource_sha256": hashlib.sha256(
            json.dumps(planned, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def _validate_terraform_resource_closure(
    manifest: dict[str, Any],
    resources_by_plan: dict[str, dict[str, dict[str, Any]]],
) -> None:
    """Reject missing, ambiguous, or undeclared planned Helm releases."""

    for plan_root, resources in resources_by_plan.items():
        expected_ids = sorted(
            surface_id
            for surface_id, owner in manifest["terraform_plan_owners"].items()
            if owner == plan_root
        )
        matched_addresses: set[str] = set()
        for surface_id in expected_ids:
            resource_name = surface_id.rsplit("::", 1)[1]
            matches = [
                address
                for address in resources
                if re.search(
                    rf"(?:^|\.)helm_release\.{re.escape(resource_name)}(?:\[.+\])?$",
                    address,
                )
            ]
            if len(matches) != 1:
                raise EvidenceError(
                    f"{surface_id}: exact plan has {len(matches)} matching resources"
                )
            matched_addresses.add(matches[0])
        if matched_addresses != set(resources):
            raise EvidenceError(
                f"{plan_root}: ungoverned planned Helm resources: "
                f"{sorted(set(resources) - matched_addresses)}"
            )


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
            try:
                references = {
                    scalar.reference
                    for scalar in independently_validated_image_scalars(
                        path.read_text(encoding="utf-8")
                    )
                }
            except SemanticYamlError as exc:
                raise EvidenceError(f"{path}: {exc}") from exc
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
    materials_authorization_path: Path,
    attestation_identity_path: Path,
) -> dict[str, Any]:
    manifest = validate_source_surfaces(root, manifest_path)
    packet = _load(packet_path)
    if packet.get("schema") != "fs2-serve.nebius.ai/release-render-packet/v2":
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
    commit, tree = _git_identity(root, trust_path)
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
    if packet.get("materials_authorization_sha256") != _sha256(
        materials_authorization_path
    ):
        raise EvidenceError(f"{packet_path}: materials authorization hash mismatch")
    authorized_materials = validate_image_gate_authorization(
        materials_authorization_path, trust_path, root
    )
    if authorized_materials != {
        "inventory_sha256": _sha256(inventory_path),
        "first_party_inventory_sha256": _sha256(first_party_inventory_path),
        "catalog_image_map_sha256": _sha256(catalog_image_map_path),
    }:
        raise EvidenceError(
            f"{packet_path}: signed materials authorization differs from render inputs"
        )
    source_catalog_map = root / "security/catalog-images.lock.json"
    if _sha256(catalog_image_map_path) != _sha256(source_catalog_map):
        raise EvidenceError(
            f"{packet_path}: catalog image map differs from reviewed source authority"
        )

    third_party_images = validate_inventory(inventory_path, trust_path)
    first_party_images = validate_first_party_inventory(
        first_party_inventory_path, trust_path, source_root=root
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
    direct_execution_bindings = _direct_execution_bindings(
        root, manifest, manifest_path
    )
    if set(declaration_hashes) != expected_surfaces:
        raise EvidenceError("release declaration hash closure differs from surfaces")
    renders = packet.get("renders")
    if not isinstance(renders, list):
        raise EvidenceError(f"{packet_path}: renders must be an array")
    actual_surfaces: set[str] = set()
    surface_executions: dict[str, dict[str, Any]] = {}
    subjects: set[str] = set()
    rendered_subjects: set[str] = set()
    subject_provenance: dict[str, dict[str, Any]] = {}
    packet_dir = packet_path.parent.resolve()
    plan_bindings = packet.get("terraform_plans")
    expected_plan_roots = set(manifest["terraform_plan_owners"].values())
    if not isinstance(plan_bindings, dict) or set(plan_bindings) != expected_plan_roots:
        raise EvidenceError(
            f"{packet_path}: exact foundation/workloads Terraform plans are incomplete"
        )
    terraform_plans = {
        plan_root: _artifact(
            packet_dir,
            plan_bindings[plan_root],
            f"exact {plan_root} Terraform plan JSON",
        )
        for plan_root in sorted(expected_plan_roots)
    }
    terraform_plan_hashes = {
        plan_root: _sha256(plan) for plan_root, plan in terraform_plans.items()
    }
    terraform_resources = {
        plan_root: _terraform_helm_resources(plan)
        for plan_root, plan in terraform_plans.items()
    }
    _validate_terraform_resource_closure(manifest, terraform_resources)
    planned_subjects = set().union(
        *(_planned_images(plan) for plan in terraform_plans.values())
    )
    placeholder_plans = sorted(
        reference
        for reference in planned_subjects
        if reference.split("/", 1)[0] in placeholder_registries
    )
    if placeholder_plans:
        raise EvidenceError(
            "production Terraform plans contain placeholder images: "
            f"{placeholder_plans}"
        )
    subjects.update(planned_subjects)
    for reference in planned_subjects:
        subject_provenance[reference] = {
            "kind": "terraform-production-plan",
            "terraform_plan_sha256": sorted(terraform_plan_hashes.values()),
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
        if surface_id in manifest["terraform_plan_owners"]:
            plan_root = manifest["terraform_plan_owners"][surface_id]
            source_execution = _terraform_execution_binding(
                surface_id=surface_id,
                plan_root=plan_root,
                resources=terraform_resources[plan_root],
                chart=chart,
                values_sha256=value_hashes,
            )
        else:
            source_execution = direct_execution_bindings.get(surface_id)
            if source_execution is None:
                raise EvidenceError(f"{surface_id}: source execution binding is missing")
        if render.get("source_execution") != source_execution:
            raise EvidenceError(
                f"{surface_id}: packet execution does not match source/plan semantics"
            )
        surface_executions[surface_id] = {
            "chart_digest": chart["digest"],
            "release_name": chart["release_name"],
            "namespace": chart["namespace"],
            "ordered_values_sha256": value_hashes,
            "rendered_manifest_sha256": _sha256(rendered),
            "source_execution": source_execution,
        }
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
            materials_authorization_sha256=_sha256(materials_authorization_path),
            trust_policy_sha256=_sha256(trust_path),
            source_execution=source_execution,
            trust_path=trust_path,
        )
        try:
            references = {
                scalar.reference
                for scalar in independently_validated_image_scalars(
                    rendered.read_text(encoding="utf-8")
                )
            }
        except SemanticYamlError as exc:
            raise EvidenceError(f"{surface_id}: {exc}") from exc
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
            "build_attestation_sha256": image["build_attestation"]["sha256"],
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
        "materials_authorization_sha256": _sha256(materials_authorization_path),
        "trust_policy_sha256": _sha256(trust_path),
        "render_packet_sha256": _sha256(packet_path),
        "render_packet_signature_sha256": _sha256(signature),
        "terraform_plan_sha256": terraform_plan_hashes,
        "surface_executions": {
            surface_id: surface_executions[surface_id]
            for surface_id in sorted(surface_executions)
        },
        "subjects": sorted(subjects),
        "subject_provenance": {
            reference: subject_provenance[reference] for reference in sorted(subjects)
        },
    }


def verify_direct_invocation(
    *,
    root: Path,
    manifest_path: Path,
    trust_path: Path,
    closure_path: Path,
    surface_id: str,
    values_paths: list[Path],
    release_name: str,
    namespace: str,
    mode: str,
    chart_path: Path,
    rendered_manifest: bytes,
) -> None:
    """Authorize one direct installer invocation against a signed closure.

    The caller may select an evidence file and values files, but cannot select
    the trust root.  The reviewed source trust policy verifies the closure,
    whose source identity, source-derived invocation, and ordered values hashes
    must all match the clean checkout and this exact invocation.
    """

    validate_detached_signature(
        closure_path, Path(f"{closure_path}.sig"), trust_path
    )
    closure = _load(closure_path)
    if closure.get("schema") != "fs2-serve.nebius.ai/release-image-closure/v2":
        raise EvidenceError(f"{closure_path}: unsupported release closure schema")
    validate_attestation_identity(
        closure.get("attestation"), trust_path, purpose=str(closure_path)
    )
    commit, tree = _git_identity(root, trust_path)
    if closure.get("source") != {"commit": commit, "tree": tree}:
        raise EvidenceError(f"{closure_path}: closure source differs from checkout")
    if closure.get("trust_policy_sha256") != _sha256(trust_path):
        raise EvidenceError(f"{closure_path}: closure trust policy differs from source")
    if closure.get("surface_manifest_sha256") != _sha256(manifest_path):
        raise EvidenceError(f"{closure_path}: closure surface policy differs from source")

    manifest = validate_source_surfaces(root, manifest_path)
    if surface_id not in set(manifest.get("direct_render_surfaces", [])):
        raise EvidenceError(f"{surface_id}: not a governed direct release surface")
    expected_execution = _direct_execution_bindings(
        root, manifest, manifest_path
    ).get(surface_id)
    surfaces = closure.get("surface_executions")
    execution = surfaces.get(surface_id) if isinstance(surfaces, dict) else None
    if not isinstance(execution, dict):
        raise EvidenceError(f"{closure_path}: direct surface execution is missing")
    if execution.get("source_execution") != expected_execution:
        raise EvidenceError(
            f"{closure_path}: direct surface invocation differs from source"
        )
    if mode not in {"install", "upgrade"}:
        raise EvidenceError(f"{closure_path}: unsupported direct release mode")
    if (
        execution.get("release_name") != release_name
        or execution.get("namespace") != namespace
        or expected_execution.get("release_name_variable") != "RELEASE_NAME"
        or expected_execution.get("namespace_variable") != "NAMESPACE"
        or mode not in expected_execution.get("commands_sha256_by_mode", {})
    ):
        raise EvidenceError(
            f"{closure_path}: release name, namespace, or mode differs from evidence"
        )
    expected_chart = _resolve(root, expected_execution["chart_source"])
    if chart_path.resolve() != expected_chart:
        raise EvidenceError(f"{closure_path}: direct chart path differs from evidence")
    if _source_tree_sha256(expected_chart) != expected_execution["chart_source_sha256"]:
        raise EvidenceError(f"{closure_path}: direct chart source differs from evidence")
    if hashlib.sha256(rendered_manifest).hexdigest() != execution.get(
        "rendered_manifest_sha256"
    ):
        raise EvidenceError(
            f"{closure_path}: actual post-rendered manifest differs from signed render"
        )
    value_hashes = [_sha256(path.resolve()) for path in values_paths]
    if execution.get("ordered_values_sha256") != value_hashes:
        raise EvidenceError(
            f"{closure_path}: selected values differ from the signed render"
        )


def verify_terraform_apply_gate(
    *,
    root: Path,
    manifest_path: Path,
    trust_path: Path,
    closure_path: Path,
    plan_root: str,
    plan_json: bytes,
) -> None:
    """Bind an actual saved-plan apply to the signed release image closure."""

    validate_detached_signature(closure_path, Path(f"{closure_path}.sig"), trust_path)
    closure = _load(closure_path)
    if closure.get("schema") != "fs2-serve.nebius.ai/release-image-closure/v2":
        raise EvidenceError(f"{closure_path}: unsupported release closure schema")
    validate_attestation_identity(
        closure.get("attestation"), trust_path, purpose=str(closure_path)
    )
    commit, tree = _git_identity(root, trust_path)
    if closure.get("source") != {"commit": commit, "tree": tree}:
        raise EvidenceError(f"{closure_path}: closure source differs from checkout")
    if closure.get("trust_policy_sha256") != _sha256(trust_path):
        raise EvidenceError(f"{closure_path}: closure trust policy differs from source")
    if closure.get("surface_manifest_sha256") != _sha256(manifest_path):
        raise EvidenceError(f"{closure_path}: closure surface policy differs from source")
    manifest = validate_source_surfaces(root, manifest_path)
    expected_roots = set(manifest.get("terraform_plan_owners", {}).values())
    if plan_root not in expected_roots:
        raise EvidenceError(f"{plan_root}: plan root is not governed")
    expected_plan_hashes = closure.get("terraform_plan_sha256")
    actual_plan_sha256 = hashlib.sha256(plan_json).hexdigest()
    if (
        not isinstance(expected_plan_hashes, dict)
        or expected_plan_hashes.get(plan_root) != actual_plan_sha256
    ):
        raise EvidenceError(f"{plan_root}: saved-plan JSON differs from signed closure")
    try:
        plan_document = json.loads(plan_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{plan_root}: saved-plan JSON is invalid") from exc
    if not isinstance(plan_document, dict):
        raise EvidenceError(f"{plan_root}: saved-plan JSON root must be an object")
    resources = _terraform_helm_resources_document(plan_document, plan_root)
    _validate_terraform_resource_closure(manifest, {plan_root: resources})
    executions = closure.get("surface_executions")
    if not isinstance(executions, dict):
        raise EvidenceError(f"{closure_path}: surface execution closure is missing")
    expected_surfaces = {
        surface_id
        for surface_id, owner in manifest["terraform_plan_owners"].items()
        if owner == plan_root
    }
    for surface_id in expected_surfaces:
        resource_name = surface_id.rsplit("::", 1)[1]
        matches = [
            values
            for address, values in resources.items()
            if re.search(
                rf"(?:^|\.)helm_release\.{re.escape(resource_name)}(?:\[.+\])?$",
                address,
            )
        ]
        execution = executions.get(surface_id)
        if len(matches) != 1 or not isinstance(execution, dict):
            raise EvidenceError(f"{surface_id}: signed execution is absent or ambiguous")
        actual_resource_sha256 = hashlib.sha256(
            json.dumps(matches[0], sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        source_execution = execution.get("source_execution")
        if (
            not isinstance(source_execution, dict)
            or source_execution.get("plan_root") != plan_root
            or source_execution.get("planned_resource_sha256")
            != actual_resource_sha256
        ):
            raise EvidenceError(f"{surface_id}: applied Helm resource differs from closure")


def _parent_pid(pid: int) -> int:
    try:
        stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        return int(stat_fields[3])
    except (OSError, ValueError, IndexError) as exc:
        raise EvidenceError("cannot prove Terraform apply process ancestry") from exc


def _terraform_apply_process(terraform_path: Path, plan_fd: int) -> int:
    """Find the exact Terraform ancestor applying the retained plan FD."""

    expected_executable = terraform_path.resolve()
    pid = os.getppid()
    for _ in range(32):
        if pid <= 1:
            break
        try:
            executable = Path(f"/proc/{pid}/exe").resolve()
            arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            pid = _parent_pid(pid)
            continue
        decoded = [value.decode("utf-8", "strict") for value in arguments if value]
        if executable == expected_executable:
            expected_plan = f"/proc/self/fd/{plan_fd}"
            if "apply" not in decoded or expected_plan not in decoded:
                raise EvidenceError(
                    "Terraform ancestor is not applying the sealed saved-plan descriptor"
                )
            return pid
        pid = _parent_pid(pid)
    raise EvidenceError("release gate has no reviewed Terraform apply ancestor")


def verify_live_terraform_apply_gate(
    *,
    root: Path,
    manifest_path: Path,
    trust_path: Path,
    closure_path: Path,
    plan_root: str,
) -> None:
    """Re-render the binary plan descriptor held by the actual apply process."""

    lock_path, environment_trust = environment_toolchain()
    if environment_trust.resolve() != trust_path.resolve():
        raise EvidenceError("apply execution trust differs from release trust")
    validate_current_python(lock_path=lock_path, trust_path=trust_path)
    terraform_path, terraform_sha256 = validated_tool(
        "terraform", lock_path=lock_path, trust_path=trust_path
    )
    if os.environ.get("FS2_SIGNED_PLAN_TERRAFORM_SHA256") != terraform_sha256:
        raise EvidenceError("apply Terraform digest differs from execution capsule")
    try:
        plan_fd = int(os.environ["FS2_SIGNED_PLAN_FD"])
    except (KeyError, ValueError) as exc:
        raise EvidenceError("sealed saved-plan descriptor is absent") from exc
    if os.environ.get("FS2_SIGNED_PLAN_ROOT") != plan_root:
        raise EvidenceError("sealed saved-plan root differs from this apply root")
    terraform_pid = _terraform_apply_process(terraform_path, plan_fd)
    exact_plan = Path(f"/proc/{terraform_pid}/fd/{plan_fd}")
    try:
        descriptor = os.open(exact_plan, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise EvidenceError("actual Terraform saved-plan descriptor is unavailable") from exc
    os.close(descriptor)
    completed = subprocess.run(
        [
            str(terraform_path),
            f"-chdir={root / plan_root}",
            "show",
            "-json",
            str(exact_plan),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise EvidenceError("actual Terraform saved plan cannot be rendered")
    verify_terraform_apply_gate(
        root=root,
        manifest_path=manifest_path,
        trust_path=trust_path,
        closure_path=closure_path,
        plan_root=plan_root,
        plan_json=completed.stdout,
    )


def main() -> int:
    if os.environ.get("FS2_EXTERNAL_CAPSULE_ACTIVE") != "1":
        print("release closure requires the external capsule", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--surfaces", required=True, type=Path)
    parser.add_argument("--render-packet", type=Path)
    parser.add_argument("--trust", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--first-party-inventory", type=Path)
    parser.add_argument("--catalog-image-map", type=Path)
    parser.add_argument("--materials-authorization", type=Path)
    parser.add_argument("--attestation-identity", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-direct-closure", type=Path)
    parser.add_argument("--surface-id")
    parser.add_argument("--values-file", action="append", default=[], type=Path)
    parser.add_argument("--release-name")
    parser.add_argument("--namespace")
    parser.add_argument("--mode", choices=("install", "upgrade"))
    parser.add_argument("--chart-path", type=Path)
    parser.add_argument("--rendered-manifest-stdin", action="store_true")
    parser.add_argument("--verify-terraform-closure", type=Path)
    parser.add_argument("--plan-root")
    parser.add_argument("--terraform-plan-json-stdin", action="store_true")
    parser.add_argument("--verify-terraform-apply-process", action="store_true")
    args = parser.parse_args()
    try:
        root = args.root.resolve()
        if args.verify_terraform_closure is not None:
            if (
                args.trust is None
                or not args.plan_root
                or args.render_packet is not None
                or args.verify_direct_closure is not None
            ):
                raise EvidenceError(
                    "--trust and --plan-root are required exclusively with "
                    "--verify-terraform-closure"
                )
            if args.verify_terraform_apply_process == args.terraform_plan_json_stdin:
                raise EvidenceError(
                    "choose exactly one actual-process or stdin Terraform plan source"
                )
            if args.verify_terraform_apply_process:
                verify_live_terraform_apply_gate(
                    root=root,
                    manifest_path=args.surfaces.resolve(),
                    trust_path=args.trust.resolve(),
                    closure_path=args.verify_terraform_closure.resolve(),
                    plan_root=args.plan_root,
                )
            else:
                verify_terraform_apply_gate(
                    root=root,
                    manifest_path=args.surfaces.resolve(),
                    trust_path=args.trust.resolve(),
                    closure_path=args.verify_terraform_closure.resolve(),
                    plan_root=args.plan_root,
                    plan_json=sys.stdin.buffer.read(),
                )
        elif args.verify_direct_closure is not None:
            if (
                args.trust is None
                or not args.surface_id
                or not args.release_name
                or not args.namespace
                or not args.mode
                or args.chart_path is None
                or not args.rendered_manifest_stdin
            ):
                raise EvidenceError(
                    "--trust, --surface-id, --release-name, --namespace, --mode, "
                    "--chart-path, and --rendered-manifest-stdin are required with "
                    "--verify-direct-closure"
                )
            if args.render_packet is not None:
                raise EvidenceError(
                    "--render-packet and --verify-direct-closure are mutually exclusive"
                )
            verify_direct_invocation(
                root=root,
                manifest_path=args.surfaces.resolve(),
                trust_path=args.trust.resolve(),
                closure_path=args.verify_direct_closure.resolve(),
                surface_id=args.surface_id,
                values_paths=args.values_file,
                release_name=args.release_name,
                namespace=args.namespace,
                mode=args.mode,
                chart_path=args.chart_path,
                rendered_manifest=sys.stdin.buffer.read(),
            )
        elif args.render_packet is None:
            validate_source_surfaces(root, args.surfaces.resolve())
        else:
            if (
                args.output is None
                or args.trust is None
                or args.inventory is None
                or args.first_party_inventory is None
                or args.catalog_image_map is None
                or args.materials_authorization is None
                or args.attestation_identity is None
            ):
                raise EvidenceError(
                    "--output, --trust, --inventory, --first-party-inventory, "
                    "--catalog-image-map, --materials-authorization, and "
                    "--attestation-identity are required "
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
                args.materials_authorization.resolve(),
                args.attestation_identity.resolve(),
            )
            args.output.write_text(
                json.dumps(closure, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    except (EvidenceError, ToolchainError) as exc:
        print(f"release image closure gate: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

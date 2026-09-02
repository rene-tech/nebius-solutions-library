#!/usr/bin/env python3
"""Fail-closed loader for the fs2-serve one-file-per-model catalog."""

from __future__ import annotations

import copy
import functools
import hashlib
import json
import re
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit


CATALOG_SCHEMA = "fs2-serve.nebius.ai/catalog/v1"
LOADER_CONTRACT = "fs2-serve.nebius.ai/catalog-loader/v1"
MODEL_SCHEMA = "fs2-serve.nebius.ai/model/v1"
MODEL_VARIANTS_SCHEMA = "fs2-serve.nebius.ai/model-variants/v4"
MODEL_VARIANT_SUPPLY_SCHEMA = "fs2-serve.nebius.ai/model-variant-supply-receipt/v5"
MODEL_VARIANT_QUALIFICATION_SCHEMA = (
    "fs2-serve.nebius.ai/model-variant-qualification-receipt/v5"
)
SUPPORTED_MODEL_FAMILIES = frozenset(
    {
        "bionemo-nim",
        "image-generation",
        "llm",
        "medical-imaging",
        "scientific-protein",
        "video-generation",
    }
)
REQUIRED_FALLBACK_CANDIDATE_IDS = frozenset(
    {
        "boltz2-hf",
        "diffdock-upstream-v1-1",
        "evo2-40b-hf",
        "genmol-hf-v2",
        "molmim-ngc-70m-v24-3",
        "msa-search-pdb70-colabfold",
        "nv-segment-ct-hf",
        "openfold2-hf-mirror",
        "openfold3-preview2-hf",
        "proteinmpnn-upstream-2023-06",
        "rfdiffusion-upstream",
    }
)
REQUIRED_TESTED_MODEL_IDS = frozenset(
    {
        "boltz2",
        "cosmos3-nano",
        "diffdock",
        "evo2-40b",
        "genmol",
        "glm-5-2-fp8",
        "molmim",
        "msa-search-pdb70",
        "nv-reason-cxr-3b",
        "nv-segment-ct",
        "openfold2",
        "openfold3",
        "proteinmpnn",
        "qwen3-8b",
        "rfdiffusion",
        "sdxl",
    }
)
MODEL_ID = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_OBJECT = re.compile(r"^[0-9a-f]{40}$")
NIM_CACHE_OWNER = "nim-operator-nimcache"
LOCALIZER_CACHE_OWNER = "fs2-serve-localizer"
BLOCKED_CACHE_OWNER = "none"
RFC3339_UTC_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class CatalogError(ValueError):
    """Catalog data violates the versioned loader contract."""


def strong_sha256(value: Any, label: str, *, image: bool = False) -> str:
    """Validate a digest and reject conspicuous placeholder/test patterns."""

    pattern = IMAGE_DIGEST if image else SHA256
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise CatalogError(f"{label} is not a lowercase digest")
    hexadecimal = value.removeprefix("sha256:")
    repeated_pattern = any(
        len(hexadecimal) % width == 0
        and hexadecimal == hexadecimal[:width] * (len(hexadecimal) // width)
        for width in range(1, 17)
    )
    if len(set(hexadecimal)) < 8 or repeated_pattern:
        raise CatalogError(f"{label} is a placeholder rather than a content digest")
    return value


def canonical_http_path(value: Any, label: str, *, allow_root: bool = False) -> str:
    """Return one unambiguous absolute HTTP path without normalization tricks."""

    path = _text(value, label)
    assert path is not None
    parsed = PurePosixPath(path)
    if (
        not path.startswith("/")
        or (path == "/" and not allow_root)
        or (path != "/" and path.endswith("/"))
        or "//" in path
        or "\\" in path
        or "?" in path
        or "#" in path
        or "%" in path
        or unquote(path) != path
        or any(part in {"", ".", ".."} for part in parsed.parts[1:])
    ):
        raise CatalogError(f"{label} is not a canonical absolute path")
    return path


def canonical_content_uri(
    value: Any,
    *,
    model_id: str,
    content_digest: str,
    scheme: str | None = None,
) -> str:
    """Validate the closed SFS, provider-PVC, and NVMe content-address layouts."""

    strong_sha256(content_digest, "artifact content digest")
    uri = _text(value, "artifact content URI")
    assert uri is not None
    parsed = urlsplit(uri)
    try:
        port = parsed.port
    except ValueError as exc:
        raise CatalogError("artifact content URI has an invalid port") from exc
    if (
        parsed.scheme not in {"sfs", "pvc", "nvme"}
        or (scheme is not None and parsed.scheme != scheme)
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.query
        or parsed.fragment
        or "%" in parsed.path
        or "//" in parsed.path
        or unquote(parsed.path) != parsed.path
    ):
        raise CatalogError("artifact content URI is not canonical")
    if parsed.scheme == "nvme":
        expected_host = "localhost"
        expected_path = f"/var/lib/fs2-serve/cache/models/{model_id}/sha256/{content_digest}"
    elif parsed.scheme == "pvc":
        if model_id != "qwen3-8b":
            raise CatalogError("provider block PVC content is currently reviewed only for Qwen")
        expected_host = "fs2-models"
        expected_path = f"/qwen3-8b-weights/models/{model_id}/sha256/{content_digest}"
    else:
        expected_host = "fs2-cache"
        expected_path = f"/mnt/fs2-serve-cache/models/{model_id}/sha256/{content_digest}"
    if parsed.netloc != expected_host or parsed.path != expected_path:
        raise CatalogError("artifact content URI differs from the owned model-scoped layout")
    return uri


def execution_identity(value: Mapping[str, Any]) -> str:
    """Bind executable argv to the immutable runtime image and model revision."""

    runtime = value["runtime"]
    source = value["model"]["source"]
    payload = {
        "argv": runtime["command"],
        "runtime_kind": runtime["kind"],
        "runtime_image_digest": runtime["image"]["digest"],
        "model_repository": source["repository"],
        "model_revision": source["revision"],
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


MODEL_CONTENT_PATH_TOKEN = "{FS2_MODEL_CONTENT_PATH}"


def resource_placement_identity(value: Mapping[str, Any]) -> str:
    """Bind workload GPU allocation separately from its selected node/cache target."""

    gpu = value["resources"]["gpu"]
    payload = {
        "gpu_class": gpu["class"],
        "workload_allocation": {
            "count": gpu["count"],
            "topology": gpu["topology"],
        },
        "placement": gpu["placement"],
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _regular_bytes(path: Path, maximum: int = 1024 * 1024) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CatalogError(f"cannot stat catalog file {path}: {type(exc).__name__}") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise CatalogError(f"catalog input must be a regular non-symlink file: {path}")
    if info.st_size <= 0 or info.st_size > maximum:
        raise CatalogError(f"catalog input size is invalid: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CatalogError(f"cannot read catalog file {path}: {type(exc).__name__}") from exc


def _load_json(path: Path) -> dict[str, Any]:
    raw = _regular_bytes(path)
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"catalog input is not UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise CatalogError(f"catalog input is not an object: {path}")
    return value


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogError(f"{label} must be an object")
    actual = set(value)
    if actual != keys:
        raise CatalogError(
            f"{label} keys differ: missing={sorted(keys - actual)} extra={sorted(actual - keys)}"
        )
    return value


def _text(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise CatalogError(f"{label} must be non-empty bounded text")
    if any(ord(character) < 0x20 for character in value):
        raise CatalogError(f"{label} contains a control character")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise CatalogError(f"{label} must be boolean")
    return value


def _positive_int(value: Any, label: str, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CatalogError(f"{label} must be a positive integer")
    return value


def _enum(value: Any, allowed: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise CatalogError(f"{label} is outside the closed set {sorted(allowed)}")
    return value


def _optional_digest(value: Any, label: str, *, image: bool = False) -> str | None:
    if value is None:
        return None
    return strong_sha256(value, label, image=image)


def _list(value: Any, label: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        raise CatalogError(f"{label} must be {'a non-empty' if nonempty else 'an'} array")
    return value


def _validate_status_binding(value: Any, label: str) -> str:
    if not isinstance(value, dict):
        raise CatalogError(f"{label} must be an object")
    required = {"id", "state", "notes"}
    fields = frozenset(value)
    if fields not in {frozenset(required), frozenset(required | {"artifact"})}:
        raise CatalogError(f"{label} has unexpected or missing fields")
    binding = value
    _text(binding["id"], f"{label}.id")
    state = _enum(binding["state"], {"verified", "unverified", "blocked"}, f"{label}.state")
    _text(binding["notes"], f"{label}.notes")
    if "artifact" in binding:
        artifact = _exact(binding["artifact"], {"url", "sha256"}, f"{label}.artifact")
        url = _text(artifact["url"], f"{label}.artifact.url")
        if url is None or not url.startswith("https://"):
            raise CatalogError(f"{label}.artifact.url must be HTTPS")
        strong_sha256(artifact["sha256"], f"{label}.artifact.sha256")
    return state


def _validate_entitlement(value: Any) -> str:
    binding = _exact(
        value,
        {"required", "state", "credential_contract", "notes"},
        "model.source.entitlement",
    )
    required = _boolean(binding["required"], "model.source.entitlement.required")
    state = _enum(
        binding["state"],
        {"not-required", "unverified", "blocked", "verified"},
        "model.source.entitlement.state",
    )
    credential = _text(
        binding["credential_contract"],
        "model.source.entitlement.credential_contract",
        nullable=True,
    )
    _text(binding["notes"], "model.source.entitlement.notes")
    if required and state == "not-required":
        raise CatalogError("required entitlement cannot be not-required")
    if not required and state != "not-required":
        raise CatalogError("optional entitlement must be not-required")
    if required and credential is None:
        raise CatalogError("required entitlement needs a credential contract")
    return state


def _validate_image(value: Any) -> tuple[str, str | None, str | None]:
    image = _exact(value, {"reference", "digest", "state"}, "runtime.image")
    reference = _text(image["reference"], "runtime.image.reference", nullable=True)
    digest = _optional_digest(image["digest"], "runtime.image.digest", image=True)
    state = _enum(
        image["state"], {"resolved", "historical-redacted", "unresolved"}, "runtime.image.state"
    )
    if state == "resolved":
        if reference is None or digest is None or not reference.endswith("@" + digest):
            raise CatalogError("resolved runtime image must carry its exact digest")
    elif state == "historical-redacted":
        if reference is not None or digest is None:
            raise CatalogError("historical-redacted image must retain only its exact digest")
    elif reference is not None or digest is not None:
        raise CatalogError("unresolved image cannot invent a reference or digest")
    return state, reference, digest


def _validate_experiment(value: Any, gpu_count: int, index: int) -> tuple[str, str]:
    required = {
        "mechanism",
        "state",
        "gate",
        "artifact_kind",
        "artifact_manifest_digest",
        "reason",
    }
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(required),
        frozenset(required | {"historical_artifact"}),
    }:
        raise CatalogError(f"startup.experiments[{index}] has unexpected or missing fields")
    item = value
    mechanism = _enum(
        item["mechanism"], {"snapshot", "sleep-wake", "custom-runtime"}, "experiment.mechanism"
    )
    state = _enum(
        item["state"],
        {"gated", "qualified", "disabled-negative-evidence", "blocked"},
        "experiment.state",
    )
    gate = _text(item["gate"], "experiment.gate", nullable=True)
    artifact_kind = _enum(
        item["artifact_kind"], {"weights", "snapshot"}, "experiment.artifact_kind"
    )
    artifact_manifest = _optional_digest(
        item["artifact_manifest_digest"], "experiment.artifact_manifest_digest"
    )
    _text(item["reason"], "experiment.reason")
    expected_kind = "snapshot" if mechanism == "snapshot" else "weights"
    if artifact_kind != expected_kind:
        raise CatalogError("experiment artifact kind does not match its mechanism")
    if (state == "qualified") != (artifact_manifest is not None):
        raise CatalogError("only a qualified experiment may bind its immutable artifact manifest")
    if state in {"gated", "qualified"} and not gate:
        raise CatalogError("gated or qualified experiment requires an explicit gate")
    if state in {"disabled-negative-evidence", "blocked"} and gate is not None:
        raise CatalogError("disabled or blocked experiment cannot carry an admission gate")
    if mechanism == "snapshot" and state in {"gated", "qualified"}:
        if gpu_count != 1 or gate != "fs2-serve/exact-b300-single-gpu-runtime-tuple/v1":
            raise CatalogError("snapshot experiments are limited to the exact single-GPU B300 tuple gate")
    if mechanism == "sleep-wake" and state in {"gated", "qualified"}:
        if gate != "fs2-serve/exact-b300-resident-runtime-tuple/v1":
            raise CatalogError("sleep-wake requires the exact resident B300 runtime tuple gate")
    if mechanism == "custom-runtime" and state in {"gated", "qualified"}:
        if gate != "fs2-serve/exact-runtime-two-semantic-responses/v1":
            raise CatalogError("custom runtime requires its exact semantic runtime gate")
    historical = item.get("historical_artifact")
    if historical is not None:
        historical = _exact(
            historical,
            {
                "kind",
                "manifest_digest",
                "expanded_bytes",
                "file_count",
                "hardware",
                "compatibility",
                "file_signatures",
            },
            "experiment historical artifact",
        )
        if mechanism != "snapshot" or historical["kind"] != "snapshot":
            raise CatalogError("historical snapshot evidence cannot be relabeled as weights")
        strong_sha256(historical["manifest_digest"], "historical snapshot manifest")
        _positive_int(historical["expanded_bytes"], "historical snapshot expanded bytes")
        _positive_int(historical["file_count"], "historical snapshot file count")
        _text(historical["hardware"], "historical snapshot hardware")
        if historical["compatibility"] != "incompatible-with-b300":
            raise CatalogError("historical cross-GPU snapshot evidence must fail closed")
        signatures = _list(
            historical["file_signatures"], "historical snapshot file signatures", nonempty=True
        )
        if signatures != sorted(set(signatures)):
            raise CatalogError("historical snapshot file signatures must be sorted and unique")
    return mechanism, state


def _validate_artifact(value: Any) -> tuple[str, bool, str | None]:
    required = {
        "state",
        "kind",
        "manifest_digest",
        "expanded_bytes",
        "minimum_bytes",
        "capacity_bound_bytes",
        "staged",
    }
    optional = {"qualification_gate", "historical_inventory", "expected_identity"}
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or set(value) - required - optional
        or ("qualification_gate" in value) != ("historical_inventory" in value)
        or ("expected_identity" in value) != ("qualification_gate" in value)
    ):
        raise CatalogError("cache.artifact has unexpected, missing, or incomplete gate fields")
    artifact = value
    state = _enum(
        artifact["state"],
        {"unresolved", "historical-verified", "platform-verified", "blocked"},
        "cache.artifact.state",
    )
    kind = (
        None
        if artifact["kind"] is None
        else _enum(artifact["kind"], {"weights", "nim-cache"}, "cache.artifact.kind")
    )
    manifest = _optional_digest(artifact["manifest_digest"], "cache.artifact.manifest_digest")
    expanded = _positive_int(artifact["expanded_bytes"], "cache.artifact.expanded_bytes", nullable=True)
    minimum = _positive_int(artifact["minimum_bytes"], "cache.artifact.minimum_bytes", nullable=True)
    capacity_bound = _positive_int(
        artifact["capacity_bound_bytes"], "cache.artifact.capacity_bound_bytes", nullable=True
    )
    staged = _boolean(artifact["staged"], "cache.artifact.staged")
    if state in {"historical-verified", "platform-verified"} and (
        manifest is None or expanded is None
    ):
        raise CatalogError("verified artifact needs a manifest digest and exact size")
    if state not in {"historical-verified", "platform-verified"} and (
        manifest is not None or expanded is not None
    ):
        raise CatalogError("unresolved/blocked artifact cannot carry a verified manifest")
    if staged:
        raise CatalogError(
            "static catalog artifact.staged must remain false; live staging is an overlay receipt"
        )
    if minimum is not None and expanded is not None and minimum > expanded:
        raise CatalogError("artifact minimum bytes exceed exact expanded bytes")
    if capacity_bound is not None and expanded is not None and expanded > capacity_bound:
        raise CatalogError("artifact expanded bytes exceed the known cache capacity bound")
    qualification_gate = artifact.get("qualification_gate")
    historical_inventory = artifact.get("historical_inventory")
    if qualification_gate is not None:
        if qualification_gate != "fs2-serve/exact-hf-weight-per-file-sha256-manifest/v1":
            raise CatalogError("artifact qualification gate is not the exact HF weights gate")
        if kind != "weights":
            raise CatalogError("exact HF weights gate cannot apply to another artifact kind")
        inventory = _exact(
            historical_inventory,
            {
                "identity_sha256",
                "identity_scope",
                "file_count",
                "logical_bytes",
                "source_revision",
                "per_file_sha256_complete",
            },
            "cache artifact historical inventory",
        )
        strong_sha256(inventory["identity_sha256"], "historical inventory identity")
        if inventory["identity_scope"] != "canonical-path-and-size-only":
            raise CatalogError("historical weight inventory cannot claim content identity")
        _positive_int(inventory["file_count"], "historical inventory file count")
        logical_bytes = _positive_int(
            inventory["logical_bytes"], "historical inventory logical bytes"
        )
        _text(inventory["source_revision"], "historical inventory source revision")
        if inventory["per_file_sha256_complete"] is not False:
            raise CatalogError("path/size inventory cannot claim complete per-file hashes")
        if state == "unresolved" and minimum != logical_bytes:
            raise CatalogError("artifact minimum must preserve the historical logical byte floor")
        if manifest is not None and manifest == inventory["identity_sha256"]:
            raise CatalogError("path/size inventory identity cannot become an artifact manifest")
    return state, staged, kind


def _verify_repo_file(
    repo_root: Path,
    catalog_root: Path,
    relative: Any,
    digest: Any,
    label: str,
) -> None:
    path_value = _text(relative, f"{label}.path")
    expected = _optional_digest(digest, f"{label}.sha256")
    assert path_value is not None and expected is not None
    repository_path = repo_root / path_value
    package_prefix = "k8s-inference/catalog/runtime/"
    if path_value.startswith(package_prefix):
        package_path = catalog_root / path_value.removeprefix(package_prefix)
    else:
        installed_mirror = catalog_root / "repository" / path_value
        source_mirror = catalog_root / "packaged-repository" / path_value
        package_path = installed_mirror if installed_mirror.is_file() else source_mirror
    path = repository_path if repository_path.is_file() else package_path
    try:
        resolved = path.resolve()
        if path == repository_path:
            resolved.relative_to(repo_root.resolve())
        else:
            resolved.relative_to(catalog_root.resolve())
    except ValueError as exc:
        raise CatalogError(f"{label} escapes the repository/package") from exc
    actual = hashlib.sha256(_regular_bytes(path, maximum=32 * 1024 * 1024)).hexdigest()
    if actual != expected:
        raise CatalogError(f"{label} digest mismatch: {path_value}")


def _validate_semantic(value: Any, repo_root: Path, catalog_root: Path) -> None:
    semantic = _exact(
        value,
        {
            "kind",
            "contract",
            "source_path",
            "source_sha256",
            "fixture_path",
            "fixture_sha256",
            "request_count",
            "distinct_requests",
            "distinct_responses",
        },
        "semantic_validator",
    )
    kind = _enum(
        semantic["kind"],
        {"repository-command", "openai-exact", "png", "medical-nonclinical", "sequence", "blocked"},
        "semantic_validator.kind",
    )
    _text(semantic["contract"], "semantic_validator.contract")
    if semantic["request_count"] != 2:
        raise CatalogError("semantic validator must issue exactly two requests")
    if semantic["distinct_requests"] is not True or semantic["distinct_responses"] is not True:
        raise CatalogError("semantic validator must require distinct requests and responses")
    source_path = semantic["source_path"]
    source_digest = semantic["source_sha256"]
    fixture_path = semantic["fixture_path"]
    fixture_digest = semantic["fixture_sha256"]
    if kind == "repository-command":
        if source_path is None or source_digest is None:
            raise CatalogError("repository-command validator needs a pinned source file")
        _verify_repo_file(
            repo_root, catalog_root, source_path, source_digest, "semantic validator"
        )
    elif source_path is not None or source_digest is not None:
        raise CatalogError("inline semantic validator cannot carry a repository source")
    if (fixture_path is None) != (fixture_digest is None):
        raise CatalogError("semantic fixture path and digest must be paired")
    if fixture_path is not None:
        _verify_repo_file(repo_root, catalog_root, fixture_path, fixture_digest, "semantic fixture")


def _validate_evidence(value: Any, index: int) -> None:
    item = _exact(
        value,
        {"classification", "hardware", "outcome", "source_commit", "summary"},
        f"evidence[{index}]",
    )
    _enum(
        item["classification"],
        {"measured-historical", "measured-platform", "negative", "blocked", "unverified"},
        "evidence.classification",
    )
    _text(item["hardware"], "evidence.hardware", nullable=True)
    _enum(
        item["outcome"],
        {
            "conventional-fallback",
            "snapshot-candidate",
            "snapshot-rejected",
            "resident-candidate",
            "custom-runtime-unqualified",
            "live-qualified",
            "no-live-lane",
            "unqualified",
        },
        "evidence.outcome",
    )
    if not isinstance(item["source_commit"], str) or GIT_OBJECT.fullmatch(item["source_commit"]) is None:
        raise CatalogError("evidence source commit is not exact")
    _text(item["summary"], "evidence.summary")


@functools.lru_cache(maxsize=512)
def _verify_git_provenance(
    repo_root: str,
    commit: str,
    tree: str,
    path: str,
    content_sha256: str,
) -> None:
    try:
        actual_tree = subprocess.run(
            ["git", "-C", repo_root, "show", "-s", "--format=%T", commit],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        ).stdout.strip()
        content = subprocess.run(
            ["git", "-C", repo_root, "show", f"{commit}:{path}"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise CatalogError(f"provenance object is unavailable: {commit}:{path}") from exc
    if actual_tree != tree:
        raise CatalogError(f"provenance tree does not match commit: {commit}")
    if hashlib.sha256(content).hexdigest() != content_sha256:
        raise CatalogError(f"provenance content differs from the packaged lock: {commit}:{path}")


def _validate_provenance(
    value: Any,
    index: int,
    repo_root: Path,
    provenance_lock: Mapping[tuple[str, str, str, str], str],
    used_provenance: set[tuple[str, str, str, str]],
) -> None:
    if isinstance(value, dict) and set(value) == {"url", "revision", "classification"}:
        item = value
        url = _text(item["url"], "provenance.url")
        revision = item["revision"]
        if (
            url is None
            or not url.startswith("https://")
            or not isinstance(revision, str)
            or GIT_OBJECT.fullmatch(revision) is None
            or revision not in url
            or item["classification"] != "reviewed-input"
        ):
            raise CatalogError("external provenance must bind an immutable HTTPS revision")
        key = (url, revision, "external", item["classification"])
        if key not in provenance_lock:
            raise CatalogError("external provenance is absent from the packaged provenance lock")
        used_provenance.add(key)
        return
    item = _exact(value, {"commit", "tree", "path", "classification"}, f"provenance[{index}]")
    for key in ("commit", "tree"):
        if not isinstance(item[key], str) or GIT_OBJECT.fullmatch(item[key]) is None:
            raise CatalogError(f"provenance {key} is not an exact Git object")
    path = _text(item["path"], "provenance.path")
    classification = _enum(
        item["classification"], {"reviewed-input", "measured-handoff", "negative-evidence"},
        "provenance.classification",
    )
    assert path is not None
    key = (item["commit"], item["tree"], path, classification)
    try:
        content_sha256 = provenance_lock[key]
    except KeyError as exc:
        raise CatalogError("model provenance is absent from the packaged provenance lock") from exc
    used_provenance.add(key)
    if (repo_root / ".git").exists():
        _verify_git_provenance(
            str(repo_root.resolve()), item["commit"], item["tree"], path, content_sha256
        )


def _validate_model(
    value: dict[str, Any],
    filename: str,
    repo_root: Path,
    catalog_root: Path,
    provenance_lock: Mapping[tuple[str, str, str, str], str],
    used_provenance: set[tuple[str, str, str, str]],
) -> dict[str, Any]:
    record = _exact(
        value,
        {
            "schema",
            "model",
            "runtime",
            "resources",
            "interface",
            "startup",
            "cache",
            "semantic_validator",
            "support",
            "evidence",
            "provenance",
        },
        filename,
    )
    if record["schema"] != MODEL_SCHEMA:
        raise CatalogError(f"{filename} uses an unsupported schema")

    model = _exact(record["model"], {"id", "display_name", "family", "tested_lane", "source"}, "model")
    model_id = model["id"]
    if not isinstance(model_id, str) or MODEL_ID.fullmatch(model_id) is None:
        raise CatalogError("model.id is not canonical")
    if filename != f"{model_id}.json":
        raise CatalogError("model filename must match model.id")
    _text(model["display_name"], "model.display_name")
    _enum(model["family"], SUPPORTED_MODEL_FAMILIES, "model.family")
    tested_lane = _boolean(model["tested_lane"], "model.tested_lane")
    source = _exact(model["source"], {"kind", "repository", "revision", "license", "entitlement"}, "model.source")
    source_kind = _enum(
        source["kind"], {"ngc-nim", "huggingface", "git"}, "model.source.kind"
    )
    _text(source["repository"], "model.source.repository", nullable=True)
    revision = _text(source["revision"], "model.source.revision", nullable=True)
    license_state = _validate_status_binding(source["license"], "model.source.license")
    entitlement_state = _validate_entitlement(source["entitlement"])
    license_artifact = source["license"].get("artifact")
    if source_kind == "huggingface" and license_artifact is not None:
        expected_license_url = (
            f"https://huggingface.co/{source['repository']}/raw/{revision}/LICENSE"
        )
        if license_artifact["url"] != expected_license_url:
            raise CatalogError(
                "Hugging Face license artifact URL must bind the exact repository revision"
            )
    immutable_revision = (
        IMAGE_DIGEST.fullmatch(revision or "")
        if source_kind == "ngc-nim"
        else GIT_OBJECT.fullmatch(revision or "")
    )
    if tested_lane:
        if immutable_revision is None:
            raise CatalogError("tested lane source revision must be immutable for its source kind")

    runtime = _exact(record["runtime"], {"kind", "version", "image", "command"}, "runtime")
    runtime_kind = _enum(
        runtime["kind"],
        {"nim", "vllm", "vllm-omni", "custom", "diffusers", "unresolved"},
        "runtime.kind",
    )
    _text(runtime["version"], "runtime.version", nullable=True)
    image_state, _, image_digest = _validate_image(runtime["image"])
    command = _list(runtime["command"], "runtime.command")
    if not all(isinstance(item, str) and item for item in command):
        raise CatalogError("runtime.command must be direct non-empty argv strings")
    if tested_lane and source_kind == "ngc-nim" and image_digest != revision:
        raise CatalogError("NIM runtime image digest must equal its immutable NIM revision")
    if tested_lane and source_kind == "huggingface" and runtime_kind == "vllm":
        repository = source["repository"]
        if command.count(MODEL_CONTENT_PATH_TOKEN) != 1:
            raise CatalogError("vLLM argv must consume the exact mounted artifact path")
        if repository in command or "--revision" in command or any(
            item.startswith("--revision=") for item in command
        ):
            raise CatalogError("vLLM argv must not reference or redownload a remote model")
        try:
            alias_index = command.index("--served-model-name") + 1
        except ValueError as exc:
            raise CatalogError("vLLM argv must define the canonical served-model alias") from exc
        if alias_index >= len(command) or command[alias_index] != model_id:
            raise CatalogError("vLLM argv served-model alias must equal model.id")
    if tested_lane and source_kind == "huggingface" and runtime_kind == "vllm-omni":
        repository = source["repository"]
        expected_command = [
            "vllm",
            "serve",
            repository,
            "--omni",
            "--revision",
            revision,
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--init-timeout",
            "1800",
            "--no-guardrails",
            "--model-class-name",
            "Cosmos3OmniDiffusersPipeline",
        ]
        if command != expected_command:
            raise CatalogError("vLLM-Omni argv differs from the reviewed immutable launch")

    resources = _exact(
        record["resources"],
        {"cpu_millis", "memory_bytes", "host_ram_min_bytes", "scaler_owner", "gpu"},
        "resources",
    )
    _positive_int(resources["cpu_millis"], "resources.cpu_millis", nullable=True)
    _positive_int(resources["memory_bytes"], "resources.memory_bytes", nullable=True)
    _positive_int(resources["host_ram_min_bytes"], "resources.host_ram_min_bytes", nullable=True)
    if resources["scaler_owner"] != "nebius-managed-node-group-autoscaler":
        raise CatalogError("the provider node-group autoscaler is the sole scaler owner")
    gpu = _exact(
        resources["gpu"],
        {"class", "count", "topology", "placement", "b300_state", "alternatives"},
        "resources.gpu",
    )
    if gpu["class"] != "NVIDIA-B300-SXM6-288GB":
        raise CatalogError("platform GPU class must be the B300 target")
    gpu_count = _positive_int(gpu["count"], "resources.gpu.count")
    assert gpu_count is not None
    if gpu_count > 8:
        raise CatalogError("GPU count exceeds one target node")
    topology = _enum(gpu["topology"], {"single-gpu", "single-node-multi-gpu"}, "resources.gpu.topology")
    if (gpu_count == 1) != (topology == "single-gpu"):
        raise CatalogError("GPU count and topology disagree")
    placement = gpu["placement"]
    if placement is not None:
        placement = _exact(
            placement,
            {
                "pool",
                "capacity_type",
                "node_gpu_count",
                "node_preset",
                "node_selector",
                "tolerations",
                "cache_capabilities",
                "qualification_sequence",
                "provider_block_pvc",
                "local_pv_pvc",
            },
            "resources.gpu.placement",
        )
        pool = _enum(
            placement["pool"],
            {"b300-hot-8x", "b300-burst-8x", "b300-burst-1x"},
            "resources.gpu.placement.pool",
        )
        capacity_type = _enum(
            placement["capacity_type"],
            {"regular", "preemptible"},
            "resources.gpu.placement.capacity_type",
        )
        node_count = _positive_int(
            placement["node_gpu_count"], "resources.gpu.placement.node_gpu_count"
        )
        assert node_count is not None
        if node_count not in {1, 8} or gpu_count > node_count:
            raise CatalogError("workload GPU allocation exceeds the selected B300 node")
        node_preset = _enum(
            placement["node_preset"],
            {"b300-1x", "b300-8x"},
            "resources.gpu.placement.node_preset",
        )
        expected_pool = {
            "b300-hot-8x": ("regular", 8, "b300-8x", "hot"),
            "b300-burst-8x": ("preemptible", 8, "b300-8x", "burst"),
            "b300-burst-1x": ("preemptible", 1, "b300-1x", "burst"),
        }[pool]
        if (capacity_type, node_count, node_preset) != expected_pool[:3]:
            raise CatalogError("GPU placement pool, capacity, node count, and preset disagree")
        selector = placement["node_selector"]
        if (
            not isinstance(selector, dict)
            or list(selector) != sorted(selector)
            or any(
                not isinstance(key, str)
                or not key
                or not isinstance(item, str)
                or not item
                for key, item in selector.items()
            )
        ):
            raise CatalogError("GPU placement selector must contain canonical string labels")
        expected_selector = {
            "capacity.fs2.nebius/gpu-count": str(node_count),
            "capacity.fs2.nebius/pool": expected_pool[3],
            "capacity.fs2.nebius/preset": node_preset,
            "capacity.fs2.nebius/type": capacity_type,
            "workload.fs2.nebius/gpu": "true",
        }
        if selector != dict(sorted(expected_selector.items())):
            raise CatalogError("GPU placement differs from the cluster-owned stable labels")
        if placement["tolerations"] != [
            {
                "key": "dedicated",
                "operator": "Equal",
                "value": "fs2-inference",
                "effect": "NoSchedule",
            }
        ]:
            raise CatalogError("GPU placement lacks the exact cluster-owned GPU taint toleration")
        capabilities = _list(
            placement["cache_capabilities"],
            "resources.gpu.placement.cache_capabilities",
            nonempty=True,
        )
        if capabilities != sorted(set(capabilities)) or any(
            item
            not in {
                "provider-block-pvc-candidate",
                "provider-block-pvc-qualified",
                "sfs-conventional-qualified",
                "node-local-pv-pvc-qualified",
            }
            for item in capabilities
        ):
            raise CatalogError("GPU placement cache capabilities are not canonical")
        provider_block_pvc = _exact(
            placement["provider_block_pvc"],
            {
                "state",
                "contract",
                "storage_class",
                "claim",
                "acquisition",
                "runtime",
                "qualification_gates",
            },
            "resources.gpu.placement.provider_block_pvc",
        )
        provider_state = _enum(
            provider_block_pvc["state"],
            {"candidate-unqualified", "qualified"},
            "provider block PVC state",
        )
        if provider_block_pvc["contract"] != "fs2-serve.nebius.ai/provider-block-pvc/v1":
            raise CatalogError("provider block PVC contract is not canonical")
        storage_class = _exact(
            provider_block_pvc["storage_class"],
            {
                "name",
                "owner",
                "provisioner",
                "reclaim_policy",
                "volume_binding_mode",
                "allow_volume_expansion",
                "parameters",
                "default_delete_class_policy",
            },
            "provider block StorageClass",
        )
        if storage_class != {
            "name": "fs2-network-ssd-retain",
            "owner": "fs2-serve-cluster",
            "provisioner": "compute.csi.nebius.com",
            "reclaim_policy": "Retain",
            "volume_binding_mode": "WaitForFirstConsumer",
            "allow_volume_expansion": True,
            "parameters": {
                "type": "NETWORK_SSD",
                "csi.storage.k8s.io/fstype": "ext4",
            },
            "default_delete_class_policy": "canary-only-with-deletion-protection",
        }:
            raise CatalogError("provider block StorageClass is not protected Retain WFFC")
        claim = _exact(
            provider_block_pvc["claim"],
            {"namespace", "name", "requested_bytes", "access_modes", "volume_mode", "fs_type"},
            "provider block PVC claim",
        )
        if claim != {
            "namespace": "fs2-models",
            "name": "qwen3-8b-weights",
            "requested_bytes": 64 * 1024**3,
            "access_modes": ["ReadWriteOnce"],
            "volume_mode": "Filesystem",
            "fs_type": "ext4",
        }:
            raise CatalogError("provider block PVC claim differs from the reviewed Qwen shape")
        acquisition_contract = _exact(
            provider_block_pvc["acquisition"],
            {"first_consumer", "sole_writer", "gpu_count", "placement_source", "atomic_publication"},
            "provider block acquisition contract",
        )
        if acquisition_contract != {
            "first_consumer": True,
            "sole_writer": True,
            "gpu_count": 0,
            "placement_source": "resources.gpu.placement.node_selector+tolerations",
            "atomic_publication": "scratch-to-clean-payload-content-address",
        }:
            raise CatalogError("provider block acquisition must be the targeted zero-GPU sole writer")
        runtime_storage = _exact(
            provider_block_pvc["runtime"],
            {"read_only", "gpu_count", "claim_reuse"},
            "provider block runtime contract",
        )
        if runtime_storage != {
            "read_only": True,
            "gpu_count": gpu_count,
            "claim_reuse": "same-pvc-uid-and-volume-name",
        }:
            raise CatalogError("provider block runtime must reuse the exact claim read-only")
        if provider_block_pvc["qualification_gates"] != [
            "bound-after-targeted-acquirer",
            "content-manifest-verified",
            "detach-reattach-controlled-node-replacement",
            "no-multi-attach",
            "sole-writer-handoff-closed",
            "two-distinct-semantic-responses",
            "scale-one-to-zero-claim-retained",
        ]:
            raise CatalogError("provider block PVC qualification gates are incomplete")
        local_pv_pvc = _exact(
            placement["local_pv_pvc"],
            {
                "state",
                "contract",
                "storage_class_name",
                "volume_binding_mode",
                "node_affinity",
                "preemption_fencing",
                "lost_node_fencing",
                "activation_generation_recreation",
                "model_pod_volume_type",
                "host_path_forbidden",
            },
            "resources.gpu.placement.local_pv_pvc",
        )
        local_pv_state = _enum(
            local_pv_pvc["state"],
            {"gated-unimplemented", "reviewed-implemented"},
            "local-PV/PVC lifecycle state",
        )
        expected_local_pv = {
            "contract": "fs2-serve.nebius.ai/local-pv-pvc-lifecycle/v1",
            "storage_class_name": "fs2-local-nvme",
            "volume_binding_mode": "WaitForFirstConsumer",
            "node_affinity": "exact-serving-node-required",
            "preemption_fencing": "pod-pvc-pv-node-uid",
            "lost_node_fencing": "invalidate-and-recreate-next-activation-generation",
            "activation_generation_recreation": True,
            "model_pod_volume_type": "persistentVolumeClaim",
            "host_path_forbidden": True,
        }
        if any(local_pv_pvc[key] != item for key, item in expected_local_pv.items()):
            raise CatalogError("local-PV/PVC lifecycle contract is not the reviewed fail-closed shape")
        sequence = _list(
            placement["qualification_sequence"],
            "resources.gpu.placement.qualification_sequence",
            nonempty=True,
        )
        expected_modes: list[str] = []
        for sequence_index, sequence_value in enumerate(sequence, start=1):
            item = _exact(
                sequence_value,
                {
                    "order",
                    "storage_mode",
                    "startup_mechanism",
                    "cohort_binding",
                    "state",
                    "qualification_gate",
                },
                f"GPU placement qualification sequence {sequence_index}",
            )
            if item["order"] != sequence_index or item["startup_mechanism"] != "conventional":
                raise CatalogError("GPU placement qualification order is not contiguous conventional-first")
            mode = _enum(
                item["storage_mode"],
                {"provider-block-pvc", "sfs-pvc", "local-nvme"},
                "GPU placement qualification storage mode",
            )
            expected_binding = {
                "provider-block-pvc": "provider-block-pvc-lifecycle",
                "sfs-pvc": "replacement-node-rwx-canary",
                "local-nvme": "activation-generation-pvc",
            }[mode]
            if item["cohort_binding"] != expected_binding:
                raise CatalogError("GPU placement cohort binding differs from its storage scope")
            sequence_state = _enum(
                item["state"],
                {"candidate-unqualified", "gated-unqualified", "qualified"},
                "GPU placement qualification state",
            )
            expected_sequence_state = (
                provider_state
                if mode == "provider-block-pvc"
                else "qualified"
                if (
                    mode == "sfs-pvc"
                    and "sfs-conventional-qualified" in capabilities
                )
                or (
                    mode == "local-nvme"
                    and local_pv_state == "reviewed-implemented"
                    and "node-local-pv-pvc-qualified" in capabilities
                )
                else "gated-unqualified"
            )
            if sequence_state != expected_sequence_state:
                raise CatalogError("GPU placement sequence overstates an unqualified storage cohort")
            _text(item["qualification_gate"], "GPU placement qualification gate")
            expected_modes.append(mode)
        expected_capabilities = [
            "provider-block-pvc-qualified"
            if provider_state == "qualified"
            else "provider-block-pvc-candidate"
        ]
        if any(
            item["storage_mode"] == "sfs-pvc" and item["state"] == "qualified"
            for item in sequence
        ):
            expected_capabilities.append("sfs-conventional-qualified")
        if any(
            item["storage_mode"] == "local-nvme" and item["state"] == "qualified"
            for item in sequence
        ):
            expected_capabilities.append("node-local-pv-pvc-qualified")
        if set(expected_capabilities) != set(capabilities):
            raise CatalogError("GPU placement qualification sequence differs from cache capabilities")
        if expected_modes[0] != "provider-block-pvc":
            raise CatalogError("protected provider block PVC must be the first Qwen cohort")
        if node_count == 1 and (
            "local-nvme" in expected_modes or "provider-block-pvc" in expected_modes
        ):
            raise CatalogError("the one-B300 pool cannot claim provider-block or node-local storage")
        if "local-nvme" in expected_modes and node_count != 8:
            raise CatalogError("node-local NVMe qualification requires an eight-B300 node")
    b300_state = _enum(
        gpu["b300_state"],
        {
            "unverified",
            "historical-supported",
            "incompatible-sm103",
            "blocked",
            "qualified",
        },
        "resources.gpu.b300_state",
    )
    for alternative_index, alternative_value in enumerate(
        _list(gpu["alternatives"], "resources.gpu.alternatives")
    ):
        alternative = _exact(
            alternative_value,
            {"id", "count", "topology", "placement", "state", "source_commit", "notes"},
            f"resources.gpu.alternatives[{alternative_index}]",
        )
        _text(alternative["id"], "GPU alternative ID")
        alternative_placement = _exact(
            alternative["placement"],
            {"node_gpu_count", "node_preset"},
            "GPU alternative placement",
        )
        if alternative_placement != {"node_gpu_count": 8, "node_preset": "b300-8x"}:
            raise CatalogError("multi-GPU alternative requires the eight-B300 node placement")
        alternative_count = _positive_int(alternative["count"], "GPU alternative count")
        if alternative_count is None or alternative_count < 2 or alternative_count > 8:
            raise CatalogError("GPU alternative count must be between two and eight")
        if alternative["topology"] != "single-node-multi-gpu":
            raise CatalogError("GPU alternative must be single-node multi-GPU")
        _enum(
            alternative["state"],
            {"measured-historical-fallback", "unverified", "negative"},
            "GPU alternative state",
        )
        if not isinstance(alternative["source_commit"], str) or GIT_OBJECT.fullmatch(
            alternative["source_commit"]
        ) is None:
            raise CatalogError("GPU alternative source commit is not exact")
        _text(alternative["notes"], "GPU alternative notes")

    interface = _exact(
        record["interface"],
        {"execution_mode", "protocols", "endpoints", "readiness", "warmup", "policy", "mcp"},
        "interface",
    )
    execution_mode = _enum(
        interface["execution_mode"], {"http", "batch", "unavailable"}, "interface.execution_mode"
    )
    protocols = _list(interface["protocols"], "interface.protocols")
    allowed_protocols = {
        "native",
        "openai-chat",
        "openai-completions",
        "openai-embeddings",
        "openai-images",
    }
    if protocols != sorted(protocols) or len(protocols) != len(set(protocols)):
        raise CatalogError("interface protocols must be sorted and unique")
    if any(protocol not in allowed_protocols for protocol in protocols):
        raise CatalogError("interface protocol is unsupported")
    endpoints = interface["endpoints"]
    if not isinstance(endpoints, dict) or set(endpoints) != set(protocols):
        raise CatalogError("interface endpoints must exactly cover protocols")
    for protocol, endpoint in endpoints.items():
        endpoint = canonical_http_path(endpoint, f"interface.endpoints.{protocol}")
        standard = {
            "openai-chat": "/v1/chat/completions",
            "openai-completions": "/v1/completions",
            "openai-embeddings": "/v1/embeddings",
            "openai-images": "/v1/images/generations",
        }.get(protocol)
        if standard is not None and endpoint != standard:
            raise CatalogError(f"{protocol} endpoint differs from the canonical protocol path")

    def validate_probe(probe_value: Any, label: str, *, nullable: bool) -> None:
        if nullable and probe_value is None:
            return
        probe = _exact(probe_value, {"method", "path", "expected_status", "timeout_seconds"}, label)
        _enum(probe["method"], {"GET", "POST"}, f"{label}.method")
        canonical_http_path(probe["path"], f"{label}.path")
        status = _positive_int(probe["expected_status"], f"{label}.expected_status")
        timeout = _positive_int(probe["timeout_seconds"], f"{label}.timeout_seconds")
        if status is None or status > 599 or timeout is None or timeout > 3600:
            raise CatalogError(f"{label} status or timeout is outside the closed bounds")

    if execution_mode == "unavailable":
        if protocols or endpoints or interface["readiness"] is not None or interface["warmup"] is not None:
            raise CatalogError("unavailable interface cannot imply a protocol or probe")
    else:
        if not protocols:
            raise CatalogError("available interface requires at least one protocol")
        validate_probe(interface["readiness"], "interface.readiness", nullable=False)
        validate_probe(interface["warmup"], "interface.warmup", nullable=True)
    policy = _exact(
        interface["policy"],
        {"operations", "license_enforced", "non_clinical", "commercial_use"},
        "interface.policy",
    )
    operations = _list(policy["operations"], "interface.policy.operations")
    if operations != sorted(operations) or len(operations) != len(set(operations)):
        raise CatalogError("policy operations must be sorted and unique")
    for operation in operations:
        _text(operation, "policy operation")
    if policy["license_enforced"] is not True:
        raise CatalogError("every listing and invocation must enforce license policy")
    policy_non_clinical = _boolean(policy["non_clinical"], "interface.policy.non_clinical")
    commercial_use = _enum(
        policy["commercial_use"],
        {"allowed", "license-dependent", "prohibited", "blocked"},
        "interface.policy.commercial_use",
    )
    mcp = _exact(interface["mcp"], {"discoverable", "invocable"}, "interface.mcp")
    mcp_discoverable = _boolean(mcp["discoverable"], "interface.mcp.discoverable")
    mcp_invocable = _boolean(mcp["invocable"], "interface.mcp.invocable")
    if execution_mode == "unavailable":
        if operations or mcp_discoverable or commercial_use != "blocked":
            raise CatalogError("unavailable interface must remain blocked")
    elif not operations:
        raise CatalogError("tested interface requires policy operations")

    startup = _exact(
        record["startup"], {"default", "fallback", "enabled_mechanisms", "experiments", "multi_gpu_criu"}, "startup"
    )
    if startup["default"] != "conventional" or startup["fallback"] != "conventional":
        raise CatalogError("conventional launch must remain the universal default/fallback")
    enabled = _list(startup["enabled_mechanisms"], "startup.enabled_mechanisms")
    allowed_mechanisms = {"conventional", "snapshot", "sleep-wake", "custom-runtime"}
    if len(enabled) != len(set(enabled)) or any(item not in allowed_mechanisms for item in enabled):
        raise CatalogError("enabled startup mechanisms are invalid")
    if startup["multi_gpu_criu"] != "unproven-disabled":
        raise CatalogError("multi-GPU CRIU cannot be claimed")
    experiment_states: dict[str, str] = {}
    for index, experiment in enumerate(_list(startup["experiments"], "startup.experiments")):
        mechanism, state = _validate_experiment(experiment, gpu_count, index)
        if mechanism in experiment_states:
            raise CatalogError("a startup mechanism may have only one experiment record")
        experiment_states[mechanism] = state
    accelerated = set(enabled) - {"conventional"}
    qualified_experiments = {
        mechanism for mechanism, state in experiment_states.items() if state == "qualified"
    }
    if tested_lane:
        if "conventional" not in enabled or accelerated != qualified_experiments:
            raise CatalogError(
                "enabled accelerated mechanisms must exactly match qualified experiments"
            )
    elif enabled or qualified_experiments:
        raise CatalogError("an untested candidate cannot enable a startup mechanism")

    cache = _exact(record["cache"], {"owner", "shared_path", "local_path", "pre_pull_image", "artifact"}, "cache")
    cache_owner = _enum(
        cache["owner"],
        {NIM_CACHE_OWNER, LOCALIZER_CACHE_OWNER, BLOCKED_CACHE_OWNER},
        "cache.owner",
    )
    shared_path = _text(cache["shared_path"], "cache.shared_path")
    local_path = _text(cache["local_path"], "cache.local_path")
    expected_shared = f"/mnt/fs2-serve-cache/models/{model_id}"
    expected_local = f"/var/lib/fs2-serve/cache/models/{model_id}"
    if shared_path != expected_shared or local_path != expected_local:
        raise CatalogError("cache paths must be model-scoped canonical paths")
    _boolean(cache["pre_pull_image"], "cache.pre_pull_image")
    artifact_state, _, artifact_kind = _validate_artifact(cache["artifact"])
    _validate_semantic(record["semantic_validator"], repo_root, catalog_root)

    support = _exact(record["support"], {"state", "route_exposed", "non_clinical", "limitations"}, "support")
    support_state = _enum(support["state"], {"unqualified", "blocked", "qualified"}, "support.state")
    route_exposed = _boolean(support["route_exposed"], "support.route_exposed")
    support_non_clinical = _boolean(support["non_clinical"], "support.non_clinical")
    if policy_non_clinical != support_non_clinical:
        raise CatalogError("listing policy and support non-clinical flags disagree")
    for limitation in _list(support["limitations"], "support.limitations", nonempty=True):
        _text(limitation, "support limitation")

    evidence = _list(record["evidence"], "evidence", nonempty=True)
    for index, item in enumerate(evidence):
        _validate_evidence(item, index)
    provenance = _list(record["provenance"], "provenance", nonempty=True)
    for index, item in enumerate(provenance):
        _validate_provenance(item, index, repo_root, provenance_lock, used_provenance)

    expected_cache_owner = (
        BLOCKED_CACHE_OWNER
        if support_state == "blocked"
        else NIM_CACHE_OWNER
        if source_kind == "ngc-nim" and runtime_kind == "nim"
        else LOCALIZER_CACHE_OWNER
    )
    if cache_owner != expected_cache_owner:
        raise CatalogError(
            f"cache owner must be {expected_cache_owner} for {model_id}; "
            "NIMCache and the fs2 localizer cannot share a path"
        )
    expected_artifact_kind = (
        None
        if cache_owner == BLOCKED_CACHE_OWNER
        else "nim-cache"
        if cache_owner == NIM_CACHE_OWNER
        else "weights"
    )
    if artifact_kind != expected_artifact_kind:
        raise CatalogError("conventional cache artifact kind differs from its sole cache owner")

    if model_id == "qwen3-8b":
        qwen_artifact = cache["artifact"]
        qwen_experiment = startup["experiments"][0]
        expected_inventory = {
            "identity_sha256": "1e89964f62cbba0c316f76db2e4ea56d2a79fcf5b8ec678bec48d53c457a30cc",
            "identity_scope": "canonical-path-and-size-only",
            "file_count": 15,
            "logical_bytes": 16_397_461_266,
            "source_revision": "b968826d9c46dd6066d109eabc6255188de91218",
            "per_file_sha256_complete": False,
        }
        expected_snapshot = {
            "kind": "snapshot",
            "manifest_digest": "76b8845141df43882a142f9085ff233a0b5bf27b55f19ae385dd9ac88dab6394",
            "expanded_bytes": 27_306_999_047,
            "file_count": 505,
            "hardware": "NVIDIA H100 80GB HBM3",
            "compatibility": "incompatible-with-b300",
            "file_signatures": ["cgroup.img", "core-*.img"],
        }
        expected_identity = _exact(
            qwen_artifact.get("expected_identity"),
            {
                "state",
                "source_url",
                "source_revision",
                "file_count",
                "expanded_bytes",
                "content_digest",
                "manifest_digest",
                "payload_policy",
                "files",
            },
            "Qwen expected artifact identity",
        )
        expected_revision = "b968826d9c46dd6066d109eabc6255188de91218"
        expected_content_digest = (
            "5b0f0f64ddb02ee2deeed4772968b9e2139a922acc9b9bb9c3488d23c678971d"
        )
        expected_manifest_digest = (
            "2cf721c69d9e1b66860274de129f0dd486172ef1dad289483ea891dab5b80806"
        )
        expected_files = _list(
            expected_identity["files"], "Qwen expected artifact files", nonempty=True
        )
        canonical_files: list[dict[str, Any]] = []
        source_identities: dict[str, str] = {}
        for file_index, raw_file in enumerate(expected_files):
            item = _exact(
                raw_file,
                {"path", "bytes", "sha256", "source_identity"},
                f"Qwen expected artifact files[{file_index}]",
            )
            path = _text(item["path"], "Qwen expected artifact path")
            size = _positive_int(item["bytes"], "Qwen expected artifact bytes")
            digest = strong_sha256(item["sha256"], "Qwen expected artifact SHA-256")
            source_identity = _enum(
                item["source_identity"],
                {"git-exact-revision-streamed-sha256", "lfs-oid-sha256"},
                "Qwen expected artifact source identity",
            )
            assert path is not None and size is not None
            canonical_files.append({"path": path, "bytes": size, "sha256": digest})
            source_identities[path] = source_identity
        lfs_paths = {
            "model-00001-of-00005.safetensors",
            "model-00002-of-00005.safetensors",
            "model-00003-of-00005.safetensors",
            "model-00004-of-00005.safetensors",
            "model-00005-of-00005.safetensors",
            "tokenizer.json",
        }
        if (
            expected_identity["state"] != "expected-only-unverified"
            or expected_identity["source_url"]
            != "https://huggingface.co/api/models/Qwen/Qwen3-8B/tree/"
            + expected_revision
            + "?recursive=true&expand=true"
            or expected_identity["source_revision"] != expected_revision
            or expected_identity["file_count"] != 15
            or expected_identity["expanded_bytes"] != 16_397_461_266
            or expected_identity["content_digest"] != expected_content_digest
            or expected_identity["manifest_digest"] != expected_manifest_digest
            or expected_identity["payload_policy"]
            != "scratch-to-clean-exact-allowlist-regular-files-only"
            or [item["path"] for item in canonical_files]
            != sorted(item["path"] for item in canonical_files)
            or len(set(source_identities)) != 15
            or {path for path, source in source_identities.items() if source == "lfs-oid-sha256"}
            != lfs_paths
            or sum(item["bytes"] for item in canonical_files) != 16_397_461_266
            or hashlib.sha256(_canonical_bytes(canonical_files)).hexdigest()
            != expected_content_digest
        ):
            raise CatalogError("Qwen expected identity differs from the exact-revision HF tree")
        expected_manifest = {
            "schema": "fs2-serve.nebius.ai/artifact-manifest/v1",
            "model_id": "qwen3-8b",
            "kind": "weights",
            "source": {"uri": "hf://Qwen/Qwen3-8B", "revision": expected_revision},
            "content": {
                "digest": expected_content_digest,
                "expanded_bytes": 16_397_461_266,
                "files": canonical_files,
            },
            "license": {"id": "apache-2.0", "state": "verified"},
            "entitlement_state": "not-required",
            "owner": "fs2-serve-localizer",
            "retention": "retained-platform",
        }
        if hashlib.sha256(_canonical_bytes(expected_manifest)).hexdigest() != expected_manifest_digest:
            raise CatalogError("Qwen expected canonical manifest digest does not reconcile")
        if (
            qwen_artifact.get("qualification_gate")
            != "fs2-serve/exact-hf-weight-per-file-sha256-manifest/v1"
            or qwen_artifact.get("historical_inventory") != expected_inventory
            or qwen_experiment.get("historical_artifact") != expected_snapshot
            or qwen_artifact["manifest_digest"]
            == expected_snapshot["manifest_digest"]
        ):
            raise CatalogError(
                "Qwen must separate the path/size weight inventory from the H100 CRIU snapshot"
            )
        if support_state != "qualified" and (
            artifact_state != "unresolved"
            or qwen_artifact["manifest_digest"] is not None
            or qwen_artifact["expanded_bytes"] is not None
            or qwen_artifact["staged"] is not False
        ):
            raise CatalogError(
                "unqualified Qwen requires reacquired per-file SHA-256 weights"
            )
        if artifact_state == "platform-verified" and (
            qwen_artifact["manifest_digest"] != expected_manifest_digest
            or qwen_artifact["expanded_bytes"] != 16_397_461_266
        ):
            raise CatalogError(
                "platform-verified Qwen must bind the exact reviewed HF weight manifest"
            )

    if b300_state == "incompatible-sm103" and accelerated:
        raise CatalogError("an SM103-incompatible identity can never be promoted on B300")
    if support_state != "qualified":
        if route_exposed or accelerated:
            raise CatalogError("unqualified records cannot expose routing or acceleration")
    if accelerated and b300_state != "qualified":
        raise CatalogError("unqualified B300 records cannot expose accelerated routing")
    if route_exposed:
        if license_state != "verified":
            raise CatalogError("route exposure requires a verified license")
        if entitlement_state not in {"not-required", "verified"}:
            raise CatalogError("route exposure requires a satisfied entitlement")
        if immutable_revision is None or image_state != "resolved":
            raise CatalogError("route exposure requires immutable image and model identities")
        if artifact_state != "platform-verified":
            raise CatalogError("route exposure requires a platform-verified immutable artifact manifest")
    if mcp_invocable and not route_exposed:
        raise CatalogError("MCP invocation cannot bypass route qualification")
    if support_state == "blocked":
        if tested_lane or enabled:
            raise CatalogError("blocked candidate cannot be a tested or enabled lane")
    else:
        if not tested_lane:
            raise CatalogError("non-blocked model record must be a tested lane")
        if support_state != "qualified" and enabled != ["conventional"]:
            raise CatalogError("tested unqualified lane must enable only conventional fallback")
        if revision is None or runtime_kind == "unresolved" or image_state == "unresolved":
            raise CatalogError("tested lane requires immutable source and runtime image identities")
        if entitlement_state == "blocked":
            raise CatalogError("blocked entitlement must block the whole candidate")
    return record


@dataclass(frozen=True)
class ModelRecord:
    """An exact validated model record and its canonical content digest."""

    model_id: str
    path: Path
    digest: str
    _value: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._value)

    @property
    def route_exposed(self) -> bool:
        return bool(self._value["support"]["route_exposed"])


@dataclass(frozen=True)
class AcquisitionPlan:
    """One immutable, model-bound artifact acquisition plan."""

    model_id: str
    method: str
    required_prerequisite_ids: tuple[str, ...]
    _value: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self._value))


@dataclass(frozen=True)
class RuntimePrerequisite:
    """Metadata-only requirement for a namespaced resource supplied externally."""

    requirement_id: str
    namespace: str
    name: str
    kind: str
    _value: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self._value))


@dataclass(frozen=True)
class FederatedBackend:
    """One exact observed SM90 upstream; never route authority by itself."""

    model_id: str
    backend_class: str
    route_state: str
    _value: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self._value))


@dataclass(frozen=True)
class SemanticRequestContract:
    """Packaged request payload, licensed-asset, and invocation authority."""

    model_id: str
    state: str
    digest: str
    asset_set_digest: str
    _value: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self._value))

    @property
    def request_ids(self) -> tuple[str, ...]:
        return tuple(item["id"] for item in self._value["requests"])

    @property
    def request_sha256(self) -> tuple[str, ...]:
        return tuple(item["payload_sha256"] for item in self._value["requests"])

    @property
    def invocation(self) -> dict[str, Any]:
        value = self._value["invocation"]
        return copy.deepcopy(value) if value is not None else {}


@dataclass(frozen=True)
class ScaleContract:
    """Immutable model-bound mutation policy for a separate activation controller."""

    model_id: str
    digest: str
    activation_mode: str
    _value: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        value = copy.deepcopy(dict(self._value))
        value["contract_digest"] = self.digest
        return value


@dataclass(frozen=True)
class ModelVariant:
    """One immutable source/runtime alternative; never route authority by itself."""

    variant_id: str
    base_model_id: str
    exposed_model_id: str
    relationship: str
    runtime_architecture: str
    digest: str
    _value: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        value = copy.deepcopy(dict(self._value))
        value["variant_digest"] = self.digest
        return value


@dataclass(frozen=True)
class FallbackCandidate:
    """Deterministic cross-lane identity for one independently researched fallback."""

    candidate_id: str
    lane_id: str
    state: str
    relationship: str
    profile_variants: Mapping[str, str | None]
    digest: str
    _value: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        value = copy.deepcopy(dict(self._value))
        value["candidate_digest"] = self.digest
        return value


@dataclass(frozen=True)
class Catalog:
    """Validated immutable catalog index used by gateway consumers."""

    version: str
    digest: str
    records: Mapping[str, ModelRecord]
    tested_model_ids: tuple[str, ...]
    blocked_candidate_ids: tuple[str, ...]
    acquisition_plans: Mapping[str, AcquisitionPlan]
    compatibility_audit: Mapping[str, Mapping[str, Any]]
    runtime_prerequisites: Mapping[str, RuntimePrerequisite]
    federated_backends: Mapping[str, FederatedBackend]
    semantic_requests: Mapping[str, SemanticRequestContract]
    scale_contracts: Mapping[str, ScaleContract]
    model_variants: Mapping[str, ModelVariant]
    fallback_candidates: Mapping[str, FallbackCandidate]

    def model(self, model_id: str) -> ModelRecord:
        try:
            return self.records[model_id]
        except KeyError as exc:
            raise CatalogError(f"unknown model ID: {model_id}") from exc

    def acquisition_plan(self, model_id: str) -> AcquisitionPlan:
        try:
            return self.acquisition_plans[model_id]
        except KeyError as exc:
            raise CatalogError(f"model has no artifact acquisition plan: {model_id}") from exc

    def prerequisite(self, requirement_id: str) -> RuntimePrerequisite:
        try:
            return self.runtime_prerequisites[requirement_id]
        except KeyError as exc:
            raise CatalogError(f"unknown runtime prerequisite: {requirement_id}") from exc

    def federated_backend(self, model_id: str) -> FederatedBackend | None:
        return self.federated_backends.get(model_id)

    def semantic_request_contract(self, model_id: str) -> SemanticRequestContract:
        try:
            return self.semantic_requests[model_id]
        except KeyError as exc:
            raise CatalogError(
                f"model has no canonical semantic request contract: {model_id}"
            ) from exc

    def scale_contract(self, model_id: str) -> ScaleContract:
        try:
            return self.scale_contracts[model_id]
        except KeyError as exc:
            raise CatalogError(f"model has no immutable scale contract: {model_id}") from exc

    def model_variant(self, variant_id: str) -> ModelVariant:
        try:
            return self.model_variants[variant_id]
        except KeyError as exc:
            raise CatalogError(f"unknown model variant ID: {variant_id}") from exc

    def fallback_candidate(self, candidate_id: str) -> FallbackCandidate:
        try:
            return self.fallback_candidates[candidate_id]
        except KeyError as exc:
            raise CatalogError(f"unknown fallback candidate ID: {candidate_id}") from exc

    def fallback_for_variant(self, variant_id: str) -> tuple[FallbackCandidate, str]:
        self.model_variant(variant_id)
        matches = [
            (candidate, profile)
            for candidate in self.fallback_candidates.values()
            for profile, mapped_variant_id in candidate.profile_variants.items()
            if mapped_variant_id == variant_id
        ]
        if len(matches) != 1:
            raise CatalogError("model variant lacks one deterministic fallback candidate join")
        return matches[0]

    def variants_for(self, model_id: str) -> tuple[ModelVariant, ...]:
        if model_id not in self.records:
            raise CatalogError(f"unknown model ID: {model_id}")
        return tuple(
            variant
            for variant in self.model_variants.values()
            if variant.base_model_id == model_id
        )

    def candidate_variant_ids(self) -> tuple[str, ...]:
        return tuple(self.model_variants)

    def routable_variant_ids(self) -> tuple[str, ...]:
        """Static variants cannot bypass base-record and serving-binding promotion."""

        return ()

    def routable_model_ids(self) -> tuple[str, ...]:
        """Return no routes: a base catalog is never routing authority by itself.

        Gateway consumers must call ``load_gateway_catalog`` with a separately
        validated serving-binding overlay. This deliberately preserves the
        original fail-closed method while preventing semantic evidence from
        being bypassed by a base-record-only consumer.
        """

        return ()


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode() + b"\n"
    except (TypeError, ValueError) as exc:
        raise CatalogError("catalog data is not canonicalizable") from exc


def _load_provenance_lock(
    catalog_root: Path, value: Any
) -> Mapping[tuple[str, str, str, str], str]:
    binding = _exact(value, {"path", "sha256"}, "catalog provenance lock")
    relative = _text(binding["path"], "catalog provenance lock path")
    digest = strong_sha256(binding["sha256"], "catalog provenance lock digest")
    assert relative is not None
    if relative != "contracts/provenance-lock.json":
        raise CatalogError("catalog provenance lock must use the packaged canonical path")
    path = catalog_root / relative
    raw = _regular_bytes(path, maximum=4 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise CatalogError("packaged provenance lock digest mismatch")
    document = _exact(_load_json(path), {"schema", "entries"}, "provenance lock")
    if document["schema"] != "fs2-serve.nebius.ai/provenance-lock/v2":
        raise CatalogError("unsupported packaged provenance lock schema")
    entries = _list(document["entries"], "provenance lock entries", nonempty=True)
    locked: dict[tuple[str, str, str, str], str] = {}
    canonical_order: list[tuple[str, str, str, str]] = []
    for index, raw_entry in enumerate(entries):
        if isinstance(raw_entry, dict) and set(raw_entry) == {
            "url",
            "revision",
            "classification",
            "content_sha256",
        }:
            url = _text(raw_entry["url"], "provenance lock external URL")
            revision = raw_entry["revision"]
            if (
                url is None
                or not url.startswith("https://")
                or not isinstance(revision, str)
                or GIT_OBJECT.fullmatch(revision) is None
                or revision not in url
                or raw_entry["classification"] != "reviewed-input"
            ):
                raise CatalogError("external provenance lock subject is not immutable")
            key = (url, revision, "external", raw_entry["classification"])
            if key in locked:
                raise CatalogError("packaged provenance lock contains a duplicate subject")
            locked[key] = strong_sha256(
                raw_entry["content_sha256"], "external provenance content digest"
            )
            canonical_order.append(key)
            continue
        entry = _exact(
            raw_entry,
            {"commit", "tree", "path", "classification", "content_sha256"},
            f"provenance lock entries[{index}]",
        )
        for key in ("commit", "tree"):
            if not isinstance(entry[key], str) or GIT_OBJECT.fullmatch(entry[key]) is None:
                raise CatalogError(f"provenance lock {key} is not exact")
        entry_path = _text(entry["path"], "provenance lock path")
        classification = _enum(
            entry["classification"],
            {"reviewed-input", "measured-handoff", "negative-evidence"},
            "provenance lock classification",
        )
        content_sha256 = strong_sha256(
            entry["content_sha256"], "provenance lock content digest"
        )
        assert entry_path is not None
        key = (entry["commit"], entry["tree"], entry_path, classification)
        if key in locked:
            raise CatalogError("packaged provenance lock contains a duplicate subject")
        locked[key] = content_sha256
        canonical_order.append(key)
    if canonical_order != sorted(canonical_order):
        raise CatalogError("packaged provenance lock entries must be canonically sorted")
    return MappingProxyType(locked)


def _load_bound_contract(
    catalog_root: Path,
    value: Any,
    *,
    expected_path: str,
    label: str,
) -> dict[str, Any]:
    binding = _exact(value, {"path", "sha256"}, f"catalog {label} binding")
    relative = _text(binding["path"], f"catalog {label} path")
    digest = strong_sha256(binding["sha256"], f"catalog {label} digest")
    if relative != expected_path:
        raise CatalogError(f"catalog {label} must use the packaged canonical path")
    path = catalog_root / relative
    raw = _regular_bytes(path, maximum=4 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise CatalogError(f"packaged {label} digest mismatch")
    return _load_json(path)


def _load_semantic_requests(
    catalog_root: Path,
    value: Any,
    records: Mapping[str, ModelRecord],
) -> Mapping[str, SemanticRequestContract]:
    document = _exact(
        _load_bound_contract(
            catalog_root,
            value,
            expected_path="contracts/semantic-requests.json",
            label="semantic request contracts",
        ),
        {"schema", "contracts"},
        "semantic request contracts",
    )
    if document["schema"] != "fs2-serve.nebius.ai/semantic-request-contracts/v1":
        raise CatalogError("unsupported semantic request contract schema")
    raw_contracts = document["contracts"]
    if not isinstance(raw_contracts, dict) or set(raw_contracts) != set(records):
        raise CatalogError("semantic request contracts must exactly cover the catalog")

    loaded: dict[str, SemanticRequestContract] = {}
    for model_id in sorted(raw_contracts):
        item = _exact(
            raw_contracts[model_id],
            {"state", "blocker", "serialization", "invocation", "requests", "assets"},
            f"semantic request contract {model_id}",
        )
        state = _enum(item["state"], {"qualified", "blocked"}, "semantic request state")
        blocker = _text(item["blocker"], "semantic request blocker", nullable=True)
        serialization = item["serialization"]
        if serialization is not None:
            _enum(
                serialization,
                {
                    "sha256-canonical-json-newline/v1",
                    "sha256-canonical-json-no-newline/v1",
                },
                "semantic request serialization",
            )
        record = records[model_id]
        record_value = record.to_dict()
        invocation = item["invocation"]
        requests = _list(item["requests"], "semantic contract requests")
        if state == "qualified":
            if blocker is not None or serialization is None or invocation is None:
                raise CatalogError(
                    "qualified semantic request contract has a blocker or missing identity"
                )
            expected_interface = record_value["interface"]
            if (
                len(expected_interface["policy"]["operations"]) != 1
                or len(expected_interface["protocols"]) != 1
            ):
                raise CatalogError(
                    "qualified semantic request contract requires one exact operation/protocol"
                )
            expected_protocol = expected_interface["protocols"][0]
            expected_invocation = {
                "operation": expected_interface["policy"]["operations"][0],
                "protocol": expected_protocol,
                "method": "POST",
                "endpoint": expected_interface["endpoints"][expected_protocol],
            }
            if _exact(
                invocation,
                {"operation", "protocol", "method", "endpoint"},
                "semantic invocation",
            ) != expected_invocation:
                raise CatalogError(
                    "semantic invocation differs from the canonical model interface"
                )
            if len(requests) != 2:
                raise CatalogError("qualified semantic contract must contain exactly two requests")
        elif blocker is None:
            raise CatalogError("blocked semantic request contract must preserve its blocker")

        request_ids: list[str] = []
        request_hashes: list[str] = []
        normalized_requests: list[dict[str, str]] = []
        for index, raw_request in enumerate(requests):
            request = _exact(
                raw_request,
                {"id", "payload_sha256"},
                f"semantic request {model_id}[{index}]",
            )
            request_id = _text(request["id"], "semantic request ID")
            payload_digest = strong_sha256(
                request["payload_sha256"], "semantic request payload digest"
            )
            assert request_id is not None
            request_ids.append(request_id)
            request_hashes.append(payload_digest)
            normalized_requests.append({"id": request_id, "payload_sha256": payload_digest})
        if len(request_ids) != len(set(request_ids)):
            raise CatalogError("semantic request IDs must be distinct")
        if state == "qualified" and len(request_hashes) != len(set(request_hashes)):
            raise CatalogError("qualified semantic request payloads must be distinct")

        assets = _list(item["assets"], "semantic request assets")
        normalized_assets: list[dict[str, Any]] = []
        for index, raw_asset in enumerate(assets):
            asset = _exact(
                raw_asset,
                {"request_id", "kind", "uri", "content_sha256", "bytes", "license"},
                f"semantic request asset {model_id}[{index}]",
            )
            request_id = _text(asset["request_id"], "semantic asset request ID")
            uri = _text(asset["uri"], "semantic asset URI")
            license_id = _text(asset["license"], "semantic asset license")
            content_digest = strong_sha256(
                asset["content_sha256"], "semantic asset content digest"
            )
            byte_count = _positive_int(asset["bytes"], "semantic asset bytes")
            assert request_id is not None and uri is not None and license_id is not None
            parsed = urlsplit(uri)
            if (
                asset["kind"] != "licensed-image"
                or request_id not in request_ids
                or parsed.scheme != "https"
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise CatalogError("semantic request asset is not an exact licensed HTTPS subject")
            normalized_assets.append(
                {
                    "request_id": request_id,
                    "kind": "licensed-image",
                    "uri": uri,
                    "content_sha256": content_digest,
                    "bytes": byte_count,
                    "license": license_id,
                }
            )
        asset_order = [
            (asset["request_id"], asset["kind"], asset["uri"])
            for asset in normalized_assets
        ]
        if len(asset_order) != len(set(asset_order)):
            raise CatalogError("semantic request assets must be unique")

        semantic = record_value["semantic_validator"]
        validator = {
            key: semantic[key]
            for key in (
                "contract",
                "source_path",
                "source_sha256",
                "fixture_path",
                "fixture_sha256",
            )
        }
        fixture_subject = {
            "path": semantic["fixture_path"],
            "sha256": semantic["fixture_sha256"],
        }
        if semantic["fixture_path"] is None:
            fixture_subject = {"path": None, "sha256": None}

        controlled_prefix = "k8s-inference/catalog/runtime/validators/assets/"
        if state == "qualified" and isinstance(semantic["fixture_path"], str) and semantic[
            "fixture_path"
        ].startswith(controlled_prefix):
            fixture_path = catalog_root / semantic["fixture_path"].removeprefix(
                "k8s-inference/catalog/runtime/"
            )
            fixture = _load_json(fixture_path)
            fixture_requests = _list(
                fixture.get("requests"), "packaged semantic fixture requests", nonempty=True
            )
            expected_requests = [
                {
                    "id": _text(entry["id"], "packaged semantic request ID"),
                    "payload_sha256": (
                        strong_sha256(
                            entry["payload_sha256"],
                            "packaged semantic request payload digest",
                        )
                        if "payload_sha256" in entry
                        else hashlib.sha256(
                            json.dumps(
                                entry["request"],
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=True,
                                allow_nan=False,
                            ).encode()
                        ).hexdigest()
                    ),
                }
                for entry in fixture_requests
            ]
            if normalized_requests != expected_requests:
                raise CatalogError(
                    "semantic request hashes differ from the packaged fixture payloads"
                )
            if model_id == "nv-reason-cxr-3b":
                expected_assets = [
                    {
                        "request_id": entry["id"],
                        "kind": "licensed-image",
                        "uri": entry["request"]["image"]["url"],
                        "content_sha256": entry["request"]["image"]["sha256"],
                        "bytes": entry["request"]["image"]["bytes"],
                        "license": entry["request"]["image"]["license"],
                    }
                    for entry in fixture_requests
                ]
                if normalized_assets != expected_assets:
                    raise CatalogError("CXR request assets differ from the licensed image fixture")

        asset_subject = {"fixture": fixture_subject, "assets": normalized_assets}
        contract_subject = {
            "model_id": model_id,
            "model_digest": record.digest,
            "validator": validator,
            "state": state,
            "serialization": serialization,
            "invocation": invocation,
            "requests": normalized_requests,
            "assets": normalized_assets,
        }
        loaded[model_id] = SemanticRequestContract(
            model_id=model_id,
            state=state,
            digest=hashlib.sha256(_canonical_bytes(contract_subject)).hexdigest(),
            asset_set_digest=hashlib.sha256(_canonical_bytes(asset_subject)).hexdigest(),
            _value=MappingProxyType(copy.deepcopy(item)),
        )
    return MappingProxyType(loaded)


_VARIANT_SUPPLY_CLAIMS = (
    "artifact-manifest",
    "build-materials",
    "builder",
    "immutable-image",
    "license-artifact",
    "provenance",
    "sbom",
    "scan",
    "signature",
    "source-repository-revision",
)
_VARIANT_QUALIFICATION_CLAIMS = (
    "cold-and-warm-cohorts-separate",
    "compute-capability-10.3",
    "determinism-and-kernel-dispatch-repeated",
    "exact-worker-gpu-runtime-tuple",
    "failures-in-denominator",
    "network-denied-mounted-artifact-startup",
    "preemption-recovery",
    "quality-comparator-identity",
    "scale-zero-ready-zero",
    "two-semantic-gateway-responses",
    "vendor-nim-baseline-evidence",
    "warm-attempts-at-least-10",
)


def _load_model_variants(
    catalog_root: Path,
    value: Any,
    records: Mapping[str, ModelRecord],
    compatibility_audit: Mapping[str, Mapping[str, Any]],
) -> tuple[Mapping[str, ModelVariant], Mapping[str, FallbackCandidate]]:
    """Load source-only runtime alternatives without adding route authority."""

    document = _exact(
        _load_bound_contract(
            catalog_root,
            value,
            expected_path="contracts/model-variants.json",
            label="model variants",
        ),
        {
            "schema",
            "shape_authority",
            "route_authority",
            "receipt_contracts",
            "fallback_candidates",
            "variants",
        },
        "model variants",
    )
    if document["schema"] != MODEL_VARIANTS_SCHEMA:
        raise CatalogError("unsupported model variant schema")
    if document["shape_authority"] != "non-authoritative-discovery-only":
        raise CatalogError("model variant JSON shape must not claim promotion authority")
    if document["route_authority"] is not False:
        raise CatalogError("the static model variant index is never route authority")
    receipt_contracts = _exact(
        document["receipt_contracts"], {"supply", "qualification"}, "variant receipts"
    )
    if receipt_contracts != {
        "supply": MODEL_VARIANT_SUPPLY_SCHEMA,
        "qualification": MODEL_VARIANT_QUALIFICATION_SCHEMA,
    }:
        raise CatalogError("model variant receipt schemas differ from the typed contract")
    raw_variants = document["variants"]
    if not isinstance(raw_variants, dict) or not raw_variants:
        raise CatalogError("model variants must be a non-empty object")
    if list(raw_variants) != sorted(raw_variants):
        raise CatalogError("model variants must be canonically sorted")

    loaded: dict[str, ModelVariant] = {}
    pairs: dict[tuple[str, str, str, str], set[str]] = {}
    for variant_id, raw_variant in raw_variants.items():
        if MODEL_ID.fullmatch(variant_id) is None:
            raise CatalogError("model variant ID is not canonical")
        item = _exact(
            raw_variant,
            {
                "variant_id",
                "base_model_id",
                "exposed_model_id",
                "variant_kind",
                "relationship",
                "source",
                "runtime",
                "receipt_requirements",
                "promotion",
                "notes",
            },
            f"model variant {variant_id}",
        )
        if item["variant_id"] != variant_id:
            raise CatalogError("model variant key and variant_id differ")
        base_model_id = _text(item["base_model_id"], "variant base model ID")
        exposed_model_id = _text(item["exposed_model_id"], "variant exposed model ID")
        assert base_model_id is not None and exposed_model_id is not None
        if (
            MODEL_ID.fullmatch(base_model_id) is None
            or MODEL_ID.fullmatch(exposed_model_id) is None
            or base_model_id not in records
        ):
            raise CatalogError("model variant references an unknown or invalid base identity")
        variant_kind = _enum(
            item["variant_kind"], {"nim", "independent-runtime"}, "variant kind"
        )

        relationship = _exact(
            item["relationship"],
            {
                "kind",
                "reference_model_id",
                "subject_model_id",
                "nim_artifact_parity",
                "distinct_base_record_required",
                "vendor_baseline",
                "notes",
            },
            "variant relationship",
        )
        relationship_kind = _enum(
            relationship["kind"],
            {"exact-model", "capability-equivalent"},
            "variant relationship kind",
        )
        if relationship["reference_model_id"] != base_model_id:
            raise CatalogError("variant relationship does not reference its base model")
        if relationship["subject_model_id"] != exposed_model_id:
            raise CatalogError("variant relationship subject differs from the exposed model")
        parity = _enum(
            relationship["nim_artifact_parity"],
            {"verified", "unverified", "not-applicable"},
            "variant NIM artifact parity",
        )
        distinct_base_required = _boolean(
            relationship["distinct_base_record_required"],
            "variant distinct base record requirement",
        )
        _text(relationship["notes"], "variant relationship notes")
        if relationship_kind == "exact-model":
            if (
                exposed_model_id != base_model_id
                or distinct_base_required
                or parity == "not-applicable"
            ):
                raise CatalogError("exact model variants must preserve the canonical model ID")
        elif (
            exposed_model_id == base_model_id
            or not distinct_base_required
            or parity != "not-applicable"
        ):
            raise CatalogError(
                "capability-equivalent variants require a distinct canonical model identity"
            )

        vendor_baseline = _exact(
            relationship["vendor_baseline"],
            {
                "mode",
                "schema",
                "record_sha256",
                "model_id",
                "execution_identity_sha256",
                "b300_state",
            },
            "variant vendor baseline",
        )
        audited_baseline = compatibility_audit[base_model_id]
        if (
            vendor_baseline["mode"]
            != "canonical-compatibility-audit-delegation"
            or vendor_baseline["schema"]
            != "fs2-serve.nebius.ai/compatibility-audit/v1"
            or vendor_baseline["record_sha256"]
            != hashlib.sha256(_canonical_bytes(dict(audited_baseline))).hexdigest()
            or vendor_baseline["model_id"] != base_model_id
            or vendor_baseline["execution_identity_sha256"]
            != audited_baseline["execution_identity_sha256"]
            or vendor_baseline["b300_state"] != audited_baseline["b300_state"]
        ):
            raise CatalogError("model variant delegates to the wrong canonical NIM evidence")
        strong_sha256(vendor_baseline["record_sha256"], "variant baseline record")
        strong_sha256(
            vendor_baseline["execution_identity_sha256"],
            "variant baseline execution identity",
        )

        source = _exact(
            item["source"],
            {
                "kind",
                "repository",
                "revision",
                "revision_url",
                "artifact",
                "license",
                "entitlement",
            },
            "variant source",
        )
        source_kind = _enum(
            source["kind"], {"ngc-nim", "huggingface", "git"}, "variant source kind"
        )
        repository = _text(source["repository"], "variant source repository")
        revision = _text(source["revision"], "variant source revision")
        revision_url = _text(source["revision_url"], "variant source revision URL")
        assert repository is not None and revision is not None and revision_url is not None
        expected_revision = IMAGE_DIGEST if source_kind == "ngc-nim" else GIT_OBJECT
        if expected_revision.fullmatch(revision) is None:
            raise CatalogError("model variant source revision is not immutable")
        if not revision_url.startswith("https://") or revision not in revision_url:
            raise CatalogError("model variant source URL does not bind the exact revision")

        artifact = _exact(
            source["artifact"],
            {
                "kind",
                "uri",
                "identity_state",
                "manifest_schema",
                "expected_file_count",
                "expected_bytes",
                "expected_content_sha256",
                "manifest_sha256",
            },
            "variant artifact",
        )
        artifact_kind = _enum(
            artifact["kind"],
            {"nim-image", "weights", "release-archive", "checkpoint-set"},
            "variant artifact kind",
        )
        artifact_uri = _text(artifact["uri"], "variant artifact URI")
        assert artifact_uri is not None
        if not artifact_uri.startswith(("https://", "nvcr.io/")):
            raise CatalogError("model variant artifact URI is not an approved immutable source")
        identity_state = _enum(
            artifact["identity_state"],
            {
                "exact-image",
                "expected-only-incomplete-manifest",
                "verified-full-per-file-manifest",
            },
            "variant artifact identity state",
        )
        if artifact["manifest_schema"] != "fs2-serve.nebius.ai/artifact-manifest/v1":
            raise CatalogError("model variant does not name the canonical per-file manifest")
        file_count = _positive_int(
            artifact["expected_file_count"], "variant expected file count", nullable=True
        )
        expected_bytes = _positive_int(
            artifact["expected_bytes"], "variant expected bytes", nullable=True
        )
        content_digests = _list(
            artifact["expected_content_sha256"],
            "variant expected content digests",
            nonempty=identity_state == "expected-only-incomplete-manifest",
        )
        if len(content_digests) != len(set(content_digests)):
            raise CatalogError("model variant expected content digests must be unique")
        for digest in content_digests:
            strong_sha256(digest, "variant expected content digest")
        if (
            identity_state == "expected-only-incomplete-manifest"
            and file_count is not None
            and file_count != len(content_digests)
        ):
            raise CatalogError("variant expected file count differs from content identities")
        manifest_digest = _optional_digest(
            artifact["manifest_sha256"], "variant artifact manifest"
        )
        if identity_state == "exact-image":
            if (
                source_kind != "ngc-nim"
                or artifact_kind != "nim-image"
                or manifest_digest != revision.removeprefix("sha256:")
            ):
                raise CatalogError("exact-image variant does not bind its NIM digest")
        elif identity_state == "expected-only-incomplete-manifest" and manifest_digest is not None:
            raise CatalogError("expected-only variant cannot claim a complete artifact manifest")
        elif identity_state == "verified-full-per-file-manifest" and (
            manifest_digest is None
            or file_count is None
            or expected_bytes is None
            or content_digests
        ):
            raise CatalogError(
                "verified variant must delegate path/size/SHA identity to one full manifest"
            )

        license_binding = _exact(
            source["license"],
            {
                "id",
                "state",
                "source_url",
                "artifact_sha256",
                "commercial_use",
                "non_clinical",
            },
            "variant license",
        )
        _text(license_binding["id"], "variant license ID")
        license_state = _enum(
            license_binding["state"],
            {
                "verified-artifact",
                "metadata-reviewed-artifact-unbound",
                "unverified",
                "blocked",
            },
            "variant license state",
        )
        license_url = _text(license_binding["source_url"], "variant license source URL")
        assert license_url is not None
        if not license_url.startswith("https://") or revision not in license_url:
            raise CatalogError("variant license source must be HTTPS and revision-bound")
        license_digest = _optional_digest(
            license_binding["artifact_sha256"], "variant license artifact"
        )
        if (license_state == "verified-artifact") != (license_digest is not None):
            raise CatalogError("variant license verification requires immutable license bytes")
        _enum(
            license_binding["commercial_use"],
            {"allowed", "license-dependent", "prohibited", "blocked"},
            "variant commercial-use policy",
        )
        _boolean(license_binding["non_clinical"], "variant non-clinical policy")
        _enum(
            source["entitlement"],
            {"not-required", "fresh-platform-ngc-required", "blocked"},
            "variant entitlement",
        )

        runtime = _exact(
            item["runtime"],
            {
                "architecture",
                "build_state",
                "image_digest",
                "device_capability",
                "network_startup",
            },
            "variant runtime",
        )
        architecture = _enum(
            runtime["architecture"],
            {"vendor-nim", "portable", "blackwell-sm103"},
            "variant runtime architecture",
        )
        build_state = _enum(
            runtime["build_state"],
            {
                "existing-exact-image",
                "source-only-candidate",
                "built-attested",
                "negative-evidence",
                "blocked",
            },
            "variant runtime build state",
        )
        image_digest = _optional_digest(
            runtime["image_digest"], "variant runtime image", image=True
        )
        device_capability = _enum(
            runtime["device_capability"],
            {
                "vendor-declared",
                "portable-unverified",
                "portable-qualified",
                "sm103-unverified",
                "sm103-qualified",
                "sm103-incompatible",
            },
            "variant device capability",
        )
        if runtime["network_startup"] != "deny-until-exact-mounted-artifact-ready":
            raise CatalogError("model variant runtime may not fetch weights during startup")
        if variant_kind == "independent-runtime":
            if architecture not in {"portable", "blackwell-sm103"} or source_kind == "ngc-nim":
                raise CatalogError("independent variants must declare portable or SM103 architecture")
            is_built = build_state == "built-attested"
            if (image_digest is not None) != is_built or build_state == "existing-exact-image":
                raise CatalogError("independent variant build state and immutable image disagree")
            expected_capability = {
                ("portable", False): "portable-unverified",
                ("portable", True): "portable-qualified",
                ("blackwell-sm103", False): "sm103-unverified",
                ("blackwell-sm103", True): "sm103-qualified",
            }[(architecture, is_built)]
            if device_capability != expected_capability:
                raise CatalogError("variant architecture and device capability disagree")
        elif architecture != "vendor-nim" or source_kind != "ngc-nim":
            raise CatalogError("NIM variants must preserve the exact vendor architecture")

        requirements = _exact(
            item["receipt_requirements"],
            {"supply_schema", "qualification_schema", "supply_claims", "qualification_claims"},
            "variant receipt requirements",
        )
        if (
            requirements["supply_schema"] != MODEL_VARIANT_SUPPLY_SCHEMA
            or requirements["qualification_schema"] != MODEL_VARIANT_QUALIFICATION_SCHEMA
            or tuple(requirements["supply_claims"]) != _VARIANT_SUPPLY_CLAIMS
            or tuple(requirements["qualification_claims"])
            != _VARIANT_QUALIFICATION_CLAIMS
        ):
            raise CatalogError("model variant receipt claims are incomplete or noncanonical")

        promotion = _exact(
            item["promotion"],
            {
                "state",
                "route_exposed",
                "supply_receipt_digest",
                "qualification_receipt_digest",
                "independent_review_receipt_digest",
            },
            "variant promotion",
        )
        promotion_state = _enum(
            promotion["state"],
            {"candidate-unqualified", "blocked", "negative"},
            "variant promotion state",
        )
        route_exposed = _boolean(promotion["route_exposed"], "variant route exposure")
        promotion_digests = tuple(
            _optional_digest(promotion[key], f"variant promotion {key}")
            for key in (
                "supply_receipt_digest",
                "qualification_receipt_digest",
                "independent_review_receipt_digest",
            )
        )
        if route_exposed:
            raise CatalogError("a static model variant contract cannot expose a route")
        if any(promotion_digests):
            raise CatalogError(
                "the source-only variant index cannot consume or assert promotion receipts"
            )
        if promotion_state == "negative" and build_state != "negative-evidence":
            raise CatalogError("negative variant promotion must preserve negative build evidence")

        if base_model_id == "molmim":
            if relationship_kind == "exact-model" and source_kind != "ngc-nim" and not (
                source_kind == "git"
                and repository == "github.com/NVIDIA/bionemo-framework"
                and artifact_kind == "weights"
                and artifact_uri.startswith("https://api.ngc.nvidia.com/")
                and source["entitlement"] == "fresh-platform-ngc-required"
                and file_count == 1
                and len(content_digests) == 1
            ):
                raise CatalogError(
                    "an independent exact MolMIM runtime must retain the exact entitled NGC artifact"
                )
            if relationship_kind == "capability-equivalent" and (
                exposed_model_id == "molmim"
                or not exposed_model_id.startswith("nv-genmol-")
                or "NV-GenMol" not in repository
            ):
                raise CatalogError("GenMol must remain a distinct capability alternative to MolMIM")

        notes = _list(item["notes"], "variant notes", nonempty=True)
        if not all(isinstance(note, str) and note for note in notes):
            raise CatalogError("variant notes must be non-empty text")
        canonical = _canonical_bytes(item)
        loaded[variant_id] = ModelVariant(
            variant_id=variant_id,
            base_model_id=base_model_id,
            exposed_model_id=exposed_model_id,
            relationship=relationship_kind,
            runtime_architecture=architecture,
            digest=hashlib.sha256(canonical).hexdigest(),
            _value=MappingProxyType(copy.deepcopy(item)),
        )
        if variant_kind == "independent-runtime":
            pair_key = (base_model_id, exposed_model_id, source_kind, revision)
            pairs.setdefault(pair_key, set()).add(architecture)

    for pair_key, architectures in pairs.items():
        if architectures != {"portable", "blackwell-sm103"}:
            raise CatalogError(
                f"independent model variant lacks portable/blackwell pair: {pair_key}"
            )
    raw_candidates = document["fallback_candidates"]
    if not isinstance(raw_candidates, dict) or list(raw_candidates) != sorted(raw_candidates):
        raise CatalogError("fallback candidates must be a non-empty canonically sorted object")
    if set(raw_candidates) != REQUIRED_FALLBACK_CANDIDATE_IDS:
        raise CatalogError("the fallback handoff must reconcile all eleven researched candidates")
    candidates: dict[str, FallbackCandidate] = {}
    mapped_variants: dict[str, tuple[str, str]] = {}
    for candidate_id, raw_candidate in raw_candidates.items():
        if MODEL_ID.fullmatch(candidate_id) is None:
            raise CatalogError("fallback candidate ID is not canonical")
        candidate = _exact(
            raw_candidate,
            {
                "candidate_id",
                "lane_id",
                "state",
                "relationship",
                "profile_variants",
                "secondary_non_alias_alternative",
                "notes",
            },
            f"fallback candidate {candidate_id}",
        )
        if candidate["candidate_id"] != candidate_id:
            raise CatalogError("fallback candidate key and candidate_id differ")
        lane_id = _text(candidate["lane_id"], "fallback candidate lane ID")
        assert lane_id is not None
        if lane_id not in records:
            raise CatalogError("fallback candidate names an unknown canonical lane")
        state = _enum(
            candidate["state"],
            {
                "mapped-source-only",
                "blocked-provenance",
                "deferred-source-reconciliation",
                "blocked-access",
                "blocked-license",
            },
            "fallback candidate state",
        )
        relationship = _enum(
            candidate["relationship"],
            {"exact-model", "capability-equivalent-non-alias"},
            "fallback candidate relationship",
        )
        profiles = _exact(
            candidate["profile_variants"],
            {"portable", "blackwell-sm103"},
            "fallback candidate profiles",
        )
        mapped = tuple(profiles.values())
        if state == "mapped-source-only":
            if any(not isinstance(value, str) or value not in loaded for value in mapped):
                raise CatalogError("mapped fallback candidate lacks both exact variant records")
        elif any(value is not None for value in mapped):
            raise CatalogError("blocked or deferred fallback candidate may not map variants")
        for profile, variant_id in profiles.items():
            if variant_id is None:
                continue
            if variant_id in mapped_variants:
                raise CatalogError("one model variant is mapped by multiple fallback candidates")
            variant = loaded[variant_id]
            if (
                variant.runtime_architecture != profile
                or variant.base_model_id != lane_id
                or variant.exposed_model_id != lane_id
                or variant.relationship != "exact-model"
            ):
                raise CatalogError("fallback candidate/profile does not join its exact model variant")
            mapped_variants[variant_id] = (candidate_id, profile)
        secondary = candidate["secondary_non_alias_alternative"]
        if secondary is not None:
            secondary = _exact(
                secondary,
                {"reference_model_id", "relationship", "alias_allowed"},
                "fallback secondary alternative",
            )
            if (
                candidate_id != "genmol-hf-v2"
                or lane_id != "genmol"
                or secondary
                != {
                    "reference_model_id": "molmim",
                    "relationship": "capability-equivalent",
                    "alias_allowed": False,
                }
            ):
                raise CatalogError("only GenMol may declare the reviewed MolMIM non-alias edge")
        elif candidate_id == "genmol-hf-v2":
            raise CatalogError("GenMol must preserve its secondary non-alias MolMIM relation")
        notes = _list(candidate["notes"], "fallback candidate notes", nonempty=True)
        if not all(isinstance(note, str) and note for note in notes):
            raise CatalogError("fallback candidate notes must be non-empty text")
        canonical = _canonical_bytes(candidate)
        candidates[candidate_id] = FallbackCandidate(
            candidate_id=candidate_id,
            lane_id=lane_id,
            state=state,
            relationship=relationship,
            profile_variants=MappingProxyType(dict(profiles)),
            digest=hashlib.sha256(canonical).hexdigest(),
            _value=MappingProxyType(copy.deepcopy(candidate)),
        )
    if set(mapped_variants) != set(loaded):
        raise CatalogError("every static model variant needs one deterministic fallback join")
    return MappingProxyType(loaded), MappingProxyType(candidates)


def _load_runtime_prerequisites(
    catalog_root: Path, value: Any
) -> Mapping[str, RuntimePrerequisite]:
    document = _exact(
        _load_bound_contract(
            catalog_root,
            value,
            expected_path="contracts/runtime-prerequisites.json",
            label="runtime prerequisites",
        ),
        {"schema", "credential_policy", "ngc_credential_contract", "resources"},
        "runtime prerequisites",
    )
    if document["schema"] != "fs2-serve.nebius.ai/runtime-prerequisites/v4":
        raise CatalogError("unsupported runtime prerequisite contract")
    policy = _exact(
        document["credential_policy"],
        {
            "values",
            "materialization",
            "fresh_platform_ngc_key",
            "legacy_ngc_secret_copy",
            "legacy_plaintext_rotation_source",
            "legacy_phase_7c_hmac_reuse",
            "exposed_evo_bearer_reuse",
        },
        "runtime prerequisite credential policy",
    )
    if policy != {
        "values": "suppressed",
        "materialization": "pre-created-secret-observation-default",
        "fresh_platform_ngc_key": "required",
        "legacy_ngc_secret_copy": "forbidden",
        "legacy_plaintext_rotation_source": "forbidden",
        "legacy_phase_7c_hmac_reuse": "forbidden",
        "exposed_evo_bearer_reuse": "forbidden",
    }:
        raise CatalogError("runtime prerequisites permit unsafe credential materialization or reuse")
    ngc_contract = _exact(
        document["ngc_credential_contract"],
        {
            "schema",
            "platform_owner",
            "default_secret_delivery",
            "optional_secret_backends",
            "freshness_evidence",
            "legacy_secret_audit",
            "secret_requirement_ids",
            "required_downstream_evidence",
            "values",
            "route_state_without_receipt",
        },
        "NGC credential contract",
    )
    if ngc_contract != {
        "schema": "fs2-serve.nebius.ai/ngc-credential-materialization/v3",
        "platform_owner": "fs2-serve-platform",
        "default_secret_delivery": {
            "schema": "fs2-serve.nebius.ai/precreated-ngc-secret-delivery/v1",
            "mode": "securely-pre-created-existing-kubernetes-secrets",
            "namespace": "fs2-models",
            "outputs": [
                {
                    "requirement_id": "fs2-models/ngc-pull-secret",
                    "secret_name": "fs2-ngc-pull",
                    "secret_type": "kubernetes.io/dockerconfigjson",
                    "required_keys": [".dockerconfigjson"],
                },
                {
                    "requirement_id": "fs2-models/ngc-runtime-secret",
                    "secret_name": "fs2-ngc-runtime",
                    "secret_type": "Opaque",
                    "required_keys": ["NGC_API_KEY"],
                },
            ],
            "helm_and_git_content": "names-and-keys-only-values-forbidden",
            "required_observation": "server-observed-uid-resourceVersion-type-key-set",
            "sole_authority": "signed-runtime-prerequisite-receipt",
        },
        "optional_secret_backends": [
            {
                "id": "external-secrets-nebius-mysterybox",
                "operator": "external-secrets",
                "version": "2.5.0",
                "provider": "nebiusmysterybox",
                "foundation_crd_state": "ineligible",
                "status": "disabled-until-separately-reviewed-eligible-provider-build-receipt",
                "required_receipt_schema": "fs2-serve.nebius.ai/external-secrets-provider-build-eligibility-receipt/v1",
                "rendering": "disabled",
                "cluster_secret_store": "forbidden",
                "static_service_account_keys": "forbidden",
                "default_service_account": "forbidden",
            }
        ],
        "freshness_evidence": "signed-issuance-validity-noncompromise",
        "legacy_secret_audit": "all-legacy-copies-no-go",
        "secret_requirement_ids": [
            "fs2-models/ngc-pull-secret",
            "fs2-models/ngc-runtime-secret",
        ],
        "required_downstream_evidence": [
            "exact-digest-b300-target-node-pull-canary",
            "nimcache-auth-readiness",
            "two-distinct-semantic-requests",
        ],
        "values": "suppressed",
        "route_state_without_receipt": "disabled",
    }:
        raise CatalogError("NGC credential contract permits legacy or incomplete promotion")
    requirements: dict[str, RuntimePrerequisite] = {}
    order: list[str] = []
    for index, raw in enumerate(_list(document["resources"], "runtime resources", nonempty=True)):
        item = _exact(
            raw,
            {
                "id",
                "api_version",
                "kind",
                "namespace",
                "name",
                "secret_type",
                "required_keys",
                "access_modes",
                "minimum_capacity_bytes",
                "required_state",
                "value_policy",
                "owner",
            },
            f"runtime resources[{index}]",
        )
        requirement_id = _text(item["id"], "runtime resource ID")
        namespace = _enum(
            item["namespace"], {"fs2-models", "fs2-faststart"}, "runtime resource namespace"
        )
        name = _text(item["name"], "runtime resource name")
        kind = _enum(
            item["kind"],
            {"Secret", "ServiceAccount", "PersistentVolumeClaim"},
            "runtime resource kind",
        )
        if item["api_version"] != "v1" or item["owner"] not in {
            "platform-bootstrap",
            "platform-security-bootstrap",
        }:
            raise CatalogError("runtime prerequisite ownership or API version differs")
        keys = _list(item["required_keys"], "runtime prerequisite keys")
        modes = _list(item["access_modes"], "runtime prerequisite access modes")
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise CatalogError("runtime prerequisite keys must be sorted and unique")
        if modes != sorted(modes) or len(modes) != len(set(modes)):
            raise CatalogError("runtime prerequisite access modes must be sorted and unique")
        if kind == "Secret":
            secret_type = _enum(
                item["secret_type"],
                {"Opaque", "kubernetes.io/dockerconfigjson"},
                "runtime prerequisite Secret type",
            )
            allowed_keys = (
                [[".dockerconfigjson"]]
                if secret_type.endswith("dockerconfigjson")
                else [["NGC_API_KEY"], ["token"]]
            )
            if keys not in allowed_keys or modes or item["minimum_capacity_bytes"] is not None:
                raise CatalogError("Secret prerequisite metadata is inconsistent")
            if item["value_policy"] != "metadata-only-values-suppressed":
                raise CatalogError("Secret prerequisite may not carry credential values")
            expected_owner = (
                "platform-security-bootstrap"
                if requirement_id in ngc_contract["secret_requirement_ids"]
                else "platform-bootstrap"
            )
            if item["owner"] != expected_owner:
                raise CatalogError("Secret prerequisite has the wrong platform owner")
        elif kind == "PersistentVolumeClaim":
            capacity = _positive_int(
                item["minimum_capacity_bytes"], "runtime prerequisite PVC capacity"
            )
            if (
                item["secret_type"] is not None
                or keys
                or modes != ["ReadWriteMany"]
                or item["required_state"] != "Bound"
                or item["value_policy"] != "not-applicable"
                or capacity is None
            ):
                raise CatalogError("PVC prerequisite metadata is inconsistent")
        else:
            if (
                item["secret_type"] is not None
                or keys
                or modes
                or item["minimum_capacity_bytes"] is not None
                or item["required_state"] != "present"
                or item["value_policy"] != "not-applicable"
            ):
                raise CatalogError("ServiceAccount prerequisite metadata is inconsistent")
        assert requirement_id is not None and name is not None
        if requirement_id != f"{namespace}/{requirement_id.split('/', 1)[-1]}":
            raise CatalogError("runtime prerequisite ID and namespace disagree")
        if requirement_id in requirements:
            raise CatalogError("runtime prerequisite IDs must be unique")
        order.append(requirement_id)
        requirements[requirement_id] = RuntimePrerequisite(
            requirement_id=requirement_id,
            namespace=namespace,
            name=name,
            kind=kind,
            _value=copy.deepcopy(item),
        )
    if not set(ngc_contract["secret_requirement_ids"]).issubset(requirements):
        raise CatalogError("NGC credential contract lacks both target Secret resources")
    if order != sorted(order):
        raise CatalogError("runtime prerequisites must be canonically sorted")
    return MappingProxyType(requirements)


def _load_legal_reviews(
    value: Any,
    records: Mapping[str, ModelRecord],
) -> Mapping[str, Mapping[str, Any]]:
    if not isinstance(value, dict) or not value:
        raise CatalogError("artifact acquisition legal reviews must be a nonempty object")
    if list(value) != sorted(value) or not set(value).issubset(records):
        raise CatalogError("artifact acquisition legal reviews are not canonical model IDs")
    reviews: dict[str, Mapping[str, Any]] = {}
    for model_id, raw_review in value.items():
        review = _exact(
            raw_review,
            {
                "review_state",
                "repository",
                "revision",
                "license_id",
                "source_url",
                "model_card_url",
                "license_file_url",
                "license_file_sha256",
                "observed_at",
                "metadata",
                "metadata_sha256",
            },
            f"artifact acquisition legal review {model_id}",
        )
        record = records[model_id].to_dict()
        source = record["model"]["source"]
        repository = _text(review["repository"], "legal review repository")
        revision = _text(review["revision"], "legal review revision")
        license_id = _text(review["license_id"], "legal review license")
        if (
            review["review_state"] != "reviewed"
            or source["kind"] != "huggingface"
            or repository != source["repository"]
            or revision != source["revision"]
            or license_id != source["license"]["id"]
            or source["license"]["state"] != "verified"
        ):
            raise CatalogError("legal review differs from the exact verified model source")
        assert repository is not None and revision is not None and license_id is not None
        expected_api = (
            f"https://huggingface.co/api/models/{repository}/revision/{revision}"
        )
        expected_card = f"https://huggingface.co/{repository}/blob/{revision}/README.md"
        expected_license = f"https://huggingface.co/{repository}/raw/{revision}/LICENSE"
        license_file_sha256 = strong_sha256(
            review["license_file_sha256"], "legal review license file digest"
        )
        if (
            review["source_url"] != expected_api
            or review["model_card_url"] != expected_card
            or review["license_file_url"] != expected_license
        ):
            raise CatalogError("legal review URLs are not exact-revision official sources")
        if source["license"].get("artifact") != {
            "url": expected_license,
            "sha256": license_file_sha256,
        }:
            raise CatalogError("legal review license artifact differs from the model source")
        observed_at = _text(review["observed_at"], "legal review observation time")
        if observed_at is None or RFC3339_UTC_SECONDS.fullmatch(observed_at) is None:
            raise CatalogError("legal review observation time must use whole RFC3339 UTC seconds")
        try:
            parsed_observation = datetime.fromisoformat(observed_at[:-1] + "+00:00")
        except ValueError as exc:
            raise CatalogError("legal review observation time is invalid") from exc
        if parsed_observation.tzinfo != timezone.utc:
            raise CatalogError("legal review observation time is not UTC")
        metadata = _exact(
            review["metadata"],
            {"id", "sha", "private", "gated", "disabled", "license"},
            "legal review canonical metadata",
        )
        if metadata != {
            "id": repository,
            "sha": revision,
            "private": False,
            "gated": False,
            "disabled": False,
            "license": license_id,
        }:
            raise CatalogError("legal review metadata is not the exact public ungated subject")
        metadata_sha256 = strong_sha256(
            review["metadata_sha256"], "legal review metadata digest"
        )
        if metadata_sha256 != hashlib.sha256(_canonical_bytes(metadata)).hexdigest():
            raise CatalogError("legal review metadata digest differs from its canonical subject")
        reviews[model_id] = MappingProxyType(copy.deepcopy(review))
    if "qwen3-8b" not in reviews:
        raise CatalogError("Qwen exact-revision legal review is required")
    return MappingProxyType(reviews)


def _validate_qualification_priority(
    value: Any,
    records: Mapping[str, ModelRecord],
    reviews: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    priority = _exact(
        value,
        {
            "rank",
            "model_id",
            "backend_class",
            "startup_mechanism",
            "state",
            "remaining_gates",
        },
        "artifact acquisition qualification priority",
    )
    expected_gates = [
        "exact-hf-weight-per-file-sha256-manifest",
        "protected-retain-provider-block-pvc",
        "targeted-zero-gpu-acquisition-and-writer-handoff",
        "detach-reattach-no-multi-attach",
        "immutable-platform-runtime-image",
        "exact-b300-runtime-qualification",
        "two-distinct-gateway-semantic-responses",
        "scale-one-to-zero-claim-retained",
        "ready-unexpired-serving-binding",
    ]
    if priority != {
        "rank": 1,
        "model_id": "qwen3-8b",
        "backend_class": "local-b300",
        "startup_mechanism": "conventional",
        "state": "candidate-unroutable",
        "remaining_gates": expected_gates,
    }:
        raise CatalogError("qualification priority must be the fail-closed Qwen B300 candidate")
    record = records["qwen3-8b"].to_dict()
    placement = record["resources"]["gpu"]["placement"]
    provider_state = (
        None if placement is None else placement["provider_block_pvc"]["state"]
    )
    expected_cache_capability = (
        "provider-block-pvc-qualified"
        if provider_state == "qualified"
        else "provider-block-pvc-candidate"
    )
    if (
        "qwen3-8b" not in reviews
        or record["startup"]["default"] != "conventional"
        or record["resources"]["gpu"]["class"] != "NVIDIA-B300-SXM6-288GB"
        or record["resources"]["gpu"]["count"] != 1
        or record["resources"]["gpu"]["topology"] != "single-gpu"
        or placement is None
        or placement["pool"] != "b300-burst-8x"
        or placement["node_selector"]
        != {
            "capacity.fs2.nebius/gpu-count": "8",
            "capacity.fs2.nebius/pool": "burst",
            "capacity.fs2.nebius/preset": "b300-8x",
            "capacity.fs2.nebius/type": "preemptible",
            "workload.fs2.nebius/gpu": "true",
        }
        or placement["cache_capabilities"]
        != [expected_cache_capability]
        or placement["qualification_sequence"][0]["storage_mode"]
        != "provider-block-pvc"
        or provider_state not in {"candidate-unqualified", "qualified"}
        or placement["provider_block_pvc"]["storage_class"]["reclaim_policy"]
        != "Retain"
    ):
        raise CatalogError("preferred qualification candidate is not the exact Qwen B300 lane")
    return MappingProxyType(copy.deepcopy(priority))


def _load_acquisition_plans(
    catalog_root: Path,
    value: Any,
    records: Mapping[str, ModelRecord],
    prerequisites: Mapping[str, RuntimePrerequisite],
) -> Mapping[str, AcquisitionPlan]:
    document = _exact(
        _load_bound_contract(
            catalog_root,
            value,
            expected_path="contracts/artifact-acquisition.json",
            label="artifact acquisition",
        ),
        {
            "schema",
            "network_policy",
            "legal_reviews",
            "qualification_priority",
            "plans",
            "helper_images",
        },
        "artifact acquisition",
    )
    if document["schema"] != "fs2-serve.nebius.ai/artifact-acquisition/v5":
        raise CatalogError("unsupported artifact acquisition contract")
    if _exact(
        document["network_policy"],
        {"runner_ngc_access", "ngc_validation_scope", "hf_access"},
        "artifact acquisition network policy",
    ) != {
        "runner_ngc_access": "waf-blocked",
        "ngc_validation_scope": "target-node-only",
        "hf_access": "public-exact-revision-only",
    }:
        raise CatalogError("artifact acquisition network policy may bypass the target node")
    legal_reviews = _load_legal_reviews(document["legal_reviews"], records)
    qualification_priority = _validate_qualification_priority(
        document["qualification_priority"], records, legal_reviews
    )
    raw_helpers = _exact(
        document["helper_images"],
        {"fs2-acquisition-helper"},
        "artifact acquisition helper images",
    )
    helper = _exact(
        raw_helpers["fs2-acquisition-helper"],
        {
            "schema",
            "state",
            "repository_suffix",
            "platform",
            "build_source",
            "entrypoint",
            "security_context",
            "required_attestations",
            "admission_receipt_schema",
            "caller_image_override",
            "job_uid_binding",
            "pod_uid_binding",
            "cleanup_contract",
        },
        "artifact acquisition helper image",
    )
    expected_helper = {
        "schema": "fs2-serve.nebius.ai/acquisition-helper-image-contract/v1",
        "state": "requires-signed-runtime-image-admission",
        "repository_suffix": "/fs2-serve/acquisition-helper",
        "platform": {"os": "linux", "architecture": "amd64"},
        "build_source": {
            "repository": "https://github.com/rene-tech/nebius-solutions-library",
            "path": "k8s-inference/catalog/runtime",
            "package": "fs2-serve-catalog",
            "package_version": "0.1.0",
            "pyproject_sha256": "6224c2e9b5528dab7cc71a9d05b133a1afdf8b1a8f0f8f39942f78c314529494",
            "uv_lock_sha256": "87a43280317c2fa4924daac6a49bca9aed29b98031c9c11d95a6fa1b12dad88b",
        },
        "entrypoint": ["python3", "-m", "fs2_serve_catalog.cli", "acquire-hf"],
        "security_context": {
            "run_as_non_root": True,
            "run_as_uid": 10001,
            "run_as_gid": 10001,
            "fs_group": 10001,
            "supplemental_groups_policy": "Strict",
            "seccomp_profile": "RuntimeDefault",
        },
        "required_attestations": {
            "signature": "verified",
            "signature_registry_identity": "exact-registry-subject",
            "provenance_predicate": "https://slsa.dev/provenance/v1",
            "provenance_materials": "exact-git-commit-tree-build-and-helper-contract",
            "provenance_builder": "exact-builder-identity-and-build-type",
            "base_images": "all-container-materials-digest-pinned",
            "sbom_predicate": "https://spdx.dev/Document",
            "sbom_subject": "exact-package-version-and-wheel",
            "subjects": "exact-oci-manifest-digest",
        },
        "admission_receipt_schema": "fs2-serve.nebius.ai/acquisition-helper-image-admission/v1",
        "caller_image_override": "forbidden",
        "job_uid_binding": "server-observed-and-patched-before-unsuspend",
        "pod_uid_binding": "downward-api-metadata-uid",
        "cleanup_contract": "uid-precondition-plus-replacement-observation",
    }
    if helper != expected_helper:
        raise CatalogError("artifact acquisition helper image contract differs")
    for file_name, field in (
        ("pyproject.toml", "pyproject_sha256"),
        ("uv.lock", "uv_lock_sha256"),
    ):
        if hashlib.sha256((catalog_root / file_name).read_bytes()).hexdigest() != helper[
            "build_source"
        ][field]:
            raise CatalogError("artifact acquisition helper build source digest differs")
    raw_plans = document["plans"]
    if not isinstance(raw_plans, dict) or set(raw_plans) != set(records):
        raise CatalogError("artifact acquisition plans must exactly cover the catalog")
    plans: dict[str, AcquisitionPlan] = {}
    for model_id in sorted(raw_plans):
        plan_fields = {
            "method",
            "source_kind",
            "repository",
            "revision",
            "artifact_kind",
            "destination_prefix",
            "required_prerequisites",
            "pull_canary",
            "publication",
            "promotion_policy",
            "helper_image_id",
        }
        if model_id == "qwen3-8b":
            plan_fields.add("storage_contract")
        item = _exact(
            raw_plans[model_id],
            plan_fields,
            f"artifact acquisition plan {model_id}",
        )
        record = records[model_id].to_dict()
        method = _enum(
            item["method"],
            {"huggingface-public-snapshot", "ngc-target-node-nimcache", "blocked"},
            "artifact acquisition method",
        )
        if (
            item["source_kind"] != record["model"]["source"]["kind"]
            or any(
                item[key] != record["model"]["source"][key]
                for key in ("repository", "revision")
            )
        ):
            raise CatalogError("artifact acquisition source differs from the model identity")
        expected_destination = (
            f"/mnt/fs2-provider-block/models/{model_id}"
            if model_id == "qwen3-8b"
            else record["cache"]["shared_path"]
        )
        if (
            item["artifact_kind"] != record["cache"]["artifact"]["kind"]
            or item["destination_prefix"] != expected_destination
        ):
            raise CatalogError("artifact acquisition subject differs from the model cache")
        required = _list(
            item["required_prerequisites"], "artifact acquisition prerequisites"
        )
        if required != sorted(required) or len(required) != len(set(required)):
            raise CatalogError("artifact acquisition prerequisites must be sorted and unique")
        unknown = set(required) - set(prerequisites)
        if unknown:
            raise CatalogError(f"artifact acquisition names unknown prerequisites: {sorted(unknown)}")
        canary = _exact(
            item["pull_canary"],
            {"required", "execution_scope", "output_policy", "receipt_schema"},
            "artifact acquisition pull canary",
        )
        if canary["output_policy"] != "values-suppressed":
            raise CatalogError("artifact pull canary may not expose credential values")
        if method == "ngc-target-node-nimcache":
            expected = {
                "fs2-models/cache-service-account",
                "fs2-models/ngc-pull-secret",
                "fs2-models/ngc-runtime-secret",
                "fs2-models/shared-cache-pvc",
            }
            if (
                record["model"]["source"]["kind"] != "ngc-nim"
                or set(required) != expected
                or canary != {
                    "required": True,
                    "execution_scope": "target-gpu-node",
                    "output_policy": "values-suppressed",
                    "receipt_schema": "fs2-serve.nebius.ai/target-node-pull-canary/v2",
                }
                or item["publication"] != "atomic-content-addressed-sfs"
                or item["helper_image_id"] is not None
            ):
                raise CatalogError("NGC acquisition must use the exact target-node canary contract")
        elif method == "huggingface-public-snapshot":
            expected = {
                "fs2-models/cache-service-account",
                "fs2-models/runtime-registry-secret",
            }
            if model_id != "qwen3-8b":
                expected.add("fs2-models/shared-cache-pvc")
            if (
                record["model"]["source"]["kind"] != "huggingface"
                or set(required) != expected
                or canary != {
                    "required": False,
                    "execution_scope": "not-required",
                    "output_policy": "values-suppressed",
                    "receipt_schema": None,
                }
                or item["publication"]
                != (
                    "atomic-content-addressed-provider-block-pvc"
                    if model_id == "qwen3-8b"
                    else "atomic-content-addressed-sfs"
                )
                or (
                    model_id == "qwen3-8b"
                    and item.get("storage_contract")
                    != "fs2-serve.nebius.ai/provider-block-pvc/v1"
                )
                or item["helper_image_id"] != "fs2-acquisition-helper"
            ):
                raise CatalogError("Hugging Face acquisition must remain public and exact-revision pinned")
        else:
            if (
                record["support"]["state"] != "blocked"
                or required
                or item["publication"] != "blocked"
                or canary["required"] is not False
                or item["helper_image_id"] is not None
            ):
                raise CatalogError("blocked acquisition may not imply resources or publication")
        promotion_policy = _text(
            item["promotion_policy"], "artifact acquisition promotion policy"
        )
        if model_id == "qwen3-8b" and promotion_policy != (
            "exact-hf-weight-manifest-provider-block-handoff-and-live-runtime-semantic-qualification-required"
        ):
            raise CatalogError("Qwen promotion may not skip exact HF weights or live gates")
        plan_value = copy.deepcopy(item)
        plan_value["helper_image"] = (
            copy.deepcopy(helper)
            if item["helper_image_id"] == "fs2-acquisition-helper"
            else None
        )
        source_review = legal_reviews.get(model_id)
        plan_value["source_review"] = (
            copy.deepcopy(dict(source_review)) if source_review is not None else None
        )
        plan_value["qualification_priority"] = (
            copy.deepcopy(dict(qualification_priority))
            if qualification_priority["model_id"] == model_id
            else None
        )
        plans[model_id] = AcquisitionPlan(
            model_id=model_id,
            method=method,
            required_prerequisite_ids=tuple(required),
            _value=plan_value,
        )
    return MappingProxyType(plans)


def _load_compatibility_audit(
    catalog_root: Path,
    value: Any,
    records: Mapping[str, ModelRecord],
) -> Mapping[str, Mapping[str, Any]]:
    document = _exact(
        _load_bound_contract(
            catalog_root,
            value,
            expected_path="contracts/compatibility-audit.json",
            label="compatibility audit",
        ),
        {"schema", "audit_id", "recorded_at", "authority", "gpu_architecture", "records"},
        "compatibility audit",
    )
    if (
        document["schema"] != "fs2-serve.nebius.ai/compatibility-audit/v1"
        or document["audit_id"] != "manager-sm103-audit-2026-08-26"
        or document["authority"] != "agent-task-deck-manager-input"
        or document["gpu_architecture"] != "NVIDIA-SM103"
    ):
        raise CatalogError("unsupported compatibility audit authority or scope")
    _text(document["recorded_at"], "compatibility audit timestamp")
    raw_records = document["records"]
    if not isinstance(raw_records, dict) or set(raw_records) != set(records):
        raise CatalogError("compatibility audit must exactly cover the catalog")
    audited: dict[str, Mapping[str, Any]] = {}
    for model_id in sorted(raw_records):
        item = _exact(
            raw_records[model_id],
            {"execution_identity_sha256", "b300_state", "route_backend_policy", "notes"},
            f"compatibility audit {model_id}",
        )
        identity = strong_sha256(
            item["execution_identity_sha256"], "compatibility execution identity"
        )
        record = records[model_id].to_dict()
        if identity != execution_identity(record):
            raise CatalogError("compatibility audit belongs to another executable/model identity")
        audit_state = _enum(
            item["b300_state"],
            {"historical-supported", "incompatible-sm103", "unverified", "blocked"},
            "compatibility audit B300 state",
        )
        record_state = record["resources"]["gpu"]["b300_state"]
        allowed_record_states = (
            {audit_state}
            if audit_state in {"incompatible-sm103", "blocked"}
            else {audit_state, "qualified"}
        )
        if record_state not in allowed_record_states:
            raise CatalogError("catalog B300 state contradicts the immutable compatibility audit")
        policy = _enum(
            item["route_backend_policy"],
            {"exact-live-backend-required", "h100-h200-or-qualified-alternative-only", "blocked"},
            "compatibility route backend policy",
        )
        if (audit_state == "incompatible-sm103") != (
            policy == "h100-h200-or-qualified-alternative-only"
        ):
            raise CatalogError("SM103 exclusion and alternative-backend policy disagree")
        if audit_state == "blocked" and policy != "blocked":
            raise CatalogError("blocked compatibility must remain blocked")
        _text(item["notes"], "compatibility audit notes")
        audited[model_id] = MappingProxyType(copy.deepcopy(item))
    return MappingProxyType(audited)


def _load_federated_backends(
    catalog_root: Path,
    value: Any,
    records: Mapping[str, ModelRecord],
    prerequisites: Mapping[str, RuntimePrerequisite],
) -> Mapping[str, FederatedBackend]:
    document = _exact(
        _load_bound_contract(
            catalog_root,
            value,
            expected_path="contracts/federated-backends.json",
            label="federated backends",
        ),
        {"schema", "recorded_at", "authority", "identity_alias_policy", "records"},
        "federated backends",
    )
    if (
        document["schema"] != "fs2-serve.nebius.ai/federated-backends/v1"
        or document["authority"] != "agent-task-deck-manager-sm90-inventory"
        or document["identity_alias_policy"] != "forbidden"
    ):
        raise CatalogError("unsupported federated backend authority or alias policy")
    _text(document["recorded_at"], "federated backend inventory timestamp")
    raw_records = document["records"]
    required = {"diffdock", "evo2-40b", "molmim", "proteinmpnn", "rfdiffusion"}
    if not isinstance(raw_records, dict) or not required.issubset(raw_records):
        raise CatalogError("federated inventory omits required exact SM90 subjects")
    if set(raw_records) - set(records):
        raise CatalogError("federated inventory names an unknown model")
    loaded: dict[str, FederatedBackend] = {}
    for model_id in sorted(raw_records):
        item = _exact(
            raw_records[model_id],
            {
                "backend_class",
                "region",
                "gpu_class",
                "runtime_image_digest",
                "backend_state",
                "route_state",
                "runtime_image_state",
                "trust_state",
                "endpoint_identity_sha256",
                "trust_bundle_sha256",
                "credential_requirement_id",
                "preference",
                "qualification_requirements",
                "notes",
            },
            f"federated backend {model_id}",
        )
        record = records[model_id].to_dict()
        runtime_digest = _text(item["runtime_image_digest"], "federated runtime digest")
        if runtime_digest != record["runtime"]["image"]["digest"]:
            raise CatalogError("federated backend aliases a different runtime image digest")
        backend_class = _enum(
            item["backend_class"],
            {"federated-kserve-nim", "federated-serverless", "historical-h100-bridge"},
            "federated backend class",
        )
        route_state = _enum(
            item["route_state"],
            {"gated", "credential-compromised", "disabled", "qualified"},
            "federated route state",
        )
        backend_state = _enum(
            item["backend_state"],
            {"ready-observed", "running-observed", "historical-only", "qualified"},
            "federated backend state",
        )
        runtime_image_state = _enum(
            item["runtime_image_state"],
            {"digest-pinned", "mutable-unverified", "historical-exact-pin-only"},
            "federated runtime image state",
        )
        trust_state = _enum(
            item["trust_state"],
            {
                "authorized-observed-unattested",
                "credential-exposed-by-provider-diagnostics",
                "historical-no-live-service",
                "verified",
            },
            "federated trust state",
        )
        preference = _enum(
            item["preference"],
            {"best-current-exact-upstream", "candidate-after-remediation", "none"},
            "federated backend preference",
        )
        region = _text(item["region"], "federated backend region", nullable=True)
        gpu_class = _enum(
            item["gpu_class"],
            {"NVIDIA-H100-SXM-80GB", "NVIDIA-H200-SXM"},
            "federated backend GPU class",
        )
        endpoint_identity = _optional_digest(
            item["endpoint_identity_sha256"], "federated endpoint identity"
        )
        trust_bundle = _optional_digest(
            item["trust_bundle_sha256"], "federated trust bundle"
        )
        credential_id = _text(
            item["credential_requirement_id"],
            "federated credential requirement",
            nullable=True,
        )
        requirements = _list(
            item["qualification_requirements"], "federated qualification requirements", nonempty=True
        )
        if requirements != sorted(requirements) or len(requirements) != len(set(requirements)):
            raise CatalogError("federated qualification requirements must be sorted and unique")
        if credential_id is not None:
            prerequisite = prerequisites.get(credential_id)
            if prerequisite is None or prerequisite.kind != "Secret":
                raise CatalogError("federated credential requirement is not a declared Secret")
        if route_state == "qualified":
            if (
                backend_class == "historical-h100-bridge"
                or region is None
                or endpoint_identity is None
                or trust_bundle is None
                or backend_state != "qualified"
                or trust_state != "verified"
                or runtime_image_state != "digest-pinned"
                or credential_id is None
            ):
                raise CatalogError("qualified federation lacks exact backend trust and identity")
        else:
            if endpoint_identity is not None or trust_bundle is not None:
                raise CatalogError("unqualified federation may not publish trusted endpoint identities")
        if model_id == "molmim" and (
            backend_class != "federated-kserve-nim"
            or region != "us-central1"
            or gpu_class != "NVIDIA-H200-SXM"
            or preference != "best-current-exact-upstream"
        ):
            raise CatalogError("MolMIM exact preferred H200 upstream facts differ")
        if model_id == "molmim" and route_state != "qualified" and (
            route_state != "gated"
            or backend_state != "ready-observed"
            or trust_state != "authorized-observed-unattested"
        ):
            raise CatalogError("MolMIM observed readiness cannot authorize a route")
        if model_id == "evo2-40b" and backend_class != "federated-serverless":
            raise CatalogError("Evo2 backend class differs from the exact inventory")
        if model_id == "evo2-40b" and route_state != "qualified" and (
            route_state != "credential-compromised"
            or runtime_image_state != "mutable-unverified"
            or trust_state != "credential-exposed-by-provider-diagnostics"
            or backend_state != "running-observed"
        ):
            raise CatalogError("Evo2 compromised Serverless backend may not be promoted")
        if model_id in {"diffdock", "proteinmpnn", "rfdiffusion"} and (
            backend_class != "historical-h100-bridge"
            or route_state != "disabled"
            or runtime_image_state != "historical-exact-pin-only"
            or backend_state != "historical-only"
            or region is not None
            or credential_id is not None
        ):
            raise CatalogError("historical H100 bridge may not masquerade as a live service")
        _text(item["notes"], "federated backend notes")
        loaded[model_id] = FederatedBackend(
            model_id=model_id,
            backend_class=backend_class,
            route_state=route_state,
            _value=MappingProxyType(copy.deepcopy(item)),
        )
    return MappingProxyType(loaded)


def _load_scale_contracts(
    catalog_root: Path,
    value: Any,
    records: Mapping[str, ModelRecord],
) -> Mapping[str, ScaleContract]:
    """Load the immutable mutation boundary shared with the activation controller."""

    document = _exact(
        _load_bound_contract(
            catalog_root,
            value,
            expected_path="contracts/scale-contracts.json",
            label="scale contracts",
        ),
        {"schema", "controller_boundary", "policy_profiles", "contracts"},
        "scale contracts",
    )
    if document["schema"] != "fs2-serve.nebius.ai/scale-contracts/v6":
        raise CatalogError("unsupported scale contract schema")
    boundary = _exact(
        document["controller_boundary"],
        {
            "activation_controller",
            "activation_intent_interface",
            "activation_store",
            "batch_controller",
            "gateway_api",
            "gateway_kubernetes_mutation",
            "authorization",
            "scope",
        },
        "scale controller boundary",
    )
    if boundary != {
        "activation_controller": {
            "namespace": "fs2-system",
            "deployment_name": "fs2-serve-control-plane-activation",
            "service_account_name": "fs2-model-activation-controller",
            "leader_lease_name": "fs2-serve-activation-controller",
            "leader_role_namespace": "fs2-system",
            "leader_role_name": "fs2-serve-control-plane-activation-leader",
            "target_role_namespace": "fs2-models",
            "target_role_name": "fs2-serve-control-plane-activation-targets",
        },
        "activation_intent_interface": {
            "schema": "fs2-serve.nebius.ai/postgres-activation-intent/v3",
            "transport": "postgresql-durable-row",
            "intent_table": "fs2_activation_intents",
            "event_table": "fs2_activation_events",
            "target_state_table": "fs2_activation_target_state",
            "submission_owner": "fs2-system/fs2-serve-control-plane",
            "claim_owner": "fs2-system/fs2-model-activation-controller",
            "database_principals": {
                "claim_owner": {
                    "database_role": "fs2_activation_claim_owner",
                    "denied_grants": [
                        "DELETE:fs2_activation_intents",
                        "DDL:*",
                    ],
                    "grants": [
                        "EXECUTE:fs2_claim_activation_intent",
                        "EXECUTE:fs2_complete_activation_intent",
                        "INSERT:fs2_activation_events",
                        "SELECT:fs2_activation_intents",
                        "SELECT:fs2_activation_target_state",
                    ],
                    "kubernetes_subject": (
                        "system:serviceaccount:fs2-system:"
                        "fs2-model-activation-controller"
                    ),
                },
                "submitter": {
                    "database_role": "fs2_activation_submitter",
                    "denied_grants": [
                        "CLAIM:fs2_activation_intents",
                        "DELETE:fs2_activation_intents",
                        "DDL:*",
                        "UPDATE:fs2_activation_target_state",
                    ],
                    "grants": [
                        "EXECUTE:fs2_submit_activation_intent",
                        "SELECT_STATUS_ONLY:fs2_activation_intents",
                    ],
                    "kubernetes_subject": (
                        "system:serviceaccount:fs2-system:fs2-serve-control-plane"
                    ),
                },
            },
            "activation_subject_fields": [
                "intent_id",
                "operation_id",
                "operation_attempt",
                "model_id",
                "model_revision",
                "binding_digest",
                "action",
                "subject_sha256",
                "store_contract_sha256",
            ],
            "claim_fence_fields": [
                "fence_operation_id",
                "controller_id",
                "previous_fencing_token",
                "fencing_token",
                "database_now",
                "claim_started_at",
                "claim_lease_expires_at",
                "leader_lease_uid",
                "leader_lease_resource_version",
                "leader_lease_holder_identity",
                "submitter_service_account_uid",
                "claim_owner_service_account_uid",
            ],
            "leader_lease_fields": [
                "leader_lease_uid",
                "leader_lease_resource_version",
                "leader_lease_holder_identity",
                "leader_lease_renew_time",
                "leader_lease_duration_seconds",
            ],
            "completion_subject_fields": [
                "scale_contract_digest",
                "target_uid",
                "target_resource_version",
                "target_observed_generation",
                "target_template_digest",
                "target_active",
            ],
            "model_lock": "postgres-advisory-xact-lock-plus-target-state-cas",
        },
        "activation_store": {
            "schema": "fs2-serve.nebius.ai/postgres-activation-store/v1",
            "ddl": {
                "path": "sql/0001_activation_store.sql",
                "sha256": "b90fa9b5317b1abe40350b55fab0c8aeb35d7a0d6364454f19eda7c0475e5053",
            },
            "database_clock": "clock_timestamp",
            "functions": {
                "submit": "fs2_submit_activation_intent",
                "claim": "fs2_claim_activation_intent",
                "complete": "fs2_complete_activation_intent",
            },
            "fencing": {
                "scope": "monotonic-per-model",
                "allocation": "target-state-row-cas-plus-advisory-xact-lock",
                "claim_lease_clock": "database",
                "completion": "db-clock-token-controller-lease-cas",
                "idempotent_replay": "same-intent-and-subject-or-reject",
                "stale_token_action": "reject",
            },
            "credential_delivery": {
                "mode": "observed-pre-created-kubernetes-secrets",
                "values": "suppressed",
                "eso": "disabled-until-reviewed-eligible-provider-build-receipt",
                "secrets": {
                    "claim_owner": {
                        "namespace": "fs2-system",
                        "name": "fs2-activation-claim-owner-db",
                        "type": "Opaque",
                        "required_keys": ["dsn"],
                        "service_account_name": "fs2-model-activation-controller",
                        "database_role": "fs2_activation_claim_owner",
                    },
                    "submitter": {
                        "namespace": "fs2-system",
                        "name": "fs2-activation-submitter-db",
                        "type": "Opaque",
                        "required_keys": ["dsn"],
                        "service_account_name": "fs2-serve-control-plane",
                        "database_role": "fs2_activation_submitter",
                    },
                },
            },
            "pod_identity": {
                "submitter_deployment": "fs2-serve-control-plane",
                "claim_owner_deployment": "fs2-serve-control-plane-activation",
                "namespace": "fs2-system",
                "source": "api-server-observed-pod-uid-owner-uid-service-account-uid",
            },
        },
        "batch_controller": {
            "namespace": "fs2-models",
            "service_account_name": "batch-service-account",
        },
        "gateway_api": {
            "namespace": "fs2-system",
            "service_account_name": "fs2-serve-control-plane",
        },
        "gateway_kubernetes_mutation": "forbidden",
        "authorization": (
            "postgres-role-grants-plus-projected-ksa-lease-operation-fence"
        ),
        "scope": "namespaced-exact-subject-only",
    }:
        raise CatalogError("scale controller ownership or gateway mutation boundary differs")
    ddl = boundary["activation_store"]["ddl"]
    ddl_path = catalog_root / ddl["path"]
    if hashlib.sha256(_regular_bytes(ddl_path, maximum=256 * 1024)).hexdigest() != ddl["sha256"]:
        raise CatalogError("activation store DDL differs from its immutable contract")

    raw_profiles = document["policy_profiles"]
    if not isinstance(raw_profiles, dict) or set(raw_profiles) != {
        "batch-job-v1",
        "disabled-v1",
        "http-deployment-zero-to-one-v1",
        "http-nim-zero-to-one-v1",
    }:
        raise CatalogError("scale policy profiles differ from the closed controller set")
    profiles: dict[str, dict[str, Any]] = {}
    for profile_id in sorted(raw_profiles):
        profile = _exact(
            raw_profiles[profile_id],
            {
                "desired_floor",
                "desired_max",
                "scale_to_zero",
                "node_scaler_owner",
                "replica_scaler_owner",
                "cooldown_seconds",
                "drain_timeout_seconds",
                "cleanup_timeout_seconds",
                "preemption",
                "cleanup",
            },
            f"scale policy profile {profile_id}",
        )
        floor = profile["desired_floor"]
        maximum = profile["desired_max"]
        for label, number in (("floor", floor), ("maximum", maximum)):
            if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                raise CatalogError(f"scale policy {label} must be a non-negative integer")
        if floor > maximum or maximum > 1:
            raise CatalogError("scale policy replica bounds exceed the reviewed zero-to-one scope")
        _boolean(profile["scale_to_zero"], "scale policy scale_to_zero")
        if profile["node_scaler_owner"] != "nebius-managed-node-group-autoscaler":
            raise CatalogError("scale policy names a foreign node scaler")
        replica_scaler_owner = _enum(
            profile["replica_scaler_owner"],
            {
                "fs2-model-activation-controller",
                "fs2-model-batch-controller",
                "none",
            },
            "scale policy replica scaler owner",
        )
        expected_replica_scaler_owner = {
            "batch-job-v1": "fs2-model-batch-controller",
            "disabled-v1": "none",
            "http-deployment-zero-to-one-v1": "fs2-model-activation-controller",
            "http-nim-zero-to-one-v1": "fs2-model-activation-controller",
        }[profile_id]
        if replica_scaler_owner != expected_replica_scaler_owner:
            raise CatalogError("scale policy names the wrong replica scaler owner")
        for key in ("cooldown_seconds", "drain_timeout_seconds", "cleanup_timeout_seconds"):
            number = profile[key]
            if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                raise CatalogError(f"scale policy {key} must be a non-negative integer")
        preemption = _exact(
            profile["preemption"],
            {
                "notice_action",
                "new_admissions",
                "retry_policy",
                "retain_all_attempts",
            },
            "scale preemption policy",
        )
        if preemption != {
            "notice_action": "mark-unready-and-drain",
            "new_admissions": "stop",
            "retry_policy": "idempotent-only-within-deadline",
            "retain_all_attempts": True,
        }:
            raise CatalogError("scale preemption policy is not fail-closed")
        cleanup = _exact(
            profile["cleanup"],
            {
                "uid_fence_required",
                "expected_resource_uids_required",
                "foreign_uid_action",
                "zero_active_assignments_required",
                "zero_gpu_clients_required",
                "retained_cache_action",
                "expected_resource_kinds",
            },
            "scale cleanup policy",
        )
        kinds = _list(cleanup["expected_resource_kinds"], "scale cleanup resource kinds")
        if kinds != sorted(set(kinds)):
            raise CatalogError("scale cleanup resource kinds must be sorted and unique")
        if any(kind not in {"Deployment", "Job", "NIMService", "Pod"} for kind in kinds):
            raise CatalogError("scale cleanup resource kind is outside the model scope")
        if (
            cleanup["uid_fence_required"] is not True
            or cleanup["expected_resource_uids_required"] is not True
            or cleanup["foreign_uid_action"] != "forbidden"
            or cleanup["zero_active_assignments_required"] is not True
            or cleanup["zero_gpu_clients_required"] is not True
            or cleanup["retained_cache_action"] != "preserve-content-addressed"
        ):
            raise CatalogError("scale cleanup policy is not UID-fenced")
        profiles[profile_id] = copy.deepcopy(profile)

    raw_contracts = document["contracts"]
    if not isinstance(raw_contracts, dict) or set(raw_contracts) != set(records):
        raise CatalogError("scale contracts must exactly cover the catalog")
    loaded: dict[str, ScaleContract] = {}
    for model_id in sorted(raw_contracts):
        item = _exact(
            raw_contracts[model_id],
            {
                "model_digest",
                "execution_identity_sha256",
                "resource_placement_identity_sha256",
                "activation_mode",
                "policy_profile",
                "target",
                "readiness",
                "warmup",
            },
            f"scale contract {model_id}",
        )
        record = records[model_id]
        record_value = record.to_dict()
        if item["model_digest"] != record.digest:
            raise CatalogError("scale contract belongs to another model record")
        if strong_sha256(
            item["execution_identity_sha256"], "scale execution identity"
        ) != execution_identity(record_value):
            raise CatalogError("scale contract belongs to another executable identity")
        if strong_sha256(
            item["resource_placement_identity_sha256"],
            "scale resource placement identity",
        ) != resource_placement_identity(record_value):
            raise CatalogError("scale contract belongs to another allocation/placement identity")
        activation_mode = _enum(
            item["activation_mode"],
            {"batch-job", "disabled", "replica-scale"},
            "scale activation mode",
        )
        expected_mode = {
            "http": "replica-scale",
            "batch": "batch-job",
            "unavailable": "disabled",
        }[record_value["interface"]["execution_mode"]]
        if activation_mode != expected_mode:
            raise CatalogError("scale activation mode differs from the model interface")
        profile_id = _text(item["policy_profile"], "scale policy profile")
        expected_profile = (
            "http-nim-zero-to-one-v1"
            if activation_mode == "replica-scale"
            and record_value["runtime"]["kind"] == "nim"
            else {
                "replica-scale": "http-deployment-zero-to-one-v1",
                "batch-job": "batch-job-v1",
                "disabled": "disabled-v1",
            }[activation_mode]
        )
        if profile_id != expected_profile:
            raise CatalogError("scale contract selects the wrong immutable policy profile")
        profile = profiles[expected_profile]
        target = item["target"]
        if activation_mode == "disabled":
            if target is not None or item["readiness"] is not None or item["warmup"] is not None:
                raise CatalogError("disabled scale contract cannot imply a target or probe")
        else:
            target = _exact(
                target,
                {
                    "api_version",
                    "kind",
                    "namespace",
                    "name",
                    "uid_source",
                    "selector",
                    "template_identity_sha256",
                },
                "scale target",
            )
            if target["namespace"] != "fs2-models":
                raise CatalogError("scale target is outside the model namespace")
            selector = target["selector"]
            expected_selector = {"fs2-serve.nebius.ai/model-id": model_id}
            if activation_mode == "batch-job":
                expected_selector["fs2-serve.nebius.ai/job-kind"] = "batch"
                expected_target = ("batch/v1", "Job", None, "signed-activation-receipt")
            elif record_value["runtime"]["kind"] == "nim":
                expected_target = (
                    "apps.nvidia.com/v1alpha1",
                    "NIMService",
                    model_id,
                    "signed-serving-binding",
                )
            else:
                expected_target = ("apps/v1", "Deployment", model_id, "signed-serving-binding")
            if (
                target["api_version"],
                target["kind"],
                target["name"],
                target["uid_source"],
            ) != expected_target or selector != dict(sorted(expected_selector.items())):
                raise CatalogError("scale target differs from the exact model-owned subject")
            template_subject = {
                "api_version": target["api_version"],
                "kind": target["kind"],
                "namespace": target["namespace"],
                "name": target["name"],
                "selector": target["selector"],
                "model_digest": record.digest,
                "execution_identity_sha256": execution_identity(record_value),
                "resource_placement_identity_sha256": resource_placement_identity(
                    record_value
                ),
            }
            expected_template = hashlib.sha256(_canonical_bytes(template_subject)).hexdigest()
            if strong_sha256(
                target["template_identity_sha256"], "scale target template identity"
            ) != expected_template:
                raise CatalogError("scale target template identity differs")
            if item["readiness"] != record_value["interface"]["readiness"]:
                raise CatalogError("scale readiness differs from the model interface")
            if item["warmup"] != record_value["interface"]["warmup"]:
                raise CatalogError("scale warmup differs from the model interface")
        expanded = {
            "schema": "fs2-serve.nebius.ai/model-scale-contract/v5",
            "model_id": model_id,
            **copy.deepcopy(item),
            "controller_boundary": copy.deepcopy(boundary),
            "policy": copy.deepcopy(profile),
        }
        digest = hashlib.sha256(_canonical_bytes(expanded)).hexdigest()
        loaded[model_id] = ScaleContract(
            model_id=model_id,
            digest=digest,
            activation_mode=activation_mode,
            _value=MappingProxyType(expanded),
        )
    return MappingProxyType(loaded)


def load_catalog(root: Path | str, *, repo_root: Path | str | None = None) -> Catalog:
    """Load and validate one exact catalog tree without network or cluster I/O."""

    catalog_root = Path(root).resolve()
    if repo_root is None:
        packaged_repository = catalog_root / "packaged-repository"
        if packaged_repository.is_dir():
            repository = packaged_repository
        else:
            try:
                repository = catalog_root.parents[2]
            except IndexError as exc:
                raise CatalogError("catalog path is not below a repository root") from exc
    else:
        repository = Path(repo_root).resolve()
    index_path = catalog_root / "catalog.json"
    index = _exact(
        _load_json(index_path),
        {
            "schema",
            "catalog_version",
            "loader_contract",
            "model_schema",
            "artifact_acquisition",
            "compatibility_audit",
            "federated_backends",
            "provenance_lock",
            "runtime_prerequisites",
            "scale_contracts",
            "semantic_requests",
            "model_variants",
            "model_files",
            "tested_model_ids",
            "blocked_candidate_ids",
        },
        "catalog index",
    )
    if index["schema"] != CATALOG_SCHEMA or index["loader_contract"] != LOADER_CONTRACT:
        raise CatalogError("catalog or loader contract version differs")
    if index["model_schema"] != MODEL_SCHEMA:
        raise CatalogError("catalog model schema version differs")
    runtime_prerequisites = _load_runtime_prerequisites(
        catalog_root, index["runtime_prerequisites"]
    )
    provenance_lock = _load_provenance_lock(catalog_root, index["provenance_lock"])
    used_provenance: set[tuple[str, str, str, str]] = set()
    version = _text(index["catalog_version"], "catalog_version")
    assert version is not None
    model_files = _list(index["model_files"], "model_files", nonempty=True)
    if model_files != sorted(model_files) or len(model_files) != len(set(model_files)):
        raise CatalogError("model_files must be unique and sorted")
    actual_files = sorted(path.name for path in (catalog_root / "models").glob("*.json"))
    if model_files != actual_files:
        raise CatalogError("catalog index and model directory differ")

    records: dict[str, ModelRecord] = {}
    canonical_records: list[dict[str, Any]] = []
    for filename in model_files:
        if Path(filename).name != filename or not filename.endswith(".json"):
            raise CatalogError("model filename is unsafe")
        path = catalog_root / "models" / filename
        value = _validate_model(
            _load_json(path),
            filename,
            repository,
            catalog_root,
            provenance_lock,
            used_provenance,
        )
        model_id = value["model"]["id"]
        if model_id in records:
            raise CatalogError(f"duplicate model ID: {model_id}")
        canonical = _canonical_bytes(value)
        records[model_id] = ModelRecord(
            model_id=model_id,
            path=path,
            digest=hashlib.sha256(canonical).hexdigest(),
            _value=value,
        )
        canonical_records.append(value)

    tested = tuple(index["tested_model_ids"])
    blocked = tuple(index["blocked_candidate_ids"])
    if tuple(sorted(tested)) != tested or not REQUIRED_TESTED_MODEL_IDS.issubset(tested):
        missing = sorted(REQUIRED_TESTED_MODEL_IDS - set(tested))
        raise CatalogError(f"catalog omits required tested model IDs: {missing}")
    if tuple(sorted(blocked)) != blocked or set(tested).intersection(blocked):
        raise CatalogError("blocked candidates must be sorted and disjoint")
    actual_tested = tuple(sorted(key for key, record in records.items() if record._value["model"]["tested_lane"]))
    actual_blocked = tuple(sorted(key for key, record in records.items() if record._value["support"]["state"] == "blocked"))
    if tested != actual_tested or blocked != actual_blocked:
        raise CatalogError("catalog classifications differ from model records")
    if set(records) != set(tested).union(blocked):
        raise CatalogError("every record must be tested or explicitly blocked")
    if used_provenance != set(provenance_lock):
        raise CatalogError("packaged provenance lock contains unused or missing subjects")
    acquisition_plans = _load_acquisition_plans(
        catalog_root,
        index["artifact_acquisition"],
        records,
        runtime_prerequisites,
    )
    compatibility_audit = _load_compatibility_audit(
        catalog_root, index["compatibility_audit"], records
    )
    federated_backends = _load_federated_backends(
        catalog_root,
        index["federated_backends"],
        records,
        runtime_prerequisites,
    )
    semantic_requests = _load_semantic_requests(
        catalog_root,
        index["semantic_requests"],
        records,
    )
    scale_contracts = _load_scale_contracts(
        catalog_root,
        index["scale_contracts"],
        records,
    )
    model_variants, fallback_candidates = _load_model_variants(
        catalog_root,
        index["model_variants"],
        records,
        compatibility_audit,
    )
    for model_id, record in records.items():
        record_value = record.to_dict()
        if not record.route_exposed:
            continue
        if semantic_requests[model_id].state != "qualified":
            raise CatalogError("route exposure requires a qualified semantic request contract")
        b300_state = record_value["resources"]["gpu"]["b300_state"]
        if b300_state == "qualified":
            continue
        federation = federated_backends.get(model_id)
        if federation is None or federation.route_state != "qualified":
            raise CatalogError(
                "route exposure requires qualified B300 or qualified exact alternative backend"
            )

    digest = hashlib.sha256(_canonical_bytes({"index": index, "models": canonical_records})).hexdigest()
    return Catalog(
        version=version,
        digest=digest,
        records=MappingProxyType(records),
        tested_model_ids=tested,
        blocked_candidate_ids=blocked,
        acquisition_plans=acquisition_plans,
        compatibility_audit=compatibility_audit,
        runtime_prerequisites=runtime_prerequisites,
        federated_backends=federated_backends,
        semantic_requests=semantic_requests,
        scale_contracts=scale_contracts,
        model_variants=model_variants,
        fallback_candidates=fallback_candidates,
    )

#!/usr/bin/env python3
"""Typed, fail-closed cold-start instrumentation and snapshot eligibility.

This module is deliberately source-only.  It consumes bounded Kubernetes and
qualification receipts, but it never calls kubectl, changes replicas, or
creates cloud resources.  The disposable benchmark runner owns those actions.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes
from uuid import uuid4

import yaml
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parent
MATRIX_PATH = ROOT / "cold-start-optimization-matrix.json"
MATRIX_SCHEMA_PATH = ROOT / "cold-start-optimization-matrix.schema.json"
BENCHMARK_RECEIPT_SCHEMA_PATH = ROOT / "post-acceptance-benchmark-receipt.schema.json"
PHASE_OBSERVATION_SCHEMA_PATH = ROOT / "startup-phase-observation.schema.json"
MAX_JSON_BYTES = 32 * 1024 * 1024
PROTECTED_CLUSTER_IDS = frozenset(
    {
        "mk8scluster-exampleone",
        "mk8scluster-exampletwo",
    }
)
CANONICAL_EVENTS = (
    "activation-accepted",
    "capacity-requested",
    "provider-instance-created",
    "node-ready",
    "workload-admitted",
    "pod-scheduled",
    "storage-attached",
    "image-or-image-volume-pull-start",
    "image-or-image-volume-pull-end",
    "artifact-localization-start",
    "artifact-localization-verified",
    "runtime-process-start",
    "weight-load-start",
    "weight-load-end",
    "engine-build-or-compile-start",
    "engine-build-or-compile-end",
    "checkpoint-restore-start",
    "checkpoint-restore-end",
    "readiness-accepted",
    "semantic-call1-accepted",
    "semantic-call2-accepted",
    "return-to-zero-accepted",
)
PHASE_GROUP_EVENTS = {
    "pod-scheduled": ("pod-scheduled",),
    "image-pull": (
        "image-or-image-volume-pull-start",
        "image-or-image-volume-pull-end",
    ),
    "artifact-localization": (
        "artifact-localization-start",
        "artifact-localization-verified",
    ),
    "runtime-process": ("runtime-process-start",),
    "weight-load": ("weight-load-start", "weight-load-end"),
    "engine-build-or-compile": (
        "engine-build-or-compile-start",
        "engine-build-or-compile-end",
    ),
    "readiness": ("readiness-accepted",),
    "semantic-calls": (
        "semantic-call1-accepted",
        "semantic-call2-accepted",
    ),
    "return-to-zero": ("return-to-zero-accepted",),
}
ORDERED_EVENT_PAIRS = (
    ("activation-accepted", "capacity-requested"),
    ("activation-accepted", "workload-admitted"),
    ("activation-accepted", "pod-scheduled"),
    ("capacity-requested", "provider-instance-created"),
    ("provider-instance-created", "node-ready"),
    ("node-ready", "pod-scheduled"),
    ("workload-admitted", "pod-scheduled"),
    ("pod-scheduled", "image-or-image-volume-pull-start"),
    ("image-or-image-volume-pull-start", "image-or-image-volume-pull-end"),
    ("pod-scheduled", "artifact-localization-start"),
    ("artifact-localization-start", "artifact-localization-verified"),
    ("pod-scheduled", "runtime-process-start"),
    ("storage-attached", "runtime-process-start"),
    ("runtime-process-start", "weight-load-start"),
    ("weight-load-start", "weight-load-end"),
    ("runtime-process-start", "engine-build-or-compile-start"),
    ("engine-build-or-compile-start", "engine-build-or-compile-end"),
    ("checkpoint-restore-start", "checkpoint-restore-end"),
    ("image-or-image-volume-pull-end", "readiness-accepted"),
    ("runtime-process-start", "readiness-accepted"),
    ("checkpoint-restore-end", "readiness-accepted"),
    ("readiness-accepted", "semantic-call1-accepted"),
    ("artifact-localization-verified", "semantic-call1-accepted"),
    ("weight-load-end", "semantic-call1-accepted"),
    ("engine-build-or-compile-end", "semantic-call1-accepted"),
    ("semantic-call1-accepted", "semantic-call2-accepted"),
    ("semantic-call2-accepted", "return-to-zero-accepted"),
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ColdStartContractError(RuntimeError):
    """Stable validation failure without secret or payload material."""


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ColdStartContractError("json_duplicate_key")
        value[key] = item
    return value


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_absolute():
        path = path.resolve()
    try:
        metadata = path.stat()
    except OSError:
        raise ColdStartContractError("json_file_unavailable") from None
    if not path.is_file() or not 1 <= metadata.st_size <= MAX_JSON_BYTES:
        raise ColdStartContractError("json_file_invalid")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ColdStartContractError("json_parse_failed") from None
    if not isinstance(value, dict):
        raise ColdStartContractError("json_root_not_object")
    return value


def canonical_digest(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def _schema_errors(value: Mapping[str, Any], schema: Mapping[str, Any]) -> list[str]:
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    return [
        "/".join(str(part) for part in error.absolute_path) or "<root>"
        for error in errors
    ]


def _resolve_source_path(reference_path: str, fs2_root: Path) -> Path:
    """Resolve one matrix-owned source without allowing an FS2-root escape."""

    if not reference_path or Path(reference_path).is_absolute():
        raise ColdStartContractError("matrix_source_reference_invalid")
    try:
        source_path = (ROOT / reference_path).resolve()
        resolved_root = fs2_root.resolve()
        contained = source_path.is_relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        raise ColdStartContractError("matrix_source_reference_invalid") from None
    if not contained:
        raise ColdStartContractError("matrix_source_path_outside_fs2")
    if not source_path.is_file():
        raise ColdStartContractError("matrix_source_reference_invalid")
    return source_path


def _decode_json_pointer(fragment: str) -> list[str]:
    """Decode an RFC 6901 URI-fragment pointer with strict escape handling."""

    for index, character in enumerate(fragment):
        if character == "%" and (
            index + 2 >= len(fragment)
            or re.fullmatch(r"[0-9A-Fa-f]{2}", fragment[index + 1 : index + 3])
            is None
        ):
            raise ColdStartContractError("matrix_json_pointer_invalid")
    try:
        pointer = unquote_to_bytes(fragment).decode("utf-8")
    except UnicodeDecodeError:
        raise ColdStartContractError("matrix_json_pointer_invalid") from None
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ColdStartContractError("matrix_json_pointer_invalid")

    decoded: list[str] = []
    for raw_token in pointer[1:].split("/"):
        token: list[str] = []
        index = 0
        while index < len(raw_token):
            character = raw_token[index]
            if character != "~":
                token.append(character)
                index += 1
                continue
            if index + 1 >= len(raw_token) or raw_token[index + 1] not in "01":
                raise ColdStartContractError("matrix_json_pointer_invalid")
            token.append("~" if raw_token[index + 1] == "0" else "/")
            index += 2
        decoded.append("".join(token))
    return decoded


def _resolve_json_scalar_source(reference: str, fs2_root: Path) -> Any:
    """Resolve a contained JSON source and return its RFC 6901 scalar."""

    if not isinstance(reference, str) or reference.count("#") != 1:
        raise ColdStartContractError("matrix_json_pointer_invalid")
    relative, fragment = reference.split("#", 1)
    source_path = _resolve_source_path(relative, fs2_root)
    if source_path.suffix.lower() != ".json":
        raise ColdStartContractError("matrix_json_source_not_json")
    tokens = _decode_json_pointer(fragment)
    value: Any = load_json(source_path)
    for token in tokens:
        if isinstance(value, dict):
            if token not in value:
                raise ColdStartContractError("matrix_json_pointer_not_found")
            value = value[token]
        elif isinstance(value, list):
            if re.fullmatch(r"0|[1-9][0-9]*", token) is None:
                raise ColdStartContractError("matrix_json_pointer_not_found")
            index = int(token)
            if index >= len(value):
                raise ColdStartContractError("matrix_json_pointer_not_found")
            value = value[index]
        else:
            raise ColdStartContractError("matrix_json_pointer_not_found")
    if isinstance(value, (dict, list)):
        raise ColdStartContractError("matrix_json_pointer_non_scalar")
    return value


def _resolve_kubernetes_yaml_scalar(reference: str, fs2_root: Path) -> Any:
    """Resolve `Kind/name/path...` from one contained multi-document YAML."""

    if not isinstance(reference, str) or reference.count("#") != 1:
        raise ColdStartContractError("matrix_kubernetes_selector_invalid")
    relative, selector = reference.split("#", 1)
    source_path = _resolve_source_path(relative, fs2_root)
    if source_path.suffix.lower() not in {".yaml", ".yml"}:
        raise ColdStartContractError("matrix_kubernetes_source_not_yaml")
    parts = selector.split("/")
    if len(parts) < 3 or any(not part for part in parts):
        raise ColdStartContractError("matrix_kubernetes_selector_invalid")
    kind, name, *value_path = parts
    try:
        documents = list(yaml.safe_load_all(source_path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ColdStartContractError("matrix_kubernetes_source_invalid") from None
    matches = [
        document
        for document in documents
        if isinstance(document, dict)
        and document.get("kind") == kind
        and isinstance(document.get("metadata"), dict)
        and document["metadata"].get("name") == name
    ]
    if len(matches) != 1:
        raise ColdStartContractError("matrix_kubernetes_selector_not_found")
    value: Any = matches[0]
    for part in value_path:
        if not isinstance(value, dict) or part not in value:
            raise ColdStartContractError("matrix_kubernetes_selector_not_found")
        value = value[part]
    if isinstance(value, (dict, list)):
        raise ColdStartContractError("matrix_kubernetes_selector_non_scalar")
    return value


def _normalized_sha256(value: Any, error_code: str) -> str:
    """Return the exact annotation form without trimming or case folding."""

    if not isinstance(value, str):
        raise ColdStartContractError(error_code)
    if _DIGEST.fullmatch(value) is not None:
        return "sha256:" + value
    if _IMAGE_DIGEST.fullmatch(value) is not None:
        return value
    raise ColdStartContractError(error_code)


def validate_matrix(
    matrix: Mapping[str, Any], *, fs2_root: Path | None = None
) -> None:
    schema = load_json(MATRIX_SCHEMA_PATH)
    errors = _schema_errors(matrix, schema)
    if errors:
        raise ColdStartContractError("matrix_schema_invalid:" + ",".join(errors[:8]))

    models = matrix["models"]
    model_ids = [item["model_id"] for item in models]
    if len(model_ids) != len(set(model_ids)):
        raise ColdStartContractError("matrix_model_ids_not_unique")
    services = [item["service"] for item in models]
    if len(services) != len(set(services)):
        raise ColdStartContractError("matrix_services_not_unique")

    floor_policies = matrix["floor_policies"]
    required_identity_sources = {
        "deployment-annotations",
        "pod-image-ids",
        "artifact-manifest-receipt",
        "runtime-argv",
        "runtime-environment-digest",
        "node-labels",
        "cuda-in-container-receipt",
        "topology-inventory-receipt",
        "allocated-device-receipt",
        "storage-identity-receipt",
    }
    for model in models:
        if model["floor_policy"] not in floor_policies:
            raise ColdStartContractError("matrix_floor_policy_unknown")
        if set(model["identity_sources"]) != required_identity_sources:
            raise ColdStartContractError("matrix_identity_sources_incomplete")
        if model["capabilities"]["conventional"]["state"] != "active":
            raise ColdStartContractError("matrix_conventional_not_active")
        if any(
            model["capabilities"][mechanism]["state"] == "active"
            for mechanism in ("cuda-criu-snapshot", "dynamo-snapshot")
        ):
            raise ColdStartContractError("matrix_snapshot_active_by_default")

    identity_contract = matrix["deployment_identity_contract"]
    identity_models = identity_contract["models"]
    if set(identity_models) != set(model_ids):
        raise ColdStartContractError("matrix_identity_model_partition_invalid")
    if fs2_root is None:
        fs2_root = ROOT.parent.parent
    resolved_compile_cache_abi = _resolve_kubernetes_yaml_scalar(
        identity_contract["compile_cache_abi_source"], fs2_root
    )
    if resolved_compile_cache_abi != identity_contract["compile_cache_abi"]:
        raise ColdStartContractError("matrix_compile_cache_abi_source_mismatch")
    required_annotations = set(identity_contract["required_annotations"])
    compile_cache_abi = identity_contract["compile_cache_abi"]
    for identity in identity_models.values():
        annotations = identity["annotations"]
        if annotations["fs2.nebius/compile-cache-abi"] != compile_cache_abi:
            raise ColdStartContractError("matrix_identity_compile_cache_abi_mismatch")
        missing = set(identity["missing_annotations"])
        if set(annotations) | missing != required_annotations:
            raise ColdStartContractError("matrix_identity_annotation_partition_invalid")
        if set(annotations) & missing:
            raise ColdStartContractError("matrix_identity_annotation_overlap")
        if identity["state"] == "complete":
            if (
                missing
                or not isinstance(identity["model_content_source"], str)
                or identity["blocker"] is not None
                or identity["blocker_source"] is not None
            ):
                raise ColdStartContractError("matrix_complete_identity_invalid")
            resolved_content_digest = _normalized_sha256(
                _resolve_json_scalar_source(identity["model_content_source"], fs2_root),
                "matrix_model_content_source_digest_invalid",
            )
            if (
                resolved_content_digest
                != annotations["fs2.nebius/model-content-digest"]
            ):
                raise ColdStartContractError("matrix_model_content_digest_mismatch")
        elif (
            missing != {"fs2.nebius/model-content-digest"}
            or identity["model_content_source"] is not None
            or not isinstance(identity["blocker"], str)
            or not isinstance(identity["blocker_source"], str)
        ):
            raise ColdStartContractError("matrix_blocked_identity_invalid")

    marker_contract = matrix["runtime_marker_contract"]
    instrumented = set(marker_contract["source_instrumented_models"])
    unqualified = set(marker_contract["unqualified_models"])
    if instrumented & unqualified or instrumented | unqualified != set(model_ids):
        raise ColdStartContractError("matrix_runtime_marker_partition_invalid")
    accepted_marker_events = set(marker_contract["accepted_events"])
    if not accepted_marker_events <= set(CANONICAL_EVENTS):
        raise ColdStartContractError("matrix_runtime_marker_event_unknown")

    baseline_ids = {
        observation["model_id"]
        for observation in matrix["baseline_evidence"]["observations"]
    }
    if not baseline_ids <= set(model_ids):
        raise ColdStartContractError("matrix_baseline_model_unknown")

    profile_path = (ROOT / matrix["catalog_contract"]["model_profile_path"]).resolve()
    route_path = (ROOT / matrix["catalog_contract"]["route_inventory_path"]).resolve()
    semantic_path = (ROOT / matrix["catalog_contract"]["semantic_contract_path"]).resolve()
    if fs2_root.resolve() not in profile_path.parents:
        raise ColdStartContractError("matrix_profile_path_outside_fs2")
    if fs2_root.resolve() not in route_path.parents:
        raise ColdStartContractError("matrix_route_path_outside_fs2")
    if fs2_root.resolve() not in semantic_path.parents:
        raise ColdStartContractError("matrix_semantic_path_outside_fs2")

    source_references = [
        identity["blocker_source"]
        for identity in identity_models.values()
        if identity["blocker_source"] is not None
    ]
    source_references.extend(
        source
        for marker in marker_contract["source_instrumented_models"].values()
        for source in marker["sources"]
    )
    for reference in source_references:
        _resolve_source_path(reference.split("#", 1)[0], fs2_root)

    profiles = load_json(profile_path)
    routes = load_json(route_path)
    semantics = load_json(semantic_path)
    expected_ids = profiles["profiles"]["full_catalog"]["canonical_routes"]
    if model_ids != expected_ids:
        raise ColdStartContractError("matrix_catalog_order_or_membership_mismatch")
    targets = profiles["model_autoscaling_targets"]
    route_values = routes["routes"]
    semantic_values = (
        semantics.get("contracts")
        or semantics.get("models")
        or semantics.get("requests")
    )
    if not isinstance(semantic_values, dict):
        raise ColdStartContractError("semantic_contract_shape_invalid")
    for model in models:
        model_id = model["model_id"]
        if model["deployment"] != targets[model_id]["deployment"]:
            raise ColdStartContractError("matrix_deployment_mismatch")
        if model["gpu_count"] != targets[model_id]["gpu_count"]:
            raise ColdStartContractError("matrix_gpu_count_mismatch")
        if model["service"] != route_values[model_id]["service"]["name"]:
            raise ColdStartContractError("matrix_service_mismatch")
        if model_id not in semantic_values:
            raise ColdStartContractError("matrix_semantic_contract_missing")


def matrix_model(matrix: Mapping[str, Any], model_id: str) -> Mapping[str, Any]:
    matches = [model for model in matrix["models"] if model["model_id"] == model_id]
    if len(matches) != 1:
        raise ColdStartContractError("matrix_model_not_unique")
    return matches[0]


def validate_deployment_identity_binding(
    matrix: Mapping[str, Any],
    *,
    model_id: str,
    compatibility_tuple: Mapping[str, Any],
) -> str:
    """Bind one runnable attempt to the matrix's complete immutable identity."""

    validate_matrix(matrix)
    validate_compatibility_tuple(compatibility_tuple)
    if compatibility_tuple["model_id"] != model_id:
        raise ColdStartContractError("deployment_identity_model_mismatch")
    identity = matrix["deployment_identity_contract"]["models"].get(model_id)
    if not isinstance(identity, dict) or identity.get("state") != "complete":
        raise ColdStartContractError("deployment_identity_not_complete")
    annotations = identity["annotations"]
    expected_content = annotations["fs2.nebius/model-content-digest"].removeprefix(
        "sha256:"
    )
    if compatibility_tuple["model_content_digest"] != expected_content:
        raise ColdStartContractError("deployment_identity_model_content_mismatch")
    if compatibility_tuple["artifact_content_digest"] != expected_content:
        raise ColdStartContractError("deployment_identity_artifact_content_mismatch")
    if (
        compatibility_tuple["runtime_image_digest"]
        != annotations["fs2.nebius/runtime-image-digest"]
    ):
        raise ColdStartContractError("deployment_identity_runtime_image_mismatch")
    if (
        compatibility_tuple["compile_cache_abi"]
        != annotations["fs2.nebius/compile-cache-abi"]
    ):
        raise ColdStartContractError("deployment_identity_compile_cache_abi_mismatch")
    return canonical_digest(identity)


def validate_compatibility_tuple(value: Mapping[str, Any]) -> None:
    receipt_schema = load_json(BENCHMARK_RECEIPT_SCHEMA_PATH)
    tuple_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **receipt_schema["$defs"]["compatibilityTuple"],
        "$defs": receipt_schema["$defs"],
    }
    errors = _schema_errors(value, tuple_schema)
    if errors:
        raise ColdStartContractError(
            "compatibility_tuple_invalid:" + ",".join(errors[:8])
        )
    if len(value["allocated_gpu_uuids"]) != value["workload_gpu_count"]:
        raise ColdStartContractError("compatibility_tuple_device_count_mismatch")
    if (value["mig_mode"] == "disabled") != (value["mig_profile"] is None):
        raise ColdStartContractError("compatibility_tuple_mig_inconsistent")


def validate_closed_promotion_receipt(value: Mapping[str, Any]) -> None:
    """Reuse the spike's closed schema plus executable statistical validator."""

    path = ROOT / "validate_post_acceptance_receipt.py"
    spec = importlib.util.spec_from_file_location(
        "fs2_post_acceptance_receipt_validator", path
    )
    if spec is None or spec.loader is None:
        raise ColdStartContractError("promotion_validator_unavailable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        module.validate_receipt(value, load_json(BENCHMARK_RECEIPT_SCHEMA_PATH))
    except BaseException:  # noqa: BLE001 - expose only a stable contract code.
        raise ColdStartContractError("promotion_receipt_invalid") from None


def _parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ColdStartContractError("timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ColdStartContractError("timestamp_invalid") from None
    return parsed.astimezone(UTC)


def evaluate_snapshot_eligibility(
    matrix: Mapping[str, Any],
    *,
    model_id: str,
    mechanism: str,
    donor: Mapping[str, Any],
    target: Mapping[str, Any],
    qualification: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a deterministic denial or exact-tuple experimental eligibility.

    Eligibility is not production promotion.  It only permits the next
    isolated benchmark attempt and never relaxes the conventional fallback.
    """

    validate_matrix(matrix)
    validate_compatibility_tuple(donor)
    validate_compatibility_tuple(target)
    model = matrix_model(matrix, model_id)
    reasons: list[str] = []
    if mechanism not in {"cuda-criu-snapshot", "dynamo-snapshot"}:
        reasons.append("mechanism_not_snapshot")
    elif model["capabilities"][mechanism]["state"] != "eligible-experiment":
        reasons.append("model_capability_blocked")
    if donor["model_id"] != model_id or target["model_id"] != model_id:
        reasons.append("model_identity_mismatch")
    if donor["mig_mode"] != target["mig_mode"]:
        reasons.append("cross_partition_restore_denied")
    partition = "full-gpu" if donor["mig_mode"] == "disabled" else "mig"
    partition_contract = matrix["snapshot_eligibility"][
        "full_gpu" if partition == "full-gpu" else "mig"
    ]
    if donor["mig_mode"] != partition_contract["required_mig_mode"]:
        reasons.append("partition_contract_mismatch")
    for field in matrix["snapshot_eligibility"]["exact_match_fields"]:
        if donor.get(field) != target.get(field):
            reasons.append("tuple_mismatch:" + field)

    if qualification is None:
        reasons.append("qualification_receipt_missing")
    else:
        required = {
            "schema",
            "status",
            "mechanism",
            "model_id",
            "qualification_scope",
            "donor_tuple_digest",
            "target_tuple_digest",
            "resource_identity",
            "semantic_equivalence_passed",
            "conventional_fallback_passed",
            "observed_at",
            "valid_until",
        }
        if set(qualification) != required:
            reasons.append("qualification_shape_invalid")
        else:
            if (
                qualification["schema"]
                != "fs2-serve.nebius.ai/snapshot-experiment-qualification/v1"
            ):
                reasons.append("qualification_schema_invalid")
            if qualification["status"] != "PASS":
                reasons.append("qualification_not_pass")
            if qualification["mechanism"] != mechanism:
                reasons.append("qualification_mechanism_mismatch")
            if qualification["model_id"] != model_id:
                reasons.append("qualification_model_mismatch")
            if qualification["qualification_scope"] != partition:
                reasons.append("qualification_partition_mismatch")
            if qualification["donor_tuple_digest"] != canonical_digest(donor):
                reasons.append("qualification_donor_digest_mismatch")
            if qualification["target_tuple_digest"] != canonical_digest(target):
                reasons.append("qualification_target_digest_mismatch")
            if qualification["semantic_equivalence_passed"] is not True:
                reasons.append("semantic_equivalence_missing")
            if qualification["conventional_fallback_passed"] is not True:
                reasons.append("conventional_fallback_missing")
            try:
                observed_at = _parse_utc(qualification["observed_at"])
                valid_until = _parse_utc(qualification["valid_until"])
                reference = now or datetime.now(UTC)
                if not observed_at <= reference < valid_until:
                    reasons.append("qualification_not_current")
            except ColdStartContractError:
                reasons.append("qualification_timestamp_invalid")

            identity = qualification.get("resource_identity")
            if not isinstance(identity, dict):
                reasons.append("qualification_resource_identity_invalid")
            elif partition == "full-gpu":
                expected = {
                    "resource_name",
                    "donor_device_uuids",
                    "target_device_uuids",
                    "gpu_topology_inventory_digest",
                    "donor_node_identity_digest",
                    "target_node_identity_digest",
                    "donor_pvc_identity_digest",
                    "target_pvc_identity_digest",
                }
                if set(identity) != expected:
                    reasons.append("full_gpu_resource_identity_shape_invalid")
                else:
                    if identity["resource_name"] != "nvidia.com/gpu":
                        reasons.append("full_gpu_resource_name_invalid")
                    if identity["donor_device_uuids"] != donor["allocated_gpu_uuids"]:
                        reasons.append("full_gpu_donor_devices_mismatch")
                    if identity["target_device_uuids"] != target["allocated_gpu_uuids"]:
                        reasons.append("full_gpu_target_devices_mismatch")
                    if (
                        identity["gpu_topology_inventory_digest"]
                        != target["gpu_topology_inventory_digest"]
                    ):
                        reasons.append("full_gpu_topology_receipt_mismatch")
                    for identity_field, tuple_value in (
                        ("donor_node_identity_digest", donor["node_identity_digest"]),
                        ("target_node_identity_digest", target["node_identity_digest"]),
                        ("donor_pvc_identity_digest", donor["pvc_identity_digest"]),
                        ("target_pvc_identity_digest", target["pvc_identity_digest"]),
                    ):
                        if identity[identity_field] != tuple_value:
                            reasons.append("full_gpu_resource_binding_mismatch:" + identity_field)
            else:
                expected = {
                    "discovered_extended_resource_name",
                    "mig_profile",
                    "donor_mig_device_uuids",
                    "target_mig_device_uuids",
                    "donor_gpu_instance_ids",
                    "target_gpu_instance_ids",
                    "donor_compute_instance_ids",
                    "target_compute_instance_ids",
                    "donor_node_identity_digest",
                    "target_node_identity_digest",
                    "donor_pvc_identity_digest",
                    "target_pvc_identity_digest",
                }
                if set(identity) != expected:
                    reasons.append("mig_resource_identity_shape_invalid")
                else:
                    if identity["mig_profile"] != donor["mig_profile"]:
                        reasons.append("mig_profile_identity_mismatch")
                    resource_name = identity["discovered_extended_resource_name"]
                    if (
                        not isinstance(resource_name, str)
                        or not resource_name.startswith("nvidia.com/mig-")
                    ):
                        reasons.append("mig_resource_name_invalid")
                    for name in (
                        "donor_mig_device_uuids",
                        "target_mig_device_uuids",
                        "donor_gpu_instance_ids",
                        "target_gpu_instance_ids",
                        "donor_compute_instance_ids",
                        "target_compute_instance_ids",
                    ):
                        values = identity[name]
                        if (
                            not isinstance(values, list)
                            or len(values) != donor["workload_gpu_count"]
                            or len(values) != len(set(values))
                        ):
                            reasons.append("mig_resource_identity_invalid:" + name)
                    for identity_field, tuple_value in (
                        ("donor_node_identity_digest", donor["node_identity_digest"]),
                        ("target_node_identity_digest", target["node_identity_digest"]),
                        ("donor_pvc_identity_digest", donor["pvc_identity_digest"]),
                        ("target_pvc_identity_digest", target["pvc_identity_digest"]),
                    ):
                        if identity[identity_field] != tuple_value:
                            reasons.append("mig_resource_binding_mismatch:" + identity_field)

    return {
        "schema": "fs2-serve.nebius.ai/snapshot-experiment-eligibility/v1",
        "model_id": model_id,
        "mechanism": mechanism,
        "partition": partition,
        "eligible_for_isolated_experiment": not reasons,
        "production_promotion": "denied",
        "conventional_fallback_required": True,
        "reason_codes": sorted(set(reasons)),
        "donor_tuple_digest": canonical_digest(donor),
        "target_tuple_digest": canonical_digest(target),
    }


def _condition_timestamp(value: Mapping[str, Any], kind: str) -> str | None:
    for condition in value.get("status", {}).get("conditions", []):
        if condition.get("type") == kind and condition.get("status") == "True":
            timestamp = condition.get("lastTransitionTime")
            return timestamp if isinstance(timestamp, str) else None
    return None


def _container_times(
    pod: Mapping[str, Any], names: set[str], *, init: bool
) -> tuple[list[str], list[str]]:
    key = "initContainerStatuses" if init else "containerStatuses"
    starts: list[str] = []
    finishes: list[str] = []
    for status in pod.get("status", {}).get(key, []):
        if status.get("name") not in names:
            continue
        state = status.get("state", {})
        running = state.get("running") or {}
        terminated = state.get("terminated") or {}
        started = running.get("startedAt") or terminated.get("startedAt")
        finished = terminated.get("finishedAt")
        if isinstance(started, str):
            starts.append(started)
        if isinstance(finished, str):
            finishes.append(finished)
    return starts, finishes


def _event_timestamp(event: Mapping[str, Any]) -> str | None:
    for field in ("eventTime", "lastTimestamp", "firstTimestamp"):
        value = event.get(field)
        if isinstance(value, str):
            return value
    metadata = event.get("metadata", {})
    value = metadata.get("creationTimestamp")
    return value if isinstance(value, str) else None


def build_phase_observation(
    matrix: Mapping[str, Any],
    *,
    model_id: str,
    mechanism: str,
    pod: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    node: Mapping[str, Any],
    external_events: Mapping[str, str | None],
    runtime_markers: Mapping[str, str],
) -> dict[str, Any]:
    """Build the canonical phase set and retain every missing measurement."""

    validate_matrix(matrix)
    model = matrix_model(matrix, model_id)
    observed: dict[str, str] = {
        name: value
        for name, value in external_events.items()
        if name in CANONICAL_EVENTS and isinstance(value, str)
    }
    scheduled = _condition_timestamp(pod, "PodScheduled")
    ready = _condition_timestamp(pod, "Ready")
    node_ready = _condition_timestamp(node, "Ready")
    if scheduled:
        observed["pod-scheduled"] = scheduled
    if ready:
        existing_ready = observed.get("readiness-accepted")
        observed["readiness-accepted"] = (
            max((ready, existing_ready), key=_parse_utc)
            if existing_ready is not None
            else ready
        )
    if node_ready:
        observed["node-ready"] = node_ready

    pulling = [
        timestamp
        for item in events
        if item.get("reason") == "Pulling"
        if (timestamp := _event_timestamp(item)) is not None
    ]
    pulled = [
        timestamp
        for item in events
        if item.get("reason") == "Pulled"
        if (timestamp := _event_timestamp(item)) is not None
    ]
    if pulling:
        observed["image-or-image-volume-pull-start"] = min(pulling)
    if pulled:
        observed["image-or-image-volume-pull-end"] = max(pulled)
        if not pulling:
            observed["image-or-image-volume-pull-start"] = min(pulled)

    init_names = set(model["artifact_init_containers"])
    init_starts, init_finishes = _container_times(pod, init_names, init=True)
    if init_starts:
        observed["artifact-localization-start"] = min(init_starts)
    if init_finishes:
        observed["artifact-localization-verified"] = max(init_finishes)
    primary_starts, _ = _container_times(
        pod, set(model["primary_containers"]), init=False
    )
    if primary_starts:
        observed["runtime-process-start"] = min(primary_starts)
    marker_contract = matrix["runtime_marker_contract"]
    if runtime_markers and model_id not in marker_contract["source_instrumented_models"]:
        raise ColdStartContractError("runtime_markers_unqualified:" + model_id)
    unknown_markers = set(runtime_markers) - set(marker_contract["accepted_events"])
    if unknown_markers:
        raise ColdStartContractError("runtime_marker_name_unreviewed")
    if any(not isinstance(timestamp, str) for timestamp in runtime_markers.values()):
        raise ColdStartContractError("runtime_marker_timestamp_invalid")
    for name, timestamp in runtime_markers.items():
        observed[name] = timestamp

    for earlier, later in ORDERED_EVENT_PAIRS:
        if (
            earlier in observed
            and later in observed
            and _parse_utc(observed[earlier]) > _parse_utc(observed[later])
        ):
            raise ColdStartContractError(
                "phase_observation_event_order_invalid:" + earlier + ":" + later
            )

    not_applicable: set[str] = set()
    if mechanism not in {"cuda-criu-snapshot", "dynamo-snapshot"}:
        not_applicable.update({"checkpoint-restore-start", "checkpoint-restore-end"})
    phases: list[dict[str, Any]] = []
    for name in CANONICAL_EVENTS:
        if name in observed:
            phases.append({"name": name, "state": "observed", "timestamp": observed[name]})
        elif name in not_applicable:
            phases.append({"name": name, "state": "not-applicable", "timestamp": None})
        else:
            phases.append({"name": name, "state": "missing", "timestamp": None})

    required_names = {
        event_name
        for phase_name, source in model["phase_sources"].items()
        if source["promotion_required"]
        for event_name in PHASE_GROUP_EVENTS.get(phase_name, ())
    }
    required_names.update(
        {
            "activation-accepted",
            "pod-scheduled",
            "runtime-process-start",
            "readiness-accepted",
            "semantic-call1-accepted",
            "semantic-call2-accepted",
            "return-to-zero-accepted",
        }
    )
    missing_required = sorted(
        name for name in required_names if name not in observed and name not in not_applicable
    )
    result = {
        "schema": "fs2-serve.nebius.ai/startup-phase-observation/v1",
        "model_id": model_id,
        "mechanism": mechanism,
        "pod_uid": pod.get("metadata", {}).get("uid"),
        "node_uid": node.get("metadata", {}).get("uid"),
        "complete_for_promotion": not missing_required,
        "missing_required_events": missing_required,
        "events": phases,
    }
    errors = _schema_errors(result, load_json(PHASE_OBSERVATION_SCHEMA_PATH))
    if errors:
        raise ColdStartContractError(
            "phase_observation_schema_invalid:" + ",".join(errors[:8])
        )
    return result


def write_mode_0600(path: Path, value: Mapping[str, Any]) -> None:
    if not path.is_absolute():
        raise ColdStartContractError("output_path_not_absolute")
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-matrix")
    validate.add_argument("--matrix", type=Path, default=MATRIX_PATH)

    eligibility = subparsers.add_parser("snapshot-eligibility")
    eligibility.add_argument("--matrix", type=Path, default=MATRIX_PATH)
    eligibility.add_argument("--model-id", required=True)
    eligibility.add_argument(
        "--mechanism", choices=("cuda-criu-snapshot", "dynamo-snapshot"), required=True
    )
    eligibility.add_argument("--donor", type=Path, required=True)
    eligibility.add_argument("--target", type=Path, required=True)
    eligibility.add_argument("--qualification", type=Path)
    eligibility.add_argument("--output", type=Path, required=True)

    promotion = subparsers.add_parser("validate-promotion")
    promotion.add_argument("--receipt", type=Path, required=True)

    collect = subparsers.add_parser("collect-phases")
    collect.add_argument("--matrix", type=Path, default=MATRIX_PATH)
    collect.add_argument("--model-id", required=True)
    collect.add_argument("--mechanism", required=True)
    collect.add_argument("--pod", type=Path, required=True)
    collect.add_argument("--events", type=Path, required=True)
    collect.add_argument("--node", type=Path, required=True)
    collect.add_argument("--external-events", type=Path, required=True)
    collect.add_argument("--runtime-markers", type=Path, required=True)
    collect.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "validate-promotion":
        validate_closed_promotion_receipt(load_json(args.receipt))
        return 0

    matrix = load_json(args.matrix)
    validate_matrix(matrix)
    if args.command == "validate-matrix":
        return 0
    if args.command == "snapshot-eligibility":
        result = evaluate_snapshot_eligibility(
            matrix,
            model_id=args.model_id,
            mechanism=args.mechanism,
            donor=load_json(args.donor),
            target=load_json(args.target),
            qualification=load_json(args.qualification) if args.qualification else None,
        )
        write_mode_0600(args.output, result)
        return 0 if result["eligible_for_isolated_experiment"] else 2
    events_value = load_json(args.events)
    external_value = load_json(args.external_events)
    markers_value = load_json(args.runtime_markers)
    event_items = events_value.get("items")
    if not isinstance(event_items, list):
        raise ColdStartContractError("events_shape_invalid")
    result = build_phase_observation(
        matrix,
        model_id=args.model_id,
        mechanism=args.mechanism,
        pod=load_json(args.pod),
        events=event_items,
        node=load_json(args.node),
        external_events=external_value,
        runtime_markers=markers_value,
    )
    write_mode_0600(args.output, result)
    return 0 if result["complete_for_promotion"] else 3


if __name__ == "__main__":
    raise SystemExit(_main())

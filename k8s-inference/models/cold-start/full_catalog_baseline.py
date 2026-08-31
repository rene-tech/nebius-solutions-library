#!/usr/bin/env python3
"""Plan, seal, and aggregate the FS2 conventional full-catalog baseline.

This tool has no live actuator.  It only consumes already captured, private
receipts.  The separate model-autoscaling acceptance harness owns requests and
Kubernetes observation; provider preemption remains manager-operated.

The receipt primitives are deliberately small adaptations of exact reviewed
Task Deck sources listed in ``full-catalog-conventional-baseline-contract.json``:
canonical JSON plus exclusive mode-0600 writes (snapshot pipeline), Linux boot
clock binding (node-local bench), byte-accounted phases (storage tiers), and an
external monotonic T0/T1 boundary (request-SLO harness).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import stat
import statistics
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parent
FS2_ROOT = ROOT.parent.parent
CONTRACT_PATH = ROOT / "full-catalog-conventional-baseline-contract.json"
MATRIX_PATH = ROOT / "cold-start-optimization-matrix.json"
PROFILE_PATH = FS2_ROOT / "catalog/profiles/model-profiles.json"
ROUTE_PATH = FS2_ROOT / "components/control-plane/contracts/all-models-live-services.json"
SEMANTIC_PATH = FS2_ROOT / "catalog/runtime/contracts/semantic-requests.json"
FRAMEWORK_PATH = ROOT / "cold_start_framework.py"
LOG_MARKER_PATH = ROOT / "capture_runtime_log_markers.py"
DCGM_CAPTURE_PATH = ROOT / "capture_dcgm_attribution.py"
MAX_JSON_BYTES = 64 * 1024 * 1024
CAPACITY_STATES = (
    "ready-pod-warm",
    "prepared-node-zero-pod",
    "fresh-node-zero-pod",
    "preemption-replacement",
)
PHASE_NAMES = (
    "provider-capacity",
    "node-ready",
    "queue-admission",
    "pod-scheduling",
    "storage-attach",
    "image-pull",
    "artifact-localization",
    "runtime-start",
    "weight-load",
    "engine-or-ptx-compile",
    "readiness",
    "semantic-call-1",
    "semantic-call-2",
    "cleanup",
    "return-to-zero",
)
EVENT_NAMES = (
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
    "readiness-accepted",
    "semantic-call1-accepted",
    "semantic-call2-accepted",
    "cleanup-start",
    "cleanup-end",
    "return-to-zero-accepted",
)
PHASE_SUPPORT_SOURCE_KINDS = (
    "provider-capacity-events",
    "kubernetes-events",
    "kueue-admission-events",
    "storage-observation",
    "runtime-markers",
    "cleanup-observation",
)
PHASE_EVENT_PAIRS: Mapping[str, tuple[str, str]] = {
    "provider-capacity": ("capacity-requested", "provider-instance-created"),
    "node-ready": ("provider-instance-created", "node-ready"),
    "queue-admission": ("activation-accepted", "workload-admitted"),
    "pod-scheduling": ("workload-admitted", "pod-scheduled"),
    "storage-attach": ("pod-scheduled", "storage-attached"),
    "image-pull": (
        "image-or-image-volume-pull-start",
        "image-or-image-volume-pull-end",
    ),
    "artifact-localization": (
        "artifact-localization-start",
        "artifact-localization-verified",
    ),
    "runtime-start": ("pod-scheduled", "runtime-process-start"),
    "weight-load": ("weight-load-start", "weight-load-end"),
    "engine-or-ptx-compile": (
        "engine-build-or-compile-start",
        "engine-build-or-compile-end",
    ),
    "readiness": ("runtime-process-start", "readiness-accepted"),
    "semantic-call-1": ("activation-accepted", "semantic-call1-accepted"),
    "semantic-call-2": (
        "semantic-call1-accepted",
        "semantic-call2-accepted",
    ),
    "cleanup": ("cleanup-start", "cleanup-end"),
    "return-to-zero": (
        "semantic-call2-accepted",
        "return-to-zero-accepted",
    ),
}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
DNS = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
BOOT_ID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


class BaselineError(ValueError):
    """A caller-controlled contract or receipt failed closed."""


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise BaselineError("json_duplicate_key")
        value[key] = item
    return value


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        + b"\n"
    )


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path, *, private: bool = False) -> Any:
    if not path.is_absolute():
        path = path.resolve()
    try:
        metadata = path.lstat()
    except OSError:
        raise BaselineError("json_unavailable") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not 1 <= metadata.st_size <= MAX_JSON_BYTES
    ):
        raise BaselineError("json_file_invalid")
    if private and (
        stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.geteuid()
    ):
        raise BaselineError("private_json_mode_or_owner_invalid")
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise BaselineError("json_parse_failed") from None


def write_json_new(path: Path, value: Any) -> None:
    if not path.is_absolute():
        raise BaselineError("output_path_not_absolute")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise BaselineError("output_create_failed") from None
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(canonical_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    parent = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _object(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BaselineError(code)
    return value


def _exact(value: Any, keys: Iterable[str], code: str) -> dict[str, Any]:
    result = _object(value, code)
    if set(result) != set(keys):
        raise BaselineError(code)
    return result


def _digest(value: Any, code: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise BaselineError(code)
    return value


def _timestamp(value: Any, code: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise BaselineError(code)
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(UTC)
    except ValueError:
        raise BaselineError(code) from None


def _seconds(start: str, end: str) -> float:
    duration = (
        _timestamp(end, "timestamp_invalid") - _timestamp(start, "timestamp_invalid")
    ).total_seconds()
    if duration < 0:
        raise BaselineError("phase_timestamp_order_invalid")
    return round(duration, 6)


def _framework() -> Any:
    spec = importlib.util.spec_from_file_location(
        "fs2_cold_start_framework", FRAMEWORK_PATH
    )
    if spec is None or spec.loader is None:
        raise BaselineError("cold_start_framework_unavailable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except BaseException:
        raise BaselineError("cold_start_framework_load_failed") from None
    return module


def _log_marker_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "fs2_runtime_log_markers", LOG_MARKER_PATH
    )
    if spec is None or spec.loader is None:
        raise BaselineError("runtime_marker_validator_unavailable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except BaseException:
        raise BaselineError("runtime_marker_validator_unavailable") from None
    return module


def _dcgm_capture_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "fs2_dcgm_attribution", DCGM_CAPTURE_PATH
    )
    if spec is None or spec.loader is None:
        raise BaselineError("dcgm_validator_unavailable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except BaseException:
        raise BaselineError("dcgm_validator_unavailable") from None
    return module


def _validate_dcgm_receipt(value: Any, attempt_id: str) -> dict[str, Any]:
    receipt = _object(value, "dcgm_receipt_invalid")
    if receipt.get("schema") != "fs2-serve.nebius.ai/dcgm-attribution/v2":
        raise BaselineError("dcgm_receipt_schema_invalid")
    if receipt.get("attempt_id") != attempt_id:
        raise BaselineError("dcgm_receipt_attempt_mismatch")
    digest = _digest(receipt.get("receipt_digest"), "dcgm_receipt_digest_invalid")
    unsigned = dict(receipt)
    del unsigned["receipt_digest"]
    if canonical_digest(unsigned) != digest:
        raise BaselineError("dcgm_receipt_digest_mismatch")
    try:
        _dcgm_capture_module().validate_receipt(receipt)
    except BaseException:
        raise BaselineError("dcgm_receipt_contract_invalid") from None
    return receipt


def _dcgm_projection(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve the proxy limitation when reducing a private DCGM receipt."""

    sampling = _object(
        receipt.get("sampling_feasibility"), "dcgm_sampling_contract_invalid"
    )
    cadence = _object(receipt.get("cadence_binding"), "dcgm_cadence_binding_invalid")
    cadence_terraform = _object(
        cadence.get("terraform"), "dcgm_cadence_binding_invalid"
    )
    cadence_source = _exact(
        cadence.get("source"), {"commit", "tree"}, "dcgm_cadence_binding_invalid"
    )
    projection = {
        "proxy_classification": sampling.get("proxy_classification"),
        "hardware_source_timestamp_state": sampling.get(
            "hardware_source_timestamp_state"
        ),
        "instrumentation_gap": sampling.get("instrumentation_gap"),
        "summary": receipt.get("summary"),
        "cadence_provenance": {
            "source": cadence_source,
            "saved_plan_sha256": cadence_terraform.get("saved_plan_sha256"),
            "terraform_output_sha256": cadence_terraform.get(
                "dcgm_attribution_contract_sha256"
            ),
            "cadence_profile_sha256": cadence_terraform.get("cadence_profile_sha256"),
            "binding_receipt_digest": cadence.get("receipt_digest"),
        },
    }
    if (
        projection["proxy_classification"] != "NOMINAL_SCRAPE_PROXY"
        or projection["hardware_source_timestamp_state"]
        != "UNOBSERVED_HARDWARE_SOURCE_TIMESTAMP"
        or projection["instrumentation_gap"] != "DCGM_SOURCE_TIMESTAMP_UNOBSERVED"
    ):
        raise BaselineError("dcgm_proxy_classification_invalid")
    return projection


def _validate_dcgm_projection(value: Any, plan: Mapping[str, Any]) -> dict[str, Any]:
    projection = _exact(
        value,
        {
            "proxy_classification",
            "hardware_source_timestamp_state",
            "instrumentation_gap",
            "summary",
            "cadence_provenance",
        },
        "passing_attempt_dcgm_invalid",
    )
    if (
        projection.get("proxy_classification") != "NOMINAL_SCRAPE_PROXY"
        or projection.get("hardware_source_timestamp_state")
        != "UNOBSERVED_HARDWARE_SOURCE_TIMESTAMP"
        or projection.get("instrumentation_gap") != "DCGM_SOURCE_TIMESTAMP_UNOBSERVED"
    ):
        raise BaselineError("passing_attempt_dcgm_classification_invalid")
    summary = _exact(
        projection.get("summary"),
        {
            "attributed_device_count",
            "gpu_utilization_sample_count",
            "mean_gpu_utilization_percent",
            "peak_gpu_utilization_percent",
            "framebuffer_sample_count",
            "peak_framebuffer_bytes",
        },
        "passing_attempt_dcgm_summary_invalid",
    )
    if any(
        isinstance(summary.get(name), bool)
        or not isinstance(summary.get(name), int)
        or summary[name] < 1
        for name in (
            "attributed_device_count",
            "gpu_utilization_sample_count",
            "framebuffer_sample_count",
        )
    ) or (
        isinstance(summary.get("peak_framebuffer_bytes"), bool)
        or not isinstance(summary.get("peak_framebuffer_bytes"), int)
        or summary["peak_framebuffer_bytes"] < 0
    ):
        raise BaselineError("passing_attempt_dcgm_summary_invalid")
    for name in ("mean_gpu_utilization_percent", "peak_gpu_utilization_percent"):
        metric = summary.get(name)
        if (
            isinstance(metric, bool)
            or not isinstance(metric, (int, float))
            or not math.isfinite(metric)
            or metric < 0
        ):
            raise BaselineError("passing_attempt_dcgm_summary_invalid")
    provenance = _exact(
        projection.get("cadence_provenance"),
        {
            "source",
            "saved_plan_sha256",
            "terraform_output_sha256",
            "cadence_profile_sha256",
            "binding_receipt_digest",
        },
        "passing_attempt_dcgm_provenance_invalid",
    )
    if provenance.get("source") != plan.get("source"):
        raise BaselineError("passing_attempt_dcgm_source_mismatch")
    for name in (
        "saved_plan_sha256",
        "terraform_output_sha256",
        "cadence_profile_sha256",
        "binding_receipt_digest",
    ):
        _digest(provenance.get(name), "passing_attempt_dcgm_provenance_invalid")
    plan_hashes = plan.get("terraform_provenance", {}).get("plan_hashes", {})
    if provenance["saved_plan_sha256"] not in plan_hashes.values():
        raise BaselineError("passing_attempt_dcgm_plan_mismatch")
    return projection


def _manifest_deployments(profile: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    deployments: dict[str, dict[str, Any]] = {}
    services: list[tuple[str, dict[str, str]]] = []
    for relative in profile["manifest_paths"]:
        path = (FS2_ROOT / relative).resolve()
        if FS2_ROOT.resolve() not in path.parents or not path.is_file():
            raise BaselineError("profile_manifest_path_invalid")
        try:
            documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, yaml.YAMLError):
            raise BaselineError("profile_manifest_invalid") from None
        for document in documents:
            if not isinstance(document, dict):
                continue
            name = document.get("metadata", {}).get("name")
            if document.get("kind") == "Service" and isinstance(name, str):
                selector = document.get("spec", {}).get("selector", {})
                if isinstance(selector, dict) and selector:
                    services.append((name, selector))
            if document.get("kind") != "Deployment" or not isinstance(name, str):
                continue
            if name in deployments:
                raise BaselineError("profile_deployment_not_unique")
            gpu_count = 0
            containers = (
                document.get("spec", {})
                .get("template", {})
                .get("spec", {})
                .get("containers", [])
            )
            for container in containers:
                resources = container.get("resources", {})
                raw = resources.get("limits", {}).get(
                    "nvidia.com/gpu",
                    resources.get("requests", {}).get("nvidia.com/gpu", 0),
                )
                try:
                    gpu_count += int(raw)
                except (TypeError, ValueError):
                    raise BaselineError("profile_gpu_count_invalid") from None
            deployments[name] = {
                "deployment": name,
                "gpu_count": gpu_count,
                "manifest_path": relative,
                "container_images": {
                    container.get("name"): container.get("image")
                    for container in containers
                    if isinstance(container, dict)
                    and isinstance(container.get("name"), str)
                    and isinstance(container.get("image"), str)
                },
                "deployment_annotations": document.get("metadata", {}).get(
                    "annotations", {}
                ),
                "pod_labels": document.get("spec", {})
                .get("template", {})
                .get("metadata", {})
                .get("labels", {}),
            }
    expected_backend_count = len(profile["canonical_routes"]) + 1
    if (
        len(deployments) != expected_backend_count
        or len(services) < expected_backend_count
    ):
        raise BaselineError("full_catalog_backend_partition_invalid")
    for deployment, value in deployments.items():
        labels = value.pop("pod_labels")
        matches = sorted(
            name
            for name, selector in services
            if all(labels.get(key) == item for key, item in selector.items())
        )
        if len(matches) != 1:
            raise BaselineError("backend_service_missing")
        deployments[deployment]["service"] = matches[0]
    return deployments


def discover_backends() -> list[dict[str, Any]]:
    contract = _object(load_json(CONTRACT_PATH), "contract_invalid")
    matrix = _object(load_json(MATRIX_PATH), "matrix_invalid")
    profiles = _object(load_json(PROFILE_PATH), "profile_invalid")
    routes = _object(load_json(ROUTE_PATH), "routes_invalid")
    semantics = _object(load_json(SEMANTIC_PATH), "semantics_invalid")
    framework = _framework()
    framework.validate_matrix(matrix)
    profile = profiles["profiles"][contract["spec"]["scope"]["profile"]]
    deployments = _manifest_deployments(profile)
    canonical = profile["canonical_routes"]
    scope = contract["spec"]["scope"]
    if (
        len(canonical) != scope["canonical_route_count"]
        or set(canonical) != set(routes["routes"])
    ):
        raise BaselineError("full_catalog_route_partition_invalid")
    if set(canonical) != set(semantics["contracts"]):
        raise BaselineError("full_catalog_semantic_partition_invalid")

    result: list[dict[str, Any]] = []
    selected_deployments: set[str] = set()
    for model in matrix["models"]:
        model_id = model["model_id"]
        deployment = model["deployment"]
        selected_deployments.add(deployment)
        rendered = deployments.get(deployment)
        if rendered is None or rendered["gpu_count"] != model["gpu_count"]:
            raise BaselineError("matrix_backend_binding_invalid")
        route = routes["routes"][model_id]
        if route["service"]["name"] != model["service"]:
            raise BaselineError("matrix_route_service_binding_invalid")
        identity = matrix["deployment_identity_contract"]["models"][model_id]
        result.append(
            {
                "backend_id": deployment,
                "model_id": model_id,
                "route_id": model_id,
                "deployment": deployment,
                "service": model["service"],
                "resource_class": "gpu",
                "gpu_count": model["gpu_count"],
                "runtime_image_digest": route["runtime_image_digest"],
                "model_revision": route["model_revision"],
                "storage_mode": route["storage_mode"],
                "semantic_contract_digest": canonical_digest(
                    semantics["contracts"][model_id]
                ),
                "identity_state": identity["state"],
                "identity_blocker": identity["blocker"],
                "manifest_path": rendered["manifest_path"],
            }
        )

    remainder = sorted(set(deployments) - selected_deployments)
    if remainder != ["msa-search-pdb70"]:
        raise BaselineError("unrouted_backend_partition_invalid")
    fallback = deployments[remainder[0]]
    fallback_images = fallback["container_images"]
    fallback_annotations = fallback["deployment_annotations"]
    if set(fallback_images) != {"msa-search-pdb70"}:
        raise BaselineError("fallback_runtime_container_invalid")
    fallback_image = fallback_images["msa-search-pdb70"]
    _, separator, fallback_image_digest = fallback_image.rpartition("@")
    if (
        not separator
        or re.fullmatch(r"sha256:[0-9a-f]{64}", fallback_image_digest) is None
    ):
        raise BaselineError("fallback_runtime_image_invalid")
    if (
        not isinstance(fallback_annotations, dict)
        or fallback_annotations.get("fs2-serve.nebius.ai/identity-relationship")
        != "capability-equivalent-non-alias"
        or fallback_annotations.get("fs2-serve.nebius.ai/exact-pdb70-parity") != "false"
        or GIT_SHA.fullmatch(
            fallback_annotations.get("fs2-serve.nebius.ai/source-revision", "")
        )
        is None
    ):
        raise BaselineError("fallback_identity_annotations_invalid")
    fallback_evidence = (
        FS2_ROOT / "catalog/runtime/contracts/model-variants.json"
    )
    fallback_candidates = _object(
        load_json(fallback_evidence), "fallback_identity_contract_invalid"
    ).get("fallback_candidates")
    if not isinstance(fallback_candidates, dict):
        raise BaselineError("fallback_identity_contract_invalid")
    fallback_candidate = fallback_candidates.get("msa-search-pdb70-colabfold")
    if (
        not isinstance(fallback_candidate, dict)
        or fallback_candidate.get("lane_id") != "msa-search-pdb70"
        or fallback_candidate.get("relationship")
        != "capability-equivalent-non-alias"
    ):
        raise BaselineError("fallback_identity_contract_invalid")
    result.append(
        {
            "backend_id": "msa-search-pdb70-fallback",
            "model_id": "msa-search-pdb70",
            "route_id": None,
            "deployment": fallback["deployment"],
            "service": fallback["service"],
            "resource_class": "cpu",
            "gpu_count": 0,
            "runtime_image_digest": fallback_image_digest,
            "model_revision": fallback_annotations[
                "fs2-serve.nebius.ai/source-revision"
            ],
            "identity_relationship": "capability-equivalent-non-alias",
            "exact_pdb70_parity": False,
            "storage_mode": "provider-block-pvc",
            "semantic_contract_digest": canonical_digest(
                semantics["contracts"]["msa-search-pdb70"]
            ),
            "identity_state": "complete",
            "identity_blocker": None,
            "manifest_path": fallback["manifest_path"],
            "fallback_evidence_sha256": file_digest(fallback_evidence),
        }
    )
    if len(result) != scope["rendered_backend_count"]:
        raise BaselineError("backend_count_invalid")
    return result


def _validate_terraform_provenance(
    value: Any, source_commit: str, source_tree: str
) -> dict[str, Any]:
    provenance = _object(value, "terraform_provenance_invalid")
    if (
        provenance.get("schema")
        != "fs2-serve.nebius.ai/full-catalog-terraform-provenance/v1"
    ):
        raise BaselineError("terraform_provenance_schema_invalid")
    if (
        provenance.get("source_commit") != source_commit
        or provenance.get("source_tree") != source_tree
    ):
        raise BaselineError("terraform_source_provenance_mismatch")
    if (
        provenance.get("profile") != "full_catalog"
        or provenance.get("replica_owner") != "keda"
    ):
        raise BaselineError("terraform_workload_contract_invalid")
    flags = _object(provenance.get("feature_flags"), "terraform_feature_flags_invalid")
    if flags.get("challengers_enabled") is not False:
        raise BaselineError("terraform_challenger_flag_not_disabled")
    if flags.get("cold_start_mechanism") != "conventional":
        raise BaselineError("terraform_mechanism_not_conventional")
    for name, digest in _object(
        provenance.get("plan_hashes"), "terraform_plan_hashes_invalid"
    ).items():
        if not isinstance(name, str):
            raise BaselineError("terraform_plan_hashes_invalid")
        _digest(digest, "terraform_plan_hash_invalid")
    return provenance


def _validate_nim_cache_overlay(
    value: Any,
    *,
    source_commit: str,
    source_tree: str,
    routes: Mapping[str, Any],
) -> dict[str, Any]:
    receipt = _exact(
        value,
        {
            "schema",
            "source",
            "subject",
            "stability",
            "artifact_manifest",
            "artifact_manifest_digest",
            "captured_at",
            "receipt_digest",
        },
        "nim_cache_identity_invalid",
    )
    if receipt.get("schema") != "fs2-serve.nebius.ai/nim-cache-manifest-capture/v1":
        raise BaselineError("nim_cache_identity_schema_invalid")
    digest = _digest(receipt.get("receipt_digest"), "nim_cache_identity_digest_invalid")
    unsigned = dict(receipt)
    del unsigned["receipt_digest"]
    if canonical_digest(unsigned) != digest:
        raise BaselineError("nim_cache_identity_digest_mismatch")
    if receipt.get("source") != {"commit": source_commit, "tree": source_tree}:
        raise BaselineError("nim_cache_identity_source_mismatch")
    _timestamp(receipt["captured_at"], "nim_cache_identity_captured_at_invalid")
    subject = _exact(
        receipt["subject"],
        {
            "model_id",
            "namespace",
            "namespace_uid",
            "pod_uid",
            "container",
            "pvc_name",
            "pvc_uid",
            "cache_root",
            "runtime_image_digest",
            "capacity_bound_bytes",
        },
        "nim_cache_identity_subject_invalid",
    )
    model_id = subject.get("model_id")
    if model_id not in {"msa-search-pdb70", "openfold2", "openfold3"}:
        raise BaselineError("nim_cache_identity_model_invalid")
    route = routes.get(model_id)
    if not isinstance(route, dict):
        raise BaselineError("nim_cache_identity_route_missing")
    if (
        subject.get("namespace") != "fs2-models"
        or subject.get("pvc_name") != f"{model_id}-nim-cache"
        or subject.get("runtime_image_digest") != route.get("runtime_image_digest")
    ):
        raise BaselineError("nim_cache_identity_runtime_binding_mismatch")
    for field in ("namespace_uid", "pod_uid", "pvc_uid", "container"):
        if not isinstance(subject.get(field), str) or not subject[field]:
            raise BaselineError("nim_cache_identity_runtime_binding_incomplete")
    catalog_model = _object(
        load_json(FS2_ROOT / f"catalog/runtime/models/{model_id}.json"),
        "nim_cache_catalog_model_invalid",
    )
    catalog_source = catalog_model["model"]["source"]
    catalog_cache = catalog_model["cache"]
    capacity_bound = catalog_cache["artifact"]["capacity_bound_bytes"]
    if subject.get("capacity_bound_bytes") != capacity_bound:
        raise BaselineError("nim_cache_identity_capacity_bound_mismatch")

    stability = _exact(
        receipt["stability"],
        {"method", "delay_seconds", "first", "second", "stable"},
        "nim_cache_identity_stability_invalid",
    )
    if (
        stability.get("stable") is not True
        or stability.get("method")
        != "two-complete-sha256-passes-with-descriptor-stat-guards"
    ):
        raise BaselineError("nim_cache_identity_not_stable")
    first = _object(stability.get("first"), "nim_cache_identity_pass_invalid")
    second = _object(stability.get("second"), "nim_cache_identity_pass_invalid")
    if set(first) != set(second) or set(first) != {
        "started_at",
        "completed_at",
        "file_count",
        "expanded_bytes",
        "content_digest",
        "metadata_digest",
    }:
        raise BaselineError("nim_cache_identity_pass_invalid")
    for field in ("file_count", "expanded_bytes", "content_digest", "metadata_digest"):
        if first.get(field) != second.get(field):
            raise BaselineError("nim_cache_identity_pass_mismatch")
    for capture in (first, second):
        started = _timestamp(capture["started_at"], "nim_cache_pass_clock_invalid")
        completed = _timestamp(capture["completed_at"], "nim_cache_pass_clock_invalid")
        if completed < started:
            raise BaselineError("nim_cache_pass_clock_invalid")
        _digest(capture["content_digest"], "nim_cache_pass_digest_invalid")
        _digest(capture["metadata_digest"], "nim_cache_pass_digest_invalid")
    if _timestamp(second["started_at"], "nim_cache_pass_clock_invalid") < _timestamp(
        first["completed_at"], "nim_cache_pass_clock_invalid"
    ):
        raise BaselineError("nim_cache_pass_clock_invalid")

    manifest = _exact(
        receipt.get("artifact_manifest"),
        {
            "schema",
            "model_id",
            "kind",
            "source",
            "content",
            "license",
            "entitlement_state",
            "owner",
            "retention",
        },
        "nim_cache_artifact_manifest_invalid",
    )
    manifest_source = _exact(
        manifest["source"], {"uri", "revision"}, "nim_cache_manifest_source_invalid"
    )
    license_value = _exact(
        manifest["license"], {"id", "state"}, "nim_cache_manifest_license_invalid"
    )
    expected_entitlement = catalog_source["entitlement"]["state"]
    if (
        manifest["schema"] != "fs2-serve.nebius.ai/artifact-manifest/v1"
        or manifest["model_id"] != model_id
        or manifest["kind"] != "nim-cache"
        or manifest_source
        != {
            "uri": f"ngc://{catalog_source['repository']}",
            "revision": route.get("model_revision"),
        }
        or license_value
        != {
            "id": catalog_source["license"]["id"],
            "state": catalog_source["license"]["state"],
        }
        or manifest["entitlement_state"] != expected_entitlement
        or manifest["owner"] != catalog_cache["owner"]
        or manifest["retention"] not in {"retained-platform", "ephemeral-test"}
    ):
        raise BaselineError("nim_cache_artifact_manifest_subject_mismatch")
    content = _exact(
        manifest["content"],
        {"digest", "expanded_bytes", "files"},
        "nim_cache_content_invalid",
    )
    content_digest = _digest(content["digest"], "nim_cache_content_digest_invalid")
    files = content["files"]
    if not isinstance(files, list) or not files:
        raise BaselineError("nim_cache_files_missing")
    normalized: list[dict[str, Any]] = []
    paths: list[str] = []
    for file in files:
        item = _exact(file, {"path", "bytes", "sha256"}, "nim_cache_file_invalid")
        if (
            not isinstance(item["path"], str)
            or not item["path"]
            or isinstance(item["bytes"], bool)
            or not isinstance(item["bytes"], int)
            or item["bytes"] < 0
        ):
            raise BaselineError("nim_cache_file_invalid")
        safe_path = PurePosixPath(item["path"])
        if safe_path.is_absolute() or any(
            part in {"", ".", ".."} for part in safe_path.parts
        ):
            raise BaselineError("nim_cache_file_path_unsafe")
        _digest(item["sha256"], "nim_cache_file_digest_invalid")
        paths.append(item["path"])
        normalized.append(item)
    if paths != sorted(set(paths)):
        raise BaselineError("nim_cache_file_order_invalid")
    if (
        sum(item["bytes"] for item in normalized) != content["expanded_bytes"]
        or content["expanded_bytes"] <= 0
        or content["expanded_bytes"] > capacity_bound
        or canonical_digest(normalized) != content_digest
        or content_digest != second.get("content_digest")
        or content["expanded_bytes"] != second.get("expanded_bytes")
        or len(normalized) != second.get("file_count")
    ):
        raise BaselineError("nim_cache_content_reconciliation_failed")
    manifest_digest = _digest(
        receipt.get("artifact_manifest_digest"),
        "nim_cache_artifact_manifest_digest_invalid",
    )
    if canonical_digest(manifest) != manifest_digest:
        raise BaselineError("nim_cache_artifact_manifest_digest_mismatch")
    return {
        "kind": "live-immutable-nim-cache-manifest",
        "receipt_digest": digest,
        "artifact_manifest_digest": manifest_digest,
        "content_digest": content_digest,
        "expanded_bytes": content["expanded_bytes"],
        "file_count": len(normalized),
        "pod_uid": subject["pod_uid"],
        "pvc_uid": subject["pvc_uid"],
        "namespace_uid": subject["namespace_uid"],
        "runtime_image_digest": subject["runtime_image_digest"],
    }


def _nim_cache_overlays(
    paths: Sequence[Path], source_commit: str, source_tree: str
) -> dict[str, dict[str, Any]]:
    routes = _object(load_json(ROUTE_PATH), "routes_invalid")["routes"]
    overlays: dict[str, dict[str, Any]] = {}
    for path in paths:
        resolved = path.resolve()
        raw = load_json(resolved, private=True)
        value = _validate_nim_cache_overlay(
            raw,
            source_commit=source_commit,
            source_tree=source_tree,
            routes=routes,
        )
        model_id = raw["subject"]["model_id"]
        if model_id in overlays:
            raise BaselineError("nim_cache_identity_duplicate")
        overlays[model_id] = {
            **value,
            "raw_receipt_sha256": file_digest(resolved),
            "raw_receipt_bytes": resolved.stat().st_size,
        }
    return overlays


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    if (
        GIT_SHA.fullmatch(args.source_commit) is None
        or GIT_SHA.fullmatch(args.source_tree) is None
    ):
        raise BaselineError("source_identity_invalid")
    if not DNS.fullmatch(args.campaign_id):
        raise BaselineError("campaign_id_invalid")
    acceptance = _object(
        load_json(args.acceptance_receipt, private=True), "acceptance_receipt_invalid"
    )
    if acceptance.get("result") != "PASS":
        raise BaselineError("current_all_model_acceptance_not_pass")
    accepted_source = acceptance.get("source_commit") or acceptance.get(
        "release_commit"
    )
    if accepted_source is not None and accepted_source != args.source_commit:
        raise BaselineError("all_model_acceptance_source_mismatch")
    provenance_path = args.terraform_provenance.resolve()
    provenance = _validate_terraform_provenance(
        load_json(provenance_path, private=True), args.source_commit, args.source_tree
    )
    overlays = _nim_cache_overlays(
        getattr(args, "nim_cache_identity", []), args.source_commit, args.source_tree
    )
    backends = discover_backends()
    for backend in backends:
        overlay = overlays.get(backend["model_id"])
        if overlay is not None:
            if backend["identity_state"] == "complete":
                raise BaselineError("nim_cache_identity_overlay_unexpected")
            backend["identity_state"] = "complete-live-overlay"
            backend["identity_blocker"] = None
            backend["identity_admission"] = overlay
        elif backend["identity_state"] == "complete":
            backend["identity_admission"] = {"kind": "source-matrix"}
        else:
            backend["identity_admission"] = {"kind": "blocked"}
    minimum = load_json(CONTRACT_PATH)["spec"]["statistics"][
        "minimum_attempts_per_admitted_cell"
    ]
    cells: list[dict[str, Any]] = []
    for backend in backends:
        for capacity_state in CAPACITY_STATES:
            if (
                backend["resource_class"] == "cpu"
                and capacity_state != "ready-pod-warm"
            ):
                admission = "not-applicable"
                reason = "no-durable-route-activation-boundary"
            elif backend["identity_state"] not in {
                "complete",
                "complete-live-overlay",
            }:
                admission = "blocked"
                reason = "exact-model-content-identity-incomplete"
            else:
                admission = "admitted"
                reason = None
            expected = (
                [
                    f"{backend['backend_id']}--{capacity_state}--r{ordinal:02d}"
                    for ordinal in range(1, minimum + 1)
                ]
                if admission == "admitted"
                else []
            )
            blocked_slots = (
                [
                    f"{backend['backend_id']}--{capacity_state}--slot{ordinal:02d}"
                    for ordinal in range(1, minimum + 1)
                ]
                if admission == "blocked"
                else []
            )
            cells.append(
                {
                    "cell_id": f"{backend['backend_id']}--{capacity_state}",
                    "backend_id": backend["backend_id"],
                    "model_id": backend["model_id"],
                    "capacity_state": capacity_state,
                    "admission": admission,
                    "reason": reason,
                    "expected_attempt_ids": expected,
                    "blocked_planned_slots": blocked_slots,
                }
            )
    plan: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/full-catalog-conventional-plan/v1",
        "campaign_id": args.campaign_id,
        "source": {"commit": args.source_commit, "tree": args.source_tree},
        "contract_digest": canonical_digest(load_json(CONTRACT_PATH)),
        "matrix_digest": canonical_digest(load_json(MATRIX_PATH)),
        "profile_digest": canonical_digest(load_json(PROFILE_PATH)),
        "route_digest": canonical_digest(load_json(ROUTE_PATH)),
        "semantic_contract_digest": canonical_digest(load_json(SEMANTIC_PATH)),
        "acceptance_receipt": {
            "sha256": file_digest(args.acceptance_receipt.resolve()),
            "result": "PASS",
        },
        "terraform_provenance": {
            "sha256": file_digest(provenance_path),
            "contract_digest": canonical_digest(provenance),
            "run_id": provenance.get("run_id"),
            "region": provenance.get("region"),
            "plan_hashes": provenance["plan_hashes"],
            "feature_flags": provenance["feature_flags"],
        },
        "minimum_attempts_per_admitted_cell": minimum,
        "backends": backends,
        "cells": cells,
        "created_at": datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    }
    plan["plan_digest"] = canonical_digest(plan)
    validate_plan(plan)
    return plan


def validate_plan(plan: Any) -> dict[str, Any]:
    value = _object(plan, "plan_invalid")
    if set(value) != {
        "schema",
        "campaign_id",
        "source",
        "contract_digest",
        "matrix_digest",
        "profile_digest",
        "route_digest",
        "semantic_contract_digest",
        "acceptance_receipt",
        "terraform_provenance",
        "minimum_attempts_per_admitted_cell",
        "backends",
        "cells",
        "created_at",
        "plan_digest",
    }:
        raise BaselineError("plan_fields_invalid")
    if value.get("schema") != "fs2-serve.nebius.ai/full-catalog-conventional-plan/v1":
        raise BaselineError("plan_schema_invalid")
    digest = value.get("plan_digest")
    _digest(digest, "plan_digest_invalid")
    unsigned = dict(value)
    del unsigned["plan_digest"]
    if canonical_digest(unsigned) != digest:
        raise BaselineError("plan_digest_mismatch")
    if value.get("minimum_attempts_per_admitted_cell") != 3:
        raise BaselineError("plan_minimum_attempts_invalid")
    if (
        not isinstance(value.get("campaign_id"), str)
        or DNS.fullmatch(value["campaign_id"]) is None
    ):
        raise BaselineError("plan_campaign_id_invalid")
    source = _exact(value.get("source"), {"commit", "tree"}, "plan_source_invalid")
    if (
        GIT_SHA.fullmatch(source["commit"]) is None
        or GIT_SHA.fullmatch(source["tree"]) is None
    ):
        raise BaselineError("plan_source_invalid")
    for field, path in (
        ("contract_digest", CONTRACT_PATH),
        ("matrix_digest", MATRIX_PATH),
        ("profile_digest", PROFILE_PATH),
        ("route_digest", ROUTE_PATH),
        ("semantic_contract_digest", SEMANTIC_PATH),
    ):
        if value.get(field) != canonical_digest(load_json(path)):
            raise BaselineError("plan_source_contract_digest_mismatch")
    _timestamp(value.get("created_at"), "plan_created_at_invalid")
    acceptance = _exact(
        value.get("acceptance_receipt"),
        {"sha256", "result"},
        "plan_acceptance_receipt_invalid",
    )
    _digest(acceptance["sha256"], "plan_acceptance_receipt_invalid")
    if acceptance["result"] != "PASS":
        raise BaselineError("plan_acceptance_result_invalid")
    terraform = _exact(
        value.get("terraform_provenance"),
        {
            "sha256",
            "contract_digest",
            "run_id",
            "region",
            "plan_hashes",
            "feature_flags",
        },
        "plan_terraform_provenance_invalid",
    )
    _digest(terraform["sha256"], "plan_terraform_provenance_invalid")
    _digest(terraform["contract_digest"], "plan_terraform_provenance_invalid")
    if not isinstance(terraform["run_id"], str) or not terraform["run_id"]:
        raise BaselineError("plan_terraform_run_invalid")
    if not isinstance(terraform["region"], str) or not terraform["region"]:
        raise BaselineError("plan_terraform_region_invalid")
    plan_hashes = _object(terraform["plan_hashes"], "plan_terraform_hashes_invalid")
    if not plan_hashes:
        raise BaselineError("plan_terraform_hashes_invalid")
    for name, plan_hash in plan_hashes.items():
        if not isinstance(name, str) or not name:
            raise BaselineError("plan_terraform_hashes_invalid")
        _digest(plan_hash, "plan_terraform_hashes_invalid")
    flags = _object(terraform["feature_flags"], "plan_feature_flags_invalid")
    if (
        flags.get("challengers_enabled") is not False
        or flags.get("cold_start_mechanism") != "conventional"
    ):
        raise BaselineError("plan_feature_flags_invalid")
    backends = value.get("backends")
    cells = value.get("cells")
    expected_backends = {
        backend["backend_id"]: backend for backend in discover_backends()
    }
    if not isinstance(backends, list) or len(backends) != len(expected_backends):
        raise BaselineError("plan_backend_count_invalid")
    validated_backends: dict[str, dict[str, Any]] = {}
    for raw_backend in backends:
        backend = _object(raw_backend, "plan_backend_invalid")
        backend_id = backend.get("backend_id")
        expected = expected_backends.get(backend_id)
        if expected is None or backend_id in validated_backends:
            raise BaselineError("plan_backend_ids_invalid")
        if set(backend) != set(expected) | {"identity_admission"}:
            raise BaselineError("plan_backend_fields_invalid")
        for field, expected_value in expected.items():
            if field in {"identity_state", "identity_blocker"}:
                continue
            if backend.get(field) != expected_value:
                raise BaselineError("plan_backend_source_binding_mismatch")
        admission = _object(
            backend.get("identity_admission"), "plan_identity_admission_invalid"
        )
        if expected["identity_state"] == "complete":
            if (
                backend.get("identity_state") != "complete"
                or backend.get("identity_blocker") is not None
                or admission != {"kind": "source-matrix"}
            ):
                raise BaselineError("plan_source_identity_admission_invalid")
        elif admission.get("kind") == "blocked":
            if (
                admission != {"kind": "blocked"}
                or backend.get("identity_state") != expected["identity_state"]
                or backend.get("identity_blocker") != expected["identity_blocker"]
            ):
                raise BaselineError("plan_blocked_identity_admission_invalid")
        else:
            if set(admission) != {
                "kind",
                "receipt_digest",
                "artifact_manifest_digest",
                "content_digest",
                "expanded_bytes",
                "file_count",
                "pod_uid",
                "pvc_uid",
                "namespace_uid",
                "runtime_image_digest",
                "raw_receipt_sha256",
                "raw_receipt_bytes",
            }:
                raise BaselineError("plan_live_identity_admission_invalid")
            if (
                admission.get("kind") != "live-immutable-nim-cache-manifest"
                or backend.get("identity_state") != "complete-live-overlay"
                or backend.get("identity_blocker") is not None
                or admission.get("runtime_image_digest")
                != expected["runtime_image_digest"]
            ):
                raise BaselineError("plan_live_identity_admission_invalid")
            for field in (
                "receipt_digest",
                "artifact_manifest_digest",
                "content_digest",
                "raw_receipt_sha256",
            ):
                _digest(admission.get(field), "plan_live_identity_digest_invalid")
            for field in ("pod_uid", "pvc_uid", "namespace_uid"):
                if not isinstance(admission.get(field), str) or not admission[field]:
                    raise BaselineError("plan_live_identity_subject_invalid")
            for field in ("expanded_bytes", "file_count", "raw_receipt_bytes"):
                if (
                    isinstance(admission.get(field), bool)
                    or not isinstance(admission.get(field), int)
                    or admission[field] < 1
                ):
                    raise BaselineError("plan_live_identity_size_invalid")
        validated_backends[backend_id] = backend
    backend_ids = list(validated_backends)
    if set(backend_ids) != set(expected_backends):
        raise BaselineError("plan_backend_ids_invalid")
    if (
        not isinstance(cells, list)
        or len(cells) != len(expected_backends) * len(CAPACITY_STATES)
    ):
        raise BaselineError("plan_cell_count_invalid")
    observed_cells: set[tuple[str, str]] = set()
    attempt_ids: list[str] = []
    blocked_slots: list[str] = []
    for cell in cells:
        item = _exact(
            cell,
            {
                "cell_id",
                "backend_id",
                "model_id",
                "capacity_state",
                "admission",
                "reason",
                "expected_attempt_ids",
                "blocked_planned_slots",
            },
            "plan_cell_invalid",
        )
        key = (item.get("backend_id"), item.get("capacity_state"))
        if (
            key[0] not in backend_ids
            or key[1] not in CAPACITY_STATES
            or key in observed_cells
        ):
            raise BaselineError("plan_cell_partition_invalid")
        observed_cells.add(key)  # type: ignore[arg-type]
        backend = validated_backends[item["backend_id"]]
        if (
            item["cell_id"] != f"{item['backend_id']}--{item['capacity_state']}"
            or item["model_id"] != backend["model_id"]
        ):
            raise BaselineError("plan_cell_identity_invalid")
        if (
            backend["resource_class"] == "cpu"
            and item["capacity_state"] != "ready-pod-warm"
        ):
            expected_admission = "not-applicable"
            expected_reason = "no-durable-route-activation-boundary"
        elif backend["identity_state"] in {"complete", "complete-live-overlay"}:
            expected_admission = "admitted"
            expected_reason = None
        else:
            expected_admission = "blocked"
            expected_reason = "exact-model-content-identity-incomplete"
        if item["admission"] != expected_admission or item["reason"] != expected_reason:
            raise BaselineError("plan_cell_admission_invalid")
        expected = item.get("expected_attempt_ids")
        reserved = item.get("blocked_planned_slots")
        if not isinstance(expected, list) or not isinstance(reserved, list):
            raise BaselineError("plan_expected_attempts_invalid")
        required_count = 3 if item.get("admission") == "admitted" else 0
        reserved_count = 3 if item.get("admission") == "blocked" else 0
        exact_attempts = (
            [
                f"{item['backend_id']}--{item['capacity_state']}--r{ordinal:02d}"
                for ordinal in range(1, 4)
            ]
            if required_count
            else []
        )
        exact_slots = (
            [
                f"{item['backend_id']}--{item['capacity_state']}--slot{ordinal:02d}"
                for ordinal in range(1, 4)
            ]
            if reserved_count
            else []
        )
        if expected != exact_attempts:
            raise BaselineError("plan_expected_attempt_count_invalid")
        if reserved != exact_slots:
            raise BaselineError("plan_blocked_slot_count_invalid")
        attempt_ids.extend(expected)
        blocked_slots.extend(reserved)
    if len(attempt_ids) != len(set(attempt_ids)):
        raise BaselineError("plan_attempt_ids_not_unique")
    if len(blocked_slots) != len(set(blocked_slots)) or set(attempt_ids) & set(
        blocked_slots
    ):
        raise BaselineError("plan_blocked_slots_not_unique")
    expected_cells = {
        (backend_id, capacity_state)
        for backend_id in expected_backends
        for capacity_state in CAPACITY_STATES
    }
    if observed_cells != expected_cells:
        raise BaselineError("plan_cell_partition_invalid")
    return value


def _cell(
    plan: Mapping[str, Any], backend_id: str, capacity_state: str
) -> dict[str, Any]:
    matches = [
        cell
        for cell in plan["cells"]
        if cell["backend_id"] == backend_id and cell["capacity_state"] == capacity_state
    ]
    if len(matches) != 1:
        raise BaselineError("attempt_cell_not_unique")
    return matches[0]


def _backend(plan: Mapping[str, Any], backend_id: str) -> dict[str, Any]:
    matches = [item for item in plan["backends"] if item["backend_id"] == backend_id]
    if len(matches) != 1:
        raise BaselineError("attempt_backend_not_unique")
    return matches[0]


def build_phase_support(args: argparse.Namespace) -> dict[str, Any]:
    """Seal allowlisted timestamps against one private raw support receipt."""

    plan = validate_plan(load_json(args.plan, private=True))
    admitted_attempts = {
        attempt_id
        for cell in plan["cells"]
        if cell["admission"] == "admitted"
        for attempt_id in cell["expected_attempt_ids"]
    }
    if args.attempt_id not in admitted_attempts:
        raise BaselineError("phase_support_attempt_not_admitted")
    raw_path = args.raw_receipt.resolve()
    raw_source = _object(
        load_json(raw_path, private=True), "phase_support_raw_receipt_invalid"
    )
    events: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in args.event:
        name, separator, timestamp = value.partition("=")
        if not separator or name not in EVENT_NAMES or name in seen:
            raise BaselineError("phase_support_event_argument_invalid")
        _timestamp(timestamp, "phase_support_timestamp_invalid")
        seen.add(name)
        events.append({"name": name, "timestamp": timestamp})
    if not events:
        raise BaselineError("phase_support_events_missing")
    if args.source_kind == "runtime-markers":
        try:
            _log_marker_module().validate_receipt(raw_source)
        except BaseException:
            raise BaselineError("runtime_marker_source_invalid")
        if raw_source.get("attempt_id") != args.attempt_id:
            raise BaselineError("runtime_marker_source_invalid")
        if events != raw_source.get("admitted_events"):
            raise BaselineError("runtime_marker_event_admission_mismatch")
    receipt: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/full-catalog-phase-support/v1",
        "attempt_id": args.attempt_id,
        "source": {
            "kind": args.source_kind,
            "raw_receipt_sha256": file_digest(raw_path),
            "raw_receipt_bytes": raw_path.stat().st_size,
        },
        "events": sorted(events, key=lambda item: EVENT_NAMES.index(item["name"])),
        "captured_at": datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    }
    receipt["receipt_digest"] = canonical_digest(receipt)
    return receipt


def _validate_phase_support(value: Any, attempt_id: str) -> dict[str, Any]:
    receipt = _exact(
        value,
        {
            "schema",
            "attempt_id",
            "source",
            "events",
            "captured_at",
            "receipt_digest",
        },
        "phase_support_invalid",
    )
    if receipt["schema"] != "fs2-serve.nebius.ai/full-catalog-phase-support/v1":
        raise BaselineError("phase_support_schema_invalid")
    if receipt["attempt_id"] != attempt_id:
        raise BaselineError("phase_support_attempt_mismatch")
    digest = _digest(receipt["receipt_digest"], "phase_support_digest_invalid")
    unsigned = dict(receipt)
    del unsigned["receipt_digest"]
    if canonical_digest(unsigned) != digest:
        raise BaselineError("phase_support_digest_mismatch")
    _timestamp(receipt["captured_at"], "phase_support_captured_at_invalid")
    source = _exact(
        receipt["source"],
        {"kind", "raw_receipt_sha256", "raw_receipt_bytes"},
        "phase_support_source_invalid",
    )
    if source["kind"] not in PHASE_SUPPORT_SOURCE_KINDS:
        raise BaselineError("phase_support_source_kind_invalid")
    _digest(source["raw_receipt_sha256"], "phase_support_raw_digest_invalid")
    if (
        isinstance(source["raw_receipt_bytes"], bool)
        or not isinstance(source["raw_receipt_bytes"], int)
        or source["raw_receipt_bytes"] < 1
    ):
        raise BaselineError("phase_support_raw_bytes_invalid")
    events = receipt["events"]
    if not isinstance(events, list) or not events:
        raise BaselineError("phase_support_events_missing")
    observed: set[str] = set()
    order: list[int] = []
    for event in events:
        item = _exact(event, {"name", "timestamp"}, "phase_support_event_invalid")
        name = item["name"]
        if name not in EVENT_NAMES or name in observed:
            raise BaselineError("phase_support_event_name_invalid")
        _timestamp(item["timestamp"], "phase_support_timestamp_invalid")
        observed.add(name)
        order.append(EVENT_NAMES.index(name))
    if order != sorted(order):
        raise BaselineError("phase_support_event_order_invalid")
    return receipt


def _support_events(
    paths: Sequence[Path], attempt_id: str
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    events: dict[str, str] = {}
    receipts: list[dict[str, Any]] = []
    for path in paths:
        resolved = path.resolve()
        value = _validate_phase_support(load_json(resolved, private=True), attempt_id)
        for item in value["events"]:
            name = item["name"]
            timestamp = item["timestamp"]
            if name in events and events[name] != timestamp:
                raise BaselineError("phase_support_event_conflict")
            events[name] = timestamp
        receipts.append(
            {
                "kind": "phase-support",
                "sha256": file_digest(resolved),
                "bytes": resolved.stat().st_size,
                "source_kind": value["source"]["kind"],
                "raw_receipt_sha256": value["source"]["raw_receipt_sha256"],
                "raw_receipt_bytes": value["source"]["raw_receipt_bytes"],
            }
        )
    return events, receipts


def _raw_events(raw: Mapping[str, Any]) -> dict[str, str]:
    events: dict[str, str] = {}
    startup = raw.get("startup_observation")
    if isinstance(startup, dict):
        observation = startup.get("phase_observation")
        if isinstance(observation, dict):
            for event in observation.get("events", []):
                if (
                    isinstance(event, dict)
                    and event.get("state") == "observed"
                    and event.get("name") in EVENT_NAMES
                    and isinstance(event.get("timestamp"), str)
                ):
                    events[event["name"]] = event["timestamp"]
    timestamps = raw.get("phase_timestamps", {})
    aliases = {
        "activation_accepted_at": "activation-accepted",
        "readiness_observed_at": "readiness-accepted",
        "semantic_call1_accepted_at": "semantic-call1-accepted",
        "semantic_call2_accepted_at": "semantic-call2-accepted",
        "return_to_floor_accepted_at": "return-to-zero-accepted",
    }
    for source, target in aliases.items():
        value = timestamps.get(source) if isinstance(timestamps, dict) else None
        if isinstance(value, str):
            events[target] = value
    operation = raw.get("operation", {})
    if isinstance(operation, dict):
        for field, name in (
            ("accepted_at", "workload-admitted"),
            ("activation_started_at", "capacity-requested"),
            ("started_at", "runtime-process-start"),
            ("ready_at", "readiness-accepted"),
        ):
            value = operation.get(field)
            if isinstance(value, str):
                events.setdefault(name, value)
    return events


def _phase_rows(
    capacity_state: str,
    events: Mapping[str, str],
    resource_class: str = "gpu",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for phase in PHASE_NAMES:
        start_name, end_name = PHASE_EVENT_PAIRS[phase]
        start = events.get(start_name)
        end = events.get(end_name)
        not_applicable = False
        reason: str | None = None
        if phase == "provider-capacity" and capacity_state in {
            "ready-pod-warm",
            "prepared-node-zero-pod",
        }:
            not_applicable = True
            reason = "node-present-at-t0"
        elif phase == "node-ready" and capacity_state == "ready-pod-warm":
            not_applicable = True
            reason = "node-ready-before-t0"
        elif (
            phase
            in {
                "pod-scheduling",
                "storage-attach",
                "image-pull",
                "artifact-localization",
                "runtime-start",
                "weight-load",
                "engine-or-ptx-compile",
                "readiness",
            }
            and capacity_state == "ready-pod-warm"
        ):
            not_applicable = True
            reason = "ready-pod-before-t0"
        elif phase == "return-to-zero" and capacity_state == "ready-pod-warm":
            not_applicable = True
            reason = "warm-floor-retained"
        elif phase == "queue-admission" and resource_class == "cpu":
            not_applicable = True
            reason = "direct-static-service-no-queue"
        elif phase == "cleanup" and resource_class == "cpu":
            not_applicable = True
            reason = "no-per-request-workload-cleanup"
        if not_applicable:
            rows.append(
                {
                    "name": phase,
                    "state": "not-applicable",
                    "start_event": start_name,
                    "end_event": end_name,
                    "started_at": None,
                    "completed_at": None,
                    "duration_seconds": None,
                    "reason": reason,
                }
            )
        elif start is not None and end is not None:
            rows.append(
                {
                    "name": phase,
                    "state": "observed",
                    "start_event": start_name,
                    "end_event": end_name,
                    "started_at": start,
                    "completed_at": end,
                    "duration_seconds": _seconds(start, end),
                    "reason": None,
                }
            )
        else:
            rows.append(
                {
                    "name": phase,
                    "state": "missing",
                    "start_event": start_name,
                    "end_event": end_name,
                    "started_at": start,
                    "completed_at": end,
                    "duration_seconds": None,
                    "reason": "UNOBSERVED_INSTRUMENTATION_GAP",
                }
            )
    return rows


def _validate_baseline_tuple(
    value: Mapping[str, Any],
    matrix: Mapping[str, Any],
    capacity_state: str,
    backend: Mapping[str, Any],
) -> None:
    if value.get("capacity_state") != capacity_state:
        raise BaselineError("compatibility_capacity_state_mismatch")
    framework = _framework()
    validation = dict(value)
    if capacity_state == "ready-pod-warm":
        validation["capacity_state"] = "prepared-node-zero-pod"
    try:
        framework.validate_compatibility_tuple(validation)
    except BaseException:
        raise BaselineError("compatibility_tuple_invalid") from None
    if validation["workload_gpu_count"] != backend["gpu_count"]:
        raise BaselineError("compatibility_tuple_gpu_count_mismatch")
    if (
        validation["semantic_request_contract_digest"]
        != backend["semantic_contract_digest"]
    ):
        raise BaselineError("compatibility_semantic_contract_mismatch")
    admission = _object(
        backend.get("identity_admission"), "compatibility_identity_admission_invalid"
    )
    if admission.get("kind") == "source-matrix":
        try:
            framework.validate_deployment_identity_binding(
                matrix,
                model_id=value["model_id"],
                compatibility_tuple=validation,
            )
        except BaseException:
            raise BaselineError("compatibility_tuple_invalid") from None
        return
    if admission.get("kind") != "live-immutable-nim-cache-manifest":
        raise BaselineError("compatibility_identity_admission_invalid")
    matrix_identity = matrix["deployment_identity_contract"]["models"].get(
        value["model_id"]
    )
    if (
        not isinstance(matrix_identity, dict)
        or matrix_identity.get("state") != "blocked"
        or matrix_identity.get("blocker") != "nim-cache-content-unresolved"
        or matrix_identity.get("missing_annotations")
        != ["fs2.nebius/model-content-digest"]
    ):
        raise BaselineError("compatibility_overlay_source_state_invalid")
    annotations = _object(
        matrix_identity.get("annotations"), "compatibility_overlay_annotations_invalid"
    )
    expected_content = admission["content_digest"]
    if (
        value["model_content_digest"] != expected_content
        or value["artifact_content_digest"] != expected_content
        or value["artifact_manifest_digest"] != admission["artifact_manifest_digest"]
        or value["artifact_bytes"] != admission["expanded_bytes"]
        or value["runtime_image_digest"] != admission["runtime_image_digest"]
        or value["runtime_image_digest"]
        != annotations.get("fs2.nebius/runtime-image-digest")
        or value["compile_cache_abi"] != annotations.get("fs2.nebius/compile-cache-abi")
    ):
        raise BaselineError("compatibility_overlay_binding_mismatch")


def _runtime_identity(raw: Mapping[str, Any]) -> dict[str, Any]:
    value = raw.get("runtime_identity_observation")
    if not isinstance(value, dict):
        startup = raw.get("startup_observation")
        value = (
            startup.get("identity_observation") if isinstance(startup, dict) else None
        )
    return _object(value, "raw_runtime_identity_observation_missing")


def _validate_observed_runtime_identity(
    raw: Mapping[str, Any],
    compatibility: Mapping[str, Any],
    backend: Mapping[str, Any],
) -> str:
    identity = _runtime_identity(raw)
    if set(identity) != {
        "pod",
        "deployment_annotations",
        "container_image_ids",
        "pod_image_ids",
        "runtime_argv_digest",
        "runtime_environment_digest",
        "node",
    }:
        raise BaselineError("raw_runtime_identity_shape_invalid")
    pod = _exact(
        identity["pod"], {"name", "uid", "node_name"}, "raw_pod_identity_invalid"
    )
    node = _object(identity["node"], "raw_node_identity_invalid")
    node_metadata = _object(node.get("metadata"), "raw_node_identity_invalid")
    node_status = _object(node.get("status"), "raw_node_identity_invalid")
    node_info = _object(node_status.get("nodeInfo"), "raw_node_info_invalid")
    operation = _object(raw.get("operation"), "raw_operation_invalid")
    runtime = _object(operation.get("runtime"), "raw_operation_runtime_invalid")
    if (
        not isinstance(pod["uid"], str)
        or not pod["uid"]
        or runtime.get("pod_uid") != pod["uid"]
        or not isinstance(node_metadata.get("uid"), str)
        or runtime.get("node_uid") != node_metadata["uid"]
        or pod["node_name"] != node_metadata.get("name")
    ):
        raise BaselineError("raw_runtime_subject_identity_mismatch")
    gpu_uuids = runtime.get("gpu_uuids")
    if (
        not isinstance(gpu_uuids, list)
        or sorted(gpu_uuids) != sorted(compatibility["allocated_gpu_uuids"])
        or runtime.get("gpu_count") != backend["gpu_count"]
    ):
        raise BaselineError("raw_runtime_gpu_identity_mismatch")
    annotations = _object(
        identity["deployment_annotations"], "raw_runtime_annotations_invalid"
    )
    if (
        annotations.get("fs2.nebius/runtime-image-digest")
        != compatibility["runtime_image_digest"]
        or annotations.get("fs2.nebius/compile-cache-abi")
        != compatibility["compile_cache_abi"]
    ):
        raise BaselineError("raw_runtime_annotation_mismatch")
    expected_content_annotation = "sha256:" + compatibility["model_content_digest"]
    content_annotation = annotations.get("fs2.nebius/model-content-digest")
    if backend["identity_admission"]["kind"] == "source-matrix":
        if content_annotation != expected_content_annotation:
            raise BaselineError("raw_runtime_content_annotation_mismatch")
    elif (
        content_annotation is not None
        and content_annotation != expected_content_annotation
    ):
        raise BaselineError("raw_runtime_content_annotation_mismatch")
    image_ids = identity["pod_image_ids"]
    if not isinstance(image_ids, list) or not any(
        isinstance(image_id, str)
        and image_id.endswith(compatibility["runtime_image_digest"])
        for image_id in image_ids
    ):
        raise BaselineError("raw_runtime_image_id_mismatch")
    if (
        identity["runtime_argv_digest"] != compatibility["runtime_argv_digest"]
        or identity["runtime_environment_digest"]
        != compatibility["runtime_environment_digest"]
    ):
        raise BaselineError("raw_runtime_process_identity_mismatch")
    if (
        node_info.get("architecture") != compatibility["host_cpu_architecture"]
        or node_info.get("kernelVersion") != compatibility["kernel_release"]
        or node_info.get("containerRuntimeVersion")
        != compatibility["container_runtime_name"]
        + "://"
        + compatibility["container_runtime_version"]
        or canonical_digest(node) != compatibility["node_identity_digest"]
    ):
        raise BaselineError("raw_runtime_node_identity_mismatch")
    return canonical_digest(identity)


def _validate_cpu_fallback_runtime_identity(
    raw: Mapping[str, Any], backend: Mapping[str, Any]
) -> str:
    if (
        raw.get("identity_relationship") != backend["identity_relationship"]
        or raw.get("exact_pdb70_parity") is not False
    ):
        raise BaselineError("cpu_fallback_relationship_invalid")
    identity = _runtime_identity(raw)
    if set(identity) != {
        "pod",
        "deployment_annotations",
        "container_image_ids",
        "pod_image_ids",
        "runtime_argv_digest",
        "runtime_environment_digest",
        "node",
    }:
        raise BaselineError("cpu_fallback_runtime_identity_shape_invalid")
    pod = _exact(
        identity["pod"], {"name", "uid", "node_name"}, "cpu_fallback_pod_invalid"
    )
    node = _object(identity["node"], "cpu_fallback_node_invalid")
    node_metadata = _object(node.get("metadata"), "cpu_fallback_node_invalid")
    observed = _exact(
        raw.get("observed_runtime_identities"),
        {"pod_uids", "node_uids"},
        "cpu_fallback_observed_identities_invalid",
    )
    if (
        observed["pod_uids"] != [pod["uid"]]
        or observed["node_uids"] != [node_metadata.get("uid")]
        or pod["node_name"] != node_metadata.get("name")
    ):
        raise BaselineError("cpu_fallback_runtime_subject_mismatch")
    images = identity["container_image_ids"]
    if not isinstance(images, list) or len(images) != 1:
        raise BaselineError("cpu_fallback_runtime_image_invalid")
    image = _exact(
        images[0], {"name", "image_id"}, "cpu_fallback_runtime_image_invalid"
    )
    if (
        image["name"] != "msa-search-pdb70"
        or not isinstance(image["image_id"], str)
        or not image["image_id"].endswith(backend["runtime_image_digest"])
        or identity["pod_image_ids"] != [image["image_id"]]
    ):
        raise BaselineError("cpu_fallback_runtime_image_invalid")
    _digest(identity["runtime_argv_digest"], "cpu_fallback_argv_digest_invalid")
    _digest(
        identity["runtime_environment_digest"],
        "cpu_fallback_environment_digest_invalid",
    )
    return canonical_digest(identity)


def record_attempt(args: argparse.Namespace) -> dict[str, Any]:
    plan = validate_plan(load_json(args.plan, private=True))
    cell = _cell(plan, args.backend_id, args.capacity_state)
    backend = _backend(plan, args.backend_id)
    if cell["admission"] != "admitted":
        raise BaselineError("attempt_cell_not_admitted")
    attempt_id = f"{args.backend_id}--{args.capacity_state}--r{args.ordinal:02d}"
    if attempt_id not in cell["expected_attempt_ids"]:
        raise BaselineError("attempt_id_not_planned")
    raw_path = args.raw_acceptance.resolve()
    raw = _object(load_json(raw_path, private=True), "raw_acceptance_invalid")
    result = raw.get("result")
    if result not in {"PASS", "FAIL"}:
        raise BaselineError("raw_acceptance_result_invalid")
    if raw.get("model_id") != backend["model_id"]:
        raise BaselineError("raw_acceptance_model_mismatch")
    target = _object(raw.get("target"), "raw_acceptance_target_invalid")
    if (
        target.get("deployment") != backend["deployment"]
        or target.get("service") != backend["service"]
    ):
        raise BaselineError("raw_acceptance_backend_mismatch")
    expected_raw_schema = (
        "fs2-serve.nebius.ai/cpu-fallback-warm-acceptance/v1"
        if backend["resource_class"] == "cpu"
        else "fs2-serve.nebius.ai/model-autoscaling-acceptance/v1"
    )
    if raw.get("schema") != expected_raw_schema:
        raise BaselineError("raw_acceptance_schema_invalid")
    if result == "PASS":
        calls = raw.get("semantic_calls")
        if not isinstance(calls, list) or [item.get("ordinal") for item in calls] != [
            1,
            2,
        ]:
            raise BaselineError("raw_acceptance_semantic_calls_invalid")
        operation_ids = [item.get("operation_id") for item in calls]
        if (
            any(not isinstance(value, str) for value in operation_ids)
            or len(set(operation_ids)) != 2
        ):
            raise BaselineError("semantic_call_operations_not_distinct")
        monotonic = _object(
            raw.get("phase_monotonic_ns"), "raw_monotonic_clock_missing"
        )
        ordered = [
            monotonic.get("activation_accepted"),
            monotonic.get("semantic_call1_accepted"),
            monotonic.get("semantic_call2_accepted"),
        ]
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in ordered
        ):
            raise BaselineError("raw_monotonic_clock_invalid")
        if ordered != sorted(ordered):
            raise BaselineError("raw_monotonic_clock_order_invalid")
        t0_to_call1 = round((ordered[1] - ordered[0]) / 1_000_000_000, 9)
        t0_to_call2 = round((ordered[2] - ordered[0]) / 1_000_000_000, 9)
    else:
        calls = []
        t0_to_call1 = None
        t0_to_call2 = None

    identity_digest: str | None = None
    runtime_identity_digest: str | None = None
    if backend["resource_class"] == "gpu" and result == "PASS":
        if args.compatibility_tuple is None:
            raise BaselineError("compatibility_tuple_required")
        tuple_path = args.compatibility_tuple.resolve()
        compatibility = _object(
            load_json(tuple_path, private=True), "compatibility_tuple_invalid"
        )
        if compatibility.get("model_id") != backend["model_id"]:
            raise BaselineError("compatibility_model_mismatch")
        _validate_baseline_tuple(
            compatibility,
            load_json(MATRIX_PATH),
            args.capacity_state,
            backend,
        )
        identity_digest = canonical_digest(compatibility)
        runtime_identity_digest = _validate_observed_runtime_identity(
            raw, compatibility, backend
        )
    elif args.compatibility_tuple is not None:
        raise BaselineError("compatibility_tuple_unexpected")
    if backend["resource_class"] == "cpu" and result == "PASS":
        runtime_identity_digest = _validate_cpu_fallback_runtime_identity(raw, backend)

    support_events, support_receipts = _support_events(args.phase_support, attempt_id)
    events = _raw_events(raw)
    for name, timestamp in support_events.items():
        if name in events and events[name] != timestamp:
            raise BaselineError("phase_support_conflicts_with_raw")
        events[name] = timestamp
    phases = (
        _phase_rows(args.capacity_state, events, backend["resource_class"])
        if result == "PASS"
        else []
    )

    raw_receipts = [
        {
            "kind": (
                "cpu-fallback-warm-acceptance"
                if backend["resource_class"] == "cpu"
                else "model-autoscaling-acceptance"
            ),
            "sha256": file_digest(raw_path),
            "bytes": raw_path.stat().st_size,
        },
        *support_receipts,
    ]
    dcgm: dict[str, Any] | None = None
    if args.dcgm_receipt is not None:
        dcgm_path = args.dcgm_receipt.resolve()
        dcgm_value = _validate_dcgm_receipt(
            load_json(dcgm_path, private=True), attempt_id
        )
        binding = _exact(
            dcgm_value.get("identity_binding"),
            {"pod_uids", "node_uids", "gpu_uuids", "attempt_t0", "attempt_t1"},
            "dcgm_identity_binding_invalid",
        )
        observed_runtime = _exact(
            raw.get("observed_runtime_identities"),
            {"pod_uids", "node_uids"},
            "raw_runtime_identity_set_invalid",
        )
        for name in ("pod_uids", "node_uids"):
            values = observed_runtime[name]
            if not isinstance(values, list) or sorted(values) != binding[name]:
                raise BaselineError("dcgm_runtime_identity_mismatch")
        timestamps = _object(
            raw.get("phase_timestamps"), "raw_phase_timestamps_invalid"
        )
        if binding["attempt_t0"] != timestamps.get("activation_accepted_at") or binding[
            "attempt_t1"
        ] != timestamps.get("semantic_call1_accepted_at"):
            raise BaselineError("dcgm_attempt_clock_mismatch")
        runtime = _object(
            _object(raw.get("operation"), "raw_operation_invalid").get("runtime"),
            "raw_operation_runtime_invalid",
        )
        if (
            runtime.get("pod_uid") not in binding["pod_uids"]
            or runtime.get("node_uid") not in binding["node_uids"]
        ):
            raise BaselineError("dcgm_final_runtime_identity_mismatch")
        runtime_gpu_uuids = runtime.get("gpu_uuids")
        if (
            not isinstance(runtime_gpu_uuids, list)
            or not set(runtime_gpu_uuids)
            or not set(runtime_gpu_uuids) <= set(binding["gpu_uuids"])
        ):
            raise BaselineError("dcgm_final_gpu_identity_mismatch")
        minimum_devices = backend["gpu_count"] * (
            2 if args.capacity_state == "preemption-replacement" else 1
        )
        if len(binding["gpu_uuids"]) < minimum_devices:
            raise BaselineError("dcgm_replacement_gpu_identity_incomplete")
        summary = _object(dcgm_value.get("summary"), "dcgm_summary_invalid")
        if summary.get("attributed_device_count") != len(binding["gpu_uuids"]):
            raise BaselineError("dcgm_attributed_device_count_mismatch")
        dcgm = _dcgm_projection(dcgm_value)
        _validate_dcgm_projection(dcgm, plan)
        raw_receipts.append(
            {
                "kind": "dcgm-attribution",
                "sha256": file_digest(dcgm_path),
                "bytes": dcgm_path.stat().st_size,
            }
        )
    if backend["resource_class"] == "gpu" and result == "PASS" and dcgm is None:
        raise BaselineError("dcgm_receipt_required")

    receipt: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/full-catalog-conventional-attempt/v1",
        "plan_digest": plan["plan_digest"],
        "attempt_id": attempt_id,
        "ordinal": args.ordinal,
        "backend_id": args.backend_id,
        "model_id": backend["model_id"],
        "capacity_state": args.capacity_state,
        "mechanism": "conventional",
        "result": result,
        "failure_code": raw.get("failure_code") if result == "FAIL" else None,
        "clock_domain": raw.get("clock", {}).get("domain")
        if result == "PASS"
        else None,
        "t0_to_call1_seconds": t0_to_call1,
        "t0_to_call2_seconds": t0_to_call2,
        "semantic_result_sha256": [item.get("result_sha256") for item in calls],
        "compatibility_tuple_digest": identity_digest,
        "runtime_identity_observation_digest": runtime_identity_digest,
        "phases": phases,
        "dcgm": dcgm,
        "raw_receipts": raw_receipts,
        "completed_at": raw.get("completed_at"),
    }
    receipt["receipt_digest"] = canonical_digest(receipt)
    validate_attempt(receipt, plan)
    return receipt


def validate_attempt(value: Any, plan: Mapping[str, Any]) -> dict[str, Any]:
    receipt = _object(value, "attempt_invalid")
    if set(receipt) != {
        "schema",
        "plan_digest",
        "attempt_id",
        "ordinal",
        "backend_id",
        "model_id",
        "capacity_state",
        "mechanism",
        "result",
        "failure_code",
        "clock_domain",
        "t0_to_call1_seconds",
        "t0_to_call2_seconds",
        "semantic_result_sha256",
        "compatibility_tuple_digest",
        "runtime_identity_observation_digest",
        "phases",
        "dcgm",
        "raw_receipts",
        "completed_at",
        "receipt_digest",
    }:
        raise BaselineError("attempt_fields_invalid")
    if (
        receipt.get("schema")
        != "fs2-serve.nebius.ai/full-catalog-conventional-attempt/v1"
    ):
        raise BaselineError("attempt_schema_invalid")
    if receipt.get("plan_digest") != plan["plan_digest"]:
        raise BaselineError("attempt_plan_mismatch")
    digest = receipt.get("receipt_digest")
    _digest(digest, "attempt_digest_invalid")
    unsigned = dict(receipt)
    del unsigned["receipt_digest"]
    if canonical_digest(unsigned) != digest:
        raise BaselineError("attempt_digest_mismatch")
    cell = _cell(plan, receipt.get("backend_id"), receipt.get("capacity_state"))
    backend = _backend(plan, receipt.get("backend_id"))
    if (
        cell["admission"] != "admitted"
        or receipt.get("attempt_id") not in cell["expected_attempt_ids"]
        or receipt.get("model_id") != backend["model_id"]
        or receipt.get("ordinal") not in {1, 2, 3}
        or receipt["attempt_id"]
        != f"{receipt['backend_id']}--{receipt['capacity_state']}--r{receipt['ordinal']:02d}"
    ):
        raise BaselineError("attempt_not_admitted")
    if receipt.get("mechanism") != "conventional" or receipt.get("result") not in {
        "PASS",
        "FAIL",
    }:
        raise BaselineError("attempt_result_or_mechanism_invalid")
    if receipt["result"] == "PASS":
        if receipt.get("failure_code") is not None:
            raise BaselineError("passing_attempt_has_failure")
        for field in ("t0_to_call1_seconds", "t0_to_call2_seconds"):
            value = receipt.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value < 0
            ):
                raise BaselineError("passing_attempt_duration_invalid")
        if receipt["t0_to_call2_seconds"] < receipt["t0_to_call1_seconds"]:
            raise BaselineError("passing_attempt_duration_order_invalid")
        if (
            not isinstance(receipt.get("clock_domain"), str)
            or BOOT_ID.fullmatch(receipt["clock_domain"]) is None
        ):
            raise BaselineError("passing_attempt_clock_invalid")
        semantic_hashes = receipt.get("semantic_result_sha256")
        if not isinstance(semantic_hashes, list) or len(semantic_hashes) != 2:
            raise BaselineError("passing_attempt_semantic_results_invalid")
        for semantic_hash in semantic_hashes:
            _digest(semantic_hash, "passing_attempt_semantic_results_invalid")
        if backend["resource_class"] == "gpu":
            _digest(
                receipt.get("compatibility_tuple_digest"),
                "passing_attempt_compatibility_digest_invalid",
            )
            _digest(
                receipt.get("runtime_identity_observation_digest"),
                "passing_attempt_runtime_identity_digest_invalid",
            )
            _validate_dcgm_projection(receipt.get("dcgm"), plan)
        elif (
            receipt.get("compatibility_tuple_digest") is not None
            or receipt.get("dcgm") is not None
        ):
            raise BaselineError("cpu_attempt_gpu_identity_unexpected")
        else:
            _digest(
                receipt.get("runtime_identity_observation_digest"),
                "cpu_attempt_runtime_identity_digest_invalid",
            )
        phases = receipt.get("phases")
        if not isinstance(phases, list) or [
            item.get("name") for item in phases
        ] != list(PHASE_NAMES):
            raise BaselineError("attempt_phase_partition_invalid")
        for phase in phases:
            item = _exact(
                phase,
                {
                    "name",
                    "state",
                    "start_event",
                    "end_event",
                    "started_at",
                    "completed_at",
                    "duration_seconds",
                    "reason",
                },
                "attempt_phase_invalid",
            )
            if (item["start_event"], item["end_event"]) != PHASE_EVENT_PAIRS[
                item["name"]
            ]:
                raise BaselineError("attempt_phase_event_binding_invalid")
            if item["state"] == "observed":
                if item["reason"] is not None or item["duration_seconds"] != _seconds(
                    item["started_at"], item["completed_at"]
                ):
                    raise BaselineError("attempt_observed_phase_invalid")
            elif item["state"] == "not-applicable":
                if (
                    item["started_at"] is not None
                    or item["completed_at"] is not None
                    or item["duration_seconds"] is not None
                    or not isinstance(item["reason"], str)
                    or not item["reason"]
                ):
                    raise BaselineError("attempt_not_applicable_phase_invalid")
            elif item["state"] == "missing":
                for timestamp in (item["started_at"], item["completed_at"]):
                    if timestamp is not None:
                        _timestamp(timestamp, "attempt_missing_phase_timestamp_invalid")
                if (
                    item["duration_seconds"] is not None
                    or item["reason"] != "UNOBSERVED_INSTRUMENTATION_GAP"
                ):
                    raise BaselineError("attempt_missing_phase_invalid")
            else:
                raise BaselineError("attempt_phase_state_invalid")
    else:
        if (
            not isinstance(receipt.get("failure_code"), str)
            or not receipt["failure_code"]
        ):
            raise BaselineError("failed_attempt_missing_failure")
        for field in (
            "clock_domain",
            "t0_to_call1_seconds",
            "t0_to_call2_seconds",
            "compatibility_tuple_digest",
            "runtime_identity_observation_digest",
        ):
            if receipt.get(field) is not None:
                raise BaselineError("failed_attempt_success_evidence_invalid")
        if receipt.get("semantic_result_sha256") != [] or receipt.get("phases") != []:
            raise BaselineError("failed_attempt_success_evidence_invalid")
    _timestamp(receipt.get("completed_at"), "attempt_completed_at_invalid")
    raw = receipt.get("raw_receipts")
    if not isinstance(raw, list) or not raw:
        raise BaselineError("attempt_raw_receipts_missing")
    for item in raw:
        raw_item = _object(item, "attempt_raw_receipt_invalid")
        _digest(raw_item.get("sha256"), "attempt_raw_receipt_digest_invalid")
        if (
            isinstance(raw_item.get("bytes"), bool)
            or not isinstance(raw_item.get("bytes"), int)
            or raw_item["bytes"] < 1
        ):
            raise BaselineError("attempt_raw_receipt_bytes_invalid")
    return receipt


def nearest_rank(
    values: Sequence[float], percentile: int, attempts: int
) -> float | None:
    minimum = {50: 2, 95: 20}.get(percentile)
    if minimum is None or attempts < minimum:
        return None
    ordered = sorted(values)
    rank = math.ceil(percentile / 100 * attempts)
    return None if rank > len(ordered) else ordered[rank - 1]


def _mad(values: Sequence[float]) -> float | None:
    if not values:
        return None
    median = statistics.median(values)
    return round(float(statistics.median(abs(value - median) for value in values)), 9)


def _aggregate_metric(
    attempts: Sequence[Mapping[str, Any]], field: str
) -> dict[str, Any]:
    values = [float(item[field]) for item in attempts if item["result"] == "PASS"]
    return {
        "p50_seconds": nearest_rank(values, 50, len(attempts)),
        "p95_seconds": nearest_rank(values, 95, len(attempts)),
        "median_absolute_deviation_seconds": _mad(values),
        "minimum_seconds": min(values) if values else None,
        "maximum_seconds": max(values) if values else None,
    }


def aggregate(args: argparse.Namespace) -> dict[str, Any]:
    plan = validate_plan(load_json(args.plan, private=True))
    receipts: dict[str, dict[str, Any]] = {}
    for path in sorted(args.attempt_dir.resolve().glob("*.json")):
        attempt = validate_attempt(load_json(path, private=True), plan)
        attempt_id = attempt["attempt_id"]
        if attempt_id in receipts:
            raise BaselineError("aggregate_duplicate_attempt")
        receipts[attempt_id] = attempt
    cells: list[dict[str, Any]] = []
    blocked_cells = 0
    blocked_planned_slots = 0
    not_applicable_cells = 0
    missing_attempts: list[str] = []
    failed_attempts: list[str] = []
    phase_missing: list[str] = []
    ranked: list[dict[str, Any]] = []
    for cell in plan["cells"]:
        expected = cell["expected_attempt_ids"]
        attempts = [receipts[item] for item in expected if item in receipts]
        missing = [item for item in expected if item not in receipts]
        missing_attempts.extend(missing)
        if cell["admission"] == "blocked":
            blocked_cells += 1
            blocked_planned_slots += len(cell["blocked_planned_slots"])
        elif cell["admission"] == "not-applicable":
            not_applicable_cells += 1
        successful = [item for item in attempts if item["result"] == "PASS"]
        failed_attempts.extend(
            item["attempt_id"] for item in attempts if item["result"] == "FAIL"
        )
        phase_aggregates: list[dict[str, Any]] = []
        for phase in PHASE_NAMES:
            durations = [
                float(row["duration_seconds"])
                for attempt in successful
                for row in attempt["phases"]
                if row["name"] == phase and row["state"] == "observed"
            ]
            missing_count = sum(
                1
                for attempt in successful
                for row in attempt["phases"]
                if row["name"] == phase and row["state"] == "missing"
            )
            if missing_count:
                phase_missing.append(f"{cell['cell_id']}:{phase}")
            phase_row = {
                "name": phase,
                "observed_count": len(durations),
                "missing_count": missing_count,
                "p50_seconds": nearest_rank(durations, 50, len(attempts)),
                "p95_seconds": nearest_rank(durations, 95, len(attempts)),
                "median_absolute_deviation_seconds": _mad(durations),
            }
            phase_aggregates.append(phase_row)
            if phase_row["p50_seconds"] is not None:
                ranked.append(
                    {
                        "cell_id": cell["cell_id"],
                        "model_id": cell["model_id"],
                        "capacity_state": cell["capacity_state"],
                        "phase": phase,
                        "p50_seconds": phase_row["p50_seconds"],
                    }
                )
        cells.append(
            {
                **cell,
                "attempted": len(attempts),
                "passed": len(successful),
                "failed": len(attempts) - len(successful),
                "missing_attempt_ids": missing,
                "t0_to_call1": _aggregate_metric(attempts, "t0_to_call1_seconds"),
                "t0_to_call2": _aggregate_metric(attempts, "t0_to_call2_seconds"),
                "phases": phase_aggregates,
            }
        )
    status = "COMPLETE"
    if blocked_cells or missing_attempts or failed_attempts or phase_missing:
        status = "INCOMPLETE"
    packet: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/full-catalog-conventional-baseline/v1",
        "plan_digest": plan["plan_digest"],
        "status": status,
        "attempt_count": len(receipts),
        "passed_attempt_count": sum(
            1 for receipt in receipts.values() if receipt["result"] == "PASS"
        ),
        "failed_attempt_count": len(failed_attempts),
        "expected_admitted_attempt_count": sum(
            len(cell["expected_attempt_ids"]) for cell in plan["cells"]
        ),
        "blocked_cell_count": blocked_cells,
        "blocked_planned_slot_count": blocked_planned_slots,
        "not_applicable_cell_count": not_applicable_cells,
        "missing_attempt_ids": sorted(missing_attempts),
        "failed_attempt_ids": sorted(failed_attempts),
        "cells_with_missing_phase_evidence": sorted(set(phase_missing)),
        "dcgm_evidence": {
            "proxy_classification": "NOMINAL_SCRAPE_PROXY",
            "hardware_source_timestamp_state": "UNOBSERVED_HARDWARE_SOURCE_TIMESTAMP",
            "instrumentation_gap": "DCGM_SOURCE_TIMESTAMP_UNOBSERVED",
            "proxy_attempt_count": sum(
                1
                for attempt in receipts.values()
                if attempt["result"] == "PASS" and attempt["dcgm"] is not None
            ),
        },
        "cells": cells,
        "bottleneck_ranking": sorted(
            ranked, key=lambda item: float(item["p50_seconds"]), reverse=True
        ),
        "raw_attempt_receipt_digests": sorted(
            attempt["receipt_digest"] for attempt in receipts.values()
        ),
        "generated_at": datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    }
    packet["packet_digest"] = canonical_digest(packet)
    return packet


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("--campaign-id", required=True)
    plan.add_argument("--source-commit", required=True)
    plan.add_argument("--source-tree", required=True)
    plan.add_argument("--acceptance-receipt", type=Path, required=True)
    plan.add_argument("--terraform-provenance", type=Path, required=True)
    plan.add_argument("--nim-cache-identity", type=Path, action="append", default=[])
    plan.add_argument("--output", type=Path, required=True)

    support = subparsers.add_parser("seal-phase-support")
    support.add_argument("--plan", type=Path, required=True)
    support.add_argument("--attempt-id", required=True)
    support.add_argument(
        "--source-kind", choices=PHASE_SUPPORT_SOURCE_KINDS, required=True
    )
    support.add_argument("--raw-receipt", type=Path, required=True)
    support.add_argument("--event", action="append", default=[], metavar="NAME=UTC")
    support.add_argument("--output", type=Path, required=True)

    record = subparsers.add_parser("record")
    record.add_argument("--plan", type=Path, required=True)
    record.add_argument("--backend-id", required=True)
    record.add_argument("--capacity-state", choices=CAPACITY_STATES, required=True)
    record.add_argument("--ordinal", type=int, choices=(1, 2, 3), required=True)
    record.add_argument("--raw-acceptance", type=Path, required=True)
    record.add_argument("--compatibility-tuple", type=Path)
    record.add_argument("--phase-support", type=Path, action="append", default=[])
    record.add_argument("--dcgm-receipt", type=Path)
    record.add_argument("--output", type=Path, required=True)

    summarize = subparsers.add_parser("aggregate")
    summarize.add_argument("--plan", type=Path, required=True)
    summarize.add_argument("--attempt-dir", type=Path, required=True)
    summarize.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "plan":
            value = build_plan(args)
        elif args.command == "seal-phase-support":
            value = build_phase_support(args)
        elif args.command == "record":
            value = record_attempt(args)
        else:
            value = aggregate(args)
        write_json_new(args.output.resolve(), value)
    except BaselineError:
        return 2
    return 0 if value.get("status") != "INCOMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())

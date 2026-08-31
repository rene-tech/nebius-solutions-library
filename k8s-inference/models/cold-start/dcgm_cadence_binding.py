#!/usr/bin/env python3
"""Validate the small Terraform/live binding used by DCGM proxy receipts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


SCHEMA = "fs2-serve.nebius.ai/dcgm-cadence-live-binding/v1"
METRICS = ("DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_FB_USED")
EXPORTER_IMAGE = (
    "nvcr.io/nvidia/k8s/dcgm-exporter@"
    "sha256:b4df763de9558e5b3f1f1d79bc65b772fcf65b8a9c3664ea7173e47153112b4a"
)
MAX_BYTES = 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class CadenceBindingError(ValueError):
    """The reviewed cadence or observed rollout binding is inconsistent."""


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        + b"\n"
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise CadenceBindingError(code)
    return value


def _sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise CadenceBindingError(code)
    return value


def _identity(value: Any, *, name: str) -> dict[str, Any]:
    result = _exact(
        value,
        {"namespace", "name", "uid", "resource_version"},
        "cadence_observed_identity_invalid",
    )
    if (
        result.get("namespace") != "fs2-observability"
        or result.get("name") != name
        or not isinstance(result.get("uid"), str)
        or IDENTITY.fullmatch(result["uid"]) is None
        or not isinstance(result.get("resource_version"), str)
        or not result["resource_version"]
    ):
        raise CadenceBindingError("cadence_observed_identity_invalid")
    return result


def _positive(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CadenceBindingError(code)
    return value


def _seconds(value: Any) -> float:
    match = (
        re.fullmatch(r"([1-9][0-9]*)(ms|s)", value) if isinstance(value, str) else None
    )
    if match is None:
        raise CadenceBindingError("cadence_interval_invalid")
    amount = int(match.group(1))
    return amount / 1000 if match.group(2) == "ms" else float(amount)


def _profile_contract(path: Path, campaign: bool) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        contract = yaml.safe_load(raw)
    except (OSError, UnicodeError, yaml.YAMLError):
        raise CadenceBindingError("cadence_profile_unavailable") from None
    contract = _exact(
        contract,
        {"schema", "campaignMetrics", "profiles"},
        "cadence_profile_invalid",
    )
    if (
        contract.get("schema") != "fs2-serve.nebius.ai/dcgm-cadence-profiles/v1"
        or contract.get("campaignMetrics") != list(METRICS)
        or not isinstance(contract.get("profiles"), dict)
        or set(contract["profiles"]) != {"standard", "coldStartCampaign"}
    ):
        raise CadenceBindingError("cadence_profile_invalid")
    profile = contract["profiles"].get("coldStartCampaign" if campaign else "standard")
    profile = _exact(
        profile,
        {
            "attributionMetricCollectionInterval",
            "minimumNominalWindowSeconds",
            "helmValues",
        },
        "cadence_profile_invalid",
    )
    return profile, hashlib.sha256(raw).hexdigest()


def validate(value: Any, *, profile_path: Path) -> dict[str, Any]:
    """Return only cadence values after exact source and rollout validation."""

    binding = _exact(
        value,
        {"schema", "source", "terraform", "observed", "captured_at", "receipt_digest"},
        "cadence_binding_shape_invalid",
    )
    if binding.get("schema") != SCHEMA:
        raise CadenceBindingError("cadence_binding_schema_invalid")
    receipt_digest = _sha256(
        binding.get("receipt_digest"), "cadence_binding_digest_invalid"
    )
    unsigned = dict(binding)
    del unsigned["receipt_digest"]
    if digest(unsigned) != receipt_digest:
        raise CadenceBindingError("cadence_binding_digest_mismatch")
    source = _exact(
        binding.get("source"), {"commit", "tree"}, "cadence_binding_source_invalid"
    )
    if any(
        not isinstance(source.get(name), str) or GIT_SHA.fullmatch(source[name]) is None
        for name in ("commit", "tree")
    ):
        raise CadenceBindingError("cadence_binding_source_invalid")

    terraform = _exact(
        binding.get("terraform"),
        {
            "saved_plan_sha256",
            "cadence_profile_sha256",
            "dcgm_attribution_contract",
            "dcgm_attribution_contract_sha256",
        },
        "cadence_terraform_binding_invalid",
    )
    saved_plan = _sha256(
        terraform.get("saved_plan_sha256"), "cadence_saved_plan_digest_invalid"
    )
    output = terraform.get("dcgm_attribution_contract")
    if not isinstance(output, dict) or not isinstance(
        output.get("campaign_enabled"), bool
    ):
        raise CadenceBindingError("cadence_terraform_output_invalid")
    if terraform.get("dcgm_attribution_contract_sha256") != digest(output):
        raise CadenceBindingError("cadence_terraform_output_digest_mismatch")
    profile, profile_digest = _profile_contract(
        profile_path, output["campaign_enabled"]
    )
    if terraform.get("cadence_profile_sha256") != profile_digest:
        raise CadenceBindingError("cadence_profile_digest_mismatch")
    monitor_profile = profile.get("helmValues", {}).get("serviceMonitor")
    if not isinstance(monitor_profile, dict):
        raise CadenceBindingError("cadence_profile_invalid")
    collection = _seconds(profile.get("attributionMetricCollectionInterval"))
    scrape = _seconds(monitor_profile.get("interval"))
    minimum = profile.get("minimumNominalWindowSeconds")
    expected_output = {
        "schema": "fs2-serve.nebius.ai/dcgm-attribution-terraform/v1",
        "campaign_enabled": output["campaign_enabled"],
        "attribution_metric_collection_interval": profile[
            "attributionMetricCollectionInterval"
        ],
        "scrape_interval": monitor_profile.get("interval"),
        "scrape_timeout": monitor_profile.get("scrapeTimeout"),
        "campaign_metrics": list(METRICS),
        "minimum_nominal_window_seconds": minimum,
        "missing_sample_policy": "FAIL_CLOSED_NO_ESTIMATE",
    }
    if (
        output != expected_output
        or isinstance(minimum, bool)
        or not isinstance(minimum, (int, float))
        or not math.isclose(float(minimum), collection + scrape, abs_tol=1e-9)
    ):
        raise CadenceBindingError("cadence_terraform_output_invalid")

    observed = _exact(
        binding.get("observed"),
        {"config_map", "service_monitor", "daemon_set"},
        "cadence_observed_invalid",
    )
    config_map = observed.get("config_map")
    config_identity: dict[str, Any] | None = None
    if output["campaign_enabled"]:
        config_map = _exact(
            config_map,
            {"identity", "data_sha256"},
            "cadence_config_map_invalid",
        )
        config_identity = _identity(
            config_map.get("identity"), name="fs2-dcgm-exporter-config"
        )
        config_data = profile.get("helmValues", {}).get("config", {}).get("data")
        expected_data_digest = (
            hashlib.sha256(config_data.encode("utf-8")).hexdigest()
            if isinstance(config_data, str)
            else None
        )
        if config_map.get("data_sha256") != expected_data_digest:
            raise CadenceBindingError("cadence_config_map_invalid")
    elif config_map is not None:
        raise CadenceBindingError("cadence_config_map_unexpected")

    service_monitor = _exact(
        observed.get("service_monitor"),
        {
            "identity",
            "generation",
            "interval",
            "scrape_timeout",
            "metric_relabelings",
            "spec_sha256",
        },
        "cadence_service_monitor_invalid",
    )
    _identity(service_monitor.get("identity"), name="fs2-dcgm-exporter")
    _positive(service_monitor.get("generation"), "cadence_service_monitor_invalid")
    _sha256(service_monitor.get("spec_sha256"), "cadence_service_monitor_invalid")
    if (
        service_monitor.get("interval") != monitor_profile.get("interval")
        or service_monitor.get("scrape_timeout") != monitor_profile.get("scrapeTimeout")
        or service_monitor.get("metric_relabelings")
        != monitor_profile.get("metricRelabelings")
    ):
        raise CadenceBindingError("cadence_service_monitor_invalid")

    daemon_set = _exact(
        observed.get("daemon_set"),
        {
            "identity",
            "generation",
            "observed_generation",
            "desired_number_scheduled",
            "updated_number_scheduled",
            "number_ready",
            "exporter_image",
            "config_map_name",
            "config_map_uid",
            "config_map_resource_version",
            "pod_template_sha256",
            "ready_pod_uids",
        },
        "cadence_daemon_set_invalid",
    )
    _identity(daemon_set.get("identity"), name="fs2-dcgm-exporter")
    generation = _positive(daemon_set.get("generation"), "cadence_daemon_set_invalid")
    observed_generation = _positive(
        daemon_set.get("observed_generation"), "cadence_daemon_set_invalid"
    )
    counts = [
        daemon_set.get("desired_number_scheduled"),
        daemon_set.get("updated_number_scheduled"),
        daemon_set.get("number_ready"),
    ]
    ready_pods = daemon_set.get("ready_pod_uids")
    if (
        observed_generation != generation
        or any(isinstance(item, bool) or not isinstance(item, int) for item in counts)
        or len(set(counts)) != 1
        or counts[0] < 1
        or not isinstance(ready_pods, list)
        or len(ready_pods) != counts[0]
        or ready_pods != sorted(set(ready_pods))
        or any(
            not isinstance(uid, str) or IDENTITY.fullmatch(uid) is None
            for uid in ready_pods
        )
        or daemon_set.get("exporter_image") != EXPORTER_IMAGE
    ):
        raise CadenceBindingError("cadence_daemon_set_rollout_invalid")
    _sha256(daemon_set.get("pod_template_sha256"), "cadence_daemon_set_rollout_invalid")
    if output["campaign_enabled"]:
        if (
            daemon_set.get("config_map_name") != "fs2-dcgm-exporter-config"
            or config_identity is None
            or daemon_set.get("config_map_uid") != config_identity["uid"]
            or daemon_set.get("config_map_resource_version")
            != config_identity["resource_version"]
        ):
            raise CadenceBindingError("cadence_daemon_set_config_invalid")
    elif any(
        daemon_set.get(name) is not None
        for name in (
            "config_map_name",
            "config_map_uid",
            "config_map_resource_version",
        )
    ):
        raise CadenceBindingError("cadence_daemon_set_config_unexpected")
    captured = binding.get("captured_at")
    if not isinstance(captured, str) or not captured.endswith("Z"):
        raise CadenceBindingError("cadence_binding_timestamp_invalid")
    try:
        datetime.fromisoformat(captured[:-1] + "+00:00")
    except ValueError:
        raise CadenceBindingError("cadence_binding_timestamp_invalid") from None
    return {
        "source": source,
        "saved_plan_sha256": saved_plan,
        "terraform_output_sha256": terraform["dcgm_attribution_contract_sha256"],
        "cadence_profile_sha256": profile_digest,
        "collection_interval_seconds": collection,
        "scrape_interval_seconds": scrape,
        "campaign_enabled": output["campaign_enabled"],
        "binding_receipt_digest": receipt_digest,
    }


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise CadenceBindingError("cadence_binding_duplicate_key")
        result[key] = item
    return result


def load_private(
    path: Path, *, profile_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load one canonical mode-0600 binding and return exact file provenance."""

    if not path.is_absolute():
        raise CadenceBindingError("cadence_binding_path_not_absolute")
    try:
        metadata = path.lstat()
        raw = path.read_bytes()
    except OSError:
        raise CadenceBindingError("cadence_binding_unavailable") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not 1 <= len(raw) <= MAX_BYTES
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
    ):
        raise CadenceBindingError("cadence_binding_file_invalid")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicates)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise CadenceBindingError("cadence_binding_parse_failed") from None
    if raw != canonical_bytes(value):
        raise CadenceBindingError("cadence_binding_not_canonical")
    normalized = validate(value, profile_path=profile_path)
    return value, {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        **normalized,
    }

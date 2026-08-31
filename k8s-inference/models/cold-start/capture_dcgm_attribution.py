#!/usr/bin/env python3
"""Capture a bounded, private DCGM range receipt for one baseline attempt."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


MAX_RESPONSE_BYTES = 32 * 1024 * 1024
METRICS = ("DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_FB_USED")
RANGE_PADDING_MILLISECONDS = 1
CADENCE_PROFILE_PATH = (
    Path(__file__).resolve().parents[2]
    / "stages/workloads/values/dcgm-cadence-profiles.yaml"
)
CADENCE_BINDING_PATH = Path(__file__).with_name("dcgm_cadence_binding.py")
POD_UID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
ATTEMPT_ID = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:--[a-z0-9][a-z0-9-]*){2}$")
NAMESPACE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")


class DcgmError(ValueError):
    """The query target or returned attribution evidence is invalid."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        + b"\n"
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _timestamp(value: Any) -> float:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DcgmError("timestamp_not_utc")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").timestamp()
    except ValueError:
        raise DcgmError("timestamp_invalid") from None


def _prometheus_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.port is None
    ):
        raise DcgmError("prometheus_origin_not_local_port_forward")
    return f"http://{parsed.hostname}:{parsed.port}"


def _query_range_vector(
    origin: str,
    expression: str,
    evaluation_time: str,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"query": expression, "time": evaluation_time})
    request = urllib.request.Request(
        origin + "/api/v1/query?" + query,
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise DcgmError("prometheus_query_failed") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise DcgmError("prometheus_response_too_large")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise DcgmError("prometheus_response_invalid") from None
    data = value.get("data") if isinstance(value, dict) else None
    result = data.get("result") if isinstance(data, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("status") != "success"
        or not isinstance(data, dict)
        or data.get("resultType") != "matrix"
        or not isinstance(result, list)
    ):
        raise DcgmError("prometheus_query_unsuccessful")
    return result


def _series(
    metric_name: str,
    values: list[dict[str, Any]],
    namespace: str,
    pod_uids: set[str],
    raw_query_floor: float,
    window_start: float,
    nominal_proxy_floor: float,
    window_end: float,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[float],
    int,
    int,
]:
    raw_output: list[dict[str, Any]] = []
    output: list[dict[str, Any]] = []
    samples: list[float] = []
    excluded_pre_t0 = 0
    excluded_pre_nominal_proxy = 0
    for raw in values:
        if not isinstance(raw, dict):
            raise DcgmError("dcgm_series_invalid")
        labels = raw.get("metric")
        points = raw.get("values")
        if (
            not isinstance(labels, dict)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in labels.items()
            )
            or not isinstance(points, list)
            or raw.get("histograms") is not None
        ):
            raise DcgmError("dcgm_series_invalid")
        if labels.get("__name__") != metric_name:
            raise DcgmError("dcgm_metric_name_mismatch")
        if (
            labels.get("namespace") != namespace
            or labels.get("pod_uid") not in pod_uids
        ):
            raise DcgmError("dcgm_attribution_mismatch")
        for required in ("pod", "pod_uid", "container", "UUID", "gpu"):
            if not isinstance(labels.get(required), str) or not labels[required]:
                raise DcgmError("dcgm_attribution_incomplete")
        normalized_raw_points: list[list[float]] = []
        normalized_points: list[list[float]] = []
        previous_timestamp: float | None = None
        for point in points:
            if not isinstance(point, list) or len(point) != 2:
                raise DcgmError("dcgm_sample_invalid")
            try:
                timestamp = float(point[0])
                sample = float(point[1])
            except (TypeError, ValueError):
                raise DcgmError("dcgm_sample_invalid") from None
            if not math.isfinite(timestamp) or not math.isfinite(sample) or sample < 0:
                raise DcgmError("dcgm_sample_invalid")
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise DcgmError("dcgm_sample_order_invalid")
            previous_timestamp = timestamp
            if not raw_query_floor < timestamp <= window_end:
                raise DcgmError("dcgm_sample_outside_raw_query")
            normalized_raw_points.append([timestamp, sample])
            if timestamp < window_start:
                excluded_pre_t0 += 1
                continue
            if timestamp < nominal_proxy_floor:
                excluded_pre_nominal_proxy += 1
                continue
            normalized_points.append([timestamp, sample])
            samples.append(sample)
        normalized_labels = {key: labels[key] for key in sorted(labels)}
        if normalized_raw_points:
            raw_output.append(
                {
                    "labels": normalized_labels,
                    "values": normalized_raw_points,
                }
            )
        if normalized_points:
            output.append(
                {
                    "labels": normalized_labels,
                    "values": normalized_points,
                }
            )
    return (
        raw_output,
        output,
        samples,
        excluded_pre_t0,
        excluded_pre_nominal_proxy,
    )


def _exact_mapping(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise DcgmError(code)
    return value


def _cadence_binding_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "fs2_dcgm_cadence_binding", CADENCE_BINDING_PATH
    )
    if spec is None or spec.loader is None:
        raise DcgmError("cadence_binding_validator_unavailable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except BaseException:
        raise DcgmError("cadence_binding_validator_unavailable") from None
    return module


def validate_cadence_binding(value: Any) -> dict[str, Any]:
    try:
        return _cadence_binding_module().validate(
            value, profile_path=CADENCE_PROFILE_PATH
        )
    except ValueError as error:
        raise DcgmError(str(error)) from None


def load_cadence_binding(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        return _cadence_binding_module().load_private(
            path, profile_path=CADENCE_PROFILE_PATH
        )
    except ValueError as error:
        raise DcgmError(str(error)) from None


def validate_receipt(value: Any) -> None:
    """Recompute every v2 query, timestamp, identity, and summary invariant."""

    unsigned_keys = {
        "schema",
        "attempt_id",
        "identity_binding",
        "window",
        "query",
        "cadence_binding",
        "cadence_binding_file",
        "sampling_feasibility",
        "namespace",
        "pod_uids",
        "expected_gpu_count",
        "summary",
        "raw_query_series",
        "series",
        "captured_at",
    }
    if not isinstance(value, dict) or set(value) not in (
        unsigned_keys,
        unsigned_keys | {"receipt_digest"},
    ):
        raise DcgmError("dcgm_receipt_shape_invalid")
    if value.get("schema") != "fs2-serve.nebius.ai/dcgm-attribution/v2":
        raise DcgmError("dcgm_receipt_schema_invalid")
    if "receipt_digest" in value:
        digest = value.get("receipt_digest")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise DcgmError("dcgm_receipt_digest_invalid")
        unsigned = dict(value)
        del unsigned["receipt_digest"]
        if _digest(unsigned) != digest:
            raise DcgmError("dcgm_receipt_digest_mismatch")
    attempt_id = value.get("attempt_id")
    if not isinstance(attempt_id, str) or ATTEMPT_ID.fullmatch(attempt_id) is None:
        raise DcgmError("attempt_id_invalid")
    binding = _exact_mapping(
        value.get("identity_binding"),
        {"pod_uids", "node_uids", "gpu_uuids", "attempt_t0", "attempt_t1"},
        "dcgm_identity_binding_invalid",
    )
    identity_patterns = {
        "pod_uids": POD_UID,
        "node_uids": POD_UID,
        "gpu_uuids": DEVICE_ID,
    }
    for name, pattern in identity_patterns.items():
        identities = binding.get(name)
        if (
            not isinstance(identities, list)
            or not identities
            or any(
                not isinstance(identity, str) or pattern.fullmatch(identity) is None
                for identity in identities
            )
            or identities != sorted(set(identities))
        ):
            raise DcgmError("dcgm_identity_binding_invalid")
    start = _timestamp(binding["attempt_t0"])
    end = _timestamp(binding["attempt_t1"])
    if end <= start or end - start > 6 * 60 * 60:
        raise DcgmError("query_window_invalid")
    window = _exact_mapping(
        value.get("window"),
        {"start", "end", "boundary"},
        "dcgm_window_invalid",
    )
    if window != {
        "start": binding["attempt_t0"],
        "end": binding["attempt_t1"],
        "boundary": "inclusive",
    }:
        raise DcgmError("dcgm_window_invalid")
    attempt_duration = end - start
    # Prometheus 3 range vectors are left-open. Padding the selector below T0
    # keeps an exact T0 sample eligible; the collector then excludes padding
    # samples from all summaries.
    range_milliseconds = math.ceil(attempt_duration * 1000) + RANGE_PADDING_MILLISECONDS
    raw_query_floor = end - (range_milliseconds / 1000)
    query = _exact_mapping(
        value.get("query"),
        {
            "endpoint",
            "evaluation_time",
            "result_type",
            "sample_timestamp_source",
            "range_selector_milliseconds",
            "raw_query_floor_epoch_seconds",
            "raw_query_floor_boundary",
            "expressions",
        },
        "dcgm_query_contract_invalid",
    )
    floor = query.get("raw_query_floor_epoch_seconds")
    if (
        query.get("endpoint") != "/api/v1/query"
        or query.get("evaluation_time") != binding["attempt_t1"]
        or query.get("result_type") != "matrix"
        or query.get("sample_timestamp_source") != "range-vector-raw-values"
        or query.get("range_selector_milliseconds") != range_milliseconds
        or isinstance(floor, bool)
        or not isinstance(floor, (int, float))
        or not math.isfinite(floor)
        or not math.isclose(floor, raw_query_floor, rel_tol=0.0, abs_tol=1e-6)
        or query.get("raw_query_floor_boundary") != "exclusive"
    ):
        raise DcgmError("dcgm_query_contract_invalid")
    namespace = value.get("namespace")
    pod_uids = value.get("pod_uids")
    expected_gpu_count = value.get("expected_gpu_count")
    if (
        not isinstance(namespace, str)
        or NAMESPACE.fullmatch(namespace) is None
        or pod_uids != binding["pod_uids"]
        or isinstance(expected_gpu_count, bool)
        or not isinstance(expected_gpu_count, int)
        or not 1 <= expected_gpu_count <= 64
        or expected_gpu_count != len(binding["gpu_uuids"])
    ):
        raise DcgmError("dcgm_attribution_contract_invalid")
    selector = ",".join(
        [
            f'namespace="{namespace}"',
            'pod!=""',
            'container!=""',
            'pod_uid=~"'
            + "|".join(re.escape(item) for item in binding["pod_uids"])
            + '"',
        ]
    )
    expressions = _exact_mapping(
        query.get("expressions"), set(METRICS), "dcgm_query_contract_invalid"
    )
    for metric in METRICS:
        expected_expression = f"{metric}{{{selector}}}[{range_milliseconds}ms]"
        if expressions.get(metric) != expected_expression:
            raise DcgmError("dcgm_query_contract_invalid")
    cadence_binding = value.get("cadence_binding")
    cadence = validate_cadence_binding(cadence_binding)
    cadence_file = _exact_mapping(
        value.get("cadence_binding_file"),
        {"sha256", "bytes"},
        "dcgm_cadence_binding_file_invalid",
    )
    if (
        cadence_file.get("sha256") != _digest(cadence_binding)
        or isinstance(cadence_file.get("bytes"), bool)
        or not isinstance(cadence_file.get("bytes"), int)
        or cadence_file["bytes"] != len(_canonical_bytes(cadence_binding))
    ):
        raise DcgmError("dcgm_cadence_binding_file_invalid")
    sampling = _exact_mapping(
        value.get("sampling_feasibility"),
        {
            "terraform_bound_collection_interval_seconds",
            "terraform_bound_scrape_interval_seconds",
            "attempt_duration_seconds",
            "minimum_nominal_proxy_offset_seconds",
            "earliest_nominal_proxy_epoch_seconds",
            "hardware_sample_timestamp_available",
            "hardware_source_timestamp_state",
            "exporter_cache_semantics",
            "nominal_proxy_window_can_fit_attempt",
            "proxy_classification",
            "instrumentation_gap",
            "missing_sample_policy",
            "excluded_pre_t0_sample_count",
            "excluded_pre_nominal_proxy_sample_count",
        },
        "dcgm_sampling_contract_invalid",
    )
    collection_interval = sampling.get("terraform_bound_collection_interval_seconds")
    scrape_interval = sampling.get("terraform_bound_scrape_interval_seconds")
    duration = sampling.get("attempt_duration_seconds")
    intervals = (collection_interval, scrape_interval)
    intervals_valid = all(
        not isinstance(interval, bool)
        and isinstance(interval, (int, float))
        and math.isfinite(interval)
        and 0.05 <= interval <= 300
        for interval in intervals
    )
    minimum_offset = collection_interval + scrape_interval if intervals_valid else None
    earliest_nominal_proxy = start + minimum_offset if intervals_valid else None
    nominal_fit = attempt_duration >= minimum_offset if intervals_valid else None
    reported_offset = sampling.get("minimum_nominal_proxy_offset_seconds")
    reported_earliest = sampling.get("earliest_nominal_proxy_epoch_seconds")
    reported_nominal_values_valid = all(
        not isinstance(item, bool)
        and isinstance(item, (int, float))
        and math.isfinite(item)
        for item in (reported_offset, reported_earliest)
    )
    if (
        not intervals_valid
        or not reported_nominal_values_valid
        or not math.isclose(
            collection_interval,
            cadence["collection_interval_seconds"],
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        or not math.isclose(
            scrape_interval,
            cadence["scrape_interval_seconds"],
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or not math.isclose(duration, attempt_duration, rel_tol=0.0, abs_tol=1e-6)
        or not math.isclose(
            reported_offset,
            minimum_offset,
            rel_tol=0.0,
            abs_tol=1e-6,
        )
        or not math.isclose(
            reported_earliest,
            earliest_nominal_proxy,
            rel_tol=0.0,
            abs_tol=1e-6,
        )
        or sampling.get("hardware_sample_timestamp_available") is not False
        or sampling.get("hardware_source_timestamp_state")
        != "UNOBSERVED_HARDWARE_SOURCE_TIMESTAMP"
        or sampling.get("exporter_cache_semantics")
        != "PROMETHEUS_SCRAPE_READS_LATEST_EXPORTER_CACHE"
        or sampling.get("nominal_proxy_window_can_fit_attempt") is not nominal_fit
        or sampling.get("proxy_classification") != "NOMINAL_SCRAPE_PROXY"
        or sampling.get("instrumentation_gap") != "DCGM_SOURCE_TIMESTAMP_UNOBSERVED"
        or sampling.get("missing_sample_policy") != "FAIL_CLOSED_NO_ESTIMATE"
        or nominal_fit is not True
    ):
        raise DcgmError("dcgm_sampling_contract_invalid")
    excluded_pre_t0 = _exact_mapping(
        sampling.get("excluded_pre_t0_sample_count"),
        set(METRICS),
        "dcgm_sampling_contract_invalid",
    )
    excluded_pre_nominal_proxy = _exact_mapping(
        sampling.get("excluded_pre_nominal_proxy_sample_count"),
        set(METRICS),
        "dcgm_sampling_contract_invalid",
    )
    raw_query_series = _exact_mapping(
        value.get("raw_query_series"), set(METRICS), "dcgm_series_invalid"
    )
    series = _exact_mapping(value.get("series"), set(METRICS), "dcgm_series_invalid")
    metric_samples: dict[str, list[float]] = {}
    normalized_series: dict[str, list[dict[str, Any]]] = {}
    for metric in METRICS:
        stored_raw = raw_query_series[metric]
        stored_filtered = series[metric]
        excluded_t0_value = excluded_pre_t0.get(metric)
        excluded_nominal_value = excluded_pre_nominal_proxy.get(metric)
        if (
            not isinstance(stored_raw, list)
            or not isinstance(stored_filtered, list)
            or isinstance(excluded_t0_value, bool)
            or not isinstance(excluded_t0_value, int)
            or excluded_t0_value < 0
            or isinstance(excluded_nominal_value, bool)
            or not isinstance(excluded_nominal_value, int)
            or excluded_nominal_value < 0
        ):
            raise DcgmError("dcgm_series_invalid")
        api_shaped: list[dict[str, Any]] = []
        for item in stored_raw:
            if not isinstance(item, dict) or set(item) != {"labels", "values"}:
                raise DcgmError("dcgm_series_invalid")
            api_shaped.append(
                {
                    "metric": item["labels"],
                    "values": item["values"],
                }
            )
        (
            raw_normalized,
            filtered,
            samples,
            excluded_t0_count,
            excluded_nominal_count,
        ) = _series(
            metric,
            api_shaped,
            namespace,
            set(binding["pod_uids"]),
            raw_query_floor,
            start,
            earliest_nominal_proxy,
            end,
        )
        if (
            raw_normalized != raw_query_series[metric]
            or filtered != stored_filtered
            or not filtered
            or not samples
            or excluded_t0_value != excluded_t0_count
            or excluded_nominal_value != excluded_nominal_count
            or {item["labels"].get("UUID") for item in filtered}
            != set(binding["gpu_uuids"])
        ):
            raise DcgmError("dcgm_series_invalid")
        normalized_series[metric] = filtered
        metric_samples[metric] = samples
    unique_devices = {
        (
            item["labels"].get("UUID"),
            item["labels"].get("gpu"),
            item["labels"].get("pod_uid"),
        )
        for item in normalized_series["DCGM_FI_DEV_GPU_UTIL"]
    }
    utilization = metric_samples["DCGM_FI_DEV_GPU_UTIL"]
    framebuffer_mib = metric_samples["DCGM_FI_DEV_FB_USED"]
    expected_summary = {
        "attributed_device_count": len(unique_devices),
        "gpu_utilization_sample_count": len(utilization),
        "mean_gpu_utilization_percent": round(sum(utilization) / len(utilization), 6),
        "peak_gpu_utilization_percent": max(utilization),
        "framebuffer_sample_count": len(framebuffer_mib),
        "peak_framebuffer_bytes": int(max(framebuffer_mib) * 1024 * 1024),
    }
    summary = _exact_mapping(
        value.get("summary"), set(expected_summary), "dcgm_summary_invalid"
    )
    for name in (
        "attributed_device_count",
        "gpu_utilization_sample_count",
        "framebuffer_sample_count",
        "peak_framebuffer_bytes",
    ):
        if (
            isinstance(summary.get(name), bool)
            or not isinstance(summary.get(name), int)
            or summary[name] < 0
        ):
            raise DcgmError("dcgm_summary_invalid")
    if len(unique_devices) != expected_gpu_count or summary != expected_summary:
        raise DcgmError("dcgm_summary_invalid")
    if _timestamp(value.get("captured_at")) < end:
        raise DcgmError("dcgm_captured_before_attempt_end")


def build_receipt(args: argparse.Namespace) -> dict[str, Any]:
    if ATTEMPT_ID.fullmatch(args.attempt_id) is None:
        raise DcgmError("attempt_id_invalid")
    if (
        not isinstance(args.namespace, str)
        or NAMESPACE.fullmatch(args.namespace) is None
    ):
        raise DcgmError("namespace_invalid")
    if not args.pod_uid or any(
        POD_UID.fullmatch(value) is None for value in args.pod_uid
    ):
        raise DcgmError("pod_uid_invalid")
    if len(set(args.pod_uid)) != len(args.pod_uid):
        raise DcgmError("pod_uid_duplicate")
    if (
        not args.node_uid
        or any(POD_UID.fullmatch(value) is None for value in args.node_uid)
        or len(set(args.node_uid)) != len(args.node_uid)
    ):
        raise DcgmError("node_uid_invalid")
    if (
        not args.gpu_uuid
        or any(DEVICE_ID.fullmatch(value) is None for value in args.gpu_uuid)
        or len(set(args.gpu_uuid)) != len(args.gpu_uuid)
    ):
        raise DcgmError("gpu_uuid_invalid")
    if len(args.gpu_uuid) != args.expected_gpu_count:
        raise DcgmError("expected_gpu_count_mismatch")
    start = _timestamp(args.start)
    end = _timestamp(args.end)
    attempt_t0 = _timestamp(args.attempt_t0)
    attempt_t1 = _timestamp(args.attempt_t1)
    if end <= start or end - start > 6 * 60 * 60:
        raise DcgmError("query_window_invalid")
    if (
        args.start != args.attempt_t0
        or args.end != args.attempt_t1
        or start != attempt_t0
        or end != attempt_t1
    ):
        raise DcgmError("query_window_not_exact_attempt")
    cadence_binding, cadence_file = load_cadence_binding(
        args.cadence_binding_receipt.resolve()
    )
    collection_interval = cadence_file["collection_interval_seconds"]
    scrape_interval = cadence_file["scrape_interval_seconds"]
    attempt_duration = end - start
    minimum_nominal_proxy_offset = collection_interval + scrape_interval
    earliest_nominal_proxy = start + minimum_nominal_proxy_offset
    if attempt_duration < minimum_nominal_proxy_offset:
        raise DcgmError("attempt_shorter_than_collection_plus_scrape")
    range_milliseconds = math.ceil(attempt_duration * 1000) + RANGE_PADDING_MILLISECONDS
    raw_query_floor = end - (range_milliseconds / 1000)
    if raw_query_floor >= start:
        raise DcgmError("raw_query_floor_invalid")
    origin = _prometheus_origin(args.prometheus_url)
    pod_uids = set(args.pod_uid)
    expressions: dict[str, str] = {}
    raw_query_series: dict[str, list[dict[str, Any]]] = {}
    raw_series: dict[str, list[dict[str, Any]]] = {}
    metric_samples: dict[str, list[float]] = {}
    excluded_pre_t0: dict[str, int] = {}
    excluded_pre_nominal_proxy: dict[str, int] = {}
    for metric in METRICS:
        selector = ",".join(
            [
                f'namespace="{args.namespace}"',
                'pod!=""',
                'container!=""',
                'pod_uid=~"'
                + "|".join(re.escape(value) for value in sorted(pod_uids))
                + '"',
            ]
        )
        expression = metric + "{" + selector + "}[" + str(range_milliseconds) + "ms]"
        (
            raw_returned,
            series,
            samples,
            excluded_t0,
            excluded_nominal_proxy,
        ) = _series(
            metric,
            _query_range_vector(origin, expression, args.attempt_t1),
            args.namespace,
            pod_uids,
            raw_query_floor,
            start,
            earliest_nominal_proxy,
            end,
        )
        if not series or not samples:
            raise DcgmError("dcgm_metric_samples_missing")
        expressions[metric] = expression
        raw_query_series[metric] = raw_returned
        raw_series[metric] = series
        metric_samples[metric] = samples
        excluded_pre_t0[metric] = excluded_t0
        excluded_pre_nominal_proxy[metric] = excluded_nominal_proxy
    unique_devices = {
        (
            item["labels"].get("UUID"),
            item["labels"].get("gpu"),
            item["labels"].get("pod_uid"),
        )
        for item in raw_series["DCGM_FI_DEV_GPU_UTIL"]
    }
    for metric in METRICS:
        observed_gpu_uuids = {item["labels"].get("UUID") for item in raw_series[metric]}
        if observed_gpu_uuids != set(args.gpu_uuid):
            raise DcgmError("dcgm_gpu_identity_mismatch")
    if len(unique_devices) != args.expected_gpu_count:
        raise DcgmError("dcgm_gpu_count_mismatch")
    utilization = metric_samples["DCGM_FI_DEV_GPU_UTIL"]
    framebuffer_mib = metric_samples["DCGM_FI_DEV_FB_USED"]
    receipt: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/dcgm-attribution/v2",
        "attempt_id": args.attempt_id,
        "identity_binding": {
            "pod_uids": sorted(pod_uids),
            "node_uids": sorted(args.node_uid),
            "gpu_uuids": sorted(args.gpu_uuid),
            "attempt_t0": args.attempt_t0,
            "attempt_t1": args.attempt_t1,
        },
        "window": {
            "start": args.start,
            "end": args.end,
            "boundary": "inclusive",
        },
        "query": {
            "endpoint": "/api/v1/query",
            "evaluation_time": args.attempt_t1,
            "result_type": "matrix",
            "sample_timestamp_source": "range-vector-raw-values",
            "range_selector_milliseconds": range_milliseconds,
            "raw_query_floor_epoch_seconds": raw_query_floor,
            "raw_query_floor_boundary": "exclusive",
            "expressions": expressions,
        },
        "cadence_binding": cadence_binding,
        "cadence_binding_file": {
            "sha256": cadence_file["sha256"],
            "bytes": cadence_file["bytes"],
        },
        "sampling_feasibility": {
            "terraform_bound_collection_interval_seconds": collection_interval,
            "terraform_bound_scrape_interval_seconds": scrape_interval,
            "attempt_duration_seconds": attempt_duration,
            "minimum_nominal_proxy_offset_seconds": minimum_nominal_proxy_offset,
            "earliest_nominal_proxy_epoch_seconds": earliest_nominal_proxy,
            "hardware_sample_timestamp_available": False,
            "hardware_source_timestamp_state": "UNOBSERVED_HARDWARE_SOURCE_TIMESTAMP",
            "exporter_cache_semantics": "PROMETHEUS_SCRAPE_READS_LATEST_EXPORTER_CACHE",
            "nominal_proxy_window_can_fit_attempt": (
                attempt_duration >= minimum_nominal_proxy_offset
            ),
            "proxy_classification": "NOMINAL_SCRAPE_PROXY",
            "instrumentation_gap": "DCGM_SOURCE_TIMESTAMP_UNOBSERVED",
            "missing_sample_policy": "FAIL_CLOSED_NO_ESTIMATE",
            "excluded_pre_t0_sample_count": excluded_pre_t0,
            "excluded_pre_nominal_proxy_sample_count": excluded_pre_nominal_proxy,
        },
        "namespace": args.namespace,
        "pod_uids": sorted(pod_uids),
        "expected_gpu_count": args.expected_gpu_count,
        "summary": {
            "attributed_device_count": len(unique_devices),
            "gpu_utilization_sample_count": len(utilization),
            "mean_gpu_utilization_percent": round(
                sum(utilization) / len(utilization), 6
            ),
            "peak_gpu_utilization_percent": max(utilization),
            "framebuffer_sample_count": len(framebuffer_mib),
            "peak_framebuffer_bytes": int(max(framebuffer_mib) * 1024 * 1024),
        },
        "raw_query_series": raw_query_series,
        "series": raw_series,
        "captured_at": datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    }
    validate_receipt(receipt)
    receipt["receipt_digest"] = _digest(receipt)
    return receipt


def write_new(path: Path, value: Any) -> None:
    if not path.is_absolute():
        raise DcgmError("output_path_not_absolute")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise DcgmError("output_create_failed") from None
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(_canonical_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise DcgmError("output_mode_invalid")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus-url", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--namespace", default="fs2-models")
    parser.add_argument("--pod-uid", action="append", required=True)
    parser.add_argument("--node-uid", action="append", required=True)
    parser.add_argument("--gpu-uuid", action="append", required=True)
    parser.add_argument("--expected-gpu-count", type=int, required=True)
    parser.add_argument("--attempt-t0", required=True)
    parser.add_argument("--attempt-t1", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--cadence-binding-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.expected_gpu_count <= 64:
        parser.error("--expected-gpu-count must be from 1 through 64")
    return args


def main() -> int:
    args = parse_args()
    try:
        write_new(args.output.resolve(), build_receipt(args))
    except DcgmError:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run repeatable, phase-aware cold-start trials for the scientific fleet.

The existing fleet acceptance runner remains the only workload submitter. This
module composes its public receipts with the authenticated scientific admin and
lifecycle projections, then uses the established fast-start statistics helper
for per-cohort summaries. Missing backend observations stay explicitly
unavailable; no phase is reconstructed from unrelated wall-clock fields.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

HERE = Path(__file__).resolve().parent
SOLUTION_ROOT = HERE.parents[1]


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path.name}")
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


# Reuse the accepted request/polling/redaction, environment-contract and
# dispersion implementations instead of introducing parallel versions here.
FLEET = _module("_fs2_scientific_fleet", HERE / "run_fleet_acceptance.py")
PUBLIC = _module("_fs2_scientific_public", HERE / "run_acceptance.py")
FAST_IDENTITY = _module(
    "_fs2_scientific_fast_identity",
    SOLUTION_ROOT / "models/cold-start/fast_start_identity.py",
)
FAST_AGGREGATE = _module(
    "_fs2_scientific_fast_aggregate",
    SOLUTION_ROOT / "models/cold-start/aggregate_fast_start_benchmark.py",
)

SCHEMA = "fs2-serve.nebius.ai/scientific-fleet-cold-start-benchmark/v1"
EXPECTED_MODELS = frozenset(
    {
        "alphafold3",
        "bindcraft",
        "boltzgen",
        "esmfold2",
        "esmfold2-fast",
        "mosaic",
        "openfold3-openbind",
        "proteina-complexa",
        "protenix-v2",
        "rfdiffusion",
    }
)
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ENVIRONMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TERMINAL_RUN_STATES = frozenset({"succeeded", "failed", "cancelled"})
PHASE_METRICS = {
    "queue_wait_seconds": ("queue",),
    "capacity_wait_seconds": ("admission",),
    "image_localization_seconds": ("image-pull",),
    "artifact_localization_seconds": ("artifact-load",),
    "runtime_model_load_seconds": ("runtime-load", "model-load"),
    "restore_seconds": ("restore",),
    "compile_warmup_seconds": ("compile", "semantic-warmup"),
    "active_compute_seconds": ("active-compute",),
}
STATISTIC_METRICS = (
    *PHASE_METRICS,
    "api_cold_start_seconds",
    "time_to_first_semantic_result_seconds",
    "total_runtime_seconds",
)


class BenchmarkError(RuntimeError):
    """A stable value-suppressed benchmark failure."""

    def __init__(self, code: str) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code) is None:
            raise ValueError("unsafe benchmark error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    endpoint: str
    repository_root: Path
    receipt_root: Path
    run_id: str
    environment_qualifications: Path
    project_id: str
    region: str
    cluster_context: str
    repetitions: int = 3
    max_parallel: int = 8
    inference_token_environment: str = "FS2_INFERENCE_TOKEN"
    admin_token_environment: str = "FS2_ADMIN_TOKEN"
    timeout_seconds: float = 7200.0
    poll_seconds: float = 5.0
    request_timeout_seconds: float = 60.0
    admin_convergence_seconds: float = 30.0
    reserved_pool_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdminEvidence:
    detail: dict[str, Any] | None
    detail_error: str | None
    lifecycle: dict[str, Any] | None
    lifecycle_error: str | None


def _object(value: object, code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BenchmarkError(code)
    return value


def _list(value: object, code: str) -> list[Any]:
    if not isinstance(value, list):
        raise BenchmarkError(code)
    return value


def _canonical(value: object, *, newline: bool = False) -> bytes:
    suffix = "\n" if newline else ""
    try:
        return (
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + suffix
        ).encode()
    except (TypeError, ValueError) as error:
        raise BenchmarkError("receipt_not_canonicalizable") from error


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise BenchmarkError("input_file_unavailable") from error


def _json_file(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > FLEET.MAX_JSON_BYTES:
            raise BenchmarkError("input_json_invalid")
        return _object(json.loads(raw), "input_json_invalid")
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
        raise BenchmarkError("input_json_invalid") from error


def _utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise BenchmarkError("timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise BenchmarkError("timestamp_invalid") from None
    if parsed.tzinfo is None:
        raise BenchmarkError("timestamp_invalid")
    return parsed.astimezone(UTC)


def _unavailable(source: str, reason: str, *, unit: str = "seconds") -> dict[str, Any]:
    return {
        "value": None,
        "unit": unit,
        "evidence": "unavailable",
        "source": source,
        "reason": reason,
    }


def _measured(value: float, source: str, *, unit: str = "seconds") -> dict[str, Any]:
    if value < 0:
        raise BenchmarkError("negative_measurement")
    return {
        "value": round(value, 6),
        "unit": unit,
        "evidence": "measured",
        "source": source,
        "reason": None,
    }


def _elapsed(start: object, end: object, source: str) -> dict[str, Any]:
    if start is None or end is None:
        return _unavailable(source, "One or both exact source timestamps are unavailable.")
    seconds = (_utc(end) - _utc(start)).total_seconds()
    if seconds < 0:
        raise BenchmarkError("timestamp_order_invalid")
    return _measured(seconds, source)


def _normalize_measurement(value: object, source_fallback: str) -> dict[str, Any]:
    item = _object(value, "admin_measurement_invalid")
    evidence = item.get("evidence")
    measured = item.get("value")
    unit = item.get("unit")
    source = item.get("source")
    reason = item.get("reason")
    if evidence not in {"measured", "estimated", "unavailable"}:
        raise BenchmarkError("admin_measurement_invalid")
    if unit not in {"seconds", "gpu-seconds", "bytes", "count"}:
        raise BenchmarkError("admin_measurement_invalid")
    if not isinstance(source, str) or not source:
        source = source_fallback
    if evidence == "unavailable":
        if measured is not None or not isinstance(reason, str) or not reason:
            raise BenchmarkError("admin_measurement_invalid")
        return _unavailable(source, reason, unit=unit)
    if isinstance(measured, bool) or not isinstance(measured, (int, float)) or measured < 0:
        raise BenchmarkError("admin_measurement_invalid")
    if evidence == "estimated" and (not isinstance(reason, str) or not reason):
        raise BenchmarkError("admin_measurement_invalid")
    return {
        "value": round(float(measured), 6),
        "unit": unit,
        "evidence": evidence,
        "source": source,
        "reason": reason if isinstance(reason, str) else None,
    }


def _environment_bindings(config: BenchmarkConfig) -> tuple[list[dict[str, Any]], str]:
    document = _json_file(config.environment_qualifications)
    try:
        bindings = FAST_IDENTITY.validate_environment_qualifications(document)
    except FAST_IDENTITY.IdentityError as error:
        raise BenchmarkError("environment_qualification_invalid") from error
    pools: set[str] = set()
    now = datetime.now(UTC)
    for binding in bindings:
        scope = binding["scope"]
        if scope != {
            "projectId": config.project_id,
            "region": config.region,
            "clusterContext": config.cluster_context,
        }:
            raise BenchmarkError("environment_scope_mismatch")
        if _utc(binding["validUntil"]) <= now:
            raise BenchmarkError("environment_qualification_expired")
        for member in binding["members"]:
            pool = member["poolRef"]
            if not isinstance(pool, str) or not pool or pool in pools:
                raise BenchmarkError("environment_pool_duplicate")
            pools.add(pool)
    if not pools:
        raise BenchmarkError("environment_pool_missing")
    if set(config.reserved_pool_ids) - pools:
        raise BenchmarkError("reserved_pool_unknown")
    return bindings, _file_digest(config.environment_qualifications)


def _pool_bindings(bindings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for binding in bindings:
        for member in binding["members"]:
            result[member["poolRef"]] = {
                "pool_id": member["poolRef"],
                "capacity_type": member["capacityType"],
                "cache_tier": binding["cacheTier"],
                "startup_scenario": binding["startupScenario"],
                "accelerator": binding["accelerator"],
                "driver_cuda": binding["driverCuda"],
                "storage_runtime": binding["storageRuntime"],
                "environment_digest": binding["environment"]["qualificationDigest"],
                "valid_until": binding["validUntil"],
            }
    return result


def _validate_config(config: BenchmarkConfig) -> tuple[Path, Path, dict[str, object]]:
    if SAFE_ID_RE.fullmatch(config.run_id) is None:
        raise BenchmarkError("run_id_invalid")
    for value in (config.project_id, config.region, config.cluster_context, *config.reserved_pool_ids):
        if not isinstance(value, str) or not value or len(value) > 128:
            raise BenchmarkError("deployment_identity_invalid")
    for name in (config.inference_token_environment, config.admin_token_environment):
        if ENVIRONMENT_RE.fullmatch(name) is None:
            raise BenchmarkError("token_environment_invalid")
        token = os.environ.get(name)
        if token is None or not token or any(character.isspace() for character in token):
            raise BenchmarkError("token_environment_missing")
    if not 3 <= config.repetitions <= 20:
        raise BenchmarkError("repetition_count_invalid")
    if not 1 <= config.max_parallel <= FLEET.MAX_PARALLEL:
        raise BenchmarkError("max_parallel_invalid")
    if (
        config.timeout_seconds < 0
        or config.poll_seconds <= 0
        or not 0 < config.request_timeout_seconds <= 600
        or config.admin_convergence_seconds < 0
    ):
        raise BenchmarkError("timeout_invalid")
    try:
        repository_root = config.repository_root.resolve(strict=True)
    except OSError as error:
        raise BenchmarkError("repository_root_invalid") from error
    if not repository_root.is_dir() or repository_root != SOLUTION_ROOT.resolve():
        raise BenchmarkError("repository_root_invalid")
    try:
        endpoint = FLEET._endpoint_identity(config.endpoint)
    except FLEET.FleetAcceptanceError as error:
        raise BenchmarkError("endpoint_invalid") from error
    try:
        root = config.receipt_root.resolve()
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        run_directory = root / config.run_id
        if run_directory.is_symlink() or run_directory.exists():
            raise BenchmarkError("receipt_directory_exists")
        run_directory.mkdir(mode=0o700)
        mode = stat.S_IMODE(run_directory.stat().st_mode)
        if mode & 0o077:
            raise BenchmarkError("receipt_directory_permissions_invalid")
    except OSError as error:
        raise BenchmarkError("receipt_directory_invalid") from error
    return repository_root, run_directory, endpoint


def _source_commit(repository_root: Path) -> str:
    import subprocess

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[a-f0-9]{40}", value) is None:
        raise BenchmarkError("source_commit_unavailable")
    return value


def _admin_json(
    client: Any,
    cookie: str,
    method: str,
    path: str,
    *,
    expected_status: int = 200,
) -> dict[str, Any]:
    try:
        response = client.request(method, path, headers={"Cookie": cookie})
    except PUBLIC.AcceptanceError as error:
        raise BenchmarkError("admin_transport_failed") from error
    if response.status != expected_status:
        raise BenchmarkError(f"admin_http_{response.status}")
    if expected_status == 204:
        return {}
    try:
        return _object(json.loads(response.body), "admin_response_invalid")
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise BenchmarkError("admin_response_invalid") from error


def _open_admin_session(client: Any) -> str:
    try:
        response = client.request("POST", "/admin/api/v1/session")
    except PUBLIC.AcceptanceError as error:
        raise BenchmarkError("admin_session_transport_failed") from error
    if response.status != 200:
        raise BenchmarkError(f"admin_session_http_{response.status}")
    header = response.headers.get("set-cookie")
    if not isinstance(header, str) or len(header) > 8192:
        raise BenchmarkError("admin_session_cookie_missing")
    cookie = SimpleCookie()
    try:
        cookie.load(header)
    except Exception as error:  # noqa: BLE001 - parser detail must not escape.
        raise BenchmarkError("admin_session_cookie_invalid") from error
    morsel = cookie.get("__Host-fs2_admin_session")
    if morsel is None or not morsel.value or len(morsel.value) > 4096:
        raise BenchmarkError("admin_session_cookie_missing")
    return f"__Host-fs2_admin_session={morsel.value}"


def _model_snapshot(client: Any, cookie: str) -> tuple[dict[str, dict[str, Any]], str]:
    document = _admin_json(client, cookie, "GET", "/admin/api/v1/scientific-models")
    data = _object(document.get("data"), "admin_model_snapshot_invalid")
    selected: dict[str, dict[str, Any]] = {}
    for raw in _list(data.get("items"), "admin_model_snapshot_invalid"):
        item = _object(raw, "admin_model_snapshot_invalid")
        model_id = item.get("model_id")
        if model_id not in EXPECTED_MODELS:
            continue
        if model_id in selected:
            raise BenchmarkError("admin_model_duplicate")
        if item.get("workload_profile") != "published" or item.get("batch_supported") is not True:
            raise BenchmarkError("admin_model_not_batch_ready")
        selected[model_id] = {
            "model_id": model_id,
            "readiness": item.get("readiness"),
            "workload_profile": item.get("workload_profile"),
            "batch_supported": item.get("batch_supported"),
            "backend": item.get("backend"),
            "access": item.get("access"),
            "caching": item.get("caching"),
        }
    if set(selected) != EXPECTED_MODELS:
        raise BenchmarkError("admin_model_set_incomplete")
    projection = [selected[key] for key in sorted(selected)]
    return selected, _digest(projection)


def _run_detail(
    client: Any,
    cookie: str,
    operation_id: str,
    model_id: str,
    convergence_seconds: float,
) -> tuple[dict[str, Any] | None, str | None]:
    deadline = time.monotonic() + convergence_seconds
    while True:
        try:
            document = _admin_json(
                client,
                cookie,
                "GET",
                f"/admin/api/v1/scientific-runs/{operation_id}",
            )
        except BenchmarkError as error:
            return None, error.code
        data = _object(document.get("data"), "admin_run_detail_invalid")
        run = _object(data.get("run"), "admin_run_detail_invalid")
        model = _object(run.get("model"), "admin_run_detail_invalid")
        if run.get("id") != operation_id or model.get("model_id") != model_id:
            raise BenchmarkError("admin_run_identity_mismatch")
        if run.get("status") in TERMINAL_RUN_STATES or time.monotonic() >= deadline:
            return data, None
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))


def _lifecycle_detail(
    client: Any,
    cookie: str,
    operation_id: str,
    convergence_seconds: float,
) -> tuple[dict[str, Any] | None, str | None]:
    deadline = time.monotonic() + convergence_seconds
    path = "/admin/api/v1/telemetry/workloads?" + urlencode(
        {"operation_id": operation_id, "limit": 200}
    )
    while True:
        try:
            document = _admin_json(client, cookie, "GET", path)
        except BenchmarkError as error:
            return None, error.code
        data = _object(document.get("data"), "admin_lifecycle_invalid")
        items = _list(data.get("items"), "admin_lifecycle_invalid")
        total = data.get("total")
        if isinstance(total, bool) or not isinstance(total, int) or total != len(items):
            return None, "admin_lifecycle_truncated"
        for raw in items:
            item = _object(raw, "admin_lifecycle_invalid")
            subject = _object(item.get("subject"), "admin_lifecycle_invalid")
            if subject.get("operation_id") != operation_id:
                raise BenchmarkError("admin_lifecycle_identity_mismatch")
        if items and all(
            isinstance(item, dict)
            and isinstance(item.get("rollup"), dict)
            and item["rollup"].get("terminal") is True
            for item in items
        ):
            return data, None
        if time.monotonic() >= deadline:
            return data, "admin_lifecycle_not_converged"
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))


def _admin_evidence(
    client: Any,
    cookie: str,
    operation_id: str,
    model_id: str,
    convergence_seconds: float,
) -> AdminEvidence:
    detail, detail_error = _run_detail(
        client, cookie, operation_id, model_id, convergence_seconds
    )
    lifecycle, lifecycle_error = _lifecycle_detail(
        client, cookie, operation_id, convergence_seconds
    )
    return AdminEvidence(detail, detail_error, lifecycle, lifecycle_error)


def _phase_map(detail: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if detail is None:
        return {}
    phases: dict[str, dict[str, Any]] = {}
    for raw in _list(detail.get("lifecycle_phases"), "admin_lifecycle_phase_invalid"):
        item = _object(raw, "admin_lifecycle_phase_invalid")
        name = item.get("phase")
        if not isinstance(name, str) or name in phases:
            raise BenchmarkError("admin_lifecycle_phase_invalid")
        phases[name] = _normalize_measurement(
            item.get("duration"), "scientific-controller-events"
        )
    return phases


def _phase_measurement(
    phases: dict[str, dict[str, Any]], aliases: tuple[str, ...]
) -> dict[str, Any]:
    found = [phases[name] for name in aliases if name in phases]
    if not found:
        return _unavailable(
            "scientific-admin",
            f"No dedicated {'/'.join(aliases)} lifecycle phase is exposed.",
        )
    available = [item for item in found if item["value"] is not None]
    if not available:
        return found[0]
    if len(available) == 1:
        return available[0]
    if any(item["evidence"] != "measured" for item in available):
        return _unavailable(
            "scientific-admin",
            f"The {'/'.join(aliases)} phases cannot be combined as exact measurements.",
        )
    return _measured(
        sum(float(item["value"]) for item in available),
        "scientific-controller-events",
    )


def _gpu_attempts(detail: dict[str, Any] | None) -> list[dict[str, Any]]:
    if detail is None:
        return []
    attempts: list[dict[str, Any]] = []
    for raw_stage in _list(detail.get("stages"), "admin_stages_invalid"):
        stage = _object(raw_stage, "admin_stage_invalid")
        if stage.get("resource_class") != "gpu":
            continue
        stage_id = stage.get("id")
        for raw_attempt in _list(stage.get("attempts"), "admin_attempts_invalid"):
            attempt = _object(raw_attempt, "admin_attempt_invalid")
            attempts.append(
                {
                    "stage_id": stage_id,
                    "attempt_id": attempt.get("id"),
                    "attempt_number": attempt.get("number"),
                    "status": attempt.get("status"),
                    "admitted_at": attempt.get("admitted_at"),
                    "started_at": attempt.get("started_at"),
                    "completed_at": attempt.get("completed_at"),
                    "gpu_count": attempt.get("gpu_count"),
                    "resolved_pool_id": attempt.get("resolved_pool_id"),
                    "admitted_resource_flavor": attempt.get("admitted_resource_flavor"),
                    "accelerator_resource_name": attempt.get("accelerator_resource_name"),
                }
            )
    return attempts


def _placement(
    detail: dict[str, Any] | None,
    public_row: dict[str, Any],
    pools: dict[str, dict[str, Any]],
    reserved_pool_ids: frozenset[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    attempts = _gpu_attempts(detail)
    pool_ids = sorted(
        {
            attempt["resolved_pool_id"]
            for attempt in attempts
            if isinstance(attempt.get("resolved_pool_id"), str)
        }
    )
    resolved = [pools[pool_id] for pool_id in pool_ids if pool_id in pools]
    missing = sorted(set(pool_ids) - set(pools))
    capacity_types = sorted({item["capacity_type"] for item in resolved})
    if missing:
        classification = "unresolved"
    elif capacity_types == ["preemptible"]:
        classification = "preemptible"
    elif capacity_types == ["regular"] and pool_ids and set(pool_ids).issubset(reserved_pool_ids):
        classification = "reserved"
    elif capacity_types == ["regular"]:
        classification = "regular"
    elif len(capacity_types) > 1:
        classification = "mixed"
    else:
        runtime = _object(
            _object(
                _object(public_row.get("api_measurements"), "public_measurements_invalid").get("cold_start"),
                "public_cold_start_invalid",
            ).get("runtime"),
            "public_runtime_invalid",
        )
        classification = "preemptible" if runtime.get("preemptible") is True else "unresolved"
    return (
        {
            "classification": classification,
            "pool_ids": pool_ids,
            "capacity_types": capacity_types,
            "environment_bindings": resolved,
            "unmatched_pool_ids": missing,
            "evidence": "kueue-admission" if pool_ids else "public-runtime-only",
        },
        attempts,
    )


def _rollup_measurement(
    value: float | None,
    *,
    quality: str,
    exact: bool,
    reason: str | None,
) -> dict[str, Any]:
    if value is None:
        return {
            **_unavailable("lifecycle-ledger", reason or "No lifecycle rollup is available.", unit="gpu-seconds"),
            "exact": False,
        }
    return {
        "value": round(value, 6),
        "unit": "gpu-seconds",
        "evidence": quality,
        "source": "lifecycle-ledger",
        "reason": reason,
        "exact": exact,
    }


def _lifecycle_accounting(
    document: dict[str, Any] | None,
    error: str | None,
) -> dict[str, Any]:
    empty = {
        "available": False,
        "exact": False,
        "rollup_count": 0,
        "reconciled": False,
        "quality": "unavailable",
        "data_gaps": [error or "lifecycle-rollup-unavailable"],
        "scheduler_occupied_gpu_seconds": _rollup_measurement(
            None, quality="unavailable", exact=False, reason=error
        ),
        "device_allocated_gpu_seconds": _rollup_measurement(
            None, quality="unavailable", exact=False, reason=error
        ),
        "active_gpu_seconds": _rollup_measurement(
            None, quality="unavailable", exact=False, reason=error
        ),
        "occupied_idle_gpu_seconds": _rollup_measurement(
            None, quality="unavailable", exact=False, reason=error
        ),
        "phase_gpu_seconds": {},
    }
    if document is None:
        return empty
    items = _list(document.get("items"), "admin_lifecycle_invalid")
    rollups: list[dict[str, Any]] = []
    for raw in items:
        item = _object(raw, "admin_lifecycle_invalid")
        rollup = item.get("rollup")
        if isinstance(rollup, dict):
            rollups.append(rollup)
    if not rollups:
        return empty
    required = (
        "scheduler_occupied_gpu_seconds",
        "device_allocated_gpu_seconds",
        "active_gpu_seconds",
        "occupied_idle_gpu_seconds",
    )
    values: dict[str, float] = {field: 0.0 for field in required}
    phases: dict[str, float] = {}
    gaps: set[str] = set()
    qualities: set[str] = set()
    terminal = True
    reconciled = True
    for rollup in rollups:
        terminal = terminal and rollup.get("terminal") is True
        reconciled = reconciled and rollup.get("reconciled") is True
        quality = rollup.get("quality")
        if quality not in {"measured", "application_observed", "estimated", "unavailable"}:
            raise BenchmarkError("admin_lifecycle_quality_invalid")
        qualities.add(quality)
        raw_gaps = _list(rollup.get("data_gaps"), "admin_lifecycle_gaps_invalid")
        if not all(isinstance(value, str) and value for value in raw_gaps):
            raise BenchmarkError("admin_lifecycle_gaps_invalid")
        gaps.update(raw_gaps)
        for field in required:
            value = rollup.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise BenchmarkError("admin_lifecycle_measurement_invalid")
            values[field] += float(value)
        raw_phases = _object(rollup.get("phase_gpu_seconds"), "admin_lifecycle_phases_invalid")
        for phase, value in raw_phases.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise BenchmarkError("admin_lifecycle_phases_invalid")
            phases[phase] = phases.get(phase, 0.0) + float(value)
    quality_order = {"measured": 0, "application_observed": 1, "estimated": 2, "unavailable": 3}
    quality = max(qualities, key=quality_order.__getitem__)
    if error is not None:
        gaps.add(error)
    exact = (
        error is None
        and terminal
        and reconciled
        and not gaps
        and quality == "measured"
    )
    reason = None if exact else "Lifecycle values are preserved but the aggregate is not fully measured and reconciled."
    return {
        "available": True,
        "exact": exact,
        "rollup_count": len(rollups),
        "reconciled": reconciled,
        "quality": quality,
        "data_gaps": sorted(gaps),
        **{
            field: _rollup_measurement(
                values[field], quality=quality, exact=exact, reason=reason
            )
            for field in required
        },
        "phase_gpu_seconds": {
            key: round(value, 6) for key, value in sorted(phases.items())
        },
    }


def _attempt(
    *,
    repetition: int,
    public_row: dict[str, Any],
    public_aggregate_path: Path,
    run_directory: Path,
    admin: AdminEvidence,
    model_snapshot: dict[str, Any],
    pools: dict[str, dict[str, Any]],
    reserved_pool_ids: frozenset[str],
) -> dict[str, Any]:
    model_id = public_row.get("model_id")
    if model_id not in EXPECTED_MODELS:
        raise BenchmarkError("public_model_unexpected")
    relative_aggregate = public_aggregate_path.relative_to(run_directory).as_posix()
    if public_row.get("status") != "succeeded":
        return {
            "repetition": repetition,
            "model_id": model_id,
            "status": "failed",
            "error_code": public_row.get("error_code") or "fleet_acceptance_failed",
            "fleet_aggregate": {"path": relative_aggregate, "sha256": _file_digest(public_aggregate_path)},
            "operation_identity": None,
            "execution_identity": None,
            "semantic_validation": "not-passed",
            "placement": None,
            "cache": {"model_readiness": model_snapshot.get("caching"), "run_observation": None},
            "gpu_attempts": [],
            "measurements": {
                metric: _unavailable("fleet-acceptance", "The public scientific run did not succeed.")
                for metric in STATISTIC_METRICS
            },
            "lifecycle_accounting": _lifecycle_accounting(None, "public-run-failed"),
            "admin_sources": {"run_detail": None, "run_detail_error": None, "lifecycle_error": None},
        }
    operation = _object(public_row.get("operation_identity"), "public_operation_invalid")
    operation_id = operation.get("operation_id")
    try:
        UUID(str(operation_id))
    except ValueError:
        raise BenchmarkError("public_operation_invalid") from None
    measurements = _object(public_row.get("api_measurements"), "public_measurements_invalid")
    runtime_projection = _object(measurements.get("runtime"), "public_runtime_projection_invalid")
    timestamps = _object(runtime_projection.get("timestamps"), "public_timestamps_invalid")
    cold_start = _object(measurements.get("cold_start"), "public_cold_start_invalid")
    phases = _phase_map(admin.detail)
    phase_measurements = {
        metric: _phase_measurement(phases, aliases)
        for metric, aliases in PHASE_METRICS.items()
    }
    cold_value = cold_start.get("cold_start_seconds")
    api_cold = (
        _measured(float(cold_value), "public-operation")
        if isinstance(cold_value, (int, float)) and not isinstance(cold_value, bool)
        else _unavailable("public-operation", "The operation did not expose a cold-start clock.")
    )
    first_semantic = _elapsed(
        timestamps.get("accepted_at"),
        timestamps.get("result_completed_at"),
        "public-operation-timestamps",
    )
    total_runtime = _elapsed(
        timestamps.get("started_at"),
        timestamps.get("completed_at"),
        "public-operation-timestamps",
    )
    placement, gpu_attempts = _placement(
        admin.detail, public_row, pools, reserved_pool_ids
    )
    run_observation = None
    admin_run = None
    if admin.detail is not None:
        admin_run = _object(admin.detail.get("run"), "admin_run_detail_invalid")
        run_observation = admin_run.get("fast_start")
    semantic = _object(public_row.get("terminal_state"), "public_terminal_invalid").get(
        "semantic_validation"
    )
    if semantic != "passed":
        raise BenchmarkError("semantic_validation_not_passed")
    return {
        "repetition": repetition,
        "model_id": model_id,
        "status": "succeeded",
        "error_code": None,
        "fleet_aggregate": {"path": relative_aggregate, "sha256": _file_digest(public_aggregate_path)},
        "operation_identity": operation,
        "execution_identity": public_row.get("execution_identity"),
        "semantic_validation": semantic,
        "placement": placement,
        "cache": {
            "model_readiness": model_snapshot.get("caching"),
            "run_observation": run_observation,
            "environment_cache_tiers": sorted(
                {
                    item["cache_tier"]
                    for item in placement["environment_bindings"]
                }
            ),
        },
        "gpu_attempts": gpu_attempts,
        "measurements": {
            **phase_measurements,
            "api_cold_start_seconds": api_cold,
            "time_to_first_semantic_result_seconds": first_semantic,
            "total_runtime_seconds": total_runtime,
        },
        "lifecycle_accounting": _lifecycle_accounting(
            admin.lifecycle, admin.lifecycle_error
        ),
        "admin_sources": {
            "run_detail": "available" if admin.detail is not None else None,
            "run_detail_error": admin.detail_error,
            "lifecycle": "available" if admin.lifecycle is not None else None,
            "lifecycle_error": admin.lifecycle_error,
        },
    }


def _cohort_identity(attempt: dict[str, Any]) -> dict[str, Any]:
    execution = _object(attempt.get("execution_identity"), "execution_identity_invalid")
    placement = _object(attempt.get("placement"), "placement_invalid")
    cache = _object(attempt.get("cache"), "cache_invalid")
    run_observation = cache.get("run_observation")
    return {
        "execution_identity_sha256": execution.get("execution_identity_sha256"),
        "runtime_image_digest": execution.get("runtime_image_digest"),
        "pool_ids": placement.get("pool_ids"),
        "capacity_types": placement.get("capacity_types"),
        "environment_cache_tiers": cache.get("environment_cache_tiers"),
        "run_fast_start_tier": run_observation.get("tier") if isinstance(run_observation, dict) else None,
        "run_fast_start_evidence": (
            run_observation.get("evidence") if isinstance(run_observation, dict) else "unavailable"
        ),
    }


def _cohorts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for attempt in attempts:
        if attempt["status"] != "succeeded":
            continue
        identity = _cohort_identity(attempt)
        key = _digest(identity)
        grouped.setdefault(key, (identity, []))[1].append(attempt)
    cohorts: list[dict[str, Any]] = []
    for key, (identity, items) in sorted(grouped.items()):
        statistics: dict[str, Any] = {}
        for metric in STATISTIC_METRICS:
            values = [
                float(value)
                for item in items
                if (
                    (value := item["measurements"][metric]["value"]) is not None
                    and item["measurements"][metric]["evidence"] == "measured"
                )
            ]
            statistics[metric] = {
                "statistics": FAST_AGGREGATE._statistics(values),
                "measured_samples": len(values),
                "unavailable_or_estimated_samples": len(items) - len(values),
            }
        cohorts.append(
            {
                "cohort_sha256": key,
                "identity": identity,
                "attempt_repetitions": [item["repetition"] for item in items],
                "semantic_successes": len(items),
                "exploratory_minimum_met": len(items) >= FAST_AGGREGATE.MINIMUM_EXPLORATORY_ATTEMPTS,
                "statistics": statistics,
            }
        )
    return cohorts


def _model_results(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_model: dict[str, list[dict[str, Any]]] = {
        model_id: [] for model_id in EXPECTED_MODELS
    }
    for attempt in attempts:
        by_model[attempt["model_id"]].append(attempt)
    return [
        {
            "model_id": model_id,
            "attempts": sorted(by_model[model_id], key=lambda item: item["repetition"]),
            "successes": sum(item["status"] == "succeeded" for item in by_model[model_id]),
            "failures": sum(item["status"] != "succeeded" for item in by_model[model_id]),
            "cohorts": _cohorts(by_model[model_id]),
        }
        for model_id in sorted(by_model)
    ]


def _validate_fleet_aggregate(value: dict[str, Any]) -> list[dict[str, Any]]:
    if value.get("schema") != FLEET.AGGREGATE_SCHEMA:
        raise BenchmarkError("fleet_aggregate_schema_invalid")
    rows = [_object(item, "fleet_model_invalid") for item in _list(value.get("models"), "fleet_models_invalid")]
    ids = [item.get("model_id") for item in rows]
    if len(rows) != len(EXPECTED_MODELS) or set(ids) != EXPECTED_MODELS or len(set(ids)) != len(ids):
        raise BenchmarkError("fleet_model_set_incomplete")
    return sorted(rows, key=lambda item: str(item["model_id"]))


def run_benchmark(config: BenchmarkConfig) -> tuple[dict[str, Any], Path]:
    repository_root, run_directory, endpoint = _validate_config(config)
    bindings, environment_digest = _environment_bindings(config)
    pool_map = _pool_bindings(bindings)
    admin_token = os.environ[config.admin_token_environment]
    try:
        client = PUBLIC.PublicApiClient(
            config.endpoint, admin_token, timeout_seconds=config.request_timeout_seconds
        )
    except PUBLIC.AcceptanceError as error:
        raise BenchmarkError("admin_client_invalid") from error
    cookie = _open_admin_session(client)
    attempts: list[dict[str, Any]] = []
    try:
        model_snapshot, before_digest = _model_snapshot(client, cookie)
        for repetition in range(1, config.repetitions + 1):
            fleet_run_id = f"{config.run_id}-r{repetition:02d}"
            try:
                result = FLEET.run_fleet(
                    FLEET.FleetConfig(
                        endpoint=config.endpoint,
                        repository_root=repository_root,
                        receipt_root=run_directory / "fleet",
                        run_id=fleet_run_id,
                        token_environment=config.inference_token_environment,
                        max_parallel=config.max_parallel,
                        timeout_seconds=config.timeout_seconds,
                        poll_seconds=config.poll_seconds,
                        request_timeout_seconds=config.request_timeout_seconds,
                    )
                )
            except FLEET.FleetAcceptanceError as error:
                raise BenchmarkError("fleet_runner_failed") from error
            rows = _validate_fleet_aggregate(result.aggregate)
            successful = [row for row in rows if row.get("status") == "succeeded"]
            evidence: dict[str, AdminEvidence] = {}
            with ThreadPoolExecutor(
                max_workers=min(config.max_parallel, max(1, len(successful))),
                thread_name_prefix="fs2-scientific-admin",
            ) as executor:
                futures = {
                    executor.submit(
                        _admin_evidence,
                        client,
                        cookie,
                        str(_object(row["operation_identity"], "public_operation_invalid")["operation_id"]),
                        str(row["model_id"]),
                        config.admin_convergence_seconds,
                    ): str(row["model_id"])
                    for row in successful
                }
                for future in as_completed(futures):
                    try:
                        evidence[futures[future]] = future.result()
                    except BenchmarkError as error:
                        evidence[futures[future]] = AdminEvidence(
                            None, error.code, None, error.code
                        )
            for row in rows:
                model_id = str(row["model_id"])
                attempts.append(
                    _attempt(
                        repetition=repetition,
                        public_row=row,
                        public_aggregate_path=result.aggregate_path,
                        run_directory=run_directory,
                        admin=evidence.get(
                            model_id,
                            AdminEvidence(None, None, None, None),
                        ),
                        model_snapshot=model_snapshot[model_id],
                        pools=pool_map,
                        reserved_pool_ids=frozenset(config.reserved_pool_ids),
                    )
                )
        _, after_digest = _model_snapshot(client, cookie)
    finally:
        try:
            _admin_json(
                client,
                cookie,
                "DELETE",
                "/admin/api/v1/session",
                expected_status=204,
            )
        except BenchmarkError:
            pass
    source_commit = _source_commit(repository_root)
    succeeded = sum(item["status"] == "succeeded" for item in attempts)
    exact_lifecycle = sum(item["lifecycle_accounting"]["exact"] for item in attempts)
    receipt = {
        "schema": SCHEMA,
        "run_id": config.run_id,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source_commit": source_commit,
        "endpoint": endpoint,
        "deployment": {
            "project_id": config.project_id,
            "region": config.region,
            "cluster_context": config.cluster_context,
        },
        "workload_contract": {
            "model_ids": sorted(EXPECTED_MODELS),
            "repetitions": config.repetitions,
            "max_parallel": config.max_parallel,
            "semantic_path": "public-scientific-batch-v1",
            "first_semantic_result_kind": "terminal-validated-response",
            "environment_qualifications_sha256": environment_digest,
            "environment_bindings": bindings,
        },
        "summary": {
            "attempts": len(attempts),
            "succeeded": succeeded,
            "failed": len(attempts) - succeeded,
            "models_succeeded_every_repetition": sum(
                all(item["status"] == "succeeded" for item in model["attempts"])
                for model in _model_results(attempts)
            ),
            "exact_lifecycle_attempts": exact_lifecycle,
            "desired_state_unchanged": before_digest == after_digest,
        },
        "desired_state": {
            "mutation_performed": False,
            "before_model_snapshot_sha256": before_digest,
            "after_model_snapshot_sha256": after_digest,
            "unchanged": before_digest == after_digest,
            "benchmark_operations_terminal": all(
                item["status"] in {"succeeded", "failed"} for item in attempts
            ),
        },
        "models": _model_results(attempts),
    }
    try:
        FLEET._assert_redacted(receipt)
    except FLEET.FleetAcceptanceError as error:
        raise BenchmarkError("receipt_redaction_failed") from error
    output = run_directory / "benchmark.json"
    try:
        FLEET._write_atomic(output, _canonical(receipt, newline=True), overwrite=False)
    except FLEET.FleetAcceptanceError as error:
        raise BenchmarkError("receipt_write_failed") from error
    return receipt, output


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument(
        "--repository-root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--environment-qualifications", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--cluster-context", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--max-parallel", type=int, default=8)
    parser.add_argument("--inference-token-env", default="FS2_INFERENCE_TOKEN")
    parser.add_argument("--admin-token-env", default="FS2_ADMIN_TOKEN")
    parser.add_argument("--timeout-seconds", type=float, default=7200.0)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--admin-convergence-seconds", type=float, default=30.0)
    parser.add_argument("--reserved-pool-id", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    config = BenchmarkConfig(
        endpoint=arguments.endpoint,
        repository_root=arguments.repository_root,
        receipt_root=arguments.receipt_root,
        run_id=arguments.run_id,
        environment_qualifications=arguments.environment_qualifications,
        project_id=arguments.project_id,
        region=arguments.region,
        cluster_context=arguments.cluster_context,
        repetitions=arguments.repetitions,
        max_parallel=arguments.max_parallel,
        inference_token_environment=arguments.inference_token_env,
        admin_token_environment=arguments.admin_token_env,
        timeout_seconds=arguments.timeout_seconds,
        poll_seconds=arguments.poll_seconds,
        request_timeout_seconds=arguments.request_timeout_seconds,
        admin_convergence_seconds=arguments.admin_convergence_seconds,
        reserved_pool_ids=tuple(arguments.reserved_pool_id),
    )
    try:
        receipt, output = run_benchmark(config)
    except BenchmarkError as error:
        print(f"scientific cold-start benchmark failed: {error.code}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "receipt": str(output),
                "status": "succeeded" if receipt["summary"]["failed"] == 0 else "failed",
                "summary": receipt["summary"],
            },
            sort_keys=True,
        )
    )
    if receipt["summary"]["failed"] or not receipt["desired_state"]["unchanged"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

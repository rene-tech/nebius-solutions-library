#!/usr/bin/env python3
"""Aggregate and validate truthful FS2 model fast-start benchmark evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "fast-start-benchmark-receipt.schema.json"
RECEIPT_SCHEMA = "fs2-serve.nebius.ai/fast-start-benchmark-receipt/v1"
ATTEMPT_SCHEMA = "fs2-serve.nebius.ai/fast-start-benchmark-attempt/v1"
MINIMUM_EXPLORATORY_ATTEMPTS = 3
MINIMUM_QUALIFICATION_ATTEMPTS = 20
PERFORMANCE_TARGETS: dict[str, float | None] = {
    "Off": None,
    "L1": 300.0,
    "L2": 120.0,
    "L3": 60.0,
    "L4": 30.0,
}
METRIC_NAMES = (
    "capacity_wait",
    "gpu_capacity_available_to_ready",
    "activation_to_ready",
    "request_to_first_byte",
    "request_to_first_semantic_output",
    "request_completion",
    "activation_to_first_semantic_output",
)
_DURATION_TOLERANCE_SECONDS = 0.25
PROMOTION_REQUIRED_TUPLE_FIELDS = (
    "runtime_argv_digest",
    "runtime_environment_digest",
    "runtime_template_digest",
    "gpu_product",
    "gpu_compute_capability",
    "gpu_memory_bytes",
    "driver_version",
    "cuda_version",
    "storage_class",
    "storage_mode",
    "semantic_validator_digest",
)


class FastStartEvidenceError(ValueError):
    """Raised when evidence is invalid or cannot be compared honestly."""


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise FastStartEvidenceError(f"invalid JSON evidence: {path}") from error


def _schema() -> dict[str, Any]:
    value = load_json(SCHEMA_PATH)
    if not isinstance(value, dict):
        raise FastStartEvidenceError("fast-start schema is not an object")
    Draft202012Validator.check_schema(value)
    return value


def _attempt_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": schema["$defs"],
        **schema["$defs"]["attempt"],
    }


def _schema_errors(value: Any, schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(
            validator.iter_errors(value), key=lambda item: list(item.absolute_path)
        )
    ]


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except (TypeError, ValueError):
        raise FastStartEvidenceError(f"invalid UTC timestamp: {value!r}") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise FastStartEvidenceError(f"timestamp is not UTC: {value!r}")
    return parsed


def _assert_time_order(timestamps: dict[str, str | None], names: Sequence[str]) -> None:
    prior_name: str | None = None
    prior_value: datetime | None = None
    for name in names:
        raw = timestamps[name]
        if raw is None:
            continue
        current = _timestamp(raw)
        if prior_value is not None and current < prior_value:
            raise FastStartEvidenceError(
                f"timestamp order invalid: {name} precedes {prior_name}"
            )
        prior_name = name
        prior_value = current


def _elapsed(timestamps: dict[str, str | None], start: str, end: str) -> float | None:
    start_value = timestamps[start]
    end_value = timestamps[end]
    if start_value is None or end_value is None:
        return None
    return (_timestamp(end_value) - _timestamp(start_value)).total_seconds()


def _assert_duration(
    durations: dict[str, float | None],
    timestamps: dict[str, str | None],
    duration_name: str,
    start_name: str,
    end_name: str,
) -> None:
    actual = durations[duration_name]
    expected = _elapsed(timestamps, start_name, end_name)
    if actual is None or expected is None:
        if actual is not None and expected is None:
            raise FastStartEvidenceError(
                f"{duration_name} is present without both source timestamps"
            )
        return
    tolerance = max(_DURATION_TOLERANCE_SECONDS, expected * 0.01)
    if not math.isclose(actual, expected, abs_tol=tolerance):
        raise FastStartEvidenceError(
            f"{duration_name}={actual} differs from timestamp interval {expected}"
        )


def validate_attempt(attempt: dict[str, Any]) -> None:
    schema = _schema()
    errors = _schema_errors(attempt, _attempt_schema(schema))
    if errors:
        raise FastStartEvidenceError(
            "attempt schema validation failed: " + "; ".join(errors)
        )

    compatibility_tuple = attempt["compatibility_tuple"]
    if attempt["compatibility_tuple_digest"] != canonical_digest(compatibility_tuple):
        raise FastStartEvidenceError("attempt compatibility tuple digest mismatch")
    if not compatibility_tuple["runtime_image_ref"].endswith(
        "@" + compatibility_tuple["runtime_image_digest"]
    ):
        raise FastStartEvidenceError("runtime image reference and digest differ")

    timestamps = attempt["timestamps"]
    durations = attempt["durations_seconds"]
    _assert_time_order(
        timestamps,
        (
            "activation_accepted",
            "gpu_capacity_requested",
            "gpu_capacity_available",
            "endpoint_ready",
        ),
    )
    _assert_time_order(
        timestamps,
        (
            "activation_accepted",
            "request_started",
            "first_response_byte",
            "first_semantic_output",
            "request_completed",
            "return_to_floor",
        ),
    )
    if timestamps["endpoint_ready"] is not None:
        endpoint_ready = _timestamp(timestamps["endpoint_ready"])
        for name in (
            "first_response_byte",
            "first_semantic_output",
            "request_completed",
        ):
            if (
                timestamps[name] is not None
                and _timestamp(timestamps[name]) < endpoint_ready
            ):
                raise FastStartEvidenceError(f"{name} precedes endpoint readiness")
    _assert_duration(
        durations,
        timestamps,
        "capacity_wait",
        "activation_accepted",
        "gpu_capacity_available",
    )
    _assert_duration(
        durations,
        timestamps,
        "gpu_capacity_available_to_ready",
        "gpu_capacity_available",
        "endpoint_ready",
    )
    _assert_duration(
        durations,
        timestamps,
        "activation_to_ready",
        "activation_accepted",
        "endpoint_ready",
    )
    _assert_duration(
        durations,
        timestamps,
        "request_to_first_byte",
        "request_started",
        "first_response_byte",
    )
    _assert_duration(
        durations,
        timestamps,
        "request_to_first_semantic_output",
        "request_started",
        "first_semantic_output",
    )
    _assert_duration(
        durations,
        timestamps,
        "request_completion",
        "request_started",
        "request_completed",
    )
    _assert_duration(
        durations,
        timestamps,
        "activation_to_first_semantic_output",
        "activation_accepted",
        "first_semantic_output",
    )

    prepared = compatibility_tuple["capacity_state"] == "prepared-node-zero-pod"
    if prepared:
        if timestamps["gpu_capacity_requested"] is not None:
            raise FastStartEvidenceError(
                "prepared-node attempt must not report a capacity request"
            )
        if durations["capacity_wait"] != 0:
            raise FastStartEvidenceError(
                "prepared-node attempt must report zero capacity wait"
            )
    elif (
        compatibility_tuple["capacity_state"]
        in {
            "fresh-node-zero-pod",
            "preemption-replacement",
        }
        and timestamps["gpu_capacity_requested"] is None
    ):
        raise FastStartEvidenceError(
            "fresh or replacement capacity attempt is missing capacity request time"
        )

    inference = attempt["inference"]
    if attempt["status"] == "PASS":
        required_timestamps = (
            "gpu_capacity_available",
            "endpoint_ready",
            "request_started",
            "first_semantic_output",
            "request_completed",
        )
        required_durations = tuple(
            name for name in METRIC_NAMES if name != "request_to_first_byte"
        )
        if attempt["failure_code"] is not None:
            raise FastStartEvidenceError("passing attempt has a failure code")
        if any(timestamps[name] is None for name in required_timestamps):
            raise FastStartEvidenceError(
                "passing attempt has an incomplete timestamp set"
            )
        if any(durations[name] is None for name in required_durations):
            raise FastStartEvidenceError(
                "passing attempt has an incomplete duration set"
            )
        if not inference["valid_output"]:
            raise FastStartEvidenceError("passing attempt did not validate its output")
        if (
            inference["http_status"] is None
            or not 200 <= inference["http_status"] < 300
        ):
            raise FastStartEvidenceError(
                "passing attempt has a non-success HTTP status"
            )
        if inference["first_output_kind"] == "none":
            raise FastStartEvidenceError("passing attempt has no semantic first output")
        if inference["output_units"] is None or inference["throughput"] is None:
            raise FastStartEvidenceError("passing attempt is missing output throughput")
        if attempt["artifacts"]["semantic_output_sha256"] is None:
            raise FastStartEvidenceError(
                "passing attempt lacks semantic output evidence"
            )
    else:
        if attempt["failure_code"] is None:
            raise FastStartEvidenceError("failed attempt is missing a failure code")
        if inference["valid_output"]:
            raise FastStartEvidenceError("failed attempt claims a valid output")


def _rounded(value: float) -> float:
    return round(float(value), 6)


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _statistics(values: Sequence[float]) -> dict[str, float | int] | None:
    if not values:
        return None
    median = statistics.median(values)
    return {
        "sample_count": len(values),
        "minimum": _rounded(min(values)),
        "maximum": _rounded(max(values)),
        "mean": _rounded(statistics.fmean(values)),
        "p50": _rounded(_nearest_rank(values, 0.50)),
        "p95": _rounded(_nearest_rank(values, 0.95)),
        "median_absolute_deviation": _rounded(
            statistics.median(abs(value - median) for value in values)
        ),
    }


def _observed_level(p95_seconds: float | None) -> str:
    if p95_seconds is None:
        return "Off"
    for level in ("L4", "L3", "L2", "L1"):
        target = PERFORMANCE_TARGETS[level]
        assert target is not None
        if p95_seconds <= target:
            return level
    return "Off"


def _qualification(
    requested_level: str,
    success_count: int,
    failure_count: int,
    model_start_p95: float | None,
    compatibility_tuple_complete: bool,
) -> dict[str, Any]:
    target = PERFORMANCE_TARGETS[requested_level]
    observed_level = _observed_level(model_start_p95)
    failure_free = failure_count == 0
    p95_eligible = (
        success_count >= MINIMUM_QUALIFICATION_ATTEMPTS
        and failure_free
        and compatibility_tuple_complete
    )
    target_met = (
        None
        if target is None
        else (model_start_p95 is not None and model_start_p95 <= target)
    )

    if requested_level == "Off":
        state = "disabled"
        reasons = ["no-cold-start-target"]
    elif not failure_free:
        state = "failed-attempts"
        reasons = ["failed-attempts-present"]
        if success_count < MINIMUM_QUALIFICATION_ATTEMPTS:
            reasons.append("minimum-successful-attempts-not-met")
    elif success_count < MINIMUM_EXPLORATORY_ATTEMPTS:
        state = "insufficient-evidence"
        reasons = [
            "minimum-exploratory-attempts-not-met",
            "minimum-successful-attempts-not-met",
        ]
        if not compatibility_tuple_complete:
            reasons.append("incomplete-compatibility-tuple")
    elif success_count < MINIMUM_QUALIFICATION_ATTEMPTS:
        state = "exploratory"
        reasons = ["minimum-successful-attempts-not-met"]
        if not compatibility_tuple_complete:
            reasons.append("incomplete-compatibility-tuple")
    elif not compatibility_tuple_complete:
        state = "incomplete-evidence"
        reasons = ["incomplete-compatibility-tuple"]
    elif target_met:
        state = "qualified"
        reasons = ["target-met"]
    else:
        state = "target-missed"
        reasons = ["target-p95-exceeded"]

    qualified_level = (
        observed_level
        if p95_eligible and observed_level in {"L1", "L2", "L3", "L4"}
        else None
    )
    return {
        "state": state,
        "minimum_exploratory_attempts": MINIMUM_EXPLORATORY_ATTEMPTS,
        "minimum_successful_attempts": MINIMUM_QUALIFICATION_ATTEMPTS,
        "comparable_successful_attempts": success_count,
        "failure_free": failure_free,
        "compatibility_tuple_complete": compatibility_tuple_complete,
        "target_seconds": target,
        "target_met": target_met,
        "observed_level": observed_level,
        "qualified_level": qualified_level,
        "p95_eligible_for_qualification": p95_eligible,
        "reasons": reasons,
    }


def build_receipt(
    attempts: Iterable[dict[str, Any]], *, generated_at: str | None = None
) -> dict[str, Any]:
    ordered = sorted(
        (deepcopy(item) for item in attempts), key=lambda item: item["ordinal"]
    )
    if not ordered:
        raise FastStartEvidenceError("at least one attempt is required")
    for attempt in ordered:
        validate_attempt(attempt)

    ordinals = [attempt["ordinal"] for attempt in ordered]
    if ordinals != list(range(1, len(ordered) + 1)):
        raise FastStartEvidenceError(
            "attempt ordinals must be contiguous and start at one"
        )
    ids = [attempt["attempt_id"] for attempt in ordered]
    if len(ids) != len(set(ids)):
        raise FastStartEvidenceError("attempt IDs must be unique")
    raw_digests = [attempt["artifacts"]["raw_attempt_sha256"] for attempt in ordered]
    if len(raw_digests) != len(set(raw_digests)):
        raise FastStartEvidenceError("raw attempt artifact digests must be unique")

    tuple_digest = ordered[0]["compatibility_tuple_digest"]
    compatibility_tuple = ordered[0]["compatibility_tuple"]
    requested_level = ordered[0]["requested_level"]
    for attempt in ordered[1:]:
        if attempt["compatibility_tuple_digest"] != tuple_digest:
            raise FastStartEvidenceError(
                "attempt compatibility tuples are not comparable"
            )
        if attempt["compatibility_tuple"] != compatibility_tuple:
            raise FastStartEvidenceError("attempt compatibility tuple values differ")
        if attempt["requested_level"] != requested_level:
            raise FastStartEvidenceError("attempt requested levels differ")

    def inference_contract(item: dict[str, Any]) -> tuple[Any, ...]:
        inference = item["inference"]
        return (
            inference["modality"],
            inference["first_output_kind"],
            inference["request_count"],
            inference["warmup_count"],
            inference["concurrency"],
            None
            if inference["input_units"] is None
            else inference["input_units"]["unit"],
            None
            if inference["output_units"] is None
            else inference["output_units"]["unit"],
            None
            if inference["throughput"] is None
            else inference["throughput"]["unit"],
        )

    successful_contracts = {
        inference_contract(item) for item in ordered if item["status"] == "PASS"
    }
    if len(successful_contracts) > 1:
        raise FastStartEvidenceError("successful attempt inference contracts differ")

    successful = [attempt for attempt in ordered if attempt["status"] == "PASS"]
    failed = [attempt for attempt in ordered if attempt["status"] == "FAIL"]
    metrics = {
        name: _statistics(
            [
                float(attempt["durations_seconds"][name])
                for attempt in successful
                if attempt["durations_seconds"][name] is not None
            ]
        )
        for name in METRIC_NAMES
    }

    throughput_units = {
        attempt["inference"]["throughput"]["unit"] for attempt in successful
    }
    if len(throughput_units) > 1:
        raise FastStartEvidenceError("successful attempt throughput units differ")
    throughput = None
    if throughput_units:
        throughput = {
            "unit": next(iter(throughput_units)),
            "statistics": _statistics(
                [
                    float(attempt["inference"]["throughput"]["value"])
                    for attempt in successful
                ]
            ),
        }

    model_start = metrics["gpu_capacity_available_to_ready"]
    model_start_p95 = None if model_start is None else float(model_start["p95"])
    failure_codes = Counter(attempt["failure_code"] for attempt in failed)
    compatibility_tuple_complete = all(
        compatibility_tuple[field] is not None
        for field in PROMOTION_REQUIRED_TUPLE_FIELDS
    )
    if generated_at is None:
        generated_at = (
            datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
    _timestamp(generated_at)

    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "receipt_digest": "0" * 64,
        "generated_at": generated_at,
        "status": "PASS" if not failed else "FAIL",
        "performance_class": {
            "requested_level": requested_level,
            "clock_boundary": "gpu-capacity-available-to-endpoint-ready",
            "capacity_wait_reported_separately": True,
            "end_to_end_reported_separately": True,
            "targets_seconds": PERFORMANCE_TARGETS,
        },
        "compatibility_tuple": compatibility_tuple,
        "compatibility_tuple_digest": tuple_digest,
        "attempts": ordered,
        "aggregates": {
            "attempt_count": len(ordered),
            "success_count": len(successful),
            "failure_count": len(failed),
            "failure_codes": dict(sorted(failure_codes.items())),
            "percentile_estimator": "nearest-rank-successful-attempts",
            "metrics_seconds": metrics,
            "throughput": throughput,
        },
        "qualification": _qualification(
            requested_level,
            len(successful),
            len(failed),
            model_start_p95,
            compatibility_tuple_complete,
        ),
    }
    unsigned = dict(receipt)
    unsigned.pop("receipt_digest")
    receipt["receipt_digest"] = canonical_digest(unsigned)
    return receipt


def validate_receipt(receipt: dict[str, Any]) -> None:
    schema = _schema()
    errors = _schema_errors(receipt, schema)
    if errors:
        raise FastStartEvidenceError(
            "receipt schema validation failed: " + "; ".join(errors)
        )
    claimed_digest = receipt["receipt_digest"]
    unsigned = dict(receipt)
    unsigned.pop("receipt_digest")
    if claimed_digest != canonical_digest(unsigned):
        raise FastStartEvidenceError("receipt digest mismatch")
    expected = build_receipt(receipt["attempts"], generated_at=receipt["generated_at"])
    if receipt != expected:
        raise FastStartEvidenceError(
            "receipt derived aggregates or qualification differ"
        )


def _write_new(path: Path, value: dict[str, Any]) -> None:
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
    except OSError as error:
        raise FastStartEvidenceError(
            f"output already exists or is unavailable: {path}"
        ) from error
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)


def _attempt_paths(arguments: argparse.Namespace) -> list[Path]:
    paths = list(arguments.attempt or [])
    if arguments.attempt_directory is not None:
        paths.extend(sorted(arguments.attempt_directory.glob("attempt-*.json")))
    if not paths:
        raise FastStartEvidenceError("no attempt files selected")
    if len(paths) != len(set(paths)):
        raise FastStartEvidenceError("attempt file selected more than once")
    return paths


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    aggregate = subparsers.add_parser("aggregate", help="aggregate raw attempt JSON")
    aggregate.add_argument("--attempt", type=Path, action="append")
    aggregate.add_argument("--attempt-directory", type=Path)
    aggregate.add_argument("--generated-at")
    aggregate.add_argument("--output", type=Path, required=True)

    validate = subparsers.add_parser("validate", help="validate one aggregate receipt")
    validate.add_argument("--receipt", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        if arguments.command == "validate":
            value = load_json(arguments.receipt)
            if not isinstance(value, dict):
                raise FastStartEvidenceError("receipt is not an object")
            validate_receipt(value)
            print("fast-start receipt: valid")
            return 0

        attempts = [load_json(path) for path in _attempt_paths(arguments)]
        if any(not isinstance(attempt, dict) for attempt in attempts):
            raise FastStartEvidenceError("every attempt must be a JSON object")
        receipt = build_receipt(attempts, generated_at=arguments.generated_at)
        validate_receipt(receipt)
        _write_new(arguments.output, receipt)
        summary = {
            "status": receipt["status"],
            "model_id": receipt["compatibility_tuple"]["model_id"],
            "attempts": receipt["aggregates"]["attempt_count"],
            "qualification": receipt["qualification"]["state"],
            "observed_level": receipt["qualification"]["observed_level"],
            "qualified_level": receipt["qualification"]["qualified_level"],
        }
        print(json.dumps(summary, sort_keys=True))
        return 0
    except FastStartEvidenceError as error:
        print(f"fast-start evidence invalid: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

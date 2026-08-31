#!/usr/bin/env python3
"""Validate a post-acceptance cold-start receipt and its FS2 invariants."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "post-acceptance-benchmark-receipt.schema.json"
CONTRACT_PATH = ROOT / "post-acceptance-benchmark-contract.json"
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
    "checkpoint-restore-start",
    "checkpoint-restore-end",
    "readiness-accepted",
    "semantic-call1-accepted",
    "semantic-call2-accepted",
    "return-to-zero-accepted",
)
CAPACITY_TO_EXISTING_COHORT = {
    "prepared-node-zero-pod": "prepared-node",
    "fresh-node-zero-pod": "new-node",
    "preemption-replacement": "new-node",
}
FLOAT_TOLERANCE_SECONDS = 0.001
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


class ReceiptValidationError(ValueError):
    """The receipt is structurally valid JSON but violates an FS2 invariant."""


def _reject_duplicate_keys(items: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in items:
        if key in value:
            raise ReceiptValidationError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _duration(start: str, end: str) -> float:
    return (_utc(end) - _utc(start)).total_seconds()


def _canonical_digest(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _ranked_percentile(
    successful_durations: Sequence[float], attempt_count: int, quantile: float
) -> float | None:
    rank = math.ceil(quantile * attempt_count)
    ranked = sorted(successful_durations)
    return None if rank > len(ranked) else ranked[rank - 1]


def _mad(successful_durations: Sequence[float]) -> float | None:
    if not successful_durations:
        return None
    median = statistics.median(successful_durations)
    return float(statistics.median(abs(item - median) for item in successful_durations))


def _same_number(left: Any, right: float | None) -> bool:
    if right is None:
        return left is None
    return isinstance(left, (int, float)) and not isinstance(left, bool) and math.isclose(
        float(left), right, rel_tol=0.0, abs_tol=1e-9
    )


def _validate_events(attempt: Mapping[str, Any]) -> None:
    events = attempt["events"]
    names = [event["name"] for event in events]
    if names != list(EVENT_NAMES):
        raise ReceiptValidationError(
            f"attempt {attempt['attempt_id']} events must use the canonical ordered event set"
        )
    for event in events:
        timestamp = event["timestamp"]
        if (event["state"] == "observed") != (timestamp is not None):
            raise ReceiptValidationError(
                f"attempt {attempt['attempt_id']} event {event['name']} has inconsistent state/timestamp"
            )
    by_name = {event["name"]: event for event in events}
    required = {
        "activation-accepted",
        "pod-scheduled",
        "runtime-process-start",
        "readiness-accepted",
        "return-to-zero-accepted",
    }
    if attempt["capacity_state"] in {"fresh-node-zero-pod", "preemption-replacement"}:
        required.update(
            {"capacity-requested", "provider-instance-created", "node-ready"}
        )
    if attempt["status"] == "PASS":
        required.update({"semantic-call1-accepted", "semantic-call2-accepted"})
    missing = sorted(name for name in required if by_name[name]["state"] != "observed")
    if missing:
        raise ReceiptValidationError(
            f"attempt {attempt['attempt_id']} lacks required observed events: {', '.join(missing)}"
        )
    if by_name["activation-accepted"]["timestamp"] != attempt["t0_utc"]:
        raise ReceiptValidationError(
            f"attempt {attempt['attempt_id']} activation event differs from T0"
        )
    for event_name, field in (
        ("semantic-call1-accepted", "call1_utc"),
        ("semantic-call2-accepted", "call2_utc"),
    ):
        if by_name[event_name]["timestamp"] != attempt[field]:
            raise ReceiptValidationError(
                f"attempt {attempt['attempt_id']} {event_name} differs from {field}"
            )
    for earlier, later in ORDERED_EVENT_PAIRS:
        earlier_timestamp = by_name[earlier]["timestamp"]
        later_timestamp = by_name[later]["timestamp"]
        if (
            earlier_timestamp is not None
            and later_timestamp is not None
            and _utc(earlier_timestamp) > _utc(later_timestamp)
        ):
            raise ReceiptValidationError(
                f"attempt {attempt['attempt_id']} events are out of order: {earlier} then {later}"
            )


def _validate_attempt(attempt: Mapping[str, Any], deadline_seconds: float) -> None:
    attempt_id = attempt["attempt_id"]
    expected_cohort = CAPACITY_TO_EXISTING_COHORT[attempt["capacity_state"]]
    if attempt["existing_cohort"] != expected_cohort:
        raise ReceiptValidationError(
            f"attempt {attempt_id} maps {attempt['capacity_state']} to the wrong existing cohort"
        )
    if not math.isclose(
        _duration(attempt["t0_utc"], attempt["deadline_utc"]),
        deadline_seconds,
        rel_tol=0.0,
        abs_tol=FLOAT_TOLERANCE_SECONDS,
    ):
        raise ReceiptValidationError(f"attempt {attempt_id} deadline differs from the policy")

    _validate_events(attempt)
    status = attempt["status"]
    if status == "PASS":
        if attempt["failure_code"] is not None:
            raise ReceiptValidationError(f"passing attempt {attempt_id} has a failure code")
        for field in (
            "call1_utc",
            "call2_utc",
            "t0_to_call1_seconds",
            "t0_to_call2_seconds",
            "semantic_receipt_digest",
        ):
            if attempt[field] is None:
                raise ReceiptValidationError(f"passing attempt {attempt_id} lacks {field}")
    elif attempt["failure_code"] is None:
        raise ReceiptValidationError(f"failed attempt {attempt_id} lacks a failure code")

    previous = _utc(attempt["t0_utc"])
    for time_field, duration_field in (
        ("call1_utc", "t0_to_call1_seconds"),
        ("call2_utc", "t0_to_call2_seconds"),
    ):
        timestamp = attempt[time_field]
        duration = attempt[duration_field]
        if (timestamp is None) != (duration is None):
            raise ReceiptValidationError(
                f"attempt {attempt_id} must pair {time_field} with {duration_field}"
            )
        if timestamp is None:
            continue
        parsed = _utc(timestamp)
        if parsed < previous or parsed > _utc(attempt["deadline_utc"]):
            raise ReceiptValidationError(f"attempt {attempt_id} response timestamps are out of order")
        previous = parsed
        if not math.isclose(
            float(duration),
            _duration(attempt["t0_utc"], timestamp),
            rel_tol=0.0,
            abs_tol=FLOAT_TOLERANCE_SECONDS,
        ):
            raise ReceiptValidationError(
                f"attempt {attempt_id} claimed duration differs from its timestamps"
            )


def _validate_aggregate(
    arm: str, attempts: Sequence[Mapping[str, Any]], aggregate: Mapping[str, Any]
) -> None:
    successes = [attempt for attempt in attempts if attempt["status"] == "PASS"]
    expected_counts = (len(attempts), len(successes), len(attempts) - len(successes))
    observed_counts = (
        aggregate["attempt_count"],
        aggregate["success_count"],
        aggregate["failure_count"],
    )
    if observed_counts != expected_counts:
        raise ReceiptValidationError(f"{arm} aggregate counts do not reconcile")

    for call in ("call1", "call2"):
        durations = [float(item[f"t0_to_{call}_seconds"]) for item in successes]
        expected = {
            f"failure_ranked_p50_t0_to_{call}_seconds": _ranked_percentile(
                durations, len(attempts), 0.50
            ),
            f"failure_ranked_p95_t0_to_{call}_seconds": _ranked_percentile(
                durations, len(attempts), 0.95
            ),
            f"{call}_median_absolute_deviation_seconds": _mad(durations),
        }
        for field, value in expected.items():
            if not _same_number(aggregate[field], value):
                raise ReceiptValidationError(f"{arm} {field} does not match raw attempts")


def validate_receipt(value: Mapping[str, Any], schema: Mapping[str, Any] | None = None) -> None:
    schema = schema or load_json(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        paths = ["/".join(str(part) for part in error.absolute_path) or "<root>" for error in errors]
        raise ReceiptValidationError(
            "; ".join(f"{path}: {error.message}" for path, error in zip(paths, errors))
        )

    compatibility = value["compatibility_tuple"]
    if value["benchmark_contract_digest"] != _canonical_digest(load_json(CONTRACT_PATH)):
        raise ReceiptValidationError("benchmark contract digest differs from source contract")
    if _utc(value["observed_at"]) >= _utc(value["valid_until"]):
        raise ReceiptValidationError("receipt validity interval is empty or reversed")
    if len(compatibility["allocated_gpu_uuids"]) != compatibility["workload_gpu_count"]:
        raise ReceiptValidationError("allocated GPU UUID count differs from workload GPU count")
    if (compatibility["mig_mode"] == "disabled") != (compatibility["mig_profile"] is None):
        raise ReceiptValidationError("MIG mode and profile are inconsistent")
    if value["control"]["mechanism"] != "conventional":
        raise ReceiptValidationError("control mechanism must be conventional")
    if value["candidate"]["mechanism"] == "conventional":
        raise ReceiptValidationError("candidate mechanism must challenge conventional startup")

    attempts = value["attempts"]
    expected_order = ["control" if index % 2 == 0 else "candidate" for index in range(len(attempts))]
    if [item["arm"] for item in attempts] != expected_order:
        raise ReceiptValidationError("attempts must strictly alternate control then candidate")

    unique_fields = ("attempt_id", "raw_attempt_receipt_digest", "cleanup_receipt_digest")
    for field in unique_fields:
        observed = [item[field] for item in attempts]
        if len(observed) != len(set(observed)):
            raise ReceiptValidationError(f"attempt {field} values must be unique")

    policy = value["statistics_policy"]
    compatibility_digest = _canonical_digest(compatibility)
    for attempt in attempts:
        if attempt["compatibility_tuple_digest"] != compatibility_digest:
            raise ReceiptValidationError(
                "attempt compatibility tuple digest differs from receipt tuple"
            )
        _validate_attempt(attempt, float(policy["attempt_deadline_seconds"]))
        if attempt["capacity_state"] != compatibility["capacity_state"]:
            raise ReceiptValidationError("attempt capacity state differs from the compatibility tuple")

    grouped = {
        arm: [attempt for attempt in attempts if attempt["arm"] == arm]
        for arm in ("control", "candidate")
    }
    if len(grouped["control"]) != len(grouped["candidate"]):
        raise ReceiptValidationError("control and candidate attempt counts must match")
    if any(len(items) < policy["minimum_attempts_per_arm"] for items in grouped.values()):
        raise ReceiptValidationError("each arm is below the preregistered minimum attempt count")
    for arm, items in grouped.items():
        _validate_aggregate(arm, items, value["aggregates"][arm])

    control = value["aggregates"]["control"]
    candidate = value["aggregates"]["candidate"]
    control_p95 = control["failure_ranked_p95_t0_to_call1_seconds"]
    candidate_p95 = candidate["failure_ranked_p95_t0_to_call1_seconds"]
    absolute_pass = False
    relative_pass = False
    if control_p95 is not None and candidate_p95 is not None:
        improvement = float(control_p95) - float(candidate_p95)
        absolute_pass = improvement >= policy["minimum_absolute_p95_improvement_seconds"]
        relative_pass = improvement / float(control_p95) >= policy[
            "minimum_relative_p95_improvement_fraction"
        ]
    failure_pass = (
        candidate["failure_count"] / candidate["attempt_count"]
        <= control["failure_count"] / control["attempt_count"]
    )
    decision = value["decision"]
    if decision["absolute_effect_passed"] != absolute_pass:
        raise ReceiptValidationError("absolute effect decision differs from raw aggregates")
    if decision["relative_effect_passed"] != relative_pass:
        raise ReceiptValidationError("relative effect decision differs from raw aggregates")
    if decision["failure_rate_non_regression_passed"] != failure_pass:
        raise ReceiptValidationError("failure-rate decision differs from raw aggregates")

    call2_control = control["failure_ranked_p95_t0_to_call2_seconds"]
    call2_candidate = candidate["failure_ranked_p95_t0_to_call2_seconds"]
    call2_non_regression = (
        call2_control is not None
        and call2_candidate is not None
        and float(call2_candidate) <= float(call2_control)
    )
    if decision["call2_latency_non_regression_passed"] != call2_non_regression:
        raise ReceiptValidationError("call-2 latency decision differs from raw aggregates")
    accepted = all(
        (
            decision["semantic_equivalence_passed"],
            decision["failure_rate_non_regression_passed"],
            decision["absolute_effect_passed"],
            decision["relative_effect_passed"],
            decision["conventional_fallback_passed"],
            decision["preemption_replacement_passed"],
            decision["return_to_zero_passed"],
            call2_non_regression,
        )
    )
    if decision["accepted"] != accepted:
        raise ReceiptValidationError("accepted decision differs from the fail-closed gate")
    if (value["status"] == "PASS") != accepted:
        raise ReceiptValidationError("receipt status differs from the accepted decision")

    unsigned = dict(value)
    claimed_digest = unsigned.pop("receipt_digest")
    if claimed_digest != _canonical_digest(unsigned):
        raise ReceiptValidationError("receipt digest differs from canonical unsigned receipt")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--schema", type=Path, default=SCHEMA_PATH)
    args = parser.parse_args()
    value = load_json(args.receipt)
    schema = load_json(args.schema)
    validate_receipt(value, schema)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

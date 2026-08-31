#!/usr/bin/env python3
"""Seal source-qualified startup markers from one private Kubernetes log."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parent
FS2_ROOT = ROOT.parent.parent
CONTRACT_PATH = ROOT / "full-catalog-runtime-log-marker-contract.json"
MATRIX_PATH = ROOT / "cold-start-optimization-matrix.json"
ROUTES_PATH = (
    FS2_ROOT / "components/control-plane/contracts/all-models-live-services.json"
)
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_LOG_BYTES = 256 * 1024 * 1024
TIME = re.compile(
    r"^(?P<second>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?P<fraction>\.[0-9]{1,9})?Z$"
)
UID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
ATTEMPT = re.compile(
    r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?--[a-z0-9][a-z0-9-]*--r[0-9]{2}$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LogMarkerError(ValueError):
    """The raw log, runtime binding, or marker evidence is invalid."""


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )


def digest_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _private_bytes(path: Path, maximum: int, code: str) -> bytes:
    if not path.is_absolute():
        raise LogMarkerError(code + "_path_not_absolute")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise LogMarkerError(code + "_unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or not 1 <= metadata.st_size <= maximum
        ):
            raise LogMarkerError(code + "_custody_invalid")
        value = bytearray()
        while len(value) <= maximum:
            block = os.read(descriptor, min(8 * 1024 * 1024, maximum + 1 - len(value)))
            if not block:
                break
            value.extend(block)
    finally:
        os.close(descriptor)
    if not value or len(value) > maximum:
        raise LogMarkerError(code + "_size_invalid")
    return bytes(value)


def _private_json(path: Path, code: str) -> tuple[dict[str, Any], bytes]:
    raw = _private_bytes(path, MAX_JSON_BYTES, code)
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise LogMarkerError(code + "_json_invalid") from None
    if not isinstance(value, dict):
        raise LogMarkerError(code + "_shape_invalid")
    return value, raw


def _public_json(path: Path, code: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise LogMarkerError(code + "_unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise LogMarkerError(code + "_json_invalid") from None
    if not isinstance(value, dict):
        raise LogMarkerError(code + "_shape_invalid")
    return value


def _nanoseconds(value: str) -> int:
    match = TIME.fullmatch(value)
    if match is None:
        raise LogMarkerError("timestamp_invalid")
    try:
        second = datetime.fromisoformat(match.group("second") + "+00:00").astimezone(
            UTC
        )
    except ValueError:
        raise LogMarkerError("timestamp_invalid") from None
    fraction = (match.group("fraction") or ".0")[1:].ljust(9, "0")
    return int(second.timestamp()) * 1_000_000_000 + int(fraction)


def _identity(raw: dict[str, Any]) -> dict[str, Any]:
    identity = raw.get("runtime_identity_observation")
    if not isinstance(identity, dict):
        startup = raw.get("startup_observation")
        identity = (
            startup.get("identity_observation") if isinstance(startup, dict) else None
        )
    if not isinstance(identity, dict):
        raise LogMarkerError("acceptance_runtime_identity_missing")
    return identity


def _contract() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    contract = _public_json(CONTRACT_PATH, "marker_contract")
    matrix = _public_json(MATRIX_PATH, "matrix")
    routes = _public_json(ROUTES_PATH, "routes")
    if (
        contract.get("schema")
        != "fs2-serve.nebius.ai/full-catalog-runtime-log-marker-contract/v1"
        or contract.get("default") != "discovery-only-no-phase-admission"
    ):
        raise LogMarkerError("marker_contract_invalid")
    return contract, matrix, routes


def _events(raw_log: bytes, allowed_events: set[str]) -> dict[str, list[str]]:
    try:
        lines = raw_log.decode("utf-8").splitlines()
    except UnicodeError:
        raise LogMarkerError("raw_log_not_utf8") from None
    events = {name: [] for name in allowed_events}
    for line in lines:
        timestamp, separator, payload = line.partition(" ")
        if not separator or TIME.fullmatch(timestamp) is None:
            continue
        try:
            value = json.loads(payload)
        except (json.JSONDecodeError, RecursionError):
            continue
        if (
            isinstance(value, dict)
            and set(value) == {"event", "name"}
            and value.get("event") == "fs2-startup-phase"
            and value.get("name") in allowed_events
        ):
            events[value["name"]].append(timestamp)
    return events


def validate_receipt(value: Any) -> dict[str, Any]:
    """Validate one discovery receipt against the checked-in source contracts."""

    if not isinstance(value, dict) or set(value) != {
        "schema",
        "attempt_id",
        "model_id",
        "runtime",
        "attempt_boundary",
        "sources",
        "admitted_events",
        "gaps",
        "status",
        "captured_at",
        "receipt_digest",
    }:
        raise LogMarkerError("discovery_receipt_shape_invalid")
    if (
        value["schema"] != "fs2-serve.nebius.ai/full-catalog-runtime-log-discovery/v1"
        or ATTEMPT.fullmatch(value["attempt_id"]) is None
    ):
        raise LogMarkerError("discovery_receipt_subject_invalid")
    digest = value["receipt_digest"]
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise LogMarkerError("discovery_receipt_digest_invalid")
    unsigned = dict(value)
    del unsigned["receipt_digest"]
    if digest_value(unsigned) != digest:
        raise LogMarkerError("discovery_receipt_digest_mismatch")

    contract, matrix, routes = _contract()
    model_by_id = {
        item.get("model_id"): item
        for item in matrix.get("models", [])
        if isinstance(item, dict)
    }
    model = model_by_id.get(value["model_id"])
    route = routes.get("routes", {}).get(value["model_id"])
    if not isinstance(model, dict) or not isinstance(route, dict):
        raise LogMarkerError("discovery_receipt_model_invalid")
    runtime = value["runtime"]
    if not isinstance(runtime, dict) or set(runtime) != {
        "pod_uid",
        "container",
        "image_id",
    }:
        raise LogMarkerError("discovery_receipt_runtime_invalid")
    containers = set(model.get("primary_containers", [])) | set(
        model.get("artifact_init_containers", [])
    )
    expected_image = route.get("runtime_image_digest")
    if (
        not isinstance(runtime["pod_uid"], str)
        or UID.fullmatch(runtime["pod_uid"]) is None
        or runtime["container"] not in containers
        or not isinstance(runtime["image_id"], str)
        or not isinstance(expected_image, str)
        or not runtime["image_id"].endswith(expected_image)
    ):
        raise LogMarkerError("discovery_receipt_runtime_invalid")

    boundary = value["attempt_boundary"]
    if not isinstance(boundary, dict) or set(boundary) != {"t0", "t1"}:
        raise LogMarkerError("discovery_receipt_boundary_invalid")
    t0_ns = _nanoseconds(boundary["t0"])
    t1_ns = _nanoseconds(boundary["t1"])
    if t1_ns <= t0_ns:
        raise LogMarkerError("discovery_receipt_boundary_invalid")
    sources = value["sources"]
    if not isinstance(sources, dict) or set(sources) != {
        "acceptance_sha256",
        "raw_log_sha256",
        "raw_log_bytes",
        "marker_contract_digest",
        "matrix_digest",
        "routes_digest",
    }:
        raise LogMarkerError("discovery_receipt_sources_invalid")
    for field in (
        "acceptance_sha256",
        "raw_log_sha256",
        "marker_contract_digest",
        "matrix_digest",
        "routes_digest",
    ):
        if (
            not isinstance(sources[field], str)
            or SHA256.fullmatch(sources[field]) is None
        ):
            raise LogMarkerError("discovery_receipt_sources_invalid")
    if (
        sources["marker_contract_digest"] != digest_value(contract)
        or sources["matrix_digest"] != digest_value(matrix)
        or sources["routes_digest"] != digest_value(routes)
        or isinstance(sources["raw_log_bytes"], bool)
        or not isinstance(sources["raw_log_bytes"], int)
        or sources["raw_log_bytes"] < 1
    ):
        raise LogMarkerError("discovery_receipt_sources_invalid")

    model_contract = contract.get("models", {}).get(value["model_id"])
    rules = [] if model_contract is None else model_contract.get("rules", [])
    expected_events = [
        rule["event"]
        for rule in rules
        if isinstance(rule, dict) and rule.get("container") == runtime["container"]
    ]
    admitted = value["admitted_events"]
    if not isinstance(admitted, list):
        raise LogMarkerError("discovery_receipt_events_invalid")
    admitted_names: list[str] = []
    admitted_times: list[int] = []
    for item in admitted:
        if not isinstance(item, dict) or set(item) != {"name", "timestamp"}:
            raise LogMarkerError("discovery_receipt_events_invalid")
        if item["name"] not in expected_events or item["name"] in admitted_names:
            raise LogMarkerError("discovery_receipt_events_invalid")
        timestamp = _nanoseconds(item["timestamp"])
        if not t0_ns <= timestamp <= t1_ns:
            raise LogMarkerError("discovery_receipt_events_invalid")
        admitted_names.append(item["name"])
        admitted_times.append(timestamp)
    if admitted_names != [name for name in expected_events if name in admitted_names]:
        raise LogMarkerError("discovery_receipt_event_order_invalid")
    if admitted_times != sorted(admitted_times):
        raise LogMarkerError("discovery_receipt_event_order_invalid")

    gaps = value["gaps"]
    if not isinstance(gaps, list):
        raise LogMarkerError("discovery_receipt_gaps_invalid")
    allowed_reasons = {
        "no-pinned-runtime-event-contract",
        "marker-absent",
        "marker-ambiguous",
        "marker-outside-attempt-boundary",
        "admitted-markers-out-of-contract-order",
    }
    for item in gaps:
        if not isinstance(item, dict) or set(item) != {
            "event",
            "state",
            "reason",
            "candidate_count",
        }:
            raise LogMarkerError("discovery_receipt_gaps_invalid")
        if (
            item["state"] != "UNOBSERVED_INSTRUMENTATION_GAP"
            or item["reason"] not in allowed_reasons
            or item["event"] not in {*expected_events, None}
            or isinstance(item["candidate_count"], bool)
            or not isinstance(item["candidate_count"], int)
            or item["candidate_count"] < 0
        ):
            raise LogMarkerError("discovery_receipt_gaps_invalid")
    if expected_events:
        covered = set(admitted_names) | {
            item["event"] for item in gaps if item["event"] is not None
        }
        general_order_gap = any(
            item["reason"] == "admitted-markers-out-of-contract-order" for item in gaps
        )
        if covered != set(expected_events) and not general_order_gap:
            raise LogMarkerError("discovery_receipt_event_partition_invalid")
    elif gaps != [
        {
            "event": None,
            "state": "UNOBSERVED_INSTRUMENTATION_GAP",
            "reason": "no-pinned-runtime-event-contract",
            "candidate_count": 0,
        }
    ]:
        raise LogMarkerError("discovery_receipt_unpinned_gap_invalid")
    expected_status = (
        "PASS" if expected_events == admitted_names and not gaps else "INCOMPLETE"
    )
    if value["status"] != expected_status:
        raise LogMarkerError("discovery_receipt_status_invalid")
    _nanoseconds(value["captured_at"])
    return value


def build_receipt(args: argparse.Namespace) -> dict[str, Any]:
    if (
        ATTEMPT.fullmatch(args.attempt_id) is None
        or UID.fullmatch(args.pod_uid) is None
    ):
        raise LogMarkerError("attempt_or_pod_identity_invalid")
    acceptance, acceptance_raw = _private_json(
        args.acceptance_receipt.resolve(), "acceptance_receipt"
    )
    if (
        acceptance.get("result") != "PASS"
        or acceptance.get("model_id") != args.model_id
    ):
        raise LogMarkerError("acceptance_subject_invalid")
    acceptance_timestamps = acceptance.get("phase_timestamps")
    if (
        not isinstance(acceptance_timestamps, dict)
        or args.attempt_t0 != acceptance_timestamps.get("activation_accepted_at")
        or args.attempt_t1 != acceptance_timestamps.get("semantic_call1_accepted_at")
    ):
        raise LogMarkerError("acceptance_attempt_boundary_mismatch")
    identity = _identity(acceptance)
    pod = identity.get("pod")
    if not isinstance(pod, dict) or pod.get("uid") != args.pod_uid:
        raise LogMarkerError("acceptance_pod_identity_mismatch")
    images = identity.get("container_image_ids")
    if not isinstance(images, list):
        raise LogMarkerError("acceptance_container_images_missing")
    image_matches = [
        item.get("image_id")
        for item in images
        if isinstance(item, dict) and item.get("name") == args.container
    ]
    if image_matches != [args.image_id]:
        raise LogMarkerError("acceptance_container_image_mismatch")

    contract, matrix, routes = _contract()
    matrix_models = {
        item["model_id"]: item
        for item in matrix.get("models", [])
        if isinstance(item, dict)
    }
    model = matrix_models.get(args.model_id)
    route = routes.get("routes", {}).get(args.model_id)
    if not isinstance(model, dict) or not isinstance(route, dict):
        raise LogMarkerError("catalog_model_unknown")
    containers = set(model.get("primary_containers", [])) | set(
        model.get("artifact_init_containers", [])
    )
    if args.container not in containers:
        raise LogMarkerError("container_not_identity_source")
    expected_image = route.get("runtime_image_digest")
    if not isinstance(expected_image, str) or not args.image_id.endswith(
        expected_image
    ):
        raise LogMarkerError("runtime_image_digest_mismatch")
    model_contract = contract.get("models", {}).get(args.model_id)
    rules: list[dict[str, str]] = []
    if model_contract is not None:
        if (
            not isinstance(model_contract, dict)
            or model_contract.get("runtime_image_digest") != expected_image
            or model_contract.get("parser")
            != "exact-json-event-after-kubernetes-rfc3339-prefix"
            or not isinstance(model_contract.get("rules"), list)
        ):
            raise LogMarkerError("model_marker_contract_invalid")
        rules = [
            rule
            for rule in model_contract["rules"]
            if isinstance(rule, dict) and rule.get("container") == args.container
        ]
    event_names = [rule.get("event") for rule in rules]
    if any(not isinstance(name, str) for name in event_names) or len(
        event_names
    ) != len(set(event_names)):
        raise LogMarkerError("model_marker_rule_invalid")

    raw_log = _private_bytes(args.raw_log.resolve(), MAX_LOG_BYTES, "raw_log")
    candidates = _events(raw_log, set(event_names))
    t0_ns = _nanoseconds(args.attempt_t0)
    t1_ns = _nanoseconds(args.attempt_t1)
    if t1_ns <= t0_ns:
        raise LogMarkerError("attempt_boundary_invalid")
    admitted: list[dict[str, str]] = []
    gaps: list[dict[str, Any]] = []
    if not rules:
        gaps.append(
            {
                "event": None,
                "state": "UNOBSERVED_INSTRUMENTATION_GAP",
                "reason": "no-pinned-runtime-event-contract",
                "candidate_count": 0,
            }
        )
    for name in event_names:
        timestamps = candidates[name]
        in_boundary = [
            timestamp
            for timestamp in timestamps
            if t0_ns <= _nanoseconds(timestamp) <= t1_ns
        ]
        if len(timestamps) == 1 and len(in_boundary) == 1:
            admitted.append({"name": name, "timestamp": in_boundary[0]})
        else:
            reason = "marker-absent"
            if len(timestamps) > 1:
                reason = "marker-ambiguous"
            elif timestamps:
                reason = "marker-outside-attempt-boundary"
            gaps.append(
                {
                    "event": name,
                    "state": "UNOBSERVED_INSTRUMENTATION_GAP",
                    "reason": reason,
                    "candidate_count": len(timestamps),
                }
            )
    ordered = sorted(admitted, key=lambda item: _nanoseconds(item["timestamp"]))
    if ordered != admitted:
        gaps.append(
            {
                "event": None,
                "state": "UNOBSERVED_INSTRUMENTATION_GAP",
                "reason": "admitted-markers-out-of-contract-order",
                "candidate_count": len(admitted),
            }
        )
        admitted = []
    receipt: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/full-catalog-runtime-log-discovery/v1",
        "attempt_id": args.attempt_id,
        "model_id": args.model_id,
        "runtime": {
            "pod_uid": args.pod_uid,
            "container": args.container,
            "image_id": args.image_id,
        },
        "attempt_boundary": {"t0": args.attempt_t0, "t1": args.attempt_t1},
        "sources": {
            "acceptance_sha256": hashlib.sha256(acceptance_raw).hexdigest(),
            "raw_log_sha256": hashlib.sha256(raw_log).hexdigest(),
            "raw_log_bytes": len(raw_log),
            "marker_contract_digest": digest_value(contract),
            "matrix_digest": digest_value(matrix),
            "routes_digest": digest_value(routes),
        },
        "admitted_events": admitted,
        "gaps": gaps,
        "status": "PASS" if admitted and not gaps else "INCOMPLETE",
        "captured_at": datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    }
    receipt["receipt_digest"] = digest_value(receipt)
    validate_receipt(receipt)
    return receipt


def write_new(path: Path, value: Any) -> None:
    if not path.is_absolute():
        raise LogMarkerError("output_path_not_absolute")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise LogMarkerError("output_create_failed") from None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise LogMarkerError("output_write_failed") from None


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--pod-uid", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--attempt-t0", required=True)
    parser.add_argument("--attempt-t1", required=True)
    parser.add_argument("--acceptance-receipt", type=Path, required=True)
    parser.add_argument("--raw-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(arguments)


def main() -> int:
    args = parse_args()
    try:
        value = build_receipt(args)
        write_new(args.output.resolve(), value)
    except LogMarkerError:
        return 2
    return 0 if value["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

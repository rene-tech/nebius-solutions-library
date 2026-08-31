#!/usr/bin/env python3
"""Aggregate source-bound runtime log discovery into an exact gap matrix.

This tool is read-only with respect to Kubernetes. It consumes the private
plan and private per-container discovery receipts produced from
``kubectl logs --timestamps`` and emits a new mode-0600 receipt. Missing or
unqualified markers remain instrumentation gaps; they are never estimates.
"""

from __future__ import annotations

import argparse
import importlib.util
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parent
BASELINE_PATH = ROOT / "full_catalog_baseline.py"
MARKER_PATH = ROOT / "capture_runtime_log_markers.py"


class GapMatrixError(ValueError):
    """A plan, discovery receipt, or exact partition was invalid."""


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise GapMatrixError("validator_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except BaseException:
        raise GapMatrixError("validator_module_unavailable") from None
    return module


BASELINE = _module("fs2_full_catalog_baseline_for_log_gaps", BASELINE_PATH)
MARKERS = _module("fs2_runtime_log_markers_for_gaps", MARKER_PATH)


def _attempt_cell(plan: Mapping[str, Any], attempt_id: str) -> dict[str, Any]:
    matches = [
        cell
        for cell in plan["cells"]
        if attempt_id in cell.get("expected_attempt_ids", [])
    ]
    if len(matches) != 1 or matches[0].get("admission") != "admitted":
        raise GapMatrixError("discovery_attempt_not_admitted")
    return matches[0]


def build_gap_matrix(args: argparse.Namespace) -> dict[str, Any]:
    plan_path = args.plan.resolve()
    try:
        plan = BASELINE.validate_plan(BASELINE.load_json(plan_path, private=True))
        contract, matrix, routes = MARKERS._contract()
    except BaseException:
        raise GapMatrixError("source_contract_invalid") from None
    matrix_models = {
        item["model_id"]: item for item in matrix["models"] if isinstance(item, dict)
    }
    route_ids = set(routes["routes"])
    if (
        set(matrix_models) != route_ids
        or len(route_ids) != matrix["catalog_contract"]["canonical_model_count"]
    ):
        raise GapMatrixError("full_catalog_partition_invalid")
    backends = {
        item["model_id"]: item
        for item in plan["backends"]
        if item["resource_class"] == "gpu"
    }
    if set(backends) != route_ids:
        raise GapMatrixError("plan_gpu_route_partition_invalid")

    receipts_by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_bindings: set[tuple[str, str, str, str]] = set()
    raw_receipt_bindings: list[dict[str, Any]] = []
    for path in args.discovery_receipt:
        resolved = path.resolve()
        try:
            receipt = MARKERS.validate_receipt(
                BASELINE.load_json(resolved, private=True)
            )
        except BaseException:
            raise GapMatrixError("discovery_receipt_invalid") from None
        cell = _attempt_cell(plan, receipt["attempt_id"])
        if cell["model_id"] != receipt["model_id"]:
            raise GapMatrixError("discovery_receipt_plan_subject_mismatch")
        runtime = receipt["runtime"]
        binding = (
            receipt["attempt_id"],
            runtime["pod_uid"],
            runtime["container"],
            runtime["image_id"],
        )
        if binding in seen_bindings:
            raise GapMatrixError("discovery_runtime_binding_duplicate")
        seen_bindings.add(binding)
        receipts_by_model[receipt["model_id"]].append(receipt)
        raw_receipt_bindings.append(
            {
                "model_id": receipt["model_id"],
                "attempt_id": receipt["attempt_id"],
                "pod_uid": runtime["pod_uid"],
                "container": runtime["container"],
                "image_id": runtime["image_id"],
                "sha256": BASELINE.file_digest(resolved),
                "bytes": resolved.stat().st_size,
                "receipt_digest": receipt["receipt_digest"],
            }
        )

    rows: list[dict[str, Any]] = []
    for model_id in sorted(route_ids):
        backend = backends[model_id]
        matrix_model = matrix_models[model_id]
        expected_containers = sorted(
            set(matrix_model.get("primary_containers", []))
            | set(matrix_model.get("artifact_init_containers", []))
        )
        model_receipts = sorted(
            receipts_by_model.get(model_id, []),
            key=lambda item: (
                item["attempt_id"],
                item["runtime"]["pod_uid"],
                item["runtime"]["container"],
            ),
        )
        captured_containers = sorted(
            {item["runtime"]["container"] for item in model_receipts}
        )
        missing_containers = sorted(set(expected_containers) - set(captured_containers))
        model_contract = contract.get("models", {}).get(model_id)
        rules = [] if model_contract is None else model_contract.get("rules", [])
        pinned_events = [
            {"container": rule["container"], "event": rule["event"]} for rule in rules
        ]
        identity_kind = backend["identity_admission"]["kind"]
        if identity_kind == "blocked" and model_receipts:
            raise GapMatrixError("blocked_route_has_discovery_receipt")
        gaps: list[dict[str, Any]] = []
        if identity_kind != "blocked":
            for container in missing_containers:
                gaps.append(
                    {
                        "attempt_id": None,
                        "pod_uid": None,
                        "container": container,
                        "event": None,
                        "state": "UNOBSERVED_INSTRUMENTATION_GAP",
                        "reason": "raw-container-log-not-captured",
                        "candidate_count": 0,
                    }
                )
            for receipt in model_receipts:
                for gap in receipt["gaps"]:
                    gaps.append(
                        {
                            "attempt_id": receipt["attempt_id"],
                            "pod_uid": receipt["runtime"]["pod_uid"],
                            "container": receipt["runtime"]["container"],
                            **gap,
                        }
                    )
            if model_contract is None and not any(
                item["reason"] == "no-pinned-runtime-event-contract" for item in gaps
            ):
                gaps.append(
                    {
                        "attempt_id": None,
                        "pod_uid": None,
                        "container": None,
                        "event": None,
                        "state": "UNOBSERVED_INSTRUMENTATION_GAP",
                        "reason": "no-pinned-runtime-event-contract",
                        "candidate_count": 0,
                    }
                )
        admitted_events = [
            {
                "attempt_id": receipt["attempt_id"],
                "pod_uid": receipt["runtime"]["pod_uid"],
                "container": receipt["runtime"]["container"],
                **event,
            }
            for receipt in model_receipts
            for event in receipt["admitted_events"]
        ]
        if identity_kind == "blocked":
            status = "BLOCKED_EXACT_CONTENT_IDENTITY"
        elif gaps:
            status = "INCOMPLETE"
        else:
            status = "COMPLETE"
        rows.append(
            {
                "model_id": model_id,
                "backend_id": backend["backend_id"],
                "identity_admission": identity_kind,
                "expected_containers": expected_containers,
                "captured_containers": captured_containers,
                "pinned_event_contract": pinned_events,
                "admitted_events": admitted_events,
                "gaps": gaps,
                "status": status,
            }
        )

    statuses = [row["status"] for row in rows]
    packet: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/runtime-instrumentation-gap-matrix/v1",
        "plan_digest": plan["plan_digest"],
        "source": plan["source"],
        "contract_digests": {
            "marker_contract": MARKERS.digest_value(contract),
            "matrix": MARKERS.digest_value(matrix),
            "routes": MARKERS.digest_value(routes),
        },
        "summary": {
            "route_count": len(rows),
            "complete_route_count": statuses.count("COMPLETE"),
            "incomplete_route_count": statuses.count("INCOMPLETE"),
            "blocked_identity_route_count": statuses.count(
                "BLOCKED_EXACT_CONTENT_IDENTITY"
            ),
            "discovery_receipt_count": len(raw_receipt_bindings),
            "admitted_event_count": sum(len(row["admitted_events"]) for row in rows),
            "instrumentation_gap_count": sum(len(row["gaps"]) for row in rows),
        },
        "rows": rows,
        "raw_discovery_receipts": sorted(
            raw_receipt_bindings,
            key=lambda item: (
                item["model_id"],
                item["attempt_id"],
                item["pod_uid"],
                item["container"],
            ),
        ),
        "status": "COMPLETE"
        if all(item == "COMPLETE" for item in statuses)
        else "INCOMPLETE",
        "generated_at": datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    }
    packet["packet_digest"] = BASELINE.canonical_digest(packet)
    return packet


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--discovery-receipt", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(arguments)


def main() -> int:
    args = parse_args()
    try:
        value = build_gap_matrix(args)
        BASELINE.write_json_new(args.output.resolve(), value)
    except (GapMatrixError, BASELINE.BaselineError):
        return 2
    return 0 if value["status"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())

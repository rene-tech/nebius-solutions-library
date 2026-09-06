#!/usr/bin/env python3
"""Exercise varied scientific inputs, concurrent shards and service classes.

Scenarios extend the model-owned accepted fixtures; uploads, invocation,
semantic validation and receipt redaction reuse the public acceptance client.
No Kubernetes or administrative credentials are required.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import run_acceptance as public
import run_fleet_acceptance as fleet

SCHEMA = "fs2-serve.nebius.ai/scientific-scenario-acceptance/v1"


def verify_download(client: public.PublicApiClient, pointer: dict[str, Any]) -> bytes:
    """Prove that the customer can retrieve the bytes named by a result."""
    response = client.request("GET", f"/v1/artifacts/{pointer['artifact_id']}/content")
    if response.status != 200:
        raise public.AcceptanceError(f"http_artifact_content_{response.status}")
    if (
        len(response.body) != pointer["size_bytes"]
        or hashlib.sha256(response.body).hexdigest() != pointer["sha256"]
        or response.headers.get("x-fs2-artifact-sha256") != pointer["sha256"]
    ):
        raise public.AcceptanceError("downloaded_artifact_identity_mismatch")
    return response.body


def verify_result_downloads(
    client: public.PublicApiClient, result: dict[str, Any]
) -> list[dict[str, Any]]:
    manifest_pointer = result["output_manifest"]
    manifest = json.loads(verify_download(client, manifest_pointer))
    candidates = [
        entry["artifact"]
        for entry in manifest["entries"]
        if 0 < entry["artifact"]["size_bytes"] <= public.MAX_JSON_BYTES
    ]
    if not candidates:
        raise public.AcceptanceError("no_bounded_downloadable_result")
    # One actual scientific output plus its manifest verifies delivery.
    # Full model-specific semantic validation remains server-owned.
    selected = min(candidates, key=lambda item: item["size_bytes"])
    verify_download(client, selected)
    return [manifest_pointer, selected]


def prepare_scenario(
    config: public.RunConfig, scenario: dict[str, Any]
) -> tuple[str, dict[str, Any], list[public.DeclaredInput], dict[str, Any]]:
    """Replace declared JSON artifacts and recalculate the entire digest chain."""
    model_id, request, declarations, fragment = public._activation(config)
    if scenario["model_id"] != model_id:
        raise public.AcceptanceError("scenario_model_mismatch")
    request["parameters"].update(copy.deepcopy(scenario.get("parameters", {})))
    request["service_class"] = scenario.get("service_class", request["service_class"])
    replacements = scenario.get("artifact_json", {})
    known = {item.name for item in declarations if item.role == "manifest-artifact"}
    if set(replacements) - known:
        raise public.AcceptanceError("scenario_artifact_unknown")
    if replacements:
        root = next(
            item for item in declarations if item.role == "request-input-manifest"
        )
        manifest = json.loads(root.data)
        changed: dict[str | None, public.DeclaredInput] = {}
        for entry in manifest["entries"]:
            name = entry["name"]
            if name not in replacements:
                continue
            declared = next(item for item in declarations if item.name == name)
            data = public._canonical_json(replacements[name], newline=True)
            entry["artifact"].update(
                sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)
            )
            changed[name] = replace(declared, data=data)
        manifest_bytes = public._canonical_json(
            manifest, newline=root.encoding == "canonical-json-newline"
        )
        request["input_manifest"].update(
            sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            size_bytes=len(manifest_bytes),
        )
        declarations = [
            replace(item, data=manifest_bytes)
            if item.role == "request-input-manifest"
            else changed.get(item.name, item)
            for item in declarations
        ]
    return model_id, request, declarations, fragment


def run_scenario(
    config: public.RunConfig, scenario: dict[str, Any], token: str
) -> dict[str, Any]:
    client = public.PublicApiClient(config.endpoint, token)
    model_id, request, declarations, fragment = prepare_scenario(config, scenario)
    description = {
        "id": scenario["id"],
        "model_id": model_id,
        "service_class": request["service_class"],
        "parameters": request["parameters"],
        "input_sha256": request["input_manifest"]["sha256"],
    }
    operation_id: str | None = None
    started = time.monotonic()
    try:
        time.sleep(scenario.get("delay_seconds", 0))
        request, uploads = public._prepare_input(
            client,
            model_id=model_id,
            request=request,
            declarations=declarations,
            run_id=config.run_id,
        )
        pointer = public._artifact_ref(
            request["input_manifest"], "scenario_pointer_invalid"
        )
        operation_id, initial = public._submit(
            client, model_id=model_id, request=request, run_id=config.run_id
        )
        public._write_receipt(
            config.receipt_path.with_suffix(".submitted.json"),
            {**description, "operation_id": operation_id},
            overwrite=False,
        )
        # Replaying precisely the same request must not allocate another job.
        replay_id, replay = public._submit(
            client, model_id=model_id, request=request, run_id=config.run_id
        )
        if replay_id != operation_id or replay["operation"]["reused"] is not True:
            raise public.AcceptanceError("idempotent_replay_changed_operation")
        if scenario.get("cancel", False):
            public._json_response(
                client.request("DELETE", f"/v1/operations/{operation_id}"),
                202,
                "cancel",
            )
            deadline = time.monotonic() + min(config.timeout_seconds, 180)
            while True:
                status = public._json_response(
                    client.request("GET", f"/v1/operations/{operation_id}"),
                    200,
                    "cancel_status",
                )
                if status["batch"]["status"] in public.TERMINAL:
                    if status["batch"]["status"] != "cancelled":
                        raise public.AcceptanceError("cancellation_terminal_mismatch")
                    if any(
                        not attempt["resource_released"]
                        for stage in status["batch"]["stages"]
                        for attempt in stage["attempts"]
                    ):
                        raise public.AcceptanceError("cancelled_resource_not_released")
                    receipt = {
                        **description,
                        "operation_id": operation_id,
                        "status": "cancelled",
                        "resources_released": True,
                    }
                    public._write_receipt(config.receipt_path, receipt, overwrite=False)
                    return {
                        **receipt,
                        "outcome": "passed",
                        "wall_seconds": time.monotonic() - started,
                    }
                if time.monotonic() >= deadline:
                    raise public.AcceptanceError("cancellation_timeout")
                time.sleep(config.poll_seconds)
        terminal = public._poll(
            client,
            operation_id=operation_id,
            model_id=model_id,
            operation=request["operation"],
            initial=initial,
            timeout_seconds=config.timeout_seconds,
            poll_seconds=config.poll_seconds,
        )
        result = public._json_response(
            client.request("GET", f"/v1/operations/{operation_id}/result"),
            200,
            "result",
        )
        public._validate_result(
            result,
            status=terminal,
            operation_id=operation_id,
            model_id=model_id,
            input_pointer=pointer,
            fragment=fragment,
        )
        for stage_id, expected_count in scenario.get("expected_shards", {}).items():
            completed = {
                attempt["shard_id"]
                for attempt in result["attempts"]
                if attempt["stage_id"] == stage_id and attempt["status"] == "succeeded"
            }
            if len(completed) != expected_count:
                raise public.AcceptanceError("scenario_shard_count_mismatch")
        receipt = public._receipt(
            client=client,
            model_id=model_id,
            status=terminal,
            result=result,
            uploads=uploads,
        )
        downloaded = verify_result_downloads(client, result)
        public._write_receipt(config.receipt_path, receipt, overwrite=False)
        return {
            **description,
            "operation_id": operation_id,
            "outcome": "passed",
            "idempotent_replay": True,
            "wall_seconds": round(time.monotonic() - started, 3),
            "receipt_sha256": hashlib.sha256(
                config.receipt_path.read_bytes()
            ).hexdigest(),
            "attempts": receipt["attempts"],
            "queue": receipt["queue"],
            "execution_identity": receipt["execution_identity"],
            "downloaded_artifacts": downloaded,
        }
    except public.AcceptanceError as error:
        if operation_id is None and error.code == scenario.get("expected_error_code"):
            receipt = {
                **description,
                "outcome": "passed",
                "error_code": error.code,
                "rejected_before_admission": True,
            }
            public._write_receipt(config.receipt_path, receipt, overwrite=False)
            return receipt
        failure: dict[str, Any] = {
            **description,
            "operation_id": operation_id,
            "outcome": "failed",
            "error_code": error.code,
            "wall_seconds": round(time.monotonic() - started, 3),
        }
        if operation_id:
            try:
                status = public._json_response(
                    client.request("GET", f"/v1/operations/{operation_id}"),
                    200,
                    "failure_status",
                )
                batch = status["batch"]
                failure["terminal"] = {
                    "status": batch["status"],
                    "failure_code": batch["failure_code"],
                    "stages": [
                        {
                            "stage_id": stage["stage_id"],
                            "status": stage["status"],
                            "failure_code": stage["failure_code"],
                            "attempts": stage["attempts"],
                        }
                        for stage in batch["stages"]
                    ],
                }
            except public.AcceptanceError:
                pass
        public._assert_redacted(failure)
        public._write_receipt(config.receipt_path, failure, overwrite=False)
        return failure


def gpu_overlap(rows: list[dict[str, Any]]) -> int:
    """Peak admitted GPU attempts, from their recorded admission/release times."""
    events: list[tuple[datetime, int]] = []
    for row in rows:
        for attempt in row.get("attempts", []):
            admission = attempt.get("scheduling_admission") or {}
            count = admission.get("accelerator_count", 0)
            begin = admission.get("admitted_at")
            end = attempt.get("completed_at")
            if count and begin and end:
                events.extend(
                    [
                        (datetime.fromisoformat(begin.replace("Z", "+00:00")), count),
                        (datetime.fromisoformat(end.replace("Z", "+00:00")), -count),
                    ]
                )
    peak = active = 0
    for _, count in sorted(events):
        active += count
        peak = max(peak, active)
    return peak


def collect_metrics(endpoint: str, rows: list[dict[str, Any]], run_root: Path) -> None:
    """Reuse the benchmark's exact phase and GPU accounting projections."""
    import run_coldstart_benchmark as benchmark

    client = benchmark.PUBLIC.PublicApiClient(endpoint, os.environ["FS2_ADMIN_TOKEN"])
    cookie = benchmark._open_admin_session(client)
    try:
        for row in rows:
            if row["outcome"] != "passed" or not row.get("attempts"):
                continue
            evidence = benchmark._admin_evidence(
                client, cookie, row["operation_id"], row["model_id"], 30
            )
            phases = benchmark._phase_map(evidence.detail)
            receipt = json.loads((run_root / f"{row['id']}.json").read_bytes())
            timestamps = receipt["timestamps"]
            row["measurements"] = {
                **{
                    metric: benchmark._phase_measurement(phases, aliases)
                    for metric, aliases in benchmark.PHASE_METRICS.items()
                },
                "time_to_first_semantic_result_seconds": benchmark._elapsed(
                    timestamps["accepted_at"],
                    timestamps["result_completed_at"],
                    "public-operation-timestamps",
                ),
                "total_runtime_seconds": benchmark._elapsed(
                    timestamps["started_at"],
                    timestamps["completed_at"],
                    "public-operation-timestamps",
                ),
            }
            row["lifecycle_accounting"] = benchmark._lifecycle_accounting(
                evidence.lifecycle, evidence.lifecycle_error
            )
            row["admin_sources"] = {
                "run_detail_error": evidence.detail_error,
                "lifecycle_error": evidence.lifecycle_error,
            }
    finally:
        benchmark._admin_json(
            client, cookie, "DELETE", "/admin/api/v1/session", expected_status=204
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="immutable deployed k8s-inference checkout supplying model fixtures",
    )
    parser.add_argument("--scenarios", type=Path, required=True)
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--max-parallel", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=7200)
    parser.add_argument(
        "--admin-metrics",
        action="store_true",
        help="include exact GPU and phase metrics using FS2_ADMIN_TOKEN",
    )
    args = parser.parse_args()
    if not fleet.SAFE_ID_RE.fullmatch(args.run_id) or not 1 <= args.max_parallel <= 32:
        parser.error("invalid run ID or parallelism")
    if args.admin_metrics and not os.environ.get("FS2_ADMIN_TOKEN"):
        parser.error("FS2_ADMIN_TOKEN is required for --admin-metrics")
    scenario_bytes = args.scenarios.read_bytes()
    scenarios = json.loads(scenario_bytes)
    ids = [scenario["id"] for scenario in scenarios]
    if (
        not ids
        or len(ids) != len(set(ids))
        or any(not fleet.SAFE_ID_RE.fullmatch(item) for item in ids)
    ):
        parser.error("scenario IDs must be unique bounded identifiers")
    token = os.environ["FS2_INFERENCE_TOKEN"]
    root = args.repository_root.resolve(strict=True)
    fragments = {item.model_id: item.path for item in fleet.discover_inputs(root)}
    run_root = args.receipt_root / args.run_id
    run_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    rows = []
    with ThreadPoolExecutor(max_workers=args.max_parallel) as executor:
        futures = []
        for scenario in scenarios:
            config = public.RunConfig(
                endpoint=args.endpoint,
                repository_root=root,
                activation_fragment=fragments[scenario["model_id"]],
                receipt_path=run_root / f"{scenario['id']}.json",
                run_id=f"{args.run_id}.{scenario['id']}",
                timeout_seconds=args.timeout_seconds,
            )
            futures.append(executor.submit(run_scenario, config, scenario, token))
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            print(
                json.dumps(
                    {
                        key: row.get(key)
                        for key in ("id", "outcome", "error_code", "wall_seconds")
                    }
                ),
                flush=True,
            )
    metrics_error = None
    if args.admin_metrics:
        try:
            collect_metrics(args.endpoint, rows, run_root)
        except (KeyError, RuntimeError, ValueError, OSError) as error:
            # Preserve all GPU outcomes even when an observability reader
            # fails; class names are safe while transport messages may not be.
            metrics_error = type(error).__name__
    summary = {
        "schema": SCHEMA,
        "run_id": args.run_id,
        "scenarios_sha256": hashlib.sha256(scenario_bytes).hexdigest(),
        "passed": sum(row["outcome"] == "passed" for row in rows),
        "failed": sum(row["outcome"] != "passed" for row in rows),
        "peak_admitted_gpu_attempts": gpu_overlap(rows),
        "metrics_error": metrics_error,
        "rows": sorted(rows, key=lambda row: row["id"]),
    }
    public._write_receipt(run_root / "aggregate.json", summary, overwrite=False)
    return int(summary["failed"] > 0 or metrics_error is not None)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Export only completed public aging acceptance; never change qualifications.

Inputs are the existing public_apps.py receipts, a release publication sidecar,
and per-model {pod, node, captured_at} witnesses. No credentials, raw logs, key
IDs, principal metadata or arbitrary source fields enter the public receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
MODELS = {"phenoage": "cpu", "altumage": "cuda"}
PUBLIC_EVIDENCE_FIELDS = (
    "audited_live_routes_sha256",
    "model_discovery_sha256",
    "mcp_discovery_sha256",
    "http_mcp_acceptance_sha256",
    "cold_start_acceptance_sha256",
    "elasticity_acceptance_sha256",
)


def require(condition, code):
    if not condition:
        raise ValueError(code)


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def value_sha(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def stamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "timestamp_without_zone")
    return parsed


def mcp_body(row):
    response = row["response"]
    require(response.get("isError") is False, "mcp_tool_failed")
    require(
        isinstance(response.get("structuredContent"), dict),
        "mcp_structured_result_missing",
    )
    return response["structuredContent"]


def source_ref(path, root):
    return {"path": str(path.relative_to(root)), "sha256": sha(path)}


def image_pull_boundary(witness, image):
    """Field-select exact Pod/image events; reported image size is not wire bytes."""
    events = []
    for event in witness.get("events", {}).get("items", []):
        message = event.get("message", "")
        if (
            event.get("involvedObject", {}).get("uid")
            != witness["pod"]["metadata"]["uid"]
            or event.get("reason") not in {"Pulling", "Pulled"}
            or image not in message
        ):
            continue
        duration = re.search(r" in (?:(\d+)m)?(\d+(?:\.\d+)?)s ", message)
        events.append(
            {
                "reason": event["reason"],
                "first_at": event.get("firstTimestamp") or event.get("eventTime"),
                "last_at": event.get("lastTimestamp") or event.get("eventTime"),
                "image_already_present": "already present on machine" in message,
                "successful_image_pull": "Successfully pulled image" in message,
                "reported_pull_seconds": None
                if duration is None
                else float(60 * Decimal(duration[1] or "0") + Decimal(duration[2])),
            }
        )
    return {
        "state": "image-already-present"
        if any(row["image_already_present"] for row in events)
        else "image-pulled"
        if any(row["successful_image_pull"] for row in events)
        else "unobserved",
        "events": events,
        "wire_bytes": None,
        "note": "Kubelet event timestamps are coarse; pull duration is kubelet-reported, not pure registry transfer time. No empty registry/cache or wire-byte claim.",
    }


def export_model(campaign_root, model, witness_path, publication_path):
    """Validate one original two-input run and return a field-selected receipt."""
    campaign = read(campaign_root / "outcome.json")
    require(campaign["outcome"] == "passed", "campaign_not_passed")
    require(
        {row["model_id"] for row in campaign["models"]} == set(MODELS),
        "campaign_models_differ",
    )
    require(
        all(row["outcome"] == "passed" for row in campaign["models"]),
        "model_not_passed",
    )
    outcome_path = campaign_root / model / "outcome.json"
    outcome = read(outcome_path)
    require(
        outcome["outcome"] == "passed" and outcome["test_key_revoked"] is True,
        "model_or_cleanup_not_passed",
    )
    require(
        outcome["model_id"] == model and outcome["release"] == campaign["release"],
        "outcome_identity_mismatch",
    )
    publication = read(publication_path)
    require(
        re.fullmatch(r"[0-9a-f]{40}", publication["source_commit"]) is not None
        and re.fullmatch(r"sha256:[0-9a-f]{64}", publication["digest"]) is not None,
        "release_not_pinned",
    )
    release_label = campaign["release"]
    require(
        len(release_label) >= 6
        and (
            publication["source_commit"].startswith(release_label)
            or publication["digest"].removeprefix("sha256:").startswith(release_label)
        ),
        "release_sidecar_mismatch",
    )
    declaration_path = ROOT / "catalog/runtime/native" / f"{model}.json"
    entry_path = (
        ROOT / "catalog/runtime/deployment-runtimes" / f"{model}-{MODELS[model]}.json"
    )
    declaration, entry = read(declaration_path), read(entry_path)
    record = entry["record"]
    require(record == declaration["record"], "native_record_changed")
    spec = outcome["test_settings"]["serving"]["spec"]
    require(
        spec == outcome["final_settings"]["serving"]["spec"],
        "settings_changed_during_trial",
    )
    require(spec["modelRef"] == model, "settings_model_mismatch")
    require(
        spec["runtime"]["image"] == record["runtime"]["image"]["reference"],
        "settings_image_mismatch",
    )
    require(
        spec["artifact"]["revision"] == record["model"]["source"]["revision"],
        "settings_source_mismatch",
    )
    require(
        spec["artifact"]["manifestDigest"].removeprefix("sha256:")
        == record["cache"]["artifact"]["manifest_digest"],
        "settings_artifact_mismatch",
    )
    require(
        spec["availability"]["minReplicas"] == 0
        and spec["availability"]["maxReplicas"] == 1,
        "not_zero_to_one_policy",
    )
    require(not spec["availability"]["warmWindows"], "warm_window_not_cold")
    require(spec["lifecycle"]["desiredState"] == "Enabled", "app_not_enabled")
    for phase in ("zero_before", "zero_after"):
        observed = outcome[phase]
        require(observed["summary"]["app_id"] == outcome["app_id"], "zero_app_mismatch")
        require(observed["summary"]["status"] == "Cold", "cold_status_not_observed")
        require(
            observed["containers"]["state"] == "available"
            and not observed["containers"]["truncated"],
            "zero_unavailable",
        )
        require(
            observed["containers"]["total"] == 0
            and observed["containers"]["items"] == [],
            "zero_not_observed",
        )
    operations = outcome["operations"]
    require(
        len(operations) == 2 and len({row["operation_id"] for row in operations}) == 2,
        "logical_operations_not_two",
    )
    require(
        [row["request_sha256"] for row in operations]
        == [
            row["payload_sha256"]
            for row in declaration["semantic_requests"]["requests"]
        ],
        "original_input_identity_mismatch",
    )
    require(outcome["usage"]["logical_runs"] == 2, "usage_not_two")
    require(
        {row["operation"]["id"] for row in outcome["runs"]["items"]}
        == {row["operation_id"] for row in operations},
        "runs_identity_mismatch",
    )

    traces = [
        (path, read(path))
        for path in sorted((campaign_root / model).glob("[0-9]*-*.json"))
    ]
    require(
        all("error_type" not in row for _, row in traces), "transport_error_recorded"
    )
    require(
        all(
            200 <= row["status"] < 300
            or (
                row["status"] == 401
                and row.get("method") == "GET"
                and row.get("path") == "/v1/models"
            )
            for _, row in traces
            if "method" in row
        ),
        "failed_http_exchange_recorded",
    )
    for _, row in traces:
        if "tool" in row:
            mcp_body(row)
    http_discovery = [
        (path, row)
        for path, row in traces
        if row.get("method") == "GET"
        and row.get("path") == "/v1/models"
        and row.get("status") == 200
    ]
    require(len(http_discovery) == 1, "http_discovery_missing_or_retried")
    require(
        [item["id"] for item in http_discovery[0][1]["response"]["data"]] == [model],
        "http_discovery_scope_mismatch",
    )
    mcp_discovery = [
        (path, row) for path, row in traces if row.get("tool") == "list_models"
    ]
    require(
        len(mcp_discovery) == 1
        and [item["id"] for item in mcp_body(mcp_discovery[0][1])["data"]] == [model],
        "mcp_discovery_scope_mismatch",
    )
    require(
        any(
            row.get("path") == "/v1/models" and row.get("status") == 401
            for _, row in traces
        ),
        "revoked_key_denial_missing",
    )
    http_submit = [
        row
        for _, row in traces
        if row.get("method") == "POST"
        and row.get("path") == f"/v1/models/{model}:invoke"
    ]
    require(
        len(http_submit) == 2
        and all(
            row["status"] in (200, 202)
            and row["operation_id"] == operations[0]["operation_id"]
            for row in http_submit
        ),
        "http_admission_or_replay_mismatch",
    )
    require(http_submit[1]["replay"] == "true", "http_replay_not_reused")
    require(http_submit[0].get("replay") != "true", "http_original_already_reused")
    mcp_submit = [
        mcp_body(row) for _, row in traces if row.get("tool") == "invoke_model"
    ]
    require(
        len(mcp_submit) == 2
        and all(row["id"] == operations[1]["operation_id"] for row in mcp_submit)
        and mcp_submit[0]["reused"] is False
        and mcp_submit[1]["reused"] is True,
        "mcp_admission_or_replay_mismatch",
    )

    direct_path = HERE / f"{model}-r01.json"
    direct = read(direct_path)
    public_operations, observed_containers = [], []
    field = (
        "phenotypic_age_years"
        if model == "phenoage"
        else "predicted_chronological_age_years"
    )
    for index, row in enumerate(operations):
        require(row["submission_index"] == index, "submission_order_mismatch")
        require(
            math.isfinite(row["client_seconds"]) and row["client_seconds"] >= 0,
            "client_duration_invalid",
        )
        terminal, result = row["terminal"], row["result"]
        require(
            terminal["id"] == row["operation_id"] and terminal["status"] == "succeeded",
            "operation_not_successful",
        )
        require(
            result["model_id"] == model
            and result["device"] == MODELS[model]
            and result["sample_count"] == 1,
            "output_runtime_mismatch",
        )
        require(len(result["predictions"]) == 1, "output_cardinality")
        reference = direct["native_http_predictions"][index]["body"]
        actual, expected = (
            result["predictions"][0][field],
            reference["predictions"][0][field],
        )
        tolerance = 1e-10 if model == "phenoage" else 0.001
        require(
            math.isfinite(actual)
            and math.isclose(actual, expected, rel_tol=0, abs_tol=tolerance),
            "output_parity_failed",
        )
        require(
            result["predictions"][0]["sample_id"]
            == reference["predictions"][0]["sample_id"],
            "sample_identity_mismatch",
        )
        identity_field = "model_version" if model == "phenoage" else "weights_sha256"
        require(
            result[identity_field] == reference[identity_field]
            and result["gpu_snapshot"] == reference["gpu_snapshot"],
            "model_or_snapshot_claim_changed",
        )
        http_results = [
            trace
            for _, trace in traces
            if trace.get("path") == f"/v1/operations/{row['operation_id']}/result"
        ]
        require(
            len(http_results) == 1
            and http_results[0]["status"] == 200
            and http_results[0]["response"] == result,
            "http_result_proof_missing",
        )
        mcp_results = [
            mcp_body(trace)
            for _, trace in traces
            if trace.get("tool") == "get_operation_result"
        ]
        require(
            any(
                item["operation"]["id"] == row["operation_id"]
                and item["result"] == result
                for item in mcp_results
            ),
            "mcp_result_proof_missing",
        )
        for observed in row["app_observations"]:
            require(
                observed["summary"]["app_id"] == outcome["app_id"]
                and observed["containers"]["state"] == "available"
                and not observed["containers"]["truncated"],
                "worker_observation_unavailable_or_wrong_app",
            )
            require(
                len({item["pod_uid"] for item in observed["containers"]["items"]}) <= 1,
                "more_than_one_worker_observed",
            )
        current = [
            container
            for observed in row["app_observations"]
            for container in observed["containers"]["items"]
            if container["ready"]
        ]
        require(bool(current), "ready_worker_not_observed")
        observed_containers.extend(current)
        accepted_at, completed_at = (
            stamp(terminal["accepted_at"]),
            stamp(terminal["completed_at"]),
        )
        require(
            stamp(outcome["zero_before"]["at"])
            <= accepted_at
            <= completed_at
            <= stamp(outcome["zero_after"]["at"]),
            "cold_clock_order_invalid",
        )
        public_operations.append(
            {
                "operation_id": row["operation_id"],
                "submission": "HTTP" if index == 0 else "MCP",
                "request_sha256": row["request_sha256"],
                "accepted_at": terminal["accepted_at"],
                "completed_at": terminal["completed_at"],
                "accepted_to_terminal_seconds": (
                    completed_at - accepted_at
                ).total_seconds(),
                "client_seconds": row["client_seconds"],
                "http_response_sha256": http_results[0]["response_sha256"],
                "prediction": actual,
                "reference_prediction": expected,
                "absolute_tolerance": tolerance,
                "result_sha256_canonical_json": value_sha(result),
                "replay_reused_original_operation": True,
            }
        )
    require(
        public_operations[0]["prediction"] != public_operations[1]["prediction"],
        "outputs_not_distinct",
    )

    witness = read(witness_path)
    pod, node = witness["pod"], witness["node"]
    require(
        stamp(outcome["zero_before"]["at"])
        <= stamp(witness["captured_at"])
        <= stamp(outcome["zero_after"]["at"]),
        "witness_outside_trial",
    )
    require(
        witness["model_id"] == model
        and pod["spec"]["nodeName"] == node["metadata"]["name"],
        "witness_model_or_node_mismatch",
    )
    matching = [
        item
        for item in observed_containers
        if item["pod_uid"] == pod["metadata"]["uid"]
        and item["node_name"] == node["metadata"]["name"]
    ]
    require(bool(matching), "witness_not_public_worker")
    image = record["runtime"]["image"]["reference"]
    require(
        all(item["image"] == image for item in observed_containers),
        "public_worker_image_mismatch",
    )
    containers = [item for item in pod["spec"]["containers"] if item["image"] == image]
    require(len(containers) == 1, "witness_runtime_image_mismatch")
    runtime = containers[0]
    require(
        any(
            item["type"] == "Ready" and item["status"] == "True"
            for item in pod["status"]["conditions"]
        ),
        "witness_pod_not_ready",
    )
    gpu_count = int(runtime["resources"]["requests"].get("nvidia.com/gpu", 0))
    require(
        gpu_count == record["resources"]["gpu"]["count"], "witness_gpu_count_mismatch"
    )
    labels = node["metadata"].get("labels", {})
    if model == "phenoage":
        require(
            labels.get("workload.fs2.nebius/general-cpu") == "true",
            "cpu_node_class_mismatch",
        )
    else:
        require(
            labels.get("accelerator.fs2.nebius/class", "").lower()
            == record["resources"]["gpu"]["class"].lower(),
            "h100_node_class_mismatch",
        )
    node_created_at = node["metadata"]["creationTimestamp"]
    node_ready_at = next(
        (
            item["lastTransitionTime"]
            for item in node["status"]["conditions"]
            if item["type"] == "Ready" and item["status"] == "True"
        ),
        None,
    )
    return {
        "schema": "fs2-aging-public-qualification/v1",
        "outcome": "passed",
        "cohort": campaign_root.name,
        "model_id": model,
        "app_id": outcome["app_id"],
        "variant_id": entry["variant_id"],
        "model_source": record["model"]["source"]["revision"],
        "runtime_image": image,
        "artifact_manifest_sha256": record["cache"]["artifact"]["manifest_digest"],
        "record_sha256_canonical_json": value_sha(record),
        "release": {
            key: publication[key]
            for key in ("source_commit", "source_tree", "repository", "digest")
        },
        "harness_release_label": release_label,
        "window": {key: outcome[key] for key in ("started_at", "completed_at")},
        "scope": {
            "replica_transition": "0-to-1-to-0",
            "maximum_configured_replicas": 1,
            "multi_replica_scale_out_qualified": False,
            "cold_boundary": "observed Cold and zero reusable workers; node lifetime is recorded below, image cache emptiness is not claimed",
            "hardware": record["resources"]["gpu"]["class"],
            "other_hardware_qualified": False,
            "gpu_snapshot": "not-applicable-cpu"
            if model == "phenoage"
            else "not-qualified",
        },
        "operations": public_operations,
        "worker": {
            "pod_uid": pod["metadata"]["uid"],
            "pod_name": pod["metadata"]["name"],
            "namespace": pod["metadata"]["namespace"],
            "node_name": node["metadata"]["name"],
            "image": image,
            "resources": runtime["resources"],
            "witness_at": witness["captured_at"],
            "pod_created_at": pod["metadata"]["creationTimestamp"],
            "node_created_at": node_created_at,
            "node_ready_transition_at": node_ready_at,
            "node_existed_before_first_admission": stamp(node_created_at)
            <= stamp(public_operations[0]["accepted_at"]),
            "image_pull_boundary": image_pull_boundary(witness, image),
        },
        "zero_workers": {
            phase: outcome[phase]["at"] for phase in ("zero_before", "zero_after")
        },
        "logical_runs": 2,
        "temporary_key_revoked_and_denied": True,
        "final_test_spec_sha256": value_sha(spec),
        "source_receipts": {
            "campaign": source_ref(campaign_root / "outcome.json", campaign_root),
            "model_outcome": source_ref(outcome_path, campaign_root),
            "http_discovery": source_ref(http_discovery[0][0], campaign_root),
            "mcp_discovery": source_ref(mcp_discovery[0][0], campaign_root),
            "model_trace_count": len(traces),
            "model_trace_manifest_sha256": value_sha(
                [source_ref(path, campaign_root) for path, _ in traces]
            ),
            "runtime_witness_sha256": sha(witness_path),
            "release_publication_sha256": sha(publication_path),
            "direct_native_receipt": {
                "path": direct_path.name,
                "sha256": sha(direct_path),
            },
            "native_declaration_sha256": sha(declaration_path),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--runtime-witness-root", type=Path, required=True)
    parser.add_argument("--publication", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Validate the complete campaign before producing any apparent success.
    receipts = {
        model: export_model(
            args.campaign_root,
            model,
            args.runtime_witness_root / f"{model}.json",
            args.publication,
        )
        for model in MODELS
    }
    args.output.mkdir(parents=True, exist_ok=False)
    for model, receipt in receipts.items():
        path = args.output / f"{model}.json"
        path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False) + "\n"
        )
        print(json.dumps({"model_id": model, "receipt_sha256": sha(path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

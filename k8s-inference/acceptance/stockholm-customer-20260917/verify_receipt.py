#!/usr/bin/env python3
"""Offline consistency gate for Stockholm acceptance receipts; performs no live work."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

GATE_PATH = Path(__file__).resolve().parents[1] / "customer-readiness" / "capability_gate.py"
SPEC = importlib.util.spec_from_file_location("stockholm_capability_gate", GATE_PATH)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

SCHEMA = "fs2-serve.nebius.ai/stockholm-customer-receipt/v1"
POLICY_FIELDS = {
    "tenant_id",
    "models",
    "scopes",
    "max_concurrency",
    "request_budget",
    "gpu_seconds_budget",
    "rate_limit_requests",
    "rate_window_seconds",
}
APP_FIELDS = {
    "app_id",
    "source_model_ref",
    "model_revision",
    "runtime_image_digest",
    "kind",
    "operation",
    "named_tool",
    "generic_tool",
    "workload_states",
}
CLIENT_FIELDS = {
    "client_build_sha256",
    "installed_skills_sha256",
    "agent_configuration_sha256",
}
SCENARIOS = {
    "named-sdk": ("mcp-sdk", "flat"),
    "generic-sdk": ("mcp-sdk", "generic-outer"),
    "legacy-generic-sdk": ("mcp-sdk", "legacy-nested"),
    "named-librechat": ("librechat+mcp", "flat"),
    "generic-librechat": ("librechat+mcp", "generic-outer"),
    "public-api": ("https-api", "api"),
}
SURFACES = ["containers", "logs", "metrics", "runs", "usage"]
CHECKS = {
    "platform_responsive",
    "catalog_refreshed",
    "usage_reconciled",
    "retry_behavior_verified",
}
COUNTERS = {
    "unexpected_restarts",
    "unexplained_warnings",
    "manual_recoveries",
    "resource_leaks",
}


class ReceiptError(ValueError):
    pass


def check(condition: object, code: str) -> None:
    if not condition:
        raise ReceiptError(code)


def exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    check(isinstance(value, dict) and set(value) == keys, label + "_fields")
    return value  # type: ignore[return-value]


def digest(value: object) -> str:
    body = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return "sha256:" + hashlib.sha256(body).hexdigest()


def sha(value: object) -> None:
    check(
        isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value),
        "invalid_digest",
    )


def timestamp(value: object) -> datetime:
    check(isinstance(value, str), "timestamp_required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))  # type: ignore[union-attr]
        check(parsed.tzinfo is not None, "timestamp_timezone_required")
        return parsed.astimezone(UTC)
    except (ValueError, TypeError):
        raise ReceiptError("invalid_timestamp") from None


def uuid(value: object) -> None:
    try:
        check(isinstance(value, str) and str(UUID(value)) == value, "invalid_uuid")
    except (ValueError, TypeError, AttributeError):
        raise ReceiptError("invalid_uuid") from None


def safe(value: object) -> None:
    # Inputs are sanitized metadata, hashes, and identifiers only. This is an
    # extra known-token guard, not a substitute for redacting collector output.
    check(
        not re.search(r"(?i)fs2_pat_|nvapi-|bearer\s+", json.dumps(value)),
        "credential_marker_present",
    )


def build_manifest(identity: object, discovery: object, team_policy: object, client: object) -> dict[str, Any]:
    release = gate.validate_identity(identity)
    check({"control-plane", "librechat"} <= set(release["runtime_images"]), "deployed_gateway_or_client_image_missing")
    endpoint = urlsplit(release["public_endpoint"])
    check(
        endpoint.hostname and not endpoint.username and not endpoint.query and not endpoint.fragment,
        "public_endpoint_must_not_contain_credentials",
    )
    policy = exact(team_policy, POLICY_FIELDS, "team_policy")
    check(
        policy["max_concurrency"] == 5 and type(policy["max_concurrency"]) is int,
        "team_concurrency_must_be_five",
    )
    check(isinstance(policy["tenant_id"], str) and policy["tenant_id"], "tenant_required")
    for field in ("models", "scopes"):
        values = policy[field]
        check(
            isinstance(values, list) and values and all(isinstance(item, str) and item for item in values),
            "policy_list_invalid",
        )
        check(values == sorted(set(values)), "policy_list_must_be_sorted_unique")
    check(
        {
            "catalog.read",
            "mcp.invoke",
            "inference.invoke",
            "operations.read",
            "operations.result",
        }
        <= set(policy["scopes"]),
        "customer_scopes_missing",
    )
    check(
        not {"tenant.admin", "tokens.manage", "audit.read"}.intersection(policy["scopes"]),
        "admin_policy_forbidden",
    )
    check(
        digest(policy) == release["tenant_policy_sha256"],
        "team_policy_identity_mismatch",
    )
    client = exact(client, CLIENT_FIELDS, "client_identity")
    for value in client.values():
        sha(value)
    check(
        client["client_build_sha256"] == release["client_build_sha256"],
        "client_build_mismatch",
    )
    discovery = exact(discovery, {"schema", "tool_catalog_sha256", "apps"}, "discovery")
    check(
        discovery["schema"] == "fs2-serve.nebius.ai/stockholm-discovery/v1",
        "discovery_schema",
    )
    check(
        discovery["tool_catalog_sha256"] == release["public_tool_catalog_sha256"],
        "tool_catalog_mismatch",
    )
    check(
        isinstance(discovery["apps"], list) and discovery["apps"],
        "advertised_apps_missing",
    )
    apps, excluded, manifests = {}, [], []
    seen = set()
    for raw in discovery["apps"]:
        app = exact(raw, APP_FIELDS, "app")
        for field in APP_FIELDS - {"workload_states"}:
            check(isinstance(app[field], str) and app[field], "app_identity_invalid")
        app_id = app["app_id"]
        check(app_id not in seen, "duplicate_app")
        seen.add(app_id)
        check(app_id in policy["models"], "advertised_app_outside_team_grants")
        if app["source_model_ref"].startswith("cosmos"):
            excluded.append(app_id)
            continue
        check(app["kind"] in {"serving", "scientific-batch"}, "unsupported_app_kind")
        check(
            app["generic_tool"] == ("invoke_model" if app["kind"] == "serving" else "submit_scientific_run"),
            "generic_tool_mismatch",
        )
        check(
            release["runtime_images"].get(app_id) == app["runtime_image_digest"],
            "app_runtime_image_missing_or_changed",
        )
        apps[app_id] = app
        scenarios = []
        for scenario_id, (client_path, _) in SCENARIOS.items():
            if app["kind"] == "scientific-batch" and scenario_id == "legacy-generic-sdk":
                continue
            scenarios.append(
                {
                    "id": scenario_id,
                    "operation": app["operation"],
                    "input_form": app["kind"],
                    "output_form": "validated-result",
                    "client_path": client_path,
                    "workload_states": app["workload_states"],
                    "integrations": ["bionemo-skills", "scientific-gateway"] if "librechat" in scenario_id else [],
                    "required_observability": SURFACES,
                    "min_clean_cohorts": 2,
                }
            )
        manifests.append(
            gate.validate_manifest(
                {
                    "schema": gate.MANIFEST_SCHEMA,
                    "app_id": app_id,
                    "release_identity": release,
                    "max_evidence_age_seconds": 86400,
                    "capabilities": [
                        {
                            "id": "stockholm-workflow",
                            "description": "Exact advertised Stockholm App workflow",
                            "advertised": True,
                            "scenarios": scenarios,
                        }
                    ],
                    "requested_capability_ids": ["stockholm-workflow"],
                    "reduced_scope_decisions": [],
                }
            )
        )
    check(apps, "no_non_cosmos_apps")
    # Grants absent from discovery still block coverage; they cannot silently
    # disappear from the manifest. Explicit Cosmos source rows are excluded.
    check(set(policy["models"]) == seen, "team_grant_missing_from_discovery")
    safe([release, policy, client, discovery])
    return {
        "release_identity": release,
        "team_policy": policy,
        "client_identity": client,
        "discovery_sha256": digest(discovery),
        "apps": apps,
        "excluded_cosmos_apps": sorted(excluded),
        "capability_manifests": manifests,
    }


def verify(
    manifest: dict[str, Any],
    receipt: object,
    librechat: object,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    release = manifest["release_identity"]
    required = {
        (item["app_id"], scenario["id"])
        for item in manifest["capability_manifests"]
        for scenario in item["capabilities"][0]["scenarios"]
    }
    rows: dict[tuple[str, str], list[dict[str, Any]]] = {key: [] for key in required}
    evidence, errors = [], []
    try:
        safe([receipt, librechat])
        receipt = exact(
            receipt,
            {
                "schema",
                "release_identity_before",
                "release_identity_after",
                "discovery_sha256",
                "team_policy",
                "canary",
                "client_identity",
                "cohorts",
            },
            "receipt",
        )
        check(receipt["schema"] == SCHEMA, "receipt_schema")
        check(
            receipt["release_identity_before"] == receipt["release_identity_after"] == release,
            "release_identity_drift",
        )
        check(
            receipt["discovery_sha256"] == manifest["discovery_sha256"],
            "discovery_identity_drift",
        )
        check(
            receipt["team_policy"] == manifest["team_policy"],
            "canary_policy_not_team_equivalent",
        )
        check(
            receipt["client_identity"] == manifest["client_identity"],
            "installed_client_or_skills_changed",
        )
        canary = exact(
            receipt["canary"],
            {"principal_id", "token_fingerprint", "disposable", "expires_at"},
            "canary",
        )
        check(
            isinstance(canary["principal_id"], str) and canary["principal_id"] and canary["disposable"] is True,
            "disposable_customer_principal_required",
        )
        sha(canary["token_fingerprint"])
        cohorts = receipt["cohorts"]
        check(
            isinstance(cohorts, list) and len(cohorts) == 2,
            "exactly_two_complete_cohorts_required",
        )
        operation_ids, expected_client_calls = set(), {}
        previous_end = None
        for sequence, raw_cohort in enumerate(cohorts, 1):
            cohort = exact(
                raw_cohort,
                {
                    "sequence",
                    "started_at",
                    "completed_at",
                    "release_identity_before",
                    "release_identity_after",
                    "checks",
                    "counters",
                    "calls",
                },
                "cohort",
            )
            check(
                type(cohort["sequence"]) is int and cohort["sequence"] == sequence,
                "cohorts_not_consecutive",
            )
            check(
                cohort["release_identity_before"] == cohort["release_identity_after"] == release,
                "cohort_identity_drift",
            )
            started, completed = (
                timestamp(cohort["started_at"]),
                timestamp(cohort["completed_at"]),
            )
            check(
                started < completed <= now and started > now - timedelta(days=1),
                "cohort_time_invalid_or_stale",
            )
            check(previous_end is None or previous_end <= started, "cohorts_overlap")
            check(
                timestamp(canary["expires_at"]) > completed,
                "canary_expired_during_cohort",
            )
            previous_end = completed
            checks = exact(cohort["checks"], CHECKS, "cohort_checks")
            check(
                all(value is True for value in checks.values()),
                "required_cohort_check_failed",
            )
            counters = exact(cohort["counters"], COUNTERS, "cohort_counters")
            check(
                all(type(value) is int and value == 0 for value in counters.values()),
                "cohort_not_clean",
            )
            check(isinstance(cohort["calls"], list), "calls_required")
            seen, events = set(), []
            for raw_call in cohort["calls"]:
                call = exact(
                    raw_call,
                    {
                        "app_id",
                        "scenario_id",
                        "operation_id",
                        "request_id",
                        "accepted_at",
                        "completed_at",
                        "replay_operation_id",
                        "terminal_status",
                        "semantic_validated",
                        "artifact_validated",
                        "fixture_sha256",
                        "result_sha256",
                        "upstream_fields",
                        "workload_states",
                        "observability",
                        "batch_upload_sha256",
                        "queue_observed",
                    },
                    "call",
                )
                key = (call["app_id"], call["scenario_id"])
                check(
                    key in required and key not in seen,
                    "unexpected_or_duplicate_scenario",
                )
                seen.add(key)
                operation = call["operation_id"]
                uuid(operation)
                uuid(call["request_id"])
                check(
                    operation not in operation_ids,
                    "operation_reused_across_scenarios_or_cohorts",
                )
                operation_ids.add(operation)
                check(
                    call["replay_operation_id"] == operation,
                    "idempotent_replay_not_proved",
                )
                accepted_at, completed_at = (
                    timestamp(call["accepted_at"]),
                    timestamp(call["completed_at"]),
                )
                check(
                    started <= accepted_at < completed_at <= completed,
                    "operation_time_outside_cohort",
                )
                events.extend(
                    [
                        (accepted_at, 1, operation, key[0]),
                        (completed_at, -1, operation, key[0]),
                    ]
                )
                check(
                    call["terminal_status"] == "succeeded" and call["semantic_validated"] is True,
                    "terminal_semantic_failure",
                )
                check(call["artifact_validated"] is True, "terminal_output_not_validated")
                sha(call["fixture_sha256"])
                sha(call["result_sha256"])
                states = call["workload_states"]
                check(
                    isinstance(states, list) and states and all(isinstance(state, str) and state for state in states),
                    "workload_state_capture_missing",
                )
                check(states == sorted(set(states)), "workload_states_not_sorted_unique")
                fields = call["upstream_fields"]
                check(
                    isinstance(fields, list) and fields and all(isinstance(field, str) for field in fields),
                    "upstream_field_capture_missing",
                )
                check(
                    not {"idempotency_key", "wait_seconds"}.intersection(fields),
                    "gateway_control_leaked_upstream",
                )
                observed = exact(call["observability"], set(SURFACES), "observability")
                for value in observed.values():
                    sha(value)
                if manifest["apps"][key[0]]["kind"] == "scientific-batch":
                    sha(call["batch_upload_sha256"])
                    check(
                        call["queue_observed"] is True,
                        "scientific_upload_queue_or_artifact_unverified",
                    )
                rows[key].append(call)
                if "librechat" in key[1]:
                    expected_client_calls[operation] = call
            check(seen == required, "advertised_app_or_route_coverage_incomplete")
            active, peak, mixed_peak = {}, 0, False
            for _, direction, operation, app in sorted(events):
                if direction == -1:
                    active.pop(operation)
                else:
                    active[operation] = app
                peak = max(peak, len(active))
                mixed_peak |= len(active) == 5 and len(set(active.values())) >= 2
            check(
                peak == 5 and mixed_peak,
                "concurrency_five_mixed_model_overlap_not_proved",
            )
        trace = exact(
            librechat,
            {
                "schema",
                "producer",
                "client_identity",
                "public_endpoint",
                "principal_id",
                "token_fingerprint",
                "operations",
            },
            "librechat_trace",
        )
        check(
            trace["schema"] == "fs2-serve.nebius.ai/stockholm-librechat-trace/v1"
            and trace["producer"] == "librechat-agent-trace",
            "actual_librechat_trace_required",
        )
        check(
            trace["client_identity"] == manifest["client_identity"]
            and trace["public_endpoint"] == release["public_endpoint"],
            "librechat_release_mismatch",
        )
        check(
            trace["principal_id"] == canary["principal_id"]
            and trace["token_fingerprint"] == canary["token_fingerprint"],
            "librechat_caller_mismatch",
        )
        check(isinstance(trace["operations"], list), "librechat_operations_missing")
        seen_client = set()
        for raw in trace["operations"]:
            item = exact(
                raw,
                {
                    "operation_id",
                    "request_id",
                    "conversation_id",
                    "message_id",
                    "tool_name",
                    "argument_shape",
                    "arguments_sha256",
                    "terminal_status",
                    "loaded_skills",
                },
                "client_call",
            )
            operation = item["operation_id"]
            check(
                operation in expected_client_calls and operation not in seen_client,
                "librechat_operation_mismatch",
            )
            seen_client.add(operation)
            call = expected_client_calls[operation]
            app = manifest["apps"][call["app_id"]]
            named = call["scenario_id"] == "named-librechat"
            check(
                item["tool_name"] == app["named_tool" if named else "generic_tool"]
                and item["argument_shape"] == ("flat" if named else "generic-outer"),
                "librechat_tool_or_shape_mismatch",
            )
            check(
                item["request_id"] == call["request_id"] and item["terminal_status"] == "succeeded",
                "librechat_terminal_correlation_missing",
            )
            check(
                all(isinstance(item[field], str) and item[field] for field in ("conversation_id", "message_id")),
                "librechat_conversation_trace_missing",
            )
            check(
                item["loaded_skills"] == ["bionemo-skills", "scientific-gateway"],
                "actual_installed_skills_not_exercised",
            )
            sha(item["arguments_sha256"])
        check(
            seen_client == set(expected_client_calls),
            "librechat_did_not_exercise_exact_operations",
        )
        for app_manifest in manifest["capability_manifests"]:
            for scenario in app_manifest["capabilities"][0]["scenarios"]:
                calls = rows[(app_manifest["app_id"], scenario["id"])]
                evidence.append(
                    {
                        "schema": gate.EVIDENCE_SCHEMA,
                        "evidence_id": "stockholm:" + calls[-1]["operation_id"],
                        "app_id": app_manifest["app_id"],
                        "capability_id": "stockholm-workflow",
                        "scenario_id": scenario["id"],
                        **{
                            field: scenario[field]
                            for field in (
                                "operation",
                                "input_form",
                                "output_form",
                                "client_path",
                            )
                        },
                        "observed_at": receipt["cohorts"][-1]["completed_at"],
                        "release_identity": release,
                        "fixture_sha256": digest([call["fixture_sha256"] for call in calls]),
                        "outcome": "passed",
                        "clean_cohorts": 2,
                        "public_endpoint_exercised": True,
                        "direct_runtime_only": False,
                        "terminal_outcome_validated": True,
                        "artifact_validated": True,
                        "tenant_policy_exercised": True,
                        "workload_states": sorted({state for call in calls for state in call["workload_states"]}),
                        "integrations": scenario["integrations"],
                        "observed_surfaces": SURFACES,
                        "notes": [],
                    }
                )
    except (ReceiptError, gate.CapabilityGateError, TypeError, ValueError, KeyError):
        error = sys.exception()
        errors.append(
            str(error) if isinstance(error, (ReceiptError, gate.CapabilityGateError)) else "malformed_receipt"
        )
        evidence = []
    verdicts = [gate.evaluate(item, evidence, evaluated_at=now) for item in manifest["capability_manifests"]]
    return {
        "schema": "fs2-serve.nebius.ai/stockholm-receipt-verdict/v1",
        "verification_scope": "offline-receipt-consistency",
        "ready": not errors and all(item["ready"] for item in verdicts),
        "errors": errors,
        "excluded_cosmos_apps": manifest["excluded_cosmos_apps"],
        "app_verdicts": verdicts,
        "required_scenarios": [list(key) for key in sorted(required)],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("release-identity", "discovery", "team-policy", "client-identity"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--librechat-trace", type=Path)
    args = parser.parse_args()
    try:

        def load(path: Path | None) -> object:
            return json.loads(path.read_text(encoding="utf-8")) if path else None

        manifest = build_manifest(
            load(args.release_identity),
            load(args.discovery),
            load(args.team_policy),
            load(args.client_identity),
        )
        report = verify(manifest, load(args.receipt), load(args.librechat_trace))
    except (OSError, ValueError, TypeError, KeyError):
        print(
            "Stockholm gate input error: invalid or missing sanitized release metadata",
            file=sys.stderr,
        )
        return 3
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

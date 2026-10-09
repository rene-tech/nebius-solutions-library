"""Synthetic two-App metadata fixtures, never live scientific input fixtures."""

import importlib.util
import json
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

SPEC = importlib.util.spec_from_file_location(
    "stockholm_receipt_verifier", Path(__file__).with_name("verify_receipt.py")
)
assert SPEC and SPEC.loader
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def metadata():
    policy = {
        "tenant_id": "stockholm-test-fixture",
        "models": ["batch-fixture", "cosmos-excluded-fixture", "protein-fixture"],
        "scopes": [
            "artifacts.write",
            "catalog.read",
            "inference.invoke",
            "mcp.invoke",
            "operations.read",
            "operations.result",
        ],
        "max_concurrency": 5,
        "request_budget": 1000,
        "gpu_seconds_budget": 100000,
        "rate_limit_requests": 100,
        "rate_window_seconds": 60,
    }
    identity = {
        "source_revision": "1" * 40,
        "runtime_images": {name: DIGEST for name in ["control-plane", "librechat", *policy["models"]]},
        "configuration_sha256": DIGEST,
        "model_revision": "synthetic-inventory-only",
        "client_build_sha256": DIGEST,
        "tenant_policy_sha256": verifier.digest(policy),
        "public_endpoint": "https://stockholm.test.invalid/mcp",
        "public_tool_catalog_sha256": DIGEST,
    }
    client = dict.fromkeys(verifier.CLIENT_FIELDS, DIGEST)
    apps = []
    for app_id, source, kind in (
        ("protein-fixture", "synthetic-protein-model", "serving"),
        ("batch-fixture", "synthetic-batch-model", "scientific-batch"),
        ("cosmos-excluded-fixture", "cosmos3-nano", "serving"),
    ):
        apps.append(
            {
                "app_id": app_id,
                "source_model_ref": source,
                "model_revision": "synthetic-v1",
                "runtime_image_digest": DIGEST,
                "kind": kind,
                "operation": "test-operation",
                "named_tool": app_id.replace("-", "_"),
                "generic_tool": "invoke_model" if kind == "serving" else "submit_scientific_run",
                "workload_states": ["hot"],
            }
        )
    discovery = {
        "schema": "fs2-serve.nebius.ai/stockholm-discovery/v1",
        "tool_catalog_sha256": DIGEST,
        "apps": apps,
    }
    return identity, discovery, policy, client


def test_wildcard_grants_expand_through_complete_discovery():
    identity, discovery, policy, client = metadata()
    policy["models"] = ["*"]
    identity["tenant_policy_sha256"] = verifier.digest(policy)
    manifest = verifier.build_manifest(identity, discovery, policy, client)
    assert set(manifest["apps"]) == {"protein-fixture", "batch-fixture"}
    assert manifest["excluded_cosmos_apps"] == ["cosmos-excluded-fixture"]


def test_explicit_grant_with_wildcard_must_not_disappear():
    identity, discovery, policy, client = metadata()
    policy["models"] = ["*", "missing-app"]
    identity["tenant_policy_sha256"] = verifier.digest(policy)
    with pytest.raises(verifier.ReceiptError, match="team_grant_missing_from_discovery"):
        verifier.build_manifest(identity, discovery, policy, client)


def inputs():
    manifest = verifier.build_manifest(*metadata())
    required = sorted(
        (
            (item["app_id"], scenario["id"])
            for item in manifest["capability_manifests"]
            for scenario in item["capabilities"][0]["scenarios"]
        ),
        key=lambda pair: (pair[1], pair[0]),
    )
    receipt = {
        "schema": verifier.SCHEMA,
        "release_identity_before": manifest["release_identity"],
        "release_identity_after": manifest["release_identity"],
        "discovery_sha256": manifest["discovery_sha256"],
        "team_policy": manifest["team_policy"],
        "client_identity": manifest["client_identity"],
        "canary": {
            "principal_id": "synthetic-disposable-canary",
            "token_fingerprint": DIGEST,
            "disposable": True,
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        },
        "cohorts": [],
    }
    trace = {
        "schema": "fs2-serve.nebius.ai/stockholm-librechat-trace/v1",
        "producer": "librechat-agent-trace",
        "client_identity": manifest["client_identity"],
        "public_endpoint": manifest["release_identity"]["public_endpoint"],
        "principal_id": receipt["canary"]["principal_id"],
        "token_fingerprint": DIGEST,
        "operations": [],
    }
    for sequence in (1, 2):
        started = NOW - timedelta(minutes=30 - sequence * 5)
        cohort = {
            "sequence": sequence,
            "started_at": started.isoformat(),
            "completed_at": (started + timedelta(minutes=1)).isoformat(),
            "release_identity_before": deepcopy(manifest["release_identity"]),
            "release_identity_after": deepcopy(manifest["release_identity"]),
            "checks": dict.fromkeys(verifier.CHECKS, True),
            "counters": dict.fromkeys(verifier.COUNTERS, 0),
            "calls": [],
        }
        for index, (app_id, scenario) in enumerate(required):
            operation, request = str(uuid4()), str(uuid4())
            accepted = started + timedelta(seconds=(index // 5) * 10)
            call = {
                "app_id": app_id,
                "scenario_id": scenario,
                "operation_id": operation,
                "request_id": request,
                "accepted_at": accepted.isoformat(),
                "completed_at": (accepted + timedelta(seconds=5)).isoformat(),
                "replay_operation_id": operation,
                "terminal_status": "succeeded",
                "semantic_validated": True,
                "artifact_validated": True,
                "fixture_sha256": DIGEST,
                "result_sha256": DIGEST,
                "upstream_fields": ["synthetic_model_field"],
                "workload_states": ["hot"],
                "observability": dict.fromkeys(verifier.SURFACES, DIGEST),
                "batch_upload_sha256": DIGEST if app_id == "batch-fixture" else None,
                "queue_observed": True,
            }
            cohort["calls"].append(call)
            if "librechat" in scenario:
                app = manifest["apps"][app_id]
                named = scenario == "named-librechat"
                trace["operations"].append(
                    {
                        "operation_id": operation,
                        "request_id": request,
                        "conversation_id": "synthetic-conversation",
                        "message_id": str(uuid4()),
                        "tool_name": app["named_tool" if named else "generic_tool"],
                        "argument_shape": "flat" if named else "generic-outer",
                        "arguments_sha256": DIGEST,
                        "terminal_status": "succeeded",
                        "loaded_skills": ["bionemo-skills", "scientific-gateway"],
                    }
                )
        receipt["cohorts"].append(cohort)
    return manifest, deepcopy(receipt), deepcopy(trace)


def test_complete_synthetic_receipt_passes_only_as_offline_consistency_evidence():
    manifest, receipt, trace = inputs()
    report = verifier.verify(manifest, receipt, trace, now=NOW)
    assert report["ready"] and report["errors"] == []
    assert report["verification_scope"] == "offline-receipt-consistency"
    assert report["excluded_cosmos_apps"] == ["cosmos-excluded-fixture"]
    assert {row["app_id"] for row in report["app_verdicts"]} == {
        "protein-fixture",
        "batch-fixture",
    }


def test_no_receipts_cannot_qualify_the_generated_manifest():
    manifest, _, _ = inputs()
    report = verifier.verify(manifest, None, None, now=NOW)
    assert not report["ready"]
    assert all(item["verdict"] == "not-ready" for item in report["app_verdicts"])


@pytest.mark.parametrize("field", sorted(verifier.CLIENT_FIELDS))
def test_actual_client_build_skills_and_agent_configuration_must_match(field):
    manifest, receipt, trace = inputs()
    trace["client_identity"][field] = "sha256:" + "b" * 64
    assert not verifier.verify(manifest, receipt, trace, now=NOW)["ready"]


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (
            lambda receipt: receipt["cohorts"].pop(),
            "exactly_two_complete_cohorts_required",
        ),
        (
            lambda receipt: receipt["cohorts"][0]["calls"].pop(),
            "advertised_app_or_route_coverage_incomplete",
        ),
        (
            lambda receipt: receipt["cohorts"][1]["release_identity_after"].update(source_revision="2" * 40),
            "cohort_identity_drift",
        ),
        (
            lambda receipt: receipt["team_policy"].update(max_concurrency=6),
            "canary_policy_not_team_equivalent",
        ),
        (
            lambda receipt: receipt["cohorts"][0]["calls"][0].update(terminal_status="failed"),
            "terminal_semantic_failure",
        ),
        (
            lambda receipt: receipt["cohorts"][0]["calls"][0].update(semantic_validated=False),
            "terminal_semantic_failure",
        ),
        (
            lambda receipt: receipt["cohorts"][0]["calls"][0].update(replay_operation_id=str(uuid4())),
            "idempotent_replay_not_proved",
        ),
        (
            lambda receipt: receipt["cohorts"][0]["calls"][0]["upstream_fields"].append("idempotency_key"),
            "gateway_control_leaked_upstream",
        ),
        (
            lambda receipt: receipt["cohorts"][0]["calls"][0]["observability"].pop("usage"),
            "observability_fields",
        ),
        (
            lambda receipt: receipt["cohorts"][0]["counters"].update(unexplained_warnings=1),
            "cohort_not_clean",
        ),
        (
            lambda receipt: receipt["cohorts"][0]["counters"].update(unexpected_restarts=1),
            "cohort_not_clean",
        ),
        (
            lambda receipt: receipt["cohorts"][0]["counters"].update(manual_recoveries=1),
            "cohort_not_clean",
        ),
        (
            lambda receipt: receipt["cohorts"][0]["counters"].update(resource_leaks=1),
            "cohort_not_clean",
        ),
        (
            lambda receipt: receipt["cohorts"][0]["checks"].update(catalog_refreshed=False),
            "required_cohort_check_failed",
        ),
        (
            lambda receipt: receipt["cohorts"][0]["calls"][0].update(queue_observed=False),
            "scientific_upload_queue_or_artifact_unverified",
        ),
    ],
)
def test_incomplete_or_changed_receipts_fail_closed(mutation, expected):
    manifest, receipt, trace = inputs()
    mutation(receipt)
    report = verifier.verify(manifest, receipt, trace, now=NOW)
    assert not report["ready"]
    assert expected in report["errors"]


def test_sequential_calls_cannot_claim_concurrency_five():
    manifest, receipt, trace = inputs()
    for cohort in receipt["cohorts"]:
        started = verifier.timestamp(cohort["started_at"])
        for index, call in enumerate(cohort["calls"]):
            call["accepted_at"] = (started + timedelta(seconds=index * 2)).isoformat()
            call["completed_at"] = (started + timedelta(seconds=index * 2 + 1)).isoformat()
    report = verifier.verify(manifest, receipt, trace, now=NOW)
    assert report["errors"] == ["concurrency_five_mixed_model_overlap_not_proved"]


@pytest.mark.parametrize("mode", ["missing", "raw-sdk", "wrong-operation", "wrong-tool", "missing-skills"])
def test_raw_sdk_or_incomplete_librechat_evidence_never_qualifies_customer_path(mode):
    manifest, receipt, trace = inputs()
    if mode == "missing":
        trace = None
    elif mode == "raw-sdk":
        trace["producer"] = "mcp-sdk"
    elif mode == "wrong-operation":
        trace["operations"][0]["operation_id"] = str(uuid4())
    elif mode == "wrong-tool":
        trace["operations"][0]["tool_name"] = "invented_tool"
    else:
        trace["operations"][0]["loaded_skills"] = []
    assert not verifier.verify(manifest, receipt, trace, now=NOW)["ready"]


def test_discovery_cannot_silently_drop_a_team_app_or_misidentify_its_runtime():
    identity, discovery, policy, client = metadata()
    discovery["apps"].pop(0)
    with pytest.raises(verifier.ReceiptError, match="team_grant_missing_from_discovery"):
        verifier.build_manifest(identity, discovery, policy, client)
    identity, discovery, policy, client = metadata()
    identity["runtime_images"]["protein-fixture"] = "sha256:" + "b" * 64
    with pytest.raises(verifier.ReceiptError, match="app_runtime_image_missing_or_changed"):
        verifier.build_manifest(identity, discovery, policy, client)


def test_expired_or_future_evidence_and_known_credential_markers_are_rejected():
    manifest, receipt, trace = inputs()
    assert not verifier.verify(manifest, receipt, trace, now=NOW + timedelta(days=2))["ready"]
    assert not verifier.verify(manifest, receipt, trace, now=NOW - timedelta(days=1))["ready"]
    trace["operations"][0]["conversation_id"] = "Bearer synthetic-no-secret"
    assert verifier.verify(manifest, receipt, trace, now=NOW)["errors"] == ["credential_marker_present"]


def test_output_artifacts_and_cosmos_exclusion_are_not_optional():
    manifest, receipt, trace = inputs()
    receipt["cohorts"][0]["calls"][0]["artifact_validated"] = False
    assert verifier.verify(manifest, receipt, trace, now=NOW)["errors"] == ["terminal_output_not_validated"]
    manifest, receipt, trace = inputs()
    receipt["cohorts"][0]["calls"][0]["app_id"] = "cosmos-excluded-fixture"
    assert verifier.verify(manifest, receipt, trace, now=NOW)["errors"] == ["unexpected_or_duplicate_scenario"]


def test_cli_without_receipts_reports_missing_coverage_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    argv = ["verify_receipt.py"]
    for option, value in zip(
        ("release-identity", "discovery", "team-policy", "client-identity"), metadata(), strict=True
    ):
        path = tmp_path / f"{option}.json"
        path.write_text(json.dumps(value))
        argv.extend(["--" + option, str(path)])
    monkeypatch.setattr(sys, "argv", argv)
    assert verifier.main() == 2
    report = json.loads(capsys.readouterr().out)
    assert not report["ready"] and len(report["required_scenarios"]) == 11


def test_gateway_and_librechat_image_digests_must_be_in_exact_release():
    identity, discovery, policy, client = metadata()
    identity["runtime_images"].pop("control-plane")
    with pytest.raises(verifier.ReceiptError, match="deployed_gateway_or_client_image_missing"):
        verifier.build_manifest(identity, discovery, policy, client)

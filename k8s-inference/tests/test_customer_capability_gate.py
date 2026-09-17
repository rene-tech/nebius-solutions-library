from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "customer_capability_gate",
    ROOT / "acceptance/customer-readiness/capability_gate.py",
)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

NOW = datetime(2026, 9, 15, 18, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64
IDENTITY = {
    "source_revision": "b" * 40,
    "runtime_images": {"control-plane": DIGEST, "cosmos": "sha256:" + "c" * 64},
    "configuration_sha256": "sha256:" + "d" * 64,
    "model_revision": "nvidia/Cosmos3-Nano@immutable",
    "client_build_sha256": "sha256:" + "e" * 64,
    "tenant_policy_sha256": "sha256:" + "f" * 64,
    "public_endpoint": "https://inference.example.test/mcp",
    "public_tool_catalog_sha256": "sha256:" + "1" * 64,
}


def scenario(identifier: str, operation: str, input_form: str, output_form: str):
    return {
        "id": identifier,
        "operation": operation,
        "input_form": input_form,
        "output_form": output_form,
        "client_path": "librechat+mcp",
        "workload_states": ["hot", "scale-from-zero"],
        "integrations": ["robotics-tenant"],
        "required_observability": ["containers", "logs", "metrics", "runs", "usage"],
        "min_clean_cohorts": 2,
    }


def manifest():
    return {
        "schema": gate.MANIFEST_SCHEMA,
        "app_id": "cosmos3-nano",
        "release_identity": IDENTITY,
        "max_evidence_age_seconds": 86400,
        "capabilities": [
            {
                "id": "text-to-video",
                "description": "Text prompt to a validated MP4 artifact.",
                "advertised": True,
                "scenarios": [
                    scenario("t2v-public", "text-to-video", "json-text", "mp4")
                ],
            },
            {
                "id": "video-to-video",
                "description": "Video-conditioned generation through both customer input transports.",
                "advertised": True,
                "scenarios": [
                    scenario("v2v-url", "video-to-video", "https-mp4-url", "mp4"),
                    scenario(
                        "v2v-upload", "video-to-video", "client-local-mp4-upload", "mp4"
                    ),
                ],
            },
            {
                "id": "lerobot-augmentation",
                "description": "LeRobot dataset to an aligned augmented LeRobot dataset.",
                "advertised": True,
                "scenarios": [
                    scenario(
                        "lerobot-lighting",
                        "augment-lighting",
                        "lerobot-v3",
                        "lerobot-v3",
                    )
                ],
            },
        ],
        "requested_capability_ids": ["lerobot-augmentation", "video-to-video"],
        "reduced_scope_decisions": [],
    }


def evidence(capability: str, selected: dict, **updates):
    value = {
        "schema": gate.EVIDENCE_SCHEMA,
        "evidence_id": f"evidence-{capability}-{selected['id']}",
        "app_id": "cosmos3-nano",
        "capability_id": capability,
        "scenario_id": selected["id"],
        "operation": selected["operation"],
        "input_form": selected["input_form"],
        "output_form": selected["output_form"],
        "client_path": selected["client_path"],
        "observed_at": NOW.isoformat(),
        "release_identity": IDENTITY,
        "fixture_sha256": "sha256:" + "2" * 64,
        "outcome": "passed",
        "clean_cohorts": 2,
        "public_endpoint_exercised": True,
        "direct_runtime_only": False,
        "terminal_outcome_validated": True,
        "artifact_validated": True,
        "tenant_policy_exercised": True,
        "workload_states": selected["workload_states"],
        "integrations": selected["integrations"],
        "observed_surfaces": selected["required_observability"],
        "notes": [],
    }
    value.update(updates)
    return value


def test_t2v_success_cannot_qualify_v2v_lerobot_app_or_platform():
    raw = manifest()
    t2v = raw["capabilities"][0]
    verdict = gate.evaluate(
        raw, [evidence(t2v["id"], t2v["scenarios"][0])], evaluated_at=NOW
    )
    states = {item["capability_id"]: item["state"] for item in verdict["capabilities"]}
    assert states == {
        "text-to-video": "qualified",
        "video-to-video": "untested",
        "lerobot-augmentation": "untested",
    }
    assert verdict["verdict"] == "not-ready" and not verdict["ready"]


def test_direct_runtime_or_single_cohort_is_only_partial_public_evidence():
    raw = manifest()
    selected = raw["capabilities"][1]["scenarios"][0]
    receipt = evidence(
        "video-to-video", selected, direct_runtime_only=True, clean_cohorts=1
    )
    verdict = gate.evaluate(raw, [receipt], evaluated_at=NOW)
    scenario_row = verdict["capabilities"][1]["scenarios"][0]
    assert scenario_row["state"] == "partial"
    assert any(
        "public customer endpoint" in reason for reason in scenario_row["reasons"]
    )
    assert any(
        "clean unchanged-release cohorts" in reason
        for reason in scenario_row["reasons"]
    )


def test_release_drift_and_expired_evidence_are_stale():
    raw = manifest()
    selected = raw["capabilities"][0]["scenarios"][0]
    drifted = evidence(
        "text-to-video",
        selected,
        release_identity={**IDENTITY, "model_revision": "changed"},
    )
    verdict = gate.evaluate(raw, [drifted], evaluated_at=NOW)
    assert verdict["capabilities"][0]["state"] == "stale"
    expired = evidence(
        "text-to-video", selected, observed_at=(NOW - timedelta(days=2)).isoformat()
    )
    verdict = gate.evaluate(raw, [expired], evaluated_at=NOW)
    assert verdict["capabilities"][0]["state"] == "stale"


def test_validity_ends_when_the_oldest_contributing_receipt_expires():
    raw = manifest()
    first = raw["capabilities"][0]["scenarios"][0]
    second = raw["capabilities"][1]["scenarios"][0]
    oldest = NOW - timedelta(hours=23)
    receipts = [
        evidence("text-to-video", first, observed_at=oldest.isoformat()),
        evidence("video-to-video", second),
    ]
    verdict = gate.evaluate(raw, receipts, evaluated_at=NOW)
    assert datetime.fromisoformat(verdict["valid_until"]) == oldest + timedelta(days=1)


def test_future_dated_evidence_cannot_extend_a_claim():
    raw = manifest()
    selected = raw["capabilities"][0]["scenarios"][0]
    receipt = evidence(
        "text-to-video", selected, observed_at=(NOW + timedelta(seconds=1)).isoformat()
    )
    with pytest.raises(gate.CapabilityGateError, match="cannot be in the future"):
        gate.evaluate(raw, [receipt], evaluated_at=NOW)


def test_all_exact_scenarios_qualify_only_complete_app():
    raw = manifest()
    receipts = [
        evidence(capability["id"], selected)
        for capability in raw["capabilities"]
        for selected in capability["scenarios"]
    ]
    verdict = gate.evaluate(raw, receipts, evaluated_at=NOW)
    assert verdict["ready"] and verdict["verdict"] == "customer-ready"
    assert {item["state"] for item in verdict["capabilities"]} == {"qualified"}


def test_explicit_reduced_scope_decision_cannot_hide_still_advertised_capability():
    raw = manifest()
    raw["reduced_scope_decisions"] = [
        {
            "capability_id": "lerobot-augmentation",
            "decision_id": "customer-decision-1",
            "decided_by": "customer-owner@example.test",
            "decided_at": NOW.isoformat(),
            "rationale": "Customer explicitly removed LeRobot augmentation from this handoff.",
        }
    ]
    receipts = [
        evidence(capability["id"], selected)
        for capability in raw["capabilities"][:2]
        for selected in capability["scenarios"]
    ]
    verdict = gate.evaluate(raw, receipts, evaluated_at=NOW)
    # It remains advertised, therefore release scope cannot silently remove it.
    assert not verdict["ready"]
    raw["capabilities"][2]["advertised"] = False
    verdict = gate.evaluate(raw, receipts, evaluated_at=NOW)
    assert verdict["ready"] and verdict["excluded_by_explicit_decision"] == [
        "lerobot-augmentation"
    ]


def test_future_scope_decision_cannot_reduce_the_customer_contract():
    raw = manifest()
    raw["capabilities"][2]["advertised"] = False
    raw["reduced_scope_decisions"] = [
        {
            "capability_id": "lerobot-augmentation",
            "decision_id": "future-decision",
            "decided_by": "customer-owner@example.test",
            "decided_at": (NOW + timedelta(seconds=1)).isoformat(),
            "rationale": "This decision does not exist yet.",
        }
    ]
    with pytest.raises(
        gate.CapabilityGateError,
        match="decision timestamp cannot be in the future",
    ):
        gate.evaluate(raw, [], evaluated_at=NOW)


def test_failure_is_not_overridden_by_older_success():
    raw = manifest()
    selected = raw["capabilities"][0]["scenarios"][0]
    passed = evidence(
        "text-to-video", selected, observed_at=(NOW - timedelta(minutes=1)).isoformat()
    )
    failed = evidence(
        "text-to-video",
        selected,
        evidence_id="newer-failure",
        observed_at=NOW.isoformat(),
        outcome="failed",
        clean_cohorts=0,
        terminal_outcome_validated=False,
        artifact_validated=False,
    )
    verdict = gate.evaluate(raw, [passed, failed], evaluated_at=NOW)
    assert verdict["capabilities"][0]["state"] == "failed"


def test_equal_timestamp_failure_is_not_masked_by_receipt_order():
    raw = manifest()
    selected = raw["capabilities"][0]["scenarios"][0]
    passed = evidence("text-to-video", selected, evidence_id="same-time-pass")
    failed = evidence(
        "text-to-video",
        selected,
        evidence_id="same-time-failure",
        outcome="failed",
        clean_cohorts=0,
        terminal_outcome_validated=False,
        artifact_validated=False,
    )
    for receipts in ([passed, failed], [failed, passed]):
        verdict = gate.evaluate(raw, receipts, evaluated_at=NOW)
        assert verdict["capabilities"][0]["state"] == "failed"

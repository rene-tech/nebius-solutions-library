from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from test_fast_start_benchmark import BENCHMARK, attempt
from test_fast_start_identity import bound_attempt

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location(
    "project_fast_start_evidence_test",
    ROOT / "project_fast_start_evidence.py",
)
assert SPEC and SPEC.loader
PROJECTOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROJECTOR
SPEC.loader.exec_module(PROJECTOR)


def test_receipt_projects_to_controller_wire_shape_without_inventing_qualification() -> (
    None
):
    receipt = BENCHMARK.build_receipt(
        [attempt(index, model_start=90 + index) for index in range(1, 4)],
        generated_at="2026-09-02T13:00:00Z",
    )

    projected = PROJECTOR.project_receipts([receipt], valid_for_days=30)

    evidence = projected["qwen3-8b"][0]
    assert evidence["receiptDigest"] == f"sha256:{receipt['receipt_digest']}"
    assert (
        evidence["compatibilityTupleDigest"]
        == f"sha256:{receipt['compatibility_tuple_digest']}"
    )
    assert evidence["compatibilityTupleComplete"] is True
    assert evidence["measurementBasis"] == "CapacityAvailableToSemanticReady"
    assert (
        evidence["templateDigest"]
        == receipt["compatibility_tuple"]["runtime_template_digest"]
    )
    assert evidence["poolRef"] == "h100-reserved-8x"
    assert evidence["validUntil"] == "2026-10-02T13:00:00Z"
    assert [sample["modelStartSeconds"] for sample in evidence["samples"]] == [
        91,
        92,
        93,
    ]
    assert receipt["qualification"]["state"] == "exploratory"


def test_incomplete_tuple_is_preserved_as_non_qualifying_controller_evidence() -> None:
    receipt = BENCHMARK.build_receipt(
        [
            attempt(index, model_start=90, tuple_overrides={"driver_version": None})
            for index in range(1, 21)
        ],
        generated_at="2026-09-02T13:00:00Z",
    )

    projected = PROJECTOR.project_receipts([receipt], valid_for_days=30)
    evidence = projected["qwen3-8b"][0]

    assert receipt["qualification"]["state"] == "incomplete-evidence"
    assert receipt["qualification"]["qualified_level"] is None
    assert evidence["compatibilityTupleComplete"] is False


def test_failed_attempt_is_retained_as_a_null_model_start_sample() -> None:
    failed = attempt(3, status="FAIL")
    receipt = BENCHMARK.build_receipt(
        [attempt(1), attempt(2), failed],
        generated_at="2026-09-02T13:00:00Z",
    )

    projected = PROJECTOR.project_receipts([receipt], valid_for_days=7)

    assert projected["qwen3-8b"][0]["samples"][-1]["modelStartSeconds"] is None


def test_v2_receipt_projects_one_bound_identity_while_v1_is_explicitly_legacy() -> None:
    v2 = BENCHMARK.build_receipt(
        [bound_attempt(index) for index in range(1, 4)],
        generated_at="2026-09-02T13:00:00Z",
    )
    v1 = BENCHMARK.build_receipt(
        [attempt(index) for index in range(1, 4)],
        generated_at="2026-09-02T13:00:00Z",
    )

    bound = PROJECTOR.project_receipts([v2], valid_for_days=30)["qwen3-8b"][0]
    legacy = PROJECTOR.project_receipts([v1], valid_for_days=30)["qwen3-8b"][0]

    assert bound["identityState"] == "Bound"
    assert bound["identityDigest"] == v2["evidence_identity_digest"]
    assert bound["identity"] == v2["evidence_identity"]
    assert bound["capacityType"] == "regular"
    assert (
        bound["mechanismConfigDigest"]
        == bound["identity"]["cache"]["mechanismConfigDigest"]
    )
    assert legacy["identityState"] == "LegacyUnbound"
    assert (
        "identity" not in legacy
        and "identityDigest" not in legacy
        and "capacityType" not in legacy
    )

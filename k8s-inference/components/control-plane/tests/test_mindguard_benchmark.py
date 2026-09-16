from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from fs2_serve.mindguard import REVISIONS


def harness() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "models/mindguard/benchmark.py"
    spec = importlib.util.spec_from_file_location("mindguard_benchmark_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mindeval_completed_transcript_expands_only_user_prefixes(tmp_path: Path) -> None:
    path = tmp_path / "transcripts.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "evaluation-1",
                "messages": [
                    {"role": "assistant", "content": "How have you been?"},
                    {"role": "user", "content": "I feel stressed."},
                    {"role": "assistant", "content": "What has been happening?"},
                    {"role": "user", "content": "Work has been busy."},
                    {"role": "assistant", "content": "That sounds difficult."},
                ],
                "expected_by_user_index": {"3": {"safety": "safe", "categories": []}},
            }
        )
        + "\n"
    )
    cases = harness().load_cases("mindeval", "mindguard-4b", path)
    assert [len(case["messages"]) for case in cases] == [2, 4]
    assert cases[0]["expected_safety"] is None
    assert cases[1]["expected_safety"] == "safe"


def test_metric_denominators_keep_errors_visible_and_exclude_warmups() -> None:
    successful = {
        "phase": "warm",
        "expected_safety": "safe",
        "expected_categories": [],
        "assessment": {"status": "completed", "safety": "unsafe", "categories": ["S1"], "latency_ms": 100.0},
    }
    failed = {
        "phase": "warm",
        "expected_safety": "safe",
        "expected_categories": [],
        "assessment": {"status": "error", "safety": None, "categories": [], "latency_ms": 1000.0},
    }
    warmup = {**successful, "phase": "warmup"}
    metrics = harness().summarize([warmup, successful, failed], 2.0)
    assert metrics["warm_requests"] == 2
    assert metrics["failed"] == 1
    assert metrics["completion_rate"] == 0.5
    assert metrics["false_positive_rate_among_completed"] == 1
    assert metrics["completed_requests_per_second"] == 0.5
    assert metrics["latency_ms_mean"] == 100
    assert metrics["auroc"] is None


@pytest.mark.parametrize("shape", ["report", "wrapped", "list"])
def test_original_completed_report_maps_patient_role_without_fake_labels(tmp_path: Path, shape: str) -> None:
    report = {"run": {"id": "run-1", "status": "completed", "state": {"transcript": [
        {"role": "patient", "content": "Hello", "seed": True},
        {"role": "clinician", "content": "How are you?", "completion": {"provider": "example"}},
        {"role": "patient", "content": "I am nervous."},
    ], "judge": {"score": 5}}}}
    path = tmp_path / "report.json"
    document = report if shape == "report" else {"report": report}
    path.write_text(json.dumps([document] if shape == "list" else document))
    cases = harness().load_cases("mindeval", "mindguard-4b", path)
    assert [case["id"] for case in cases] == ["run-1-user-0", "run-1-user-2"]
    assert [message["role"] for message in cases[1]["messages"]] == ["user", "assistant", "user"]
    assert all(case["expected_safety"] is None for case in cases)


def test_incomplete_mindeval_report_is_not_benchmarked_as_completed(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"run": {"id": "run-1", "status": "running"}}))
    with pytest.raises(ValueError, match="completed run"):
        harness().load_cases("mindeval", "mindguard-4b", path)


def test_unlabeled_transcripts_never_get_an_accuracy_estimate() -> None:
    row = {
        "phase": "warm",
        "expected_safety": None,
        "expected_categories": None,
        "assessment": {"status": "completed", "safety": "safe", "categories": [], "latency_ms": 80.0},
    }
    metrics = harness().summarize([row], 1.0)
    assert metrics["accuracy_among_completed"] is None
    assert metrics["category_exact_match_among_completed"] is None
    assert metrics["false_positive_rate_among_completed"] is None


def test_api_revisions_match_serving_and_artifact_locks() -> None:
    root = Path(__file__).resolve().parents[3] / "models/mindguard"
    lock = json.loads((root / "public-models.lock.json").read_text())
    assert {model: item["revision"] for model, item in lock["models"].items()} == REVISIONS
    for model, revision in REVISIONS.items():
        manifest = json.loads((root / "artifacts" / f"{model}.json").read_text())
        assert manifest["revision"] == revision
        assert manifest["repo_id"] == lock["models"][model]["repo_id"]

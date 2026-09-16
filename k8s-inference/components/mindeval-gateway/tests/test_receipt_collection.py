import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("classification_ok", [True, False])
async def test_collection_never_submits_or_promotes_interrupted_run(tmp_path, monkeypatch, classification_ok):
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("receipt_collector", scripts / "collect_existing_rehearsal.py")
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    source = {
        "base_url": "https://fixture.test",
        "label": "interrupted",
        "image_provenance": {"gateway": "gateway-digest", "workshop": "workshop-digest"},
        "checks": [{"name": "model_grants_and_fixed_judge", "judge": "judge"}],
        "repetitions": [{"run_ids": {"0": ["fixture-run"]}}],
    }
    summary = tmp_path / "original.json"
    summary.write_text(json.dumps(source))
    original = summary.read_bytes()
    keys = tmp_path / "fixture-keys.json"
    keys.write_text(json.dumps([f"fixture-token-{i}" for i in range(10)]))
    calls, saved = [], {}
    criteria = collector.validate_completed.__globals__["CRITERIA"]
    row = {
        "id": "fixture-run",
        "status": "completed",
        "state": {
            "benchmark_eligible": True,
            "judgment": {"model": "judge", "judgment": dict.fromkeys(criteria, 4)},
            "transcript": [{"role": "patient" if i % 2 == 0 else "clinician"} for i in range(5)],
            "classification": {
                "status": "completed" if classification_ok else "unavailable",
                "input_user_turns": 3,
                "evaluated_user_turns": 3,
                "assessments": [{"status": "completed", "coverage": {"truncated": False}} for _ in range(3)],
            },
        },
    }

    class FakeClient:
        def __init__(self, *args):
            self.client = self

        async def aclose(self):
            pass

        async def request(self, method, path, **kwargs):
            calls.append((method, path))
            assert method == "GET" and kwargs["team"] == 0
            if path.endswith("/report"):
                return {"run": row}, 200
            if path.endswith("/events"):
                return {"data": []}, 200
            return row, 200

        def save(self, name, data):
            saved[name] = data

    monkeypatch.setattr(collector, "Rehearsal", FakeClient)
    args = SimpleNamespace(
        summary=summary, keys_file=keys, output=tmp_path, insecure=False, ca_file=None, timeout_seconds=1
    )
    assert await collector.collect(args) is classification_ok
    assert len(calls) == 3 and all(method == "GET" for method, _ in calls)
    result = saved["collection-summary.json"]
    assert result["release_acceptance"] is False
    assert result["strict_existing_run_validation_passed"] is classification_ok
    assert len(result["failures"]) == (0 if classification_ok else 1)
    assert saved["collected-reports.json"][0]["report"]["run"] == row
    assert summary.read_bytes() == original

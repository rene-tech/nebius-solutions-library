"""Offline guards for the bounded live experiment and credential-free export."""

import json
from pathlib import Path

import yaml

from export import documents

ROOT = Path(__file__).parent


def test_manifest_scope_is_exactly_one_cpu_and_one_existing_h100():
    for model in ("phenoage", "altumage"):
        pod = yaml.safe_load((ROOT / f"{model}-pod.yaml").read_text())
        assert pod["kind"] == "Pod"
        assert pod["metadata"]["namespace"] == "fs2-models"
        assert pod["metadata"]["name"] == f"fs2-aging-20260908-{model}-r01"
        assert pod["spec"]["activeDeadlineSeconds"] == 1800
        assert pod["spec"]["restartPolicy"] == "Never"
        assert len(pod["spec"]["containers"]) == 1
        runtime = pod["spec"]["containers"][0]
        assert "@sha256:" in runtime["image"]
        assert "command" not in runtime  # Original, digest-pinned entrypoint.
        assert runtime["resources"]["requests"].get("nvidia.com/gpu", "0") == (
            "0" if model == "phenoage" else "1"
        )
        assert runtime["readinessProbe"]["httpGet"]["path"] == "/v1/health/ready"


def test_partial_event_records_and_complete_report_remain_separately_parseable():
    recorded = '{"event":"native_http_response","status":200}\n{\n"outcome":"passed"\n}\n'
    assert list(documents(recorded)) == [
        {"event": "native_http_response", "status": 200}, {"outcome": "passed"},
    ]


def test_phenoage_export_retains_exact_reference_and_no_gpu_or_snapshot_claim():
    receipt = json.loads((ROOT / "phenoage-r01.json").read_text())
    assert receipt["outcome"] == "passed"
    assert receipt["model"]["gpu_snapshot"] == "not-applicable-cpu"
    assert receipt["gpu"] is None
    values = [item["body"]["predictions"][0]["phenotypic_age_years"] for item in receipt["native_http_predictions"]]
    assert abs(values[0] - 41.90792243377999) < 1e-10
    assert values[0] != values[1]
    assert receipt["cleanup"]["uid_verified"]
    assert receipt["pod"]["restart_count"] == 0
    assert [batch["repeats"] for batch in receipt["module_benchmarks"][0]["batches"]] == [20, 20]

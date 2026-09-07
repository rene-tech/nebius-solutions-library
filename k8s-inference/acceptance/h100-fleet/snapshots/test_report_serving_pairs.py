import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "report_serving_pairs", Path(__file__).with_name("report_serving_pairs.py")
)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def receipt():
    runs = []
    for repetition in range(1, 4):
        for mode in ("normal", "restore"):
            runs.append(
                {
                    "mode": mode,
                    "repetition": repetition,
                    "status": "passed",
                    "gpu_pod_deleted": True,
                    "pod_uid": f"{mode}-{repetition}",
                    "created_request_at": "2026-09-07T10:00:00+00:00",
                    "pod_created_at": "2026-09-07T10:00:01Z",
                    "container_started_at": "2026-09-07T10:00:03Z",
                    "ready_observed_at": "2026-09-07T10:00:13+00:00",
                    "both_full_outputs_observed_at": "2026-09-07T10:00:19+00:00",
                    "pod_create_request_to_ready_seconds": 13.25,
                    "semantics": {"requests": [{"passed": True}, {"passed": True}]},
                    "private_credentials": "not-exported",
                }
            )
    return {"status": "passed", "model": "qwen3-8b", "cache": "retained", "runs": runs}


def test_uses_independent_clocks_and_does_not_imply_production_selection():
    result = report.project(receipt())
    assert not result["production_selectable"]
    assert (
        result["statistics"]["restore"]["container_start_to_ready_seconds"][
            "median_seconds"
        ]
        == 10
    )
    assert (
        result["statistics"]["normal"]["pod_create_request_to_ready_seconds"][
            "median_seconds"
        ]
        == 13.25
    )
    assert result["runs"][0]["container_start_to_both_full_outputs_seconds"] == 16
    assert "private_credentials" not in result["runs"][0]


@pytest.mark.parametrize("change", ["missing", "invalid-output", "not-released"])
def test_incomplete_qualification_does_not_produce_success(change):
    source = receipt()
    if change == "missing":
        source["runs"].pop()
    elif change == "invalid-output":
        source["runs"][0]["semantics"]["requests"][0]["passed"] = False
    else:
        source["runs"][0]["gpu_pod_deleted"] = False
    with pytest.raises(ValueError):
        report.project(source)

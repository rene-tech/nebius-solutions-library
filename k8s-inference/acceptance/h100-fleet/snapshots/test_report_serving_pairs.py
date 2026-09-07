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


def test_genmol_reports_original_fixture_scope_without_claiming_unseen_inputs():
    source = receipt()
    source["model"] = "genmol"
    result = report.project(source)
    assert "original two pinned QED and LogP" in result["semantic_scope"]
    assert all("First unseen input" not in note for note in result["clock_notes"])


def test_openfold3_reports_standalone_original_input_and_native_launch_scope():
    source = receipt()
    source["model"] = "openfold3"
    result = report.project(source)
    assert "standalone Preview2" in result["semantic_scope"]
    assert "not unseen-input evidence" in result["semantic_scope"]
    assert "OpenBind" in result["semantic_scope"]
    assert any("native bash activation" in note for note in result["clock_notes"])


def test_segment_reports_original_nonclinical_masks_without_unseen_claim():
    source = receipt()
    source["model"] = "nv-segment-ct"
    result = report.project(source)
    assert "synthetic non-clinical CT masks" in result["semantic_scope"]
    assert "not unseen-input evidence" in result["semantic_scope"]


def test_sdxl_reports_exact_original_seed_scope_without_unseen_claim():
    source = receipt()
    source["model"] = "sdxl"
    result = report.project(source)
    assert "512x512 prompts" in result["semantic_scope"]
    assert "2407 and 2408" in result["semantic_scope"]
    assert "not unseen-input evidence" in result["semantic_scope"]


def test_diffdock_reports_exact_receptor_ligand_and_seed_scope():
    source = receipt()
    source["model"] = "diffdock"
    result = report.project(source)
    assert "RCSB 1UBQ receptor" in result["semantic_scope"]
    assert "aspirin ligand" in result["semantic_scope"]
    assert "2370 and 2371" in result["semantic_scope"]
    assert "not unseen-input evidence" in result["semantic_scope"]


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

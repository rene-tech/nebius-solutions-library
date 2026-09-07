"""ESM reports retain restore clocks, rather than donor loader measurements."""

import copy
import json
from pathlib import Path

import pytest

from report_pairs import esm_report


def inputs():
    prior = json.loads((Path(__file__).resolve().parent.parent / "protenix-v2-h100-20260907.json").read_text())
    receipt = {"status": "passed", "cache": "retained cache", "runs": []}
    for row in prior["runs"]:
        source = copy.deepcopy(row)
        source.update(status="passed", creation_to_ready_observed_seconds=row["pod_create_request_to_ready_seconds"],
                      ready={"load_seconds": 20.0 if row["mode"] == "normal" else 9999.0})
        receipt["runs"].append(source)
    manifest = {"bundle_sha256": "a" * 64, "bytes": 100, "file_count": 2, "compatibility": {}}
    config = {"model_id": "esmfold2-fast", "bundle_id": "esm-fast-test", "bundle_path": "esm-fast-test",
              "cli_sha256": "b" * 64}
    cases = [{"raw_sha256": "c" * 64, "seed": 101}, {"raw_sha256": "d" * 64, "seed": 102}]
    return receipt, manifest, config, cases


def test_esm_exact_profile_and_independent_restore_clock():
    report = esm_report(*inputs())
    assert report["model_id"] == "esmfold2-fast"
    assert report["stage_id"] == "fold"
    assert report["production_selectable"] is False
    assert report["bundle"]["id"] == "esm-fast-test"
    assert report["parameters"]["num_sampling_steps"] == 200
    assert all(row["normal_model_loader_seconds"] is None for row in report["runs"] if row["mode"] == "restore")
    assert all(row["normal_model_loader_seconds"] == 20 for row in report["runs"] if row["mode"] == "normal")
    assert report["statistics"]["restore"]["container_start_to_ready_seconds"]["max_seconds"] < 9999


def test_unfinished_trials_are_not_promoted():
    receipt, *rest = inputs()
    receipt["runs"][0]["gpu_pod_deleted"] = False
    with pytest.raises(ValueError, match="release"):
        esm_report(receipt, *rest)

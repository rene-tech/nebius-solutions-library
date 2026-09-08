"""Offline fixture and evidence guards; these tests never contact the cluster."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

import scientific_app_clone as clone


def test_prepare_keeps_the_exact_existing_accepted_scientific_input():
    plan, request, declarations, fragment = clone.fixture()
    assert plan["source_model_id"] == fragment["model_id"] == "protenix-v2"
    assert (
        plan["fixture_sha256"]
        == "0f1358c242cb762855143b289353fa3fbc85f72bbf5d5e027f7aa4333c49bcf7"
    )
    assert (
        plan["request_sha256"]
        == "34cce20d2e5fcadb68a9804788da19462e7dac7d3d1a5f7e6d174e9b4ebdeb3a"
    )
    assert request["parameters"] == {
        "checkpoint": "protenix-v2",
        "model_seeds": [101],
        "msa_mode": "none",
        "sample_count": 1,
    }
    assert (
        len(declarations) == 2
        and plan["logical_operations"] == plan["parallel_clients"] == 1
    )


def test_prepare_only_needs_no_credential_read_or_network(tmp_path, capsys):
    assert (
        clone.run(
            SimpleNamespace(
                prepare_only=True, release="test", access_bundle=tmp_path / "absent"
            )
        )
        == 0
    )
    assert '"mutations_performed": false' in capsys.readouterr().out


def test_source_setting_comparison_ignores_live_enforcement_not_desired_revision():
    source = {
        "app_revision": 1,
        "display_name": "Protenix",
        "academic_required": False,
        "scientific": {
            "desired": {"revision": 1, "paused": False},
            "enforcement": {"active_runs": 0},
        },
    }
    changed_observation = deepcopy(source)
    changed_observation["scientific"]["enforcement"]["active_runs"] = 1
    assert clone.settings_identity(source) == clone.settings_identity(
        changed_observation
    )
    changed_observation["scientific"]["desired"]["paused"] = True
    assert clone.settings_identity(source) != clone.settings_identity(
        changed_observation
    )


def test_resumed_app_keeps_real_failed_history_and_adds_exactly_one_run():
    previous = {"operation": {"id": "failed-r02", "status": "failed"}}
    new = {"operation": {"id": "new-r03", "status": "succeeded"}}
    clone.check_run_increment(["failed-r02"], [new, previous], "new-r03")
    for rows in ([new], [new, new, previous], [previous]):
        with pytest.raises(clone.public.AcceptanceError):
            clone.check_run_increment(["failed-r02"], rows, "new-r03")

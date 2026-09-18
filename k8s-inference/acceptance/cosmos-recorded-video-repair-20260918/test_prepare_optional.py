import copy
import json
from pathlib import Path

import prepare_optional as p
import pytest
from test_prepare_presets import fixture


def measured():
    folder = Path(__file__).with_name("warmed-snapshot")
    return json.loads((folder / "bundle.json").read_bytes()), (folder / "qualification.json").read_bytes()


def test_registration_retains_never_and_keeps_strict_trial_separate():
    values = fixture()
    original = copy.deepcopy(values)
    snapshot, report = measured()
    envelope, bundles, ordinary, strict = p.extend(*values, snapshot, report)
    assert values == original
    assert ordinary["spec"]["cache"] == original[2]["spec"]["cache"] == {"snapshotPreference": "Never"}
    assert "snapshotRef" not in ordinary["spec"]["cache"]
    assert strict["spec"]["cache"]["snapshotPreference"] == "Require"
    assert strict["spec"]["cache"]["snapshotRef"]["name"] == snapshot["bundle_id"]
    registry = envelope["qualifications"]["cosmos3-nano"]["gpuSnapshotBundles"]
    assert registry["historical"] == {"keep": True}
    assert registry[snapshot["bundle_id"]] == snapshot
    assert bundles[:-1] == original[1]


def test_changed_report_cannot_inherit_snapshot_qualification():
    snapshot, report = measured()
    with pytest.raises(ValueError, match="bind"):
        p.extend(*fixture(), snapshot, report + b"\n")


def test_existing_snapshot_selection_requires_coordination():
    values = fixture()
    values[2]["spec"]["cache"]["snapshotPreference"] = "Prefer"
    with pytest.raises(ValueError, match="coordinate"):
        p.extend(*values, *measured())

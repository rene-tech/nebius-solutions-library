"""A failed client boundary must never become scientific completion evidence."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "remediation_experience_export", Path(__file__).with_name("export_experience.py")
)
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


@pytest.mark.parametrize("completed", [False, True])
def test_stopped_client_is_distinct_from_completed_science(tmp_path, monkeypatch, completed):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "sampler-completed.json").write_text("{}")
    output = tmp_path / "report.json"
    boundary = "2026-09-07T20:40:14Z"
    monkeypatch.setattr(sys, "argv", [
        "export", "--raw", str(raw), "--scientific-start", "2026-09-07T20:10:47Z",
        "--scientific-end" if completed else "--scientific-client-stopped-at", boundary,
        "--output", str(output),
    ])
    export.main()
    report = json.loads(output.read_text())
    assert report["scientific_completion_confirmed"] is completed
    assert report["scientific_completed_at"] == (boundary if completed else None)
    assert report["scientific_client_stopped_at"] == (None if completed else boundary)
    assert report["total"]["samples"] == 0

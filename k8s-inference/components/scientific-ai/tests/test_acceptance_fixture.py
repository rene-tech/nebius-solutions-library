"""Offline checks for the public-input qualification driver (no cloud credentials)."""

import hashlib
import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

from scientific_ai.contracts import FileRecord, GromacsRun

SOURCE = Path(__file__).parents[3] / "acceptance/serverless2-gromacs-20261011/protein_fixture.py"
SPEC = importlib.util.spec_from_file_location("protein_fixture", SOURCE)
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


def test_fixture_pins_input_and_preserves_protocol(tmp_path, monkeypatch):
    source = tmp_path / "benchMEM.tpr"
    source.write_bytes(b"unit-test fake TPR; never a live science fixture")
    with pytest.raises(ValueError, match="pinned public input"):
        fixture.fixture(source, 100000)
    monkeypatch.setattr(fixture, "TPR_SHA256", hashlib.sha256(source.read_bytes()).hexdigest())
    bundle, parameters, expected = fixture.fixture(source, 100000)
    assert fixture.fixture(source, 100000)[0] == bundle
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as archive:
        assert archive.getnames() == ["benchMEM.tpr"]
        assert archive.extractfile("benchMEM.tpr").read() == source.read_bytes()
    GromacsRun(
        job_id="benchmem",
        parameters=parameters,
        input_bundle=FileRecord(
            path="input.tar.gz",
            sha256=hashlib.sha256(bundle).hexdigest(),
            size_bytes=len(bundle),
        ),
    )
    assert parameters["jobs"][0]["steps"][1]["args"][-2:] == ["-update", "cpu"]
    assert expected["protocol"]["trajectory_output"] is False
    assert expected["protocol"]["duration_ps"] == 200


@pytest.mark.parametrize("steps", [-1, 0, True, 1, 2000001])
def test_fixture_rejects_unbounded_or_too_short_run(tmp_path, steps):
    with pytest.raises(ValueError, match="finite steps"):
        fixture.fixture(tmp_path / "not-read.tpr", steps)


def test_native_gate_detects_repeated_preparation_and_incomplete_steps():
    result = {
        "completed_steps": ["extend", "md"],
        "commands": [
            {"command": ["gmx", "convert-tpr"], "exit_code": 0, "wall_seconds": 1},
            {
                "command": ["gmx", "mdrun"],
                "checkpoint_step": 50000,
                "exit_code": 0,
                "wall_seconds": 80,
                "performance_ns_per_day": 108,
            },
        ],
    }
    receipt = {
        "steps": 50000,
        "expected": {
            "completed_steps": ["extend", "md"],
            "command_counts": {"convert-tpr": 1, "grompp": 0},
        },
    }
    assert fixture.validate_native(result, receipt)["final_step"] == 50000
    with pytest.raises(AssertionError, match="requested completion"):
        fixture.validate_native(result, {**receipt, "steps": 100000})
    result["commands"].insert(0, result["commands"][0])
    with pytest.raises(AssertionError, match="repeated"):
        fixture.validate_native(result, receipt)


def test_final_structure_must_be_complete_and_finite():
    atom = f"{1:5d}{'ALA':<5}{'CA':>5}{1:5d}{0.1:8.3f}{0.2:8.3f}{0.3:8.3f}"
    data = f"Synthetic parser test\n1\n{atom}\n1 1 1\n".encode()
    assert fixture.validate_coordinates(data, atoms=1)["particle_count"] == 1
    with pytest.raises(AssertionError, match="particle count"):
        fixture.validate_coordinates(data)
    with pytest.raises(AssertionError, match="non-finite"):
        fixture.validate_coordinates(data.replace(b"   0.100", b"     nan"), atoms=1)

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("run_case", Path(__file__).parents[1] / "run_case.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def case(tmp_path):
    manifest = {"schema": "fs2-four-engine-alanine-case/v1", "client_image": "client@sha256:" + "a" * 64,
                "engines": {}}
    for engine in runner.ENGINES:
        directory = tmp_path / "inputs" / engine
        directory.mkdir(parents=True)
        files = {"input.tar.gz": b"test fixture, not a real simulation",
                 "request.json": json.dumps({"jobs": [{"id": "canonical"}]}).encode()}
        for name, data in files.items():
            (directory / name).write_bytes(data)
        manifest["engines"][engine] = {"worker_image": engine + "@sha256:" + "b" * 64,
                                      "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    (tmp_path / "case.json").write_text(json.dumps(manifest))
    return tmp_path


@pytest.mark.parametrize("engine", runner.ENGINES)
@pytest.mark.parametrize("mode", ["hosted", "native"])
def test_exact_engine_and_identity_are_preserved(tmp_path, monkeypatch, engine, mode):
    monkeypatch.setenv("SCIENTIFIC_MODELS_API_KEY", "example-secret-never-an-argument")
    identity, command = runner.describe(case(tmp_path), engine, mode, tmp_path / "run", "GPU-example")
    assert identity["engine"] == engine
    assert "example-secret-never-an-argument" not in " ".join(command)
    if mode == "native":
        assert f"fs2_{engine}.worker" in command
        assert "device=GPU-example" in command
        assert "local" in command
    else:
        assert f"submit_{engine}_workflow" in command
        assert f"{engine}-input-bundle/v1" in command
        assert "--display-name" in command


def test_changed_input_is_rejected_before_submission(tmp_path):
    root = case(tmp_path)
    (root / "inputs/amber/request.json").write_text("{}")
    with pytest.raises(ValueError, match="changed"):
        runner.describe(root, "amber", "hosted", root / "run", "0")

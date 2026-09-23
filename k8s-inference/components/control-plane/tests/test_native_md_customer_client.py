import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = (
    Path(__file__).resolve().parents[3] / "models/molecular-dynamics/gromacs/qualification/run_image_customer_client.py"
)
SPEC = importlib.util.spec_from_file_location("native_md_customer_client", PATH)
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)


@pytest.mark.parametrize("model", ["gromacs", "gromacs-mpi", "lammps", "namd"])
def test_exact_customer_image_uses_engine_contract_and_non_argument_credential(model, tmp_path, monkeypatch):
    secret = "synthetic-test-credential"
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"secret": secret}))
    inputs = tmp_path / "fixture"
    inputs.mkdir()
    output = tmp_path / "receipt"
    image = "registry.test/client@sha256:" + "c" * 64
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(PATH),
            "--model",
            model,
            "--image",
            image,
            "--key-file",
            str(key),
            "--fixture",
            str(inputs),
            "--output",
            str(output),
            "--idempotency-key",
            "synthetic-md-client-test",
        ],
    )
    captured = {}

    def run(command, *, env):
        captured.update(command=command, env=env)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(client.subprocess, "run", run)
    with pytest.raises(SystemExit) as result:
        client.main()
    assert result.value.code == 0
    command = captured["command"]
    tool, family = client.MODEL_CONTRACTS[model]
    assert command[command.index("--model") + 1] == model
    assert command[command.index("--tool") + 1] == tool
    assert command[command.index("--entry-name") + 1] == f"{family}-inputs"
    assert command[command.index("--semantic-type") + 1] == f"{family}-input-bundle/v1"
    assert secret not in " ".join(command)
    assert captured["env"]["SCIENTIFIC_MODELS_API_KEY"] == secret
    assert image in command

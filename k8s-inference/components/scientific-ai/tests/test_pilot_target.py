"""Fleet selection must not silently reuse another cluster, key or storage prefix."""

import importlib.util
import json
from pathlib import Path

import pytest

from scientific_ai.gateway import Settings


@pytest.fixture
def pilot(monkeypatch):
    source = Path(__file__).parents[3] / "acceptance/serverless2-gromacs-20261011/pilot.py"
    monkeypatch.syspath_prepend(str(source.parent))
    spec = importlib.util.spec_from_file_location("pilot_under_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def target(tmp_path):
    value = {
        "operator_stack": str(tmp_path),
        "tfvars": str(tmp_path / "fleet.tfvars"),
        "kubeconfig": str(tmp_path / "kubeconfig"),
        "control_context": "new-control",
        "api_url": "https://api.example.test",
        "registry": "registry.example.test/new",
        "model_id": "scientific-gromacs-pilot",
        "tenant": "system",
        "key_names": {"admin": "development", "worker": "qa"},
        "contexts": {"south": "new-south", "north": "new-north"},
        "clusters": {"south": "mk8scluster-south", "north": "mk8scluster-north"},
        "storage_prefix": "runs/new-fleet-test",
    }
    path = tmp_path / "target.json"
    path.write_text(json.dumps(value))
    return path, value


def test_new_target_keys_and_storage_are_selected_explicitly(pilot, target, tmp_path, monkeypatch, capsys):
    path, value = target
    pilot.configure(path)
    commands = []

    def command(*args):
        commands.append(args)
        if args[0] == "./stack.sh":
            return json.dumps({"system": {"development": "test-admin-only", "qa": "test-inference-only"}})
        assert args[:2] == ("crane", "digest")
        assert args[2].startswith(value["registry"] + "/scientific-ai/")
        return "sha256:" + "a" * 64

    monkeypatch.setattr(pilot, "command", command)
    internal = tmp_path / "internal.json"
    original = {
        "tenants": {
            "demo": {
                "endpoint": "https://storage.example.test",
                "bucket": "internal-workspace",
                "region": "eu-north1",
                "access_key": "test-storage-access",
                "secret_key": "test-storage-secret",
                "execution_key": "old-key-must-not-copy",
                "prefix": "old-prefix-must-not-copy",
            }
        }
    }
    internal.write_text(json.dumps(original))
    internal.chmod(0o600)
    directory = tmp_path / "private"
    pilot.prepare(
        directory,
        "worker-r7",
        "api-r12",
        automatic_recovery=True,
        storage_config=internal,
        storage_tenant="demo",
    )
    settings = Settings.model_validate_json((directory / "config.json").read_bytes())
    assert settings.tenants["system"].execution_key.get_secret_value() == "test-inference-only"
    assert settings.tenants["system"].prefix == "runs/new-fleet-test"
    assert settings.tenants["system"].namespace == "tenant-system"
    assert settings.runtime.allowed_clusters == ("south", "north")
    assert set(settings.workload_identity.issuers) == {"south", "north"}
    assert pilot.CONTEXTS == value["contexts"]
    assert pilot.OPERATOR_ENV == {"TFVARS": value["tfvars"], "KUBECONFIG": value["kubeconfig"]}
    assert json.loads(internal.read_bytes()) == original
    assert not any(args[0] == "kubectl" for args in commands)
    assert (directory / "config.json").stat().st_mode & 0o077 == 0
    assert "test-storage-secret" not in capsys.readouterr().out
    variables = json.loads((directory / "pilot.tfvars.json").read_bytes())
    assert variables["kube_context"] == "new-control"
    assert variables["kube_config_path"] == value["kubeconfig"]
    assert variables["api_host"] == "api.example.test"


def test_acceptance_refuses_customer_identity(pilot, target):
    path, value = target
    value["tenant"] = "customer"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="internal tenant"):
        pilot.configure(path)


def test_acceptance_refuses_missing_worker_identity(pilot, target):
    path, value = target
    value["contexts"].pop("north")
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="explicit context"):
        pilot.configure(path)

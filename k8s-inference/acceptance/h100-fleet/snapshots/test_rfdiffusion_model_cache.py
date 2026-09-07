"""CPU contracts for request-independent RF model reuse (not GPU qualification)."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest


SOURCE = Path(__file__).resolve().parents[3] / "models/scientific-snapshot"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SOURCE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_sampler_gets_independent_config_and_reuses_only_weights(tmp_path):
    module = load("rfdiffusion_model_cache")
    model = SimpleNamespace(eval=lambda: model)
    steps = []
    cache = module.ModelOnlyCache(model=model, checkpoint_path=tmp_path / "Base.pt",
        checkpoint_config={"model": {"layers": [8]}}, model_config={"layers": 8}, d_t1d=22, d_t2d=44,
        to_container=lambda value: value, configure_timestep=lambda model, count: steps.append(count))

    class Sampler:
        def __init__(self, count):
            self.ckpt_path = str(tmp_path / "Base.pt")
            self._conf = SimpleNamespace(model={"layers": 8}, preprocess=SimpleNamespace(d_t1d=22, d_t2d=44),
                                         diffuser=SimpleNamespace(T=count))
            self.target = []

    cache.install(Sampler)
    first, second = Sampler(20), Sampler(50)
    first.load_checkpoint()
    second.load_checkpoint()
    first.ckpt["config_dict"]["model"]["layers"].append(99)
    first.target.append("request-A")
    assert second.ckpt["config_dict"] == {"model": {"layers": [8]}}
    assert second.target == []
    assert first.load_model() is second.load_model() is model
    assert steps == [20, 50]
    second._conf.model["layers"] = 9
    with pytest.raises(ValueError, match="architecture"):
        second.load_model()
    second.ckpt_path = str(tmp_path / "Other.pt")
    with pytest.raises(ValueError, match="checkpoint"):
        second.load_checkpoint()


def test_optional_proxy_preserves_original_overrides_and_compute_boundary(tmp_path, monkeypatch):
    proxy = load("rfdiffusion_cli_proxy")
    monkeypatch.setenv("FS2_RFDIFFUSION_WORKER_URL", "http://127.0.0.1:8000")
    marker = 'FS2_RF_SNAPSHOT_EXECUTION {"first_design_seconds":1.25,"upstream_seconds":12}\n'
    response = io.BytesIO(json.dumps({"stdout": marker, "stderr": "", "exit_code": 0}).encode())
    argv = ["python", "/opt/rfdiffusion/scripts/run_inference.py", "diffuser.T=20", "inference.num_designs=1"]
    with patch.object(proxy.urllib.request, "urlopen", return_value=response) as call:
        result = proxy.remote_upstream(argv, cwd=Path("/opt/rfdiffusion"), log_path=tmp_path / "log",
                                       environment={"OMP_NUM_THREADS": "1", "UNRELATED": "not-forwarded"}, timeout_seconds=50)
    assert result[:2] == (0, 1.25)
    assert (tmp_path / "log").read_text() == marker
    request = json.loads(call.call_args.args[0].data)
    assert request == {"argv": ["run-inference", *argv[2:]], "environment": {"OMP_NUM_THREADS": "1"}}
    assert call.call_args.kwargs["timeout"] == 50


def test_python310_digest_compatibility_keeps_exact_bytes(monkeypatch):
    monkeypatch.delattr(hashlib, "file_digest")
    load("python310_sitecustomize")
    assert hashlib.file_digest(io.BytesIO(b"native RF image Python3.10"), "sha256").hexdigest() == hashlib.sha256(
        b"native RF image Python3.10").hexdigest()


def test_native_request_passes_only_task_config_not_hydra_sweep_metadata(monkeypatch, capsys):
    # Native RF serializes the whole task config into the result .trb. Passing
    # return_hydra_config=True makes that fail on unresolved hydra.job.num.
    task_config = {"inference": {"num_designs": 1}, "contigmap": {"contigs": ["96-96"]}}
    calls = []

    def compose(**kwargs):
        calls.append(kwargs)
        assert not kwargs.get("return_hydra_config", False)
        return task_config

    fake_hydra = SimpleNamespace(initialize_config_dir=lambda **kwargs: nullcontext(), compose=compose)
    monkeypatch.setitem(sys.modules, "hydra", fake_hydra)
    monkeypatch.setitem(sys.modules, "rfdiffusion_model_cache", load("rfdiffusion_model_cache"))
    monkeypatch.setitem(sys.modules, "scientific_server", SimpleNamespace(serve=lambda backend: None))
    module = load("rfdiffusion_server")
    backend = module.RFdiffusionBackend.__new__(module.RFdiffusionBackend)
    backend.root = Path("/opt/rfdiffusion")
    passed = []
    backend.upstream = SimpleNamespace(main=SimpleNamespace(__wrapped__=passed.append))
    backend.torch = SimpleNamespace(cuda=SimpleNamespace(synchronize=lambda: None))
    backend.execute(["run-inference", "contigmap.contigs=[96-96]", "inference.design_startnum=8200"])
    assert passed == [task_config]
    assert calls == [{"config_name": "base", "overrides": ["contigmap.contigs=[96-96]", "inference.design_startnum=8200"]}]
    assert "FS2_RF_SNAPSHOT_EXECUTION" in capsys.readouterr().out

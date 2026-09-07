import importlib.util
import os
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[3] / "models/scientific-snapshot"


def module(monkeypatch):
    monkeypatch.syspath_prepend(str(SOURCE))
    spec = importlib.util.spec_from_file_location(
        "scientific_request_entrypoint", SOURCE / "scientific_request_entrypoint.py"
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_fallback_does_not_keep_a_stale_worker_url(monkeypatch):
    entry = module(monkeypatch)
    existing = {"KEEP": "unchanged", "FS2_RFDIFFUSION_WORKER_URL": "stale"}
    assert entry.request_environment(existing, "FS2_RFDIFFUSION_WORKER_URL", False) == {"KEEP": "unchanged"}
    assert entry.request_environment(existing, "FS2_RFDIFFUSION_WORKER_URL", True) == {
        "KEEP": "unchanged",
        "FS2_RFDIFFUSION_WORKER_URL": "http://127.0.0.1:8000",
    }
    assert existing["FS2_RFDIFFUSION_WORKER_URL"] == "stale"
    assert entry.observed_startup(False, bundle_id="selected-snapshot", manifest_sha256="a" * 64)["backend"] == "normal-load"
    assert entry.observed_startup(True, bundle_id="selected-snapshot", manifest_sha256="a" * 64)["backend"] == "cuda-criu"


def test_original_request_exit_status_and_restored_cohort_cleanup(monkeypatch, tmp_path):
    entry = module(monkeypatch)
    stopped = []
    monkeypatch.setattr(entry.lifecycle, "stop_restored_worker", stopped.append)
    assert (
        entry.run_request(
            [sys.executable, "-c", "raise SystemExit(7)"],
            os.environ.copy(),
            uid=None,
            gid=None,
            directory=tmp_path,
            restored=True,
        )
        == 7
    )
    assert stopped == [tmp_path]
    assert (
        entry.run_request(
            [sys.executable, "-c", "pass"], os.environ.copy(), uid=None, gid=None, directory=tmp_path, restored=False
        )
        == 0
    )
    assert stopped == [tmp_path]

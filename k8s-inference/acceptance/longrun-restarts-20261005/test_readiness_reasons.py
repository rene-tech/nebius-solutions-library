import importlib.util
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


@pytest.mark.parametrize("outcome", [200, 503, "timeout"])
def test_readiness_sampler_retains_only_failure_reason(monkeypatch, capsys, outcome):
    spec = importlib.util.spec_from_file_location(
        "readiness_reason_sampler",
        Path(__file__).with_name("sample_readiness_reasons.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clock = iter([0.0, 0.0, 0.0, 0.125, 99.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(sys, "argv", ["probe", "30"])

    class SuccessfulResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, *_):
            raise AssertionError("Successful readiness documents must not be collected")

    def request(url, timeout):
        assert url == "http://127.0.0.1:8080/readyz"
        assert timeout == 3
        if outcome == 503:
            payload = {
                "error": {
                    "type": "database_unavailable",
                    "message": "database readiness check timed out",
                },
                "discarded": "must-not-appear",
            }
            raise urllib.error.HTTPError(
                url, 503, "unavailable", {}, io.BytesIO(json.dumps(payload).encode())
            )
        if outcome == "timeout":
            raise TimeoutError("must-not-appear")
        return SuccessfulResponse()

    monkeypatch.setattr(urllib.request, "urlopen", request)
    exec(compile(module.REMOTE, "<readiness-probe>", "exec"), {})
    output = capsys.readouterr().out
    event = json.loads(output)
    assert "must-not-appear" not in output
    assert event["elapsed_seconds"] == 0.125
    if outcome == 503:
        assert event["code"] == "database_unavailable"
        assert event["message"] == "database readiness check timed out"
    elif outcome == "timeout":
        assert event["error_type"] == "TimeoutError"
    else:
        assert event["status"] == 200
        assert set(event) == {"at", "status", "elapsed_seconds"}

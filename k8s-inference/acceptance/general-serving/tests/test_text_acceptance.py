import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

PATH = Path(__file__).resolve().parents[1] / "run_text_acceptance.py"
SPEC = importlib.util.spec_from_file_location("text_acceptance", PATH)
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


@pytest.mark.parametrize("tokens", [0, -1, True, "9"])
def test_bad_token_count_is_not_a_throughput_measurement(tokens):
    with pytest.raises(ValueError):
        RUNNER.validate_result(
            {
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"completion_tokens": tokens},
            },
            {"oracle": {"type": "exact-content", "expected": "OK"}},
        )


def test_exact_oracle_is_checked():
    with pytest.raises(ValueError):
        RUNNER.validate_result(
            {
                "choices": [{"message": {"content": "WRONG"}}],
                "usage": {"completion_tokens": 9},
            },
            {"oracle": {"type": "exact-content", "expected": "OK"}},
        )


@pytest.mark.parametrize("terminal", ["succeeded", "failed"])
def test_real_client_protocol_and_receipt(monkeypatch, tmp_path, terminal):
    token = "test-value-never-copied-to-receipt"
    bundle = tmp_path / "bundle.json"
    fixture = tmp_path / "fixture.json"
    receipt = tmp_path / "receipt.json"
    bundle.write_text(
        json.dumps(
            {
                "credentials": {"inference_access_token": token},
                "endpoints": {"admin_portal_url": "https://example.invalid/admin/"},
            }
        )
    )
    fixture.write_text(
        json.dumps(
            {
                "requests": [
                    {
                        "id": "exact",
                        "request": {"model": "qwen3-8b"},
                        "oracle": {"type": "exact-content", "expected": "OK"},
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(PATH),
            "--bundle",
            str(bundle),
            "--fixture",
            str(fixture),
            "--receipt",
            str(receipt),
            "--repetitions",
            "1",
        ],
    )
    calls = []

    def handle(request):
        assert request.headers["authorization"] == "Bearer " + token
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(
                202,
                json={"status": "queued", "id": "operation-1"},
                headers={"x-fs2-operation-id": "operation-1"},
            )
        if request.url.path.endswith("/result"):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "OK"}}],
                    "usage": {"completion_tokens": 9},
                },
            )
        return httpx.Response(
            200,
            json={
                "status": terminal,
                "accepted_at": "2026-09-06T00:00:00Z",
                "completed_at": "2026-09-06T00:00:02Z",
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        RUNNER.httpx,
        "Client",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handle)),
    )
    assert RUNNER.main() == (0 if terminal == "succeeded" else 1)
    encoded = receipt.read_text()
    evidence = json.loads(encoded)
    assert token not in encoded
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert evidence["ttft_seconds"] is None
    assert evidence["requests"][0]["passed"] == (terminal == "succeeded")
    assert sum(method == "POST" for method, _ in calls) == 1
    if terminal == "succeeded":
        assert evidence["requests"][0]["end_to_end_output_tokens_per_second"] == 4.5
        assert evidence["result"] == "PASS"
    else:
        assert "end_to_end_output_tokens_per_second" not in evidence["requests"][0]

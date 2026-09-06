from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

RUNNER_PATH = Path(__file__).resolve().parents[1] / "run_cosmos_acceptance.py"
SOLUTION = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("cosmos_acceptance", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def setup_run(monkeypatch, tmp_path, *, identical=False):
    token = "PRIVATE_TEST_TOKEN_MUST_NOT_APPEAR"
    bundle = tmp_path / "bundle.json"
    bundle.write_text(
        json.dumps(
            {
                "credentials": {"inference_access_token": token},
                "endpoints": {"admin_portal_url": "https://example.invalid/admin/"},
            }
        )
    )
    bundle.chmod(0o600)
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(RUNNER_PATH),
            "--solution",
            str(SOLUTION),
            "--bundle",
            str(bundle),
            "--kubeconfig",
            str(tmp_path / "kubeconfig"),
            "--context",
            "unit-test",
            "--receipt",
            str(receipt),
            "--repetitions",
            "1",
        ],
    )
    monkeypatch.setattr(RUNNER.subprocess, "check_output", lambda args: b'{"items":[]}')
    monkeypatch.setattr(RUNNER.time, "sleep", lambda seconds: None)
    responses = {}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["headers"]["authorization"] == "Bearer " + token

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, path, *, headers, json):
            operation_id = str(len(responses) + 1)
            marker = "same" if identical else str(json["payload"]["seed"])
            media = b"\x00\x00\x00\x20ftypisom00000000" + marker.encode()
            responses[operation_id] = {
                "model": "nvidia/Cosmos3-Nano",
                "revision": "7a312c868bcce8e40b3eb40861300a9d0ba3fde1",
                "mode": "text-to-video",
                "mime_type": "video/mp4",
                "width": 448,
                "height": 256,
                "frames": 25,
                "fps": 24,
                "data_base64": base64.b64encode(media).decode(),
                "bytes": len(media),
                "sha256": hashlib.sha256(media).hexdigest(),
                "timings_ms": {"queue": 0, "upstream": 500, "total": 600},
            }
            return httpx.Response(
                202,
                request=httpx.Request("POST", "https://example.invalid" + path),
                json={
                    "id": operation_id,
                    "status": "succeeded",
                    "accepted_at": "2026-09-06T00:00:00+00:00",
                    "completed_at": "2026-09-06T00:00:01+00:00",
                },
            )

        def get(self, path):
            return httpx.Response(
                200,
                request=httpx.Request("GET", "https://example.invalid" + path),
                json=responses[path.split("/")[-2]],
            )

    monkeypatch.setattr(RUNNER.httpx, "Client", Client)
    return receipt, token


def test_two_distinct_media_cases_produce_secret_free_receipt(monkeypatch, tmp_path):
    receipt, token = setup_run(monkeypatch, tmp_path)
    assert RUNNER.main() == 0
    text = receipt.read_text()
    evidence = json.loads(text)
    assert token not in text
    assert "data_base64" not in text
    assert evidence["result"] == "PASS"
    assert evidence["distinct_case_outputs"] is True
    assert len(evidence["requests"]) == 2
    assert all(
        case["operation_end_to_end_seconds"] == 1 for case in evidence["requests"]
    )
    assert receipt.stat().st_mode & 0o777 == 0o600


def test_identical_outputs_for_distinct_prompts_fail(monkeypatch, tmp_path):
    receipt, _ = setup_run(monkeypatch, tmp_path, identical=True)
    assert RUNNER.main() == 1
    assert json.loads(receipt.read_text())["distinct_case_outputs"] is False


def test_zero_repetitions_cannot_pass_without_running_a_model(monkeypatch, tmp_path):
    receipt, _ = setup_run(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", [*sys.argv[:-1], "0"])
    with pytest.raises(SystemExit):
        RUNNER.main()
    assert not receipt.exists()


def test_prior_receipt_is_not_overwritten(monkeypatch, tmp_path):
    receipt, _ = setup_run(monkeypatch, tmp_path)
    receipt.write_text("prior evidence")
    with pytest.raises(FileExistsError):
        RUNNER.main()
    assert receipt.read_text() == "prior evidence"

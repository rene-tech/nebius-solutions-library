from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime/src"))

from fs2_lerobot_augmentation import cli  # noqa: E402
from fs2_lerobot_augmentation.cosmos import CosmosError  # noqa: E402


@pytest.mark.parametrize("code", list(cli.ERROR_DETAILS) + ["unrecognized-secret-value"])
def test_termination_error_is_bounded_static_and_allowlisted(tmp_path, monkeypatch, capsys, code):
    path = tmp_path / "termination.json"
    monkeypatch.setattr(cli, "TERMINATION_PATH", path)
    cli.report_error(code, retryable=True)
    raw = path.read_bytes()
    value = json.loads(raw)
    expected = code if code in cli.ERROR_DETAILS else "COSMOS_OPERATION_FAILED"
    assert len(raw) <= 1024
    assert value == {
        "schema": cli.TERMINATION_SCHEMA,
        "code": expected,
        "detail": cli.ERROR_DETAILS[expected],
        "retryable": expected in cli.RETRYABLE_CODES,
    }
    assert len(value["detail"]) <= 256
    assert "unrecognized-secret-value" not in raw.decode()
    assert json.loads(capsys.readouterr().err) == value


def test_cli_reports_admission_protocol_failure_without_raw_exception_or_retry(tmp_path, monkeypatch, capsys):
    path = tmp_path / "termination.json"
    monkeypatch.setattr(cli, "TERMINATION_PATH", path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worker",
            "--request",
            str(ROOT / "fixtures/fixture-request.json"),
            "--operation-id",
            "00000000-0000-4000-8000-000000000001",
            "--workspace",
            str(tmp_path),
            "--platform-base-url",
            "https://unused.invalid",
        ],
    )

    def fail(*args, **kwargs):
        raise CosmosError("PLATFORM_RESPONSE_INVALID", "sensitive /private/path?token=do-not-emit", retryable=False)

    monkeypatch.setattr(cli, "run", fail)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 65
    value = json.loads(path.read_bytes())
    assert value["code"] == "PLATFORM_RESPONSE_INVALID" and not value["retryable"]
    assert "do-not-emit" not in path.read_text() + capsys.readouterr().err


def test_missing_termination_file_does_not_replace_operation_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "TERMINATION_PATH", tmp_path / "absent/termination")
    cli.report_error("DATASET_INVALID", retryable=False)
    assert json.loads(capsys.readouterr().err)["code"] == "DATASET_INVALID"

import importlib.util
import io
import json
from pathlib import Path
import tarfile


spec = importlib.util.spec_from_file_location(
    "validate_genmol_snapshot", Path(__file__).with_name("validate_genmol.py")
)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


def _summary():
    cases = [
        {"ok": True, "name": "qed", "input_id": "one"},
        {"ok": True, "name": "logp", "input_id": "two"},
    ]
    return {
        "validator": "genmol-faststart-semantic-v1",
        "ok": True,
        "status": "PASS",
        "passed_case_count": 2,
        "cases": cases,
    }


def test_archive_contains_only_frozen_validator_and_fixture():
    with tarfile.open(fileobj=io.BytesIO(validator.validator_archive())) as archive:
        assert archive.getnames() == ["validate_genmol.py", "requests-qed-logp.json"]
        fixture = json.load(archive.extractfile("requests-qed-logp.json"))
    assert [case["name"] for case in fixture["calls"]] == ["qed", "logp"]


def test_projection_requires_both_strict_cases():
    assert validator.project_summary(_summary())["passed"]
    source = _summary()
    source["cases"][1]["ok"] = False
    assert not validator.project_summary(source)["passed"]

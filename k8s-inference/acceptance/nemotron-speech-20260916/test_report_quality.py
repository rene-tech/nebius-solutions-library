import json
from pathlib import Path

import pytest

from report_quality import digest, german_indices, parse_response
from summarize_report_quality import review_schema_ok, summarize


def test_fixed_german_selection_and_challenge():
    indices = german_indices(1091)
    assert len(indices) == 31
    assert indices[0] == 0 and indices[-1] == 1090 and 193 in indices


def test_stable_digest():
    assert digest({"b": 2, "a": 1}) == digest({"a": 1, "b": 2})


def test_reject_truncated_response():
    with pytest.raises(ValueError, match="Incomplete"):
        parse_response({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]})


def test_parse_json_response():
    assert parse_response({"choices": [{"finish_reason": "stop", "message": {"content": "{\"facts\": []}"}}]}) == {"facts": []}


def test_retained_experiment_completeness_and_reference_blind_generation():
    root = Path(__file__).parent / "report-quality-r1"
    manifest = json.loads((root / "manifest.json").read_text())
    assert len(manifest["inputs"]) == 71
    assert len(manifest["cases"]) == 36
    for item in manifest["inputs"]:
        receipt = json.loads((root / "generation" / f"{item['id']}.json").read_text())
        assert receipt["status"] == "ok"
        assert receipt["request_sha256"] == digest(receipt["request"])
        sent = json.loads(receipt["request"]["messages"][1]["content"])
        assert set(sent) == {"language", "input_kind", "transcript"}
        assert sent["transcript"] == item["text"]
        assert item["text_sha256"] == digest(item["text"])
        report = parse_response(receipt["raw_response"])
        assert isinstance(report["report_markdown"], str) and report["report_markdown"].strip()
        assert isinstance(report["facts"], list)
        assert (root / "generation" / f"{item['id']}.md").read_text() == report["report_markdown"] + "\n"
    malformed = []
    for case in manifest["cases"]:
        receipt = json.loads((root / "review" / f"{case}.json").read_text())
        assert receipt["status"] == "ok"
        expected = {item["id"] for item in manifest["inputs"] if item["case"] == case}
        if not review_schema_ok(receipt["parsed"], expected):
            malformed.append(case)
    # Retain the actual failed judge output; never turn it into a passing review.
    assert malformed == ["multimed-0112"]
    summary = summarize(root)
    assert [row["case"] for row in summary["reviews"] if not row["schema_valid"]] == malformed


def test_agent_checked_quotes_are_actual_source_and_report_substrings():
    root = Path(__file__).parent / "report-quality-r1"
    manifest = json.loads((root / "manifest.json").read_text())
    inputs = {item["id"]: item for item in manifest["inputs"]}
    findings = json.loads((root / "agent-checked-findings.json").read_text())
    for finding in findings["findings"]:
        report = (root / "generation" / f"{finding['input_id']}.md").read_text()
        assert finding["report_quote"] in report
        if finding.get("source_quote"):
            item = inputs[finding["input_id"]]
            reference_text = manifest["cases"][item["case"]]["human_reference"]
            text = reference_text if finding["basis"] == "human transcript" else item["text"]
            assert finding["source_quote"] in text

"""Behavioral invariants for the portable clinical documentation skill."""

import json
import sys
from itertools import pairwise
from pathlib import Path

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "integrations/librechat/skills/clinical-documentation/scripts"
sys.path.insert(0, str(SCRIPTS))
from clinical_report import (
    Platform,
    document_transcript,
    parse_completion,
    resolve_result,
    same_origin_path,
)
from document import (
    apply_review,
    chunks,
    completion_schema,
    digest,
    render,
    source_segments,
    validate_extraction,
    validate_questions,
)


def candidate(statement="No fever", quote="No fever"):
    return {"kind": "consultation", "facts": [{"section": "history", "statement": statement,
            "quotes": [quote], "uncertain": False}], "uncertainties": []}


def test_chunk_coverage_and_overlap():
    text = "Some words with repeated context. " * 1000
    parts = list(chunks(text))
    positions = set()
    for part in parts:
        assert text[part["start"]:part["end"]] == part["text"]
        positions.update(range(part["start"], part["end"]))
    assert positions == set(range(len(text)))
    assert all(b["start"] < a["end"] for a, b in pairwise(parts))


def test_nonliteral_evidence_is_retained_as_rejected_not_fact():
    good, _doubts, rejected, index = validate_extraction(candidate(), {"text": "Do you have fever?", "start": 5})
    assert not good and len(rejected) == 1 and index == 2


def test_repeated_quotes_keep_ambiguity_and_global_offsets():
    facts, _, _, _ = validate_extraction(candidate("No fever", "no"), {"text": "no fever, no cough", "start": 100})
    assert facts[0]["evidence"][0]["spans"] == [{"start": 100, "end": 102}, {"start": 110, "end": 112}]


def test_literal_quote_does_not_bypass_context_review():
    facts, _, _, _ = validate_extraction(candidate("No fever", "fever"), {"text": "Any fever?", "start": 0})
    kept, rejected = apply_review(facts, {"decisions": [{"id": facts[0]["id"], "verdict": "unsupported", "reason": "Question, no answer"}]})
    assert not kept and len(rejected) == 1


@pytest.mark.parametrize("decisions", [[], [{"id": "F9999", "verdict": "supported", "reason": "yes"}]])
def test_partial_review_fails(decisions):
    facts, *_ = validate_extraction(candidate(), {"text": "No fever", "start": 0})
    with pytest.raises(ValueError):
        apply_review(facts, {"decisions": decisions})


@pytest.mark.parametrize("finish", ["length", "content_filter", None])
def test_incomplete_completion_is_not_a_report(finish):
    with pytest.raises(ValueError):
        parse_completion({"choices": [{"finish_reason": finish, "message": {"content": "{}"}}]})


def test_empty_transcript_never_calls_model(tmp_path):
    with pytest.raises(ValueError, match="empty transcript"):
        document_transcript(" ", "de", None, tmp_path)


@pytest.mark.parametrize("path", ["https://external/v1/file", "//external/v1/file", "/admin/file", "/v1/\\evil"])
def test_only_platform_upload_paths(path):
    with pytest.raises(ValueError):
        same_origin_path(path)


def test_result_artifact_integrity():
    data = b'{"text":"A complete long transcript"}'
    envelope = {"schema": "fs2-serve.nebius.ai/operation-artifact-result/v1", "content_type": "application/json",
                "artifact": {"artifact_id": "id", "compression": "none", "size_bytes": len(data), "sha256": digest(data)}}
    with httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=data))) as client:
        assert resolve_result(client, envelope)["text"].startswith("A complete")
        envelope["artifact"]["sha256"] = "bad"
        with pytest.raises(ValueError):
            resolve_result(client, envelope)


def test_saved_operation_resumes_without_repost(tmp_path):
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json={"text": "done"})
        return httpx.Response(200, json={"id": "existing-id", "status": "succeeded"})
    payload = {"test": True}
    folder = tmp_path / "calls/asr"
    folder.mkdir(parents=True)
    (folder / "state.json").write_text(json.dumps({"request_sha256": digest({"endpoint": "/v1/test", "payload": payload}), "operation_id": "existing-id"}))
    platform = Platform("https://example.test", "not-real", tmp_path, "run", poll_seconds=0)
    platform.client.close()
    platform.client = httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))
    try:
        assert platform.operation("asr", "/v1/test", payload)["text"] == "done"
        assert all(method == "GET" for method, _ in calls)
        before = len(calls)
        platform.operation("asr", "/v1/test", payload)
        assert len(calls) == before
    finally:
        platform.close()


def test_report_and_suggestions_are_separate():
    facts, *_ = validate_extraction(candidate(), {"text": "No fever", "start": 0})
    doc = {"kind": "consultation", "facts": facts, "rejected": [], "uncertainties": [],
           "questions": [{"question": "Any travel?", "reason": "Not documented", "fact_ids": ["F0001"], "basis": "not_documented"}]}
    report, questions = render(doc, "en")
    assert "Any travel" not in report and "Any travel" in questions
    assert "No entries assigned to this section" in report
    assert "No travel" not in report


def test_unknown_question_fact_is_rejected():
    with pytest.raises(ValueError):
        validate_questions({"questions": [{"question": "Q", "reason": "R", "fact_ids": ["F999"], "basis": "not_documented"}]}, [])


def test_unclear_citation_is_not_a_supported_fact():
    facts, *_ = validate_extraction(candidate(), {"text": "No fever", "start": 0})
    kept, disputed = apply_review(facts, {"decisions": [{"id": "F0001", "verdict": "unclear", "reason": "Insufficient source context"}]})
    assert not kept and disputed[0]["verdict"] == "unclear"


def test_source_id_citations_preserve_actual_text_and_offsets():
    chunk = {"text": "Arzt: Kopfschmerzen? Patient: Ich weiß es nicht.", "start": 200}
    chunk["segments"] = source_segments(chunk)
    value = candidate("Headache status unclear")
    value["facts"][0].pop("quotes")
    value["facts"][0].update(source_ids=[chunk["segments"][0]["id"]], uncertain=True)
    facts, _, rejected, _ = validate_extraction(value, chunk)
    assert not rejected and facts[0]["uncertain"]
    assert facts[0]["evidence"][0]["quote"] == chunk["text"]
    assert facts[0]["evidence"][0]["spans"] == [{"start": 200, "end": 200 + len(chunk["text"])}]


def test_schema_requires_uncertainty_and_known_source_ids():
    schema = completion_schema("extract-000", {"segments": [{"id": "S0000010", "text": "Example"}]})
    fact = schema["properties"]["facts"]["items"]
    assert "uncertain" in fact["required"]
    assert fact["properties"]["source_ids"]["items"]["enum"] == ["S0000010"]


def test_review_isolated_per_fact_and_bad_citation_repaired(tmp_path):
    text = "No fever. " + "Conversation context. " * 30 + "Take five milligrams daily."
    segments = source_segments({"text": text, "start": 0})
    last = segments[-1]["id"]

    class FakeReporter:
        def complete(self, stage, prompt, data):
            if stage.startswith("extract"):
                return {"kind": "consultation", "uncertainties": [], "facts": [
                    {"section": "history", "statement": "No fever", "source_ids": ["S1"], "uncertain": False},
                    {"section": "plan", "statement": "Five milligrams daily", "source_ids": ["S1"], "uncertain": False}]}
            if stage.startswith("review"):
                assert set(data) == {"language", "facts"}
                assert len(data["facts"]) == 1  # No other fact's evidence can leak in.
                fact = data["facts"][0]
                correct = fact["id"] == "F0001" or "repaired" in stage
                return {"decisions": [{"id": fact["id"], "verdict": "supported" if correct else "unsupported", "reason": "Test source check"}]}
            if stage.startswith("locate"):
                return {"source_ids": [last]}
            assert stage == "questions"
            return {"questions": []}

    result = document_transcript(text, "en", FakeReporter(), tmp_path)
    assert len(result["facts"]) == 2 and not result["rejected"]
    repaired = result["facts"][1]
    assert repaired["citation_repair"]["original_evidence"][0]["source_id"] == "S1"
    assert "five milligrams" in repaired["evidence"][0]["quote"]

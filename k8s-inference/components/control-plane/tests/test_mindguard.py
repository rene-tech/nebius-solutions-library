from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from fs2_serve.mindguard import (
    MindGuardAssessment,
    MindGuardMessage,
    assess_mindguard,
    assess_mindguard_transcript,
    parse_mindguard_output,
)


def transcript() -> list[MindGuardMessage]:
    return [
        MindGuardMessage(role="user", content="I am worried about work."),
        MindGuardMessage(role="assistant", content="What happened today?"),
        MindGuardMessage(role="user", content="The meeting went better than expected."),
        MindGuardMessage(role="assistant", content="That sounds encouraging."),
    ]


def response(output: str = "Safety: Safe\nCategories: None", **changes: object) -> httpx.Response:
    body = {
        "model": "mindguard-4b",
        "choices": [{"message": {"content": output}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 800, "completion_tokens": 8},
    }
    body.update(changes)
    return httpx.Response(200, json=body)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Safety: Safe\nCategories: None", ("safe", [])),
        ("Safety: Unsafe\nCategories: S1", ("unsafe", ["S1"])),
        ("Safety: Unsafe\nCategories: S2, S1", ("unsafe", ["S1", "S2"])),
    ],
)
def test_parse_valid(output: str, expected: tuple[str, list[str]]) -> None:
    assert parse_mindguard_output(output) == expected


@pytest.mark.parametrize(
    "output",
    [
        "safe",
        "Safety: Unsafe\nCategories: None",
        "Safety: Safe\nCategories: S1",
        "Safety: Unsafe\nCategories: S3",
        "Safety: Unsafe\nCategories:",
        "Safety: Safe\nCategories: None\nSafety: Unsafe",
        "Safety: Unsafe\nCategories: S1 explanation",
    ],
)
def test_parse_ambiguous_never_becomes_safe(output: str) -> None:
    with pytest.raises(ValueError):
        parse_mindguard_output(output)


async def test_only_target_user_prefix_is_sent_without_template_replacement() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await assess_mindguard(
            transcript(), model_id="mindguard-4b", endpoint="http://runtime/v1", client=client
        )
    assert requests[0]["messages"] == [message.model_dump() for message in transcript()[:3]]
    assert requests[0]["max_tokens"] == 15
    assert requests[0]["temperature"] == 0
    assert "truncate_prompt_tokens" not in requests[0]
    assert result.coverage.target_user_message_index == 2
    assert result.coverage.context_message_count == 3
    assert result.coverage.input_message_count == 4
    assert result.coverage.evaluated_user_turns == 1
    assert result.role == "safety_classifier"
    assert result.enforcement == "observe"


async def test_transcript_evaluates_all_user_turns_and_retains_failure() -> None:
    lengths = []

    def handler(request: httpx.Request) -> httpx.Response:
        lengths.append(len(json.loads(request.content)["messages"]))
        return response("Safety: Unsafe\nCategories: S1") if len(lengths) == 1 else httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await assess_mindguard_transcript(
            transcript(),
            model_id="mindguard-4b",
            endpoint="http://runtime/v1",
            client=client,
        )
    assert lengths == [1, 3]
    assert result.status == "partial"
    assert result.input_user_turns == 2
    assert result.evaluated_user_turns == 1
    assert result.flagged_user_message_indices == [0]
    assert result.assessments[1].safety is None
    assert result.assessments[1].error.retryable


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        (response(model="Qwen/Qwen3-4B"), "model_identity_mismatch"),
        (
            response(choices=[{"message": {"content": "Safety: Safe\nCategories: None"}, "finish_reason": "length"}]),
            "incomplete_classification",
        ),
        (response("Safety: Safe\nCategories: S1"), "invalid_classification"),
        (httpx.Response(200, json={}), "model_identity_mismatch"),
        (httpx.Response(400, text="private input and upstream token"), "classifier_http_error"),
    ],
)
async def test_bad_runtime_output_is_retained_as_error(reply: httpx.Response, code: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: reply)) as client:
        result = await assess_mindguard(
            transcript(), model_id="mindguard-4b", endpoint="http://runtime/v1", client=client
        )
    assert result.status == "error"
    assert result.safety is None
    assert result.categories == []
    assert result.error.code == code
    assert "private input" not in result.model_dump_json()


async def test_missing_endpoint_and_unsupported_language_make_no_network_call() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        unavailable = await assess_mindguard(transcript(), model_id="mindguard-4b", endpoint=None, client=client)
        unsupported = await assess_mindguard(
            transcript(),
            model_id="mindguard-4b",
            endpoint="http://runtime/v1",
            client=client,
            language="pt",
        )
    assert unavailable.status == "unavailable"
    assert unavailable.safety is None
    assert unsupported.error.code == "unsupported_language"


def test_no_enforcement_or_clinician_relabeling_in_schema() -> None:
    baseline = {
        "model_id": "mindguard-4b",
        "model_revision": "f66279d31561e5e96736cb9cc5c2ec6e7d49d1c2",
        "status": "unavailable",
        "coverage": {"input_message_count": 0, "context_message_count": 0},
        "error": {"code": "unavailable", "message": "unavailable"},
    }
    for change in [{"enforcement": "block"}, {"role": "clinician"}, {"model_revision": "main"}, {"safety": "safe"}]:
        with pytest.raises(ValidationError):
            MindGuardAssessment.model_validate({**baseline, **change})

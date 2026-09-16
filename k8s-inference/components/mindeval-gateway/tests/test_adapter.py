import json

import httpx
import pytest

from fs2_mindeval.adapter import TokenFactoryAdapter, parse_judgment, parse_response
from fs2_mindeval.contracts import GatewayError
from fs2_mindeval.prompts import CRITERIA
from fs2_mindeval.scheduler import FairScheduler


def response(content="Hello", finish="stop", **message):
    return {
        "choices": [{"message": {"content": content, **message}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


@pytest.mark.parametrize(
    "content,finish,message,code",
    [
        (None, "stop", {"reasoning_content": "private reasoning"}, "reasoning_only"),
        (None, "stop", {"reasoning": "private reasoning"}, "reasoning_only"),
        ("", "stop", {}, "empty_content"),
        ("answer", "length", {}, "length_finished"),
        ("<think>unfinished", "stop", {}, "reasoning_only"),
        ("answer", "tool_calls", {}, "unexpected_finish"),
    ],
)
def test_response_failure_is_explicit(content, finish, message, code):
    with pytest.raises(GatewayError) as caught:
        parse_response(response(content, finish, **message))
    assert caught.value.code == code


def test_reasoning_is_separate_and_usage_preserved():
    parsed = parse_response(response("<think>reason</think>visible", reasoning="other"))
    assert parsed["content"] == "visible"
    assert parsed["reasoning"] == "other\nreason"
    assert parsed["usage"]["total_tokens"] == 8
    assert parsed["finish_reason"] == "stop"


def test_fractional_rubric_scores_are_preserved():
    text = "\n".join(f"{criterion}: 3.125" for criterion in CRITERIA)
    assert set(parse_judgment(text).values()) == {3.125}
    assert parse_judgment(f"<output>\n{text}\n</output>") == parse_judgment(text)
    assert parse_judgment(json.dumps(dict.fromkeys(CRITERIA, 4))) == dict.fromkeys(CRITERIA, 4.0)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{}",
        "{'broken': 4}",
        "Clinical Accuracy & Competence: 4",
        json.dumps(dict.fromkeys(CRITERIA, 7)),
        json.dumps(dict.fromkeys(CRITERIA, True)),
        "\n".join(f"{criterion}: 4" for criterion in CRITERIA) + "\nClinical Accuracy & Competence: 5",
    ],
)
def test_invalid_judgments_never_default(text):
    with pytest.raises(GatewayError, match="judge|criterion") as caught:
        parse_judgment(text)
    assert caught.value.code == "invalid_judgment"


async def complete_responses(responses):
    sent = []
    sleeps = []

    async def handle(request):
        sent.append(json.loads(request.content))
        item = responses[min(len(sent) - 1, len(responses) - 1)]
        return httpx.Response(item[0], json=item[1])

    async def sleep(delay):
        sleeps.append(delay)

    scheduler = FairScheduler(window=0.01)
    adapter = TokenFactoryAdapter(
        "test-secret", scheduler, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), sleep=sleep
    )
    try:
        result = await adapter.complete(
            team="team",
            model="test",
            messages=[{"role": "user", "content": "hi"}],
            max_completion_tokens=128,
            temperature=0,
        )
        return result, sent, sleeps
    finally:
        await scheduler.close()
        await adapter.client.aclose()


async def test_429_and_transient_5xx_have_bounded_retries():
    result, requests, sleeps = await complete_responses([(429, {}), (503, {}), (200, response())])
    assert result["telemetry"]["retries"] == 2
    assert len(requests) == 3
    assert sleeps == [1, 2]


async def test_persistent_429_exhausts_four_attempts():
    with pytest.raises(GatewayError) as caught:
        await complete_responses([(429, {})])
    assert caught.value.code == "provider_retries_exhausted"
    assert caught.value.telemetry["retries"] == 3


async def test_400_is_not_blindly_retried():
    with pytest.raises(GatewayError) as caught:
        await complete_responses([(400, {"error": "invalid model"})])
    assert caught.value.code == "provider_http_400"
    assert caught.value.telemetry["retries"] == 0


async def test_max_tokens_fallback_requires_specific_400():
    result, requests, _ = await complete_responses(
        [(400, {"error": "unsupported parameter max_completion_tokens"}), (200, response())]
    )
    assert requests[0]["max_completion_tokens"] == requests[1]["max_tokens"] == 128
    assert "max_completion_tokens" not in requests[1]
    assert result["telemetry"]["token_parameter"] == "max_tokens"


async def test_invalid_json_is_not_retried():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, text="not json")

    scheduler = FairScheduler()
    adapter = TokenFactoryAdapter("test", scheduler, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        with pytest.raises(GatewayError) as caught:
            await adapter.complete(
                team="t",
                model="m",
                messages=[{"role": "user", "content": "hello"}],
                max_completion_tokens=128,
                temperature=0,
            )
        assert caught.value.code == "invalid_json"
        assert len(calls) == 1
    finally:
        await scheduler.close()

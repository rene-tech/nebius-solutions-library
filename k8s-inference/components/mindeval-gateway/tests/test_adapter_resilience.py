"""Deterministic failure injection; no faults are injected into shared services."""

import json

import httpx
import pytest

from fs2_mindeval import BASE_URL
from fs2_mindeval.adapter import TokenFactoryAdapter
from fs2_mindeval.contracts import GatewayError
from fs2_mindeval.scheduler import FairScheduler

SUCCESS = {
    "choices": [{"message": {"content": "visible"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}


async def exercise(handler, *, repeats=1):
    requests, sleeps = [], []

    async def send(request):
        requests.append(request)
        assert str(request.url) == BASE_URL + "/chat/completions"
        return handler(request, len(requests))

    async def sleep(delay):
        sleeps.append(delay)

    scheduler = FairScheduler(window=0.01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        adapter = TokenFactoryAdapter("fixture-only-key", scheduler, client=client, sleep=sleep)
        try:
            results = []
            for _ in range(repeats):
                try:
                    results.append(
                        await adapter.complete(
                            team="fixture-team",
                            model="fixture-model",
                            messages=[{"role": "user", "content": "fixture"}],
                            max_completion_tokens=128,
                            temperature=0,
                        )
                    )
                except GatewayError as exc:
                    results.append(exc)
            return results, requests, sleeps
        finally:
            await scheduler.close()


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_every_retryable_http_status_exhausts_four_attempts(status):
    results, requests, sleeps = await exercise(lambda request, count: httpx.Response(status, text="upstream secret"))
    failure = results[0]
    assert isinstance(failure, GatewayError)
    assert failure.code == "provider_retries_exhausted"
    assert len(requests) == 4 and sleeps == [1, 2, 4]
    assert failure.telemetry["retry_codes"] == [f"http_{status}"] * 4
    assert failure.telemetry["retries"] == 3
    assert "upstream secret" not in str(failure)


@pytest.mark.parametrize("header,delay", [("5", 5), ("999", 30), ("-4", 1), ("invalid", 1)])
async def test_retry_after_is_honored_but_bounded(header, delay):
    def handle(request, count):
        return httpx.Response(429, headers={"retry-after": header}) if count == 1 else httpx.Response(200, json=SUCCESS)

    results, requests, sleeps = await exercise(handle)
    assert len(requests) == 2 and sleeps == [delay]
    assert results[0]["telemetry"]["retry_codes"] == ["http_429"]


@pytest.mark.parametrize("kind", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError])
async def test_transport_failure_recovers_with_original_request(kind):
    def handle(request, count):
        if count == 1:
            raise kind("fixture transport failure", request=request)
        return httpx.Response(200, json=SUCCESS)

    results, requests, sleeps = await exercise(handle)
    assert results[0]["content"] == "visible"
    assert results[0]["telemetry"]["retry_codes"] == ["transport_error"]
    assert requests[0].content == requests[1].content and sleeps == [1]


async def test_persistent_transport_failure_has_finite_budget():
    def handle(request, count):
        raise httpx.ReadTimeout("fixture timeout", request=request)

    results, requests, sleeps = await exercise(handle)
    assert results[0].code == "provider_retries_exhausted"
    assert len(requests) == 4 and sleeps == [1, 2, 4]
    assert results[0].telemetry["retry_codes"] == ["transport_error"] * 4


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_permanent_errors_are_not_retried_or_echoed(status):
    results, requests, sleeps = await exercise(lambda request, count: httpx.Response(status, text="upstream secret"))
    assert results[0].code == f"provider_http_{status}"
    assert len(requests) == 1 and not sleeps
    assert "upstream secret" not in str(results[0])


async def test_specific_compatibility_fallback_is_cached_without_rewriting_messages():
    def handle(request, count):
        if count == 1:
            return httpx.Response(400, text="unsupported parameter max_completion_tokens")
        return httpx.Response(200, json=SUCCESS)

    results, requests, sleeps = await exercise(handle, repeats=2)
    payloads = [json.loads(request.content) for request in requests]
    assert len(requests) == 3 and not sleeps
    assert payloads[0]["max_completion_tokens"] == payloads[1]["max_tokens"] == payloads[2]["max_tokens"] == 128
    assert payloads[0]["messages"] == payloads[1]["messages"] == payloads[2]["messages"]
    assert all("max_completion_tokens" not in item for item in payloads[1:])
    assert results[0]["telemetry"]["retry_codes"] == ["max_tokens_compatibility"]
    assert results[1]["telemetry"]["retries"] == 0


async def test_noncompatibility_400_does_not_change_token_parameter():
    def handle(request, count):
        return httpx.Response(400, text="max_completion_tokens exceeds permitted range")

    results, requests, sleeps = await exercise(handle)
    assert results[0].code == "provider_http_400"
    assert len(requests) == 1 and not sleeps
    assert "max_completion_tokens" in json.loads(requests[0].content)

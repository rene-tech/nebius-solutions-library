"""Offline bounded-request and per-Pod attribution helpers; no live traffic."""

import asyncio

import httpx
import pytest

import qwen_startup_qualification as qualification


def test_distinct_bounded_public_payloads_and_useful_response_checks():
    first_marker, first = qualification.payload_for(1)
    second_marker, second = qualification.payload_for(2)
    assert first != second and first_marker != second_marker
    assert first["model"] == "qwen3-8b" and first["max_tokens"] == 1024
    assert not first["chat_template_kwargs"]["enable_thinking"]
    assert qualification.useful_output(
        first_marker + " " + "educational " * 100, first_marker
    )
    assert not qualification.useful_output("short answer", first_marker)
    assert not qualification.useful_output(
        second_marker + " " + "educational " * 100, first_marker
    )


def test_counter_uses_only_successful_stop_or_length_not_abort_error_or_absolute_zero():
    metric = 'vllm:request_success_total{{engine="0",finished_reason="{}"}} {}'
    text = "\n".join(
        metric.format(reason, count)
        for reason, count in (
            ("stop", 12),
            ("length", 3),
            ("abort", 7),
            ("error", 9),
            ("repetition", 8),
        )
    )
    assert qualification.success_counter(text) == 15
    assert qualification.success_counter(metric.format("stop", 0)) == 0
    assert qualification.success_counter("# no runtime counter") is None


@pytest.mark.parametrize("failed", [False, True])
def test_public_request_has_one_submission_and_retains_errors(monkeypatch, failed):
    calls = []
    marker, _ = qualification.payload_for(1)

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            if failed:
                return httpx.Response(409, json={"detail": "conflict"})
            return httpx.Response(
                202, json={"id": "operation-1", "status": "succeeded"}
            )
        assert request.url.path == "/v1/operations/operation-1/result"
        return httpx.Response(
            200,
            json={
                "id": "upstream-1",
                "choices": [
                    {"message": {"content": marker + " " + "educational " * 100}}
                ],
            },
        )

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        qualification.httpx,
        "AsyncClient",
        lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(handler)),
    )
    row = asyncio.run(
        qualification.public_request("https://cluster.example", "fixture-token", 1)
    )
    assert sum(method == "POST" for method, _ in calls) == 1
    assert row["submission_attempts"] == 1
    assert row["status"] == ("failed" if failed else "passed")
    if failed:
        assert row["http_statuses"] == [409]
        assert row["failure"]["http_status"] == 409
        assert "operation_id" not in row
    else:
        assert row["response_id"] == "upstream-1" and row["useful_output"]

"""The rerun preserves nested failures without retaining secret error text."""

import importlib.util
import json
from builtins import ExceptionGroup
from pathlib import Path
from types import SimpleNamespace

import httpx

spec = importlib.util.spec_from_file_location(
    "remediation_sampler", Path(__file__).with_name("interactive_sampler.py")
)
sampler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sampler)


def test_nested_http_failure_keeps_type_and_status_not_url_or_token():
    request = httpx.Request("GET", "https://private.invalid/?token=SECRET")
    response = httpx.Response(503, request=request)
    leaf = httpx.HTTPStatusError("Bearer SECRET", request=request, response=response)
    result = sampler.safe_exception(
        ExceptionGroup("SECRET", [ValueError("SECRET"), leaf])
    )
    assert result["type"] == "ExceptionGroup"
    assert result["exceptions"][1] == {"type": "HTTPStatusError", "http_status": 503}
    assert "SECRET" not in json.dumps(result)
    assert "private.invalid" not in json.dumps(result)


def test_structured_mcp_error_keeps_correlation_and_retry_classification_only():
    request_id = "3421b9e3-87a8-4f52-9e38-7b1d28f4cd10"
    result = SimpleNamespace(
        is_error=True,
        structured_content={
            "error": {
                "type": "route_unavailable",
                "retryable": True,
                "request_id": request_id,
                "message": "SECRET",
            },
            "headers": {"authorization": "SECRET"},
        },
    )
    assert sampler.safe_tool_error(result) == {
        "is_error": True,
        "type": "route_unavailable",
        "retryable": True,
        "request_id": request_id,
    }


def test_exception_group_bounds_are_visible():
    result = sampler.safe_exception(
        ExceptionGroup("private", [ValueError("private")] * 20)
    )
    assert len(result["exceptions"]) == 16
    assert result["omitted_children"] == 4


def test_unknown_machine_fields_are_not_assumed_safe():
    result = SimpleNamespace(
        is_error=True,
        structured_content={"error": {"type": "Bearer SECRET", "request_id": "SECRET"}},
    )
    assert sampler.safe_tool_error(result) == {"is_error": True}

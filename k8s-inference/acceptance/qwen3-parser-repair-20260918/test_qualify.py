import json

import pytest
from qualify import combine_stream, extra_cases, validate


def response(content='{"answer":226}', finish="stop", calls=None):
    return {
        "choices": [
            {
                "message": {"content": content, "tool_calls": calls, "reasoning": "Separate reasoning."},
                "finish_reason": finish,
            }
        ]
    }


@pytest.mark.parametrize(
    "payload",
    [
        response('<think></think>{"answer":226}'),
        response("", "length"),
        response('{"answer":225}'),
        response("", "stop"),
    ],
)
def test_no_tag_stripping_or_truncation_or_answer_substitution(payload):
    with pytest.raises(ValueError):
        validate(payload, {"evaluator": "chat_json", "reference_answer": {"answer": 226}})


def test_separate_reasoning_is_preserved_and_visible_answer_checked():
    receipt = validate(response(), {"evaluator": "chat_json", "reference_answer": {"answer": 226}})
    assert receipt["passed"] and receipt["reasoning_characters"] > 0


def test_forced_tool_must_be_returned_as_structured_call():
    expected = {"evaluator": "chat_tool_call", "tool_name": "save", "reference_answer": {"answer": 226}}
    with pytest.raises(ValueError, match="missing_or_wrong_tool"):
        validate(response(), expected)
    assert validate(response(None, calls=[{"function": {"name": "save", "arguments": '{"answer":226}'}}]), expected)[
        "passed"
    ]


def test_stream_assembles_real_tool_argument_fragments_and_requires_done():
    chunks = [
        {"choices": [{"delta": {"reasoning": "Think"}, "finish_reason": None}]},
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"name": "save", "arguments": '{"answer":'}}]}}
            ]
        },
        {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "226}"}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"choices": [], "usage": {"completion_tokens": 20}},
    ]
    wire = "\n\n".join("data: " + json.dumps(c) for c in chunks)
    with pytest.raises(ValueError, match="stream_missing_done"):
        combine_stream(wire)
    output = combine_stream(wire + "\n\ndata: [DONE]\n")
    receipt = validate(output, {"kind": "chat_tool_call", "tool_name": "save", "reference_answer": {"answer": 226}})
    assert receipt["passed"] and receipt["usage"]["completion_tokens"] == 20


def test_additional_cases_preserve_budgets_and_cover_both_protocols():
    cases = extra_cases()
    assert len(cases) == 14
    assert {c["arguments"]["max_completion_tokens"] for c in cases} == {2048}
    assert {c["arguments"]["stream"] for c in cases} == {False, True}
    assert all("enable_thinking" not in c["arguments"] for c in cases)

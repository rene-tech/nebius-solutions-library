import copy

import pytest
from jsonschema import Draft202012Validator, ValidationError
from request_contract import FINDINGS, response_format, revised_case

from fs2_serve.model_input_contracts import _chat


def original_case():
    return {
        "case_id": "synthetic-contract-test",
        "model_id": "nv-reason-cxr-3b",
        "arguments": {
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Unchanged research prompt"},
                {"type": "image_url", "image_url": {"url": "https://example.org/test.png"}},
            ]}],
            "temperature": 0, "max_completion_tokens": 4096, "stream": False,
            "response_format": {"type": "json_object"},
        },
        "expected": {"evaluator": "cxr_findings", "allowed_findings": list(FINDINGS),
                     "reference_findings": ["Nodule"]},
    }


def test_changes_only_response_format_and_never_mutates_source():
    case = original_case()
    before = copy.deepcopy(case)
    candidate = revised_case(case)
    assert case == before
    assert candidate["expected"] == case["expected"]
    old, new = case["arguments"], candidate["arguments"]
    assert {key for key in old if old[key] != new[key]} == {"response_format"}
    assert candidate["case_id"] != case["case_id"]
    case["expected"]["reference_findings"] = ["DO_NOT_LEAK_REFERENCE"]
    assert revised_case(case)["arguments"] == new


def test_existing_published_chat_schema_accepts_revised_request():
    schema, _ = _chat("nv-reason-cxr-3b")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(revised_case(original_case())["arguments"])


@pytest.mark.parametrize("labels", [[name] for name in FINDINGS] + [["Nodule", "Mass"]])
def test_exact_vocabulary_is_accepted_without_remapping(labels):
    schema = response_format()["json_schema"]["schema"]
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate({"findings": labels, "limitations": "Research only; uncertain."})


@pytest.mark.parametrize("labels", [
    [" mass"], ["consolidation"], ["CONSolidation"], ["Pleural_Thinking"],
    ["Lung Opacity"], ["Pleural Effusion"], ["No Finding", "Nodule"], [], ["Nodule"] * 15,
])
def test_original_failure_classes_are_not_normalized_or_accepted(labels):
    with pytest.raises(ValidationError):
        Draft202012Validator(response_format()["json_schema"]["schema"]).validate(
            {"findings": labels, "limitations": "Uncertain."}
        )


@pytest.mark.parametrize("answer", [
    {"findings": ["No Finding"]},
    {"findings": ["No Finding"], "limitations": ""},
    {"findings": ["No Finding"], "limitations": "x" * 513},
    {"findings": ["No Finding"], "limitations": "Uncertain", "diagnosis": "unsupported"},
])
def test_required_bounded_fields(answer):
    with pytest.raises(ValidationError):
        Draft202012Validator(response_format()["json_schema"]["schema"]).validate(answer)


def test_refuse_unreviewed_source_vocabulary_or_mode():
    for change in ("vocabulary", "format", "model"):
        case = original_case()
        if change == "vocabulary":
            case["expected"]["allowed_findings"].append("unreviewed")
        elif change == "format":
            case["arguments"]["response_format"] = {"type": "text"}
        else:
            case["model_id"] = "other"
        with pytest.raises(ValueError):
            revised_case(case)

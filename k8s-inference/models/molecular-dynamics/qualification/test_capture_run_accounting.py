"""Only explicitly selected lifecycle facts may enter the private receipt."""

from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "capture_run_accounting", Path(__file__).with_name("capture_run_accounting.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

TENANT = "qualification"
OPERATION = "e07faffd-79bd-482d-9e14-24e0ecf0f6f1"
SUBJECT = "2863557f-c5c8-5bcf-a65f-e5d84e1f7fb6"
DETAIL = {
    "subject": {"tenant_id": TENANT, "operation_id": OPERATION, "subject_id": SUBJECT},
    "payloads_exposed": False,
    "rollup": {
        "quality": "application_observed",
        "data_gaps": ["trace_context_missing"],
    },
}


def test_exact_owner_and_operation_preserve_observation_limitations():
    assert module.validate_subject(DETAIL["subject"], TENANT, OPERATION) == SUBJECT
    assert module.validate_detail(DETAIL, TENANT, OPERATION, SUBJECT) is DETAIL


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "different-tenant"),
        ("operation_id", "56d37398-518c-4f97-98ed-949b4d9d79f7"),
        ("subject_id", "56d37398-518c-4f97-98ed-949b4d9d79f7"),
    ],
)
def test_detail_cannot_change_owner_run_or_attempt(field, value):
    detail = deepcopy(DETAIL)
    detail["subject"][field] = value
    with pytest.raises(ValueError):
        module.validate_detail(detail, TENANT, OPERATION, SUBJECT)


@pytest.mark.parametrize("flag", [True, None])
def test_payload_export_is_explicitly_disabled(flag):
    detail = deepcopy(DETAIL)
    detail["payloads_exposed"] = flag
    with pytest.raises(ValueError):
        module.validate_detail(detail, TENANT, OPERATION, SUBJECT)

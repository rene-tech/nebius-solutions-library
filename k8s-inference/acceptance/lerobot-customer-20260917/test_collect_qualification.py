from __future__ import annotations

import copy
import json
from pathlib import Path
from uuid import uuid4

import collect_qualification as collector
import pytest
from collect_live import GuardError
from jsonschema import Draft202012Validator


def evidence():
    parent_id, attempt_id = str(uuid4()), str(uuid4())
    principal = collector.PREFIX + str(uuid4())
    value = {
        "database_transaction_read_only": True,
        "parent": {
            "id": parent_id,
            "principal_id": principal,
            "tenant_id": "robotics",
            "model_id": collector.MODEL,
            "stages": [{"attempts": [{"attempt_id": attempt_id}]}],
        },
        "children": [
            {
                "parent_operation_id": parent_id,
                "parent_attempt_id": attempt_id,
                "same_token": True,
                "same_principal": True,
                "same_tenant": True,
                "model_id": "cosmos3-nano",
            }
        ],
    }
    return value, parent_id, principal


def test_collector_is_exact_parent_readonly_and_credential_free():
    value, parent, principal = evidence()
    collector.validate(value, parent, principal)
    script = collector.make_script(parent, principal)
    compile(script, "<collector>", "exec")
    assert "connection.transaction(readonly=True" in script
    assert "WHERE o.id=$1 AND o.principal_id=$2 AND o.tenant_id='robotics'" in script
    assert "WHERE p.id=$1 AND p.principal_id=$2 AND p.tenant_id='robotics'" in script
    for forbidden in (
        "request_ciphertext",
        "response_ciphertext",
        "request_nonce",
        "secret",
        "->'invocations'",
        "->'environment'",
    ):
        assert forbidden not in script


@pytest.mark.parametrize(
    "change", ["principal", "tenant", "parent", "attempt", "token"]
)
def test_collector_never_accepts_a_different_owner_or_attempt(change):
    value, parent, principal = evidence()
    value = copy.deepcopy(value)
    if change in {"principal", "tenant"}:
        value["parent"][change + "_id"] = "another"
    elif change == "parent":
        value["children"][0]["parent_operation_id"] = str(uuid4())
    elif change == "attempt":
        value["children"][0]["parent_attempt_id"] = str(uuid4())
    else:
        value["children"][0]["same_token"] = False
    with pytest.raises(GuardError):
        collector.validate(value, parent, principal)


def test_collector_rejects_non_disposable_or_malformed_inputs():
    with pytest.raises(GuardError):
        collector.make_script(str(uuid4()), "actual-customer")
    with pytest.raises(ValueError):
        collector.make_script("not-uuid", collector.PREFIX + str(uuid4()))


def test_new_receipt_kind_does_not_change_old_scheduler_schema_shapes():
    root = Path(__file__).resolve().parents[2]
    schema = json.loads(
        (
            root
            / "catalog/runtime/schema/scientific-scheduler-eligibility-receipt.schema.json"
        ).read_text()
    )
    Draft202012Validator.check_schema(schema)
    kinds = schema["properties"]["acceptance_input"]["properties"]["kind"]["enum"]
    assert kinds == [
        "primary-activation-fragment",
        "secondary-public-acceptance",
        "lerobot-public-acceptance",
    ]
    old = next(
        (root / "models").glob(
            "**/activation/qualification/scheduler-eligibility-*.json"
        )
    )
    receipt = json.loads(old.read_text())
    Draft202012Validator(schema).validate(receipt)
    receipt["acceptance_input"]["kind"] = "lerobot-public-acceptance"
    Draft202012Validator(schema).validate(receipt)

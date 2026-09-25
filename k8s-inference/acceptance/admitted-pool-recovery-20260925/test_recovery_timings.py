from copy import deepcopy

import pytest

from recovery_timings import measure
from run_acceptance import FAILURE, GateError


def evidence():
    first = {"attempt_id": "first", "shard_id": "window-01", "attempt_number": 1,
             "outcome": "failed", "resource_released": True, "failure_code": FAILURE,
             "scheduling_admission": {"admitted_at": "2026-09-25T06:18:00Z", "resolved_pool_id": "h100-1x"},
             "recovery": {"state": "recovered", "admitted_wait_seconds": 120.5}}
    second = {**deepcopy(first), "attempt_id": "second", "attempt_number": 2,
              "outcome": "succeeded", "failure_code": None,
              "scheduling_admission": {"admitted_at": "2026-09-25T06:20:20Z", "resolved_pool_id": "h100-ondemand-1x"}}
    return {"operation": {"status": "succeeded"}, "batch": {"stages": [{"stage_id": "workflow", "attempts": [first, second]}]}}


def test_separates_confirmation_detection_and_destination_admission():
    result = measure(evidence())[0]
    assert result["admission_to_readmission_seconds"] == 140
    assert result["confirmed_to_readmission_seconds"] == 20
    assert result["detected_to_readmission_seconds"] == 19.5
    assert result["target_met"]


def test_late_readmission_cannot_be_presented_as_meeting_slo():
    value = evidence()
    value["batch"]["stages"][0]["attempts"][1]["scheduling_admission"]["admitted_at"] = "2026-09-25T06:25:01Z"
    with pytest.raises(GateError, match="slo_exceeded"):
        measure(value)


def test_incomplete_work_is_not_a_successful_recovery_measurement():
    value = evidence()
    value["operation"]["status"] = "running"
    with pytest.raises(GateError, match="successful_native"):
        measure(value)

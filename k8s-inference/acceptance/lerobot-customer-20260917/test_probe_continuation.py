import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "lerobot_probe_continuation", Path(__file__).with_name("run_probe_continuation.py")
)
continuation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(continuation)
driver = continuation.driver
client = continuation.client
OP = "00000000-0000-4000-8000-000000000099"


def terminal():
    return {
        "operation": {
            "id": OP,
            "status": "failed",
            "http_status": 422,
            "error_code": "INVALID_REQUEST",
            "error_detail": driver.ERROR_DETAILS["INVALID_REQUEST"],
        },
        "batch": {
            "status": "failed",
            "stages": [{"attempts": [{"resource_released": True}]}],
        },
    }


@pytest.mark.parametrize("wrong_id", [False, True])
def test_existing_invalid_selection_is_read_by_exact_id_without_admission(wrong_id):
    calls = []

    class Public:
        state = {"parent_ids": []}

        async def response(self, method, path):
            calls.append((method, path))
            value = terminal()
            if wrong_id:
                value["operation"]["id"] = "different-operation"
            return value

        def persist(self):
            pass

    previous = {
        "invalid_input": {"status": 422},
        "worker_invalid_dataset": {"operation_id": OP},
    }
    public = Public()
    if wrong_id:
        with pytest.raises(client.harness.AcceptanceError, match="identity_mismatch"):
            asyncio.run(driver.verify_existing_worker_failure(public, previous))
    else:
        asyncio.run(driver.verify_existing_worker_failure(public, previous))
        assert public.state["parent_ids"] == [OP]
    assert calls == [("GET", f"/v1/operations/{OP}")]


@pytest.mark.parametrize("children", [[], [{"id": "unexpected-child"}]])
def test_continuation_binds_unchanged_receipts_and_operator_zero_child_proof(
    tmp_path, children
):
    previous_output = tmp_path / "old"
    (previous_output / "cohort-1-blur").mkdir(parents=True)
    (previous_output / "probes").mkdir()
    release = {"sha256": "exact-release"}
    client.save(
        previous_output / "acceptance.json",
        {
            "error": "worker_failure_code_or_detail_mismatch",
            "outcome": "failed_stop_new_admissions",
            "release_receipt": release,
        },
    )
    client.save(
        previous_output / "cohort-1-blur/run.json",
        {
            "operation_id": "blur-parent",
            "outcome": "dataset_integrity_passed",
            "validations": [{}, {}],
        },
    )
    client.save(
        previous_output / "probes/run.json",
        {
            "invalid_input": {"status": 422},
            "worker_invalid_dataset": {"operation_id": OP, "terminal": terminal()},
        },
    )
    proof_path = tmp_path / "proof.json"
    client.save(
        proof_path,
        {
            "schema": "fs2-serve.nebius.ai/lerobot-parent-child-observation/v1",
            "database_transaction_read_only": True,
            "parent": {
                "id": OP,
                "status": "failed",
                "error_code": "INVALID_REQUEST",
                "stages": [{"attempts": [{"resource_released": True}]}],
            },
            "children": children,
        },
    )
    args = SimpleNamespace(
        previous_output=previous_output, zero_child_receipt=proof_path
    )
    state = {"release_receipt": release}
    originals = {path: path.read_bytes() for path in tmp_path.rglob("*.json")}
    if children:
        with pytest.raises(client.harness.AcceptanceError, match="zero_child_proof"):
            continuation.prior_inputs(args, state)
    else:
        continuation.prior_inputs(args, state)
        assert state["zero_generation_children_verified"]
        assert not state["final_cohorts_verified"]
        assert len(state["prior_receipts"]) == 4
    assert originals == {path: path.read_bytes() for path in originals}


def test_continuation_only_calls_remaining_probes(tmp_path, monkeypatch):
    blur, previous, calls = {"request": {}}, {"known": "failure"}, []
    monkeypatch.setattr(
        continuation, "prior_inputs", lambda args, state: (blur, previous)
    )

    async def probes(args, state, token, successful, *, previous):
        assert successful is blur and previous == {"known": "failure"}
        calls.append("remaining-probes")

    monkeypatch.setattr(driver, "probes", probes)
    state = {"customer_ready": False, "final_cohorts_verified": False}
    asyncio.run(
        continuation.execute(SimpleNamespace(output=tmp_path), state, "test-key")
    )
    assert calls == ["remaining-probes"]
    assert state["new_dataset_runs"] == 0 and state["validated_datasets"] == 2
    assert not state["customer_ready"] and not state["final_cohorts_verified"]

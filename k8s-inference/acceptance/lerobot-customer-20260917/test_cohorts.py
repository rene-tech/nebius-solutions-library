import asyncio
import importlib.util
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest

import client

spec = importlib.util.spec_from_file_location(
    "lerobot_cohorts", Path(__file__).with_name("run_acceptance.py")
)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
OP = "00000000-0000-4000-8000-000000000033"
SECOND = "00000000-0000-4000-8000-000000000034"


def test_matrix_has_two_real_dimensions_both_protocols_and_explicit_blur():
    assert driver.cases(False) == [
        (1, "lighting", "mcp"),
        (1, "environment", "http"),
        (2, "lighting", "http"),
        (2, "environment", "mcp"),
    ]
    assert driver.cases(True)[-1] == (2, "blur", "mcp")


@pytest.mark.parametrize(
    "invalid_status,second_status,expected",
    [
        (422, 429, None),
        (403, 429, "invalid_input_not_public_422"),
        (422, 202, "concurrency_contract_was_not_429"),
    ],
)
def test_public_negative_contract_and_cancellation(
    tmp_path, monkeypatch, invalid_status, second_status, expected
):
    calls, ids, settled_ids = [], [], []

    class FakePublic:
        def __init__(self, journal, output):
            self.state, self.output, self.http = journal, output, self

        def persist(self):
            client.save(self.output / "run.json", self.state)

        async def post(self, path, **kwargs):
            calls.append(("POST", path, kwargs))
            if kwargs["json"]["parameters"]["variants"]["count"] == 0:
                return httpx2.Response(
                    invalid_status, json={"error": {"code": "invalid_request"}}
                )
            value = (
                {"operation": {"id": SECOND}}
                if second_status == 202
                else {"error": {"code": "concurrency_exceeded"}}
            )
            return httpx2.Response(second_status, json=value)

        async def submit(self, request, key, protocol):
            ids.append(OP)
            return {"operation": {"id": OP}}

        async def response(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "POST":
                return {"id": path.split("/")[-1].split(":")[0], "status": "cancelled"}
            return {
                "operation": {"id": OP, "status": "running"},
                "batch": {
                    "status": "running",
                    "stages": [
                        {
                            "stage_id": "augment",
                            "status": "active",
                            "attempts": [
                                {
                                    "last_phase": "active_compute",
                                    "resource_released": False,
                                }
                            ],
                        }
                    ],
                },
            }

        async def wait_existing(self, timeout, expected):
            assert expected == "cancelled"
            settled_ids.append(self.state["operation_id"])
            return {
                "operation": {"id": self.state["operation_id"], "status": "cancelled"}
            }

    @asynccontextmanager
    async def connect(endpoint, token, journal, output):
        yield FakePublic(journal, output)

    monkeypatch.setattr(client, "connect", connect)
    args = SimpleNamespace(
        output=tmp_path, endpoint="https://gateway.example", timeout_seconds=10
    )
    request = {"request": {"parameters": {"variants": {"count": 1}}}}
    if expected:
        with pytest.raises(client.harness.AcceptanceError, match=expected):
            asyncio.run(driver.probes(args, {}, "not-recorded-test-key", request))
    else:
        state = {}
        asyncio.run(driver.probes(args, state, "not-recorded-test-key", request))
        assert state["probes"]["outcome"] == "expected_negative_cases_passed"
    if invalid_status != 422:
        assert ids == [] and len(calls) == 1
    else:
        assert ids == [OP] and settled_ids[-1] == OP
        journal = json.loads((tmp_path / "probes/run.json").read_text())
        if second_status == 202:
            assert journal["parent_ids"] == [OP, SECOND] and SECOND in settled_ids
        assert journal["concurrency"]["status"] == second_status


def test_running_stage_is_not_inferred_from_parent_alone():
    assert not driver.stage_running({"operation": {"status": "running"}})
    value = {
        "operation": {"status": "running"},
        "batch": {
            "stages": [
                {
                    "status": "active",
                    "attempts": [
                        {"last_phase": "image_loading", "resource_released": False}
                    ],
                }
            ]
        },
    }
    assert not driver.stage_running(value)
    attempt = value["batch"]["stages"][0]["attempts"][0]
    attempt["last_phase"] = "active_compute"
    assert driver.stage_running(value)
    attempt["resource_released"] = True
    assert not driver.stage_running(value)
    attempt["resource_released"] = False
    value["operation"]["status"] = "succeeded"
    assert not driver.stage_running(value)

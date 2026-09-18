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
INVALID = "00000000-0000-4000-8000-000000000035"


def test_matrix_has_two_real_dimensions_both_protocols_and_explicit_blur():
    assert driver.cases(False) == [
        (1, "lighting", "mcp"),
        (1, "environment", "http"),
        (2, "lighting", "http"),
        (2, "environment", "mcp"),
    ]
    assert driver.cases(True)[-1] == (2, "blur", "mcp")
    blur = json.loads((Path(__file__).parent / "blur.json").read_text())
    assert blur["variants"]["count"] == 2
    assert len(set(blur["variants"]["seeds"])) == 2
    assert blur["selection"] == {
        "episodes": [1],
        "cameras": ["observation.images.wrist"],
    }


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
            identifier = (
                INVALID
                if request["parameters"]["selection"]["episodes"] == [999]
                else OP
            )
            ids.append(identifier)
            return {"operation": {"id": identifier}}

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
            settled_ids.append(self.state["operation_id"])
            if expected == "failed":
                return {
                    "operation": {
                        "id": INVALID,
                        "status": "failed",
                        "http_status": 422,
                        "error_code": "DATASET_INVALID",
                        "error_detail": driver.ERROR_DETAILS["DATASET_INVALID"],
                    },
                    "batch": {
                        "status": "failed",
                        "stages": [{"attempts": [{"resource_released": True}]}],
                    },
                }
            assert expected == "cancelled"
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
    request = {
        "request": {
            "parameters": {
                "variants": {"count": 1},
                "selection": {"episodes": [0], "cameras": ["front"]},
            }
        }
    }
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
        assert ids == [INVALID, OP] and settled_ids[-1] == OP
        journal = json.loads((tmp_path / "probes/run.json").read_text())
        assert request["request"]["parameters"]["selection"]["episodes"] == [0]
        assert (
            journal["worker_invalid_dataset"]["phase"]
            == "expected_failure_and_released"
        )
        assert (
            journal["worker_invalid_dataset"]["zero_generation_children_verified"]
            is False
        )
        if second_status == 202:
            assert (
                journal["parent_ids"] == [INVALID, OP, SECOND] and SECOND in settled_ids
            )
        assert journal["concurrency"]["status"] == second_status


def test_worker_failure_probe_rejects_generic_or_unsafe_detail(tmp_path):
    class FakePublic:
        state = {"run_id": "test", "parent_ids": []}

        def persist(self):
            pass

        async def submit(self, request, key, protocol):
            assert protocol == "http" and request["parameters"]["selection"][
                "episodes"
            ] == [999]
            return {"operation": {"id": INVALID}}

        async def wait_existing(self, timeout, expected):
            assert expected == "failed"
            return {
                "operation": {
                    "id": INVALID,
                    "status": "failed",
                    "http_status": 422,
                    "error_code": "DATASET_INVALID",
                    "error_detail": "private exception data",
                }
            }

    with pytest.raises(
        client.harness.AcceptanceError, match="worker_failure_code_or_detail_mismatch"
    ):
        asyncio.run(
            driver.worker_failure_probe(
                FakePublic(),
                SimpleNamespace(timeout_seconds=10),
                {"parameters": {"selection": {"episodes": [0]}}},
            )
        )


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

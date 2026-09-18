import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "lerobot_targeted_debug", Path(__file__).with_name("run_targeted_debug.py")
)
targeted = importlib.util.module_from_spec(spec)
spec.loader.exec_module(targeted)


@pytest.mark.parametrize("failed_step", [None, "blur", "probes"])
def test_targeted_debug_is_bounded_and_never_qualifies_final_cohorts(
    tmp_path, monkeypatch, failed_step
):
    calls = []
    successful = {"request": {"existing": "finalized-inputs"}}

    async def run_directory(args, state, token, cohort, policy, protocol):
        calls.append((cohort, policy, protocol))
        if failed_step == "blur":
            raise targeted.driver.client.harness.AcceptanceError("blur_failed")
        state["runs"].append({"validated_datasets": 2})
        return successful

    async def probes(args, state, token, run):
        assert run is successful
        calls.append("probes")
        if failed_step == "probes":
            raise targeted.driver.client.harness.AcceptanceError("probe_failed")

    monkeypatch.setattr(targeted.driver, "run_directory", run_directory)
    monkeypatch.setattr(targeted.driver, "probes", probes)
    state = {"runs": [], "outcome": "in_progress", "customer_ready": False}
    args = SimpleNamespace(output=tmp_path)
    if failed_step:
        with pytest.raises(targeted.driver.client.harness.AcceptanceError):
            asyncio.run(targeted.execute(args, state, "test-key-not-recorded"))
        assert state["outcome"] == "in_progress"
    else:
        asyncio.run(targeted.execute(args, state, "test-key-not-recorded"))
        assert state["outcome"] == "targeted_blur_probes_passed"
        assert state["validated_datasets"] == 2
    assert calls == (
        [(1, "blur", "mcp")]
        if failed_step == "blur"
        else [(1, "blur", "mcp"), "probes"]
    )
    assert not state["final_cohorts_verified"] and not state["customer_ready"]
    assert state["scope"] == "targeted_two_variant_blur_and_negative_probes"

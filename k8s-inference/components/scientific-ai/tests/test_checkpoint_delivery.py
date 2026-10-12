"""Final-checkpoint transport loss is delivery failure, not failed simulation."""

import json
import socket

import pytest

from scientific_ai.gromacs import DeliveryPending, GromacsApplication


@pytest.mark.parametrize("failure", ["dns", "denied", "incomplete", "native-error", "inventory"])
def test_completed_native_work_is_not_recomputed_after_checkpoint_transport_loss(
    setup, monkeypatch, failure
):
    args, objects, lifecycle = setup
    app = GromacsApplication(**args)
    calls = []
    original_put = objects.put
    monkeypatch.setattr("scientific_ai.transfer.time.sleep", lambda _: None)

    def unavailable(key, source):
        if failure == "denied":
            raise PermissionError("withdrawn storage access")
        raise socket.gaierror(socket.EAI_AGAIN, "temporary DNS outage")

    # Exercise the actual worker's exception/result/inventory path. Only the
    # external engine command is replaced; native GPU execution is a live gate.
    def initialize():
        app.worker.data.mkdir()
        (app.worker.data / "final.gro").write_bytes(b"test native output")
        app.worker.version = "test-only"

    def native(step):
        calls.append(step["id"])
        app.worker.state["commands"].append(
            {"step_id": step["id"], "exit_code": 1 if failure == "native-error" else 0}
        )
        if failure != "incomplete":
            app.worker.state["completed_steps"].append(step["id"])
        objects.put = unavailable
        try:
            app.worker.checkpoint()
        finally:
            if failure == "inventory":
                (app.worker.data / "untrusted-link").symlink_to("/etc/passwd")

    monkeypatch.setattr(app.worker, "initialize", initialize)
    monkeypatch.setattr(app.worker, "run_step", native)
    if failure != "dns":
        result = app.execute()
        assert result["compute"] == "failed"
        assert not (app.workspace.parent / "DELIVERY.json").exists()
    else:
        with pytest.raises(DeliveryPending):
            app.execute()
        result = json.loads((app.workspace / "result.json").read_text())
        assert result["status"] == "succeeded"
        assert result["checkpoint_commit_pending"]
        assert result["checkpoint_publication_error_type"] == "gaierror"
        assert result["native_checkpoint_generation"] == 1
        assert result["committed_checkpoint_generation"] == 0
        assert lifecycle.head is None
        assert (app.workspace.parent / "DELIVERY.json").is_file()
        objects.put = original_put
        finalizer = GromacsApplication(**args)
        monkeypatch.setattr(finalizer.worker, "run", lambda: pytest.fail("must not rerun MD"))
        receipt = finalizer.finish_delivery()
        assert receipt["export"] == "committed" and lifecycle.done == receipt["reference"]
        assert finalizer.artifacts.load(lifecycle.done).result == result
    assert calls == ["md"]

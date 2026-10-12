import json

import pytest
from conftest import initialized_application, successful_result
from fs2_gromacs.files import atomic_json
from pydantic import ValidationError

from scientific_ai.contracts import Attempt
from scientific_ai.delivery import retained_workspace
from scientific_ai.gromacs import DeliveryPending, GromacsApplication
from scientific_ai.lifecycle import FileLifecycle


def pending(setup):
    app, objects, lifecycle = initialized_application(setup)
    app.worker.checkpoint()
    result = successful_result(app)
    lifecycle.fail_ack = True
    with pytest.raises(DeliveryPending):
        app.finish_delivery(result)
    lifecycle.fail_ack = False
    finalizer = Attempt.model_validate(
        {
            **app.attempt.model_dump(),
            "attempt_role": "finalize",
            "gpus": 0,
            "gpu_class": None,
            "epoch": 2,
            "attempt_id": "delivery-attempt-1",
        }
    )
    lifecycle.current = finalizer
    return app, objects, lifecycle, finalizer


def test_cpu_delivery_preserves_native_result_and_never_executes_md(setup, monkeypatch):
    args, _, _ = setup
    app, _, lifecycle, finalizer = pending(setup)
    marker = (app.workspace.parent / "DELIVERY.json").read_bytes()
    path = retained_workspace(
        app.workspace.parent,
        attempt=finalizer,
        run=app.run_spec,
        profile=app.profile,
        compute_pod=app.attempt.attempt_id,
        compute_epoch=1,
    )
    delivery = GromacsApplication(**{**args, "attempt": finalizer, "workspace": path})
    monkeypatch.setattr(delivery.worker, "run", lambda: pytest.fail("CPU finalizer executed MD"))
    with pytest.raises(PermissionError, match="never execute"):
        delivery.execute()
    receipt = delivery.finish_delivery()
    assert receipt["export"] == "committed"
    snapshot = delivery.artifacts.load(lifecycle.done)
    assert snapshot.attempt_id == finalizer.attempt_id and snapshot.epoch == 2
    assert snapshot.result == json.loads((app.workspace / "result.json").read_bytes())
    assert (app.workspace.parent / "DELIVERY.json").read_bytes() == marker


@pytest.mark.parametrize("change", ["pod", "epoch", "result", "symlink", "image", "input", "tenant"])
def test_delivery_refuses_stale_tampered_or_foreign_workspace(setup, tmp_path, change):
    app, _, _, finalizer = pending(setup)
    pod, epoch, run = app.attempt.attempt_id, 1, app.run_spec
    if change == "pod":
        pod = "another-pod"
    elif change == "epoch":
        epoch = 9
    elif change == "result":
        result = json.loads((app.workspace / "result.json").read_bytes())
        atomic_json(app.workspace / "result.json", {**result, "status": "failed"})
    elif change == "symlink":
        (app.workspace.parent / "DELIVERY.json").rename(tmp_path / "stolen-marker.json")
        (app.workspace.parent / "DELIVERY.json").symlink_to(tmp_path / "stolen-marker.json")
    elif change == "image":
        finalizer = finalizer.model_copy(update={"image": app.profile.images["l40s"]})
    elif change == "input":
        run = run.model_copy(
            update={"input_bundle": run.input_bundle.model_copy(update={"sha256": "b" * 64})}
        )
    else:
        finalizer = finalizer.model_copy(
            update={"owner": finalizer.owner.model_copy(update={"tenant_id": "other"})}
        )
    with pytest.raises((ValueError, PermissionError)):
        retained_workspace(
            app.workspace.parent,
            attempt=finalizer,
            run=run,
            profile=app.profile,
            compute_pod=pod,
            compute_epoch=epoch,
        )


def test_finalizer_role_requires_zero_gpu_allocation(setup):
    original = setup[0]["attempt"].model_dump()
    with pytest.raises(ValidationError, match="no GPU"):
        Attempt.model_validate({**original, "attempt_role": "finalize"})
    with pytest.raises(ValidationError, match="exactly one"):
        Attempt.model_validate({**original, "gpus": 0, "gpu_class": None})


@pytest.mark.parametrize("role_field", ["role", "attempt_role"])
def test_file_bridge_requires_platform_finalizer_role(setup, tmp_path, role_field):
    app, _, _, attempt = pending(setup)
    record = {
        "pod": attempt.attempt_id,
        "root": attempt.root_operation_id,
        "epoch": 2,
        "tenant": attempt.owner.tenant_id,
        "region": attempt.cluster,
        "image": attempt.image,
        "gpus": 0,
        role_field: "finalize",
        "status": "started",
    }
    atomic_json(tmp_path / "attempts/.ack.json", record)
    bridge = FileLifecycle(tmp_path)
    assert bridge.discover(pod_name=attempt.attempt_id, owner=attempt.owner, profile=app.profile) == attempt
    record.pop(role_field)
    atomic_json(tmp_path / "attempts/.ack.json", record)
    with pytest.raises(ValidationError):
        bridge.discover(pod_name=attempt.attempt_id, owner=attempt.owner, profile=app.profile)

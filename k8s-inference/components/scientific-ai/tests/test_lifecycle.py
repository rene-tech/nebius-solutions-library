import json
import threading
import time

import pytest
from fs2_gromacs.files import atomic_json

from scientific_ai.lifecycle import FileLifecycle


def record(attempt):
    return {
        "pod": attempt.attempt_id,
        "epoch": attempt.epoch,
        "root": attempt.root_operation_id,
        "tenant": attempt.owner.tenant_id,
        "region": attempt.cluster,
        "gpu_class": attempt.gpu_class,
        "image": attempt.image,
        "status": "started",
    }


def test_cluster_identity_is_not_the_physical_region(setup, tmp_path):
    args, _, _ = setup
    attempt = args["attempt"]
    atomic_json(
        tmp_path / "attempts/.ack.json",
        {**record(attempt), "region": "eu-south1", "cluster": attempt.cluster},
    )
    bridge = FileLifecycle(tmp_path)
    assert (
        bridge.discover(pod_name=attempt.attempt_id, owner=attempt.owner, profile=args["profile"]) == attempt
    )


def test_foreign_cluster_is_not_authorized_by_a_matching_region_name(setup, tmp_path):
    args, _, _ = setup
    attempt = args["attempt"]
    atomic_json(
        tmp_path / "attempts/.ack.json",
        {**record(attempt), "cluster": "foreign-cluster"},
    )
    with pytest.raises(PermissionError):
        FileLifecycle(tmp_path).discover(
            pod_name=attempt.attempt_id, owner=attempt.owner, profile=args["profile"]
        )


def test_file_bridge_waits_for_authenticated_durable_pointer_and_publishes_result(setup, tmp_path):
    args, _, _ = setup
    attempt = args["attempt"]
    work = tmp_path / "bridge"
    ack = work / "attempts/.ack.json"
    atomic_json(ack, record(attempt))
    bridge = FileLifecycle(work, timeout=1, poll_seconds=0.005)
    assert (
        bridge.discover(pod_name=attempt.attempt_id, owner=attempt.owner, profile=args["profile"]) == attempt
    )
    stop = threading.Event()

    def uploader():
        while not stop.wait(0.005):
            if not (work / "CHECKPOINT.json").exists():
                continue
            pointer = json.loads((work / "CHECKPOINT.json").read_text())
            response = {
                **record(attempt),
                "checkpoint_ref": pointer["checkpoint_ref"],
                "checkpoint_generation": pointer["generation"],
            }
            if (work / "RESULT.json").exists():
                response.update(json.loads((work / "RESULT.json").read_text()))
            atomic_json(ack, response)

    worker = threading.Thread(target=uploader)
    worker.start()
    try:
        bridge.checkpoint(attempt, "a" * 64, 3)
        bridge.finalize(attempt, "b" * 64)
        assert bridge.generation == 4
        assert json.loads(ack.read_text())["result_ref"] == "b" * 64
    finally:
        stop.set()
        worker.join()


def test_no_ack_does_not_report_checkpoint_committed(setup, tmp_path):
    attempt = setup[0]["attempt"]
    atomic_json(tmp_path / "attempts/.ack.json", record(attempt))
    bridge = FileLifecycle(tmp_path, timeout=0.03, poll_seconds=0.005)
    with pytest.raises(TimeoutError):
        bridge.checkpoint(attempt, "a" * 64, 1)
    assert bridge.generation == 0


def test_replaced_epoch_cannot_publish(setup, tmp_path):
    attempt = setup[0]["attempt"]
    atomic_json(tmp_path / "attempts/.ack.json", {**record(attempt), "epoch": attempt.epoch + 1})
    bridge = FileLifecycle(tmp_path)
    with pytest.raises(PermissionError):
        bridge.checkpoint(attempt, "a" * 64, 1)
    assert not (tmp_path / "CHECKPOINT.json").exists()


def test_refused_pointer_is_not_silently_accepted(setup, tmp_path):
    attempt = setup[0]["attempt"]
    ack = tmp_path / "attempts/.ack.json"
    atomic_json(ack, record(attempt))
    bridge = FileLifecycle(tmp_path, timeout=0.1, poll_seconds=0.005)

    def reject():
        time.sleep(0.01)
        atomic_json(ack, {"detail": "private upstream error"})

    worker = threading.Thread(target=reject)
    worker.start()
    try:
        with pytest.raises(PermissionError, match="refused") as exc:
            bridge.checkpoint(attempt, "a" * 64, 1)
        assert "private" not in str(exc.value)
    finally:
        worker.join()

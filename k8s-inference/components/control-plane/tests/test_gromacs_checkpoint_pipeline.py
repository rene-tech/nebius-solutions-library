"""Bounded metadata overlap preserves the stopped-native commit boundary."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from test_gromacs_checkpoints import Artifacts, invocation, ready

from fs2_serve.scientific_batch.companion import WorkloadArtifactHttpClient
from fs2_serve.scientific_batch.gromacs_checkpoints import GromacsCheckpointTransport
from fs2_serve.scientific_batch.native_workflows import workflow_for_collector


def checkpoint(tmp_path, count=192, *, model="gromacs"):
    client = Artifacts()
    transport = GromacsCheckpointTransport(
        client, invocation(), tmp_path, workflow=workflow_for_collector(f"{model}-workflow-v1"),
    )
    transport.data.mkdir()
    for number in range(count):
        (transport.data / f"part-{number:05d}.xtc").write_bytes(f"native part {number}".encode())
    (transport.data / "same-part-alias.xtc").write_bytes((transport.data / "part-00000.xtc").read_bytes())
    ready(tmp_path, 1)
    return transport, client


@pytest.mark.parametrize("model", ["gromacs", "gromacs-mpi"])
@pytest.mark.parametrize("phase", ["checkpoint", "final-rehome"])
def test_two_cohorts_overlap_but_final_commit_is_ordered_and_aliases_share_bytes(tmp_path, monkeypatch, model, phase):
    transport, client = checkpoint(tmp_path, model=model)
    active = maximum = calls = 0
    lock = threading.Lock()
    first_pair = threading.Barrier(2)
    finished = []
    exported = []

    def upload_files(*, paths, on_phase, **kwargs):
        nonlocal active, maximum, calls
        with lock:
            calls += 1
            number = calls
            active += 1
            maximum = max(maximum, active)
        try:
            on_phase("platform-begin")
            if number <= 2:
                first_pair.wait(timeout=5)
            on_phase("platform-transfer")
            refs = [client.upload_file(path=path, **kwargs) for path in paths]
            on_phase("platform-finalize")
            finished.extend(path.name for path in paths)
            return refs
        finally:
            with lock:
                active -= 1

    def export(state, files):
        assert active == 0 and len(finished) == 192
        exported.append(state["generation"])

    monkeypatch.setattr(client, "upload_files", upload_files)
    transport.customer = SimpleNamespace(publish=export)
    if phase == "checkpoint":
        transport.publish_ready()
        assert exported == [1] and transport.generation == 1
        manifest = json.loads(client.objects[client.latest["artifact_id"]])
        assert len(manifest["files"]) == 193
        assert [item["path"] for item in manifest["files"]] == sorted(transport.files)
        assert (transport.meta / "checkpoint-ack.json").is_file()
    else:
        initial = json.loads((transport.meta / "checkpoint-ready.json").read_text())
        transport.files = {
            item["path"]: {**item, "uploaded_attempt": "prior", "artifact": {"artifact_id": str(uuid4())}}
            for item in initial["files"]
        }
        transport._prepare_final_references()
        assert transport.final_references_prepared and len(transport.final_files) == 192
        assert not exported and not (transport.meta / "checkpoint-ack.json").exists()
    assert maximum == 2 and active == 0 and calls == 3 and client.uploads == 192
    progress = json.loads((transport.meta / "transfer-progress.json").read_text())
    assert progress["completed_files"] == 193 and progress["max_parallel_cohorts"] == 2
    assert progress["active_cohort_phases"] == {}
    assert "platform-pipeline" in progress["phase_seconds"]
    assert set(progress["cohort_phase_seconds"]) == {
        "platform-begin", "platform-transfer", "platform-finalize",
    }
    # Parallel work is explicitly separate from non-overlapping wall phases.
    assert sum(progress["phase_seconds"].values()) <= progress["elapsed_seconds"] + 0.001
    assert all(value >= 0 for value in progress["cohort_phase_seconds"].values())


@pytest.mark.parametrize("failure", [RuntimeError, CancelledError])
@pytest.mark.parametrize("phase", ["checkpoint", "final-rehome"])
def test_failure_or_cancellation_drains_peer_cohort_before_return_without_committing(
    tmp_path, monkeypatch, failure, phase,
):
    transport, client = checkpoint(tmp_path, count=256)
    first_started, second_started, release_peer = threading.Event(), threading.Event(), threading.Event()
    second_finished = threading.Event()
    calls = []
    exported = []

    def upload_files(*, paths, **kwargs):
        calls.append(paths)
        if paths[0].name == "part-00000.xtc":
            first_started.set()
            assert second_started.wait(5)
            raise failure("injected cohort failure")
        second_started.set()
        assert release_peer.wait(5)
        second_finished.set()
        kwargs.pop("on_phase", None)
        return [client.upload_file(path=path, **kwargs) for path in paths]

    monkeypatch.setattr(client, "upload_files", upload_files)
    transport.customer = SimpleNamespace(publish=lambda *args: exported.append(args))
    if phase == "checkpoint":
        action = transport.publish_ready
    else:
        initial = json.loads((transport.meta / "checkpoint-ready.json").read_text())
        transport.files = {
            item["path"]: {**item, "uploaded_attempt": "prior", "artifact": {"artifact_id": str(uuid4())}}
            for item in initial["files"]
        }
        action = transport._prepare_final_references
    with ThreadPoolExecutor(max_workers=1) as executor:
        task = executor.submit(action)
        try:
            assert first_started.wait(5) and second_started.wait(5)
            assert not task.done() and not second_finished.is_set()
            assert not exported and not (transport.meta / "checkpoint-ack.json").exists()
        finally:
            release_peer.set()
        with pytest.raises(failure, match="injected cohort failure"):
            task.result(timeout=5)
    assert len(calls) == 2 and second_finished.is_set()
    assert not exported and transport.generation == 0 and not transport.final_references_prepared
    assert not (transport.meta / "checkpoint-ack.json").exists()


def test_file_changed_after_closed_inventory_cannot_be_acknowledged(tmp_path, monkeypatch):
    transport, client = checkpoint(tmp_path, count=128)
    original = client.upload_files
    exported = []

    def mutate_between_validation_and_upload(*, paths, **kwargs):
        if paths[0].name == "part-00000.xtc":
            paths[0].write_bytes(b"different stopped bytes")
        return original(paths=paths, **kwargs)

    monkeypatch.setattr(client, "upload_files", mutate_between_validation_and_upload)
    transport.customer = SimpleNamespace(publish=lambda *args: exported.append(args))
    with pytest.raises(ValueError, match="stopped checkpoint bytes"):
        transport.publish_ready()
    assert not exported and transport.generation == 0
    assert not (transport.meta / "checkpoint-ack.json").exists()


def test_inflight_file_byte_bound_serializes_large_or_oversized_cohorts(tmp_path, monkeypatch):
    transport, client = checkpoint(tmp_path, count=1)
    sizes = [600 * 1024**2, 600 * 1024**2, 2 * 1024**3, 600 * 1024**2]
    items = [{"path": str(i), "size_bytes": size, "sha256": f"{i:064x}"} for i, size in enumerate(sizes)]
    active = maximum = 0
    lock = threading.Lock()

    def upload_files(*, paths, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.01)
        refs = [{"sha256": items[int(path.name)]["sha256"], "size_bytes": sizes[int(path.name)]} for path in paths]
        with lock:
            active -= 1
        return refs

    monkeypatch.setattr(client, "upload_files", upload_files)
    transport._begin_progress("publish", 1, len(items))
    groups = list(transport._upload_native_groups(items))
    assert len(groups) == 4 and all(len(group) == 1 for group, _ in groups)
    assert maximum == 1 and active == 0


def test_two_http_cohorts_share_eight_put_lanes_and_keep_retry_identity(tmp_path, monkeypatch):
    prepared = {}
    attempts = {}
    active = maximum = 0
    lock = threading.Lock()
    begun = threading.Barrier(2)
    eight_entered, release = threading.Event(), threading.Event()
    failed_once = False

    def handler(request):
        nonlocal active, maximum, failed_once
        if request.url.path.endswith("uploads:batch"):
            uploads = json.loads(request.content)["uploads"]
            with lock:
                prepared.update((item["upload_id"], item) for item in uploads)
            begun.wait(timeout=5)
            return httpx.Response(200, json=[{
                "upload_id": item["upload_id"],
                "handle": {"method": "PUT", "url": f"https://objects.test/{item['upload_id']}", "headers": {}},
            } for item in uploads])
        if request.method == "PUT":
            identity = request.url.path.removeprefix("/")
            with lock:
                active += 1
                maximum = max(maximum, active)
                attempts[identity] = attempts.get(identity, 0) + 1
                if active == 8:
                    eight_entered.set()
                fail = not failed_once
                failed_once = True
            try:
                assert release.wait(5)
                assert hashlib.sha256(request.read()).hexdigest() == prepared[identity]["sha256"]
                return httpx.Response(503 if fail else 200)
            finally:
                with lock:
                    active -= 1
        if request.url.path.endswith("uploads:finalize"):
            identities = json.loads(request.content)["upload_ids"]
            return httpx.Response(200, json=[{
                "artifact_id": identity,
                "sha256": prepared[identity]["sha256"],
                "size_bytes": prepared[identity]["size_bytes"],
                "media_type": prepared[identity]["media_type"],
            } for identity in identities])
        raise AssertionError("unexpected HTTP path")

    monkeypatch.setattr("fs2_serve.scientific_batch.companion._ARTIFACT_UPLOAD_BASE_BACKOFF_SECONDS", 0)
    paths = []
    for number in range(128):
        path = tmp_path / str(number)
        path.write_bytes(f"immutable file {number}".encode())
        paths.append(path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = WorkloadArtifactHttpClient(base_url="https://platform.test", capability="test", client=http)
        with ThreadPoolExecutor(max_workers=2) as executor:
            tasks = [executor.submit(
                client.upload_files, identity="native", paths=tuple(paths[offset:offset + 64]),
                media_type="application/octet-stream", compression=None,
            ) for offset in (0, 64)]
            try:
                assert eight_entered.wait(5)
                assert active == maximum == 8
            finally:
                release.set()
            results = [task.result(timeout=5) for task in tasks]
    assert [len(group) for group in results] == [64, 64]
    assert active == 0 and maximum == 8 and len(attempts) == 128
    assert sum(attempts.values()) == 129 and sorted(attempts.values())[-1] == 2


@pytest.mark.parametrize("lost_phase", ["begin", "put", "finalize"])
def test_native_files_recover_when_response_is_lost_after_server_acceptance(tmp_path, monkeypatch, lost_phase):
    """Replay accepted writes; especially the batched finalize that stopped Lynx."""
    reservations, objects, committed = {}, {}, {}
    calls = {"begin": [], "put": [], "finalize": []}
    lost = False

    def handle(request):
        nonlocal lost
        if request.url.path.endswith("uploads:batch"):
            phase = "begin"
            uploads = json.loads(request.content)["uploads"]
            for item in uploads:
                reservations.setdefault(item["upload_id"], item)
                assert reservations[item["upload_id"]] == item
            response = httpx.Response(200, json=[{
                "upload_id": item["upload_id"],
                "handle": {"method": "PUT", "url": f"https://objects.test/{item['upload_id']}", "headers": {}},
            } for item in uploads])
        elif request.method == "PUT":
            phase = "put"
            identity = request.url.path.removeprefix("/")
            content = request.read()
            assert hashlib.sha256(content).hexdigest() == reservations[identity]["sha256"]
            objects.setdefault(identity, content)
            assert objects[identity] == content
            response = httpx.Response(200)
        else:
            phase = "finalize"
            assert request.url.path.endswith("uploads:finalize")
            identities = json.loads(request.content)["upload_ids"]
            for identity in identities:
                assert identity in objects
                committed.setdefault(identity, {
                    "artifact_id": identity,
                    **{key: reservations[identity][key] for key in ("sha256", "size_bytes", "media_type")},
                })
            response = httpx.Response(200, json=[committed[identity] for identity in identities])
        calls[phase].append(request.content)
        if phase == lost_phase and not lost:
            lost = True
            raise httpx.RemoteProtocolError("Server disconnected without sending a response", request=request)
        return response

    monkeypatch.setattr("fs2_serve.scientific_batch.companion._ARTIFACT_UPLOAD_BASE_BACKOFF_SECONDS", 0)
    path = tmp_path / "production.cpt"
    path.write_bytes(b"closed native checkpoint")
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = WorkloadArtifactHttpClient(base_url="https://platform.test", capability="test", client=http)
        refs = client.upload_files(identity="native-checkpoint-67", paths=(path,),
                                   media_type="application/octet-stream", compression=None)
    assert lost and len(reservations) == len(objects) == len(committed) == len(refs) == 1
    assert refs == list(committed.values())
    assert calls[lost_phase] == [calls[lost_phase][0]] * 2
    assert all(len(requests) == (2 if phase == lost_phase else 1) for phase, requests in calls.items())

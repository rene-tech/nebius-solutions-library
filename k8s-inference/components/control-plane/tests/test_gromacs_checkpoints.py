import asyncio
import hashlib
import json
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import pytest
from fs2_gromacs.files import atomic_json, inventory

from fs2_serve.scientific_batch.companion import WorkloadArtifactHttpClient
from fs2_serve.scientific_batch.gromacs_checkpoints import GromacsCheckpointTransport
from fs2_serve.scientific_batch.native_workflows import workflow_for_collector

OPERATION = "9eb1af68-cee7-46ea-9c3c-270e039ba923"


class Artifacts:
    def __init__(self, checkpoint_media="application/vnd.fs2.gromacs-checkpoint+json"):
        self.base_url = "http://api.test"
        self.headers = {}
        self.objects = {}
        self.latest = None
        self.uploads = 0
        self.addresses = {}
        self.checkpoint_media = checkpoint_media

    def _download_get(self, url, **kwargs):
        return {"checkpoint": self.latest}

    def upload(self, *, identity, content, media_type, compression):
        digest = hashlib.sha256(content).hexdigest()
        # Mirror the real repository's unique content address reservation.
        # The old fake accepted duplicate identities and hid a hosted 409.
        if digest in self.addresses:
            previous_identity, artifact = self.addresses[digest]
            if previous_identity != identity:
                raise ValueError("this content address is already reserved")
            if (artifact["media_type"], artifact["compression"]) != (media_type, compression or "none"):
                raise ValueError("upload identity has conflicting immutable metadata")
            return artifact
        artifact = {
            "artifact_id": str(uuid4()),
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "media_type": media_type,
            "compression": compression or "none",
        }
        self.objects[artifact["artifact_id"]] = content
        self.addresses[digest] = (identity, artifact)
        if media_type == self.checkpoint_media:
            self.latest = artifact
        else:
            self.uploads += 1
        return artifact

    def upload_file(self, *, path, **kwargs):
        return self.upload(content=path.read_bytes(), **kwargs)

    def upload_files(self, *, paths, on_phase=None, **kwargs):
        return [self.upload_file(path=path, **kwargs) for path in paths]

    def download(self, artifact_id, **kwargs):
        return self.objects[str(artifact_id)]

    def prepare_downloads(self, artifact_ids):
        assert 1 <= len(artifact_ids) <= 128

    def download_file(self, artifact_id, *, destination, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.objects[str(artifact_id)])


def invocation():
    return SimpleNamespace(
        argv=("--operation-id", OPERATION), shard_id="replica", produces="output", max_output_bytes=10000
    )


def ready(root, generation, engine="gromacs"):
    atomic_json(
        root / ".fs2/checkpoint-ready.json",
        {
            "state": {
                "schema": f"fs2-serve.nebius.ai/{engine}-checkpoint/v1",
                "operation_id": OPERATION,
                "job_id": "replica",
                "generation": generation,
                "recipe_sha256": "a" * 64,
            },
            "files": inventory(root / "data", max_bytes=10000),
        },
    )


@pytest.fixture(autouse=True)
def no_customer_export_in_transport_unit_tests(monkeypatch):
    monkeypatch.setattr("fs2_serve.scientific_batch.gromacs_storage.GromacsCustomerStorage.initialize", lambda _: None)


def test_checkpoint_commit_and_restore_reuse_closed_segments(tmp_path, monkeypatch):
    monkeypatch.setenv("FS2_ATTEMPT_ID", str(uuid4()))
    client = Artifacts()
    first = tmp_path / "first"
    (first / "data").mkdir(parents=True)
    transport = GromacsCheckpointTransport(client, invocation(), first)
    transport.restore()
    (first / "data/md.part0001.xtc").write_bytes(b"trajectory segment")
    (first / "data/native.cpt").write_bytes(b"checkpoint one")
    ready(first, 1)
    transport.publish_ready()
    assert client.uploads == 2
    assert json.loads((first / ".fs2/checkpoint-ack.json").read_text())["generation"] == 1
    (first / "data/native.cpt").write_bytes(b"checkpoint two")
    ready(first, 2)
    transport.publish_ready()
    assert client.uploads == 3  # unchanged trajectory is not reuploaded
    second = tmp_path / "new-node"
    resumed = GromacsCheckpointTransport(client, invocation(), second)
    resumed.restore()
    assert resumed.generation == 2
    progress = json.loads((second / ".fs2/transfer-progress.json").read_text())
    assert progress["phase"] == "restored" and progress["completed_files"] == 2
    assert progress["elapsed_seconds"] >= 0
    assert set(progress["phase_seconds"]) >= {"validation", "restore-authorize", "restore-transfer"}
    assert (second / "data/native.cpt").read_bytes() == b"checkpoint two"
    assert (second / "data/md.part0001.xtc").read_bytes() == b"trajectory segment"


def test_prepared_restore_handle_is_reused_for_immutable_file_aliases(tmp_path):
    artifact_id = uuid4()
    content = b"the same checkpoint under two native filenames"
    sha256 = hashlib.sha256(content).hexdigest()
    requests = []

    def reply(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/internal/scientific-workloads/artifacts:download":
            return httpx.Response(
                200,
                json=[
                    {
                        "artifact": {
                            "artifact_id": str(artifact_id),
                            "sha256": sha256,
                            "size_bytes": len(content),
                            "media_type": "application/octet-stream",
                        },
                        "handle": {"method": "GET", "url": "https://object.test/closed"},
                    }
                ],
            )
        assert request.url.host == "object.test" and request.url.path == "/closed"
        return httpx.Response(200, stream=httpx.ByteStream(content))

    client = WorkloadArtifactHttpClient(
        base_url="https://api.test",
        capability="internal-test",
        client=httpx.Client(transport=httpx.MockTransport(reply)),
    )
    client.prepare_downloads((artifact_id,))
    for name in ("md.gro", "md.part0001.gro"):
        client.download_file(
            artifact_id,
            destination=tmp_path / name,
            expected_digest="sha256:" + sha256,
            expected_size_bytes=len(content),
            expected_media_type="application/octet-stream",
        )
        assert (tmp_path / name).read_bytes() == content
    assert requests == [
        ("POST", "/internal/scientific-workloads/artifacts:download"),
        ("GET", "/closed"),
        ("GET", "/closed"),
    ]


@pytest.mark.parametrize("corrupt", [False, True])
def test_late_same_operation_restore_is_bounded_parallel_and_commits_only_verified_files(tmp_path, corrupt):
    client = Artifacts()
    first = tmp_path / "first"
    (first / "data").mkdir(parents=True)
    for index in range(2048):
        (first / "data" / f"part-{index:05d}").write_bytes(index.to_bytes(4, "big"))
    original = GromacsCheckpointTransport(client, invocation(), first)
    original.restore()
    ready(first, 1)
    original.publish_ready()
    prepared = []
    counters = {"active": 0, "peak": 0, "done": 0}
    lock = threading.Lock()
    base_download = client.download_file

    def checked_download(artifact_id, *, destination, **kwargs):
        with lock:
            counters["active"] += 1
            counters["peak"] = max(counters["peak"], counters["active"])
        try:
            time.sleep(0.001)
            content = client.objects[str(artifact_id)]
            if corrupt and content == (128).to_bytes(4, "big"):
                content += b"corrupt"
            if "sha256:" + hashlib.sha256(content).hexdigest() != kwargs["expected_digest"]:
                raise ValueError("artifact stream differs from its immutable digest")
            assert len(content) == kwargs["expected_size_bytes"]
            base_download(artifact_id, destination=destination, **kwargs)
            with lock:
                counters["done"] += 1
        finally:
            with lock:
                counters["active"] -= 1

    client.prepare_downloads = prepared.append
    client.download_file = checked_download
    second = tmp_path / "replacement"
    resumed = GromacsCheckpointTransport(client, invocation(), second)
    if corrupt:
        with pytest.raises(ValueError, match="immutable digest"):
            resumed.restore()
        assert not (second / ".fs2/restore-complete.json").exists()
        assert not (second / ".fs2/gromacs-state.json").exists()
        assert resumed.generation == 0
    else:
        resumed.restore()
        assert counters["done"] == 2048
        assert len(prepared) == 16 and all(len(group) == 128 for group in prepared)
        assert resumed.generation == 1
        assert (second / ".fs2/restore-complete.json").exists()
        for index in range(2048):
            assert (second / "data" / f"part-{index:05d}").read_bytes() == index.to_bytes(4, "big")
    assert 1 < counters["peak"] <= 8


def test_failed_diagnostic_reuses_exact_committed_artifact_and_keeps_new_bytes_uncommitted(tmp_path, monkeypatch):
    monkeypatch.setenv("FS2_ATTEMPT_ID", str(uuid4()))
    client = Artifacts()
    (tmp_path / "data").mkdir()
    native = tmp_path / "data/native.log"
    native.write_bytes(b"original native failure")
    transport = GromacsCheckpointTransport(client, invocation(), tmp_path)
    transport.restore()
    ready(tmp_path, 1)
    transport.publish_ready()
    committed = transport.files["native.log"]["artifact"]
    uploads = client.uploads
    assert transport.diagnostic_file_reference(native) == committed
    assert client.uploads == uploads
    assert committed["media_type"] == "application/octet-stream"
    native.write_bytes(b"new failure bytes not in checkpoint")
    new = transport.diagnostic_file_reference(native)
    assert new["sha256"] != committed["sha256"]
    assert new["media_type"] == "application/octet-stream"
    assert transport.generation == 1
    assert transport.files["native.log"]["artifact"] == committed
    alias = tmp_path / "data/native.txt"
    alias.write_bytes(native.read_bytes())
    assert transport.diagnostic_file_reference(alias) == new
    assert client.uploads == uploads + 1
    assert json.loads((tmp_path / ".fs2/checkpoint-ack.json").read_text())["generation"] == 1


@pytest.mark.parametrize("engine", ["lammps", "namd", "amber"])
def test_native_engines_share_transport_without_conflating_checkpoint_formats(tmp_path, engine):
    workflow = workflow_for_collector(f"{engine}-workflow-v1")
    client = Artifacts(workflow.checkpoint_media_type)
    first = tmp_path / "first"
    (first / "data").mkdir(parents=True)
    transport = GromacsCheckpointTransport(client, invocation(), first, workflow=workflow)
    transport.restore()
    (first / "data/closed-trajectory").write_bytes(b"closed native segment")
    ready(first, 1, engine)
    transport.publish_ready()
    assert client.latest["media_type"] == workflow.checkpoint_media_type
    assert client.uploads == 1

    replacement = tmp_path / "replacement"
    resumed = GromacsCheckpointTransport(client, invocation(), replacement, workflow=workflow)
    resumed.restore()
    assert resumed.generation == 1
    assert (replacement / "data/closed-trajectory").read_bytes() == b"closed native segment"
    assert (
        json.loads((replacement / ".fs2" / workflow.state_filename).read_text())["schema"] == workflow.checkpoint_schema
    )
    assert not (replacement / ".fs2/gromacs-state.json").exists()

    other_engine = "lammps" if engine != "lammps" else "namd"
    wrong = workflow_for_collector(f"{other_engine}-workflow-v1")
    with pytest.raises(ValueError, match="size/type"):
        GromacsCheckpointTransport(client, invocation(), tmp_path / "wrong", workflow=wrong).restore()


@pytest.mark.parametrize("engine", ["lammps", "namd", "amber"])
def test_native_transport_rejects_gromacs_state_under_another_engine_binding(tmp_path, engine):
    (tmp_path / "data").mkdir()
    ready(tmp_path, 1)
    workflow = workflow_for_collector(f"{engine}-workflow-v1")
    with pytest.raises(ValueError, match="another workflow"):
        GromacsCheckpointTransport(Artifacts(), invocation(), tmp_path, workflow=workflow).publish_ready()


def test_partial_upload_does_not_commit_a_generation(tmp_path):
    client = Artifacts()
    (tmp_path / "data").mkdir()
    (tmp_path / "data/cpt").write_bytes(b"native")
    transport = GromacsCheckpointTransport(client, invocation(), tmp_path)
    transport.restore()
    ready(tmp_path, 1)

    def fail(**kwargs):
        raise ConnectionError("injected interrupted upload")

    client.upload_file = fail
    with pytest.raises(ConnectionError):
        transport.publish_ready()
    assert client.latest is None and not (tmp_path / ".fs2/checkpoint-ack.json").exists()
    assert json.loads((tmp_path / ".fs2/transport-error.json").read_text()) == {"status": "failed", "phase": "publish"}


def test_restore_failure_notifies_the_engine_without_provider_details(tmp_path, monkeypatch):
    transport = GromacsCheckpointTransport(Artifacts(), invocation(), tmp_path)

    def fail():
        raise RuntimeError("provider details that must not enter the workspace")

    monkeypatch.setattr(transport.customer, "initialize", fail)
    with pytest.raises(RuntimeError):
        transport.restore()
    assert json.loads((tmp_path / ".fs2/transport-error.json").read_text()) == {"status": "failed", "phase": "restore"}
    assert not (tmp_path / ".fs2/restore-complete.json").exists()


def test_checkpoint_cannot_claim_another_replica(tmp_path):
    (tmp_path / "data").mkdir()
    ready(tmp_path, 1)
    raw = json.loads((tmp_path / ".fs2/checkpoint-ready.json").read_text())
    raw["state"]["job_id"] = "someone-else"
    atomic_json(tmp_path / ".fs2/checkpoint-ready.json", raw)
    with pytest.raises(ValueError, match="another workflow"):
        GromacsCheckpointTransport(Artifacts(), invocation(), tmp_path).publish_ready()


def test_native_aliases_share_one_content_address_and_keep_every_filename(tmp_path, monkeypatch):
    client = Artifacts()
    (tmp_path / "data").mkdir()
    for name in ("md.gro", "md.part0001.gro", "saved-coordinate.backup"):
        (tmp_path / "data" / name).write_bytes(b"same complete coordinates")
    monkeypatch.setenv("FS2_ATTEMPT_ID", "first")
    transport = GromacsCheckpointTransport(client, invocation(), tmp_path)
    transport.restore()
    ready(tmp_path, 1)
    transport.publish_ready()
    assert client.uploads == 1
    manifest = json.loads(client.objects[client.latest["artifact_id"]])
    assert len(manifest["files"]) == 3
    refs = [transport.final_file_reference(tmp_path / "data" / item["path"]) for item in manifest["files"]]
    assert refs[0] == refs[1] == refs[2]
    assert client.uploads == 1

    # Simulate a new attempt with an independent scoped reservation table,
    # while preserving the prior attempt's immutable artifact read access.
    client.addresses.clear()
    monkeypatch.setenv("FS2_ATTEMPT_ID", "replacement")
    recovered = GromacsCheckpointTransport(client, invocation(), tmp_path / "replacement")
    recovered.restore()
    refs = [recovered.final_file_reference(recovered.data / item["path"]) for item in manifest["files"]]
    assert refs[0] == refs[1] == refs[2]
    assert client.uploads == 2


@pytest.mark.parametrize("fault", [None, "corrupt", "upload"])
def test_late_recovered_final_files_are_rehomed_in_bounded_batches(tmp_path, monkeypatch, fault):
    client = Artifacts()
    transport = GromacsCheckpointTransport(client, invocation(), tmp_path)
    transport.data.mkdir()
    transport.meta.mkdir()
    transport.generation = 17
    batches = []
    for index in range(2048):
        path = transport.data / f"part-{index:05d}.xtc"
        content = f"verified prior attempt part {index}".encode()
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        transport.files[path.name] = {
            "path": path.name,
            "sha256": digest,
            "size_bytes": len(content),
            "uploaded_attempt": "previous",
            "artifact": {"artifact_id": str(uuid4()), "sha256": digest},
        }
    alias = transport.data / "same-part-alias.xtc"
    first = transport.data / "part-00000.xtc"
    alias.write_bytes(first.read_bytes())
    transport.files[alias.name] = {**transport.files[first.name], "path": alias.name}

    def upload_files(*, paths, on_phase=None, **kwargs):
        batches.append(paths)
        assert 1 <= len(paths) <= 64
        if fault == "upload" and len(batches) == 2:
            raise RuntimeError("bounded transfer failed")
        return [client.upload(content=path.read_bytes(), **kwargs) for path in paths]

    def disallow_serial_upload(**kwargs):
        raise AssertionError("recovered final publication must not serialize individual uploads")

    monkeypatch.setattr(client, "upload_files", upload_files)
    monkeypatch.setattr(client, "upload_file", disallow_serial_upload)
    if fault == "corrupt":
        # Corruption in a later file must be detected before any batch is sent.
        (transport.data / "part-02047.xtc").write_bytes(b"changed")
        with pytest.raises(ValueError, match="differs from its committed checkpoint"):
            transport.final_file_reference(first)
        assert not batches and not transport.final_references_prepared
    elif fault == "upload":
        with pytest.raises(RuntimeError, match="bounded transfer failed"):
            transport.final_file_reference(first)
        assert len(batches) == 2 and not transport.final_references_prepared
    else:
        refs = [transport.final_file_reference(transport.data / name) for name in transport.files]
        assert refs[0] == refs[-1]
        assert len(batches) == 32 and client.uploads == 2048
        assert transport.final_references_prepared
        progress = json.loads((transport.meta / "transfer-progress.json").read_text())
        assert progress["phase"] == "finalized" and progress["completed_files"] == 2049
        assert all(identity == f"output:{transport.attempt}:native-file" for identity, _ in client.addresses.values())


def test_alias_checkpoint_and_stage_commit_use_the_real_artifact_service(tmp_path, monkeypatch):
    from test_scientific_artifacts import NOW, TENANT, FakeObjectStore, open_attempt

    from fs2_serve.scientific_artifacts import (
        ArtifactDirection,
        AttemptStatus,
        BeginArtifactUpload,
        CloseStageAttempt,
        CommitStageResult,
        FinalizeArtifactUpload,
        ManifestEntryDraft,
        MemoryArtifactRepository,
        ScientificArtifactService,
    )

    with asyncio.Runner() as runner:
        repository = MemoryArtifactRepository(clock=lambda: NOW)
        objects = FakeObjectStore()
        service = ScientificArtifactService(
            repository=repository,
            object_store=objects,
            clock=lambda: NOW,
            allowed_media_types={"application/octet-stream", "application/vnd.fs2.gromacs-checkpoint+json"},
        )
        operation_id = UUID(OPERATION)
        runner.run(repository.register_operation(operation_id, tenant_id=TENANT))
        attempt_id = runner.run(
            open_attempt(service, operation_id=operation_id, stage_id="workflow", shard_id="replica")
        )
        monkeypatch.setenv("FS2_ATTEMPT_ID", str(attempt_id))

        class RealArtifacts(Artifacts):
            def upload(self, *, identity, content, media_type, compression):
                digest = hashlib.sha256(content).hexdigest()
                upload_id = uuid5(NAMESPACE_URL, f"fs2-scientific-upload:{identity}:{digest}")
                begun = runner.run(
                    service.begin_upload(
                        BeginArtifactUpload(
                            upload_id=upload_id,
                            attempt_id=attempt_id,
                            operation_id=operation_id,
                            tenant_id=TENANT,
                            direction=ArtifactDirection.OUTPUT,
                            expected_digest="sha256:" + digest,
                            expected_size_bytes=len(content),
                            media_type=media_type,
                            compression=compression,
                        )
                    )
                )
                objects.put(begun.upload.storage_key, content, media_type, compression)
                record = runner.run(
                    service.finalize_upload(
                        FinalizeArtifactUpload(upload_id=upload_id, operation_id=operation_id, tenant_id=TENANT)
                    )
                )
                self.objects[str(record.artifact_id)] = content
                ref = record.to_public_ref().model_dump(mode="json")
                if media_type == "application/vnd.fs2.gromacs-checkpoint+json":
                    self.latest = ref
                return ref

        client = RealArtifacts()
        (tmp_path / "data").mkdir()
        names = ("md.gro", "md.part0001.gro", "copy.backup")
        for name in names:
            (tmp_path / "data" / name).write_bytes(b"same native coordinates")
        transport = GromacsCheckpointTransport(client, invocation(), tmp_path)
        transport.restore()
        ready(tmp_path, 1)
        transport.publish_ready()
        refs = [transport.final_file_reference(tmp_path / "data" / name) for name in names]
        assert refs[0] == refs[1] == refs[2]
        runner.run(
            service.close_attempt(
                CloseStageAttempt(
                    attempt_id=attempt_id,
                    operation_id=operation_id,
                    tenant_id=TENANT,
                    status=AttemptStatus.SUCCEEDED,
                    completed_at=NOW + timedelta(minutes=1),
                )
            )
        )
        commit = runner.run(
            service.commit_stage(
                CommitStageResult(
                    operation_id=operation_id,
                    tenant_id=TENANT,
                    stage_id="workflow",
                    attempt_ids=(attempt_id,),
                    entries=tuple(
                        ManifestEntryDraft(
                            name=f"file-{i:05d}", semantic_type="gromacs-file/v1", artifact_id=UUID(ref["artifact_id"])
                        )
                        for i, ref in enumerate(refs)
                    ),
                    validation_digest="sha256:" + "9" * 64,
                    semantic_valid=True,
                    committed_at=NOW + timedelta(minutes=2),
                    validated_at=NOW + timedelta(minutes=2),
                )
            )
        )
        assert len(commit.manifest.entries) == 3


def test_large_file_put_retries_with_the_whole_file_and_no_read_bytes(tmp_path, monkeypatch):
    payload = b"closed trajectory bytes\x00" * 10000
    source = tmp_path / "trajectory.xtc"
    source.write_bytes(payload)
    puts = []

    def server(request):
        if request.method == "PUT":
            puts.append(request.read())
            return httpx.Response(503 if len(puts) == 1 else 200)
        if request.url.path.endswith(":finalize"):
            return httpx.Response(200, json={"artifact_id": str(uuid4())})
        return httpx.Response(
            201, json={"handle": {"method": "PUT", "url": "https://object.test/trajectory", "headers": {}}}
        )

    monkeypatch.setattr("fs2_serve.scientific_batch.companion.time.sleep", lambda _: None)
    monkeypatch.setattr(Path, "read_bytes", lambda _: (_ for _ in ()).throw(AssertionError("whole file buffered")))
    client = WorkloadArtifactHttpClient(
        base_url="http://api.test", capability="test", client=httpx.Client(transport=httpx.MockTransport(server))
    )
    assert client.upload_file(identity="closed", path=source, media_type="application/octet-stream", compression=None)[
        "artifact_id"
    ]
    assert puts == [payload, payload]

import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import pytest
from fs2_gromacs.files import atomic_json, inventory

from fs2_serve.scientific_batch.companion import WorkloadArtifactHttpClient
from fs2_serve.scientific_batch.gromacs_checkpoints import GromacsCheckpointTransport

OPERATION = "9eb1af68-cee7-46ea-9c3c-270e039ba923"


class Artifacts:
    def __init__(self):
        self.base_url = "http://api.test"
        self.headers = {}
        self.objects = {}
        self.latest = None
        self.uploads = 0
        self.addresses = {}

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
        if media_type == "application/vnd.fs2.gromacs-checkpoint+json":
            self.latest = artifact
        else:
            self.uploads += 1
        return artifact

    def upload_file(self, *, path, **kwargs):
        return self.upload(content=path.read_bytes(), **kwargs)

    def download(self, artifact_id, **kwargs):
        return self.objects[str(artifact_id)]

    def download_file(self, artifact_id, *, destination, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.objects[str(artifact_id)])


def invocation():
    return SimpleNamespace(
        argv=("--operation-id", OPERATION), shard_id="replica", produces="output", max_output_bytes=10000
    )


def ready(root, generation):
    atomic_json(
        root / ".fs2/checkpoint-ready.json",
        {
            "state": {
                "schema": "fs2-serve.nebius.ai/gromacs-checkpoint/v1",
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
    assert (second / "data/native.cpt").read_bytes() == b"checkpoint two"
    assert (second / "data/md.part0001.xtc").read_bytes() == b"trajectory segment"


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

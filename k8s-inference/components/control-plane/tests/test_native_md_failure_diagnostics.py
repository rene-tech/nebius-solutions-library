"""Failed scientific attempts retain evidence without becoming successful ones."""

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fs2_gromacs.files import atomic_json, inventory
from test_scientific_artifacts import (
    TENANT,
    FakeObjectStore,
    execution_identity,
    open_attempt,
    scheduling_snapshot,
    upload,
)
from test_scientific_batch_production import profile_catalog

from fs2_serve.scientific_artifacts import (
    ArtifactAccess,
    ArtifactDirection,
    ArtifactNotFoundError,
    AttemptStatus,
    CloseStageAttempt,
    MemoryArtifactRepository,
    RunResultDraft,
    ScientificArtifactService,
)
from fs2_serve.scientific_batch import companion, native_failures
from fs2_serve.scientific_batch.adapters import ScientificAdapterError
from fs2_serve.scientific_batch.adapters.staged_workspace import wrap_stage_argv
from fs2_serve.scientific_batch.artifact_bridge import ArtifactServiceBridge, FailedDiagnosticValidationError
from fs2_serve.scientific_batch.execution import _invocation_json
from fs2_serve.scientific_batch.gromacs_checkpoints import GromacsCheckpointTransport
from fs2_serve.scientific_batch.models import AttemptOutcome, BatchStatus, StageInvocation
from fs2_serve.scientific_batch.native_workflows import WORKFLOWS, workflow_for_collector
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileError

OP = "9eb1af68-cee7-46ea-9c3c-270e039ba923"
CATALOG = Path(__file__).resolve().parents[3] / "catalog/runtime"
NATIVE_ERROR = b"ERROR: Unknown command: fs2_deliberately_invalid_native_command\n"


def fixture(root, collector="lammps-workflow-v1", *, marker=True):
    workflow = workflow_for_collector(collector)
    runtime = import_module(workflow.runtime_package)
    contracts = import_module(workflow.runtime_package + ".contracts")
    step = {"id": "production", "input": "in.native"}
    if workflow.engine == "gromacs":
        step = {"id": "production", "command": "mdrun", "args": ["-s", "system.tpr"]}
    elif workflow.engine == "namd":
        step = {
            "id": "production",
            "config": "production.namd",
            "mode": "dynamics",
            "steps": 10,
            "output_prefix": "production",
        }
    elif workflow.engine == "amber":
        step = {
            "id": "production",
            "input": "production.in",
            "topology": "system.prmtop",
            "coordinates": "system.rst7",
            "expected_nsteps": 10,
        }
    shard = "gang" if workflow.model_id == "gromacs-mpi" else "main"
    request = workflow.normalize({"schema": workflow.parameter_schema, "jobs": [{"id": shard, "steps": [step]}]})
    engine = (
        (runtime.MPI_ENGINE if workflow.model_id == "gromacs-mpi" else runtime.NVIDIA_IMAGE)
        if workflow.engine == "gromacs"
        else runtime.ENGINE_ID
    )
    command = (
        sys.executable,
        "-c",
        "from pathlib import Path; Path('executed-once').open('x').close(); raise SystemExit(23)",
        "--operation-id",
        OP,
    )
    invocation = StageInvocation(
        stage_id="workflow",
        shard_id=shard,
        argv=wrap_stage_argv("/mnt/fs2-scientific/test/main", command),
        environment=(),
        working_directory="/mnt/fs2-scientific/test/main",
        consumes=(),
        produces="run.test.workflow.main",
        collector_id=collector,
        validator_id=collector,
        max_output_artifacts=10000,
        max_output_bytes=request["max_output_bytes"] + 16 * 1024**2,
    )
    (root / "data").mkdir(parents=True)
    (root / "data/native.log").write_bytes(NATIVE_ERROR)
    (root / "data/partial.nc").write_bytes(b"not a certified trajectory")
    atomic_json(root / ".fs2/request.json", request)
    result = {
        "schema": runtime.RESULT_SCHEMA,
        "operation_id": OP,
        "job_id": shard,
        "status": "failed",
        "engine_id": engine,
        "recipe_sha256": hashlib.sha256(
            contracts.canonical({"request": request, "job": shard, "image": engine})
        ).hexdigest(),
        "completed_steps": [],
        "commands": [{"log": "native.log", "exit_code": 23}],
        "files": inventory(root / "data", max_bytes=request["max_output_bytes"]),
    }
    atomic_json(root / "result.json", result)
    if marker:
        atomic_json(
            root / ".fs2/stage-failed.json",
            {
                "schema": native_failures.FAILURE_SCHEMA,
                "status": "failed",
                "exit_code": 23,
                "stage_id": "workflow",
                "shard_id": shard,
                "logical_output_id": invocation.produces,
                "collector_id": collector,
                "validator_id": collector,
                "argv_sha256": hashlib.sha256(json.dumps(command, separators=(",", ":")).encode()).hexdigest(),
            },
        )
    return invocation, workflow, result


@pytest.mark.parametrize("collector", [item.collector_id for item in WORKFLOWS if item.engine != "scvi"])
def test_all_native_engines_preserve_exact_failure_logs_not_partial_science(tmp_path, collector):
    invocation, workflow, _ = fixture(tmp_path, collector)
    output = native_failures.collect_failed_diagnostics(invocation, tmp_path, workflow)
    assert output.validation["status"] == "failed"
    assert output.validation["scientific_validation_passed"] is False
    assert output.validation["checkpoint_generation_created"] is False
    assert [item.semantic_type for item in output.artifacts] == [
        f"{workflow.engine}-failed-result/v1",
        f"{workflow.engine}-failed-log/v1",
        "native-failed-diagnostics/v1",
    ]
    assert output.artifacts[1].path.read_bytes() == NATIVE_ERROR
    assert not any(item.path.name == "partial.nc" for item in output.artifacts)
    assert not (tmp_path / ".fs2/checkpoint-ack.json").exists()
    assert not (tmp_path / ".fs2/stage-complete.json").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", "other"),
        ("operation_id", str(uuid4())),
        ("recipe_sha256", "0" * 64),
        ("engine_id", "other"),
        ("status", "succeeded"),
        ("completed_steps", ["not-requested"]),
    ],
)
def test_failure_result_cannot_adopt_another_identity_or_success(tmp_path, field, value):
    invocation, workflow, result = fixture(tmp_path)
    atomic_json(tmp_path / "result.json", {**result, field: value})
    with pytest.raises(ScientificAdapterError):
        native_failures.collect_failed_diagnostics(invocation, tmp_path, workflow)


@pytest.mark.parametrize("change", ["changed", "symlink", "stale-marker"])
def test_failed_diagnostic_files_and_marker_are_bound(tmp_path, change):
    invocation, workflow, _ = fixture(tmp_path)
    if change == "changed":
        (tmp_path / "data/native.log").write_bytes(b"changed log")
    elif change == "symlink":
        (tmp_path / "data/native.log").unlink()
        (tmp_path / "data/native.log").symlink_to(tmp_path / "result.json")
    else:
        marker = tmp_path / ".fs2/stage-failed.json"
        atomic_json(marker, {**json.loads(marker.read_text()), "shard_id": "other"})
    with pytest.raises(ScientificAdapterError):
        native_failures.collect_failed_diagnostics(invocation, tmp_path, workflow)


def test_omitted_large_log_is_explicit_and_no_success_marker_means_pending(tmp_path, monkeypatch):
    invocation, workflow, _ = fixture(tmp_path)
    monkeypatch.setattr(native_failures, "MAX_DIAGNOSTIC_BYTES", 1)
    output = native_failures.collect_failed_diagnostics(invocation, tmp_path, workflow)
    assert output.validation["preserved_logs"] == []
    assert output.validation["omitted_logs"][0]["path"] == "native.log"
    (tmp_path / ".fs2/stage-failed.json").unlink()
    assert native_failures.collect_failed_diagnostics(invocation, tmp_path, workflow) is None


def test_broken_native_collector_notifies_the_waiting_worker_without_ack(tmp_path, monkeypatch):
    invocation, _, _ = fixture(tmp_path, marker=False)

    class Transport:
        def __init__(self, *args, **kwargs):
            pass

        def restore(self):
            pass

        def publish_ready(self):
            pass

        def _notify_failure(self, phase):
            atomic_json(tmp_path / ".fs2/transport-error.json", {"status": "failed", "phase": phase})

    def broken(*args):
        raise KeyError("collector unavailable")

    monkeypatch.setattr("fs2_serve.scientific_batch.gromacs_checkpoints.NativeCheckpointTransport", Transport)
    monkeypatch.setattr(companion, "collect_stage_output", broken)
    with pytest.raises(KeyError):
        companion.collect_and_commit(
            client=None,
            collector_id=invocation.collector_id,
            validator_id=invocation.validator_id,
            invocation_json=_invocation_json(invocation),
            workspace=tmp_path,
            catalog_dir=CATALOG,
            collection_deadline_seconds=5,
            poll_seconds=0.01,
            max_artifacts=invocation.max_output_artifacts,
            max_output_bytes=invocation.max_output_bytes,
        )
    assert json.loads((tmp_path / ".fs2/transport-error.json").read_text()) == {"status": "failed", "phase": "collect"}
    assert not (tmp_path / ".fs2/checkpoint-ack.json").exists()


class Client:
    def __init__(self):
        self.uploads = []

    def upload_file(self, *, identity, path, media_type, compression):
        return self.upload(identity=identity, content=path.read_bytes(), media_type=media_type, compression=compression)

    def upload(self, *, identity, content, media_type, compression):
        ref = {
            "artifact_id": str(uuid4()),
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "media_type": media_type,
            "compression": "none",
        }
        self.uploads.append((ref, content, identity))
        return ref


def collect(invocation, root, monkeypatch):
    class Transport:
        attempt = "test-attempt"

        def __init__(self, *args, **kwargs):
            self.client, self.invocation, self.workspace = args[:3]
            self.data = self.workspace / "data"
            self.files, self.diagnostic_files = {}, {}

        diagnostic_file_reference = GromacsCheckpointTransport.diagnostic_file_reference

        def restore(self):
            pass

        def publish_ready(self):
            pytest.fail("failed diagnostic publication must not commit a checkpoint")

        def final_file_reference(self, path):
            pytest.fail("failed bytes must not masquerade as committed checkpoint files")

    monkeypatch.setattr("fs2_serve.scientific_batch.gromacs_checkpoints.NativeCheckpointTransport", Transport)
    client = Client()
    companion.collect_and_commit(
        client=client,
        collector_id=invocation.collector_id,
        validator_id=invocation.validator_id,
        invocation_json=_invocation_json(invocation),
        workspace=root,
        catalog_dir=CATALOG,
        collection_deadline_seconds=5,
        poll_seconds=0.01,
        max_artifacts=invocation.max_output_artifacts,
        max_output_bytes=invocation.max_output_bytes,
    )
    return client


def test_real_runner_waits_for_durable_diagnostics_and_keeps_original_exit(tmp_path, monkeypatch):
    invocation, _, _ = fixture(tmp_path, marker=False)
    runner = tmp_path / ".fs2/stage-runner.py"
    runner.write_bytes(companion._STAGE_RUNNER_SOURCE)
    env = {
        **os.environ,
        "FS2_STAGE_ID": invocation.stage_id,
        "FS2_SHARD_ID": invocation.shard_id,
        "FS2_LOGICAL_OUTPUT_ID": invocation.produces,
        "FS2_COLLECTOR_ID": invocation.collector_id,
        "FS2_VALIDATOR_ID": invocation.validator_id,
    }
    process = subprocess.Popen(  # noqa: S603 - test-owned fixed child and generated runner
        [sys.executable, str(runner), "--", *invocation.argv[3:]], cwd=tmp_path, env=env
    )
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / ".fs2/stage-failed.json").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert process.poll() is None
        atomic_json(
            tmp_path / ".fs2/failed-diagnostics-ack.json",
            {"status": "diagnostics-exported", "failure_marker_sha256": "0" * 64},
        )
        time.sleep(0.15)
        assert process.poll() is None
        client = collect(invocation, tmp_path, monkeypatch)
        assert process.wait(timeout=5) == 23
        assert (tmp_path / "executed-once").exists()
        assert not (tmp_path / ".fs2/stage-complete.json").exists()
        assert any(content == NATIVE_ERROR for _, content, _ in client.uploads)
        validation = next(
            json.loads(content)
            for ref, content, _ in client.uploads
            if ref["media_type"] == "application/vnd.fs2.scientific-validation+json"
        )
        manifest = next(
            content
            for ref, content, _ in client.uploads
            if ref["media_type"] == "application/vnd.fs2.scientific-manifest+json"
        )
        assert validation["status"] == "failed"
        assert validation["diagnostic_manifest_sha256"] == hashlib.sha256(manifest).hexdigest()
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def test_failed_upload_never_acknowledges_diagnostic_publication(tmp_path, monkeypatch):
    invocation, _, _ = fixture(tmp_path)

    def unavailable(*args, **kwargs):
        raise RuntimeError("test artifact storage unavailable")

    monkeypatch.setattr(Client, "upload", unavailable)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        collect(invocation, tmp_path, monkeypatch)
    assert not (tmp_path / ".fs2/failed-diagnostics-ack.json").exists()
    assert not (tmp_path / ".fs2/checkpoint-ack.json").exists()


@pytest.mark.parametrize("collector", [item.collector_id for item in WORKFLOWS if item.engine != "scvi"])
def test_failed_log_reuses_checkpoint_upload_identity_without_certifying_progress(tmp_path, monkeypatch, collector):
    invocation, _, _ = fixture(tmp_path, collector)
    native_identity = f"{invocation.produces}:test-attempt:native-file"
    native_digest = hashlib.sha256(NATIVE_ERROR).hexdigest()
    original_upload = Client.upload

    def content_addressed_upload(self, *, identity, content, media_type, compression):
        # The checkpoint transport may have uploaded this closed native log
        # before the engine exited. Production reserves one upload identity per
        # attempt/digest; a diagnostic-specific identity used to raise HTTP 409.
        if hashlib.sha256(content).hexdigest() == native_digest:
            assert identity == native_identity, "conflicting upload reservation for checkpointed failure log"
            assert media_type == "application/octet-stream"
            assert compression in (None, "none")
        return original_upload(self, identity=identity, content=content, media_type=media_type, compression=compression)

    monkeypatch.setattr(Client, "upload", content_addressed_upload)
    client = collect(invocation, tmp_path, monkeypatch)
    assert any(content == NATIVE_ERROR and identity == native_identity for _, content, identity in client.uploads)
    assert (tmp_path / ".fs2/failed-diagnostics-ack.json").exists()
    assert not (tmp_path / ".fs2/checkpoint-ack.json").exists()
    assert not (tmp_path / ".fs2/stage-complete.json").exists()


async def failed_artifact_fixture(tmp_path, monkeypatch):
    invocation, _, _ = fixture(tmp_path)
    client = collect(invocation, tmp_path, monkeypatch)
    repository = MemoryArtifactRepository(clock=lambda: datetime.now(UTC))
    objects = FakeObjectStore(clock=lambda: datetime.now(UTC))
    service = ScientificArtifactService(
        repository=repository,
        object_store=objects,
        allowed_media_types={
            "application/json",
            "application/octet-stream",
            "text/plain",
            "application/vnd.fs2.scientific-manifest+json",
            "application/vnd.fs2.scientific-validation+json",
        },
    )
    operation = UUID(OP)
    await repository.register_operation(operation, tenant_id=TENANT)
    attempt = await open_attempt(service, operation_id=operation, stage_id="workflow", shard_id="main")
    pointers, stored, raw_manifest = {}, {}, None
    for ref, content, _ in client.uploads:
        if ref["media_type"] == "application/vnd.fs2.scientific-manifest+json":
            value = json.loads(content)
            for entry in value["entries"]:
                entry["artifact"] = pointers[entry["artifact"]["artifact_id"]]
            content = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            raw_manifest = content
        elif ref["media_type"] == "application/vnd.fs2.scientific-validation+json":
            value = json.loads(content)
            value["diagnostic_manifest_sha256"] = hashlib.sha256(raw_manifest).hexdigest()
            content = json.dumps(value).encode()
        record = await upload(
            service, objects, operation_id=operation, attempt_id=attempt, value=content, media_type=ref["media_type"]
        )
        pointers[ref["artifact_id"]] = record.to_public_ref().model_dump(mode="json")
        stored[record.artifact_id] = content
    input_record = await upload(
        service,
        objects,
        operation_id=operation,
        attempt_id=attempt,
        value=b'{"input":true}',
        media_type="application/json",
        direction=ArtifactDirection.INPUT,
    )
    await service.close_attempt(
        CloseStageAttempt(
            operation_id=operation,
            attempt_id=attempt,
            tenant_id=TENANT,
            status=AttemptStatus.FAILED,
            completed_at=datetime.now(UTC),
        )
    )

    class Reader:
        async def read(self, identity, *, tenant_id, maximum_bytes):
            await repository.get_artifact(identity, tenant_id=tenant_id)
            assert len(stored[identity]) <= maximum_bytes
            return stored[identity]

    bridge = ArtifactServiceBridge(
        artifacts=repository,
        batches=SimpleNamespace(),
        profiles=profile_catalog(),
        store=SimpleNamespace(),
        service=service,
        content_reader=Reader(),
    )
    attempt_state = SimpleNamespace(
        attempt_id=attempt, stage_id="workflow", shard_id="main", outcome=AttemptOutcome.FAILED
    )
    state = SimpleNamespace(
        operation_id=operation,
        tenant_id=TENANT,
        model_id="lammps",
        execution_plan=SimpleNamespace(invocation=lambda *_: invocation),
        stages=[SimpleNamespace(attempts=[attempt_state])],
    )
    return bridge, state, service, repository, stored, input_record, invocation


@pytest.mark.asyncio
async def test_failed_artifacts_remain_owner_scoped_and_not_committed_science(tmp_path, monkeypatch):
    bridge, state, service, repository, stored, input_record, invocation = await failed_artifact_fixture(
        tmp_path, monkeypatch
    )
    operation = state.operation_id
    identity = await bridge._failed_diagnostic_manifest(state)
    assert identity is not None
    with pytest.raises(ArtifactNotFoundError):
        await repository.get_artifact(identity, tenant_id="another-tenant")
    assert await service.stage_commit(operation, stage_id="workflow", tenant_id=TENANT) is None
    result = await service.commit_run_result(
        RunResultDraft(
            operation_id=operation,
            tenant_id=TENANT,
            terminal_status="failed",
            submitted_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            execution_identity=execution_identity(),
            access=ArtifactAccess(),
            scheduling_snapshot=scheduling_snapshot(("workflow",)),
            input_manifest_artifact_id=input_record.artifact_id,
            output_manifest_artifact_id=identity,
            validator_id=invocation.validator_id,
            validation_status="failed",
            error_code="NATIVE_COMMAND_FAILED",
            error_message="Native command failed; see diagnostic artifacts",
            error_retryable=False,
        )
    )
    assert result.result.terminal_status == "failed"
    assert result.result.semantic_validation.status == "failed"
    assert result.result.output_manifest.artifact_id == str(identity)
    assert result.result.attempts[0].checkpoint_output is None
    validation_id = next(
        identity
        for identity, raw in stored.items()
        if raw.startswith(b"{") and json.loads(raw).get("diagnostic_manifest_sha256") is not None
    )
    original_validation = stored[validation_id]
    # A successful head result in a failed gang is not a failed-diagnostic set.
    stored[validation_id] = b'{"status":"passed"}'
    assert await bridge._failed_diagnostic_manifest(state) is None
    stored[validation_id] = original_validation
    # Even a rehashed manifest cannot refer to another attempt's artifact.
    bad = json.loads(stored[identity])
    bad["entries"][0]["artifact"]["artifact_id"] = str(uuid4())
    stored[identity] = json.dumps(bad).encode()
    with pytest.raises(ScientificProfileError, match="another or changed"):
        await bridge._failed_diagnostic_manifest(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "null", "owner", "malformed", "manifest", "inventory"])
async def test_invalid_failed_receipt_publishes_actionable_failure_not_science(tmp_path, monkeypatch, invalid):
    bridge, state, service, _, stored, input_record, _ = await failed_artifact_fixture(tmp_path, monkeypatch)
    validation_id = next(
        key
        for key, raw in stored.items()
        if raw.startswith(b"{") and json.loads(raw).get("diagnostic_manifest_sha256") is not None
    )
    validation = json.loads(stored[validation_id])
    if invalid in {"missing", "null"}:
        # Exact scVI legacy shape: these fields were absent, not false/main.
        for key in ("job_id", "checkpoint_generation_created"):
            if invalid == "missing":
                validation.pop(key)
            else:
                validation[key] = None
    elif invalid == "owner":
        validation["operation_id"] = str(uuid4())
    stored[validation_id] = b"{" if invalid == "malformed" else json.dumps(validation).encode()
    if invalid in {"manifest", "inventory"}:
        manifest_id = next(
            key
            for key, raw in stored.items()
            if raw.startswith(b"{") and json.loads(raw).get("manifest_id") is not None
        )
        document = json.loads(stored[manifest_id])
        if invalid == "manifest":
            document["manifest_id"] = "another.run"
        else:
            document["entries"] = [e for e in document["entries"] if e["name"] == "failed-result"]
        stored[manifest_id] = json.dumps(document).encode()
    with pytest.raises(FailedDiagnosticValidationError):
        await bridge._failed_diagnostic_manifest(state)

    state.status = BatchStatus.FAILED
    state.result_published = False
    state.input_manifest = SimpleNamespace(manifest_artifact_id=input_record.artifact_id)
    state.variant_id = "canonical-runtime"
    state.failure_code = "NATIVE_COMMAND_FAILED"
    state.access_context = SimpleNamespace(profile="public", receipt_digest=None)
    identity = execution_identity()
    identity["artifact_manifest_digest"] = identity["model_artifact_manifest_digest"]
    monkeypatch.setattr(
        bridge.profiles,
        "get",
        lambda *_, **__: SimpleNamespace(
            value={
                "execution_identity": identity,
                "semantic_validation": {"validator_id": "lammps-workflow-v1"},
            }
        ),
    )
    bridge.store.get_operation = AsyncMock(return_value=SimpleNamespace(accepted_at=datetime.now(UTC)))
    monkeypatch.setattr(
        bridge, "_terminal_event", AsyncMock(return_value=SimpleNamespace(occurred_at=datetime.now(UTC)))
    )
    monkeypatch.setattr(bridge, "_scheduling_snapshot", lambda _: scheduling_snapshot(("workflow",)))
    publish = AsyncMock()
    monkeypatch.setattr(service, "commit_run_result", publish)
    await bridge.publish_terminal(state)
    draft = publish.call_args.args[0]
    assert draft.terminal_status == "failed"
    assert draft.validation_status == "failed"
    assert draft.validation_receipt_digest is None
    assert draft.output_manifest_artifact_id is None
    assert draft.error_code == "SCIENTIFIC_DIAGNOSTICS_INVALID"
    assert "NATIVE_COMMAND_FAILED" in draft.error_message
    assert "contact platform support" in draft.error_message
    assert draft.error_retryable is False


@pytest.mark.asyncio
async def test_diagnostic_shared_storage_outage_is_not_reclassified_as_bad_receipt(tmp_path, monkeypatch):
    bridge, state, _, _, _, _, _ = await failed_artifact_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge.content_reader, "read", AsyncMock(side_effect=ConnectionError("storage unavailable")))
    with pytest.raises(ConnectionError, match="storage unavailable"):
        await bridge._failed_diagnostic_manifest(state)

import copy
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from fs2_gromacs.contracts import normalize

from fs2_serve.scientific_artifacts import ArtifactConflictError, ArtifactDirection
from fs2_serve.scientific_batch.gromacs_resume import (
    CHECKPOINT_MEDIA,
    GromacsResumeRequest,
    checkpoint_choices,
    continuation_inputs,
    continuation_parameters,
    resume_gromacs,
)
from fs2_serve.scientific_batch.profile_catalog import ScientificRequestError
from fs2_serve.scientific_batch.worker_errors import worker_error_code, worker_error_detail
from fs2_serve.scientific_run_result import ArtifactRef


def original():
    return normalize(
        {
            "schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1",
            "max_wall_seconds": 21600,
            "jobs": [
                {
                    "id": "replica",
                    "steps": [
                        {"id": "prepare", "command": "grompp", "args": ["-o", "run.tpr"]},
                        {"id": "production", "command": "mdrun", "args": ["-s", "run.tpr", "-deffnm", "md"]},
                        {"id": "join", "command": "trjcat", "args": ["-f", {"files": "md.part*.xtc"}, "-o", "md.xtc"]},
                    ],
                }
            ],
        }
    )


class Artifacts:
    def __init__(self):
        self.objects, self.records, self.reads = {}, [], []
        self.list_calls = []

    def add(self, name, content):
        ref = ArtifactRef(
            artifact_id=str(uuid4()),
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            media_type="application/octet-stream",
            compression="none",
        )
        record = SimpleNamespace(
            artifact_id=ref.artifact_id,
            to_public_ref=lambda: ref,
            direction=ArtifactDirection.OUTPUT,
            media_type=ref.media_type,
            shard_id="replica",
            created_at=datetime.now(UTC),
        )
        self.records.append(record)
        self.objects[ref.artifact_id] = content
        return {
            "path": name,
            "artifact": ref.model_dump(mode="json"),
            "sha256": ref.sha256,
            "size_bytes": ref.size_bytes,
        }

    async def open_content(self, artifact_id, *, tenant_id):
        self.reads.append((str(artifact_id), tenant_id))

        async def chunks():
            yield self.objects[str(artifact_id)]

        return SimpleNamespace(chunks=chunks())

    async def list_artifacts(self, operation_id, *, tenant_id, stage_id):
        assert tenant_id == "system"
        self.list_calls.append((operation_id, tenant_id, stage_id))
        return self.records


def fixture():
    artifacts = Artifacts()
    files = [
        artifacts.add("run.tpr", b"original immutable TPR"),
        artifacts.add("fs2-production.cpt", b"native state with velocities"),
        artifacts.add("md.part0001.xtc", b"old trajectory"),
        artifacts.add("fs2-production-segment-000001.log", b"old wrapper log"),
    ]
    checkpoint = {
        "state": {
            "job_id": "replica",
            "generation": 71,
            "completed_steps": ["prepare"],
            "active_step": {"id": "production", "tpr_sha256": files[0]["sha256"], "target_step": 500000000},
        },
        "files": files,
    }
    return artifacts, checkpoint


@pytest.mark.parametrize("budget", [604800, 1209600])
def test_resume_reuses_original_physics_and_skips_completed_preparation(budget):
    _, checkpoint = fixture()
    parameters = original()
    before = copy.deepcopy(parameters)
    result = continuation_parameters(parameters, checkpoint, model_id="gromacs", max_wall_seconds=budget)
    assert parameters == before
    assert result["max_wall_seconds"] == budget
    steps = result["jobs"][0]["steps"]
    assert [step["id"] for step in steps] == ["production", "join"]
    assert steps[0]["args"] == before["jobs"][0]["steps"][1]["args"]
    assert steps[0]["restart_checkpoint"] == "fs2-production.cpt"
    assert steps[1] == before["jobs"][0]["steps"][2]


@pytest.mark.parametrize("damage", ["checkpoint", "tpr", "completed", "job", "active"])
def test_resume_rejects_missing_or_incompatible_scientific_state(damage):
    _, checkpoint = fixture()
    if damage == "checkpoint":
        checkpoint["files"] = [row for row in checkpoint["files"] if not row["path"].endswith(".cpt")]
    elif damage == "tpr":
        checkpoint["state"]["active_step"]["tpr_sha256"] = "0" * 64
    elif damage == "completed":
        checkpoint["state"]["completed_steps"] = ["production"]
    elif damage == "job":
        checkpoint["state"]["job_id"] = "other"
    else:
        checkpoint["state"]["active_step"]["id"] = "other"
    with pytest.raises(ScientificRequestError):
        continuation_parameters(original(), checkpoint, model_id="gromacs", max_wall_seconds=604800)


@pytest.mark.parametrize("budget", [0, 59, True, 1209601])
def test_invalid_resume_budget_is_rejected(budget):
    _, checkpoint = fixture()
    with pytest.raises(ScientificRequestError):
        continuation_parameters(original(), checkpoint, model_id="gromacs", max_wall_seconds=budget)


def test_continuation_reuses_artifact_references_without_copying_large_trajectories():
    artifacts, checkpoint = fixture()
    before = copy.deepcopy(checkpoint)
    entries, paths = continuation_inputs(checkpoint, artifacts.records)
    assert {row["path"] for row in paths} == {"run.tpr", "fs2-production.cpt", "md.part0001.xtc"}
    assert {row["artifact"]["artifact_id"] for row in entries} == {
        row["artifact"]["artifact_id"] for row in checkpoint["files"][:3]
    }
    assert not artifacts.reads
    assert checkpoint == before


def test_continuation_refuses_unrelated_artifacts_and_changed_identity():
    artifacts, checkpoint = fixture()
    with pytest.raises(ScientificRequestError, match="outside"):
        continuation_inputs(checkpoint, [])
    checkpoint["files"][0]["sha256"] = "0" * 64
    with pytest.raises(ScientificRequestError, match="identity"):
        continuation_inputs(checkpoint, artifacts.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("gang", [False, True])
async def test_resume_full_path_preserves_inputs_new_budget_and_idempotency(gang):
    artifacts, checkpoint = fixture()
    operation_id = uuid4()
    checkpoint["state"].update(schema="fs2-serve.nebius.ai/gromacs-checkpoint/v1", operation_id=str(operation_id))
    if gang:
        checkpoint["state"]["job_id"] = "gang"
    manifest = artifacts.add("checkpoint.json", json.dumps(checkpoint).encode())
    artifacts.records[-1].media_type = CHECKPOINT_MEDIA
    if gang:
        for record in artifacts.records:
            record.shard_id = None
    parameters = original()
    if gang:
        parameters.update(schema="fs2-serve.nebius.ai/gromacs-mpi-workflow-request/v1", nodes=2)
        parameters["jobs"][0]["id"] = "gang"

    async def get(*args, **kwargs):
        def invocation(stage, shard):
            assert shard == (None if gang else "replica")
            return SimpleNamespace(
                workspace_documents=[
                    SimpleNamespace(relative_path=".fs2/request.json", canonical_json=json.dumps(parameters))
                ]
            )

        return SimpleNamespace(
            execution_plan=SimpleNamespace(invocation=invocation),
            scheduling=SimpleNamespace(service_class="customer-batch"),
        )

    async def status(*args, **kwargs):
        return {"batch": {"status": "failed", "model_id": "gromacs-mpi" if gang else "gromacs"}}

    submitted = {}

    async def submit(**kwargs):
        key = kwargs["idempotency_key"]
        if key in submitted:
            assert submitted[key] == kwargs["request"]
        submitted[key] = kwargs["request"]
        return {"operation": {"id": str(uuid5(NAMESPACE_URL, key))}}

    uploads = FakeUploads()
    batches = SimpleNamespace(
        status=status,
        repository=SimpleNamespace(get=get),
        submit=submit,
        profiles=SimpleNamespace(get=lambda model: SimpleNamespace(runtime_image_digest="test")),
    )
    principal = SimpleNamespace(tenant_id="system", require=lambda *args: None)
    kwargs = dict(
        batches=batches,
        artifacts=artifacts,
        uploads=uploads,
        principal=principal,
        operation_id=operation_id,
        request=GromacsResumeRequest(),
        idempotency_key="test-resume-1",
    )
    first, second = await resume_gromacs(**kwargs), await resume_gromacs(**kwargs)
    assert first == second
    # Each request authorizes and reads its own inventory once. An idempotent
    # replay must not reuse another request's authorization or inventory.
    assert artifacts.list_calls == [(operation_id, "system", "workflow")] * 2
    assert len(submitted) == 1
    request = next(iter(submitted.values()))
    assert request["parameters"]["max_wall_seconds"] == 1209600
    assert request["parameters"]["jobs"][0]["steps"][0]["restart_checkpoint"] == "fs2-production.cpt"
    assert not request.get("parent_operation_id")
    assert request["client_context"]["correlation_id"] == str(operation_id)
    assert len(request["parameters"]["continuation_files"]) == 4
    assert request["parameters"]["jobs"][0]["steps"][1]["args"][1]["nonempty"] is True
    assert first["continuation"]["adjustments"]["analysis_selectors"]
    assert all(read[0] == manifest["artifact"]["artifact_id"] for read in artifacts.reads)
    assert all(len(content) < 4096 for content in uploads.contents.values())


class FakeUploads:
    def __init__(self):
        self.contents, self.requests = {}, {}
        self.finalized = set()

    async def begin(self, *, request, idempotency_key, principal):
        identity = uuid5(NAMESPACE_URL, idempotency_key)
        self.requests[identity] = request
        return SimpleNamespace(operation_id=identity, upload_id=identity)

    async def store_content(self, *, principal, operation_id, upload_id, content):
        if upload_id in self.finalized:
            raise ArtifactConflictError("a finalized upload cannot accept new bytes")
        if upload_id in self.contents:
            assert self.contents[upload_id] == content
        self.contents[upload_id] = content

    async def finalize(self, *, principal, operation_id, upload_id):
        request = self.requests[upload_id]
        self.finalized.add(upload_id)
        return ArtifactRef(
            artifact_id=str(upload_id),
            sha256=request.sha256,
            size_bytes=request.size_bytes,
            media_type=request.media_type,
        )


@pytest.mark.asyncio
async def test_resume_metadata_replay_uses_real_write_once_upload_service(registry, cipher, hasher):
    from test_scientific_artifact_public_bytes import _artifact_plane, _token
    from test_scientific_batch_production import scientific_runtime

    from fs2_serve.scientific_batch.gromacs_resume import _upload_json
    from fs2_serve.store import ConflictError

    runtime, _, _, _, _ = scientific_runtime(registry, cipher, hasher)
    objects, _ = _artifact_plane(runtime)
    token = await _token(runtime, principal_id="qa", tenant_id="system")
    principal = await runtime.tokens.verify(token)
    arguments = dict(
        principal=principal,
        content=b'{"source":"checkpoint"}',
        model_id="protein-design",
        key="resume-real-upload-replay",
        media_type="application/json",
    )
    first = await _upload_json(runtime.scientific_input_uploads, **arguments)
    second = await _upload_json(runtime.scientific_input_uploads, **arguments)
    assert first == second
    assert len(objects.written) == 1
    with pytest.raises(ConflictError):
        await _upload_json(runtime.scientific_input_uploads, **{**arguments, "content": b"{}"})


@pytest.mark.asyncio
async def test_checkpoint_lookup_checks_operation_ownership_before_storage():
    async def status(*args, **kwargs):
        raise PermissionError("not this caller's operation")

    with pytest.raises(PermissionError):
        await checkpoint_choices(
            batches=SimpleNamespace(status=status),
            artifacts=object(),
            principal=SimpleNamespace(require=lambda *args: None),
            operation_id=uuid4(),
        )


def test_gromacs_timeout_report_is_model_bound_and_has_actionable_detail():
    message = json.dumps(
        {"schema": "fs2-serve.nebius.ai/gromacs-worker-error/v1", "code": "WORKFLOW_TIME_LIMIT_EXCEEDED"}
    )
    for model in ("gromacs", "gromacs-mpi"):
        assert worker_error_code(message, model_id=model) == "WORKFLOW_TIME_LIMIT_EXCEEDED"
        assert "resume" in worker_error_detail(model, "WORKFLOW_TIME_LIMIT_EXCEEDED")
    assert worker_error_code(message) is None
    assert worker_error_code(message, model_id="namd") is None
    assert worker_error_detail("namd", "WORKFLOW_TIME_LIMIT_EXCEEDED") is None


def test_resume_rest_requires_auth_idempotency_and_bounded_budget(registry, cipher, hasher, monkeypatch):
    from test_api_mcp import TestClient, build_runtime, issue

    from fs2_serve.api import create_app
    from fs2_serve.models import Scope
    from fs2_serve.scientific_batch.gromacs_resume import ContinuationError

    runtime = build_runtime(registry, cipher, hasher)
    runtime.scientific_batches = SimpleNamespace()
    runtime.artifact_service = SimpleNamespace()
    runtime.scientific_input_uploads = SimpleNamespace()
    calls = []

    async def resume(**kwargs):
        calls.append(kwargs)
        if kwargs["request"].job_id == "absent":
            raise ContinuationError("select exactly one job_id with a committed checkpoint")
        return {"operation": {"id": str(uuid4()), "reused": False}, "batch": {"model_id": "gromacs"}}

    monkeypatch.setattr("fs2_serve.scientific_batch.gromacs_resume.resume_gromacs", resume)
    with TestClient(create_app(runtime)) as client:
        token = issue(
            client,
            principal="qa",
            tenant="system",
            scopes=[Scope.INFERENCE_INVOKE, Scope.OPERATIONS_RESULT],
            models=["*"],
        )
        path = f"/v1/operations/{uuid4()}:resume"
        headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "test-resume-contract"}
        assert client.post(path, json={}).status_code == 401
        assert client.post(path, json={}, headers={"Authorization": headers["Authorization"]}).status_code == 400
        assert client.post(path, json={"max_wall_seconds": 1209601}, headers=headers).status_code == 422
        response = client.post(path, json={}, headers=headers)
        assert response.status_code == 202
        assert calls[-1]["request"].max_wall_seconds == 1209600
        assert calls[-1]["principal"].tenant_id == "system"
        assert response.headers["location"].endswith(response.json()["operation"]["id"])
        error = client.post(path, json={"job_id": "absent"}, headers=headers)
        assert error.status_code == 422
        assert "committed checkpoint" in error.text

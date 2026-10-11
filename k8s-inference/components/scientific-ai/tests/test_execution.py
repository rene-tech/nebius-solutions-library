import hashlib
import json

import httpx
import pytest
from conftest import initialized_application, successful_result
from fs2_gromacs.files import atomic_json
from jsonschema import ValidationError as NativeValidationError
from pydantic import ValidationError

from scientific_ai.contracts import Attempt, FileRecord, GromacsRun, Owner, RuntimeProfile
from scientific_ai.gromacs import DeliveryPending, GromacsApplication
from scientific_ai.serverless import (
    REQUIRED_CAPABILITIES,
    ServerlessClient,
    UnsupportedExecution,
    invoke_payload,
)
from scientific_ai.storage import Artifacts, S3Objects


@pytest.mark.parametrize(
    "change",
    [
        {"owner": Owner(tenant_id="other", principal_id="qa")},
        {"owner": Owner(tenant_id="system", principal_id="another-user")},
        {"root_operation_id": "another-run"},
        {"cluster": "unapproved"},
        {"gpu_class": "h100"},
        {"image": "example.invalid/gromacs@sha256:" + "c" * 64},
    ],
)
def test_wrong_owner_job_placement_or_image_is_rejected_before_io(setup, change):
    args, objects, lifecycle = setup
    args["attempt"] = Attempt.model_validate({**args["attempt"].model_dump(), **change})
    lifecycle.current = args["attempt"]
    with pytest.raises(PermissionError):
        GromacsApplication(**args)
    assert objects.puts == [] and not args["workspace"].exists()


def test_pinned_runtime_required_for_each_preferred_gpu(setup):
    args, _, _ = setup
    value = args["profile"].model_dump()
    value["images"] = {"rtx-pro-6000": "gromacs:latest"}
    with pytest.raises(ValidationError):
        RuntimeProfile.model_validate(value)


def test_native_request_contract_is_not_replaced_or_physics_rewritten(setup):
    args, _, _ = setup
    run = args["run"]
    assert run.parameters["jobs"][0]["steps"][0]["args"] == ["-s", "run.tpr"]
    for parameters in (
        {**run.parameters, "gpu_snapshot": True},
        {**run.parameters, "output_destination": "platform-artifacts"},
    ):
        with pytest.raises((ValueError, NativeValidationError)):
            GromacsRun(job_id=run.job_id, input_bundle=run.input_bundle, parameters=parameters)


def test_portable_checkpoint_recovery_uses_new_image_without_old_volume(setup, tmp_path):
    args, _, _ = setup
    app, objects, lifecycle = initialized_application(setup)
    (app.worker.data / "state.cpt").write_bytes(b"closed native checkpoint")
    app.worker.state["completed_steps"] = ["preparation"]
    app.worker.checkpoint()
    assert app.worker.committed_generation == 1
    assert objects.puts[-1].startswith("manifests/")
    attempt = Attempt.model_validate(
        {
            **app.attempt.model_dump(),
            "attempt_id": "attempt-2",
            "epoch": 2,
            "gpu_class": "l40s",
            "cluster": "test-b",
            "image": args["profile"].images["l40s"],
        }
    )
    lifecycle.current = attempt
    restored = GromacsApplication(**{**args, "attempt": attempt, "workspace": tmp_path / "replacement"})
    snapshot = restored.prepare(lifecycle.head)
    assert snapshot.image != restored.attempt.image
    assert restored.worker.recipe == app.worker.recipe
    assert (restored.worker.data / "state.cpt").read_bytes() == b"closed native checkpoint"
    assert restored.worker.engine_id == attempt.image
    assert json.loads((restored.worker.meta / "gromacs-state.json").read_text())["generation"] == 1


def test_old_attempt_cannot_publish_head_after_replacement(setup):
    app, objects, lifecycle = initialized_application(setup)
    lifecycle.current = None
    with pytest.raises(PermissionError):
        app.worker.checkpoint()
    assert lifecycle.head is None and app.worker.committed_generation == 0
    assert not objects.puts


def test_epoch_is_checked_atomically_after_upload_not_just_before(setup):
    app, objects, lifecycle = initialized_application(setup)
    original = objects.put

    def supersede(key, source):
        original(key, source)
        if key.startswith("manifests/"):
            lifecycle.current = None

    objects.put = supersede
    with pytest.raises(PermissionError):
        app.worker.checkpoint()
    assert lifecycle.head is None and app.worker.committed_generation == 0
    assert any(key.startswith("manifests/") for key in objects.data)  # unreferenced, never a committed head


@pytest.mark.parametrize("failure", ["denied", "corrupt"])
def test_partial_or_denied_upload_never_commits_checkpoint(setup, failure):
    app, objects, lifecycle = initialized_application(setup)
    objects.fail_put = failure == "denied"
    objects.corrupt_get = failure == "corrupt"
    with pytest.raises((OSError, ValueError)):
        app.worker.checkpoint()
    assert lifecycle.head is None and app.worker.committed_generation == 0
    assert not any(key.startswith("manifests/") for key in objects.puts)


def test_unchanged_segments_are_not_uploaded_again(setup):
    app, objects, _ = initialized_application(setup)
    app.worker.checkpoint()
    app.worker.checkpoint()
    assert len([key for key in objects.puts if key.startswith("blobs/")]) == 1
    assert len([key for key in objects.puts if key.startswith("manifests/")]) == 2


@pytest.mark.parametrize("change", ["input", "recipe", "generation", "owner"])
def test_restore_rejects_incompatible_identity_before_materializing(setup, tmp_path, change):
    args, _, _ = setup
    app, _, lifecycle = initialized_application(setup)
    app.worker.checkpoint()
    saved = app.artifacts.load(lifecycle.head).model_dump()
    if change == "input":
        saved["input_sha256"] = "c" * 64
    elif change == "recipe":
        saved["state"]["recipe_sha256"] = "c" * 64
    elif change == "generation":
        saved["epoch"] = 5
    else:
        saved["owner"]["tenant_id"] = "other"
    from scientific_ai.contracts import Snapshot

    reference = app.artifacts.commit(Snapshot.model_validate(saved))
    new = GromacsApplication(**{**args, "workspace": tmp_path / "new"})
    with pytest.raises(ValueError, match="identity"):
        new.prepare(reference)
    assert not new.worker.data.exists()


@pytest.mark.parametrize("failure", ["upload", "ack"])
def test_failed_delivery_retains_scratch_and_retries_without_recomputation(setup, failure, monkeypatch):
    args, _, _ = setup
    app, objects, lifecycle = initialized_application(setup)
    app.worker.checkpoint()
    result = successful_result(app)
    atomic_json(app.workspace / "result.json", result)
    # Include one not-yet-uploaded final artifact to exercise export failure.
    (app.worker.data / "final.gro").write_bytes(b"final coordinates")
    result = successful_result(app)
    atomic_json(app.workspace / "result.json", result)
    objects.fail_put, lifecycle.fail_ack = failure == "upload", failure == "ack"
    with pytest.raises(DeliveryPending):
        app.finish_delivery(result)
    assert lifecycle.done is None and (app.worker.data / "final.gro").is_file()
    assert json.loads((app.workspace / "delivery.json").read_text())["export"] != "committed"
    objects.fail_put = lifecycle.fail_ack = False
    # A process restart, not only a retry on the same Python object.
    restarted = GromacsApplication(**args)
    monkeypatch.setattr(restarted.worker, "run", lambda: pytest.fail("export retried computation"))
    receipt = restarted.finish_delivery()
    assert receipt["export"] == "committed" and lifecycle.done == receipt["reference"]
    saved = app.artifacts.load(receipt["reference"])
    assert saved.state["generation"] == 1 and saved.result["status"] == "succeeded"


def test_scientific_failure_cannot_be_published_as_success(setup):
    app, _, lifecycle = initialized_application(setup)
    with pytest.raises(ValueError):
        app.finish_delivery({**successful_result(app), "status": "failed"})
    assert lifecycle.done is None


@pytest.mark.parametrize("path", ["../outside", "/absolute", "a/../b", "a\\b", "x\x00y"])
def test_file_and_s3_paths_are_contained(path):
    with pytest.raises(ValueError):
        FileRecord(path=path, sha256="a" * 64, size_bytes=1)
    with pytest.raises(ValueError):
        S3Objects._key(path)


def test_corrupt_restore_leaves_no_usable_partial_file(setup, tmp_path):
    args, objects, _ = setup
    record = args["run"].input_bundle
    objects.data["blobs/" + record.sha256] = b"incorrect"
    with pytest.raises(ValueError):
        Artifacts(objects).download(record, tmp_path / "file")
    assert not (tmp_path / "file").exists() and not list(tmp_path.glob(".incoming-*"))


def test_symlink_output_is_rejected(setup, tmp_path):
    app, _, lifecycle = initialized_application(setup)
    outside = tmp_path / "private"
    outside.write_bytes(b"secret")
    (app.worker.data / "leak").symlink_to(outside)
    with pytest.raises(ValueError):
        app.worker.checkpoint()
    assert lifecycle.head is None


def test_private_data_and_raw_storage_handles_do_not_enter_public_submission(setup):
    args, _, _ = setup
    payload = invoke_payload(
        args["profile"],
        profile_ref="protected-v1",
        identity_ref="grant-opaque",
        private_input_ref="input-opaque",
    )
    encoded = json.dumps(payload)
    for forbidden in ("input.tar.gz", "s3://", "parameters", "access_key", "secret", "run.tpr", "image"):
        assert forbidden not in encoded
    assert payload["execution"]["placement"]["gpu_preference"] == ["rtx-pro-6000", "l40s"]
    with pytest.raises(ValidationError):
        invoke_payload(
            args["profile"],
            profile_ref="protected",
            identity_ref="https://signed.example?token=abc",
            private_input_ref="ref",
        )


@pytest.mark.parametrize("missing", [None, *sorted(REQUIRED_CAPABILITIES)])
def test_missing_s2_contract_prevents_submission(setup, missing):
    calls = []

    def handle(request):
        calls.append(request.method)
        supported = {key: True for key in REQUIRED_CAPABILITIES if key != missing}
        return httpx.Response(404 if missing is None else 200, json={"execution": supported})

    client = ServerlessClient(
        httpx.Client(base_url="https://s2.invalid", transport=httpx.MockTransport(handle)),
        capability_path="/v1/capabilities",
    )
    args, _, _ = setup
    payload = invoke_payload(
        args["profile"], profile_ref="profile", identity_ref="identity", private_input_ref="ref"
    )
    with pytest.raises(UnsupportedExecution):
        client.submit("gromacs", payload, idempotency_key="request-1")
    assert calls == ["GET"]


def test_submission_has_stable_idempotency_and_does_not_retry_implicitly(setup):
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"execution": dict.fromkeys(REQUIRED_CAPABILITIES, True)})
        return httpx.Response(202, json={"root_operation_id": "run-1"})

    args, _, _ = setup
    client = ServerlessClient(
        httpx.Client(base_url="https://s2.invalid", transport=httpx.MockTransport(handle)),
        capability_path="/v1/capabilities",
    )
    payload = invoke_payload(
        args["profile"], profile_ref="profile", identity_ref="identity", private_input_ref="ref"
    )
    assert client.submit("gromacs", payload, idempotency_key="request-1")["root_operation_id"] == "run-1"
    assert calls[-1].headers["Idempotency-Key"] == "request-1"
    assert len(calls) == 2


def test_manifest_tampering_is_detected(setup):
    app, objects, lifecycle = initialized_application(setup)
    app.worker.checkpoint()
    objects.data["manifests/" + lifecycle.head] = b"{}"
    with pytest.raises(ValueError, match="identity|reference"):
        app.artifacts.load(lifecycle.head)


def test_digest_is_sha256_not_s3_multipart_etag(setup):
    app, _, lifecycle = initialized_application(setup)
    app.worker.checkpoint()
    file = app.artifacts.load(lifecycle.head).files[0]
    assert file.sha256 == hashlib.sha256(b"synthetic protocol").hexdigest()


def test_status_and_cancel_use_serverless_state_without_a_local_retry_loop():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "root_operation_id": "run-1",
                "phase": "CANCELLED" if request.method == "POST" else "FINALIZING",
            },
        )

    client = ServerlessClient(
        httpx.Client(base_url="https://s2.invalid", transport=httpx.MockTransport(handle)),
        capability_path="/v1/capabilities",
    )
    assert client.status("run-1")["phase"] == "FINALIZING"
    assert client.cancel("run-1", idempotency_key="cancel-1")["phase"] == "CANCELLED"
    assert [(item.method, item.url.path) for item in calls] == [
        ("GET", "/v1/operations/run-1"),
        ("POST", "/v1/operations/run-1:cancel"),
    ]


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"execution": []},
        {"execution": "all"},
        {"execution": dict.fromkeys(REQUIRED_CAPABILITIES, "true")},
    ],
)
def test_malformed_or_stringified_capabilities_are_not_accepted(document):
    client = ServerlessClient(
        httpx.Client(
            base_url="https://s2.invalid",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=document)),
        ),
        capability_path="/v1/capabilities",
    )
    with pytest.raises(UnsupportedExecution):
        client.require_capabilities()


def test_http_and_redirects_cannot_expose_confidential_references():
    with pytest.raises(ValueError):
        ServerlessClient(httpx.Client(base_url="http://s2.invalid"), capability_path="/v1/capabilities")
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://other.invalid"})

    client = ServerlessClient(
        httpx.Client(base_url="https://s2.invalid", transport=httpx.MockTransport(handle)),
        capability_path="/v1/capabilities",
    )
    with pytest.raises(UnsupportedExecution):
        client.require_capabilities()
    assert len(calls) == 1


def test_original_worker_checkpoint_identity_and_modes_remain_unchanged(setup, tmp_path):
    from fs2_gromacs.contracts import canonical, normalize
    from fs2_gromacs.worker import Workflow

    args, _, _ = setup
    run = args["run"]
    worker = Workflow(run.parameters, job_id=run.job_id, operation_id="legacy", workspace=tmp_path)
    expected = hashlib.sha256(
        canonical(
            {
                "request": normalize(run.parameters),
                "job": run.job_id,
                "image": worker.engine_id,
            }
        )
    ).hexdigest()
    assert worker.recipe == expected and worker.checkpoint_mode == "local"
    with pytest.raises(ValueError):
        Workflow(
            run.parameters,
            job_id=run.job_id,
            operation_id="run",
            workspace=tmp_path,
            checkpoint_mode="application",
        )


def test_invalid_private_workflow_error_does_not_repeat_request_contents(setup):
    args, _, _ = setup
    with pytest.raises(ValidationError) as error:
        GromacsRun(
            job_id="replica",
            input_bundle=args["run"].input_bundle,
            parameters={"confidential": "patient-sentinel-never-log"},
        )
    assert "patient-sentinel" not in str(error.value)


def test_completed_manifest_recovers_after_callback_loss_without_running_engine(setup, tmp_path, monkeypatch):
    args, _, _ = setup
    app, _, lifecycle = initialized_application(setup)
    app.worker.checkpoint()
    lifecycle.fail_ack = True
    with pytest.raises(DeliveryPending):
        app.finish_delivery(successful_result(app))
    receipt = json.loads((app.workspace / "delivery.json").read_text())
    replacement = Attempt.model_validate({**app.attempt.model_dump(), "attempt_id": "attempt-2", "epoch": 2})
    lifecycle.current, lifecycle.fail_ack = replacement, False
    recovered = GromacsApplication(**{**args, "attempt": replacement, "workspace": tmp_path / "replacement"})
    monkeypatch.setattr(recovered.worker, "run", lambda: pytest.fail("completed computation ran again"))
    assert recovered.execute(receipt["reference"])["export"] == "committed"

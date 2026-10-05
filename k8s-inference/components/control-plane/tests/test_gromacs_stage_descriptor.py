"""Late native restarts use bounded Pod metadata and an authenticated file."""

import asyncio
import hashlib
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from test_gromacs_adapter import OP, source
from test_gromacs_compact_capability import active_workload
from test_gromacs_execution_shapes import mpi_request, renderer, resolver, resource_for

from fs2_serve import scientific_companion_cli as cli
from fs2_serve.scientific_artifacts import EphemeralHandle
from fs2_serve.scientific_batch.capability import CapabilityArtifact, ScientificWorkloadCapabilityAuthority
from fs2_serve.scientific_batch.codec import COMPACT_METADATA_SCHEMA, state_from_value, state_to_json, state_to_value
from fs2_serve.scientific_batch.companion import WorkloadArtifactHttpClient, _invocation
from fs2_serve.scientific_batch.execution import _invocation_json
from fs2_serve.scientific_batch.models import ResolvedArtifactMaterialization
from fs2_serve.scientific_batch.stage_descriptor import descriptor_bytes, invocation_json, verified_descriptor
from fs2_serve.scientific_batch.workload_routes import scientific_workload_artifact_router
from fs2_serve.scientific_run_result import ArtifactRef


def bindings(entries):
    return tuple(
        CapabilityArtifact(
            logical_artifact_id=item.logical_artifact_id,
            artifact_id=item.artifact_id,
            digest=item.digest,
            size_bytes=item.size_bytes,
            media_type=item.media_type,
            compression=item.compression,
        )
        for item in entries
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 3000, 5000, 20000])
async def test_frozen_descriptor_roundtrips_large_inventory_over_authorized_http(hasher, count):
    state, resource, repository = active_workload(count)
    assert state_from_value(state_to_json(state)) == state
    if count == 20000:
        encoded = state_to_value(state)
        assert encoded["adapter_execution"]["encoding"] == COMPACT_METADATA_SCHEMA
        assert encoded["input_manifest"]["encoding"] == COMPACT_METADATA_SCHEMA
        # Mutable controller transitions cannot rewrite frozen compressed bytes;
        # PostgreSQL compares these exact subdocuments for immutability.
        changed = state_to_value(replace(state, cancel_requested=True, revision=state.revision + 1))
        assert changed["adapter_execution"] == encoded["adapter_execution"]
        assert changed["input_manifest"] == encoded["input_manifest"]
    authority = ScientificWorkloadCapabilityAuthority(hasher)
    token = authority.issue(resource)
    expected = descriptor_bytes(resource.invocation, bindings(state.input_manifest.entries))
    # The compact serializer must retain the legacy companion invocation shape.
    assert invocation_json(resource.invocation) == _invocation_json(resource.invocation)
    assert _invocation(invocation_json(resource.invocation)) == resource.invocation
    app = FastAPI()
    app.include_router(
        scientific_workload_artifact_router(
            authority=authority,
            artifacts=object(),
            batches=repository,
        )
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/internal/scientific-workloads/stage-descriptor",
            headers={
                "Authorization": "Bearer " + token,
            },
        )
        assert response.status_code == 200
        assert response.content == expected
        assert response.headers["cache-control"] == "no-store"
        assert len(response.json()["materializations"]) == count
        repository.records[state.operation_id] = replace(state, cancel_requested=True)
        assert (
            await client.get(
                "/internal/scientific-workloads/stage-descriptor",
                headers={
                    "Authorization": "Bearer " + token,
                },
            )
        ).status_code == 409
        assert (await client.get("/internal/scientific-workloads/stage-descriptor")).status_code == 401


@pytest.mark.parametrize("nodes", [1, 2])
def test_thousands_of_files_render_without_growing_pod_argv_or_environment(tmp_path, nodes):
    runtime, profile = renderer(tmp_path)
    body = mpi_request(nodes, 1)
    count = 3000
    body["parameters"]["continuation_files"] = [
        {"input_id": f"resume-{i:05d}", "path": f"md.part{i:05d}.xtc"} for i in range(count)
    ]
    entries = tuple(
        replace(
            source(),
            logical_artifact_id=f"resume-{i:05d}",
            artifact_id=uuid4(),
            semantic_type="gromacs-continuation-file/v1",
            media_type="application/octet-stream",
            compression=None,
        )
        for i in range(count)
    )
    from test_gromacs_execution_shapes import ACCESS

    plan = runtime.plan(profile, body, operation_id=UUID(OP), access_context=ACCESS, input_artifacts=entries)
    snapshot = resolver(runtime).freeze(
        service_class="customer-batch",
        model_id="gromacs-mpi",
        tenant_id="tenant-a",
        profile=profile.value,
        plan=plan.controller_plan,
    )
    invocation = plan.invocations[0]
    template_plan = replace(
        plan,
        invocations=(
            replace(invocation, consumes=(invocation.consumes[0],), materializations=(invocation.materializations[0],)),
        ),
    )
    resource = replace(
        resource_for(template_plan, snapshot, nodes),
        invocation=invocation,
        materializations=tuple(
            ResolvedArtifactMaterialization.resolve(
                item,
                artifact_id=entry.artifact_id,
                digest=entry.digest,
                size_bytes=entry.size_bytes,
                media_type=entry.media_type,
                compression=entry.compression,
            )
            for item, entry in zip(invocation.materializations, entries, strict=True)
        ),
    )
    manifest = runtime.render(resource)
    pod = (
        manifest["spec"]["template"]
        if nodes == 1
        else manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]
    )
    assert len(json.dumps(manifest).encode()) < 40000
    containers = pod["spec"]["containers"] + pod["spec"]["initContainers"]
    for container in containers:
        assert all(len(item.get("value", "")) < 4096 for item in container.get("env", []))
        assert "FS2_STAGE_INVOCATION_JSON" not in {item["name"] for item in container.get("env", [])}
    materializer = next(item for item in containers if item["name"] == "materialize-inputs")
    assert materializer["command"] == ["fs2-serve", "scientific-materialize-many"]
    descriptor_env = {item["name"]: item.get("value") for item in materializer["env"]}
    assert (
        descriptor_env["FS2_STAGE_DESCRIPTOR_SHA256"]
        == hashlib.sha256(descriptor_bytes(invocation, bindings(entries))).hexdigest()
    )


def test_materializer_reads_large_descriptor_file_and_verifies_every_input(monkeypatch, tmp_path):
    state, resource, _ = active_workload(3000)
    content = descriptor_bytes(resource.invocation, bindings(state.input_manifest.entries))
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_bytes(content)
    monkeypatch.setattr(cli, "_contained", lambda path: path)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    monkeypatch.setenv("FS2_STAGE_DESCRIPTOR_FILE", str(descriptor))
    monkeypatch.setenv("FS2_STAGE_DESCRIPTOR_SHA256", hashlib.sha256(content).hexdigest())
    monkeypatch.setenv("FS2_SCIENTIFIC_INTERNAL_API_URL", "http://unit.test")
    monkeypatch.setenv("FS2_SCIENTIFIC_WORKLOAD_CAPABILITY", "test-only")
    prepared = []
    client = SimpleNamespace(client=SimpleNamespace(close=lambda: None), prepare_downloads=prepared.append)
    monkeypatch.setattr(cli, "WorkloadArtifactHttpClient", lambda **kwargs: client)
    calls = []
    monkeypatch.setattr(cli, "materialize_artifact", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["fs2-serve", "scientific-materialize-many"])
    cli.main()
    assert len(calls) == 3000
    assert {call["artifact_id"] for call in calls} == {item.artifact_id for item in state.input_manifest.entries}
    assert sum(map(len, prepared)) == 3000 and max(map(len, prepared)) == 128
    assert all(call["expected_digest"] == "sha256:" + "b" * 64 for call in calls)
    descriptor.write_bytes(content + b" ")
    with pytest.raises(ValueError, match="frozen identity"):
        cli.main()
    assert len(calls) == 3000


def test_descriptor_download_retries_only_transient_errors(monkeypatch):
    _, resource, _ = active_workload(1)
    content = descriptor_bytes(resource.invocation, bindings(resource.materializations))
    attempts = []

    def respond(request):
        attempts.append(request)
        return httpx.Response(503 if len(attempts) == 1 else 200, content=content)

    monkeypatch.setattr("fs2_serve.scientific_batch.companion.time.sleep", lambda _: None)
    client = WorkloadArtifactHttpClient(
        base_url="http://unit.test", capability="test-only", client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    assert client.stage_descriptor() == content
    assert len(attempts) == 2
    verified_descriptor(content, hashlib.sha256(content).hexdigest())
    with pytest.raises(ValueError, match="frozen identity"):
        verified_descriptor(content, "0" * 64)


@pytest.mark.parametrize("damage", ["digest", "compressed", "size", "bomb"])
def test_large_immutable_metadata_rejects_corruption_and_expansion_outside_bound(damage):
    state, _, _ = active_workload(20000)
    document = state_to_value(state)
    compact = document["adapter_execution"]
    assert compact["encoding"] == COMPACT_METADATA_SCHEMA
    if damage == "digest":
        compact["sha256"] = "0" * 64
    elif damage == "compressed":
        compact["data"] = "invalid-base64"
    elif damage == "size":
        compact["size_bytes"] += 1
    else:
        compact["size_bytes"] = 32 * 1024**2 + 1
    with pytest.raises(ValueError, match="immutable metadata"):
        state_from_value(document)


@pytest.mark.asyncio
async def test_batch_input_handles_preserve_exact_capability_scope_with_bounded_parallelism(hasher):
    state, resource, repository = active_workload(20000)
    authority = ScientificWorkloadCapabilityAuthority(hasher)
    token = authority.issue(resource)
    entries = {entry.artifact_id: entry for entry in state.input_manifest.entries}
    counters = {"active": 0, "max": 0, "calls": 0}

    async def download(artifact_id, *, tenant_id):
        assert tenant_id == "system"
        counters["calls"] += 1
        counters["active"] += 1
        counters["max"] = max(counters["max"], counters["active"])
        await asyncio.sleep(0)
        entry = entries[artifact_id]
        ref = ArtifactRef(
            artifact_id=str(artifact_id),
            sha256=entry.digest.removeprefix("sha256:"),
            size_bytes=entry.size_bytes,
            media_type=entry.media_type,
            compression="none",
        )
        counters["active"] -= 1
        return SimpleNamespace(
            artifact=SimpleNamespace(
                digest=entry.digest,
                size_bytes=entry.size_bytes,
                media_type=entry.media_type,
                compression=None,
                to_public_ref=lambda: ref,
            ),
            handle=EphemeralHandle(
                method="GET", url="https://objects.test/input", expires_at=datetime.now(UTC) + timedelta(minutes=10)
            ),
        )

    app = FastAPI()
    app.include_router(
        scientific_workload_artifact_router(
            authority=authority, artifacts=SimpleNamespace(download=download), batches=repository
        )
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": "Bearer " + token}
        ids = [str(item) for item in list(entries)[:128]]
        response = await client.post(
            "/internal/scientific-workloads/artifacts:download", json={"artifact_ids": ids}, headers=headers
        )
        assert response.status_code == 200
        assert [item["artifact"]["artifact_id"] for item in response.json()] == ids
        assert counters["calls"] == 128 and counters["max"] == 8
        for bad in ([str(uuid4())], [ids[0], ids[0]]):
            response = await client.post(
                "/internal/scientific-workloads/artifacts:download", json={"artifact_ids": bad}, headers=headers
            )
            assert response.status_code == 403
        assert counters["calls"] == 128
        response = await client.post(
            "/internal/scientific-workloads/artifacts:download",
            json={"artifact_ids": ids + [str(uuid4())]},
            headers=headers,
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_bulk_uploads_retain_original_tenant_operation_attempt_and_idempotent_ids(hasher):
    state, resource, repository = active_workload(20000)
    authority = ScientificWorkloadCapabilityAuthority(hasher)
    token = authority.issue(resource)
    opened, begun, finalized = [], {}, []

    async def open_attempt(request):
        opened.append(request)

    async def begin_upload(request):
        assert request.tenant_id == "system" and request.operation_id == state.operation_id
        assert request.attempt_id == resource.attempt_id
        begun[request.upload_id] = request
        return SimpleNamespace(
            upload=SimpleNamespace(upload_id=request.upload_id),
            handle=EphemeralHandle(
                method="PUT", url="https://objects.test/output", expires_at=datetime.now(UTC) + timedelta(minutes=10)
            ),
        )

    async def finalize_upload(request):
        assert request.tenant_id == "system" and request.operation_id == state.operation_id
        finalized.append(request.upload_id)
        source = begun[request.upload_id]
        ref = ArtifactRef(
            artifact_id=str(request.upload_id),
            sha256=source.expected_digest.removeprefix("sha256:"),
            size_bytes=source.expected_size_bytes,
            media_type=source.media_type,
            compression="none",
        )
        return SimpleNamespace(to_public_ref=lambda: ref)

    app = FastAPI()
    app.include_router(
        scientific_workload_artifact_router(
            authority=authority,
            batches=repository,
            artifacts=SimpleNamespace(
                open_attempt=open_attempt, begin_upload=begin_upload, finalize_upload=finalize_upload
            ),
        )
    )
    requests = [
        {
            "upload_id": str(uuid4()),
            "sha256": hashlib.sha256(str(i).encode()).hexdigest(),
            "size_bytes": i,
            "media_type": "application/octet-stream",
            "compression": "none",
        }
        for i in range(64)
    ]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": "Bearer " + token}
        for _ in range(2):
            response = await client.post(
                "/internal/scientific-workloads/uploads:batch", json={"uploads": requests}, headers=headers
            )
            assert response.status_code == 201
            assert [item["upload_id"] for item in response.json()] == [item["upload_id"] for item in requests]
        assert len(begun) == 64
        assert all(item.attempt_id == resource.attempt_id for item in opened)
        response = await client.post(
            "/internal/scientific-workloads/uploads:finalize",
            json={"upload_ids": [item["upload_id"] for item in requests]},
            headers=headers,
        )
        assert response.status_code == 200 and len(finalized) == 64
        repository.records[state.operation_id] = replace(state, cancel_requested=True)
        response = await client.post(
            "/internal/scientific-workloads/uploads:finalize",
            json={"upload_ids": [requests[0]["upload_id"]]},
            headers=headers,
        )
        assert response.status_code == 409


@pytest.mark.parametrize("fail_put", [False, True])
def test_bulk_file_client_streams_validates_and_never_finalizes_failed_transfers(tmp_path, fail_put):
    paths = tuple(tmp_path / f"native-{i}.bin" for i in range(5))
    for i, path in enumerate(paths):
        path.write_bytes((str(i) + "-native-bytes").encode())
    uploads, stored, requests = {}, {}, []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path.endswith("uploads:batch"):
            body = json.loads(request.content)
            uploads.update({item["upload_id"]: item for item in body["uploads"]})
            return httpx.Response(
                201,
                json=[
                    {
                        "upload_id": item["upload_id"],
                        "handle": {"method": "PUT", "url": "https://objects.test/" + item["upload_id"], "headers": {}},
                    }
                    for item in body["uploads"]
                ],
            )
        if request.method == "PUT":
            if fail_put:
                return httpx.Response(403)
            stored[request.url.path[1:]] = request.content
            return httpx.Response(200)
        if request.url.path.endswith("uploads:finalize"):
            refs = []
            for identity in json.loads(request.content)["upload_ids"]:
                item = uploads[identity]
                assert hashlib.sha256(stored[identity]).hexdigest() == item["sha256"]
                refs.append(
                    {
                        "artifact_id": identity,
                        "sha256": item["sha256"],
                        "size_bytes": len(stored[identity]),
                        "media_type": item["media_type"],
                        "compression": "none",
                    }
                )
            return httpx.Response(200, json=refs)
        raise AssertionError("unexpected bulk transfer route")

    client = WorkloadArtifactHttpClient(
        base_url="https://api.test", capability="test-only", client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    if fail_put:
        with pytest.raises(httpx.HTTPStatusError):
            client.upload_files(identity="native", paths=paths, media_type="application/octet-stream", compression=None)
        assert not any(path.endswith("uploads:finalize") for path in requests)
    else:
        refs = client.upload_files(
            identity="native", paths=paths, media_type="application/octet-stream", compression=None
        )
        assert len(refs) == len(paths)
        assert len(requests) == len(paths) + 2

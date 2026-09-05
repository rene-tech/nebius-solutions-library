from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from fs2_serve.scientific_batch import companion
from fs2_serve.scientific_batch.adapters import CollectionPendingError, ScientificAdapterError, staged_workspace
from fs2_serve.scientific_batch.adapters.staged_workspace import (
    STAGE_COMPLETION_RELATIVE_PATH,
    collect_workspace_handoff,
    completion_marker,
    contained_stable_file,
    snapshot_workspace,
    wrap_stage_argv,
)
from fs2_serve.scientific_batch.execution import _invocation_json
from fs2_serve.scientific_batch.models import MaterializationMode, StageInvocation, StageWorkspaceDocument


def _invocation(command: tuple[str, ...]) -> StageInvocation:
    workspace = "/mnt/fs2-scientific/work/test/main"
    return StageInvocation(
        stage_id="prepare",
        shard_id="main",
        argv=wrap_stage_argv(workspace, command),
        environment=(),
        working_directory=workspace,
        consumes=(),
        produces="run.test.prepare.main",
        collector_id="test-collector-v1",
        validator_id="test-validator-v1",
        handoff_name="stage-handoff",
        max_output_artifacts=1,
        max_output_bytes=1024 * 1024,
    )


def _runtime_marker(invocation: StageInvocation) -> str:
    return json.dumps(
        {
            "schema": companion.RUNTIME_LOCALIZATION_SCHEMA,
            "operation_id": "00000000-0000-4000-8000-000000000001",
            "attempt_id": "00000000-0000-4000-8000-000000000002",
            "tenant_id": "staged-workspace-test",
            "model_id": "test-model",
            "variant_id": "test-variant",
            "stage_id": invocation.stage_id,
            "artifacts": [],
        }
    )


def _runner_environment(invocation: StageInvocation) -> dict[str, str]:
    return {
        **os.environ,
        "FS2_STAGE_ID": invocation.stage_id,
        "FS2_SHARD_ID": invocation.shard_id,
        "FS2_LOGICAL_OUTPUT_ID": invocation.produces,
        "FS2_COLLECTOR_ID": invocation.collector_id,
        "FS2_VALIDATOR_ID": invocation.validator_id,
    }


class _ArtifactClient:
    def __init__(self, content: bytes, *, media_type: str = "application/octet-stream") -> None:
        self.content = content
        self.media_type = media_type

    def download(
        self,
        _artifact_id: UUID,
        *,
        expected_digest: str,
        expected_size_bytes: int,
        expected_media_type: str,
    ) -> bytes:
        assert expected_digest == "sha256:" + hashlib.sha256(self.content).hexdigest()
        assert expected_size_bytes == len(self.content)
        assert expected_media_type == self.media_type
        return self.content


def test_workload_download_verifies_raw_gzip_bytes_without_http_decoding() -> None:
    artifact_id = UUID("00000000-0000-4000-8000-000000000003")
    content = gzip.compress(b"immutable scientific input", mtime=0)
    digest = hashlib.sha256(content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "artifacts.internal":
            return httpx.Response(
                200,
                json={
                    "artifact": {
                        "sha256": digest,
                        "size_bytes": len(content),
                        "media_type": "application/x-tar",
                    },
                    "handle": {
                        "method": "GET",
                        "url": "https://objects.test/input.tar.gz",
                        "headers": {},
                    },
                },
            )
        return httpx.Response(200, stream=httpx.ByteStream(content), headers={"Content-Encoding": "gzip"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = companion.WorkloadArtifactHttpClient(
        base_url="https://artifacts.internal",
        capability="test-capability",
        client=http,
    )

    assert (
        client.download(
            artifact_id,
            expected_digest=f"sha256:{digest}",
            expected_size_bytes=len(content),
            expected_media_type="application/x-tar",
        )
        == content
    )


def test_workload_download_rejects_raw_bytes_beyond_the_frozen_size() -> None:
    artifact_id = UUID("00000000-0000-4000-8000-000000000004")
    expected = b"bounded"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "artifacts.internal":
            return httpx.Response(
                200,
                json={
                    "artifact": {
                        "sha256": hashlib.sha256(expected).hexdigest(),
                        "size_bytes": len(expected),
                        "media_type": "application/octet-stream",
                    },
                    "handle": {
                        "method": "GET",
                        "url": "https://objects.test/input.bin",
                        "headers": {},
                    },
                },
            )
        return httpx.Response(200, stream=httpx.ByteStream(expected + b"-overrun"))

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = companion.WorkloadArtifactHttpClient(
        base_url="https://artifacts.internal",
        capability="test-capability",
        client=http,
    )

    with pytest.raises(ValueError, match="exceeds its immutable pointer"):
        client.download(
            artifact_id,
            expected_digest=f"sha256:{hashlib.sha256(expected).hexdigest()}",
            expected_size_bytes=len(expected),
            expected_media_type="application/octet-stream",
        )


def test_workload_download_retries_bounded_transient_pointer_and_object_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id = UUID("00000000-0000-4000-8000-000000000014")
    content = b"eventually available"
    digest = hashlib.sha256(content).hexdigest()
    pointer_attempts = 0
    object_attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal object_attempts, pointer_attempts
        if request.url.host == "artifacts.internal":
            pointer_attempts += 1
            if pointer_attempts == 1:
                raise httpx.ConnectError("connection refused", request=request)
            if pointer_attempts == 2:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={
                    "artifact": {
                        "sha256": digest,
                        "size_bytes": len(content),
                        "media_type": "application/octet-stream",
                    },
                    "handle": {
                        "method": "GET",
                        "url": "https://objects.test/input.bin",
                        "headers": {},
                    },
                },
            )
        object_attempts += 1
        if object_attempts == 1:
            return httpx.Response(429)
        return httpx.Response(200, stream=httpx.ByteStream(content))

    monkeypatch.setattr(companion.time, "sleep", sleeps.append)
    client = companion.WorkloadArtifactHttpClient(
        base_url="https://artifacts.internal",
        capability="test-capability",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert (
        client.download(
            artifact_id,
            expected_digest=f"sha256:{digest}",
            expected_size_bytes=len(content),
            expected_media_type="application/octet-stream",
        )
        == content
    )
    assert pointer_attempts == 3
    assert object_attempts == 2
    assert sleeps == [0.5, 1.0, 0.5]


def test_workload_download_does_not_retry_immutable_pointer_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []
    expected = b"frozen"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.url.host == "artifacts.internal"
        return httpx.Response(
            200,
            json={
                "artifact": {
                    "sha256": hashlib.sha256(b"different").hexdigest(),
                    "size_bytes": len(expected),
                    "media_type": "application/octet-stream",
                },
                "handle": {
                    "method": "GET",
                    "url": "https://objects.test/input.bin",
                    "headers": {},
                },
            },
        )

    monkeypatch.setattr(companion.time, "sleep", sleeps.append)
    client = companion.WorkloadArtifactHttpClient(
        base_url="https://artifacts.internal",
        capability="test-capability",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ValueError, match="pointer differs"):
        client.download(
            UUID("00000000-0000-4000-8000-000000000017"),
            expected_digest=f"sha256:{hashlib.sha256(expected).hexdigest()}",
            expected_size_bytes=len(expected),
            expected_media_type="application/octet-stream",
        )
    assert attempts == 1
    assert sleeps == []


@pytest.mark.parametrize("status", [401, 404])
def test_workload_download_does_not_retry_auth_or_not_found(status: int, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status)

    monkeypatch.setattr(companion.time, "sleep", sleeps.append)
    client = companion.WorkloadArtifactHttpClient(
        base_url="https://artifacts.internal",
        capability="test-capability",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(httpx.HTTPStatusError):
        client.download(
            UUID("00000000-0000-4000-8000-000000000015"),
            expected_digest=f"sha256:{'0' * 64}",
            expected_size_bytes=1,
            expected_media_type="application/octet-stream",
        )
    assert attempts == 1
    assert sleeps == []


def test_workload_download_stops_after_transient_retry_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    monkeypatch.setattr(companion.time, "sleep", sleeps.append)
    client = companion.WorkloadArtifactHttpClient(
        base_url="https://artifacts.internal",
        capability="test-capability",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(httpx.HTTPStatusError):
        client.download(
            UUID("00000000-0000-4000-8000-000000000016"),
            expected_digest=f"sha256:{'0' * 64}",
            expected_size_bytes=1,
            expected_media_type="application/octet-stream",
        )
    assert attempts == companion._ARTIFACT_DOWNLOAD_MAX_ATTEMPTS
    assert sleeps == [0.5, 1.0, 2.0, 4.0]


def test_proteina_initial_archive_materializes_beside_trusted_prepared_workspace_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "scientific"
    root.mkdir()
    monkeypatch.setattr(companion, "_ROOT", root)
    invocation = replace(
        _invocation(("complexa", "generate")),
        workspace_documents=(StageWorkspaceDocument(".fs2/public-request.json", '{"operation":"design-binders"}'),),
    )
    workspace = root / "work/proteina-complexa/main"
    companion.prepare_workspace(
        workspace,
        runtime_localization_json=_runtime_marker(invocation),
        stage_invocation_json=_invocation_json(invocation),
    )
    runner = workspace / companion.STAGE_RUNNER_RELATIVE_PATH
    request = workspace / ".fs2/public-request.json"
    trusted_files = {runner: runner.read_bytes(), request: request.read_bytes()}

    target_content = b"ATOM      1  CA  ALA A   1\n"
    archive_bytes = io.BytesIO()
    archive_path = "assets/target_data/bindcraft_targets/PD-L1.pdb"
    with tarfile.open(fileobj=archive_bytes, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        member = tarfile.TarInfo(archive_path)
        member.size = len(target_content)
        member.mode = 0o444
        archive.addfile(member, io.BytesIO(target_content))
    content = archive_bytes.getvalue()
    artifact_id = UUID("00000000-0000-4000-8000-000000000005")
    client = _ArtifactClient(content, media_type="application/x-tar")

    companion.materialize_artifact(
        client=client,  # type: ignore[arg-type]
        artifact_id=artifact_id,
        destination=workspace,
        mode=MaterializationMode.EXTRACT_TAR,
        compression="gzip",
        yaml_name=None,
        reuse_prefix=None,
        expected_digest="sha256:" + hashlib.sha256(content).hexdigest(),
        expected_size_bytes=len(content),
        expected_media_type="application/x-tar",
    )

    assert (workspace / archive_path).read_bytes() == target_content
    assert {path: path.read_bytes() for path in trusted_files} == trusted_files
    with pytest.raises(ValueError, match="requires an empty destination"):
        companion.materialize_artifact(
            client=client,  # type: ignore[arg-type]
            artifact_id=artifact_id,
            destination=workspace,
            mode=MaterializationMode.EXTRACT_TAR,
            compression="gzip",
            yaml_name=None,
            reuse_prefix=None,
            expected_digest="sha256:" + hashlib.sha256(content).hexdigest(),
            expected_size_bytes=len(content),
            expected_media_type="application/x-tar",
        )


def test_runner_completion_and_handoff_are_atomic_deterministic_and_materializable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "scientific"
    root.mkdir()
    monkeypatch.setattr(companion, "_ROOT", root)
    child = tmp_path / "child.py"
    child.write_text(
        "from pathlib import Path\n"
        "Path('outputs').mkdir()\n"
        "Path('outputs/result.json').write_text('{\"status\":\"passed\"}')\n",
        encoding="utf-8",
    )
    invocation = _invocation((sys.executable, str(child)))
    workspace = root / "work/test/main"
    companion.prepare_workspace(
        workspace,
        runtime_localization_json=_runtime_marker(invocation),
        stage_invocation_json=_invocation_json(invocation),
    )
    completed = subprocess.run(  # noqa: S603 - executable and argv are test-owned fixtures
        [sys.executable, workspace / companion.STAGE_RUNNER_RELATIVE_PATH, "--", *invocation.argv[3:]],
        cwd=workspace,
        env=_runner_environment(invocation),
        check=False,
    )
    assert completed.returncode == 0

    first = collect_workspace_handoff(
        invocation,
        workspace,
        label="TestModel",
        name="stage-handoff",
        semantic_type="test-workspace-handoff/v1",
        maximum_members=32,
        maximum_content_bytes=1024 * 1024,
        maximum_archive_bytes=2 * 1024 * 1024,
    )
    first_content = first.artifacts[0].path.read_bytes()
    second = collect_workspace_handoff(
        invocation,
        workspace,
        label="TestModel",
        name="stage-handoff",
        semantic_type="test-workspace-handoff/v1",
        maximum_members=32,
        maximum_content_bytes=1024 * 1024,
        maximum_archive_bytes=2 * 1024 * 1024,
    )
    assert second.artifacts[0].path.read_bytes() == first_content
    assert b"stage-complete.json" not in first_content

    destination = root / "work/test/next"
    destination.mkdir(parents=True)
    companion.materialize_artifact(
        client=_ArtifactClient(first_content),  # type: ignore[arg-type]
        artifact_id=UUID("00000000-0000-4000-8000-000000000003"),
        destination=destination,
        mode=MaterializationMode.OVERLAY_TAR,
        compression="zstd",
        yaml_name=None,
        reuse_prefix=None,
        expected_digest="sha256:" + hashlib.sha256(first_content).hexdigest(),
        expected_size_bytes=len(first_content),
        expected_media_type="application/octet-stream",
    )
    assert (destination / "outputs/result.json").read_text(encoding="utf-8") == '{"status":"passed"}'
    assert not (destination / STAGE_COMPLETION_RELATIVE_PATH).exists()


def test_runner_nonzero_has_no_completion_and_partial_or_stale_identity_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "scientific"
    root.mkdir()
    monkeypatch.setattr(companion, "_ROOT", root)
    child = tmp_path / "fail.py"
    child.write_text("raise SystemExit(23)\n", encoding="utf-8")
    invocation = _invocation((sys.executable, str(child)))
    workspace = root / "work/test/main"
    companion.prepare_workspace(
        workspace,
        runtime_localization_json=_runtime_marker(invocation),
        stage_invocation_json=_invocation_json(invocation),
    )
    completed = subprocess.run(  # noqa: S603 - executable and argv are test-owned fixtures
        [sys.executable, workspace / companion.STAGE_RUNNER_RELATIVE_PATH, "--", *invocation.argv[3:]],
        cwd=workspace,
        env=_runner_environment(invocation),
        check=False,
    )
    assert completed.returncode == 23
    with pytest.raises(CollectionPendingError):
        completion_marker(invocation, workspace, label="TestModel")

    partial = workspace / ".fs2/.stage-complete.json.1.partial"
    partial.write_text("{}", encoding="utf-8")
    with pytest.raises(CollectionPendingError):
        completion_marker(invocation, workspace, label="TestModel")
    marker = workspace / STAGE_COMPLETION_RELATIVE_PATH
    marker.write_text(
        json.dumps(
            {
                "schema": companion.STAGE_COMPLETION_SCHEMA,
                "status": "passed",
                "stage_id": "other-stage",
                "shard_id": invocation.shard_id,
                "logical_output_id": invocation.produces,
                "collector_id": invocation.collector_id,
                "validator_id": invocation.validator_id,
                "argv_sha256": "0" * 64,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    with pytest.raises(ScientificAdapterError, match="differs from the frozen invocation"):
        completion_marker(invocation, workspace, label="TestModel")


def test_snapshot_rejects_symlinks_and_oversized_or_empty_workspaces(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ScientificAdapterError, match="no regular files"):
        snapshot_workspace(
            workspace,
            label="TestModel",
            maximum_members=4,
            maximum_content_bytes=4,
        )
    target = tmp_path / "outside"
    target.write_bytes(b"x")
    (workspace / "escape").symlink_to(target)
    with pytest.raises(ScientificAdapterError, match="symbolic-link"):
        snapshot_workspace(
            workspace,
            label="TestModel",
            maximum_members=4,
            maximum_content_bytes=4,
        )
    (workspace / "escape").unlink()
    (workspace / "large").write_bytes(b"12345")
    with pytest.raises(ScientificAdapterError, match="size or type is outside the bound"):
        snapshot_workspace(
            workspace,
            label="TestModel",
            maximum_members=4,
            maximum_content_bytes=4,
        )


def test_contained_reader_rejects_traversal_and_a_file_replaced_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"{}")
    with pytest.raises(ScientificAdapterError, match="path is unsafe"):
        contained_stable_file(
            workspace,
            "../outside.json",
            maximum_bytes=16,
            label="TestModel result",
        )

    real = workspace / "real"
    real.mkdir()
    (real / "result.json").write_bytes(b"{}")
    (workspace / "alias").symlink_to(real, target_is_directory=True)
    with pytest.raises(ScientificAdapterError, match="not a contained regular file"):
        contained_stable_file(
            workspace,
            "alias/result.json",
            maximum_bytes=16,
            label="TestModel result",
        )

    target = workspace / "result.json"
    target.write_bytes(b"old!")
    original_fstat = staged_workspace.os.fstat
    calls = 0

    def racing_fstat(descriptor: int):
        nonlocal calls
        metadata = original_fstat(descriptor)
        calls += 1
        if calls == 1:
            replacement = workspace / "replacement.json"
            replacement.write_bytes(b"new!")
            os.replace(replacement, target)
        return metadata

    monkeypatch.setattr(staged_workspace.os, "fstat", racing_fstat)
    with pytest.raises(ScientificAdapterError, match="changed while it was read"):
        contained_stable_file(
            workspace,
            "result.json",
            maximum_bytes=16,
            label="TestModel result",
        )


def test_completion_rejects_a_runner_path_not_bound_to_the_workspace(tmp_path: Path) -> None:
    invocation = _invocation((sys.executable, "child.py"))
    drifted = replace(invocation, argv=("python", "/mnt/fs2-scientific/other/.fs2/stage-runner.py", "--", "true"))
    workspace = tmp_path / "workspace"
    (workspace / ".fs2").mkdir(parents=True)
    (workspace / STAGE_COMPLETION_RELATIVE_PATH).write_text("{}", encoding="utf-8")
    with pytest.raises(ScientificAdapterError, match="trusted completion runner"):
        completion_marker(drifted, workspace, label="TestModel")

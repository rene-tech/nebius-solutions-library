from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from fs2_serve.scientific_batch import companion
from fs2_serve.scientific_batch.adapters import CollectionPendingError, ScientificAdapterError
from fs2_serve.scientific_batch.adapters.staged_workspace import (
    STAGE_COMPLETION_RELATIVE_PATH,
    collect_workspace_handoff,
    completion_marker,
    snapshot_workspace,
    wrap_stage_argv,
)
from fs2_serve.scientific_batch.execution import _invocation_json
from fs2_serve.scientific_batch.models import MaterializationMode, StageInvocation


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
    def __init__(self, content: bytes) -> None:
        self.content = content

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
        assert expected_media_type == "application/octet-stream"
        return self.content


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


def test_completion_rejects_a_runner_path_not_bound_to_the_workspace(tmp_path: Path) -> None:
    invocation = _invocation((sys.executable, "child.py"))
    drifted = replace(invocation, argv=("python", "/mnt/fs2-scientific/other/.fs2/stage-runner.py", "--", "true"))
    workspace = tmp_path / "workspace"
    (workspace / ".fs2").mkdir(parents=True)
    (workspace / STAGE_COMPLETION_RELATIVE_PATH).write_text("{}", encoding="utf-8")
    with pytest.raises(ScientificAdapterError, match="trusted completion runner"):
        completion_marker(drifted, workspace, label="TestModel")

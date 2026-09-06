from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from fs2_serve.entrypoint import SCIENTIFIC_COMPANION_COMMANDS

CONTROL_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("fail_first", [False, True])
def test_many_materializations_preserve_order_verification_and_stop_on_error(monkeypatch, fail_first: bool) -> None:
    from fs2_serve import scientific_companion_cli as cli

    calls = []
    closed = []
    client = SimpleNamespace(client=SimpleNamespace(close=lambda: closed.append(True)))
    monkeypatch.setattr(cli, "WorkloadArtifactHttpClient", lambda **kwargs: client)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    monkeypatch.setenv("FS2_SCIENTIFIC_INTERNAL_API_URL", "http://unit.test")
    monkeypatch.setenv("FS2_SCIENTIFIC_WORKLOAD_CAPABILITY", "test-only")

    def materialize(**kwargs):
        calls.append(kwargs)
        if fail_first:
            raise ValueError("artifact content digest mismatch")

    monkeypatch.setattr(cli, "materialize_artifact", materialize)
    commands = [
        [
            "--logical-artifact-id",
            f"input-{i}",
            "--artifact-id",
            f"00000000-0000-4000-8000-00000000000{i}",
            "--destination",
            f"/mnt/fs2-scientific/work/input-{i}.json",
            "--mode",
            "copy-file",
            "--expected-digest",
            "sha256:" + str(i) * 64,
            "--expected-size-bytes",
            str(i),
            "--expected-media-type",
            "application/json",
        ]
        for i in (1, 2)
    ]
    monkeypatch.setattr(
        sys, "argv", ["fs2-serve", "scientific-materialize-many", "--commands-json", json.dumps(commands)]
    )
    if fail_first:
        with pytest.raises(ValueError, match="content digest mismatch"):
            cli.main()
    else:
        cli.main()
    assert closed == [True]
    assert [call["expected_size_bytes"] for call in calls] == ([1] if fail_first else [1, 2])
    assert all(call["client"] is client for call in calls)
    assert calls[0]["expected_digest"] == "sha256:" + "1" * 64
    assert str(calls[0]["destination"]) == "/mnt/fs2-scientific/work/input-1.json"
    assert cli.logging.getLogger("httpx").getEffectiveLevel() == cli.logging.WARNING


def _environment() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(CONTROL_ROOT / "src"), str(CONTROL_ROOT.parents[1] / "catalog/runtime"))),
    }


@pytest.mark.parametrize("signum", (signal.SIGTERM, signal.SIGINT))
def test_companion_installs_explicit_container_termination_handler(tmp_path: Path, signum: int) -> None:
    source = """
import os, sys, time
from pathlib import Path
from fs2_serve import scientific_companion_cli as cli
ready = Path(sys.argv[1])
def prepare(*args, **kwargs):
    ready.write_text('ready')
    time.sleep(30)
cli.prepare_workspace = prepare
os.environ['FS2_RUNTIME_ARTIFACTS_JSON'] = '{}'
os.environ['FS2_STAGE_INVOCATION_JSON'] = '{}'
sys.argv = ['fs2-serve', 'scientific-prepare-workspace', '--workspace', str(ready.parent)]
cli.main()
"""
    ready = tmp_path / "ready"
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and test-owned code
        [sys.executable, "-c", source, str(ready)], env=_environment()
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        process.send_signal(signum)
        assert process.wait(timeout=5) == 128 + signum
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize("command", SCIENTIFIC_COMPANION_COMMANDS)
def test_companion_dispatch_does_not_import_gateway(command: str) -> None:
    source = """
import sys
from fs2_serve.entrypoint import main
sys.argv = ['fs2-serve', sys.argv[1], '--help']
try:
    main()
except SystemExit as error:
    assert error.code == 0
assert not {'fs2_serve.cli', 'fs2_serve.api', 'fs2_serve.mcp_server', 'uvicorn'} & sys.modules.keys()
"""
    subprocess.run(  # noqa: S603 - fixed interpreter and test-owned command.
        [sys.executable, "-c", source, command],
        env=_environment(),
        check=True,
        capture_output=True,
        timeout=30,
    )


@pytest.mark.parametrize("corrupt", (False, True))
def test_lightweight_verifier_preserves_content_validation(tmp_path: Path, corrupt: bool) -> None:
    content = b"scientific checkpoint test content"
    checkpoint = tmp_path / "checkpoint.bin"
    checkpoint.write_bytes(content if not corrupt else b"X" * len(content))
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    marker = {
        "schema": "fs2-serve.nebius.ai/runtime-localization-marker/v1",
        "operation_id": "00000000-0000-4000-8000-000000000001",
        "attempt_id": "00000000-0000-4000-8000-000000000002",
        "tenant_id": "startup-test",
        "model_id": "startup-test",
        "variant_id": "startup-test",
        "stage_id": "inference",
        "artifacts": [
            {
                "artifact_id": "checkpoint",
                "mount_path": str(checkpoint),
                "content_digest": digest,
                "artifact_manifest_sha256": "a" * 64,
                "localization_receipt_digest": digest,
                "sub_path": None,
                "readiness_receipt_sha256": "b" * 64,
                "authorization_receipt_sha256": "c" * 64,
                "verification_receipt": None,
                "files": [{"path": "checkpoint.bin", "digest": digest, "size_bytes": len(content)}],
                "aggregate_tree": None,
            }
        ],
    }
    result = subprocess.run(  # noqa: S603 - fixed interpreter and local test marker.
        [sys.executable, "-m", "fs2_serve.entrypoint", "scientific-verify-runtime-artifacts"],
        env={**_environment(), "FS2_RUNTIME_ARTIFACTS_JSON": json.dumps(marker)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) is not corrupt
    if corrupt:
        assert "runtime artifact file digest differs" in result.stderr

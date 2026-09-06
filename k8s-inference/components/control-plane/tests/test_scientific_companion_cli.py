from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fs2_serve.entrypoint import SCIENTIFIC_COMPANION_COMMANDS

CONTROL_ROOT = Path(__file__).resolve().parents[1]


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

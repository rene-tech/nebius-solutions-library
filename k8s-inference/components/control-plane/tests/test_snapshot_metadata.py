import hashlib
import os
import stat
import subprocess
from pathlib import Path

import pytest

from fs2_serve.snapshot_metadata import (
    SnapshotWorkerLog,
    prepare_worker_log_shadow,
    preserve_shared_snapshot_metadata,
)


@pytest.mark.parametrize("changed_bytes", [False, True])
@pytest.mark.parametrize("prior_arguments", [[], ["capture-dir", "10001"]])
def test_shadow_reconstructs_only_verified_copy_and_preserves_fallback(tmp_path, changed_bytes, prior_arguments):
    source = tmp_path / "bundle"
    source.mkdir()
    (source / "cache").mkdir()
    log = source / "worker.log"
    payload = b"captured log\n"
    log.write_bytes(payload + (b"changed" if changed_bytes else b""))
    log.chmod(0o664)
    metadata = SnapshotWorkerLog(
        sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload), mode=0o644, uid=os.getuid(), gid=os.getgid()
    )
    initializer = {"command": ["/bin/sh", "-c", "true", "snapshot-tools", *prior_arguments]}
    runtime = {"command": ["supervisor", "--source-directory", "/snapshot-bundle", "restore"]}
    prepare_worker_log_shadow(initializer, runtime, str(tmp_path / "scratch"), metadata)
    command = initializer["command"]
    command[2] = command[2].replace("/snapshot-bundle", str(source))
    subprocess.run(command, check=True, capture_output=True)  # noqa: S603 - generated command, isolated fixture paths
    shadow = Path(runtime["command"][2])
    assert stat.S_IMODE(log.stat().st_mode) == 0o664
    assert log.read_bytes() == payload + (b"changed" if changed_bytes else b"")
    if changed_bytes:
        assert not (shadow / "worker.log").exists()  # Existing supervisor selects ordinary fallback.
        assert (shadow / "worker.log.invalid").exists()
    else:
        copied = shadow / "worker.log"
        assert copied.read_bytes() == payload
        assert stat.S_IMODE(copied.stat().st_mode) == 0o644
        assert (shadow / "images").is_symlink()  # No checkpoint-page copy.


def test_group_access_without_recursively_changing_shared_files():
    spec = {"securityContext": {"fsGroup": 1000, "fsGroupChangePolicy": "OnRootMismatch", "supplementalGroups": [42]}}
    preserve_shared_snapshot_metadata(spec)
    assert spec["securityContext"] == {"supplementalGroups": [42, 1000]}


def test_legacy_snapshot_without_metadata_keeps_existing_command():
    initializer = {"command": ["sh", "-c", "true", "tools"]}
    runtime = {"command": ["supervisor", "--source-directory", "/snapshot-bundle"]}
    prepare_worker_log_shadow(initializer, runtime, "/checkpoints/run", None)
    assert runtime["command"][-1] == "/snapshot-bundle"
    assert initializer["command"][2] == "true"

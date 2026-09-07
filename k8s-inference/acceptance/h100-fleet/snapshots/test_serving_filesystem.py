import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest


SOURCE = Path(__file__).resolve().parents[3] / "models/scientific-snapshot"
spec = importlib.util.spec_from_file_location(
    "snapshot_serving_filesystem", SOURCE / "serving_filesystem.py"
)
filesystem = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = filesystem
spec.loader.exec_module(filesystem)


def test_named_shm_contents_and_identity_survive_fresh_directory(tmp_path):
    donor = tmp_path / "donor-shm"
    donor.mkdir()
    data = donor / "psm_actual_backing"
    data.write_bytes(bytes(range(256)) * 100)
    data.chmod(0o600)
    bundle = tmp_path / "bundle"
    manifest = filesystem.capture(bundle, donor)
    data.unlink()
    restored = tmp_path / "restored-shm"
    restored.mkdir()
    filesystem.restore(bundle, restored)
    actual = restored / "psm_actual_backing"
    assert actual.read_bytes() == bytes(range(256)) * 100
    assert actual.stat().st_mode & 0o777 == 0o600
    assert actual.stat().st_uid == os.getuid()
    assert manifest["files"][0]["bytes"] == 25600


def test_restore_does_not_fabricate_missing_or_changed_contents(tmp_path):
    donor = tmp_path / "donor"
    donor.mkdir()
    (donor / "psm_backing").write_bytes(b"original shared data")
    bundle = tmp_path / "bundle"
    filesystem.capture(bundle, donor)
    (bundle / "filesystem/dev-shm/psm_backing").write_bytes(b"changed shared data!")
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ValueError, match="content differs"):
        filesystem.restore(bundle, target)
    assert list(target.iterdir()) == []


def test_restore_cannot_overwrite_an_existing_mapping(tmp_path):
    donor = tmp_path / "donor"
    donor.mkdir()
    (donor / "psm_backing").write_bytes(b"captured")
    filesystem.capture(tmp_path / "bundle", donor)
    with pytest.raises(FileExistsError):
        filesystem.restore(tmp_path / "bundle", donor)
    assert (donor / "psm_backing").read_bytes() == b"captured"


def test_large_logical_shared_memory_keeps_zero_extents_sparse(tmp_path):
    source = tmp_path / "source"
    with source.open("wb") as stream:
        stream.write(b"actual data")
        stream.seek(240 * 1024 * 1024)
        stream.write(b"last byte")
    target = tmp_path / "target"
    filesystem.copy_sparse(source, target)
    assert target.stat().st_size == source.stat().st_size
    assert target.stat().st_blocks * 512 < 4 * 1024 * 1024
    assert filesystem.digest(source) == filesystem.digest(target)


def test_capture_rejects_symlink_and_restore_rejects_parent_path(tmp_path):
    donor = tmp_path / "donor"
    donor.mkdir()
    (donor / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="unsupported"):
        filesystem.capture(tmp_path / "bundle", donor)
    marker = tmp_path / "invalid/filesystem/manifest.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/serving-filesystem/v1",
                "files": [{"name": "../outside"}],
            }
        )
    )
    with pytest.raises(ValueError, match="contained name"):
        filesystem.restore(tmp_path / "invalid", donor)

"""Preparation cannot silently replace capture content or upgrade its evidence."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "cosmos_snapshot_preparation", Path(__file__).with_name("prepare_snapshot.py")
)
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


def inventory():
    manifest = {
        "schema": "captured-content-and-filesystem-metadata/v2",
        "entries": [
            {"path": "worker.log", "type": "file", "bytes": 12, "sha256": "a" * 64, "mode": 420, "uid": 0, "gid": 1000},
        ],
    }
    return {
        "status": "passed",
        "read_only": True,
        "storage_path": subject.STORAGE,
        "manifest": manifest,
        "manifest_sha256": hashlib.sha256(subject.canonical(manifest)).hexdigest(),
        "bytes": 12,
        "entry_count": 1,
    }


def test_inventory_binds_exact_worker_log_bytes_and_filesystem_metadata():
    assert subject.validate_inventory(inventory()) == {
        "bytes": 12,
        "sha256": "a" * 64,
        "mode": 420,
        "uid": 0,
        "gid": 1000,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "failed"),
        ("read_only", False),
        ("storage_path", "cosmos-r7"),
        ("manifest_sha256", "0" * 64),
        ("bytes", 99),
        ("entry_count", 4),
    ],
)
def test_inventory_drift_does_not_become_qualified(field, value):
    row = inventory()
    row[field] = value
    with pytest.raises(ValueError):
        subject.validate_inventory(row)


def test_inventory_detects_mode_change_even_with_identical_file_content():
    row = copy.deepcopy(inventory())
    row["manifest"]["entries"][0]["mode"] = 384
    with pytest.raises(ValueError, match="digest"):
        subject.validate_inventory(row)


def test_video_proof_checks_decoded_shape_not_just_success(tmp_path):
    content = b"retained verified video bytes"
    (tmp_path / "output.mp4").write_bytes(content)
    docs = {
        "receipt.json": {"status_code": 200, "valid": True, "sha256": hashlib.sha256(content).hexdigest()},
        "request.json": {"size": "640x480", "num_frames": 64, "fps": 25},
        "ffprobe.json": {
            "streams": [
                {"codec_type": "video", "width": 640, "height": 480, "nb_read_frames": "65", "avg_frame_rate": "25/1"}
            ]
        },
    }
    for name, value in docs.items():
        (tmp_path / name).write_text(json.dumps(value))
    with pytest.raises(ValueError, match="shape"):
        subject.verify_output(tmp_path)
    docs["ffprobe.json"]["streams"][0]["nb_read_frames"] = "64"
    (tmp_path / "ffprobe.json").write_text(json.dumps(docs["ffprobe.json"]))
    assert subject.verify_output(tmp_path)["output_sha256"] == hashlib.sha256(content).hexdigest()

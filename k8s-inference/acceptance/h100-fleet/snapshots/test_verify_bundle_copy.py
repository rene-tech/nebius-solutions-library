import importlib.util
from pathlib import Path
import shutil
import socket

import pytest


spec = importlib.util.spec_from_file_location(
    "verify_bundle_copy", Path(__file__).with_name("verify_bundle_copy.py")
)
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


def copied_bundle(tmp_path):
    source = tmp_path / "captured"
    (source / "images").mkdir(parents=True)
    (source / "images/compatibility.json").write_text("{}")
    (source / "worker.log").write_bytes(b"captured worker log")
    (source / "worker.log").chmod(0o644)
    destination = tmp_path / "copy"
    shutil.copytree(source, destination)
    (destination / "worker.log").chmod(0o600)
    return source, destination


def test_restores_exact_captured_mode_after_content_verification(tmp_path):
    source, destination = copied_bundle(tmp_path)
    receipt = publication.verify(source, destination)
    assert receipt["status"] == "passed"
    assert receipt["metadata_corrected"] == ["worker.log"]
    assert (destination / "worker.log").stat().st_mode & 0o777 == 0o644
    assert (source / "worker.log").read_bytes() == b"captured worker log"


def test_changed_bytes_are_rejected_without_metadata_mutation(tmp_path):
    source, destination = copied_bundle(tmp_path)
    (destination / "worker.log").write_bytes(b"modified worker log")
    with pytest.raises(ValueError, match="content differs"):
        publication.verify(source, destination)
    assert (destination / "worker.log").stat().st_mode & 0o777 == 0o600


def test_incomplete_or_overlapping_copies_are_rejected(tmp_path):
    source, destination = copied_bundle(tmp_path)
    with pytest.raises(ValueError, match="separate"):
        publication.verify(source, source)
    (destination / "worker.log").unlink()
    with pytest.raises(ValueError, match="file set differs"):
        publication.verify(source, destination)


def test_preserves_captured_unix_socket_mode_without_reading_endpoint(tmp_path):
    source, destination = copied_bundle(tmp_path)
    with socket.socket(socket.AF_UNIX) as donor, socket.socket(socket.AF_UNIX) as copy:
        donor.bind(str(source / "worker.sock"))
        copy.bind(str(destination / "worker.sock"))
        (source / "worker.sock").chmod(0o755)
        (destination / "worker.sock").chmod(0o700)
        receipt = publication.verify(source, destination)
    assert "worker.sock" in receipt["metadata_corrected"]
    assert (destination / "worker.sock").stat().st_mode & 0o777 == 0o755

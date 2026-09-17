from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "checkpoint_durability",
    ROOT / "reference-data" / "verify_checkpoint_durability.py",
)
assert SPEC and SPEC.loader
durability = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(durability)


def test_candidate_crash_cannot_wedge_a_later_no_delete_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = "a" * 64
    payload = b'{"complete":true}\n'
    target = tmp_path / f".fs2-durability-{generation[:24]}"
    real_link = durability.os.link

    def crash_before_publish(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated crash after candidate fsync")

    monkeypatch.setattr(durability.os, "link", crash_before_publish)
    with pytest.raises(OSError, match="simulated crash"):
        durability.publish_marker(tmp_path, target, generation, payload)
    assert not target.exists()
    first_candidates = sorted(tmp_path.glob(".fs2-durability-candidate-*"))
    assert len(first_candidates) == 1
    assert first_candidates[0].read_bytes() == payload

    monkeypatch.setattr(durability.os, "link", real_link)
    durability.publish_marker(tmp_path, target, generation, payload)
    assert target.read_bytes() == payload
    # No failed or successful candidate was unlinked.
    assert len(list(tmp_path.glob(".fs2-durability-candidate-*"))) == 2


def test_short_writes_are_completed_before_atomic_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = "b" * 64
    payload = b"0123456789abcdef\n"
    target = tmp_path / f".fs2-durability-{generation[:24]}"
    real_write = durability.os.write

    def short_write(descriptor: int, value: bytes) -> int:
        return real_write(descriptor, value[:3])

    monkeypatch.setattr(durability.os, "write", short_write)
    durability.publish_marker(tmp_path, target, generation, payload)
    assert target.read_bytes() == payload


def test_atomic_publication_never_overwrites_a_different_final_marker(tmp_path: Path) -> None:
    generation = "c" * 64
    target = tmp_path / f".fs2-durability-{generation[:24]}"
    target.write_bytes(b"different\n")
    with pytest.raises(durability.DurabilityError, match="differs|exact regular file"):
        durability.publish_marker(tmp_path, target, generation, b"expected\n")
    assert target.read_bytes() == b"different\n"


def test_retry_fsyncs_directory_after_verified_existing_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = "d" * 64
    args = SimpleNamespace(
        root=tmp_path,
        generation=generation,
        attempt=1,
        pvc_uid="pvc-uid",
        pvc_resource_version="17",
        volume_name="pv-name",
        challenge="retry-after-link",
        mode="write",
    )
    payload = durability.canonical(
        {
            "schema": "fs2-serve.nebius.ai/checkpoint-durability-marker/v2",
            "pvc": {"uid": "pvc-uid", "resource_version": "17", "volume_name": "pv-name"},
            "challenge": "retry-after-link",
            "generation": generation,
            "attempt": 1,
        }
    ) + b"\n"
    target = tmp_path / f".fs2-durability-{generation[:24]}"
    target.write_bytes(payload)
    fsynced: list[int] = []
    real_fsync = durability.os.fsync

    def record_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            fsynced.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(durability.os, "fsync", record_fsync)
    monkeypatch.setattr(durability, "validate_root", lambda _root: tmp_path)
    durability.proof(args)
    assert len(fsynced) == 1

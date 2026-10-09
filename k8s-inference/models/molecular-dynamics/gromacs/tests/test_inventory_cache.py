"""Incremental hashing must preserve the existing stopped-file integrity gate."""

import hashlib
import os
import time

import pytest
from fs2_gromacs import files


def test_unchanged_segments_are_hashed_once_but_modified_checkpoint_is_rehashed(tmp_path, monkeypatch):
    original = files.digest_file
    reads = []

    def measured(path):
        reads.append(path.name)
        return original(path)

    monkeypatch.setattr(files, "digest_file", measured)
    (tmp_path / "md.part0001.xtc").write_bytes(b"unchanged trajectory")
    checkpoint = tmp_path / "native.cpt"
    checkpoint.write_bytes(b"first checkpoint")
    time.sleep(1.05)  # Closed prior segments, outside filesystem timestamp granularity.
    cache = files.FileDigestCache()
    first = files.inventory(tmp_path, max_bytes=1000, digest_cache=cache)
    assert files.inventory(tmp_path, max_bytes=1000, digest_cache=cache) == first
    assert len(reads) == 2
    checkpoint.write_bytes(b"second checkpoint")
    second = files.inventory(tmp_path, max_bytes=1000, digest_cache=cache)
    assert len(reads) == 3 and reads[-1] == "native.cpt"
    assert next(row for row in second if row["path"] == "native.cpt")["sha256"] == hashlib.sha256(
        b"second checkpoint"
    ).hexdigest()


def test_replacing_same_size_bytes_and_restoring_mtime_still_invalidates(tmp_path):
    path = tmp_path / "data.xtc"
    path.write_bytes(b"first")
    cache = files.FileDigestCache()
    before = path.stat()
    first = cache.digest(path)
    path.write_bytes(b"other")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert cache.digest(path) != first
    assert cache.digest(path) == hashlib.sha256(b"other").hexdigest()


def test_restart_cache_is_cold_and_bounded(tmp_path, monkeypatch):
    original = files.digest_file
    reads = []
    monkeypatch.setattr(files, "digest_file", lambda path: (reads.append(path), original(path))[1])
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    time.sleep(1.05)
    cache = files.FileDigestCache(max_entries=1)
    cache.digest(first)
    cache.digest(second)
    cache.digest(first)
    assert len(reads) == 3
    files.FileDigestCache().digest(first)
    assert len(reads) == 4
    with pytest.raises(ValueError):
        files.FileDigestCache(max_entries=0)


def test_cached_file_replaced_by_link_or_changed_during_hash_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "native.xtc"
    path.write_bytes(b"first")
    cache = files.FileDigestCache()
    cache.digest(path)
    other = tmp_path / "other"
    other.write_bytes(b"other")
    path.unlink()
    path.symlink_to(other)
    with pytest.raises(ValueError, match="regular files"):
        cache.digest(path)
    path.unlink()
    path.write_bytes(b"first")
    original = files.digest_file

    def changing(source):
        result = original(source)
        source.write_bytes(b"changed")
        return result

    monkeypatch.setattr(files, "digest_file", changing)
    with pytest.raises(ValueError, match="changed"):
        cache.digest(path)


def test_inventory_cache_never_bypasses_byte_or_file_budget(tmp_path, monkeypatch):
    (tmp_path / "first").write_bytes(b"12345")
    cache = files.FileDigestCache()
    files.inventory(tmp_path, max_bytes=10, digest_cache=cache)
    with pytest.raises(ValueError, match="budget"):
        files.inventory(tmp_path, max_bytes=4, digest_cache=cache)
    (tmp_path / "second").write_bytes(b"second")
    monkeypatch.setattr(files, "MAX_WORKSPACE_FILES", 1)
    with pytest.raises(ValueError, match="budget"):
        files.inventory(tmp_path, max_bytes=100, digest_cache=cache)


def test_twenty_thousand_file_late_inventory_is_complete(tmp_path):
    for number in range(20_000):
        (tmp_path / f"segment-{number:05d}.log").write_bytes(str(number).encode())
    cache = files.FileDigestCache()
    cold = files.inventory(tmp_path, max_bytes=100_000, digest_cache=cache)
    warm = files.inventory(tmp_path, max_bytes=100_000, digest_cache=cache)
    assert len(cold) == 20_000 and warm == cold
    assert files.MAX_WORKSPACE_FILES >= len(cold)

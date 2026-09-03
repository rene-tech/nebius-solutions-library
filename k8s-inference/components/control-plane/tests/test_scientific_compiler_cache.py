from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fs2_serve.scientific_batch.compiler_cache import (
    CACHE_MARKER,
    CompilerCacheIdentity,
    artifact_set_sha256,
    prepare_compiler_cache,
)


def identity(*, image: str = "a", artifact: str = "b", sm: str = "sm90") -> CompilerCacheIdentity:
    return CompilerCacheIdentity(
        model_id="protenix-v2",
        stage_id="sample-structure",
        variant_id="upstream-v2-0-0",
        runtime_image_digest="sha256:" + image * 64,
        artifact_set_sha256=artifact * 64,
        accelerator_sm=sm,
    )


def test_cache_identity_separates_image_artifact_and_accelerator() -> None:
    expected = identity()
    assert CompilerCacheIdentity.from_mapping(expected.marker()) == expected
    assert (
        len(
            {
                expected.sub_path,
                identity(image="c").sub_path,
                identity(artifact="d").sub_path,
                identity(sm="sm80").sub_path,
            }
        )
        == 4
    )
    assert expected.sub_path.endswith("/sm90")
    assert "/" + "a" * 64 + "/" in expected.sub_path
    assert "/" + "b" * 64 + "/" in expected.sub_path

    changed = expected.marker()
    changed["artifact_set_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="digest or subPath differs"):
        CompilerCacheIdentity.from_mapping(changed)


def test_artifact_set_identity_is_order_independent_and_content_bound() -> None:
    first = {
        "artifact_id": "weights",
        "content_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
    }
    second = {
        "artifact_id": "ccd",
        "content_sha256": "3" * 64,
        "manifest_sha256": "4" * 64,
    }
    assert artifact_set_sha256([first, second]) == artifact_set_sha256([second, first])
    changed = dict(second, manifest_sha256="5" * 64)
    assert artifact_set_sha256([first, second]) != artifact_set_sha256([first, changed])


@pytest.mark.skipif(os.geteuid() != 0, reason="exact UID/GID 10001 ownership probe requires root")
def test_cache_preparer_chowns_and_proves_nonroot_write_read_reuse(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache-pvc"
    cache_root.mkdir(mode=0o755)
    expected = identity()

    prepare_compiler_cache(cache_root, sub_path=expected.sub_path, identity=expected)
    instance = cache_root / expected.sub_path
    stat = instance.stat()
    assert (stat.st_uid, stat.st_gid, stat.st_mode & 0o777) == (10001, 10001, 0o700)
    assert json.loads((instance / CACHE_MARKER).read_text()) == expected.marker()

    def nonroot_reuse(*, write: bool) -> None:
        directory_fd = os.open(instance, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        child = os.fork()
        if child == 0:
            os.setgroups([])
            os.setgid(10001)
            os.setuid(10001)
            if write:
                descriptor = os.open(
                    "compiled-kernel.bin",
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                os.write(descriptor, b"warm-cache-reuse")
                os.fsync(descriptor)
                os.close(descriptor)
            else:
                descriptor = os.open("compiled-kernel.bin", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                if os.read(descriptor, 64) != b"warm-cache-reuse":
                    os._exit(2)
                os.close(descriptor)
            os._exit(0)
        _pid, status = os.waitpid(child, 0)
        os.close(directory_fd)
        assert status == 0

    nonroot_reuse(write=True)
    prepare_compiler_cache(cache_root, sub_path=expected.sub_path, identity=expected)
    nonroot_reuse(write=False)


def test_cache_preparer_rejects_symlinked_identity_path(tmp_path: Path) -> None:
    expected = identity()
    cache_root = tmp_path / "cache-pvc"
    target = tmp_path / "tenant-controlled"
    target.mkdir()
    prefix = cache_root / "scientific-cache"
    prefix.mkdir(parents=True)
    (prefix / "v1").symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError):
        prepare_compiler_cache(cache_root, sub_path=expected.sub_path, identity=expected)

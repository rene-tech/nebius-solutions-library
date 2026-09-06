"""An owner's private release directory must not restrict public image assets."""

from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path


def test_git_archive_preserves_readable_assets_under_private_release_umask(tmp_path: Path) -> None:
    path = Path(__file__).resolve().parents[1] / "scripts/build_image.py"
    spec = importlib.util.spec_from_file_location("fs2_build_image_permissions", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = tmp_path / "repo"
    repo.mkdir()
    module._run(["git", "init", "--quiet", str(repo)], cwd=tmp_path)
    catalog = repo / "catalog"
    catalog.mkdir()
    (catalog / "schema.json").write_text('{"type":"object"}\n')
    script = repo / "runtime.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    module._run(["git", "add", "."], cwd=repo)
    module._run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "fixture"],
        cwd=repo,
    )
    destination = tmp_path / "private-context"
    destination.mkdir(mode=0o700)
    previous = os.umask(0o077)
    try:
        module._archive(repo, "HEAD", destination)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert (destination / "catalog").stat().st_mode & 0o055 == 0o055
    assert (destination / "catalog/schema.json").stat().st_mode & 0o044 == 0o044
    assert (destination / "runtime.sh").stat().st_mode & 0o055 == 0o055

"""Actual-image acceptance for the shared scientific workspace identity.

The test is opt-in because the immutable/private model images are intentionally
not pulled by ordinary unit CI.  Release qualification sets the image names and
``FS2_RUN_ACTUAL_IMAGE_WORKSPACE_TEST=1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("FS2_RUN_ACTUAL_IMAGE_WORKSPACE_TEST") != "1"
    or shutil.which("docker") is None,
    reason="private immutable model images are not part of ordinary unit CI",
)

DOCKER = shutil.which("docker")


TOOLS_IMAGE = os.environ.get(
    "FS2_WORKSPACE_TEST_TOOLS_IMAGE", "fs2-serve-control-plane:git-003064c440c4"
)
IMAGE_CASES = (
    (
        os.environ.get(
            "FS2_WORKSPACE_TEST_BOLTZGEN_IMAGE",
            "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/boltzgen@sha256:"
            "9c3230424e02d725dc145b8f21a18f283910e1beba1f37466598ee832813820e",
        ),
        10001,
        "python",
    ),
    (
        os.environ.get("FS2_WORKSPACE_TEST_AF3_IMAGE", "fs2-cancer/alphafold3:3.0.4-85c4d205-r6"),
        1001,
        "/alphafold3_venv/bin/python3",
    ),
)


def _run(image: str, uid: int, python: str, workspace: Path, source: str) -> None:
    assert DOCKER is not None
    subprocess.run(  # noqa: S603 - release-controlled immutable images and argv
        [
            DOCKER,
            "run",
            "--rm",
            "--user",
            f"{uid}:{uid}",
            "--entrypoint",
            python,
            "--volume",
            f"{workspace}:/workspace",
            image,
            "-c",
            source,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(("model_image", "workspace_uid", "model_python"), IMAGE_CASES)
def test_actual_tools_and_model_images_exchange_0700_0400_workspace_files(
    tmp_path: Path, model_image: str, workspace_uid: int, model_python: str
) -> None:
    tmp_path.chmod(0o777)  # Kubernetes emptyDir is initially writable by the pod identity.
    _run(
        TOOLS_IMAGE,
        workspace_uid,
        "python3",
        tmp_path,
        "from pathlib import Path; p=Path('/workspace/stage'); p.mkdir(mode=0o700); "
        "(p/'prepared').write_text('prepared'); (p/'prepared').chmod(0o400)",
    )
    _run(
        model_image,
        workspace_uid,
        model_python,
        tmp_path,
        "from pathlib import Path; p=Path('/workspace/stage'); "
        "assert (p/'prepared').read_text() == 'prepared'; "
        "(p/'result').write_text('result'); (p/'result').chmod(0o400)",
    )
    _run(
        TOOLS_IMAGE,
        workspace_uid,
        "python3",
        tmp_path,
        "from pathlib import Path; p=Path('/workspace/stage'); "
        "assert (p/'prepared').read_text() == 'prepared'; assert (p/'result').read_text() == 'result'",
    )

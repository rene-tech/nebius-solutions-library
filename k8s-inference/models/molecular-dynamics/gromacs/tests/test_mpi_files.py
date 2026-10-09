import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from fs2_gromacs.mpi_files import InputStager, md_inputs, receive


def test_generated_tpr_restart_and_many_tables_are_staged_but_outputs_are_not(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for name in ("md.tpr", "state.cpt", "table_b0.xvg", "table_a0.xvg"):
        (data / name).write_bytes(b"input")
    paths = md_inputs(
        data,
        data,
        [
            "-s",
            "md.tpr",
            "-cpi",
            "state.cpt",
            "-tableb",
            "table_b0.xvg",
            "table_a0.xvg",
            "-deffnm",
            "result",
            "-o",
            "huge.trr",
        ],
    )
    assert [path.name for path in paths] == [
        "md.tpr",
        "state.cpt",
        "table_b0.xvg",
        "table_a0.xvg",
    ]


def test_receive_checks_content_and_keeps_previous_checkpoint_on_failed_transfer(
    tmp_path,
):
    content = b"native checkpoint"
    digest = hashlib.sha256(content).hexdigest()
    receive(tmp_path, "md/state.cpt", len(content), digest, io.BytesIO(content))
    assert (tmp_path / "md/state.cpt").read_bytes() == content
    for invalid in (b"short", b"x" * len(content), content + b"too much"):
        with pytest.raises(ValueError):
            receive(tmp_path, "md/state.cpt", len(content), digest, io.BytesIO(invalid))
        assert (tmp_path / "md/state.cpt").read_bytes() == content
    assert not list(tmp_path.rglob("*.mpi-transfer"))


def test_rank_inputs_cannot_escape_workspace(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "other.tpr").write_bytes(b"x")
    (tmp_path / "data/link.tpr").symlink_to(tmp_path / "other.tpr")
    for name in ("../other.tpr", "link.tpr", "missing.tpr"):
        with pytest.raises(ValueError):
            md_inputs(tmp_path / "data", tmp_path / "data", ["-s", name])


def test_only_changed_inputs_are_transferred_again(tmp_path, monkeypatch):
    monkeypatch.setenv("FS2_MPI_RANK", "0")
    monkeypatch.setenv("FS2_MPI_HOSTS", "first,second")
    data = tmp_path / "data"
    data.mkdir()
    (data / "md.tpr").write_bytes(b"tpr")
    (data / "restart.cpt").write_bytes(b"first checkpoint")
    sent = []

    def transport(argv, *, stdin, **kwargs):
        sent.append(stdin.read())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("fs2_gromacs.mpi_files.subprocess.run", transport)
    stager = InputStager(tmp_path)
    args = ["-s", "md.tpr", "-cpi", "restart.cpt"]
    assert stager.stage(data, args, timeout=10)["peers"][0]["files_transferred"] == 2
    assert stager.stage(data, args, timeout=10)["peers"][0]["files_transferred"] == 0
    (data / "restart.cpt").write_bytes(b"next checkpoint")
    assert stager.stage(data, args, timeout=10)["peers"][0]["files_transferred"] == 1
    assert sent == [b"tpr", b"first checkpoint", b"next checkpoint"]

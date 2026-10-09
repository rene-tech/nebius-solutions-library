"""Explicit empty-part selection is additive for single-GPU and MPI workflows."""

import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from fs2_gromacs import MPI_PARAMETER_SCHEMA, PARAMETER_SCHEMA
from fs2_gromacs.contracts import normalize, request_schema
from fs2_gromacs.worker import expand_args


def request(token, *, mpi=False):
    return {
        "schema": MPI_PARAMETER_SCHEMA if mpi else PARAMETER_SCHEMA,
        **({"nodes": 1} if mpi else {}),
        "jobs": [{"id": "gang" if mpi else "analysis", "steps": [{
            "id": "join", "command": "trjcat", "args": ["-f", token, "-o", "joined.xtc"],
        }]}],
    }


@pytest.mark.parametrize("mpi", [False, True])
@pytest.mark.parametrize("option", [None, False, True])
def test_both_contracts_preserve_exact_old_and_new_selectors(mpi, option):
    token = {"files": "md.part*.xtc"}
    if option is not None:
        token["nonempty"] = option
    value = normalize(request(token, mpi=mpi), mpi=mpi)
    assert value["jobs"][0]["steps"][0]["args"][1] == token
    if option is None:
        assert "nonempty" not in value["jobs"][0]["steps"][0]["args"][1]


@pytest.mark.parametrize("mpi", [False, True])
@pytest.mark.parametrize("option", [0, 1, "true", "false", None, [], {}])
def test_nonempty_is_a_boolean_not_a_truthy_value(mpi, option):
    with pytest.raises(ValidationError):
        normalize(request({"files": "md.part*.xtc", "nonempty": option}, mpi=mpi), mpi=mpi)


@pytest.mark.parametrize("option", [None, False, True])
def test_filter_is_explicit_ordered_and_preserves_every_original(tmp_path, option):
    contents = {"md.part0003.xtc": b"third", "md.part0001.xtc": b"", "md.part0002.xtc": b"second"}
    for name, data in contents.items():
        (tmp_path / name).write_bytes(data)
    token = {"files": "md.part*.xtc"}
    if option is not None:
        token["nonempty"] = option
    names = sorted(name for name, data in contents.items() if data or option is not True)
    assert expand_args(["-f", token, "-o", "joined.xtc"], tmp_path) == ["-f", *names, "-o", "joined.xtc"]
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == contents


@pytest.mark.parametrize("matches", [False, True])
def test_no_eligible_input_fails_instead_of_running_an_empty_native_command(tmp_path, matches):
    if matches:
        (tmp_path / "md.part0001.xtc").touch()
    with pytest.raises(ValueError, match="no nonempty files" if matches else "no files"):
        expand_args([{"files": "md.part*.xtc", "nonempty": True}], tmp_path)


def test_legacy_all_empty_expansion_is_not_silently_changed(tmp_path):
    (tmp_path / "md.part0001.xtc").touch()
    assert expand_args([{"files": "md.part*.xtc"}], tmp_path) == ["md.part0001.xtc"]
    assert expand_args([{"files": "md.part*.xtc", "nonempty": False}], tmp_path) == ["md.part0001.xtc"]


@pytest.mark.parametrize("kind", ["symlink", "directory", "outside"])
def test_empty_filter_does_not_hide_invalid_file_matches(tmp_path, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "md.part0001.xtc").write_bytes(b"frame")
    invalid = workspace / "md.part0002.xtc"
    if kind == "directory":
        invalid.mkdir()
    elif kind == "symlink":
        invalid.symlink_to(workspace / "md.part0001.xtc")
    else:
        outside = tmp_path / "outside.xtc"
        outside.touch()
        invalid.symlink_to(outside)
    with pytest.raises(ValueError, match="contained regular files"):
        expand_args([{"files": "md.part*.xtc", "nonempty": True}], workspace)


@pytest.mark.parametrize("mpi", [False, True])
def test_all_public_schema_projections_match_canonical_contract(mpi):
    root = Path(__file__).resolve().parents[4]
    stem = "gromacs-mpi-workflow" if mpi else "gromacs-workflow"
    for path in (root / "catalog/runtime/schema" / f"{stem}-request.schema.json",
                 root / "components/control-plane/src/fs2_serve/model_input_schemas" / f"{stem}.json"):
        assert json.loads(path.read_text()) == request_schema(mpi=mpi)

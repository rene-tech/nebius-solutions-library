import pytest
from jsonschema.exceptions import ValidationError

from recipes import parameters


def test_default_preserves_science_and_bounded_repeats():
    value = parameters()
    commands = value["jobs"][0]["steps"]
    assert commands[0]["args"] == ["-s", "original.tpr", "-o", "benchmark.tpr", "-nsteps", "50000"]
    runs = [c for c in commands if c["command"] == "mdrun"]
    assert len(runs) == 3
    assert all("-resethway" not in c["args"] for c in runs)
    assert value["threads"] == 8
    assert value["segment_minutes"] == 60


@pytest.mark.parametrize("nodes,gpus", [(1, 1), (1, 2), (1, 4), (1, 8), (2, 8)])
def test_supported_mpi_shapes(nodes, gpus):
    value = parameters(mpi=True, nodes=nodes, gpus_per_node=gpus, pme="gpu", update="cpu")
    run = value["jobs"][0]["steps"][1]["args"]
    assert value["jobs"][0]["id"] == "gang"
    assert run[run.index("-npme") + 1] == ("0" if nodes * gpus == 1 else "1")
    assert "-notunepme" in run
    assert "-ntomp" not in run


@pytest.mark.parametrize("changes", [{"steps": -1}, {"steps": 500000000}, {"repetitions": 4},
                                      {"mpi": True, "nodes": 3, "gpus_per_node": 8, "update": "cpu"},
                                      {"gpus_per_node": 2}, {"mpi": True, "gpus_per_node": 2},
                                      {"threads": 16}, {"nstlist": 500}])
def test_out_of_scope_rejected(changes):
    with pytest.raises((ValueError, ValidationError)):
        parameters(**changes)

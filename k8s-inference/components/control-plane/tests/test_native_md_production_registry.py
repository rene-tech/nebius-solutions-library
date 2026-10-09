"""Exercise the production dispatch path, not only adapter-local collectors."""

import pytest

from fs2_serve.scientific_batch.adapters import (
    _COLLECTORS, _COMPILERS, amber, collect_stage_output, gromacs, gromacs_mpi, lammps, namd,
)
from fs2_serve.scientific_batch.adapters.production_registry import install_production_adapters
from test_native_md_adapters import completed


@pytest.mark.parametrize('engine', [amber, gromacs, gromacs_mpi, lammps, namd])
def test_native_compiler_and_collector_are_both_registered(engine):
    install_production_adapters()
    assert engine.MODEL_ID in _COMPILERS
    assert _COLLECTORS[engine.COLLECTOR_ID] == engine.collect_companion_output


@pytest.mark.parametrize('engine', [amber, lammps, namd])
def test_published_native_files_reach_production_collector(engine, tmp_path):
    install_production_adapters()
    invocation, _ = completed(engine, tmp_path)
    output = collect_stage_output(invocation, tmp_path)
    assert output.validation['status'] == 'passed'
    assert [entry.name for entry in output.artifacts] == ['result', 'file-00000']

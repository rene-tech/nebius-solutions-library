from pathlib import Path

import pytest

from fs2_gromacs.native_architecture import validate_cubins


def listing(architectures):
    return "\n".join(
        f"ELF file {index}: libgromacs.so.{index}.sm_{arch}.cubin"
        for index, arch in enumerate(architectures, 1)
    )


def test_single_gpu_recipe_preserves_existing_targets_and_adds_ada():
    recipe = (Path(__file__).parents[1] / "runtime/Containerfile.native").read_text()
    assert "ARG CUDA_ARCHITECTURES=75;80;86;89;90" in recipe
    assert '-DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCHITECTURES}"' in recipe
    assert "-DGMX_THREAD_MPI=ON" in recipe
    assert "-DGMX_USE_COLVARS=internal -DGMX_USE_PLUMED=ON" in recipe


def test_native_architecture_audit_requires_actual_cubins_not_ptx_or_flags():
    with pytest.raises(ValueError, match="89"):
        validate_cubins(listing([75, 80, 86, 90]) + "\ncompute_89.ptx sm_89")
    assert validate_cubins(listing([75, 80, 86, 89, 90]))["89"] == 1


def test_native_architecture_audit_rejects_partial_architecture_coverage():
    with pytest.raises(ValueError, match="different CUDA cubin counts"):
        validate_cubins(listing([75, 80, 86, 89, 90, 75, 80, 86, 90]))

"""Distributed GROMACS variant sharing the single-GPU workflow/data contract."""

from fs2_gromacs import MPI_ENGINE, MPI_PARAMETER_SCHEMA

from . import gromacs

MODEL_ID = "gromacs-mpi"
VARIANT_ID = "upstream-2026-2-mpi-v1"
SOURCE_REPOSITORY = "gromacs/gromacs"
SOURCE_REVISION = MPI_ENGINE.split("@")[1]
PARAMETER_SCHEMA = MPI_PARAMETER_SCHEMA
COLLECTOR_ID = VALIDATOR_ID = "gromacs-mpi-workflow-v1"
REQUIRES_VERIFIED_INPUT_ARTIFACTS = True
public_input_contract = gromacs.public_input_contract


def compile_run(profile, request_value, *, operation_id, input_artifacts=None):
    return gromacs.compile_run(
        profile, request_value, operation_id=operation_id, input_artifacts=input_artifacts, mpi=True
    )


def collect_companion_output(invocation, workspace):
    return gromacs.collect_companion_output(invocation, workspace, mpi=True)

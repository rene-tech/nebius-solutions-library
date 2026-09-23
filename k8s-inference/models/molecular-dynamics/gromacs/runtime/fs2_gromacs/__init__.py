"""NVIDIA GROMACS native workflow runtime for Scientific AI."""

MODEL_ID = "gromacs"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/gromacs-workflow-request/v1"
RESULT_SCHEMA = "fs2-serve.nebius.ai/gromacs-workflow-result/v1"
NVIDIA_IMAGE = "nvcr.io/nvidia/gromacs@sha256:0e52e3ae971453898956379952b9ea606f5400cbdb4d439773ecbae4d8f5ad59"
MPI_ENGINE = "gromacs/2026.2@da9e013175bae98b31b34384f6b4864ff29f65a5"
MPI_PARAMETER_SCHEMA = "fs2-serve.nebius.ai/gromacs-mpi-workflow-request/v1"

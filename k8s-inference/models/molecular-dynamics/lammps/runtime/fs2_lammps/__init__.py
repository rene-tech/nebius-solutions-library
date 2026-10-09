"""Native NVIDIA LAMMPS workflow runtime for Scientific AI."""

MODEL_ID = "lammps"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/lammps-workflow-request/v1"
RESULT_SCHEMA = "fs2-serve.nebius.ai/lammps-workflow-result/v1"
ENGINE_ID = "nvcr.io/nvidia/lammps@sha256:d8a0076dfe84fcbc98db05531993c1cd9deb964050b9c122b92655dc3685d731"
NVIDIA_IMAGE = ENGINE_ID

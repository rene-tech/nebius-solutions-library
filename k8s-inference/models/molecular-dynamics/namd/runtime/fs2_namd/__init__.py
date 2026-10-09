"""Pinned NVIDIA NAMD native workflow contract."""
MODEL_ID = "namd"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/namd-workflow-request/v1"
RESULT_SCHEMA = "fs2-serve.nebius.ai/namd-workflow-result/v1"
ENGINE_ID = "nvcr.io/nvidia/namd@sha256:e1ebab672b968e0b287ba91c3dc19cdad9b693e8b59e74d7782cb753d6a7460f"
NVIDIA_IMAGE = ENGINE_ID

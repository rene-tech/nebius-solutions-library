"""Reuse the existing project mirror flow for the selected NVIDIA NAMD digest."""
import importlib.util
from pathlib import Path

SOURCE = "nvcr.io/nvidia/namd@sha256:e1ebab672b968e0b287ba91c3dc19cdad9b693e8b59e74d7782cb753d6a7460f"
TARGET = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/namd-ngc:3.0.2-r20260923"


if __name__ == "__main__":
    path = Path(__file__).resolve().parents[2] / "gromacs/qualification/mirror_runtime.py"
    spec = importlib.util.spec_from_file_location("gromacs_mirror", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.NAME = "fs2-namd-r20260923-mirror"
    module.SOURCE, module.TARGET = SOURCE, TARGET
    module.main()

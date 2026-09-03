#!/usr/bin/env python3
"""Weight-free import and CUDA ABI probe for scientific runtime images."""

from __future__ import annotations

import argparse
import ctypes
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
from typing import Any


CONFIG = {
    "proteina-complexa": {
        "imports": ["proteinfoundation", "proteinfoundation.cli.cli_runner", "atomworks", "colabdesign"],
        "package": "proteinfoundation",
        "framework": "torch",
        "framework_version": "2.7.0+cu126",
        "cuda_abi": "12.6",
    },
    "boltzgen": {
        "imports": ["boltzgen", "boltzgen.cli.boltzgen", "cuequivariance_torch"],
        "package": "boltzgen",
        "framework": "torch",
        "framework_version": "2.7.1+cu126",
        "cuda_abi": "12.6",
        "package_versions": {
            "cuequivariance": "0.10.0",
            "cuequivariance-torch": "0.10.0",
            "cuequivariance-ops-cu12": "0.10.0",
            "cuequivariance-ops-torch-cu12": "0.10.0",
            "triton": "3.3.1",
        },
    },
}
WEIGHT_SUFFIXES = {".ckpt", ".onnx", ".pt", ".pth", ".safetensors"}


def _cuda_runtime_version() -> str | None:
    try:
        runtime = ctypes.CDLL("libcudart.so.12")
        version = ctypes.c_int()
        if runtime.cudaRuntimeGetVersion(ctypes.byref(version)) != 0:
            return None
        value = version.value
        return f"{value // 1000}.{(value % 1000) // 10}"
    except OSError:
        return None


def _package_roots(name: str) -> list[Path]:
    spec = importlib.util.find_spec(name)
    if spec is None:
        raise RuntimeError(f"package {name!r} has no import spec")
    roots: set[Path] = {Path("/opt/fs2/source")}
    if spec.submodule_search_locations:
        roots.update(Path(item) for item in spec.submodule_search_locations)
    elif spec.origin:
        roots.add(Path(spec.origin).parent)
    return sorted(roots)


def _embedded_weights(roots: list[Path]) -> list[str]:
    found: set[str] = set()
    artifact_root = Path(os.environ.get("FS2_ARTIFACT_ROOT", "/opt/fs2/artifacts")).resolve()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in WEIGHT_SUFFIXES:
                continue
            resolved = path.resolve()
            if artifact_root == resolved or artifact_root in resolved.parents:
                continue
            found.add(str(resolved))
    return sorted(found)


def _torch_probe(require_gpu: bool, expected: dict[str, str]) -> dict[str, Any]:
    import torch

    if torch.__version__ != expected["framework_version"]:
        raise RuntimeError(f"unexpected torch version {torch.__version__}")
    if torch.version.cuda != expected["cuda_abi"]:
        raise RuntimeError(f"unexpected torch CUDA ABI {torch.version.cuda}")
    result: dict[str, Any] = {
        "version": torch.__version__,
        "cuda_build": torch.version.cuda,
        "cuda_available": torch.cuda.is_available(),
    }
    if require_gpu:
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA GPU is required but PyTorch cannot discover one")
        tensor = torch.arange(8, device="cuda", dtype=torch.float32).square()
        torch.cuda.synchronize()
        result.update(
            {
                "device_name": torch.cuda.get_device_name(0),
                "device_capability": list(torch.cuda.get_device_capability(0)),
                "allocation_checksum": float(tensor.sum().item()),
            }
        )
    return result


def _jax_probe(require_gpu: bool, expected: dict[str, str]) -> dict[str, Any]:
    import jax
    import jax.numpy as jnp

    if jax.__version__ != expected["framework_version"]:
        raise RuntimeError(f"unexpected JAX version {jax.__version__}")
    result: dict[str, Any] = {"version": jax.__version__}
    if require_gpu:
        devices = jax.devices("gpu")
        if not devices:
            raise RuntimeError("CUDA GPU is required but JAX cannot discover one")
        value = jnp.square(jnp.arange(8, dtype=jnp.float32)).sum()
        value.block_until_ready()
        result.update(
            {
                "backend": jax.default_backend(),
                "devices": [str(device) for device in devices],
                "allocation_checksum": float(value),
            }
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=sorted(CONFIG), required=True)
    parser.add_argument("--require-gpu", action="store_true")
    args = parser.parse_args()
    config = CONFIG[args.model]

    imported = []
    for module in config["imports"]:
        importlib.import_module(module)
        imported.append(module)

    package_versions = {}
    for package, expected_version in config.get("package_versions", {}).items():
        observed_version = importlib.metadata.version(package)
        if observed_version != expected_version:
            raise RuntimeError(
                f"unexpected {package} version {observed_version}; expected {expected_version}"
            )
        package_versions[package] = observed_version

    roots = _package_roots(config["package"])
    embedded = _embedded_weights(roots)
    if embedded:
        raise RuntimeError("default L1 image embeds model weights: " + ", ".join(embedded))

    framework = (
        _torch_probe(args.require_gpu, config)
        if config["framework"] == "torch"
        else _jax_probe(args.require_gpu, config)
    )
    evidence = {
        "schema": "fs2.nebius.ai/scientific-runtime-image-smoke/v1",
        "model": args.model,
        "imports": imported,
        "package_versions": package_versions,
        "framework": framework,
        "container_cuda_runtime": _cuda_runtime_version(),
        "expected_cuda_abi": config["cuda_abi"],
        "gpu_required": args.require_gpu,
        "embedded_weights": embedded,
        "artifact_root": os.environ.get("FS2_ARTIFACT_ROOT", "/opt/fs2/artifacts"),
    }
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()

"""Exact Evo2-40B model-parallel H100 backend using the retained upstream stack.

The source Evo2/Vortex loader already shards complete layers over visible GPUs.
This adapter preserves that loader, checkpoint, precision and generation API;
it replaces only the earlier benchmark wrapper's single-B300 hardware fence.
"""
from __future__ import annotations

import importlib.metadata
import functools
import time
from pathlib import Path

from evo2_deep.runtime import Evo2Backend, RuntimeFailure, _driver_version, _tool_identity


def validate_devices(devices):
    if len(devices) != 2:
        raise RuntimeFailure("Evo2 H100 profile requires exactly two visible GPUs")
    for device in devices:
        if tuple(device["capability"]) != (9, 0) or "H100" not in device["name"].upper():
            raise RuntimeFailure("Evo2 H100 profile requires two NVIDIA H100 SM90 GPUs")


def guard_block_device(torch, block, device):
    """Launch each block's native kernels on its actual model-parallel device.

    Tensor transfers alone do not change CUDA's current device. In particular,
    Triton's launcher reads that current device and stream. Guard the complete
    unchanged block, not only its Transformer Engine projection.
    """
    original = block.forward

    @functools.wraps(original)
    def forward(*args, **kwargs):
        with torch.cuda.device(device):
            return original(*args, **kwargs)

    block.forward = forward


class H100Backend(Evo2Backend):
    def __init__(self, model_path: Path, *, use_kernels: bool = True):
        if not model_path.is_absolute() or model_path.is_symlink() or not model_path.is_file():
            raise RuntimeFailure("model path must be an absolute regular non-symlink file")
        self._ready = False
        import torch
        import transformer_engine
        import triton
        from evo2 import Evo2
        from vortex.model.generation import Generator, prepare_batch

        devices = [{"index": i, "name": torch.cuda.get_device_name(i),
                    "capability": list(torch.cuda.get_device_capability(i))}
                   for i in range(torch.cuda.device_count())]
        validate_devices(devices)
        target = triton.runtime.driver.active.get_current_target()
        if str(target.backend).lower() != "cuda" or str(target.arch).lower() not in {"90", "90a", "sm90", "sm_90", "sm_90a"}:
            raise RuntimeFailure("active Triton target is not CUDA SM90")
        started = time.perf_counter_ns()
        self._model_wrapper = Evo2("evo2_40b", local_path=str(model_path), use_kernels=use_kernels)
        for block_idx, block in enumerate(self._model_wrapper.model.blocks):
            guard_block_device(torch, block, self._model_wrapper.model.block_idx_to_device[block_idx])
        for i in range(2):
            torch.cuda.synchronize(i)
        self._torch, self._Generator, self._prepare_batch = torch, Generator, prepare_batch
        placement = {}
        for parameter in self._model_wrapper.model.parameters():
            key = str(parameter.device)
            placement[key] = placement.get(key, 0) + parameter.numel() * parameter.element_size()
        if set(placement) != {"cuda:0", "cuda:1"}:
            raise RuntimeFailure(f"unexpected parameter placement: {sorted(placement)}")
        self._identity = {
            "model": "evo2-40b", "visible_gpu_count": 2, "gpu_name": devices[0]["name"],
            "devices": devices, "compute_capability": "9.0", "cuda": str(torch.version.cuda),
            "cuda_driver": _driver_version(), "cudnn": str(torch.backends.cudnn.version()),
            "torch": str(torch.__version__), "torch_arch_list": torch.cuda.get_arch_list(),
            "triton": str(triton.__version__), "triton_target": {"backend": str(target.backend), "arch": str(target.arch)},
            "transformer_engine": str(transformer_engine.__version__),
            "flash_attention": importlib.metadata.version("flash-attn"),
            "cuda_toolchain": {"nvcc": _tool_identity("nvcc"), "ptxas": _tool_identity("ptxas")},
            "kernel_mode": "hcs-hcm-hcl" if use_kernels else "reference",
            "model_load_ms": (time.perf_counter_ns()-started)/1e6,
            "model_path_bytes": model_path.stat().st_size,
            "parameter_bytes_by_device": placement,
            "parallelism": "upstream-vortex-layer-model-parallel",
            "per_block_cuda_device_guard": True,
            "native_precision": "bf16-with-transformer-engine-fp8-input-projections",
        }
        self._ready = True

    def generate(self, request):
        # The retained Generator's callbacks synchronize the output device;
        # also fence both visible devices around each complete request.
        for i in range(2):
            self._torch.cuda.synchronize(i)
        result = super().generate(request)
        for i in range(2):
            self._torch.cuda.synchronize(i)
        return result

#!/usr/bin/env python3
"""Shared non-secret startup gate and import smoke for scientific runtimes."""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import json
import os
import sys
from pathlib import Path

from artifact_gate import ArtifactGateError, verify_from_environment


RUNTIME_IMPORTS = {
    "bindcraft-academic": ("torch", "jax", "jaxlib", "colabdesign", "Bio"),
    "freebindcraft-open-fallback": ("torch", "jax", "jaxlib", "colabdesign", "openmm", "pdbfixer"),
    "rfdiffusion": ("torch", "dgl", "se3_transformer", "rfdiffusion.RoseTTAFoldModel"),
    "proteinmpnn": ("torch", "protein_mpnn_utils"),
}
PYROSETTA_SITE_PACKAGES = Path("/opt/fs2/academic/pyrosetta-bindcraft/site-packages")


def _runtime() -> str:
    name = os.environ.get("FS2_RUNTIME_NAME", "")
    if name not in RUNTIME_IMPORTS:
        raise ArtifactGateError("runtime identity is unsupported")
    return name


def _assert_no_embedded_artifacts(runtime: str) -> None:
    roots = {
        "bindcraft-academic": Path("/opt/bindcraft"),
        "freebindcraft-open-fallback": Path("/opt/freebindcraft"),
        "rfdiffusion": Path("/opt/rfdiffusion"),
        "proteinmpnn": Path("/opt/proteinmpnn"),
    }
    forbidden_suffixes = {".ckpt", ".pkl", ".pth", ".pt"}
    forbidden_dirs = {"params", "models", "vanilla_model_weights", "soluble_model_weights", "ca_model_weights"}
    root = roots[runtime]
    scan_roots = [root]
    if runtime in {"bindcraft-academic", "freebindcraft-open-fallback"}:
        scan_roots.append(Path(importlib.import_module("colabdesign").__file__).resolve().parent)
    for scan_root in scan_roots:
        for path in scan_root.rglob("*"):
            if path.name in forbidden_dirs or (path.is_file() and path.suffix.lower() in forbidden_suffixes):
                raise ArtifactGateError("runtime image unexpectedly contains a model artifact")
    if importlib.util.find_spec("pyrosetta") is not None:
        raise ArtifactGateError("runtime image unexpectedly contains PyRosetta")


def image_smoke() -> None:
    runtime = _runtime()
    _assert_no_embedded_artifacts(runtime)
    # Avoid JAX's default large device-memory reservation during a shared-H100
    # canary. The smoke needs one real kernel, not ownership of most of a GPU.
    os.environ.setdefault("XLA_PYTHON_CLIENT_PREALLOCATE", "false")
    versions: dict[str, str] = {}
    torch = importlib.import_module("torch")
    cuda_available = bool(torch.cuda.is_available())
    module_names = RUNTIME_IMPORTS[runtime]
    deferred_imports: tuple[str, ...] = ()
    if runtime == "rfdiffusion" and not cuda_available:
        # The CUDA DGL wheel loads libcuda while importing GraphBolt. A
        # driverless builder can still prove the exact installed distributions;
        # the full DGL/SE(3)/RFdiffusion import is mandatory on the H100 canary.
        module_names = ("torch", "rfdiffusion.chemical")
        deferred_imports = ("dgl", "se3_transformer", "rfdiffusion.RoseTTAFoldModel")
        versions["dgl-distribution"] = importlib.metadata.version("dgl")
        versions["se3-transformer-distribution"] = importlib.metadata.version("se3-transformer")
    for module_name in module_names:
        module = importlib.import_module(module_name)
        if module_name == "jaxlib":
            versions[module_name] = importlib.metadata.version("jaxlib")
        else:
            versions[module_name] = str(getattr(module, "__version__", "imported"))
    cuda = str(torch.version.cuda)
    if not cuda.startswith("12."):
        raise ArtifactGateError("runtime was not built against modern CUDA 12")
    if runtime in {"bindcraft-academic", "freebindcraft-open-fallback"}:
        if "+cuda12.cudnn89" not in versions.get("jaxlib", ""):
            raise ArtifactGateError("BindCraft JAX runtime is not the locked CUDA 12/cuDNN 8.9 build")
    gpu_evidence: dict[str, object] = {}
    if cuda_available:
        value = torch.ones(1, device="cuda").add_(1).cpu().item()
        if value != 2:
            raise ArtifactGateError("CUDA kernel smoke returned an invalid value")
        gpu_evidence = {
            "device_name": torch.cuda.get_device_name(0),
            "compute_capability": list(torch.cuda.get_device_capability(0)),
            "torch_kernel_result": value,
        }
        if runtime in {"bindcraft-academic", "freebindcraft-open-fallback"}:
            jax = importlib.import_module("jax")
            jnp = importlib.import_module("jax.numpy")
            devices = jax.devices("gpu")
            if not devices or int(jnp.add(jnp.asarray(1), 1)) != 2:
                raise ArtifactGateError("JAX CUDA kernel smoke failed")
            gpu_evidence["jax_device"] = str(devices[0])
    print(
        json.dumps(
            {
                "schema": "fs2.nebius.ai/runtime-image-smoke/v1",
                "runtime": runtime,
                "source_revision": os.environ.get("FS2_SOURCE_REVISION"),
                "cuda_build": cuda,
                "cuda_available": cuda_available,
                "gpu_evidence": gpu_evidence,
                "imports": versions,
                "gpu_required_imports_deferred": list(deferred_imports),
                "embedded_model_artifacts": false_value(),
                "embedded_pyrosetta": false_value(),
            },
            sort_keys=True,
        )
    )


def false_value() -> bool:
    """Keep smoke JSON fields explicit and easy for contract tests to locate."""

    return False


def _bind_preinstalled_pyrosetta() -> None:
    if _runtime() != "bindcraft-academic":
        return
    target = PYROSETTA_SITE_PACKAGES
    if not target.is_dir() or target.is_symlink() or not os.access(target, os.R_OK | os.X_OK):
        raise ArtifactGateError("tenant-private preinstalled PyRosetta tree is required and must be readable")
    canonical = str(target)
    configured = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    if not configured or configured[0] != canonical:
        raise ArtifactGateError("PYTHONPATH must begin with the canonical tenant-private PyRosetta tree")
    if not sys.path or sys.path[0] != canonical:
        sys.path.insert(0, canonical)
    importlib.invalidate_caches()
    try:
        module = importlib.import_module("pyrosetta")
    except Exception as exc:
        raise ArtifactGateError("tenant-private preinstalled PyRosetta import failed") from exc
    origin = Path(str(module.__file__)).resolve()
    if target.resolve() not in origin.parents:
        raise ArtifactGateError("PyRosetta resolved outside the tenant-private mounted tree")


def main() -> None:
    if sys.argv[1:] == ["--fs2-image-smoke"]:
        image_smoke()
        # Older CUDA-enabled JAX releases can block in interpreter teardown
        # after all work and output have completed. This path is a dedicated
        # one-shot image canary, so flush the receipt and bypass only Python's
        # shutdown hooks. Normal runtime commands still use os.execvp below.
        sys.stdout.flush()
        os._exit(0)
    if len(sys.argv) < 2:
        raise ArtifactGateError("runtime command is required")
    artifact_receipt = verify_from_environment()
    _bind_preinstalled_pyrosetta()
    print(
        json.dumps(
            {
                "event": "external_artifacts_admitted",
                "runtime": _runtime(),
                **artifact_receipt,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    try:
        main()
    except ArtifactGateError as exc:
        print(json.dumps({"event": "runtime_gate_rejected", "reason": str(exc)}), file=sys.stderr)
        raise SystemExit(78) from None

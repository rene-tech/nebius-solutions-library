#!/usr/bin/env python3
"""Build qualification or exact-artifact H100 semantic image smoke."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from result_contract import sha256_file


SOURCE_REVISION_FILE = Path("/opt/fs2/source-revision")
EXTERNAL_ROOTS = (Path("/models"), Path("/databases"))


def _run_cli(
    command: list[str], expected_text: str, accepted_codes: set[int] = {0}
) -> None:
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=90,
    )
    if completed.returncode not in accepted_codes:
        raise RuntimeError(
            f"CLI smoke failed with {completed.returncode}: {completed.stdout[-1200:]}"
        )
    if expected_text.lower() not in completed.stdout.lower():
        raise RuntimeError(f"CLI help did not contain {expected_text!r}")


def _assert_external_roots_are_empty() -> dict[str, list[str]]:
    contents: dict[str, list[str]] = {}
    for root in EXTERNAL_ROOTS:
        if not root.exists():
            continue
        paths = sorted(
            str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
        )
        if paths:
            raise RuntimeError(
                f"runtime image contains forbidden artifacts below {root}: {paths[:8]}"
            )
        contents[str(root)] = paths
    return contents


def _torch_build_smoke() -> dict[str, Any]:
    import torch

    return {
        "framework": "torch",
        "version": torch.__version__,
        "cuda_runtime": torch.version.cuda,
        "cuda_available": torch.cuda.is_available(),
    }


def _jax_build_smoke() -> dict[str, Any]:
    import jax

    return {
        "framework": "jax",
        "version": jax.__version__,
        "default_backend": jax.default_backend(),
        "devices": [str(device) for device in jax.devices()],
    }


def _build_smoke(runtime_id: str) -> dict[str, Any]:
    if runtime_id in {"esmfold2", "esmfold2-fast"}:
        from esm.models.esmfold2 import ESMFold2InputBuilder, EsmFold2Model

        if not ESMFold2InputBuilder or not EsmFold2Model:
            raise RuntimeError("ESMFold2 public inference API is incomplete")
        result = _torch_build_smoke()
        result["package_version"] = importlib.metadata.version("esm")
        result["api"] = "esm.models.esmfold2"
        _run_cli(["/usr/local/bin/fs2-run-esmfold2", "--help"], "prepare-input")
        result["cli"] = "fs2-run-esmfold2 prepare-input|fold"
        return result

    if runtime_id == "protenix-v2":
        expected_prefix = "/opt/protenix-venv/"
        resolutions = {}
        package_spec = importlib.util.find_spec("protenix")
        if package_spec is None or package_spec.submodule_search_locations is None:
            raise RuntimeError("installed Protenix package is missing")
        for module in ("protenix", "runner", "configs"):
            spec = importlib.util.find_spec(module)
            origin = spec.origin if spec else None
            if origin is None or not origin.startswith(expected_prefix):
                raise RuntimeError(f"{module} resolved outside installed runtime: {origin}")
            resolutions[module] = origin
        package_root = Path(next(iter(package_spec.submodule_search_locations)))
        layer_norm = package_root / "model/layer_norm"
        extension = layer_norm / "fast_layer_norm_cuda_v2.so"
        if not extension.is_file():
            raise RuntimeError("prebuilt Protenix layer norm extension is missing")
        if (layer_norm / "torch_ext_compile.py").exists() or (layer_norm / "kernel").exists():
            raise RuntimeError("runtime Protenix package still contains the fast-layernorm build path")
        if shutil.which("nvcc"):
            raise RuntimeError("Protenix runtime unexpectedly contains nvcc")
        triton_cache = Path(os.environ.get("TRITON_CACHE_DIR", ""))
        cueq_cache = Path(os.environ.get("CUEQ_TRITON_CACHE_DIR", ""))
        for cache in (triton_cache, cueq_cache):
            if not cache.is_absolute() or not cache.is_dir() or not os.access(cache, os.W_OK):
                raise RuntimeError(f"Protenix runtime cache is not writable: {cache}")
        _run_cli(["/opt/protenix-venv/bin/protenix", "--help"], "pred")
        result = _torch_build_smoke()
        result["package_version"] = importlib.metadata.version("protenix")
        result["cli"] = "protenix prep|pred"
        result["module_resolutions"] = resolutions
        result["prebuilt_extension"] = str(extension)
        result["runtime_compilation"] = {
            "fast_layernorm": "prebuilt-sm90-cubin-plus-compute90-ptx",
            "cuequivariance": "active-triton-jit-first-shape-then-cache",
            "triton_cache_dir": str(triton_cache),
            "cueq_triton_cache_dir": str(cueq_cache),
            "h100_first_call_vs_warm_call": "pending-semantic-measurement",
        }
        return result

    if runtime_id == "alphafold3":
        import alphafold3
        import alphafold3.cpp
        from alphafold3.model import model

        if not alphafold3 or not alphafold3.cpp or not model:
            raise RuntimeError("AlphaFold3 compiled/model API is incomplete")
        _run_cli(
            [sys.executable, "/opt/alphafold3/run_alphafold.py", "--helpshort"],
            "model_dir",
            {0, 1},
        )
        result = _jax_build_smoke()
        result["package_version"] = importlib.metadata.version("alphafold3")
        if result["package_version"] != "3.0.4":
            raise RuntimeError(
                f"AlphaFold3 package identity is not v3.0.4: {result['package_version']}"
            )
        _run_cli(["/usr/local/bin/fs2-run-alphafold3", "--help"], "inference")
        result["cli"] = "fs2-run-alphafold3 data|inference"
        return result

    if runtime_id == "openfold3":
        import openfold3
        from biotite.structure.info import ccd
        from openfold3.projects.of3_all_atom import model

        if not openfold3 or not model or not callable(ccd.set_ccd_path):
            raise RuntimeError("OpenFold3 model or public Biotite CCD API is incomplete")
        _run_cli(["run_openfold", "predict", "--help"], "inference-ckpt-path")
        result = _torch_build_smoke()
        result["package_version"] = importlib.metadata.version("openfold3")
        if result["package_version"] != "0.5.0":
            raise RuntimeError(
                f"OpenFold3 package identity is not v0.5.0: {result['package_version']}"
            )
        _run_cli(["/usr/local/bin/fs2-run-openfold3", "--help"], "checkpoint")
        _run_cli([sys.executable, "/opt/fs2/prepare_openfold3.py", "--help"], "msa-mode")
        result["cli"] = "/opt/fs2/prepare_openfold3.py then run_openfold predict"
        result["ccd_api"] = "biotite.structure.info.ccd.set_ccd_path"
        return result

    raise RuntimeError(f"unsupported FS2_RUNTIME_ID: {runtime_id!r}")


def _assert_h100(runtime_id: str) -> dict[str, object]:
    if runtime_id == "alphafold3":
        import jax

        devices = [device for device in jax.devices() if device.platform == "gpu"]
        if not devices or "H100" not in devices[0].device_kind:
            raise RuntimeError(
                f"exact H100 semantic smoke requires H100; JAX devices={jax.devices()}"
            )
        return {"device_name": devices[0].device_kind, "compute_capability": [9, 0]}

    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("exact H100 semantic smoke requires CUDA")
    capability = tuple(torch.cuda.get_device_capability(0))
    if capability != (9, 0):
        raise RuntimeError(f"exact H100 semantic smoke requires SM90, got {capability}")
    return {
        "device_name": torch.cuda.get_device_name(0),
        "compute_capability": list(capability),
    }


def _semantic_smoke(
    runtime_id: str,
    request: Path,
    output_dir: Path,
    *,
    seeds: str,
    msa_mode: str,
    runner_yaml: Path | None,
    prepared_marker: Path | None,
) -> dict[str, object]:
    commands = {
        "esmfold2": [
            "/usr/local/bin/fs2-run-esmfold2",
            "fold",
            "--input",
            str(request),
            "--output-dir",
            str(output_dir),
            "--variant",
            "esmfold2",
            "--smoke",
        ],
        "esmfold2-fast": [
            "/usr/local/bin/fs2-run-esmfold2",
            "fold",
            "--input",
            str(request),
            "--output-dir",
            str(output_dir),
            "--variant",
            "esmfold2-fast",
            "--smoke",
        ],
        "protenix-v2": [
            "/usr/local/bin/fs2-run-protenix",
            "pred",
            "--input",
            str(request),
            "--output-dir",
            str(output_dir),
            "--msa-mode",
            msa_mode,
            "--seeds",
            seeds,
        ],
        "alphafold3": [
            "/usr/local/bin/fs2-run-alphafold3",
            "inference",
            "--processed-json",
            str(request),
            "--output-dir",
            str(output_dir),
            "--seeds",
            seeds,
        ],
        "openfold3": [
            "/usr/local/bin/fs2-run-openfold3",
            "--query-json",
            str(request),
            "--output-dir",
            str(output_dir),
            "--runner-yaml",
            str(runner_yaml) if runner_yaml is not None else "",
            "--prepared-marker",
            str(prepared_marker) if prepared_marker is not None else "",
            "--seeds",
            seeds,
        ],
    }
    command = commands[runtime_id]
    environment = dict(os.environ)
    environment.update(
        {
            "FS2_NETWORK_MODE": "offline",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    completed = subprocess.run(
        command,
        check=False,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"semantic wrapper failed with {completed.returncode}: {completed.stdout[-4000:]}"
        )
    structures = sorted(
        path
        for path in output_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".cif", ".mmcif", ".pdb"}
    )
    nonempty = [path for path in structures if path.stat().st_size > 0]
    if not nonempty:
        raise RuntimeError("semantic wrapper produced no non-empty structure artifact")
    confidence_path = output_dir / "confidence.json"
    try:
        confidence = json.loads(confidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"semantic wrapper produced no valid confidence.json: {exc}") from exc
    if not isinstance(confidence, dict):
        raise RuntimeError("semantic confidence envelope must be a JSON object")
    results = confidence.get("results")
    if (
        confidence.get("schema") != "fs2.nebius.ai/structure-confidence/v1"
        or confidence.get("runtime_id") != runtime_id
        or not isinstance(results, list)
        or not 1 <= len(results) <= 256
    ):
        raise RuntimeError("semantic confidence envelope is missing or unbounded")
    for result in results:
        structure = result.get("structure") if isinstance(result, dict) else None
        filename = structure.get("filename") if isinstance(structure, dict) else None
        if not isinstance(filename, str):
            raise RuntimeError("semantic confidence result lacks a relative structure filename")
        path = output_dir / filename
        if (
            Path(filename).is_absolute()
            or ".." in Path(filename).parts
            or not path.is_file()
            or structure.get("bytes") != path.stat().st_size
            or structure.get("sha256") != sha256_file(path)
        ):
            raise RuntimeError("semantic confidence result does not bind its structure bytes")
    return {
        "argv": command,
        "output_file_count": len(nonempty),
        "output_bytes": sum(path.stat().st_size for path in nonempty),
        "confidence_sha256": sha256_file(confidence_path),
        "confidence_result_count": len(results),
        "stdout_tail": completed.stdout[-2000:],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="check package/CLI identity only; this is not semantic readiness",
    )
    parser.add_argument("--semantic-request")
    parser.add_argument("--output-dir")
    parser.add_argument("--seeds", default="101")
    parser.add_argument(
        "--msa-mode",
        choices=("none", "precomputed"),
        default="none",
        help="Protenix handoff mode; ignored by other runtimes",
    )
    parser.add_argument(
        "--runner-yaml",
        help="OpenFold3 prepared runner YAML containing the exact model seeds",
    )
    parser.add_argument(
        "--prepared-marker",
        help="OpenFold3 immutable handoff marker emitted by prepare_openfold3.py",
    )
    args = parser.parse_args()

    runtime_id = os.environ.get("FS2_RUNTIME_ID", "")
    if Path.cwd() != Path("/opt/fs2/runtime"):
        raise RuntimeError(f"runtime working directory is not isolated: {Path.cwd()}")
    expected_revision = os.environ.get("FS2_SOURCE_REVISION", "")
    actual_revision = SOURCE_REVISION_FILE.read_text(encoding="ascii").strip()
    if not expected_revision or actual_revision != expected_revision:
        raise RuntimeError(
            f"source revision mismatch: expected={expected_revision!r} actual={actual_revision!r}"
        )

    build = _build_smoke(runtime_id)
    result: dict[str, object] = {
        "schema": "fs2.nebius.ai/structure-secondary-image-smoke/v2",
        "runtime_id": runtime_id,
        "source_revision": actual_revision,
        "artifact_policy": "external-only",
        "build": build,
    }
    if args.build_only:
        if args.semantic_request or args.output_dir:
            raise SystemExit("--build-only cannot be combined with semantic arguments")
        result.update(
            {
                "mode": "build-only-not-semantic-readiness",
                "external_roots": _assert_external_roots_are_empty(),
                "status": "passed",
            }
        )
    else:
        if not args.semantic_request or not args.output_dir:
            raise SystemExit(
                "semantic smoke requires --semantic-request and --output-dir; missing artifacts fail closed"
            )
        request = Path(args.semantic_request)
        output_dir = Path(args.output_dir)
        if not request.is_absolute() or not request.is_file():
            raise SystemExit("semantic-request must be an existing absolute file")
        if not output_dir.is_absolute():
            raise SystemExit("output-dir must be absolute")
        runner_yaml = Path(args.runner_yaml) if args.runner_yaml else None
        prepared_marker = Path(args.prepared_marker) if args.prepared_marker else None
        if runtime_id == "openfold3" and (
            runner_yaml is None
            or not runner_yaml.is_absolute()
            or not runner_yaml.is_file()
            or prepared_marker is None
            or not prepared_marker.is_absolute()
            or not prepared_marker.is_file()
        ):
            raise SystemExit(
                "OpenFold3 semantic smoke requires existing absolute --runner-yaml and --prepared-marker"
            )
        if runtime_id != "openfold3" and (
            runner_yaml is not None or prepared_marker is not None
        ):
            raise SystemExit(
                "--runner-yaml and --prepared-marker are valid only for OpenFold3"
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        result.update(
            {
                "mode": "exact-artifact-h100-semantic",
                "accelerator": _assert_h100(runtime_id),
                "semantic": _semantic_smoke(
                    runtime_id,
                    request,
                    output_dir,
                    seeds=args.seeds,
                    msa_mode=args.msa_mode,
                    runner_yaml=runner_yaml,
                    prepared_marker=prepared_marker,
                ),
                "status": "passed",
            }
        )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()

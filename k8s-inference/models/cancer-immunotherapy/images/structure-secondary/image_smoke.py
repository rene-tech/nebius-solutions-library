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
import sysconfig
import tempfile
from typing import Any

from result_contract import sha256_file, validate_confidence_envelope


SOURCE_REVISION_FILE = Path("/opt/fs2/source-revision")
EXTERNAL_ROOTS = (Path("/models"), Path("/databases"))
MIN_ATOM_RECORDS = 10
IMAGE_RUNTIME_UID = 10001
IMAGE_RUNTIME_GID = 10001
CACHE_WRITE_PROBE = b"fs2-cache-write-probe-v1\n"
BUILD_CACHE_CONTRACTS: dict[str, dict[str, object]] = {
    "protenix-v2": {
        "mount_roots": ["/cache/protenix"],
        "environment": {
            "TRITON_CACHE_DIR": "/cache/protenix/triton",
            "CUEQ_TRITON_CACHE_DIR": "/cache/protenix/cueq-triton",
            "TORCH_EXTENSIONS_DIR": "/cache/protenix/torch-extensions",
            "XDG_CACHE_HOME": "/cache/protenix/xdg",
        },
    },
    "openfold3": {
        "mount_roots": ["/cache/openfold3"],
        "environment": {
            "TRITON_CACHE_DIR": "/cache/openfold3/triton",
            "TORCH_EXTENSIONS_DIR": "/cache/openfold3/torch-extensions",
            "XDG_CACHE_HOME": "/cache/openfold3/xdg",
        },
    },
}


def _probe_cache_directory(directory: Path) -> dict[str, object]:
    if not directory.is_absolute():
        raise RuntimeError(f"cache directory must be absolute: {directory}")
    if not directory.is_dir():
        raise RuntimeError(f"cache directory does not exist: {directory}")
    if not os.access(directory, os.W_OK, effective_ids=True):
        raise RuntimeError(
            f"cache directory is not writable by UID {IMAGE_RUNTIME_UID}: {directory}"
        )

    descriptor = -1
    probe: Path | None = None
    try:
        descriptor, probe_name = tempfile.mkstemp(
            prefix=".fs2-cache-smoke.", dir=str(directory)
        )
        probe = Path(probe_name)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(CACHE_WRITE_PROBE)
            stream.flush()
        if probe.read_bytes() != CACHE_WRITE_PROBE:
            raise RuntimeError(f"cache write probe readback failed: {directory}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if probe is not None:
            probe.unlink(missing_ok=True)
    if probe is None or probe.exists():
        raise RuntimeError(f"cache write probe cleanup failed: {directory}")
    return {
        "path": str(directory),
        "probe": "bounded-create-read-remove-passed",
        "probe_bytes": len(CACHE_WRITE_PROBE),
    }


def _validate_build_cache_contract(runtime_id: str) -> dict[str, object]:
    effective_uid = os.geteuid()
    effective_gid = os.getegid()
    if effective_uid != IMAGE_RUNTIME_UID or effective_gid != IMAGE_RUNTIME_GID:
        raise RuntimeError(
            "build-only cache smoke must run as the final image user "
            f"{IMAGE_RUNTIME_UID}:{IMAGE_RUNTIME_GID}, got "
            f"{effective_uid}:{effective_gid}"
        )

    contract = BUILD_CACHE_CONTRACTS.get(
        runtime_id, {"mount_roots": [], "environment": {}}
    )
    mount_roots = list(contract["mount_roots"])
    expected_environment = dict(contract["environment"])
    actual_environment = {
        name: os.environ.get(name) for name in expected_environment
    }
    if actual_environment != expected_environment:
        raise RuntimeError(
            "cache environment does not exactly match the canonical contract: "
            f"expected={expected_environment!r} actual={actual_environment!r}"
        )

    paths = sorted(set(mount_roots) | set(expected_environment.values()))
    directories = [_probe_cache_directory(Path(path)) for path in paths]
    return {
        "scope": "built-image-filesystem-only",
        "deployment_persistent_mount_readiness": "not-tested",
        "effective_uid": effective_uid,
        "effective_gid": effective_gid,
        "declared": bool(mount_roots or expected_environment),
        "mount_roots": mount_roots,
        "environment": expected_environment,
        "directories": directories,
    }


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


def _probe_python_launcher_compiler(compiler: str, cache_dir: Path) -> int:
    """Compile one bounded Python launcher object using the runtime toolchain."""
    include_dir = Path(sysconfig.get_paths()["include"])
    python_header = include_dir / "Python.h"
    if not python_header.is_file():
        raise RuntimeError(f"runtime Python headers are missing: {python_header}")
    source_text = """\
#include <Python.h>
static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "fs2_launcher_probe", NULL, -1, NULL};
PyMODINIT_FUNC PyInit_fs2_launcher_probe(void) { return PyModule_Create(&module); }
"""
    with tempfile.TemporaryDirectory(prefix=".fs2-launcher-probe.", dir=cache_dir) as root:
        root_path = Path(root)
        source = root_path / "launcher.c"
        output = root_path / "fs2_launcher_probe.so"
        source.write_text(source_text, encoding="utf-8")
        completed = subprocess.run(
            [
                compiler,
                str(source),
                "-O2",
                "-shared",
                "-fPIC",
                f"-I{include_dir}",
                "-o",
                str(output),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0 or not output.is_file():
            raise RuntimeError(
                "Protenix runtime launcher compiler probe failed: "
                f"exit={completed.returncode} output={completed.stdout[-1200:]}"
            )
        output_size = output.stat().st_size
        if not 1_000 <= output_size <= 1_000_000:
            raise RuntimeError(
                f"Protenix runtime launcher probe size is implausible: {output_size}"
            )
        return output_size


def _build_smoke(runtime_id: str) -> dict[str, Any]:
    if runtime_id in {"esmfold2", "esmfold2-fast"}:
        from esm.models.esmfold2 import ESMFold2InputBuilder, EsmFold2Model

        if not ESMFold2InputBuilder or not EsmFold2Model:
            raise RuntimeError("ESMFold2 public inference API is incomplete")
        result = _torch_build_smoke()
        result["package_version"] = importlib.metadata.version("esm")
        if result["package_version"] != "3.4.0":
            raise RuntimeError(
                f"installed esm package must be exactly 3.4.0, got {result['package_version']}"
            )
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
        launcher_compiler = shutil.which("gcc") or shutil.which("clang")
        if launcher_compiler is None:
            raise RuntimeError(
                "Protenix cuequivariance Triton JIT requires a runtime C launcher compiler"
            )
        launcher_probe_bytes = _probe_python_launcher_compiler(
            launcher_compiler, Path(os.environ["TRITON_CACHE_DIR"])
        )
        _run_cli(["/opt/protenix-venv/bin/protenix", "--help"], "pred")
        _run_cli(["/usr/local/bin/fs2-run-protenix", "--help"], "prep")
        result = _torch_build_smoke()
        result["package_version"] = importlib.metadata.version("protenix")
        if result["package_version"] != "2.0.0":
            raise RuntimeError(
                "installed protenix package must be exactly 2.0.0, "
                f"got {result['package_version']}"
            )
        result["cli"] = "protenix prep|pred"
        result["module_resolutions"] = resolutions
        result["prebuilt_extension"] = str(extension)
        result["runtime_compilation"] = {
            "fast_layernorm": "prebuilt-sm90-cubin-plus-compute90-ptx",
            "cuequivariance": "active-triton-jit-first-shape-then-cache",
            "launcher_compiler": launcher_compiler,
            "launcher_probe": "bounded-python-extension-compile-passed",
            "launcher_probe_bytes": launcher_probe_bytes,
            "nvcc": "absent",
            "triton_cache_dir": os.environ.get("TRITON_CACHE_DIR"),
            "cueq_triton_cache_dir": os.environ.get("CUEQ_TRITON_CACHE_DIR"),
            "h100_first_call_vs_warm_call": "pending-semantic-measurement",
        }
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
        _run_cli(["/usr/local/bin/fs2-run-openfold3", "--help"], "prepare")
        _run_cli(["/usr/local/bin/fs2-run-openfold3", "predict", "--help"], "checkpoint")
        result["cli"] = "fs2-run-openfold3 prepare|predict"
        result["ccd_api"] = "biotite.structure.info.ccd.set_ccd_path"
        return result

    raise RuntimeError(f"unsupported FS2_RUNTIME_ID: {runtime_id!r}")


def _assert_h100(runtime_id: str) -> dict[str, object]:
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


def _validate_semantic_output(
    output_dir: Path,
    *,
    runtime_id: str,
    seeds: list[int],
    samples_per_seed: int,
) -> tuple[Path, dict[str, object], list[Path]]:
    confidence_path = output_dir / "confidence.json"
    try:
        confidence = json.loads(confidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"semantic wrapper produced no valid confidence.json: {exc}") from exc
    try:
        validated = validate_confidence_envelope(
            output_dir,
            confidence,
            expected_runtime_id=runtime_id,
            expected_seeds=seeds,
            expected_samples_per_seed=samples_per_seed,
        )
    except SystemExit as exc:
        raise RuntimeError(str(exc)) from exc
    structures: list[Path] = []
    for result in validated["results"]:
        structure = result["structure"]
        path = output_dir / structure["filename"]
        atom_count = sum(
            1
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.lstrip().startswith(("ATOM ", "HETATM "))
        )
        if atom_count < MIN_ATOM_RECORDS:
            raise RuntimeError(
                f"semantic structure has fewer than {MIN_ATOM_RECORDS} atom records: {path}"
            )
        structures.append(path)
    return confidence_path, validated, structures


def _semantic_smoke(
    runtime_id: str,
    request: Path,
    output_dir: Path,
    *,
    seeds: str,
    msa_mode: str,
    runner_yaml: Path | None,
    provenance_marker: Path | None,
    runtime_localization_marker: Path,
    input_artifact_id: str | None,
    samples_per_seed: int,
) -> dict[str, object]:
    parsed_seeds = [int(value) for value in seeds.split(",")]
    if not 1 <= len(parsed_seeds) <= 16 or len(set(parsed_seeds)) != len(parsed_seeds):
        raise RuntimeError("semantic smoke seeds must be 1..16 unique integers")
    if not 1 <= samples_per_seed <= 16:
        raise RuntimeError("semantic smoke samples-per-seed must be in [1, 16]")
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
            "--seed",
            str(parsed_seeds[0]),
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
            "--seed",
            str(parsed_seeds[0]),
        ],
        "protenix-v2": [
            "/usr/local/bin/fs2-run-protenix",
            "pred",
            "--input",
            str(request),
            "--output-dir",
            str(output_dir),
            "--msa-mode",
            "none",
            "--input-marker",
            str(provenance_marker) if provenance_marker is not None else "",
            "--input-artifact-id",
            input_artifact_id or "",
            "--checkpoint",
            "/models/protenix-v2/checkpoint/protenix-v2.pt",
            "--common-dir",
            "/models/protenix-v2/common",
            "--seeds",
            seeds,
            "--sample-count",
            str(samples_per_seed),
            "--disable-templates",
            "--disable-rna-msa",
        ],
        "openfold3": [
            "/usr/local/bin/fs2-run-openfold3",
            "predict",
            "--query-json",
            str(request),
            "--provenance-marker",
            str(provenance_marker) if provenance_marker is not None else "",
            "--input-artifact-id",
            input_artifact_id or "",
            "--expected-raw-input-sha256",
            (
                str(json.loads(provenance_marker.read_text(encoding="utf-8")).get("raw_input_sha256", ""))
                if provenance_marker is not None
                else ""
            ),
            "--output-dir",
            str(output_dir),
            "--runner-yaml",
            str(runner_yaml) if runner_yaml is not None else "",
            "--base-runner-yaml",
            "/opt/fs2/runtime/openfold3/runner-base.yaml",
            "--checkpoint",
            "/models/openfold3/of3-ob-2025-06-30-174k.pt",
            "--ccd-path",
            "/databases/openfold3/components.bcif",
            "--num-diffusion-samples",
            "1",
            "--num-model-seeds",
            str(len(parsed_seeds)),
            "--model-seeds",
            seeds,
            "--msa-mode",
            "none",
            "--use-templates",
            "false",
        ],
    }
    command = commands[runtime_id]
    command.extend(
        ["--runtime-localization-marker", str(runtime_localization_marker)]
    )
    if runtime_id in {"esmfold2", "esmfold2-fast"} and len(parsed_seeds) != 1:
        raise RuntimeError(f"{runtime_id} semantic smoke accepts exactly one seed")
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
    expected_samples = 1 if runtime_id in {"esmfold2", "esmfold2-fast", "openfold3"} else samples_per_seed
    expected_seeds = parsed_seeds
    if runtime_id in {"esmfold2", "esmfold2-fast"}:
        expected_seeds = parsed_seeds[:1]
    confidence_path, validated, nonempty = _validate_semantic_output(
        output_dir,
        runtime_id=runtime_id,
        seeds=expected_seeds,
        samples_per_seed=expected_samples,
    )
    results = validated["results"]
    return {
        "argv": command,
        "output_file_count": len(nonempty),
        "output_bytes": sum(path.stat().st_size for path in nonempty),
        "confidence_sha256": sha256_file(confidence_path),
        "confidence_result_count": len(results),
        "minimum_atom_records": MIN_ATOM_RECORDS,
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
        choices=("none",),
        default="none",
        help="Protenix handoff mode; ignored by other runtimes",
    )
    parser.add_argument(
        "--runner-yaml",
        help="OpenFold3 prepared runner YAML containing the exact model seeds",
    )
    parser.add_argument("--provenance-marker")
    parser.add_argument("--runtime-localization-marker")
    parser.add_argument("--input-artifact-id")
    parser.add_argument("--samples-per-seed", type=int, default=1)
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
        if args.semantic_request or args.output_dir or args.runtime_localization_marker:
            raise SystemExit("--build-only cannot be combined with semantic arguments")
        result.update(
            {
                "mode": "build-only-not-semantic-readiness",
                "build_cache": _validate_build_cache_contract(runtime_id),
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
        provenance_marker = Path(args.provenance_marker) if args.provenance_marker else None
        runtime_localization_marker = (
            Path(args.runtime_localization_marker)
            if args.runtime_localization_marker
            else None
        )
        if (
            runtime_localization_marker is None
            or not runtime_localization_marker.is_absolute()
            or not runtime_localization_marker.is_file()
        ):
            raise SystemExit(
                "semantic smoke requires an absolute existing --runtime-localization-marker"
            )
        if runtime_id == "openfold3" and (
            runner_yaml is None or not runner_yaml.is_absolute()
        ):
            raise SystemExit(
                "OpenFold3 semantic smoke requires an absolute --runner-yaml output path"
            )
        if runtime_id != "openfold3" and runner_yaml is not None:
            raise SystemExit("--runner-yaml is valid only for OpenFold3")
        if runtime_id in {"protenix-v2", "openfold3"} and (
            provenance_marker is None
            or not provenance_marker.is_absolute()
            or not provenance_marker.is_file()
            or not args.input_artifact_id
        ):
            raise SystemExit(
                "staged semantic smoke requires --provenance-marker and --input-artifact-id"
            )
        if runtime_id not in {"protenix-v2", "openfold3"} and (
            provenance_marker is not None or args.input_artifact_id is not None
        ):
            raise SystemExit("handoff provenance arguments are valid only for staged runtimes")
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
                    provenance_marker=provenance_marker,
                    runtime_localization_marker=runtime_localization_marker,
                    input_artifact_id=args.input_artifact_id,
                    samples_per_seed=args.samples_per_seed,
                ),
                "status": "passed",
            }
        )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()

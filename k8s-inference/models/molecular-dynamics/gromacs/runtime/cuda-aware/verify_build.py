"""Fail-closed compile-time gate; never substitute this for a real GPU probe."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def check_capability(
    config: str,
    info: str,
    extension: str,
    cache: str,
    symbols: str,
    component_symbols: str,
) -> None:
    checks = {
        "Open MPI configured CUDA support": r"(?m)^#define OPAL_CUDA_SUPPORT 1$",
        "Open MPI compiled capability": r"(?m)^mca:opal:base:param:opal_built_with_cuda_support:value:true$",
        "MPI extension CUDA macro": r"(?m)^#define MPIX_CUDA_AWARE_SUPPORT 1$",
        "GROMACS MPI query compiled": r"(?m)^MPI_SUPPORTS_CUDA_AWARE_DETECTION:INTERNAL=1$",
        "GROMACS uses the runtime query": r"(?m)^\s+U MPIX_Query_cuda_support$",
    }
    for (name, pattern), evidence in zip(
        checks.items(), (config, info, extension, cache, symbols)
    ):
        if not re.search(pattern, evidence):
            raise ValueError(f"missing positive evidence: {name}")
    # A CPU builder has no injected libcuda.so.1, so ompi_info cannot dlopen
    # this DSO. Assert its real exported component, not CPU-time loadability.
    if not re.search(
        r"(?m)^[0-9a-fA-F]+\s+[BD]\s+mca_accelerator_cuda_component$", component_symbols
    ):
        raise ValueError("CUDA accelerator component is missing")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--mpi-prefix", type=Path, required=True)
    parser.add_argument("--gromacs-prefix", type=Path, required=True)
    args = parser.parse_args()
    prov = args.provenance
    extension = args.mpi_prefix / "include/openmpi/mpiext/mpiext_cuda_c.h"
    binary = args.gromacs_prefix / "lib/libgromacs_mpi.so"
    symbols = subprocess.check_output(["nm", "-D", str(binary)], text=True)
    component = args.mpi_prefix / "lib/openmpi/mca_accelerator_cuda.so"
    component_symbols = subprocess.check_output(["nm", "-D", str(component)], text=True)
    info = subprocess.run(
        [str(args.mpi_prefix / "bin/ompi_info"), "--all", "--parsable"],
        capture_output=True,
        text=True,
        check=True,
    )
    (prov / "ompi-info.stderr.txt").write_text(info.stderr)
    (prov / "cuda-component-symbols.txt").write_text(component_symbols)
    check_capability(
        (prov / "opal_config.h").read_text(),
        (prov / "ompi-info.txt").read_text(),
        extension.read_text(),
        (prov / "original/gromacs-CMakeCache.txt").read_text(),
        symbols,
        component_symbols,
    )
    # A build-only driver stub must never become an installed runtime search path.
    for library in (args.mpi_prefix / "lib").rglob("*.so*"):
        if library.is_symlink() or not library.is_file():
            continue
        dynamic = subprocess.check_output(["readelf", "-d", str(library)], text=True)
        for line in dynamic.splitlines():
            if ("RPATH" in line or "RUNPATH" in line) and "/stubs" in line:
                raise ValueError(f"driver stub in runtime search path: {library}")
    files = [p for p in prov.rglob("*") if p.is_file()]
    receipt = {
        "schema": "fs2-gromacs-mpi-cuda-build/v1",
        "status": "passed",
        "scope": "compile-time capability only; initialized real-GPU query and device-buffer communication still required",
        "gromacs_binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "files": {
            str(p.relative_to(prov)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(files)
        },
        "runtime_gpu_qualified": False,
        "customer_ready": False,
    }
    (prov / "build-verification.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"status": "passed", "scope": "compile-time only"}))


if __name__ == "__main__":
    main()

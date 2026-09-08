"""Bounded local CPU/CUDA timing; not a Kubernetes or public-API cold-start claim."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path


def process_memory() -> dict[str, int]:
    """Linux process-local RSS/HWM, without inherited pre-exec ru_maxrss peaks."""
    fields = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(("VmRSS:", "VmHWM:")):
            key, value, unit = line.split()
            if unit != "kB":
                raise RuntimeError("unexpected Linux RSS unit")
            fields[key.rstrip(":")] = int(value) * 1024
    return fields


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=("phenoage", "altumage"))
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--artifact-root", type=Path, default=Path("/opt/altumage"))
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=[1, 8, 64])
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 100 or any(
        not 1 <= value <= 128 for value in args.batch_sizes
    ):
        parser.error("use 1–100 repeats and batches of 1–128 samples")
    if args.model == "phenoage" and args.device != "cpu":
        parser.error("clinical PhenoAge is a CPU formula, not a GPU workload")
    started = time.perf_counter()
    from aging.contracts import AltumAgeRequest, ClinicalRequest
    from aging.fixtures import clinical_payload, methylation_payload

    if args.model == "phenoage":
        from aging.phenoage.runtime import ClinicalPhenoAgeRuntime

        runtime = ClinicalPhenoAgeRuntime()
        request_class = ClinicalRequest
    else:
        from aging.altumage.runtime import AltumAgeRuntime

        runtime = AltumAgeRuntime(args.artifact_root, args.device, args.threads)
        request_class = AltumAgeRequest
    imported_and_loaded = time.perf_counter() - started
    source_root = Path(__file__).resolve().parent
    report = {
        "schema": "fs2-aging-local-runtime-benchmark/v1",
        "measured_at": datetime.now(UTC).isoformat(),
        "model": runtime.metadata(),
        "machine": {
            "architecture": platform.machine(),
            "logical_cpus": os.cpu_count(),
            "python": platform.python_version(),
            "configured_torch_threads": args.threads,
        },
        "boundary": "local process imports + model constructor; warm JSON decode + validation + inference",
        "excluded": [
            "container pull",
            "node provisioning",
            "Kubernetes scheduling",
            "public admission",
            "network transfer",
            "JSON response encoding",
        ],
        "cache_state": "artifact files already present; filesystem/page-cache state uncontrolled",
        "imports_and_model_load_seconds": imported_and_loaded,
        "source_sha256": {
            str(path.relative_to(source_root)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(source_root.rglob("*.py"))
            if "tests" not in path.parts
        },
        "batches": [],
    }
    for count in args.batch_sizes:
        payload = (
            clinical_payload(count)
            if args.model == "phenoage"
            else methylation_payload(args.artifact_root, count)
        )
        encoded = json.dumps(payload, separators=(",", ":"))
        end_to_end, compute = [], []
        for _ in range(args.repeats):
            begin = time.perf_counter()
            request = request_class.model_validate_json(encoded)
            validated = time.perf_counter()
            results = runtime.predict(request)
            finish = time.perf_counter()
            assert len(results) == count
            end_to_end.append(finish - begin)
            compute.append(finish - validated)
        report["batches"].append(
            {
                "samples": count,
                "repeats": args.repeats,
                "request_bytes": len(encoded.encode()),
                "json_validate_and_predict_seconds": {
                    "median": statistics.median(end_to_end),
                    "min": min(end_to_end),
                    "max": max(end_to_end),
                },
                "predict_including_scaling_and_device_transfers_seconds": {
                    "median": statistics.median(compute),
                    "min": min(compute),
                    "max": max(compute),
                },
                "samples_per_second_at_median": count / statistics.median(end_to_end),
            }
        )
    report["linux_process_memory_bytes"] = process_memory()
    if args.device == "cuda":
        import torch

        report["gpu"] = {
            "name": torch.cuda.get_device_name(),
            "torch_version": torch.__version__,
            "cuda_version": torch.version.cuda,
            "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
            "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
        }
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()

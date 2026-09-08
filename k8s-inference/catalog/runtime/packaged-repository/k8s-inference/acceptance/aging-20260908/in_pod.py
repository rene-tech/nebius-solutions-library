"""Run exact packaged aging fixtures against an isolated worker, not a public App."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen


def http(path, payload=None):
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    started = time.perf_counter()
    with urlopen(Request(
        "http://127.0.0.1:8000" + path, data=body,
        headers={} if body is None else {"Content-Type": "application/json"},
    ), timeout=30) as response:
        raw = response.read()
        result = {
            "path": path, "status": response.status,
            "elapsed_seconds": time.perf_counter() - started,
            "request_sha256": None if body is None else hashlib.sha256(body).hexdigest(),
            "request_bytes": 0 if body is None else len(body),
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "response_bytes": len(raw),
        }
        result["body"] = json.loads(raw) if path != "/metrics" else raw.decode()
        print(json.dumps({"event": "native_http_response", "result": result}), flush=True)
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("model", choices=("phenoage", "altumage"))
    args = parser.parse_args()
    from aging.fixtures import clinical_payload, methylation_payload

    artifact_root = Path("/opt/altumage")
    payload = clinical_payload(2) if args.model == "phenoage" else methylation_payload(artifact_root, 2)
    report = {
        "schema": "fs2-aging-isolated-runtime-qualification/v1",
        "started_at": datetime.now(UTC).isoformat(),
        "model": args.model,
        "boundary": "native worker HTTP loopback and packaged local module; excludes public App admission/routing",
        "readiness": http("/v1/health/ready"),
        "predictions": [], "benchmarks": [],
    }
    expected_device = "cpu" if args.model == "phenoage" else "cuda"
    assert report["readiness"]["body"]["device"] == expected_device
    field = "phenotypic_age_years" if args.model == "phenoage" else "predicted_chronological_age_years"
    for sample in payload["samples"]:
        response = http("/v1/predict", {**payload, "samples": [sample]})
        report["predictions"].append(response)
        assert response["body"]["device"] == expected_device
        assert response["body"]["sample_count"] == 1
    values = [reply["body"]["predictions"][0][field] for reply in report["predictions"]]
    assert all(math.isfinite(value) for value in values) and values[0] != values[1]
    if args.model == "phenoage":
        assert math.isclose(values[0], 41.90792243377999, rel_tol=0, abs_tol=1e-10)
    else:
        import torch
        from aging.altumage.runtime import AltumAgeRuntime
        from aging.contracts import AltumAgeRequest

        cpu = AltumAgeRuntime(artifact_root, "cpu", 1)
        gpu = AltumAgeRuntime(artifact_root, "cuda", 1)
        report["parity"] = []
        for count in (1, 2, 8):
            request = AltumAgeRequest.model_validate(methylation_payload(artifact_root, count))
            cpu_results, gpu_results = cpu.predict(request), gpu.predict(request)
            differences = [
                abs(left[field] - right[field])
                for left, right in zip(cpu_results, gpu_results, strict=True)
            ]
            assert max(differences) <= 0.001
            report["parity"].append({
                "samples": count, "max_absolute_age_difference": max(differences),
                "tolerance": 0.001, "cpu": cpu_results, "cuda": gpu_results,
            })
        report["gpu"] = {
            "name": torch.cuda.get_device_name(),
            "compute_capability": list(torch.cuda.get_device_capability()),
            "torch": torch.__version__, "cuda_build": torch.version.cuda,
            "model_parameter_device": str(next(gpu.model.parameters()).device),
            "weights_sha256": gpu.weights_sha256,
        }
    for device in (("cpu",) if args.model == "phenoage" else ("cpu", "cuda")):
        command = [
            sys.executable, "-m", "aging.benchmark_runtime", args.model,
            "--device", device, "--batch-sizes", "1", "8", "--repeats", "20", "--threads", "1",
        ]
        executed = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
        report["benchmarks"].append({
            "command": command, "returncode": executed.returncode,
            "stdout": executed.stdout, "stderr": executed.stderr,
        })
        print(json.dumps({"event": "module_benchmark", "result": report["benchmarks"][-1]}), flush=True)
        assert executed.returncode == 0
        report["benchmarks"][-1]["result"] = json.loads(executed.stdout)
    report["metrics"] = http("/metrics")
    report["completed_at"] = datetime.now(UTC).isoformat()
    report["outcome"] = "passed"
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

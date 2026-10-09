"""Run a fixed bounded subset of private installed PMEMD upstream GPU tests.

Run inside the candidate image. Licensed assets and raw outputs stay private.
Upstream comparison thresholds and protocols are not changed by this harness.
The upstream scripts remove their temporary restart/trajectory files; separate
worker cohorts qualify full-output/restart retention and sustained dynamics.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from fs2_gromacs.files import atomic_json, digest_file, inventory
from fs2_amber import ENGINE_ID, PMEMD_SOURCE_SHA256

CASES = [
    ("dhfr-nve", "cuda/dhfr", "Run.dhfr"),
    ("dhfr-minimization", "cuda/dhfr", "Run.dhfr.min"),
    ("dhfr-npt", "cuda/dhfr", "Run.dhfr.ntb2"),
    ("ala3-gb5", "cuda/gb_ala3", "Run.irest1_ntt0_igb5_ntc2"),
    ("myoglobin-gb8", "cuda/myoglobin", "Run_md_myoglobin_igb8"),
    ("sodium-ti", "cuda/gti/Na", "Run.NVE"),
    ("sodium-softcore-mbar", "cuda/gti/Na", "Run.SC_NVT_MBAR"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = Path("/opt/amber26/test")
    gpu = subprocess.check_output(["nvidia-smi", "--query-gpu=name,uuid,driver_version,compute_cap", "--format=csv,noheader"], text=True).strip()
    receipt = {"schema": "fs2-serve.nebius.ai/amber-upstream-regression/v1", "engine_id": ENGINE_ID, "pmemd_source_sha256": PMEMD_SOURCE_SHA256, "gpu": gpu, "status": "incomplete", "tests": [], "customer_ready": False, "sustained_benchmark": False, "comparison_policy": "unmodified upstream scripts and precision-specific thresholds; ignored upstream failures still fail this cohort"}
    environment = {**os.environ, "AMBERHOME": "/opt/amber26", "OMP_NUM_THREADS": "1"}
    environment.pop("DO_PARALLEL", None)
    environment.pop("TESTsander", None)
    with (args.output / "gpu.csv").open("w") as output:
        monitor = subprocess.Popen(["nvidia-smi", "--query-gpu=timestamp,name,utilization.gpu,memory.used,power.draw", "--format=csv,noheader,nounits", "-lms", "1000"], stdout=output)
        try:
            for precision in ("SPFP", "DPFP"):
                for case, directory, script in CASES:
                    root = args.output / (case + "-" + precision.lower())
                    target = root / "test" / directory
                    shutil.copytree(source / directory, target)
                    shutil.copy2(source / "dacdif", root / "test/dacdif")
                    original = inventory(target, max_bytes=512 * 1024**2)
                    log = root / "regression.log"
                    command = ["/bin/tcsh", "-f", script, precision]
                    start = time.monotonic()
                    with log.open("w") as handle:
                        try:
                            child = subprocess.Popen(command, cwd=target, env=environment, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
                            code, timed_out = child.wait(timeout=180), False
                        except subprocess.TimeoutExpired:
                            os.killpg(child.pid, signal.SIGTERM)
                            try:
                                child.wait(timeout=15)
                            except subprocess.TimeoutExpired:
                                os.killpg(child.pid, signal.SIGKILL)
                                child.wait()
                            code, timed_out = None, True
                    text = log.read_text(errors="replace")
                    passed = code == 0 and "PASSED" in text and "FAILURE" not in text and not list((root / "test").rglob("*.dif"))
                    item = {"case": case, "precision": precision, "status": "passed" if passed else "failed", "argv": command, "exit_code": code, "timed_out": timed_out, "wall_seconds": time.monotonic() - start, "upstream_script_sha256": digest_file(source / directory / script), "source_files": original, "log": str(log.relative_to(args.output)), "log_sha256": digest_file(log)}
                    receipt["tests"].append(item)
                    atomic_json(args.output / "regression.json", receipt)
                    print(json.dumps({key: item[key] for key in ("case", "precision", "status", "wall_seconds")}), flush=True)
        finally:
            monitor.terminate()
            monitor.wait(timeout=5)
    receipt["status"] = "passed" if len(receipt["tests"]) == len(CASES) * 2 and all(item["status"] == "passed" for item in receipt["tests"]) else "failed"
    receipt["files"] = [item for item in inventory(args.output, max_bytes=2 * 1024**3) if item["path"] != "regression.json"]
    atomic_json(args.output / "regression.json", receipt)
    raise SystemExit(0 if receipt["status"] == "passed" else 1)


if __name__ == "__main__":
    main()

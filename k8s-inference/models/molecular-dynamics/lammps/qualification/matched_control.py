"""Repeat one unchanged fixture on one already-owned experimental image Pod.

This intentionally cannot emit a full native qualification or promotion receipt.
The caller owns sequential Pod replacement and the approved GPU concurrency.
"""

import argparse
import json
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from cluster import owned, run
from mirror_runtime import KUBE
from native_receipt import case_receipt, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--case", choices=("lj", "eam", "tersoff", "snap", "reaxff", "rhodo"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, choices=(3,), default=3)
    args = parser.parse_args()
    pod = owned(args.pod)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "pod.json").write_text(json.dumps(pod, indent=2) + "\n")
    sources = {name: digest(args.fixture / name) for name in ("input.tar.gz", "request.json", "fixture-manifest.json")}
    for name, command in (
        ("gpu-context.txt", ["nvidia-smi", "-q"]),
        ("cpu-context.txt", ["lscpu"]),
        ("kernel-context.txt", ["uname", "-a"]),
    ):
        (args.output / name).write_bytes(subprocess.check_output(KUBE + ["exec", args.pod, "--"] + command))
    tests = []
    for repetition in range(1, args.repetitions + 1):
        # No regeneration: all images receive exactly the same immutable tar.
        if any(digest(args.fixture / name) != value for name, value in sources.items()):
            raise ValueError("the immutable matched input changed")
        destination = args.output / (args.output.name + "-r" + str(repetition))
        run(SimpleNamespace(pod=args.pod, input=args.fixture, output=destination, job=args.case))
        receipt = case_receipt(destination, revalidate=True)
        if receipt["input_sha256"] != sources["input.tar.gz"]:
            raise ValueError("experimental worker used different input bytes")
        receipt["repetition"] = repetition
        tests.append(receipt)
        passed = len(tests) == args.repetitions and all(t["status"] == "passed" for t in tests)
        report = {"schema": "fs2-serve.nebius.ai/lammps-matched-control/v1", "status": "passed" if passed else "incomplete", "recorded_at": datetime.now(timezone.utc).isoformat(), "runtime_image": receipt["runtime_image"], "case": args.case, "source_hashes": sources, "tests": tests, "customer_ready": False, "promotion_approved": False, "full_native_qualification": False, "timing_boundary": "fresh native process per repetition; same Pod and immutable input; no profiler during timed run"}
        if passed:
            values = [t["scientific_validation"] for t in tests]
            report["median_native_seconds"] = statistics.median(v["native_loop_seconds"] for v in values)
            report["median_atom_timesteps_per_second"] = statistics.median(v["atom_timesteps_per_second"] for v in values)
            report["median_kspace_seconds"] = statistics.median(v["native_timing_seconds"].get("Kspace", 0) for v in values)
        (args.output / "matched-control.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"case": args.case, "repetition": repetition, "status": receipt["status"], "native_seconds": receipt["scientific_validation"].get("native_loop_seconds")}), flush=True)
        if receipt["status"] != "passed":
            raise SystemExit("failed matched attempt retained; control stopped without promotion")


if __name__ == "__main__":
    main()

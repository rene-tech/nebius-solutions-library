"""Validate/compare only the named fixed-input MEM PME controls; no live calls."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import statistics

from candidate_report import gpu_observations, reference
from qualify_candidate import save, sha, validate

MEM_PARAMETERS = {"dt": 0.002, "nsteps": 10000, "rcoulomb": 1, "rvdw": 1,
                  "fourier-nx": 96, "fourier-ny": 96, "fourier-nz": 80, "pme-order": 4}


def native_parameters(text, pme):
    values = {}
    for key in MEM_PARAMETERS:
        match = re.search(r"^\s*" + re.escape(key) + r"\s*=\s*(\S+)", text, re.M)
        values[key] = float(match[1]) if match else None
    failures = [key for key, value in MEM_PARAMETERS.items() if values[key] != value]
    if "PP/PME load balancing changed" in text:
        failures.append("PME autotuning changed original cutoff/grid")
    gpu_pme = "PME tasks will do all aspects on the GPU" in text
    if gpu_pme != (pme == "gpu"):
        failures.append("native GPU PME dispatch differs from requested protocol")
    if "PP tasks will do non-perturbed short-ranged interactions on the GPU" not in text:
        failures.append("nonbonded GPU / bonded CPU dispatch not observed")
    if "PP task will update and constrain coordinates on the CPU" not in text:
        failures.append("CPU update not observed")
    return {"parameters": values, "errors": failures, "gpu_pme_observed": gpu_pme,
            "gpu_aware_mpi_not_detected": "GPU-aware MPI was not detected" in text,
            "neighbor_list_adjustments": [line.strip() for line in text.splitlines() if "Changing nstlist" in line]}


def control(root, ranks, pme):
    case = f"mpi{ranks}-{pme}"
    directory, fixture = root / case, root / "fixtures" / case
    receipt = json.loads((directory / "receipt.json").read_text())
    request = json.loads((fixture / "request.json").read_text())
    validation = validate(directory / "workspace", request, ranks, True, False, receipt["worker_exit_code"])
    result = json.loads((directory / "workspace/result.json").read_text())
    commands = [c for c in result["commands"] if "mdrun" in c["command"]]
    native = []
    errors = list(validation["errors"])
    for i, command in enumerate(commands, 1):
        path = directory / "workspace/data" / f"repeat{i}.part0001.log"
        observed = native_parameters(path.read_text(), pme)
        errors += [f"repeat {i}: {error}" for error in observed["errors"]]
        args = command["command"]
        expected = {"-pme": pme, "-pmefft": pme, "-bonded": "cpu", "-update": "cpu",
                    "-npme": "0" if pme == "cpu" or ranks == 1 else "1"}
        if "-notunepme" not in args or "-resethway" in args:
            errors.append(f"repeat {i}: timing/autotuning flag mismatch")
        for flag, value in expected.items():
            if args.count(flag) != 1 or args[args.index(flag) + 1] != value:
                errors.append(f"repeat {i}: {flag} mismatch")
        native.append({"repeat": i, "log": reference(path, root), **observed})
    rates = [command["performance_ns_per_day"] for command in commands]
    return {"case": case, "status": "passed" if not errors else "failed", "errors": errors,
            "nodes": 1, "gpus_per_node": ranks, "image": receipt["image"], "source_revision": receipt["source_revision"],
            "input_sha256": sha(fixture / "input.tar.gz"), "request_sha256": sha(fixture / "request.json"),
            "original_tpr_sha256": sha(directory / "workspace/data/original.tpr"),
            "finite_tpr_sha256": sha(directory / "workspace/data/benchmark.tpr"),
            "gpu_observations": gpu_observations(directory), "observed_image_id": receipt["image_id"],
            "native_ns_per_day": {"repeats": rates, "mean": statistics.mean(rates), "median": statistics.median(rates),
                                  "sample_stdev": statistics.stdev(rates)},
            "runtime_wall_seconds": receipt["runtime_wall_seconds"], "native": native, "validation": validation,
            "evidence": [reference(directory / name, root) for name in ("receipt.json", "workspace/result.json", "native-mdrun-help.txt")],
            "cleanup": receipt["cleanup"]}


def report(root):
    controls = [control(root, ranks, pme) for ranks in (1, 2) for pme in ("cpu", "gpu")]
    parity = {field: len({c[field] for c in controls}) == 1 for field in
              ("image", "input_sha256", "original_tpr_sha256", "finite_tpr_sha256")}
    errors = [f"{c['case']}: validation failed" for c in controls if c["status"] != "passed"]
    errors += [field + " differs between controls" for field, ok in parity.items() if not ok]
    comparisons = []
    for ranks in (1, 2):
        cpu = next(c for c in controls if c["case"] == f"mpi{ranks}-cpu")
        gpu = next(c for c in controls if c["case"] == f"mpi{ranks}-gpu")
        comparisons.append({"ranks": ranks, "mean_rate_gpu_over_cpu": gpu["native_ns_per_day"]["mean"] / cpu["native_ns_per_day"]["mean"],
                            "definition": "ratio of arithmetic means of three warm-up-inclusive native rates; not a confidence interval or steady-state guarantee"})
    references = []
    for path in sorted((root / "references").iterdir()):
        references.append(reference(path, root))
    return {"schema": "fs2-serve.nebius.ai/gromacs-pme-control/v1", "status": "passed" if not errors else "failed",
            "recorded_at": datetime.now(timezone.utc).isoformat(), "errors": errors, "controls": controls,
            "input_engine_parity": parity, "comparisons": comparisons, "references": references,
            "customer_ready": False, "customer_path_tested": False, "gpu_snapshot_used": False,
            "limits": ["MEM input only, 1/2 L40S GPUs only; larger GPU-PME shapes remain unqualified.",
                       "Three 10k-step repeats restart the same input; warm-up included, no steady-state or ensemble-convergence claim.",
                       "1-rank/2-rank and CPU/GPU task splits differ intentionally; identical TPR and numerical cutoff/grid are verified.",
                       "GPU-aware MPI was not detected; no forced support flag, RDMA, or direct-GPU communication claim.",
                       "Fixed-input protocol is separate from prior auto-tuned NGC/single and CPU-PME MPI baselines."],
            "source_sha256": sha(__file__)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = report(args.root)
    save(args.output, value)
    print(json.dumps({"status": value["status"], "path": str(args.output), "sha256": sha(args.output),
                      "comparisons": value["comparisons"]}))
    return 0 if value["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

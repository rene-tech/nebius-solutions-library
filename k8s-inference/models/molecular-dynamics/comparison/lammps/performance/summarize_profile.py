"""Attribute captured GPU kernels without treating profiler timings as throughput."""
import argparse
import csv
import json
from pathlib import Path

from screen import sha


def summarize(campaign):
    report = {"scope": "CUDA/NVTX diagnostic; whole capture includes setup and 100+200 native steps",
              "timings_are_not_production_performance": True, "variants": {}}
    for variant in ("baseline-triclinic", "orthogonal"):
        case = campaign / (variant + "-r1")
        with (case / "summary_cuda_gpu_kern_sum.csv").open() as stream:
            kernels = list(csv.DictReader(stream))
        total = sum(int(row["Total Time (ns)"]) for row in kernels)
        selected = []
        for row in kernels:
            if "compute_gf_ik" in row["Name"] or "kiss_fft_functor" in row["Name"]:
                selected.append({"name": row["Name"], "instances": int(row["Instances"]),
                                 "total_seconds": int(row["Total Time (ns)"]) / 1e9,
                                 "mean_microseconds": float(row["Avg (ns)"]) / 1e3,
                                 "summed_kernel_time_percent": int(row["Total Time (ns)"]) / total * 100})
        if len(selected) != 2 or any(row["instances"] <= 0 for row in selected):
            raise ValueError("expected native Green-function and GPU FFT kernels absent")
        names = ("timeline.qdstrm", "timeline.nsys-rep", "timeline.sqlite", "summary_cuda_gpu_kern_sum.csv", "summary_cuda_api_sum.csv", "native.log", "measurement.json")
        report["variants"][variant] = {"summed_kernel_seconds": total / 1e9, "selected_kernels": selected,
                                       "files": {name: sha(case / name) for name in names}}
    validation = json.loads((campaign / "scientific-validation.json").read_text())
    if validation["status"] != "passed" or validation["unprofiled_summary"]:
        raise ValueError("profile must have separate scientific validation and no throughput summary")
    report["scientific_validation_sha256"] = sha(campaign / "scientific-validation.json")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = summarize(args.campaign)
    with args.output.open("x") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "sha256": sha(args.output)}))

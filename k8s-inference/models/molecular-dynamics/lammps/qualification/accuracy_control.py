"""Explicit conservative-neighbor controls; never replace the fixed baseline."""

import argparse
import json
import statistics
from pathlib import Path
from types import SimpleNamespace

from cluster import run
from make_fixture import fixture, write_fixture
from native_receipt import case_receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    baseline = json.loads((args.baseline / "campaign.json").read_text())
    results = []
    for case, steps, original, replacement in (
        ("lj", 300000, "neigh_modify delay 0 every 20 check no", "neigh_modify delay 0 every 1 check yes"),
        ("eam", 150000, "neigh_modify every 1 delay 5 check yes", "neigh_modify every 1 delay 0 check yes"),
    ):
        body, files = fixture(case, args.assets, steps, warmup=2000, segment_seconds=60, trajectory_every=10000)
        if original.encode() not in files["protocol.inc"]:
            raise ValueError("expected baseline neighbor policy is absent")
        files["protocol.inc"] = files["protocol.inc"].replace(original.encode(), replacement.encode())
        protocol = json.loads(files["protocol.json"])
        protocol["accuracy_control"] = {"baseline_neighbor_policy": original, "control_neighbor_policy": replacement, "other_physics_changed": False}
        files["protocol.json"] = (json.dumps(protocol, indent=2) + "\n").encode()
        source = args.output / (case + "-fixture")
        write_fixture(source, body, files)
        destination = args.output / (case + "-control")
        run(SimpleNamespace(pod=args.pod, output=destination, input=source, job=case))
        receipt = case_receipt(destination, revalidate=True)
        previous = [b for b in baseline if b["case"] == case and b["status"] == "passed"]
        if len(previous) != 3:
            raise ValueError("control comparison requires all three retained baseline repetitions")
        baseline_rate = statistics.median(b["atom_timesteps_per_second"] for b in previous)
        baseline_span = statistics.median(b["relative_total_energy_span"] for b in previous)
        science = receipt["scientific_validation"]
        results.append({"case": case, "baseline_raw_evidence": [b["directory"] for b in previous], "control": receipt, "baseline_median_atom_timesteps_per_second": baseline_rate, "control_throughput_ratio": science["atom_timesteps_per_second"] / baseline_rate, "baseline_median_relative_energy_span": baseline_span, "control_energy_span_ratio": science["relative_total_energy_span"] / baseline_span, "control_repetitions": 1, "performance_significance_claimed": False, "interpretation": "single bounded accuracy control, not a repeated optimization confirmation or a scientific convergence proof"})
        (args.output / "accuracy-control.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps({"case": case, "status": receipt["status"], "throughput_ratio": results[-1]["control_throughput_ratio"], "energy_span_ratio": results[-1]["control_energy_span_ratio"]}), flush=True)


if __name__ == "__main__":
    main()

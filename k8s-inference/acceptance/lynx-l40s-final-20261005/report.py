"""One immutable final-round report using the existing native/phase validators."""

import argparse
import json
import statistics
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "lynx-performance-20261005"
sys.path.insert(0, str(BASE))
from collect_report import reference
from cost_report import allocation_bounds
from native_probe import validate
from recipes import save, sha
from summarize_rdma import command_phases, project_samples, validate_source_bindings
from summarize_samples import summarize


def rates(simulated_ns, counter_seconds, process_seconds, worker_seconds):
    def rate(seconds):
        return simulated_ns * 86400 / seconds if seconds is not None and seconds > 0 else None

    return {"simulated_ns": simulated_ns,
            "native_counter_ns_per_day": rate(counter_seconds),
            "mdrun_process_ns_per_day": rate(process_seconds),
            "native_worker_ns_per_day": rate(worker_seconds),
            "public_delivered_ns_per_day": None,
            "scope": "Local native qualification only. Worker wall includes local staging/analysis/validation; "
                     "there is no platform checkpoint publication or customer export in this probe."}


def screen_rows(directory):
    receipt = json.loads((directory / "receipt.json").read_text())
    if receipt.get("status") != "passed" or "absence observed" not in receipt.get("cleanup", ""):
        raise ValueError("Require terminal owned screen and observed cleanup")
    records = []
    for candidate in receipt["summary"]["records"]:
        root = directory / "results" / candidate["case"]
        runs = json.loads((root / "measurements.json").read_text())
        for run in runs:
            for item in run["output_inventory"]:
                path = root / run["cohort"] / item["name"]
                if path.stat().st_size != item["bytes"] or sha(path) != item["sha256"]:
                    raise ValueError("Retained screen inventory changed")
        records.append({"case": candidate["case"], "nstlist": candidate["nstlist"],
            "pin": candidate.get("pin", "auto"), "environment": candidate["environment"],
            "all_native_valid": candidate["all_native_valid"],
            "warm_median_ns_per_day": candidate["warm_median_ns_per_day"],
            "warm_range_ns_per_day": candidate["warm_range_ns_per_day"],
            "runs": [{"cohort": r["cohort"], "native_ns_per_day": r["native_ns_per_day"],
                      "process_wall_seconds": r["process_wall_seconds"], "exit_code": r["exit_code"],
                      "validation": r.get("validation")} for r in runs],
            "measurements": reference(root / "measurements.json"),
            "actual_thread_masks": reference(directory / "results" / (candidate["case"] + "-affinity.json")),
            "graph_execution_proven": False if candidate["case"] == "graph-eligibility" else None})
    return {"receipt": reference(directory / "receipt.json"), "image": receipt["image"], "records": records,
            "environment": reference(directory / "environment.json"),
            "topology": reference(directory / "results" / "topology.json"),
            "full_tpr_comparison": reference(directory / "results" / "full-tpr-comparison.log")}


def collect(args):
    receipt = json.loads((args.confirmation / "receipt.json").read_text())
    if "absence observed" not in receipt.get("cleanup", ""):
        raise ValueError("Require observed final confirmation cleanup")
    validate_source_bindings({"native_md": receipt}, args.fixture)
    request = json.loads((args.fixture / "request.json").read_text())
    workspace = args.confirmation / "workspace"
    result = json.loads((workspace / "result.json").read_text())
    verified = validate(workspace, request, 1, False, False, receipt["worker_exit_code"])
    if verified != receipt["validation"] or verified["status"] != "passed":
        raise ValueError("Independent native confirmation verification failed")
    pod = json.loads((args.confirmation / "pod-final.json").read_text())
    uid = pod["metadata"]["uid"]
    resources = pod["spec"]["containers"][0]["resources"]
    if (uid != receipt["pod_uid"] or resources["requests"]["nvidia.com/gpu"] != "1"
            or resources["limits"]["cpu"] != "8" or resources["limits"]["memory"] != "16Gi"):
        raise ValueError("Confirmation did not use the reviewed one-GPU/eight-CPU envelope")
    capacity = json.loads((args.confirmation / "capacity-before.json").read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    last_seen = project_samples(args.samples, args.output / "projected-samples.jsonl", result["operation_id"],
                                {uid: pod}, {receipt["node"]: capacity})
    native = [c for c in result["commands"] if "mdrun" in c["command"]]
    counters = [r["native_mdrun_counter_wall_seconds"] for r in verified["repeats"]]
    counter = sum(counters) if None not in counters else None
    process = sum(c["wall_seconds"] for c in native)
    total_ns = sum(r["simulated_ns"] for r in verified["repeats"])
    scheduled = next(c["lastTransitionTime"] for c in pod["status"]["conditions"]
                     if c["type"] == "PodScheduled" and c["status"] == "True")
    dispatch = []
    for command in native:
        log = workspace / "data" / command["log"]
        lines = log.read_text(errors="replace").splitlines()
        dispatch.append({"step_id": command["step_id"], "argv": command["command"], "log": reference(log),
            "actual_dispatch": [line for line in lines if any(s in line for s in
                ("PP tasks will", "PP task will", "PME tasks will", "thread pinning", "Changing nstlist"))]})
    native_rates = [r["native_inclusive_ns_per_day"] for r in verified["repeats"]]
    report = {"schema": "lynx-final-l40s-round/v1", "collector": reference(Path(__file__)),
        "screen": screen_rows(args.screen),
        "public_pin_control": screen_rows(args.public_pin), "confirmation_receipt": reference(args.confirmation / "receipt.json"),
        "image": receipt["image"], "node": receipt["node"], "pod_uid": uid,
        "native_finished_at": receipt["finished_at"],
        "gpu_count": 1, "requested_cpu_cores": 8, "requested_memory_gib": 16,
        "hardware_evidence": {name: reference(args.confirmation / filename) for name, filename in
                              (("capacity", "capacity-before.json"), ("cpu", "cpu.txt"),
                               ("gpu", "gpu.txt"), ("topology", "topology.txt"), ("pod", "pod-final.json"))},
        "validation": verified, "request": reference(args.fixture / "request.json"),
        "rates": rates(total_ns, counter, process, receipt["runtime_wall_seconds"]),
        "native_rate_median": statistics.median(native_rates) if None not in native_rates else None,
        "native_mdrun_counter_wall_seconds": counter, "native_mdrun_process_wall_seconds": process,
        "native_worker_wall_seconds": receipt["runtime_wall_seconds"],
        "phase_measurements": command_phases(result, request, workspace / "data", 1, None),
        "allocation_seconds_bounds": allocation_bounds({"scheduled_at": scheduled,
            "release_lower_bound": last_seen.get(uid), "release_upper_bound": receipt["finished_at"]}),
        "retained_inventory_bytes": sum(f["size_bytes"] for f in result["files"]),
        "network_transfer_total_bytes": None, "checkpoint_publication_seconds": None,
        "customer_export_seconds": None, "public_delivered_ns_per_day": None,
        "actual_dispatch": dispatch, "samples": reference(args.samples),
        "sampled_cpu_gpu": summarize(args.output / "projected-samples.jsonl"),
        "local_checkpoint_step_replay": 0, "durable_remote_step_replay": None,
        "boundary": "Original-state finite repeats, not a resumed customer trajectory, a public receipt, "
                    "ensemble convergence, a bill, or a demonstrated >=200 ns/day customer delivery."}
    save(args.output / "report.json", report)
    print(json.dumps(reference(args.output / "report.json")))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("screen", "public-pin", "confirmation", "fixture", "samples", "output"):
        parser.add_argument("--" + field, type=Path, required=True)
    collect(parser.parse_args())

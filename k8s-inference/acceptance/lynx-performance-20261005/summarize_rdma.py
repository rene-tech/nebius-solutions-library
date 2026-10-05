"""Project one terminal native RDMA proof into the existing cost/phase helpers.

No API, database or GPU actions. Native Pod bounds are not public lifecycle
rollups. No trajectory copies or inferred residual phases.
"""

import argparse
import csv
import io
import json
from pathlib import Path

from collect_report import reference
from cost_report import (
    allocation_bounds,
    hourly_price,
    measured_phase,
    metric,
    phase_summary,
    preset_shape,
    staging_measurements,
    sum_bounds,
)
from ledger import native_counter_seconds
from native_probe import validate
from recipes import save
from summarize_samples import summarize


def validate_source_bindings(record, fixture):
    """Bind the independently read request/archive to the terminal receipt."""
    md = record["native_md"]
    for name, field in (("request.json", "request_sha256"), ("input.tar.gz", "input_sha256")):
        if reference(fixture / name)["sha256"] != md[field]:
            raise ValueError(f"Native {name} differs from the retained receipt")


def project_samples(path, destination, operation_id, identities, capacities):
    """Adapt retained native sampling fields, leaving absent memory peak null."""
    last_seen = {}
    previous_at, previous_uids = None, set()
    with path.open() as source, destination.open("x") as target:
        for line in source:
            row = json.loads(line)
            for sample in row["samples"]:
                if sample["uid"] not in identities:
                    continue
                if sample["node"] != identities[sample["uid"]]["spec"]["nodeName"]:
                    raise ValueError("Native sample node differs from retained Pod UID")
                # This sampler stamps after its measurements. The preceding
                # completed iteration is a conservative lower bound only when
                # the next fresh Running-Pod query still contains this UID.
                # A slow query's completion after deletion is not proof of a
                # reservation still being held until that completion.
                if sample["uid"] in previous_uids:
                    last_seen[sample["uid"]] = previous_at
                gpu = dict(sample["gpu"])
                if gpu.get("returncode") == 0:
                    output = io.StringIO()
                    writer = csv.writer(output)
                    driver = capacities[sample["node"]]["labels"].get("nebius.com/nvidia_driver_version", "unknown")
                    for fields in csv.reader(gpu.get("stdout", "").splitlines(), skipinitialspace=True):
                        if len(fields) != 7:
                            raise ValueError("Native GPU sample does not match the retained seven-column query")
                        uuid, name, utilization, memory_utilization, used, power, _clock = fields
                        writer.writerow((uuid, name, driver, "[N/A]", used, utilization, memory_utilization, power))
                    gpu["stdout"] = output.getvalue()
                cpu = dict(sample["cpu"])
                if cpu.get("returncode") == 0:
                    # cpu.max is a quota declaration, not a measured counter.
                    lines = [s for s in cpu.get("stdout", "").splitlines()
                             if not (len(s.split()) == 2 and (s.split()[0].isdigit() or s.split()[0] == "max"))]
                    cpu["stdout"] = "\n".join([*lines, "[N/A]"]) + "\n"
                target.write(json.dumps({"observed_at": row["at"], "pod_uid": sample["uid"],
                    "pod": sample["pod"], "node": sample["node"], "container": "runtime",
                    "labels": {"fs2.nebius.ai/operation-id": "native:" + operation_id},
                    "measurements": {"gpu": gpu, "cpu_memory_counters": cpu}}) + "\n")
            previous_at = row["at"]
            previous_uids = {s["uid"] for s in row["samples"] if s["uid"] in identities}
    return last_seen


def command_phases(result, request, data, gpus, hourly):
    requested = {s["id"]: s["command"] for s in request["jobs"][0]["steps"]}
    phases = []
    for command in result["commands"]:
        name = requested[command["step_id"]]
        item = {"command_id": command["step_id"]}
        source = reference(data / command["log"])
        if name == "mdrun":
            item["native_mdrun_counter_wall_seconds"] = measured_phase(
                native_counter_seconds((data / command["log"]).read_text(errors="replace")), gpus, hourly, source,
                "GROMACS native Wall t counter inside process wall; not pure integration or GPU computation")
            seconds, size = staging_measurements(command)
            if "mpi_input_staging" in command:
                item["mpi_input_staging_seconds"] = measured_phase(seconds, gpus, hourly, source,
                    "Recorded coordinator peer-staging wall before mdrun; not a sum of parallel peer clocks")
                item["mpi_input_staging_bytes"] = metric(size, "bytes", "measured", "Recorded peer bytes, not object inventory")
        elif name in {"energy", "eneconv", "trjcat", "check"}:
            item["analysis_command_seconds"] = measured_phase(command["wall_seconds"], gpus, hourly, source,
                "Analysis command process wall while all native Pod resources remain held")
        phases.append(item)
    return phase_summary([], phases)


def collect(receipt_path, samples, references, output, pricing_date):
    record = json.loads(receipt_path.read_text())
    if record.get("status") != "passed" or "absence observed" not in record.get("cleanup", ""):
        raise ValueError("Require a final passed native RDMA receipt with observed cleanup")
    md = record["native_md"]
    fixture = Path(md["fixture"])
    validate_source_bindings(record, fixture)
    request = json.loads((fixture / "request.json").read_text())
    workspace = receipt_path.parent / "workspace"
    result = json.loads((workspace / "result.json").read_text())
    pods = json.loads((receipt_path.parent / "pods.json").read_text())
    identities = {p["metadata"]["uid"]: p for p in pods}
    capacities = {n["node"]: n for n in json.loads((receipt_path.parent / "capacity-before.json").read_text())}
    gpus = sum(int(c["resources"]["requests"].get("nvidia.com/gpu", 0)) for p in pods for c in p["spec"]["containers"])
    if (len(pods) != request["nodes"] or len({p["spec"]["nodeName"] for p in pods}) != request["nodes"]
            or gpus != request["nodes"] * request["gpus_per_node"]
            or any(sum(int(c["resources"]["requests"].get("nvidia.com/gpu", 0)) for c in p["spec"]["containers"])
                   != request["gpus_per_node"] for p in pods)):
        raise ValueError("Observed native allocation differs from the exact request")
    verified = validate(workspace, request, gpus, True, False, 0)
    if verified != md["validation"] or verified["status"] != "passed":
        raise ValueError("Independent native revalidation differs from retained receipt")
    output.mkdir(parents=True, exist_ok=False)
    last_seen = project_samples(samples, output / "projected-samples.jsonl", md["native_operation_id"], identities, capacities)
    allocations = []
    for uid, pod in identities.items():
        labels = capacities[pod["spec"]["nodeName"]]["labels"]
        count = sum(int(c["resources"]["requests"].get("nvidia.com/gpu", 0)) for c in pod["spec"]["containers"])
        shape = preset_shape(labels.get("node.kubernetes.io/instance-type"), labels.get("nebius.com/resource-preset"),
                             labels.get("topology.kubernetes.io/region"), str(receipt_path.parent / "capacity-before.json"))
        scheduled = next((c["lastTransitionTime"] for c in pod["status"]["conditions"]
                          if c["type"] == "PodScheduled" and c["status"] == "True"), None)
        price = hourly_price(shape, count, references, pricing_date)
        allocations.append({"pod_uid": uid, "node": pod["spec"]["nodeName"], "gpu_count": count,
            "shape": shape, "price": price, "bounds": allocation_bounds({"scheduled_at": scheduled,
                "release_lower_bound": last_seen.get(uid), "release_upper_bound": record["finished_at"]})})
    rates = [a["price"]["allocated_share_per_hour"] for a in allocations]
    hourly = sum(rates) if rates and None not in rates else None
    work_ns = sum(r["simulated_ns"] for r in verified["repeats"])
    process = sum(c["wall_seconds"] for c in result["commands"] if "mdrun" in c["command"])
    summary = {"scope": "Native qualification only, outside public PostgreSQL accounting totals. Observation-bounded "
                        "whole-Pod occupancy includes communication gates/preparation/analysis; no VM invoice or pure GPU compute claim.",
        "receipt": reference(receipt_path), "samples": reference(samples), "image": record["image"],
        "input_tpr_sha256": verified["input_tpr_sha256"], "validation": verified,
        "allocations": allocations, "gpu_count": gpus,
        "observed_gpu_seconds": sum_bounds(allocations, "bounds", lambda a: a["gpu_count"]),
        "observed_allocation_share_usd": sum_bounds(allocations, "bounds",
            lambda a: a["price"]["allocated_share_per_hour"] / 3600 if a["price"]["allocated_share_per_hour"] is not None else None),
        "native_mdrun_process": measured_phase(process, gpus, hourly, str(workspace / "result.json"),
            "Sum of actual mdrun subprocess wall; includes native initialization and MPI launch, excludes separate analysis"),
        "native_supervisor_work_seconds": md["runtime_wall_seconds"],
        "native_supervisor_work_ns_per_day": work_ns * 86400 / md["runtime_wall_seconds"],
        "native_supervisor_scope": "Includes task input copies, worker workflow and local evidence retrieval; not public delivered throughput",
        "retained_inventory_bytes": sum(f["size_bytes"] for f in result["files"]),
        "network_transfer_total_bytes": None, "checkpoint_publication_seconds": None, "export_seconds": None,
        "local_checkpoint_step_replay": 0, "durable_remote_step_replay": None,
        "phases": command_phases(result, request, workspace / "data", gpus, hourly),
        "sampled_cpu_gpu": summarize(output / "projected-samples.jsonl"),
        "pricing_date": pricing_date, "pricing_scope": references["pricing"]}
    save(output / "report.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("receipt", "samples", "references", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--pricing-date", default="2026-10-05")
    args = parser.parse_args()
    collect(args.receipt, args.samples, json.loads(args.references.read_text()), args.output, args.pricing_date)
    print(json.dumps(reference(args.output / "report.json")))


if __name__ == "__main__":
    main()

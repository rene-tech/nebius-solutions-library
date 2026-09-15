#!/usr/bin/env python3
"""Bound whole Protenix pod GPU reservations using saved evidence only.

This script does not contact Kubernetes, start work, or modify other reports.
Request wall time is not a measurement of GPU busy time.
"""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVALUATION = HERE.parent
SOURCES = {}


def read(path):
    data = path.read_bytes()
    SOURCES[str(path.relative_to(EVALUATION))] = {
        "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)
    }
    return json.loads(data)


def seconds(timestamp):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def scheduled(pod):
    rows = [condition for condition in pod["status"]["conditions"]
            if condition["type"] == "PodScheduled" and condition["status"] == "True"]
    assert len(rows) == 1
    return seconds(rows[0]["lastTransitionTime"])


def gpu_request(pod):
    def count(container):
        return int(container.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0))
    return max(sum(map(count, pod["spec"]["containers"])),
               max(map(count, pod["spec"].get("initContainers", [])), default=0))


def union_duration(rows):
    intervals = sorted((seconds(row["created_at"]),
                        seconds(row["created_at"]) + row["wall_seconds"]) for row in rows)
    total = 0.0
    end = float("-inf")
    for start, stop in intervals:
        total += max(0.0, stop - max(start, end))
        end = max(end, stop)
    return total


def main():
    analysis = read(HERE / "analysis.json")
    events = read(HERE / "raw/root-protenix-lifecycle-events.stdout")["items"]
    absence = read(HERE / "raw/root-protenix-final-pods.stdout")
    absence_receipt = read(HERE / "raw/root-protenix-final-pods.command.json")
    assert absence["items"] == [] and absence_receipt["return_code"] == 0
    final_absence_upper = seconds(absence_receipt["created_at"]) + absence_receipt["elapsed_seconds"]
    nodes = read(EVALUATION / "coverage/inventory/nodes.json")["items"]
    node_capacity = {node["metadata"]["name"]: int(node["status"]["allocatable"].get("nvidia.com/gpu", 0))
                     for node in nodes}

    mapping = {
        "protenix-current-h100": {
            "snapshot": "persistent-pod.stdout",
            "cohorts": ["current-h100", "current-persistent-h100"],
            "next_pod": "protenix-bir-h100",
        },
        "protenix-bir-prototype": {
            "snapshot": "prototype-pod.stdout",
            "cohorts": ["prototype-eager", "prototype-complex"],
            "next_snapshot": "snapshot/lifecycle/fs2-bioir-snapshot-boltz2-donor-auto-before-delete.json",
        },
        "protenix-bir-h100": {
            "snapshot": "candidate-graph-pod.stdout",
            "cohorts": ["bir-graphs-h100", "bir-premature-harness"],
            "next_pod": "protenix-bir-eager-h100",
            "cleanup": "candidate-graph-cleanup.command.json",
            "cleanup_retry": "candidate-graph-cleanup-corrected.command.json",
        },
        "protenix-bir-eager-h100": {
            "snapshot": "candidate-eager-pod.stdout",
            "cohorts": ["bir-eager-h100"],
            "next_pod": "protenix-bir-primary-h100",
            "cleanup": "candidate-eager-cleanup.command.json",
        },
        "protenix-bir-primary-h100": {
            "snapshot": "candidate-primary-pod.stdout",
            "cohorts": ["bir-primary-h100", "bir-features-h100"],
            "next_pod": "protenix-native-features-h100",
            "cleanup": "candidate-primary-cleanup.command.json",
        },
        "protenix-native-features-h100": {
            "snapshot": "native-features-pod.stdout",
            "cohorts": ["native-features-h100"],
            "cleanup": "native-features-cleanup.command.json",
        },
    }
    pods = {name: read(HERE / "raw" / config["snapshot"]) for name, config in mapping.items()}
    assert all(pod["metadata"]["name"] == name for name, pod in pods.items())
    cohort_to_pod = {cohort: name for name, config in mapping.items() for cohort in config["cohorts"]}
    grouped = defaultdict(list)
    for row in analysis["attempts"]:
        cohort = row["evidence"].split("/")[1]
        assert cohort in cohort_to_pod, cohort
        grouped[cohort_to_pod[cohort]].append(row)

    result = []
    for name, config in mapping.items():
        pod = pods[name]
        uid = pod["metadata"]["uid"]
        node = pod["spec"]["nodeName"]
        gpu_count = gpu_request(pod)
        assert gpu_count == 1 and node_capacity[node] == 1
        start = scheduled(pod)
        killing = [event for event in events if event.get("reason") == "Killing"
                   and event.get("involvedObject", {}).get("uid") == uid]
        assert len(killing) == 1, (name, len(killing))
        kill = killing[0]
        stop_lower = seconds(kill.get("eventTime") or kill["firstTimestamp"])
        cleanup = read(HERE / "raw" / config["cleanup"]) if config.get("cleanup") else None
        cleanup_retry = read(HERE / "raw" / config["cleanup_retry"]) if config.get("cleanup_retry") else None
        if config.get("next_pod") or config.get("next_snapshot"):
            successor = pods[config["next_pod"]] if config.get("next_pod") else read(EVALUATION / config["next_snapshot"])
            assert successor["spec"]["nodeName"] == node and gpu_request(successor) == 1
            stop_upper = scheduled(successor)
            upper_padding = 1.0
            upper_source = "next_single_GPU_pod_scheduled_same_single_GPU_node"
            successor_identity = {"name": successor["metadata"]["name"], "uid": successor["metadata"]["uid"]}
        else:
            assert cleanup and cleanup["return_code"] == 0 and "--wait=true" in cleanup["command"]
            stop_upper = seconds(cleanup["created_at"]) + cleanup["elapsed_seconds"]
            upper_padding = 0.0
            upper_source = "successful_wait_true_delete_command_completion"
            successor_identity = None
        assert start < stop_lower <= stop_upper <= final_absence_upper
        all_rows = grouped[name]
        requests = [row for row in all_rows if row.get("phase") != "cpu-prepare"]
        preparation = [row for row in all_rows if row.get("phase") == "cpu-prepare"]
        valid = sum(row["status"] == "passed" for row in requests)
        for row in all_rows:
            assert start <= seconds(row["created_at"])
            assert seconds(row["created_at"]) + row["wall_seconds"] <= stop_lower + 1.0
        request_wall = sum(row["wall_seconds"] for row in requests)
        request_union = union_duration(requests)
        # One-second padding accommodates timestamp serialization granularity.
        observed_lower = (stop_lower - start) * gpu_count
        observed_upper = (stop_upper - start) * gpu_count
        padded_lower = max(0.0, observed_lower - gpu_count)
        padded_upper = observed_upper + upper_padding * gpu_count
        cohorts = []
        for cohort in config["cohorts"]:
            rows = [row for row in requests if row["evidence"].split("/")[1] == cohort]
            cohorts.append({"cohort": cohort, "pod_uid": uid,
                            "attempts": len(rows), "valid_requests": sum(row["status"] == "passed" for row in rows),
                            "recorded_request_wall_seconds": sum(row["wall_seconds"] for row in rows),
                            "evidence": "protenix/raw/" + cohort + "/attempts.jsonl"})
        record = {
            "pod": name, "uid": uid, "node": node, "gpu_count": gpu_count,
            "source_pod_snapshot": "protenix/raw/" + config["snapshot"],
            "cohorts": cohorts, "attempts": len(requests), "valid_requests": valid,
            "failed_requests": len(requests) - valid,
            "cpu_preparation_records_excluded_from_inference_count": len(preparation),
            "cpu_preparation_wall_seconds": sum(row["wall_seconds"] for row in preparation),
            "pod_scheduled_at": iso(start), "release_lower_observation_at": iso(stop_lower),
            "release_upper_observation_at": iso(stop_upper),
            "release_lower_evidence": {"reason": kill["reason"], "message": kill["message"],
                                       "uid": kill["involvedObject"]["uid"],
                                       "source": "protenix/raw/root-protenix-lifecycle-events.stdout"},
            "release_upper_basis": upper_source, "successor": successor_identity,
            "cleanup_command_receipt": cleanup,
            "cleanup_command_receipts": [
                {"evidence": "protenix/raw/" + config[key], "receipt": receipt}
                for key, receipt in [("cleanup", cleanup), ("cleanup_retry", cleanup_retry)] if receipt
            ],
            "cleanup_retry_receipt": cleanup_retry,
            "cleanup_command_is_confirmed_release": bool(cleanup and cleanup["return_code"] == 0 and "--wait=true" in cleanup["command"]),
            "allocation_gpu_seconds_at_recorded_timestamp_resolution": {"lower": observed_lower, "upper": observed_upper},
            "allocation_gpu_seconds_bounds": {"lower": padded_lower, "upper": padded_upper},
            "recorded_request_wall_seconds": request_wall,
            "recorded_request_interval_union_seconds": request_union,
            "sum_minus_union_seconds": request_wall - request_union,
            "occupied_outside_recorded_requests_gpu_seconds_bounds": {
                "lower": padded_lower - request_union * gpu_count,
                "upper": padded_upper - request_union * gpu_count,
            },
            "gpu_seconds_per_valid_request_bounds": {"lower": padded_lower / valid, "upper": padded_upper / valid},
            "valid_requests_per_allocated_gpu_hour_bounds": {"lower": 3600 * valid / padded_upper,
                                                           "upper": 3600 * valid / padded_lower},
            "limitations": ["Allocation outside recorded requests is not measured GPU idle time.",
                            "No per-cohort division of shared pod loading/operator gaps is inferred."],
        }
        if config.get("next_snapshot"):
            record["limitations"].append("Prototype upper bound is deliberately loose: the next recorded pod is the later Boltz snapshot donor, not a release observation.")
        if cleanup and "--wait=false" in cleanup["command"]:
            record["limitations"].append("Asynchronous cleanup receipt does not establish GPU release; the next same-node schedule supplies the upper bound.")
        if cleanup and cleanup["return_code"] != 0:
            record["limitations"].append("Initial cleanup command failed due to resource syntax. The retained corrected retry returned code0 with wait=false, confirming deletion acceptance but not release. Later Killing/successor evidence establishes the unchanged bounds.")
        result.append(record)

    total_valid = sum(row["valid_requests"] for row in result)
    total_attempts = sum(row["attempts"] for row in result)
    assert (total_valid, total_attempts) == (65, 66)
    lower = sum(row["allocation_gpu_seconds_bounds"]["lower"] for row in result)
    upper = sum(row["allocation_gpu_seconds_bounds"]["upper"] for row in result)
    request_wall = sum(row["recorded_request_wall_seconds"] for row in result)
    request_union = sum(row["recorded_request_interval_union_seconds"] for row in result)
    prep_wall = sum(row["cpu_preparation_wall_seconds"] for row in result)
    output = {
        "schema": "fs2-protenix-allocation-bounds/v1",
        "method": "Offline evidence-only whole scheduled-pod GPU reservation accounting; no GPU execution or cluster access.",
        "clock_assumption": "Recorded cluster/client UTC clocks are comparable. Bounds do not cover unmeasured clock skew. Killing is treated as stopping initiation, not release completion.",
        "timestamp_resolution_policy": "Primary bounds subtract one second per pod from the recorded lower duration; successor-schedule upper bounds add one second. Successful wait=true completion is already a later client-clock upper endpoint. Raw recorded-time bounds are also retained.",
        "denominator": "Artifact-valid inference requests, not samples, generated structures, clinically useful results, CPU prepare calls or only warm successes. Includes exploratory/private-seed candidates and batch requests.",
        "scope": "Six Protenix benchmark/prototype pod UIDs only; snapshot experiments and model image-build GPU resources are not included. Parallel reservations on the two distinct nodes are added as GPU-seconds, not merged into wall time.",
        "totals": {
            "pod_count": len(result), "attempts": total_attempts, "valid_requests": total_valid,
            "failed_requests": total_attempts - total_valid, "cpu_preparation_records": 3,
            "allocation_gpu_seconds_at_recorded_timestamp_resolution": {
                key: sum(row["allocation_gpu_seconds_at_recorded_timestamp_resolution"][key] for row in result)
                for key in ["lower", "upper"]},
            "allocation_gpu_seconds_bounds": {"lower": lower, "upper": upper},
            "allocated_gpu_hours_bounds": {"lower": lower / 3600, "upper": upper / 3600},
            "gpu_seconds_per_valid_request_bounds": {"lower": lower / total_valid, "upper": upper / total_valid},
            "valid_requests_per_allocated_gpu_hour_bounds": {"lower": 3600 * total_valid / upper,
                                                           "upper": 3600 * total_valid / lower},
            "recorded_inference_wall_seconds_including_failure": request_wall,
            "recorded_inference_interval_union_seconds": request_union,
            "cpu_preparation_wall_seconds": prep_wall,
            "occupied_outside_recorded_requests_gpu_seconds_bounds": {"lower": lower - request_union,
                                                                     "upper": upper - request_union},
            "allocated_outside_all_recorded_request_and_prepare_intervals_gpu_seconds_bounds": {
                "lower": lower - sum(union_duration(grouped[name]) for name in mapping),
                "upper": upper - sum(union_duration(grouped[name]) for name in mapping)},
        },
        "pods": result,
        "final_absence": {"observed_no_pods": True, "command_completion_upper": iso(final_absence_upper),
                          "evidence": "protenix/raw/root-protenix-final-pods.stdout",
                          "receipt": "protenix/raw/root-protenix-final-pods.command.json"},
        "limitations": [
            "These are resource-reservation bounds, not profiler-measured GPU occupied/busy seconds or GPU utilization.",
            "Request wall time includes CPU work, wrapper, validation and I/O; subtracting it does not isolate GPU idle time.",
            "Unrecorded allocation includes initialization, build/compile work inside each pod, operator gaps, request preparation, collection and teardown. No idle-time attribution is inferred.",
            "Successor scheduling proves an upper bound only under the frozen single-GPU node and exclusive one-GPU pod allocation assumptions.",
            "Whole-pod accounting includes the retained failed request and all intervals between scheduling and release bounds. Mixed cohorts sharing one UID cannot receive independently measured full-allocation costs.",
            "Prototype's broad upper interval includes time that may already have been released; it is not asserted to be truly allocated until its successor.",
            "No production serving price, monetary rate or steady-state cost forecast is implied by this small evaluation's full allocation cost.",
        ],
        "evidence_sha256": SOURCES,
    }
    (HERE / "allocation.json").write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(json.dumps(output["totals"], indent=2))


if __name__ == "__main__":
    main()

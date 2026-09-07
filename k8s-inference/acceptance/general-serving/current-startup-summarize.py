#!/usr/bin/env python3
"""Build an auditable redacted report, keeping new-node and reused-node cohorts separate."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics
from datetime import datetime


def delta(end, start):
    if not end or not start:
        return None
    return (datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()


def cohort(runs):
    keys = ("pod_to_ready_seconds", "pod_to_application_ready_seconds", "process_to_ready_seconds",
            "process_to_kubernetes_ready_seconds", "first_request_seconds", "pod_to_first_valid_output_seconds")
    result = {"n": len(runs), "trials": runs, "statistics": {}}
    for key in keys:
        values = [r[key] for r in runs if r.get(key) is not None]
        if values:
            result["statistics"][key] = {"n": len(values), "median": statistics.median(values), "minimum": min(values), "maximum": max(values)}
    return result


def read_runs(root, offset=0):
    receipt = json.loads((root / "receipt.json").read_text())
    if receipt["result"] != "PASS":
        raise ValueError("cannot summarize an incomplete/failed receipt as successful")
    template = json.loads((root / "source-template.json").read_text())
    runs = []
    for raw in receipt["runs"]:
        if raw["status"] != "PASS" or not raw.get("validation", {}).get("passed") or not raw.get("cleanup_completed_at"):
            raise ValueError("trial validation or cleanup is incomplete")
        rep = raw["repetition"]
        pod = json.loads((root / f"rep-{rep}-pod-final.json").read_text())
        node = json.loads((root / f"rep-{rep}-node.json").read_text())
        events = json.loads((root / f"rep-{rep}-events.json").read_text())["items"]
        for c in pod["status"]["containerStatuses"] + pod["status"].get("initContainerStatuses", []):
            if c.get("restartCount", 0):
                raise ValueError("a measured pod restarted")
        manifest = json.loads((root / f"rep-{rep}-deployment.json").read_text())
        if manifest["spec"]["template"]["spec"] != template["spec"]:
            raise ValueError("benchmark altered the source runtime pod spec")
        timeline_keys = ("launch_at", "pod_created_at", "runtime_started_at", "application_ready_at", "ready_at", "first_request_at", "first_response_at", "cleanup_completed_at")
        timeline = {k: raw.get(k) for k in timeline_keys}
        timeline["node_created_at"] = node["metadata"]["creationTimestamp"]
        timeline["node_ready_at"] = next((c["lastTransitionTime"] for c in node["status"]["conditions"] if c["type"] == "Ready" and c["status"] == "True"), None)
        timeline["pod_scheduled_at"] = next((c["lastTransitionTime"] for c in pod["status"]["conditions"] if c["type"] == "PodScheduled"), None)
        timeline["scaleup_requested_at"] = next((e.get("firstTimestamp") for e in events if e["reason"] == "TriggeredScaleUp"), None)
        timeline["image_pull_started_at"] = next((e.get("firstTimestamp") for e in events if e["reason"] == "Pulling"), None)
        timeline["image_pull_finished_at"] = next((e.get("firstTimestamp") for e in events if e["reason"] == "Pulled" and "Successfully pulled" in e["message"]), None)
        init = pod["status"]["initContainerStatuses"][0]["state"]["terminated"]
        timeline["localizer_started_at"] = init["startedAt"]
        timeline["localizer_finished_at"] = init["finishedAt"]
        pull_message = next((e["message"] for e in events if e["reason"] == "Pulled" and "Successfully pulled" in e["message"]), "image already present")
        pull_match = re.search(r" in (?:(\d+)m)?([0-9.]+)s", pull_message)
        image_pull_seconds = (int(pull_match[1] or 0) * 60 + float(pull_match[2])) if pull_match else None
        localizer_logs = (root / f"rep-{rep}-localize-model.log").read_text()
        localizer = next((json.loads(line.split(" ", 1)[1]) for line in localizer_logs.splitlines() if '"outcome"' in line), None)
        runtime_name = "vllm" if receipt["model"] == "qwen3-8b" else "vllm-omni"
        runtime_logs = re.sub(r"\x1b\[[0-9;]*m", "", (root / f"rep-{rep}-{runtime_name}.log").read_text())
        version = re.search(r"\bversion ([0-9][A-Za-z0-9.+_-]*)", runtime_logs)
        weights = re.search(r"Loading weights took ([0-9.]+) seconds", runtime_logs)
        compilation = re.search(r"torch\.compile took ([0-9.]+) s in total", runtime_logs)
        field_keys = ("pod_to_ready_seconds", "pod_to_application_ready_seconds", "process_to_ready_seconds", "process_to_kubernetes_ready_seconds", "first_request_seconds", "pod_to_first_valid_output_seconds", "validation", "generation_timings_ms", "runtime_image_id")
        run = {k: raw[k] for k in field_keys if k in raw}
        run["runtime_image_digest"] = run.pop("runtime_image_id").split("@")[-1]
        gpu = [v.strip() for v in raw["gpu_inventory"].strip().split(",")]
        run.update({"trial": offset + rep, "passed": True, "timeline": timeline,
                    "runtime": {"vllm_version": version[1] if version else None,
                                "weight_loading_seconds": float(weights[1]) if weights else None,
                                "reported_torch_compile_seconds": float(compilation[1]) if compilation else None},
                    "hardware": {"gpu": gpu[0], "driver": gpu[2], "memory": gpu[3], "compute_capability": gpu[4],
                                 "pool": node["metadata"]["labels"].get("accelerator.fs2.nebius/pool-id"),
                                 "capacity_type": node["metadata"]["labels"].get("capacity.fs2.nebius/type")},
                    "phases": {"pod_to_scheduled_seconds": delta(timeline["pod_scheduled_at"], timeline["pod_created_at"]),
                               "image_pull_seconds": image_pull_seconds,
                               "init_container_seconds": delta(init["finishedAt"], init["startedAt"]),
                               "init_completion_to_runtime_start_seconds": delta(timeline["runtime_started_at"], init["finishedAt"])},
                    "cache": {"image_already_present": image_pull_seconds is None,
                              "node_created_after_trial_start": delta(timeline["node_created_at"], timeline["launch_at"]) > 0,
                              "localizer": localizer, "runtime_cache": "fresh emptyDir", "host_page_cache": "uncontrolled"},
                    "source_pod_spec_unchanged": True,
                    "raw_artifact_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(root.glob(f"rep-{rep}-*")) if path.is_file()}})
        runs.append(run)
    annotations = template["metadata"]["annotations"]
    safe_env = {"FS2_MODEL_PRECISION", "HF_HUB_DISABLE_TELEMETRY", "HF_HUB_OFFLINE", "HF_HOME", "HOME",
                "VLLM_NO_USAGE_STATS", "XDG_CACHE_HOME", "TRANSFORMERS_OFFLINE", "VLLM_CACHE_ROOT",
                "TRITON_CACHE_DIR", "POD_NAME", "PYTHONDONTWRITEBYTECODE"}
    contract = {"model_revision": annotations.get("fs2.nebius/model-revision"),
                "model_content_digest": annotations.get("fs2.nebius/model-content-digest"),
                "runtime_image_digest": annotations.get("fs2.nebius/runtime-image-digest"),
                "source_template_sha256": receipt["source_template_sha256"],
                "fixture_file_sha256": receipt["fixture_file_sha256"], "fixture_case": receipt["fixture_case"],
                "runtime_containers": [{"name": c["name"], "args": c.get("args"), "resources": c["resources"],
                                        "env": [e for e in c.get("env", []) if e["name"] in safe_env]}
                                       for c in template["spec"]["containers"]]}
    return runs, contract


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--qwen", type=Path, required=True)
    p.add_argument("--cosmos", type=Path, required=True)
    p.add_argument("--cosmos-supplement", type=Path, required=True)
    p.add_argument("--media-validation", type=Path, required=True)
    args = p.parse_args()
    qwen, qcontract = read_runs(args.qwen)
    cosmos, ccontract = read_runs(args.cosmos)
    supplement, extra_contract = read_runs(args.cosmos_supplement, offset=len(cosmos))
    if ccontract != extra_contract:
        raise ValueError("supplemental Cosmos trial used a different contract")
    cosmos += supplement
    cached = [r for r in cosmos if r["cache"]["image_already_present"] and not r["cache"]["node_created_after_trial_start"]]
    new_node = [r for r in cosmos if r not in cached]
    if len(qwen) < 3 or len(cached) < 3:
        raise ValueError("three comparable cached-image trials are required per model")
    media = json.loads(args.media_validation.read_text())
    if media["result"] != "PASS" or len(media["outputs"]) != len(cosmos):
        raise ValueError("every Cosmos output must fully decode")
    if sorted(r["validation"]["sha256"] for r in cosmos) != sorted(r["sha256"] for r in media["outputs"]):
        raise ValueError("decoded media do not match the measured responses")
    report = {"schema": "fs2-serve.nebius.ai/current-startup-report/v1", "date": "2026-09-07", "result": "PASS",
              "scope": "isolated replicas cloned from current live H100 serving templates",
              "request_to_ready_seconds": None, "request_to_ready_reason": "No public activation request was used",
              "qwen3-8b": {"contract": qcontract, "cached_image_fresh_process": cohort(qwen)},
              "cosmos3-nano": {"contract": ccontract, "cached_image_fresh_process": cohort(cached), "new_node_image_pull": cohort(new_node)},
              "full_media_decode": media,
              "limitations": ["At least three trials per comparable cohort; no p95 estimate",
                              "New-node Cosmos sample has n=1 and is excluded from cached-image statistics",
                              "Shared model PVC cache is retained; host page-cache residency was not forced",
                              "Each runtime has a fresh emptyDir compile cache and fresh GPU process",
                              "Container/Kubernetes timestamps have one-second granularity",
                              "First-output clock includes readiness observation and port-forward setup",
                              "Dedicated GPU allocations share nodes with other authorized workloads"],
              "private_artifact_roots": [str(args.qwen), str(args.cosmos), str(args.cosmos_supplement)]}
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

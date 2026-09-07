#!/usr/bin/env python3
"""Measure isolated replicas of live serving templates without changing live models.

Each repetition creates a new Deployment and emptyDir runtime cache, retains the
exact image, arguments, resources, model PVC, probes and node selection, validates
the first fixed fixture, then removes only its UID-checked benchmark deployment.
No public request-to-ready claim is made by this direct replica benchmark.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import time
from datetime import UTC, datetime
from urllib.request import Request, urlopen


def utc():
    return datetime.now(UTC).isoformat()


def seconds(end, start):
    return (datetime.fromisoformat(end.replace("Z", "+00:00")) -
            datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kubeconfig", required=True)
    p.add_argument("--context", required=True)
    p.add_argument("--model", choices=["qwen3-8b", "cosmos3-nano"], required=True)
    p.add_argument("--source-deployment", required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--repetitions", type=int, default=3)
    p.add_argument("--namespace", default="fs2-models")
    p.add_argument("--run-id", default="r20260907")
    p.add_argument("--timeout", type=int, default=3000)
    args = p.parse_args()
    if args.repetitions < 1:
        p.error("repetitions must be positive; compare at least three trials per cache cohort")
    args.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    result_path = args.output_dir / "receipt.json"
    if result_path.exists():
        raise FileExistsError(result_path)
    cmd = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context]
    ncmd = cmd + ["-n", args.namespace]

    def kubectl(*parts, payload=None, cluster=False):
        return subprocess.check_output((cmd if cluster else ncmd) + list(parts),
                                       input=payload, stderr=subprocess.PIPE, timeout=60).decode()

    def save(name, value):
        dest = args.output_dir / name
        dest.write_text(value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True) + "\n")
        dest.chmod(0o600)

    source = json.loads(kubectl("get", "deployment", args.source_deployment, "-o", "json"))
    source_template = source["spec"]["template"]
    save("source-template.json", source_template)
    solution = Path(__file__).resolve().parents[2]
    fixture_path = solution / "catalog/runtime/validators/assets" / (args.model + ".json")
    fixture = json.loads(fixture_path.read_text())
    case = fixture["requests"][0]
    report = {"schema": "fs2-serve.nebius.ai/isolated-startup-benchmark/v1",
              "started_at": utc(), "model": args.model, "source_deployment": args.source_deployment,
              "source_deployment_uid": source["metadata"]["uid"],
              "source_template_sha256": hashlib.sha256(json.dumps(source_template, sort_keys=True).encode()).hexdigest(),
              "fixture_file_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
              "fixture_case": case, "context": args.context, "namespace": args.namespace,
              "cache_contract": {"model_weights": "existing shared PVC; cache residency unforced",
                                 "runtime_compile_cache": "new emptyDir per repetition",
                                 "image": "IfNotPresent; pull events recorded per repetition",
                                 "host_page_cache": "not evicted; residency unknown"},
              "clock_contract": {"request_to_ready_seconds": None,
                                 "reason": "isolated replica; no public activation request",
                                 "pod_to_ready": "Kubernetes pod creationTimestamp to Ready lastTransitionTime",
                                 "process_to_ready": "runtime container startedAt to first application-startup-complete log",
                                 "first_valid_output": "direct first fixed fixture after readiness; separate from startup"},
              "statistics_note": "Raw-run statistics can mix cache conditions; use current-startup-summarize.py for cohort comparisons",
              "runs": [], "result": "RUNNING"}
    save("receipt.json", report)
    for rep in range(1, args.repetitions + 1):
        name = f"startup-{args.model}-{args.run_id}-{rep}"
        template = copy.deepcopy(source_template)
        labels = {k: v for k, v in template["metadata"].get("labels", {}).items() if k.startswith("kueue.")}
        labels.update({"benchmark.fs2.nebius/run": args.run_id, "benchmark.fs2.nebius/replica": name,
                       "app.kubernetes.io/name": name, "app.kubernetes.io/managed-by": "startup-benchmark"})
        template["metadata"]["labels"] = labels
        template["metadata"].pop("creationTimestamp", None)
        manifest = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": args.namespace,
                    "labels": {"benchmark.fs2.nebius/run": args.run_id}},
                    "spec": {"replicas": 1, "selector": {"matchLabels": {"benchmark.fs2.nebius/replica": name}},
                             "template": template}}
        save(f"rep-{rep}-deployment.json", manifest)
        payload = json.dumps(manifest).encode()
        kubectl("create", "--dry-run=client", "-f", "-", payload=payload)
        run = {"repetition": rep, "deployment": name, "launch_at": utc(), "status": "RUNNING"}
        report["runs"].append(run)
        save("receipt.json", report)
        created = json.loads(kubectl("create", "-f", "-", "-o", "json", payload=payload))
        run["deployment_uid"] = created["metadata"]["uid"]
        run["deployment_created_at"] = created["metadata"]["creationTimestamp"]
        print(json.dumps({"event": "replica_created", "model": args.model, **run}), flush=True)
        pod = None
        forward = None
        try:
            deadline = time.monotonic() + args.timeout
            previous_status = None
            while time.monotonic() < deadline:
                pods = json.loads(kubectl("get", "pods", "-l", "benchmark.fs2.nebius/replica=" + name, "-o", "json"))["items"]
                if pods:
                    pod = pods[0]
                    save(f"rep-{rep}-pod.json", pod)
                    phase = pod.get("status", {}).get("phase")
                    statuses = pod.get("status", {}).get("containerStatuses", [])
                    state = (phase, tuple(s.get("ready") for s in statuses))
                    if state != previous_status:
                        print(json.dumps({"event": "pod_state", "at": utc(), "model": args.model,
                                          "repetition": rep, "phase": phase, "node": pod["spec"].get("nodeName"),
                                          "states": [s.get("state") for s in statuses]}), flush=True)
                        previous_status = state
                    conditions = {c["type"]: c for c in pod.get("status", {}).get("conditions", [])}
                    if conditions.get("Ready", {}).get("status") == "True":
                        run["ready_at"] = conditions["Ready"]["lastTransitionTime"]
                        break
                    if any(s.get("restartCount", 0) for s in statuses):
                        raise RuntimeError("runtime restarted during measured startup")
                    if phase == "Failed":
                        raise RuntimeError("pod failed during measured startup")
                time.sleep(1)
            else:
                raise TimeoutError("replica startup deadline exceeded")
            run["pod_name"] = pod["metadata"]["name"]
            run["pod_uid"] = pod["metadata"]["uid"]
            run["pod_created_at"] = pod["metadata"]["creationTimestamp"]
            run["node"] = pod["spec"]["nodeName"]
            run["pod_to_ready_seconds"] = seconds(run["ready_at"], run["pod_created_at"])
            run["launch_to_ready_seconds"] = seconds(run["ready_at"], run["launch_at"])
            runtime_name = "vllm" if args.model == "qwen3-8b" else "vllm-omni"
            runtime_status = next(s for s in pod["status"]["containerStatuses"] if s["name"] == runtime_name)
            run["runtime_started_at"] = runtime_status["state"]["running"]["startedAt"]
            run["runtime_image_id"] = runtime_status["imageID"]
            run["process_to_kubernetes_ready_seconds"] = seconds(run["ready_at"], run["runtime_started_at"])
            save("receipt.json", report)
            with socket.socket() as unused_port:
                unused_port.bind(("127.0.0.1", 0))
                port = unused_port.getsockname()[1]
            target = 8000 if args.model == "qwen3-8b" else 8080
            with (args.output_dir / f"rep-{rep}-port-forward.log").open("w") as log:
                forward = subprocess.Popen(ncmd + ["port-forward", "pod/" + run["pod_name"], f"{port}:{target}"], stdout=log, stderr=log)
                for _ in range(100):
                    if forward.poll() is not None:
                        raise RuntimeError("pod port-forward exited before validation")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise TimeoutError("pod port-forward did not become available")
                path = "/v1/chat/completions" if args.model == "qwen3-8b" else "/generate"
                request = Request(f"http://127.0.0.1:{port}" + path, data=json.dumps(case["request"]).encode(),
                                  headers={"Content-Type": "application/json"})
                run["first_request_at"] = utc()
                started = time.monotonic()
                with urlopen(request, timeout=1800) as response:
                    result = json.load(response)
                run["first_response_at"] = utc()
                run["first_request_seconds"] = time.monotonic() - started
                if args.model == "qwen3-8b":
                    content = result["choices"][0]["message"]["content"]
                    if content.strip() != case["oracle"]["expected"]:
                        raise ValueError("first text did not match exact oracle")
                    run["validation"] = {"passed": True, "output_sha256": hashlib.sha256(content.encode()).hexdigest(),
                                         "usage": result["usage"]}
                else:
                    spec = importlib.util.spec_from_file_location("validator", solution / "catalog/runtime/validators/validate_cosmos3_nano.py")
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    run["validation"] = {"passed": True, **module.validate_response(result, case, case["id"])}
                    run["generation_timings_ms"] = result["timings_ms"]
                    media = base64.b64decode(result["data_base64"], validate=True)
                    media_path = args.output_dir / f"rep-{rep}-first-output.mp4"
                    media_path.write_bytes(media)
                    media_path.chmod(0o600)
                    if subprocess.call(["which", "ffprobe"], stdout=subprocess.DEVNULL) == 0:
                        probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-of", "json", str(media_path)]))
                        run["media_probe"] = probe
                        stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
                        if (stream["width"], stream["height"], int(stream["nb_read_frames"])) != (448, 256, 25):
                            raise ValueError("decoded video dimensions/frame count differ")
                run["pod_to_first_valid_output_seconds"] = seconds(run["first_response_at"], run["pod_created_at"])
            run["gpu_inventory"] = kubectl("exec", run["pod_name"], "-c", runtime_name, "--", "nvidia-smi", "--query-gpu=name,uuid,driver_version,memory.total,compute_cap", "--format=csv,noheader")
            run["status"] = "PASS"
        except Exception as error:
            run["status"] = "FAIL"
            run["error"] = {"type": type(error).__name__, "message": str(error)[:1000]}
        finally:
            if forward is not None:
                forward.terminate()
                forward.wait(timeout=15)
            if pod:
                save(f"rep-{rep}-pod-final.json", json.loads(kubectl("get", "pod", pod["metadata"]["name"], "-o", "json")))
                events = json.loads(kubectl("get", "events", "--field-selector", "involvedObject.uid=" + pod["metadata"]["uid"], "-o", "json"))
                save(f"rep-{rep}-events.json", events)
                run["image_pull_events"] = [{"reason": e["reason"], "message": e["message"], "firstTimestamp": e.get("firstTimestamp"), "lastTimestamp": e.get("lastTimestamp")} for e in events["items"] if e["reason"] in ("Pulling", "Pulled")]
                if pod["spec"].get("nodeName"):
                    node = json.loads(kubectl("get", "node", pod["spec"]["nodeName"], "-o", "json", cluster=True))
                    save(f"rep-{rep}-node.json", node)
                    run["node_created_at"] = node["metadata"]["creationTimestamp"]
                for c in pod["spec"].get("initContainers", []) + pod["spec"]["containers"]:
                    try:
                        logs = kubectl("logs", pod["metadata"]["name"], "-c", c["name"], "--timestamps")
                        save(f"rep-{rep}-{c['name']}.log", logs)
                        markers = [line for line in logs.splitlines() if "Application startup complete" in line]
                        if markers and c["name"] in ("vllm", "vllm-omni") and "runtime_started_at" in run:
                            run["application_ready_at"] = markers[0].split()[0]
                            run["process_to_ready_seconds"] = seconds(run["application_ready_at"], run["runtime_started_at"])
                            run["pod_to_application_ready_seconds"] = seconds(run["application_ready_at"], run["pod_created_at"])
                            run["readiness_marker"] = markers[0]
                    except subprocess.CalledProcessError:
                        pass
            owned = json.loads(kubectl("get", "deployment", name, "-o", "json"))
            if owned["metadata"]["uid"] != run["deployment_uid"]:
                raise RuntimeError("refusing cleanup: deployment UID changed")
            kubectl("delete", "deployment", name, "--wait=false")
            run["cleanup_requested_at"] = utc()
            save("receipt.json", report)
        print(json.dumps({"event": "replica_complete", "model": args.model, **run}), flush=True)
        if run["status"] != "PASS":
            report["result"] = "FAIL"
            save("receipt.json", report)
            return 1
        if pod:
            try:
                kubectl("wait", "--for=delete", "pod/" + pod["metadata"]["name"], "--timeout=55s")
                run["cleanup_completed_at"] = utc()
            except subprocess.CalledProcessError:
                raise RuntimeError("previous benchmark pod did not terminate")
    report["statistics"] = {}
    for key in ("pod_to_ready_seconds", "process_to_ready_seconds", "process_to_kubernetes_ready_seconds", "first_request_seconds", "pod_to_first_valid_output_seconds"):
        values = [run[key] for run in report["runs"] if key in run]
        if values:
            report["statistics"][key] = {"n": len(values), "median": statistics.median(values), "minimum": min(values), "maximum": max(values), "values": values}
    report["result"] = "PASS"
    report["completed_at"] = utc()
    save("receipt.json", report)
    print(json.dumps({"model": args.model, "result": "PASS", "statistics": report["statistics"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

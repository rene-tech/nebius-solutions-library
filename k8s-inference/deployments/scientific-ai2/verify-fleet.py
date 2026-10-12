#!/usr/bin/env python3
"""Bounded GPU infrastructure qualification through the NEW fleet's public API.

Uses system/qa, not customer/admin keys. Executes the maintained Nebius Spot
CUDA kernel probe on each GPU class. This does not qualify scientific models,
customer storage, recovery, or production throughput.
"""
import argparse
import base64
import datetime as dt
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", type=Path, required=True)
    parser.add_argument("--tfvars", type=Path, required=True)
    parser.add_argument("--cuda-probe", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--deadline-seconds", type=int, default=1800)
    targets = {"h100": "north", "h200": "north", "l40s": "north", "rtx-pro-6000": "south"}
    device_names = {"h100": "H100", "h200": "H200", "l40s": "L40S", "rtx-pro-6000": "RTX PRO 6000"}
    parser.add_argument("--gpu-classes", nargs="+", choices=list(targets), default=list(targets))
    args = parser.parse_args()
    selected = list(dict.fromkeys(args.gpu_classes))
    env = {**os.environ, "TFVARS": str(args.tfvars.resolve())}

    def output(stage, *names):
        proc = subprocess.run(
            [str(args.stack / "stack.sh"), "output", stage, *names],
            cwd=args.stack, env=env, check=True, capture_output=True, text=True,
        )
        return json.loads(proc.stdout)

    clusters = output("cloud", "-json", "clusters")
    assert set(clusters) == {"control", "north", "south"}, "Unexpected fleet shape"
    assert all(c["name"] == f"scientific-ai2-{k}" for k, c in clusters.items()), "Wrong fleet"
    api = output("platform", "control", "-json", "api_url")
    assert clusters["control"]["gateway_ip"] in api, "API does not belong to this fleet"
    key = output("platform", "control", "-json", "tenant_keys")["system"]["qa"]

    def request(path, body=None, idem=None):
        headers = {"Authorization": f"Bearer {key}"}
        if idem:
            headers["Idempotency-Key"] = idem
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(api + path, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
        with urllib.request.urlopen(req, timeout=45) as response:
            return json.load(response)

    encoded = base64.b64encode(args.cuda_probe.read_bytes()).decode()
    command = "python -c " + shlex.quote(
        "import base64,json,os; from pathlib import Path; "
        f"exec(compile(base64.b64decode({encoded!r}), 'cuda_probe.py', 'exec'), {{'__name__':'__main__'}}); "
        "Path('/work/RESULT.json').write_text(json.dumps({'result_ref': "
        "'/v1/operations/' + os.environ['OPERATION'] + '/logs'}))"
    )
    image = ("registry.serverless2.local/nebius/serverless2/api@"
             "sha256:5c39a7743c3f51f393943ac03356425d54e017f02fbd4d9c2212089e8edf80ff")
    receipt = {"fleet": "scientific-ai2", "run_id": args.run_id, "principal": "system/qa",
               "scope": "single-GPU kernel through S2 API; not model/customer qualification",
               "requested_gpu_classes": selected,
               "started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "runs": []}

    def save():
        args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
        args.receipt.chmod(0o600)

    deadline = time.monotonic() + args.deadline_seconds
    terminal = {"SUCCEEDED", "FAILED", "CANCELLED", "EXPORT_FAILED"}
    try:
        # Independent jobs are submitted before polling; no sequential GPU startup.
        for gpu in selected:
            cluster = targets[gpu]
            body = {
                "mode": "run", "name": f"fleet-qualification-{args.run_id}-{gpu}",
                "timeout_s": args.deadline_seconds,
                "input": {"image": image, "command": command, "gpus": 1,
                          "cpu": "1", "memory": "2Gi", "disk_gi": 2,
                          "env": {"NVIDIA_DRIVER_CAPABILITIES": "compute,utility"}},
                "execution": {"io_mode": "application", "placement": {
                    "gpu_preference": [gpu], "allowed_clusters": [cluster]},
                    "recovery": {"on_infrastructure_loss": "fail"}},
            }
            operation = request("/v1/models/container-run:invoke", body,
                                f"scientific-ai2-{args.run_id}-{gpu}")
            run = {"gpu_class": gpu, "cluster": cluster, "operation_id": operation["id"],
                   "status": operation.get("status"), "cuda_kernel_passed": False,
                   "gpu_class_matches": False}
            receipt["runs"].append(run)
            save()
            print(json.dumps(run), flush=True)

        while time.monotonic() < deadline:
            for run in receipt["runs"]:
                if run.get("cuda_kernel_passed") or run["status"] in terminal - {"SUCCEEDED"}:
                    continue
                current = request(f"/v1/operations/{run['operation_id']}")
                if current["status"] != run["status"]:
                    run["status"] = current["status"]
                    print(json.dumps(run), flush=True)
                run["operation"] = current
                if run["status"] == "SUCCEEDED":
                    logs = request(f"/v1/operations/{run['operation_id']}/logs?limit=100")
                    for line in logs.get("lines", []):
                        try:
                            evidence = json.loads(line["line"])
                        except (ValueError, TypeError):
                            continue
                        if isinstance(evidence, dict) and evidence.get("cuda_kernel_passed"):
                            run["kernel"] = evidence
                            run["cuda_kernel_passed"] = evidence.get("validated_elements") == 65536
                            run["gpu_class_matches"] = device_names[run["gpu_class"]] in str(evidence.get("device", ""))
            save()
            if all(r["cuda_kernel_passed"] or r["status"] in terminal - {"SUCCEEDED"}
                   for r in receipt["runs"]):
                break
            time.sleep(15)
    except Exception as error:
        # No credentials, request headers or response bodies in a failure receipt.
        receipt["error_type"] = type(error).__name__
        if isinstance(error, urllib.error.HTTPError):
            receipt["http_status"] = error.code
    finally:
        # Never leave our verification jobs running after a timeout or partial submission.
        # Only IDs returned to this invocation are cancellation targets.
        for run in receipt["runs"]:
            if run["status"] in terminal:
                continue
            try:
                current = request(f"/v1/operations/{run['operation_id']}")
                if current["status"] not in terminal:
                    current = request(f"/v1/operations/{run['operation_id']}:cancel", {})
                    run["cancel_requested"] = True
                run["status"] = current["status"]
            except Exception as error:
                run["cleanup_error_type"] = type(error).__name__
                run["operator_followup_required"] = True
        save()

    passed = ("error_type" not in receipt and len(receipt["runs"]) == len(selected)
              and all(r["cuda_kernel_passed"] and r["gpu_class_matches"]
                      and r["status"] == "SUCCEEDED" for r in receipt["runs"]))
    receipt.update(passed=passed, finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
    save()
    print(json.dumps({"passed": passed, "receipt": str(args.receipt)}), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

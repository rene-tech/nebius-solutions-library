"""Interrupt only a newly submitted internal scVI worker after durable commit.

The normal controller must retry the same operation and restore a committed
generation. Never touch a customer pod/key or terminate a node.
"""

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--qa-env", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cohort", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    env = dict(line.split("=", 1) for line in args.qa_env.read_text().splitlines() if "=" in line)
    with httpx.Client(base_url="https://89.169.99.188", headers={"Authorization": "Bearer " + env["SCIENTIFIC_MODELS_API_KEY"]}, timeout=120, trust_env=False) as client:
        me = client.get("/v1/me").json()
        if (me["tenant_id"], me["principal_id"]) != ("system", "qa"):
            raise ValueError("Internal QA only")
        body = json.loads(args.request.read_text())
        body["parameters"].update(max_epochs=30, scanvi_max_epochs=2, checkpoint_every_n_epochs=1, early_stopping=False)
        response = client.post("/v1/models/scvi-scanvi:submit", json=body, headers={"Idempotency-Key": "scvi-recovery-20261006-" + args.cohort})
        response.raise_for_status()
        admitted = response.json()
        (args.output / "admission.json").write_text(json.dumps(admitted, indent=2) + "\n")
        (args.output / "request.json").write_text(json.dumps(body, indent=2) + "\n")
        operation = admitted["operation"]["id"]
        print(json.dumps({"operation_id": operation, "phase": "waiting-for-checkpoint"}), flush=True)
        if (args.output / "interruption.json").exists():
            raise ValueError("Fault was already injected; monitor the existing operation")
        workspace = "/mnt/fs2-scientific/work/scvi-scanvi/" + hashlib.sha256(operation.encode()).hexdigest()[:20] + "/main"
        kube = ["kubectl", "--context", args.context, "-n", "fs2-models"]
        following = set()
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            data = json.loads(subprocess.check_output([*kube, "get", "pods", "-l", "fs2.nebius.ai/operation-id=" + operation, "-o", "json"]))
            for pod in data["items"]:
                labels = pod["metadata"]["labels"]
                if labels.get("fs2.nebius.ai/tenant-id") != "system" or labels.get("fs2.nebius.ai/model-id") != "scvi-scanvi":
                    raise ValueError("Unexpected pod identity")
                if not any(s["name"] == "scientific-stage" and s.get("state", {}).get("running") for s in pod["status"].get("containerStatuses", [])):
                    continue
                if pod["metadata"]["uid"] not in following:
                    following.add(pod["metadata"]["uid"])
                    with (args.output / "interrupted-worker.log").open("w") as log:
                        subprocess.Popen([*kube, "logs", "-f", pod["metadata"]["name"], "-c", "scientific-stage"], stdout=log, stderr=subprocess.STDOUT)
                # Fixed script, exact operation/namespace/container. No shell
                # matching or node-wide process mutation.
                code = '''import json, os, signal, sys, time
from pathlib import Path
root, operation = Path(sys.argv[1]), sys.argv[2]
ack = root / '.fs2/checkpoint-ack.json'
statefile = root / '.fs2/scvi-state.json'
if not ack.exists() or not statefile.exists(): sys.exit(3)
commit, state = json.loads(ack.read_text()), json.loads(statefile.read_text())
if commit.get('status') != 'committed' or commit.get('generation', 0) < 2 or state.get('active_stage') != 'scvi': sys.exit(3)
for proc in Path('/proc').iterdir():
    if not proc.name.isdigit(): continue
    try: argv = (proc / 'cmdline').read_bytes().split(b'\\0')
    except OSError: continue
    # The trusted PID-1 stage runner also contains the child command in its
    # arguments. Select the actual Python -m worker, not that wrapper (which
    # deliberately reports 143 when it receives Kubernetes termination).
    if argv[1:3] == [b'-m', b'fs2_scvi.worker'] and operation.encode() in argv:
        os.kill(int(proc.name), signal.SIGTERM)
        observed = {'generation': commit['generation'], 'epoch': state.get('epoch'), 'operation_id': operation, 'signal': 'SIGTERM'}
        for _ in range(50):
            result = root / 'result.json'
            marker = root / '.fs2/stage-failed.json'
            if result.exists(): observed['worker_result'] = json.loads(result.read_text())
            if marker.exists():
                observed['stage_exit_code'] = json.loads(marker.read_text())['exit_code']
                break
            time.sleep(.05)
        print(json.dumps(observed))
        sys.exit(0)
sys.exit(3)
'''
                result = subprocess.run([*kube, "exec", pod["metadata"]["name"], "-c", "scientific-stage", "--", "python", "-c", code, workspace, operation], capture_output=True, text=True)
                if result.returncode == 0:
                    receipt = json.loads(result.stdout)
                    receipt.update(pod_uid=pod["metadata"]["uid"], pod_name=pod["metadata"]["name"])
                    (args.output / "interruption.json").write_text(json.dumps(receipt, indent=2) + "\n")
                    print(json.dumps(receipt), flush=True)
                    # Observe the replacement while it exists: the production
                    # controller cleans completed Pods promptly. Retain actual
                    # restore evidence, not merely two attempt numbers.
                    for _ in range(180):
                        replacements = json.loads(subprocess.check_output([
                            *kube, "get", "pods", "-l", "fs2.nebius.ai/operation-id=" + operation, "-o", "json",
                        ]))
                        for replacement in replacements["items"]:
                            if replacement["metadata"]["uid"] == receipt["pod_uid"]:
                                continue
                            labels = replacement["metadata"]["labels"]
                            if labels.get("fs2.nebius.ai/tenant-id") != "system" or labels.get("fs2.nebius.ai/model-id") != "scvi-scanvi":
                                raise ValueError("Replacement identity differs from the internal test")
                            observed = subprocess.run([
                                *kube, "logs", replacement["metadata"]["name"], "-c", "scientific-stage",
                            ], capture_output=True, text=True)
                            if observed.returncode == 0:
                                (args.output / "replacement-worker.log").write_text(observed.stdout)
                                if "Restored all states from the checkpoint" in observed.stdout:
                                    (args.output / "replacement-pod.json").write_text(json.dumps(replacement, indent=2) + "\n")
                                    print(json.dumps({"operation_id": operation, "replacement_pod": replacement["metadata"]["name"], "full_state_restore_observed": True}), flush=True)
                                    return
                        status = client.get(f"/v1/operations/{operation}")
                        status.raise_for_status()
                        status_value = status.json()
                        state = status_value["batch"]["status"] if "batch" in status_value else status_value["status"]
                        if state in {"failed", "cancelled", "succeeded"}:
                            raise RuntimeError("Operation became terminal before full-state replacement evidence was observed")
                        time.sleep(2)
                    raise TimeoutError("No verified replacement restore; operation retained")
            time.sleep(2)
        raise TimeoutError("No safe committed mid-training interruption point; operation retained")


if __name__ == "__main__":
    main()

"""Bounded two-node QA gate using the unchanged worker's JobSet/SSH protocol."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import time
import uuid

from qualify_candidate import KUBE, NS, call, free_capacity, normalize, save, sha, validate, validate_device_probe

LABEL = "gromacs-cuda-aware-20261003"
POOL = "h100-ondemand-1x"
REMOTE = "/mnt/fs2-scientific/probe"

# Executes only the installed test binary, through the existing attempt-local
# SSH and rank-binding implementation. No source/runtime monkey-patching.
PROBE_CODE = r'''
import json,subprocess
from pathlib import Path
from fs2_gromacs.mpi import prepare_keys,rank,peers,configure_launcher,launch_command,ssh_options,wait_peer
from fs2_gromacs.contracts import normalize
w=Path('/mnt/fs2-scientific/buffer-probe')
request=normalize(json.loads(Path('/mnt/fs2-scientific/probe/request.json').read_text()),mpi=True)
d=prepare_keys(w)
log=(d/'sshd.log').open('wb')
server=subprocess.Popen(['/usr/sbin/sshd','-D','-e','-f',str(d/'sshd_config')],stdout=log,stderr=subprocess.STDOUT)
code=1
try:
 (d/'ready').touch()
 if rank():
  code=wait_peer(w,180)
 else:
  configure_launcher(w,d,request['threads'],1)
  try:
   code=subprocess.run(launch_command(request,['/opt/fs2-cuda-aware/mpi-device-probe']),timeout=90).returncode
  except subprocess.TimeoutExpired:
   print('device-buffer probe exceeded 90 seconds',flush=True)
   code=124
  for host in peers()[1:]:
   subprocess.run([*ssh_options(d),'fs2@'+host,'env','PYTHONPATH=/opt/fs2/gromacs','python3','-m','fs2_gromacs.mpi','--workspace',str(w),'--mark-complete','succeeded' if code==0 else 'failed'],check=True,timeout=15)
finally:
 server.terminate()
 server.wait(timeout=10)
 log.close()
raise SystemExit(code)
'''


def fixture(source, output):
    request = json.loads((source / "request.json").read_text())
    request.update(nodes=2, gpus_per_node=1)
    request = normalize(request, mpi=True)
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(source / "input.tar.gz", output / "input.tar.gz")
    save(output / "request.json", request)
    save(output / "fixture.json", {"source": str(source), "source_request_sha256": sha(source / "request.json"),
                                  "input_sha256": sha(output / "input.tar.gz"), "request_sha256": sha(output / "request.json"),
                                  "change": "Only shape: 1x2 to 2x1, same two ranks, thread count, scripts and TPR bytes"})


def manifest(name, image, nodes, seed):
    peers = [f"{name}-gang-{i}-0.{name}" for i in range(2)]
    resources = {"cpu": "8", "memory": "16Gi", "nvidia.com/gpu": "1", "ephemeral-storage": "16Gi"}
    pod = {"metadata": {"labels": {"scientific-ai.nebius.com/task": LABEL}}, "spec": {
        "automountServiceAccountToken": False, "restartPolicy": "Never", "nodeSelector": {"accelerator.fs2.nebius/pool-id": POOL},
        "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}],
        "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": nodes}]}]}},
                     "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"labelSelector": {"matchLabels": {"jobset.sigs.k8s.io/jobset-name": name}}, "topologyKey": "kubernetes.io/hostname"}]}},
        "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001},
        "containers": [{"name": "runtime", "image": image, "command": ["sleep", "1100"],
                        "env": [{"name": "FS2_MPI_HOSTS", "value": ",".join(peers)}, {"name": "FS2_MPI_SSH_SEED", "value": seed},
                                {"name": "FS2_MPI_RANK", "valueFrom": {"fieldRef": {"fieldPath": "metadata.labels['jobset.sigs.k8s.io/job-index']"}}}],
                        "resources": {"requests": resources, "limits": resources},
                        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                        "volumeMounts": [{"name": "workspace", "mountPath": "/mnt/fs2-scientific"}, {"name": "shm", "mountPath": "/dev/shm"}]}],
        "volumes": [{"name": "workspace", "emptyDir": {"sizeLimit": "16Gi"}}, {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "2Gi"}}]}}
    return {"apiVersion": "jobset.x-k8s.io/v1alpha2", "kind": "JobSet", "metadata": {"name": name, "namespace": NS, "labels": {"scientific-ai.nebius.com/task": LABEL}},
            "spec": {"network": {"enableDNSHostnames": True, "publishNotReadyAddresses": True}, "failurePolicy": {"maxRestarts": 0},
                     "replicatedJobs": [{"name": "gang", "replicas": 2, "template": {"spec": {"parallelism": 1, "completions": 1, "backoffLimit": 0, "activeDeadlineSeconds": 1200, "template": pod}}}]}}


def passed(record, device_probe):
    return (record.get("validation", {}).get("status") == "passed"
            and (not device_probe or record.get("device_buffer_validation", {}).get("status") == "passed")
            and "observed" in record.get("cleanup", ""))


def run(args):
    if len(set(args.nodes)) != 2 or "@sha256:" not in args.image or not args.name.startswith("fs2-gmx-cuda-tcp-"):
        raise ValueError("require two explicit distinct nodes, immutable image, task-owned name")
    request = normalize(json.loads((args.input / "request.json").read_text()), mpi=True)
    if (request["nodes"], request.get("gpus_per_node", 1)) != (2, 1):
        raise ValueError("this helper qualifies only 2x1")
    args.output.mkdir(parents=True, exist_ok=False)
    inventory = json.loads(call(["get", "nodes", "-o", "json"]))["items"]
    pods = json.loads(call(["get", "pods", "-A", "-o", "json"]))["items"]
    capacity = [free_capacity(node, inventory, pods, 1) for node in args.nodes]
    if any(item["labels"].get("accelerator.fs2.nebius/pool-id") != POOL for item in capacity):
        raise ValueError("only the existing H100 one-GPU QA pool is allowed")
    # Preserve both node checks while retaining the existing evidence adapter's
    # single-pool representative capacity shape.
    save(args.output / "capacity-nodes-before.json", capacity)
    save(args.output / "capacity-before.json", capacity[0])
    index_bytes = subprocess.check_output(["crane", "manifest", args.image], timeout=60)
    (args.output / "registry-index.json").write_bytes(index_bytes)
    index = json.loads(index_bytes)
    allowed = [args.image.split("@", 1)[1], *[m["digest"] for m in index.get("manifests", []) if m.get("platform") == {"architecture": "amd64", "os": "linux"}]]
    spec = manifest(args.name, args.image, args.nodes, secrets.token_hex(32))
    sanitized = json.loads(json.dumps(spec))
    sanitized["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]["containers"][0]["env"][1]["value"] = "REDACTED_ATTEMPT_SECRET"
    save(args.output / "jobset-request-redacted.json", sanitized)
    record = {"image": args.image, "source_revision": args.source_revision, "input_sha256": sha(args.input / "input.tar.gz"),
              "request_sha256": sha(args.input / "request.json"), "started_at": datetime.now(timezone.utc).isoformat(),
              "customer_path_tested": False, "checkpoint_mode": "local-only", "transport": "tcp-host-staged", "rdma": False,
              "nodes": 2, "gpus": 1, "total_gpus": 2, "allowed_image_digests": allowed}
    uid = None
    try:
        created = json.loads(call(["-n", NS, "create", "-f", "-", "-o", "json"], input=json.dumps(spec).encode()))
        uid = created["metadata"]["uid"]
        record.update(jobset=args.name, jobset_uid=uid)
        for _ in range(120):
            pods = json.loads(call(["-n", NS, "get", "pods", "-l", "jobset.sigs.k8s.io/jobset-name=" + args.name, "-o", "json"]))["items"]
            if len(pods) == 2 and all(any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", [])) for p in pods):
                break
            time.sleep(2)
        else:
            raise TimeoutError("bounded gang startup timed out")
        if {p["spec"]["nodeName"] for p in pods} != set(args.nodes):
            raise ValueError("gang placement differs from the two admitted free nodes")
        pods.sort(key=lambda p: p["metadata"]["labels"]["jobset.sigs.k8s.io/job-index"])
        identities, gpu_lines = [], []
        for pod in pods:
            name = pod["metadata"]["name"]
            image_id = pod["status"]["containerStatuses"][0].get("imageID", "")
            if not any(d in image_id for d in allowed):
                raise ValueError("observed image differs from immutable candidate")
            gpu = call(["-n", NS, "exec", name, "--", "nvidia-smi", "--query-gpu=uuid,name,driver_version,compute_cap,memory.total", "--format=csv"]).decode()
            gpu_lines += gpu.splitlines() if not gpu_lines else gpu.splitlines()[1:]
            identities.append({"pod": name, "uid": pod["metadata"]["uid"], "node": pod["spec"]["nodeName"], "image_id": image_id})
        record.update(pods=identities, image_id=identities[0]["image_id"])
        (args.output / "gpu.txt").write_text("\n".join(gpu_lines) + "\n")
        operation = str(uuid.uuid4())

        def execute(pod):
            name = pod["metadata"]["name"]
            rank = int(pod["metadata"]["labels"]["jobset.sigs.k8s.io/job-index"])
            call(["-n", NS, "cp", "--no-preserve", str(args.input), name + ":" + REMOTE])
            if args.device_probe:
                with (args.output / f"buffer-rank-{rank}.log").open("xb") as log:
                    result = subprocess.run(KUBE + ["-n", NS, "exec", name, "--", "python3", "-c", PROBE_CODE], stdout=log, stderr=subprocess.STDOUT, timeout=220)
                if result.returncode:
                    return {"rank": rank, "probe_exit_code": result.returncode, "worker_exit_code": None}
            with (args.output / f"rank-{rank}.log").open("xb") as log:
                result = subprocess.run(KUBE + ["-n", NS, "exec", name, "--", "python3", "-m", "fs2_gromacs.mpi", "--workspace", REMOTE,
                                                "--request", REMOTE + "/request.json", "--job-id", "gang", "--operation-id", operation, "--checkpoint-mode", "local"],
                                        stdout=log, stderr=subprocess.STDOUT, timeout=900)
            if rank == 0:
                workspace = args.output / "workspace"
                workspace.mkdir()
                # No whole workspace copy: never export attempt SSH identities.
                for relative in ("data", "result.json", ".fs2/mpi-topology.json", ".fs2/engine.json"):
                    call(["-n", NS, "cp", name + ":" + REMOTE + "/" + relative, str(workspace / Path(relative).name)])
            return {"rank": rank, "probe_exit_code": 0 if args.device_probe else None, "worker_exit_code": result.returncode}

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(execute, pods))
        record.update(rank_outcomes=outcomes, runtime_wall_seconds=time.monotonic() - started)
        if args.device_probe:
            record["device_buffer_validation"] = validate_device_probe((args.output / "buffer-rank-0.log").read_text(errors="replace"),
                                                                       (args.output / "gpu.txt").read_text(), 2, max(r["probe_exit_code"] for r in outcomes))
        record["worker_exit_code"] = max(r["worker_exit_code"] if r["worker_exit_code"] is not None else 1 for r in outcomes)
        record["validation"] = validate(args.output / "workspace", request, 2, True, False, record["worker_exit_code"])
    except Exception as exc:
        record.update(qualification_error=str(exc), validation={"status": "failed", "errors": [str(exc)]})
    finally:
        if uid:
            current = json.loads(call(["-n", NS, "get", "jobset", args.name, "-o", "json"]))
            if current["metadata"]["uid"] != uid or current["metadata"]["labels"].get("scientific-ai.nebius.com/task") != LABEL:
                raise ValueError("refuse cleanup of changed/unowned JobSet")
            call(["-n", NS, "delete", "jobset", args.name, "--wait=false"])
            for _ in range(30):
                remaining = json.loads(call(["-n", NS, "get", "pods", "-l", "jobset.sigs.k8s.io/jobset-name=" + args.name, "-o", "json"]))["items"]
                if not remaining:
                    record["cleanup"] = "owned JobSet and Pods deleted; absence observed"
                    break
                time.sleep(2)
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        record["status"] = "passed" if passed(record, args.device_probe) else "failed"
        save(args.output / "receipt.json", record)
    print(json.dumps({"output": str(args.output), "status": record["status"], "cleanup": record.get("cleanup"), "error": record.get("qualification_error")}))
    return 0 if passed(record, args.device_probe) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    p = subs.add_parser("prepare")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p = subs.add_parser("run")
    p.add_argument("--nodes", nargs=2, required=True)
    for name in ("name", "image", "source-revision"):
        p.add_argument("--" + name, required=True)
    for name in ("input", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--device-probe", action="store_true")
    args = parser.parse_args()
    if args.action == "prepare":
        fixture(args.source, args.output)
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())

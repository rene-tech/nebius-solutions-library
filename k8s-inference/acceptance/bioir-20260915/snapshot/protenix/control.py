#!/usr/bin/env python3
"""Owned same-GPU Protenix snapshot cohort; no production resources are mutated."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent
COHORT = os.environ.get("BIOIR_SNAPSHOT_COHORT", "fkt")
assert COHORT in {"fkt", "xjaw", "y0jt"}
ROOT = BASE if COHORT == "fkt" else BASE / COHORT
LANE = BASE.parents[1] / "protenix"
K8S = BASE.parents[3]
NS = "fs2-bioir-protenix"
NODE, GPU_UUID, DRIVER = {
    "fkt": ("computeinstance-e00fkt1bsa4ec657sn", "GPU-1d3b90d3-7eed-1abd-59e8-95141843bde0", "580.159.04"),
    "xjaw": ("computeinstance-e00xjaw5jqexvvnpat", "GPU-ca98bcbc-cf06-a4c2-e3bc-c27a0cba95bc", "580.159.04"),
    "y0jt": ("computeinstance-e00y0jttwekyghrznp", "GPU-9885f9c6-110a-10b5-2c26-c256e895d575", "580.173.02"),
}[COHORT]
SUFFIX = "" if COHORT == "fkt" else "-" + COHORT
PREFIX = "bir-protenix-snapshot" if COHORT == "fkt" else "bir-protenix-" + COHORT
PVC = "fs2-bioir-protenix-snapshot" + SUFFIX
APP = "fs2-bioir-protenix-snapshot-app" + SUFFIX
SOURCE = "fs2-bioir-protenix-snapshot-source" + SUFFIX
CONTAINER = "scientific-stage"
PYTHON = "/opt/protenix-venv/bin/python"
IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-evaluation/bioir-protenix@sha256:0c391bdc2ab0c5260a222ec7df5e5adc5ea9bfdbc2e158b386a97aac5dc72e94"
TOOLS = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-snapshot/scientific-tools@sha256:17cc3536dd847355b8457b2e92bd7d0fdf292bdd8e6acc457e25f14e28284ba4"
LABELS = {"evaluation": "fs2-bioir-20260915", "lane": "snapshot", "benchmark-model": "protenix-bir"}
if COHORT != "fkt":
    LABELS["snapshot-cohort"] = COHORT
K = ["kubectl", "--kubeconfig", "/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig", "--context", "k8s-inference-h100"]


def k(*args, **kwargs):
    return subprocess.check_output(K + list(args), text=True, **kwargs)


def save(name, value):
    path = ROOT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def apply(name, value):
    save("manifests/" + name + ".json", value)
    path = str(ROOT / "manifests" / (name + ".json"))
    print(k("apply", "--dry-run=client", "-f", path), flush=True)
    print(k("apply", "-f", path), flush=True)


def prepare():
    source = K8S / "models/scientific-snapshot"
    names = ["supervisor.py", "process_checkpoint.py", "serving_checkpoint.py", "serving_supervisor.py", "serving_filesystem.py", "serving_launcher.py", "sitecustomize.py"]
    data = {name: (source / name).read_text() for name in names}
    hashes = {"snapshot/" + name: hashlib.sha256(text.encode()).hexdigest() for name, text in data.items()}
    apply("source", {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": SOURCE, "namespace": NS, "labels": LABELS}, "immutable": True, "data": data})
    candidate = json.loads(subprocess.check_output([sys.executable, str(LANE / "render_candidate.py"), "--image", IMAGE, "--node", NODE, "--graphs", "1"], text=True))
    cm, original = candidate["items"]
    cm["metadata"] = {"name": APP, "namespace": NS, "labels": LABELS}
    cm["immutable"] = True
    cm["data"].update({name: (LANE / name).read_text() for name in ["run_baseline.py", "run_prepared.py"]})
    cm["data"]["request.py"] = (BASE / "request.py").read_text()
    cm["data"]["marker.json"] = (LANE / "raw/current-h100/ubiquitin-76/localization-prep.json").read_text()
    hashes.update({"app/" + name: hashlib.sha256(text.encode()).hexdigest() for name, text in cm["data"].items()})
    apply("app", cm)
    save("inventory/source-hashes.json", hashes)
    save("inventory/candidate.json", original)
    apply("pvc", {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": PVC, "namespace": NS, "labels": LABELS}, "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "compute-csi-default-sc", "resources": {"requests": {"storage": "128Gi"}}}})


def preflight(tag):
    pods = json.loads(k("get", "pods", "-A", "-o", "json"))["items"]
    node = json.loads(k("get", "node", NODE, "-o", "json"))
    def gpu(p):
        return any(int(c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0)) for c in p["spec"]["containers"] + p["spec"].get("initContainers", []))
    allocated = [p for p in pods if p["spec"].get("nodeName") == NODE and p["status"]["phase"] in ["Running", "Pending"] and gpu(p)]
    pending = [p["metadata"]["namespace"] + "/" + p["metadata"]["name"] for p in pods if p["status"]["phase"] == "Pending" and not p["metadata"].get("labels", {}).get("evaluation") and gpu(p)]
    save("inventory/" + tag + "-preflight.json", {"unix": time.time(), "node": node, "allocated": allocated, "pending_customer_gpu_pods": pending})
    assert not allocated, "Assigned node has another GPU pod"
    assert not pending, "Pending customer GPU workload needs coordinator review"
    assert any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"])
    detector = next(p["metadata"]["name"] for p in pods if p["spec"].get("nodeName") == NODE and p["metadata"]["name"].startswith("nebius-node-problem-detector-gpu-"))
    processes = k("-n", "kube-system", "exec", detector, "--", "/usr/bin/nvidia-smi", "--query-compute-apps=pid,process_name,used_gpu_memory", "--format=csv,noheader").strip()
    telemetry = k("-n", "kube-system", "exec", detector, "--", "/usr/bin/nvidia-smi", "--query-gpu=uuid,name,driver_version,memory.used,utilization.gpu", "--format=csv,noheader")
    save("inventory/" + tag + "-gpu.json", {"unix": time.time(), "processes": processes, "telemetry": telemetry})
    assert not processes, "Assigned GPU has live compute processes"
    assert GPU_UUID in telemetry and DRIVER in telemetry, "GPU/driver differs from the authorized independent cohort"


def donor(name, run):
    preflight(name)
    sys.path.insert(0, str(K8S / "acceptance/h100-fleet/snapshots"))
    import render_serving_probe
    original = json.loads((ROOT / "inventory/candidate.json").read_text())
    spec = original["spec"]
    next(v for v in spec["volumes"] if v["name"] == "source")["configMap"]["name"] = APP
    args = argparse.Namespace(container=CONTAINER, entrypoint_json="[]", asyncio_loop=False, python=PYTHON, run=run, fallback="fail", request_uid=10001, allow_device_remap=False, mode="donor", tools_image=TOOLS, model_revision="5e1c3b548af40752bb15f9f2ba06590e20e2b165e3fe9ab3fa99af9977574d48:8f931f9774a396b67033d0e58628e1834f4a1448165e04254b40a780b0c0d599", model_id="protenix-v2-bir-public-0.1.0-global-rng", source_configmap=SOURCE, pvc=PVC, node=NODE, name=name)
    value = render_serving_probe.render({"metadata": {"namespace": NS}, "spec": {"template": {"spec": spec}}}, args)
    value["metadata"]["labels"] = LABELS
    value["spec"]["activeDeadlineSeconds"] = 14400
    runtime = value["spec"]["containers"][0]
    runtime["command"] = [a.replace("/snapshot-source/supervisor.py", "/snapshot-source/serving_supervisor.py") for a in runtime["command"]]
    init = value["spec"]["initContainers"][0]
    init["volumeMounts"].append({"name": "workspace", "mountPath": "/mnt/fs2-scientific"})
    init["command"][2] += ' && chown 10001:10001 /mnt/fs2-scientific'
    for service in json.loads(k("-n", NS, "get", "services", "-o", "json"))["items"]:
        selector = service["spec"].get("selector")
        assert not selector or not all(LABELS.get(a) == b for a, b in selector.items())
    apply(name, value)
    save("lifecycle/" + name + "-created.json", {"unix": time.time(), "run": run, "variant": "normal"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "donor", "preflight"])
    parser.add_argument("--name")
    parser.add_argument("--run")
    args = parser.parse_args()
    if args.action == "donor":
        donor(args.name, args.run)
    elif args.action == "preflight":
        preflight(args.name)
    else:
        prepare()

#!/usr/bin/env python3
"""Create an isolated exact-profile ESMFold donor from a retained accepted Job.

The model image, localized immutable artifacts and resource envelope are kept.
This creates only a labelled test Pod and immutable source ConfigMap, never a
production deployment. Snapshot storage must be an existing task-owned claim.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def captured_model_revision(image_lock, model):
    """Preserve the image's weight identity, not its upstream code revision.

    The original scientific collector checks this value in confidence.json.
    Catalog execution identity separately records the code/profile revision.
    """
    matches = [item["build_args"]["MODEL_REVISION"] for item in image_lock["images"]
               if item.get("build_args", {}).get("RUNTIME_ID") == model]
    if len(matches) != 1:
        raise ValueError("the exact runtime must have one locked model revision")
    return matches[0]


def restore_compatible_donor(pod):
    """Keep the root ESM worker's captured capabilities equal to restore.

    Generic serving donors need SYS_RESOURCE to inspect another UID's rlimits.
    The ESM model worker and capture process both run as root, so that extra
    capability is unnecessary and must not become part of its checkpoint.
    """
    runtime = next(item for item in pod["spec"]["containers"]
                   if item["name"] == "scientific-stage")
    capabilities = runtime["securityContext"]["capabilities"]["add"]
    if "SYS_RESOURCE" in capabilities:
        capabilities.remove("SYS_RESOURCE")
    return pod


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("jobs", "directory", "kubeconfig"):
        parser.add_argument("--" + key, type=Path, required=True)
    for key in ("model", "pvc", "name", "bundle-path"):
        parser.add_argument("--" + key, required=True)
    placement = parser.add_mutually_exclusive_group(required=True)
    placement.add_argument("--node")
    placement.add_argument("--pool", help="Existing pool; allow its configured autoscaler to schedule the donor")
    args = parser.parse_args()
    if args.model not in {"esmfold2", "esmfold2-fast"}:
        parser.error("select the exact ESMFold2 or ESMFold2-Fast profile")
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=False)
    solution = Path(__file__).resolve().parents[4]
    source_root = solution / "models/scientific-snapshot"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from render_serving_probe import render

    sources = {name: (source_root / name).read_text() for name in (
        "supervisor.py", "process_checkpoint.py", "esmfold2_server.py",
    )}
    # The immutable production images predate the optional request bridge.
    # Mount the existing, independently tested wrapper as a versioned overlay;
    # its normal-loading branch and scientific argument checks stay intact.
    sources["run_esmfold2.py"] = (
        solution / "models/cancer-immunotherapy/images/structure-secondary/run_esmfold2.py"
    ).read_text()
    source = next(job for job in json.loads(args.jobs.read_bytes())["items"]
                  if job["metadata"]["labels"].get("fs2.nebius.ai/model-id") == args.model
                  and job["metadata"]["labels"].get("fs2.nebius.ai/stage-id") == "fold")
    (args.directory / "original-job-private.json").write_text(json.dumps(source))
    spec = source["spec"]["template"]["spec"]
    runtime = next(c for c in spec["containers"] if c["name"] == "scientific-stage")
    original = runtime["command"] + runtime.get("args", [])
    (args.directory / "original-command.json").write_text(json.dumps(original))
    spec["containers"] = [runtime]
    spec["initContainers"] = []
    python = "/opt/esm/.pixi/envs/gpu/bin/python"
    runtime["command"] = ["/bin/bash", "-c", 'source /opt/fs2/activate.sh; exec "$@"',
                          "esmfold-snapshot", python, "-u", "/snapshot-source/esmfold2_server.py"]
    runtime.pop("args", None)
    environment = {item["name"]: item["value"] for item in runtime.get("env", []) if "value" in item
                   and not any(word in item["name"] for word in ("TOKEN", "CAPABILITY", "SECRET", "PASSWORD"))}
    environment.update({
        "FS2_SNAPSHOT_MODEL_DIR": original[original.index("--model-dir") + 1],
        "FS2_ESMC_MODEL_DIR": original[original.index("--esmc-dir") + 1],
        "FS2_SNAPSHOT_ESMC_PRECISION": original[original.index("--esmc-precision") + 1],
        "FS2_SNAPSHOT_ATTENTION": "flash_attention_2",
    })
    runtime["env"] = [{"name": key, "value": value} for key, value in environment.items()]
    template = json.loads((solution / "acceptance/h100-fleet/snapshots/protenix-v2-bundle.json").read_bytes())
    configmap_name = "fs2-fleet-snapshot-esmfold-current-v2"
    image_lock = json.loads((solution / "models/cancer-immunotherapy/images/structure-secondary/image-lock.json").read_bytes())
    options = argparse.Namespace(
        container="scientific-stage", entrypoint_json="[]", asyncio_loop=False,
        python=python, run=args.bundle_path, fallback="fail", mode="donor", request_uid=10001,
        allow_device_remap=True, tools_image=template["tools_image"],
        model_revision=captured_model_revision(image_lock, args.model), model_id=args.model,
        source_configmap=configmap_name, pvc=args.pvc, name=args.name, node=args.node,
    )
    pod = restore_compatible_donor(render(source, options))
    next(c for c in pod["spec"]["containers"] if c["name"] == "scientific-stage")["volumeMounts"].append({
        "name": "snapshot-source", "mountPath": "/opt/fs2/run_esmfold2.py",
        "subPath": "run_esmfold2.py", "readOnly": True,
    })
    if args.pool:
        pod["spec"]["nodeSelector"].pop("kubernetes.io/hostname", None)
        pod["spec"]["nodeSelector"]["accelerator.fs2.nebius/pool-id"] = args.pool
    configmap = {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
                 "metadata": {"name": configmap_name, "namespace": source["metadata"]["namespace"],
                              "labels": {"snapshot.fs2.nebius/task": "fs2-h100-fleet-snapshot-options-r20260907"}},
                 "data": sources}
    (args.directory / "source-worker-private.json").write_text(json.dumps(source))
    (args.directory / "donor-private.json").write_text(json.dumps(pod))
    (args.directory / "source-sha256.json").write_text(json.dumps(
        {key: hashlib.sha256(value.encode()).hexdigest() for key, value in sources.items()}, indent=2))
    kube = ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", "k8s-inference-h100",
            "-n", source["metadata"]["namespace"]]
    existing = subprocess.run([*kube, "get", "configmap", configmap_name, "-o", "json"], capture_output=True, text=True)
    if existing.returncode == 0:
        if json.loads(existing.stdout)["data"] != sources:
            raise RuntimeError("existing immutable snapshot source differs")
    else:
        subprocess.run([*kube, "create", "--dry-run=client", "-f", "-"], input=json.dumps(configmap), text=True, check=True)
        created = subprocess.run([*kube, "create", "-f", "-"], input=json.dumps(configmap), text=True)
        if created.returncode:
            # Independently launched exact-profile donors can share this same
            # immutable source. Accept a create race only after byte equality.
            raced = subprocess.run([*kube, "get", "configmap", configmap_name, "-o", "json"],
                                   capture_output=True, text=True, check=True)
            if json.loads(raced.stdout)["data"] != sources:
                raise RuntimeError("concurrent immutable snapshot source differs")
    subprocess.run([*kube, "create", "--dry-run=client", "-f", "-"], input=json.dumps(pod), text=True, check=True)
    subprocess.run([*kube, "create", "-f", "-"], input=json.dumps(pod), text=True, check=True)


if __name__ == "__main__":
    main()

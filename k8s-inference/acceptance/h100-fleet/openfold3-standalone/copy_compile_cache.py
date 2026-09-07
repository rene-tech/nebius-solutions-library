#!/usr/bin/env python3
"""Copy only the measured OF3 compile-cache tree with a CPU-only task Pod.

Source is read-only. Existing destination files must match; no file is replaced.
New files become visible atomically via a same-directory temporary hard link.
No weights are downloaded, GPU is allocated, or production workload is changed.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
COPY = r'''
import hashlib,json,os,shutil,stat,sys,tempfile
from pathlib import Path
source=Path('/source')/sys.argv[1]
destination=Path('/destination')/sys.argv[1]
assert source.is_dir()
parent_metadata=source.parent.stat()
destination.parent.mkdir(parents=True,exist_ok=True)
os.chown(destination.parent,parent_metadata.st_uid,parent_metadata.st_gid)
os.chmod(destination.parent,stat.S_IMODE(parent_metadata.st_mode))
rows=[]
for path in [source,*sorted(source.rglob('*'))]:
    target=destination/path.relative_to(source)
    metadata=path.lstat()
    if stat.S_ISDIR(metadata.st_mode):
        target.mkdir(parents=True,exist_ok=True)
        os.chown(target,metadata.st_uid,metadata.st_gid)
        os.chmod(target,stat.S_IMODE(metadata.st_mode))
        continue
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError('Unexpected non-regular compile-cache entry: '+str(path))
    with path.open('rb') as stream:
        digest=hashlib.file_digest(stream,'sha256').hexdigest()
    if not target.exists():
        descriptor,temporary=tempfile.mkstemp(prefix='.fs2-compile-copy-',dir=target.parent)
        try:
            with os.fdopen(descriptor,'wb') as stream,path.open('rb') as incoming:
                shutil.copyfileobj(incoming,stream,1024*1024)
                stream.flush();os.fsync(stream.fileno())
            os.chown(temporary,metadata.st_uid,metadata.st_gid)
            os.chmod(temporary,stat.S_IMODE(metadata.st_mode))
            try: os.link(temporary,target)
            except FileExistsError: pass
        finally: os.unlink(temporary)
    with target.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=digest:
            raise ValueError('Destination cache differs: '+str(target))
    # Ninja uses dependency mtimes; matching bytes alone can still trigger a relink.
    os.utime(target,ns=(metadata.st_atime_ns,metadata.st_mtime_ns))
    rows.append({'path':str(path.relative_to(source)),'bytes':metadata.st_size,'sha256':digest})
print(json.dumps({'status':'passed','files':rows,'bytes':sum(r['bytes'] for r in rows)},indent=2))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--destination-pvc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    integration = json.loads((HERE / "integration.json").read_text())
    source = integration["cache"]["pvc"]
    if source == args.destination_pvc:
        raise ValueError("Source and destination claims must be distinct")
    relative = integration["cache"]["runtime_path"].removeprefix("/model-cache/")
    name = "fs2-mm-openfold3-compile-copy-20260907"
    kube = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", "k8s-inference-h100", "-n", "fs2-models"]

    def call(command, *, data=None, timeout=60):
        return subprocess.run([*kube, *command], input=data, text=True, capture_output=True, check=True, timeout=timeout).stdout

    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "fs2-models",
        "labels": {"fs2.nebius/task": "fs2-h100-fleet-medical-media-r20260907"}},
        "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": 1800,
        "automountServiceAccountToken": False, "enableServiceLinks": False,
        "nodeSelector": {"kubernetes.io/hostname": "computeinstance-e00p3acr87k9k4mckj"},
        "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}],
        "containers": [{"name": "copy", "image": integration["image"],
            "command": ["/bin/bash", "-ec", "source /opt/fs2/activate.sh; exec python -c 'import time; time.sleep(1700)'"],
            "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
            "securityContext": {"runAsUser": 0, "runAsGroup": 0, "runAsNonRoot": False},
            "resources": {"requests": {"cpu": "1", "memory": "256Mi"}, "limits": {"cpu": "2", "memory": "2Gi"}},
            "volumeMounts": [{"name": "source", "mountPath": "/source", "readOnly": True},
                             {"name": "destination", "mountPath": "/destination"}]}],
        "volumes": [{"name": "source", "persistentVolumeClaim": {"claimName": source, "readOnly": True}},
                    {"name": "destination", "persistentVolumeClaim": {"claimName": args.destination_pvc}}]}}
    (args.output / "pod-manifest.json").write_text(json.dumps(pod, indent=2) + "\n")
    call(["create", "--dry-run=client", "-f", "-"], data=json.dumps(pod))
    call(["create", "-f", "-"], data=json.dumps(pod))
    call(["wait", "--for=condition=Ready", "pod/" + name, "--timeout=300s"], timeout=310)
    before = dt.datetime.now(dt.timezone.utc).isoformat()
    result = call(["exec", "-i", name, "--", "/bin/bash", "-ec", "source /opt/fs2/activate.sh; exec python - \"$1\"", "copy", relative], data=COPY, timeout=900)
    (args.output / "file-manifest.json").write_text(result)
    receipt = {"status": "passed", "source_pvc": source, "destination_pvc": args.destination_pvc,
               "relative_path": relative, "started_at": before, "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
               "files": len(json.loads(result)["files"]), "bytes": json.loads(result)["bytes"]}
    for claim in (source, args.destination_pvc):
        (args.output / (claim + ".json")).write_text(call(["get", "pvc", claim, "-o", "json"]))
    call(["delete", "pod", name, "--wait=true", "--timeout=60s"], timeout=70)
    receipt["copy_pod_deleted"] = True
    (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()

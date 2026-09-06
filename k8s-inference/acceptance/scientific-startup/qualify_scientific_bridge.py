#!/usr/bin/env python3
"""Qualify a readonly snapshot with a real controller-issued scientific workspace.

The bundle must contain images/, cache/, worker.log and fixture/ copied from
the supplied production pod while its scientific stage was running. No frozen
input, marker, scientific parameter or stage-runner source is synthesized here.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import subprocess
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--restore-template", type=Path, required=True)
    parser.add_argument("--production-pod", type=Path, required=True)
    parser.add_argument("--bundle-subdir", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--name-prefix", required=True)
    parser.add_argument("--mechanisms", nargs="+", choices=("restored", "normal-fallback"), default=("restored", "normal-fallback"))
    parser.add_argument("--diagnose-cuda-initialization", action="store_true")
    args = parser.parse_args()
    template = json.loads(args.restore_template.read_text())
    original = next(
        container for container in json.loads(args.production_pod.read_text())["spec"]["containers"]
        if container["name"] == "scientific-stage"
    )
    directory = "/checkpoints/" + args.bundle_subdir
    python = "/opt/esm/.pixi/envs/gpu/bin/python"
    kubectl = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context,
               "-n", template["metadata"]["namespace"]]
    args.output_directory.mkdir(parents=True, exist_ok=True)
    receipt = {"schema": "fs2-serve.nebius.ai/scientific-snapshot-cli-bridge/v1", "runs": []}

    def run(arguments, *, payload=None, check=True):
        return subprocess.run([*kubectl, *arguments], input=payload, capture_output=True,
                              text=True, check=check, timeout=120)

    for mechanism in args.mechanisms:
        pod = copy.deepcopy(template)
        name = args.name_prefix + "-" + mechanism
        pod["metadata"]["name"] = name
        runtime = pod["spec"]["containers"][0]
        pvc = next(volume for volume in pod["spec"]["volumes"] if volume["name"] == "checkpoints")
        pod["spec"]["volumes"] += [{"name": "scratch", "emptyDir": {}}, {"name": "workspace", "emptyDir": {}}]
        pvc["persistentVolumeClaim"]["readOnly"] = True
        runtime["volumeMounts"] = [mount for mount in runtime["volumeMounts"] if mount["name"] != "checkpoints"]
        runtime["volumeMounts"] += [
            {"name": "checkpoints", "mountPath": "/snapshot-bundle", "subPath": args.bundle_subdir, "readOnly": True},
            {"name": "scratch", "mountPath": directory},
            {"name": "checkpoints", "mountPath": directory + "/images", "subPath": args.bundle_subdir + "/images", "readOnly": True},
            {"name": "workspace", "mountPath": "/mnt/fs2-scientific"},
        ]
        pod["spec"]["initContainers"].append({
            "name": "copy-real-scientific-fixture", "image": runtime["image"],
            "command": ["/bin/cp", "-a", "/snapshot-bundle/fixture/.", "/mnt/fs2-scientific/"],
            "securityContext": {"runAsUser": 0, "runAsGroup": 0},
            "volumeMounts": [runtime["volumeMounts"][-4], runtime["volumeMounts"][-1]],
        })
        runtime["workingDir"] = original["workingDir"]
        runtime["env"] += [item for item in original["env"] if "valueFrom" not in item]
        command = [
            python, "/opt/fs2/snapshot/supervisor.py", "--directory",
            directory if mechanism == "restored" else "/tmp/fs2-intentionally-absent-checkpoint",
            "--request-uid", "10001", "--request-gid", "10001",
            "--fallback", "fail" if mechanism == "restored" else "normal-load",
        ]
        if mechanism == "restored":
            command += ["--source-directory", "/snapshot-bundle"]
        command += ["restore", "--", python, original["workingDir"] + "/.fs2/stage-runner.py", "--", *original["command"]]
        activation = (
            'source /opt/fs2/activate.sh; '
            'case "${NVIDIA_VISIBLE_DEVICES-}" in GPU-*) export CUDA_VISIBLE_DEVICES="$NVIDIA_VISIBLE_DEVICES";; esac; '
        )
        if args.diagnose_cuda_initialization:
            activation += (
                "python -c 'import os,ctypes,json; print(json.dumps({\"gpu_environment\": "
                "{k:os.environ.get(k) for k in (\"NVIDIA_VISIBLE_DEVICES\",\"CUDA_VISIBLE_DEVICES\")}, "
                "\"cuInit\":ctypes.CDLL(\"libcuda.so.1\").cuInit(0)}),flush=True)'; "
            )
        runtime["command"] = [
            "/bin/bash", "-c", activation + 'exec "$@"',
            "fs2-snapshot", *command,
        ]
        output_dir = original["command"][original["command"].index("--output-dir") + 1]
        reader = (
            "import hashlib,json,os,pathlib,re,time; "
            f"root=pathlib.Path({original['workingDir']!r}); output=pathlib.Path({output_dir!r}); "
            "marker=root/'.fs2/stage-complete.json'; deadline=time.monotonic()+480; "
            "\nwhile not marker.exists() and time.monotonic()<deadline: time.sleep(.25)\n"
            "assert marker.exists(), 'completion marker missing'; "
            "completion=json.loads(marker.read_text()); confidence=json.loads((output/'confidence.json').read_text()); "
            "cif=(output/'fs2-result.cif').read_text(); "
            f"expected=hashlib.sha256(json.dumps({original['command']!r},sort_keys=True,separators=(',',':')).encode()).hexdigest(); "
            "assert completion['argv_sha256']==expected; "
            "assert len(re.findall(r'^(?:ATOM|HETATM)\\s',cif,re.MULTILINE))>100; "
            "print(json.dumps({'reader_uid':os.getuid(),'completion':completion,'confidence':confidence," 
            "'cif_sha256':hashlib.sha256(cif.encode()).hexdigest(),'files':[{" 
            "'name':p.name,'uid':p.stat().st_uid,'mode':oct(p.stat().st_mode&0o777)} "
            "for p in (marker,output/'confidence.json',output/'fs2-result.cif')]}))"
        )
        pod["spec"]["containers"].append({
            "name": "collector-readability", "image": runtime["image"],
            "command": [python, "-c", reader],
            "securityContext": {"runAsUser": 10001, "runAsGroup": 10001},
            "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            "volumeMounts": [{"name": "workspace", "mountPath": "/mnt/fs2-scientific", "readOnly": True}],
        })
        (args.output_directory / f"{mechanism}-pod.json").write_text(json.dumps(pod, indent=2))
        started = time.monotonic()
        run(["apply", "-f", "-"], payload=json.dumps(pod))
        row = {"mechanism": mechanism, "pod": name}
        receipt["runs"].append(row)
        try:
            while time.monotonic() - started < 540:
                observed = json.loads(run(["get", "pod", name, "-o", "json"]).stdout)
                statuses = observed["status"].get("containerStatuses", [])
                failures = [status for status in statuses if status.get("state", {}).get("terminated", {}).get("exitCode", 0)]
                if failures or observed["status"]["phase"] in ("Succeeded", "Failed"):
                    break
                time.sleep(1)
            row["seconds"] = time.monotonic() - started
            row["pod_uid"] = observed["metadata"]["uid"]
            row["phase"] = observed["status"]["phase"]
            for container in ("runtime", "collector-readability"):
                logs = run(["logs", name, "-c", container], check=False)
                (args.output_directory / f"{mechanism}-{container}.log").write_text(logs.stdout + logs.stderr)
            if row["phase"] != "Succeeded":
                raise RuntimeError(f"{mechanism} did not complete successfully")
            row["reader"] = json.loads((args.output_directory / f"{mechanism}-collector-readability.log").read_text())
            row["status"] = "passed"
        except Exception as error:
            row.update(status="failed", error=str(error))
            raise
        finally:
            (args.output_directory / "receipt.json").write_text(json.dumps(receipt, indent=2))
            run(["delete", "pod", name, "--wait=true", "--timeout=60s"], check=False)
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()

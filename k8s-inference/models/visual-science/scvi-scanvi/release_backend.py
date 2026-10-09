"""Image-only rolling release preserving the actual live configuration.

Helm values can lag later operator changes. This narrowly updates the shared
backend image after checking every current template, and emits exact rollback
patches. It does not reset model routes, scheduling, keys or running jobs.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

REPOSITORY = (
    "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane"
)
TARGETS = [
    ("deployment", "fs2-serve-control-plane"),
    ("deployment", "fs2-serve-control-plane-model-controller"),
    ("cronjob", "fs2-serve-control-plane-maintenance"),
]


def ensure_configmap(kube, path, *, apply=False):
    desired = json.loads(path.read_text())
    current = subprocess.check_output([
        *kube, "get", "configmap", desired["metadata"]["name"],
        "--ignore-not-found", "-o", "json",
    ])
    if current.strip():
        existing = json.loads(current)
        if any(existing.get(key, {}) != desired.get(key, {}) for key in ("data", "binaryData")):
            raise ValueError("Existing digest-named ConfigMap has different bytes; recapture before release")
        # Older generators can omit immutable=true. Reapplying identical data
        # with client-side apply would then attempt to unset immutability.
        return "reused"
    command = [*kube, "apply", "-f", str(path)]
    subprocess.run([*command, "--dry-run=server"], check=True)
    if apply:
        subprocess.run(command, check=True)
    return "created" if apply else "validated"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--baseline-image", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--activation", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    for image in (args.baseline_image, args.image):
        if not re.fullmatch(re.escape(REPOSITORY) + r"@sha256:[0-9a-f]{64}", image):
            raise ValueError("Use immutable images in the existing backend repository")
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    kube = [
        "kubectl",
        "--context",
        args.context,
        "-n",
        "fs2-system",
        "--request-timeout=30s",
    ]
    activation = None
    if args.activation:
        if args.baseline is None:
            raise ValueError("Activation requires the actual captured baseline")
        activation = json.loads(
            (args.activation / "activation.values.json").read_text()
        )
        captured = json.loads((args.baseline / "deployment.json").read_text())["spec"][
            "template"
        ]["spec"]
        desired = activation["scientificBatch"]
        execution = json.dumps(
            desired["executionMap"], sort_keys=True, separators=(",", ":")
        )
        execution_name = (
            "fs2-scvi-execution-" + hashlib.sha256(execution.encode()).hexdigest()[:12]
        )
        cm = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": execution_name, "namespace": "fs2-system"},
            "immutable": True,
            "data": {"execution-map.json": execution},
        }
        execution_path = args.output / "execution.configmap.json"
        execution_path.write_text(json.dumps(cm, indent=2) + "\n")
        for path in (execution_path, args.activation / "scheduling.configmap.json"):
            ensure_configmap(kube, path, apply=args.apply)
    changes = []
    for kind, name in TARGETS:
        obj = json.loads(
            subprocess.check_output([*kube, "get", kind, name, "-o", "json"])
        )
        spec = obj["spec"]["jobTemplate"]["spec"] if kind == "cronjob" else obj["spec"]
        before = spec["template"]
        after = copy.deepcopy(before)
        if activation and name == "fs2-serve-control-plane":
            watched = {"scientific-batch-scheduling", "scientific-batch-execution"}
            actual = {
                v["name"]: v for v in before["spec"]["volumes"] if v["name"] in watched
            }
            previous = {
                v["name"]: v for v in captured["volumes"] if v["name"] in watched
            }
            if actual != previous:
                raise ValueError("Live scientific bindings changed after capture")
            for volume in after["spec"]["volumes"]:
                if volume["name"] == "scientific-batch-scheduling":
                    volume["configMap"]["name"] = desired[
                        "schedulingContractConfigMapName"
                    ]
                elif volume["name"] == "scientific-batch-execution":
                    volume["configMap"]["name"] = execution_name
            for container in after["spec"]["containers"]:
                for env in container.get("env", []):
                    if env["name"] == "FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_SHA256":
                        env["value"] = desired["schedulingContractSha256"]
                    elif env["name"] == "FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE":
                        env["value"] = args.image
                    elif env["name"] == "FS2_ARTIFACT_MEDIA_TYPES":
                        env["value"] = ",".join(
                            sorted(
                                set(env["value"].split(","))
                                | {
                                    "application/x-hdf5",
                                    "application/vnd.fs2.scvi-checkpoint+json",
                                }
                            )
                        )
        for field in ("containers", "initContainers"):
            for container in after["spec"].get(field, []):
                if container["image"].startswith(REPOSITORY + "@"):
                    if container["image"] not in (args.baseline_image, args.image):
                        raise ValueError(
                            f"Concurrent release changed {name}; recapture before proceeding"
                        )
                    container["image"] = args.image
        if before == after:
            continue
        after.setdefault("metadata", {}).setdefault("annotations", {})[
            "fs2.nebius.ai/image-digest"
        ] = args.image.split("@")[1]
        path = (
            "/spec/jobTemplate/spec/template" if kind == "cronjob" else "/spec/template"
        )
        for label, old, new in (("patch", before, after), ("rollback", after, before)):
            patch = [
                {"op": "test", "path": path, "value": old},
                {"op": "replace", "path": path, "value": new},
            ]
            (args.output / f"{name}.{label}.json").write_text(
                json.dumps(patch, indent=2) + "\n"
            )
        command = [
            *kube,
            "patch",
            kind,
            name,
            "--type=json",
            "--patch-file",
            str(args.output / f"{name}.patch.json"),
        ]
        subprocess.run([*command, "--dry-run=server", "-o", "name"], check=True)
        changes.append(command)
    if args.apply:
        for command in changes:
            subprocess.run([*command, "-o", "name"], check=True)
    print(
        json.dumps(
            {"applied": args.apply, "targets": len(changes), "image": args.image}
        )
    )


if __name__ == "__main__":
    main()

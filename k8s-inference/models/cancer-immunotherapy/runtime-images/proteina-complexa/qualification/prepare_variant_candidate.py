#!/usr/bin/env python3
"""Render one isolated H100 matrix from current adapter argv and exact fixtures."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path

from fs2_serve.scientific_batch.adapters import proteina_complexa as adapter


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]


def prepare(manifests, image, output, name):
    cases, binary = [], {}
    for manifest, selected in manifests:
        for case in json.loads(manifest.read_text())["cases"]:
            if selected is not None and case["case_id"] not in selected:
                continue
            if case["model_id"] != adapter.MODEL_ID:
                continue
            parameters = adapter.ProteinaParameters.parse(case["arguments"]["parameters"])
            item = case["preparation"]["inputs"][0]
            raw = (manifest.parent / item["local_path"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == item["sha256"]
            bundle_name = item["sha256"] + ".tar.gz"
            binary[bundle_name] = base64.b64encode(raw).decode()
            workspace = "/workspace/" + case["case_id"]
            cases.append({"case_id": case["case_id"], "parameters": case["arguments"]["parameters"],
                "bundle_name": bundle_name, "bundle_sha256": item["sha256"],
                "stages": [{"stage_id": stage, "argv": adapter._argv(parameters, stage),
                    "environment": dict(adapter._environment(parameters, stage, workspace))}
                    for stage in ("generate", "filter", "evaluate", "analyze")]})
    if len(cases) != 5:
        raise ValueError("Expected four original variant cases plus one protein regression")
    execution = json.loads((ROOT / "catalog/runtime/contracts/scientific-execution-map.json").read_text())
    model = next(m for m in execution["models"] if m["model_id"] == adapter.MODEL_ID)
    stage = next(s for s in model["stages"] if s["stage_id"] == "generate")
    volumes = [{"name": "workspace", "emptyDir": {}}, {"name": "tmp", "emptyDir": {}},
               {"name": "inputs", "configMap": {"name": name + "-input"}}]
    mounts = [{"name": "workspace", "mountPath": "/workspace"}, {"name": "tmp", "mountPath": "/tmp"},
              {"name": "inputs", "mountPath": "/inputs", "readOnly": True}]
    for mount in stage["mounts"]:
        if mount["kind"] != "reference":
            continue
        assert mount["read_only"]
        volumes.append({"name": mount["name"], "hostPath": {"path": mount["host_path"], "type": "Directory"}})
        mounts.append({"name": mount["name"], "mountPath": mount["mount_path"],
                       "subPath": mount["sub_path"], "readOnly": True})
    resources = {kind: {"ephemeral-storage" if k == "ephemeral_storage" else k: v
                       for k, v in values.items()} for kind, values in stage["resources"].items()}
    for values in resources.values():
        values["nvidia.com/gpu"] = "1"
    config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name+"-input", "namespace": "fs2-models"},
        "binaryData": binary, "data": {"plan.json": json.dumps({"cases": cases, "image": image}),
            "run_variant_candidate.py": (HERE / "run_variant_candidate.py").read_text(),
            "runtime_entrypoint.py": (HERE.parent / "runtime_entrypoint.py").read_text()}}
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "fs2-models",
        "labels": {"app": name, "fs2-task": "proteina-variant-repair-20260918"}}, "spec": {
        "restartPolicy": "Never", "activeDeadlineSeconds": 14400, "terminationGracePeriodSeconds": 30,
        "automountServiceAccountToken": False, "nodeSelector": stage["required_node_labels"],
        "affinity": {"nodeAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [
            {"weight": 80, "preference": {"matchExpressions": [
                {"key": "accelerator.fs2.nebius/pool-id", "operator": "In", "values": ["h100-1x"]}]}}]}},
        "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                        {"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}],
        "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
                            "supplementalGroups": [1000]}, "volumes": volumes,
        "containers": [{"name": "candidate", "image": image,
            "command": ["/opt/venv/bin/python", "/inputs/run_variant_candidate.py"],
            "workingDir": "/workspace", "volumeMounts": mounts, "resources": resources,
            "securityContext": {"allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]}},
            "env": [{"name": "PYTHONPATH", "value": "/inputs:/opt/fs2/source/src"}]}]}}
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    (output / "candidate.json").write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": [config, pod]}, indent=2)+"\n")
    (output / "plan.json").write_text(json.dumps({"cases": cases, "image": image, "resources": resources,
        "runtime_artifacts": model["runtime_artifacts"], "scope": "isolated same-argv runtime test; no API/scheduler claim"}, indent=2)+"\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variants", type=Path, required=True)
    parser.add_argument("--protein", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    prepare([(args.variants, None), (args.protein, {"proteina-complexa-pdl1-s1-n1"})],
            args.image, args.output, args.name)

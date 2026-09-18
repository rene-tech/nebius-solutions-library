"""Prepare a CXR response-identity successor and isolated Pods, never apply it."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = "nv-reason-cxr-3b"
TEMPLATE_NAME = MODEL + ".response-identity-20260918"
MODULE = "fs2_runtime_identity.RuntimeIdentityMiddleware"
IMAGE = "sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635"
ANNOTATION = "telemetry.fs2.nebius.ai/response-identity"


def candidate_template(previous):
    candidate = copy.deepcopy(previous)
    if candidate["modelRef"] != MODEL or candidate["runtimeContainerName"] != "vllm":
        raise ValueError("unexpected_cxr_template")
    deployment = next(r for r in candidate["resources"] if r["kind"] == "Deployment")
    pod = deployment["spec"]["template"]
    container = next(c for c in pod["spec"]["containers"] if c["name"] == "vllm")
    if not container["image"].endswith("@" + IMAGE) or container.get("command"):
        raise ValueError("expected_plain_pinned_runtime")
    if "--middleware" in container["args"] or any(
        e["name"] == "PYTHONPATH" for e in container["env"]
    ):
        raise ValueError("existing_middleware_or_pythonpath_requires_review")
    source = (ROOT / "models/general-media/fs2_runtime_identity.py").read_text()
    sha = hashlib.sha256(source.encode()).hexdigest()
    config_name = "fs2-response-identity-" + sha[:16]
    candidate["resources"].append(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "immutable": True,
            "metadata": {"name": config_name, "namespace": "fs2-models"},
            "data": {"fs2_runtime_identity.py": source},
        }
    )
    pod["metadata"].setdefault("annotations", {})[ANNOTATION] = "asgi-v1"
    container["args"].extend(["--middleware", MODULE])
    container["env"].extend(
        [
            {
                "name": "FS2_RUNTIME_POD_UID",
                "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
            },
            {"name": "PYTHONPATH", "value": "/opt/fs2-response-identity"},
        ]
    )
    pod["spec"]["volumes"].append(
        {
            "name": "response-identity",
            "configMap": {"name": config_name, "defaultMode": 292},
        }
    )
    container["volumeMounts"].append(
        {
            "name": "response-identity",
            "mountPath": "/opt/fs2-response-identity",
            "readOnly": True,
        }
    )
    path = ROOT / "acceptance/cosmos-stockholm-deployment-20260917/prepare_contract.py"
    spec = importlib.util.spec_from_file_location("attribution_existing_contract", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    candidate["templateDigest"] = module.terraform_digest(candidate["resources"])
    return candidate


def proposal(original, template):
    result = copy.deepcopy(original)
    if result["modelRef"] != MODEL or result["fastStart"]["level"] != "Off":
        raise ValueError("expected_cxr_off")
    result["runtime"]["templateRef"] = {
        "name": TEMPLATE_NAME,
        "digest": template["templateDigest"],
    }
    result["cache"]["snapshotPreference"] = "Never"
    result["cache"].pop("snapshotRef", None)
    return result


def isolated(template, nodes):
    resources = [
        copy.deepcopy(r) for r in template["resources"] if r["kind"] == "ConfigMap"
    ]
    base = next(r for r in template["resources"] if r["kind"] == "Deployment")["spec"][
        "template"
    ]
    selector = {"fs2-serve.nebius.ai/attribution-test": "cxr-20260918"}
    for index in range(2):
        pod = copy.deepcopy(base)
        pod.update(apiVersion="v1", kind="Pod")
        pod["metadata"] = {
            "name": f"fs2-cxr-attribution-{index}-20260918",
            "namespace": "fs2-models",
            "annotations": pod["metadata"]["annotations"],
            "labels": {**selector, "fs2-serve.nebius.ai/model-id": MODEL},
        }
        spec = pod["spec"]
        spec.update(
            restartPolicy="Never",
            activeDeadlineSeconds=3600,
            nodeSelector={"accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb"},
            affinity={
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "kubernetes.io/hostname",
                                        "operator": "In",
                                        "values": list(nodes),
                                    }
                                ]
                            }
                        ]
                    }
                }
            },
        )
        spec.pop("initContainers", None)
        container = next(c for c in spec["containers"] if c["name"] == "vllm")
        for mount in container["volumeMounts"]:
            if mount["name"] == "model-cache":
                mount["readOnly"] = True
        for env in container["env"]:
            if env["name"] in {
                "CUDA_CACHE_PATH",
                "TORCH_EXTENSIONS_DIR",
                "TORCHINDUCTOR_CACHE_DIR",
                "TRITON_CACHE_DIR",
                "VLLM_CACHE_ROOT",
            }:
                env["value"] = "/runtime-cache/" + env["name"].lower()
        resources.append(pod)
    resources.append(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": "fs2-cxr-attribution-20260918",
                "namespace": "fs2-models",
            },
            "spec": {
                "selector": selector,
                "ports": [{"name": "http", "port": 8000, "targetPort": "http"}],
            },
        }
    )
    return {"apiVersion": "v1", "kind": "List", "items": resources}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--nodes", nargs="+", required=True)
    args = parser.parse_args()
    captured = json.loads(args.capture.read_bytes())
    original = captured["modeldeployment"]["spec"]
    digest = original["runtime"]["templateRef"]["digest"]
    bundles = [
        b
        for c in captured["configmaps"]["items"]
        if "renderer-bundles.json" in c.get("data", {})
        for b in json.loads(c["data"]["renderer-bundles.json"])
        if b["modelRef"] == MODEL and b["templateDigest"] == digest
    ]
    if not bundles or any(b != bundles[0] for b in bundles):
        raise ValueError("missing_or_conflicting_original_template")
    candidate = candidate_template(bundles[0])
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, value in {
        "template.json": candidate,
        "original.json": original,
        "proposal.json": proposal(original, candidate),
        "isolated.json": isolated(candidate, args.nodes),
    }.items():
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")
    print(
        json.dumps(
            {
                "applied": False,
                "template_digest": candidate["templateDigest"],
                "pods": 2,
            }
        )
    )


if __name__ == "__main__":
    main()

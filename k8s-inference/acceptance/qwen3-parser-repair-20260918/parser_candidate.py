"""Prepare a parser-only Qwen template or isolated Pod; never contact a cluster."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGE_DIGEST = "2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635"
PARSERS = ["--enable-auto-tool-choice", "--tool-call-parser", "hermes", "--reasoning-parser", "qwen3"]
TEMPLATE_NAME = "qwen3-8b.parsers-20260918"


def parser_template(previous):
    """Extend the plain stored bundle, not a generated snapshot-wrapped workload."""
    candidate = copy.deepcopy(previous)
    if candidate["modelRef"] != "qwen3-8b" or candidate["runtimeContainerName"] != "vllm":
        raise ValueError("unexpected_model")
    deployments = [r for r in candidate["resources"] if r["kind"] == "Deployment"]
    if len(deployments) != 1:
        raise ValueError("expected_one_runtime_deployment")
    spec = deployments[0]["spec"]["template"]["spec"]
    container = next(c for c in spec["containers"] if c["name"] == "vllm")
    if not container["image"].endswith("@sha256:" + IMAGE_DIGEST):
        raise ValueError("unexpected_runtime_image")
    if container.get("command") or any(v["name"].startswith("snapshot-") for v in spec["volumes"]):
        raise ValueError("expected_plain_stored_template_not_generated_snapshot")
    args = container["args"]
    if any(flag in args for flag in PARSERS if flag.startswith("--")):
        raise ValueError("parser_flags_already_present")
    if args[args.index("--max-model-len") + 1] != "32768":
        raise ValueError("unexpected_context_contract")
    args.extend(PARSERS)
    # The existing Terraform/Helm canonical resource digest is authoritative.
    path = ROOT / "acceptance/cosmos-stockholm-deployment-20260917/prepare_contract.py"
    module_spec = importlib.util.spec_from_file_location("qwen_existing_contract", path)
    assert module_spec and module_spec.loader
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    candidate["templateDigest"] = module.terraform_digest(candidate["resources"])
    return candidate


def proposal(previous, template):
    """Do not restore a snapshot of the old parser-less process, even with Off."""
    result = copy.deepcopy(previous)
    if result["modelRef"] != "qwen3-8b" or result["fastStart"]["level"] != "Off":
        raise ValueError("expected_qwen_off_contract")
    result["runtime"]["templateRef"] = {"name": TEMPLATE_NAME, "digest": template["templateDigest"]}
    result["cache"]["snapshotPreference"] = "Never"
    result["cache"].pop("snapshotRef", None)
    return result


def isolated_pod(template, *, node, name, registry):
    """Use existing immutable weights read-only and a private ephemeral compile cache."""
    deployment = next(r for r in template["resources"] if r["kind"] == "Deployment")
    spec = copy.deepcopy(deployment["spec"]["template"]["spec"])
    spec.update(
        restartPolicy="Never",
        activeDeadlineSeconds=1800,
        nodeSelector={"kubernetes.io/hostname": node, "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb"},
    )
    # Already localized content: no download, cache mutation, or snapshot side effects.
    spec.pop("initContainers", None)
    spec["volumes"] = [v for v in spec["volumes"] if v["name"] != "contract"]
    model_volume = next(v for v in spec["volumes"] if v["name"] == "model")
    model_volume["persistentVolumeClaim"]["readOnly"] = True
    container = next(c for c in spec["containers"] if c["name"] == "vllm")
    container["image"] = registry + "/vllm-openai@sha256:" + IMAGE_DIGEST
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": "fs2-models",
            "labels": {
                "app.kubernetes.io/name": "qwen-parser-qualification",
                "fs2-serve.nebius.ai/task": "scientific-qualification-20260918",
            },
        },
        "spec": spec,
    }


def load_bundle(path):
    maps = json.loads(Path(path).read_bytes())["items"]
    bundles = json.loads(
        next(m["data"]["renderer-bundles.json"] for m in maps if "renderer-bundles.json" in m.get("data", {}))
    )
    return next(b for b in bundles if b["modelRef"] == "qwen3-8b")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-configmaps", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--pod-name", required=True)
    parser.add_argument("--registry", required=True)
    args = parser.parse_args()
    candidate = parser_template(load_bundle(args.bundle_configmaps))
    pod = isolated_pod(candidate, node=args.node, name=args.pod_name, registry=args.registry)
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, value in (("candidate-template.json", candidate), ("candidate-pod.json", pod)):
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")
    print(
        json.dumps(
            {"applied": False, "template_digest": candidate["templateDigest"], "pod": args.pod_name, "node": args.node}
        )
    )


if __name__ == "__main__":
    main()

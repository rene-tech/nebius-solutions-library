"""Prepare additive existing-runtime registration for idle visual-science Apps.

Reads current platform configuration and validates the exact pinned runtime;
does not deploy, take ownership, or change node-pool limits. The generated
ConfigMaps retain every previously registered model and pool without alteration.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import yaml

from fs2_serve.model_deployment import (
    InfrastructureEnvelope, LegacyManifestRenderer, LegacyTemplateBundle,
    ModelDeploymentSpec, RenderContext, ValidationDisposition,
    canonical_digest, canonical_json, validate_model_deployment,
)
from remove_hot_floors import kubectl

ROOT = Path(__file__).resolve().parents[2]
MODELS = ("scvi-scanvi", "cellpose-cpsam-v2", "sam2-1-hiera-large")
TOOLS = {"scvi-scanvi": "integrate_single_cell", "cellpose-cpsam-v2": "segment_cells",
         "sam2-1-hiera-large": "segment_track_media", "ace-step-1-5": "generate_music",
         "wan2-2-t2v-nim": "generate_video", "wan2-2-i2v-nim": "animate_image",
         "mindguard-4b": "assess_mindguard_4b", "mindguard-8b": "assess_mindguard_8b"}


def workload_name(model: str) -> str:
    return "fs2-mindguard-r20260916-" + model.removeprefix("mindguard-") if model.startswith("mindguard-") else model


def source_resources(model: str) -> list[dict]:
    if model.startswith("mindguard-"):
        spec = importlib.util.spec_from_file_location("mindguard_renderer", ROOT / "models/mindguard/render_preview.py")
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
        return [r for r in renderer.render(model, replicas=0)["items"] if r["kind"] != "PersistentVolumeClaim"]
    resources = list(yaml.safe_load_all(manifest_path(model).read_text()))
    return [r for r in resources if r["kind"] != "PersistentVolumeClaim"
            and (not model.startswith("wan2-") or r["metadata"]["name"] == model)]


def manifest_path(model: str) -> Path:
    group = "general-media" if model == "ace-step-1-5" or model.startswith("wan2-") else "visual-science"
    filename = "wan2-2-nim" if model.startswith("wan2-") else model
    return ROOT / "models" / group / "k8s" / (filename + ".yaml")


def append_models(envelope: dict, bundles: list, selections: dict,
                  baseline_spec: dict, source_resources: dict, *, models=MODELS) -> tuple:
    envelope, bundles = copy.deepcopy((envelope, bundles))
    proposals = []
    for model in models:
        if model in envelope["qualifications"]:
            raise ValueError("model is already registered; never overwrite it")
        entry = selections["models"].get(model)
        if entry is None:
            entry = json.loads((ROOT / "catalog/runtime/native" / (model + ".json")).read_text())
        record = entry["record"]
        resources = copy.deepcopy(source_resources[model])
        workload = next(r for r in resources if r["kind"] == "Deployment")
        runtime = next(c for c in workload["spec"]["template"]["spec"]["containers"]
                       if c["image"] == record["runtime"]["image"]["reference"])
        service = next(r for r in resources if r["kind"] == "Service")
        digest = canonical_digest(resources)
        artifact = "sha256:" + record["cache"]["artifact"]["manifest_digest"]
        tool = TOOLS[model]
        pool_ref = ("wan2-h200-1x" if model.startswith("wan2-") else
                    "l40s-1x" if model == "ace-step-1-5" or model.startswith("mindguard-") else "h100-ondemand-1x")
        accelerator_class = envelope["pools"][pool_ref]["acceleratorClass"]
        bundle = {"modelRef": model, "runtimeProfile": "custom",
                  "templateDigest": digest, "primaryWorkloadName": workload["metadata"]["name"],
                  "runtimeContainerName": runtime["name"],
                  "primaryServiceName": service["metadata"]["name"],
                  "primaryServicePort": service["spec"]["ports"][0]["port"],
                  "resources": resources}
        LegacyTemplateBundle.model_validate(bundle)
        bundles.append(bundle)
        envelope["qualifications"][model] = {
            "modelRef": model, "runtimeProfile": "custom",
            "artifactManifestDigests": [artifact],
            "artifactRevisions": {record["model"]["source"]["revision"]: artifact},
            "runtimeImages": [runtime["image"]],
            "acceleratorClasses": [accelerator_class],
            "maxAcceleratorsPerReplica": 1, "scaleToZeroQualified": False,
            "templateDigests": [digest], "templateRefs": {model + ".legacy-v1": digest},
            "templateCacheTiers": {digest: "Disabled"},
            "openAIQualified": False, "mcpToolName": tool,
        }
        spec = copy.deepcopy(baseline_spec)
        spec.update(modelRef=model, artifact={"manifestDigest": artifact,
                    "revision": record["model"]["source"]["revision"]},
                    runtime={"image": runtime["image"], "profile": "custom",
                    "templateRef": {"name": model + ".legacy-v1", "digest": digest}})
        spec["availability"].update(minReplicas=0, maxReplicas=1)
        # Same qualified GPU SKU, using an existing single-GPU pool. Do not
        # consume the two full H100 nodes needed for multi-GPU execution.
        spec["placement"]["poolRefs"] = [pool_ref]
        spec["cache"] = {"tier": "Disabled", "snapshotPreference": "Never"}
        spec["exposure"]["mcpToolName"] = tool
        spec["policy"]["allowedPrincipalIds"] = []
        proposals.append({"name": model, "namespace": "fs2-models", "spec": spec})
    envelope.pop("revision")
    envelope["revision"] = canonical_digest(envelope)
    typed = InfrastructureEnvelope.model_validate(envelope)
    renderer = LegacyManifestRenderer({(b["modelRef"], b["templateDigest"]):
                                      LegacyTemplateBundle.model_validate(b) for b in bundles})
    for proposal in proposals:
        spec = ModelDeploymentSpec.model_validate(proposal["spec"])
        decision = validate_model_deployment(spec, typed)
        if decision.disposition is not ValidationDisposition.ACCEPTED:
            raise ValueError(decision.model_dump(mode="json", by_alias=True))
        pool = typed.pools[decision.admitted_pool_ref]
        renderer.render(spec, RenderContext(name=proposal["name"], namespace="fs2-models",
                        uid="qualification-not-live", generation=1, pool=pool,
                        eligible_pools=[pool],
                        prometheus_server_address="http://prometheus.invalid:9090"))
    return envelope, bundles, proposals


def main(args: argparse.Namespace) -> None:
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    def get(kind: str, name: str, namespace: str = "fs2-system") -> dict:
        return json.loads(kubectl(args.context, "-n", namespace, "get", kind, name, "-o", "json"))
    original = {key: get("configmap", getattr(args, key)) for key in ("envelope", "bundles", "routes")}
    live = {model: get("deploy,svc", workload_name(model), "fs2-models") for model in args.models}
    baseline = get("modeldeployment", "altumage", "fs2-models")
    selections = json.loads(original["routes"]["data"]["deployment-runtimes.json"])
    resources = {model: source_resources(model) for model in args.models}
    for model in args.models:
        source_images = [c["image"] for r in resources[model] if r["kind"] == "Deployment"
                         for c in r["spec"]["template"]["spec"]["containers"]]
        live_images = [c["image"] for r in live[model]["items"] if r["kind"] == "Deployment"
                       for c in r["spec"]["template"]["spec"]["containers"]]
        if source_images != live_images:
            raise ValueError("source runtime differs from currently deployed " + model)
    prior_envelope = json.loads(original["envelope"]["data"]["infrastructure-envelope.json"])
    if args.existing_pool:
        declaration = json.loads(args.existing_pool.read_text())
        pool = declaration["envelope"]
        if pool["poolId"] in prior_envelope["pools"]:
            raise ValueError("refuse overwrite of an existing pool")
        prior_envelope["pools"][pool["poolId"]] = pool
    if args.repair_existing_tool_names:
        for model in MODELS:
            qualification = prior_envelope["qualifications"][model]
            if qualification["mcpToolName"] not in {"infer_" + model.replace("-", "_"), TOOLS[model]}:
                raise ValueError("unreviewed tool identity; do not overwrite")
            qualification["mcpToolName"] = TOOLS[model]
    envelope, bundles, proposals = append_models(
        prior_envelope,
        json.loads(original["bundles"]["data"]["renderer-bundles.json"]),
        selections, baseline["spec"], resources, models=args.models,
    )
    configs = []
    for prefix, key, value in (("envelope", "infrastructure-envelope.json", envelope),
                                ("bundles", "renderer-bundles.json", bundles)):
        data = {key: canonical_json(value).decode()}
        name = "fs2-idle-" + prefix + "-" + hashlib.sha256(canonical_json(data)).hexdigest()[:12]
        configs.append({"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
                        "metadata": {"name": name, "namespace": "fs2-system",
                                     "labels": {"workload.fs2.nebius/owner": "idle-scale-zero-20261005"}},
                        "data": data})
    for name, value in (("before-configmaps", original), ("before-workloads", live),
                        ("proposals", proposals),
                        ("registration-configmaps", {"apiVersion": "v1", "kind": "List", "items": configs})):
        (args.output / (name + ".json")).write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps({"models": args.models, "configmaps": [c["metadata"]["name"] for c in configs],
                      "source_runtime_matches_live": True, "apply_performed": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("context", "envelope", "bundles", "routes"):
        parser.add_argument("--" + option, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models", choices=tuple(TOOLS), nargs="+", default=MODELS)
    parser.add_argument("--repair-existing-tool-names", action="store_true")
    parser.add_argument("--existing-pool", type=Path)
    main(parser.parse_args())

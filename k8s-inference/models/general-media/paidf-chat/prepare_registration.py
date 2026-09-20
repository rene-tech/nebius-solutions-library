"""Prepare additive canary registration against a fresh live snapshot; no apply."""

import argparse
import copy
import hashlib
import json
from pathlib import Path

from fs2_serve.model_deployment import (
    InfrastructureEnvelope, LegacyManifestRenderer, LegacyTemplateBundle,
    ModelDeploymentSpec, RenderContext, ValidationDisposition,
    canonical_digest, canonical_json, validate_model_deployment,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TRANSFER = "cosmos-transfer2-5-2b"
TENANT = "video-canary-20260920"
ADAPTER = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/general-media/cosmos-transfer25-adapter@sha256:4297473ad12501a70ef4d1e0b706bd829a660e4e1e946b82f2d5da44fdab2dea"
OLD_ADAPTER = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/general-media/cosmos-transfer25-adapter@sha256:1a14cccb640d08ae5775f6ebcb9e2fe766f08b717e43526e30057a8c68edfadd"


def prepare(envelope, bundles, routes, current):
    envelope, bundles, routes = copy.deepcopy((envelope, bundles, routes))
    if current["spec"]["modelRef"] != TRANSFER or current["spec"]["tenantId"] != TENANT:
        raise ValueError("Not the authorized isolated canary")
    selections = json.loads(routes["deployment-runtimes.json"])
    original_ids = set(selections["models"])
    runtime = json.loads((HERE / "runtime-20260920.json").read_text())["items"]
    proposals, new_bundles = [], []
    for model, pool_id, profile in (("qwen3-6-27b-fp8", "h100-ondemand-1x", "vllm"),
                                     ("qwen2-5-14b-instruct", "l40s-1x", "vllm"),
                                     (TRANSFER, "h100-ondemand-1x", "nim")):
        entry = json.loads((ROOT / "catalog/runtime/deployment-runtimes" / (model + ".json")).read_text())
        record = entry["record"]
        if model != TRANSFER and (model in envelope["qualifications"] or model in selections["models"]):
            raise ValueError("New model already registered; refresh the snapshot and inspect")
        if model == TRANSFER:
            old_digest = current["spec"]["runtime"]["templateRef"]["digest"]
            matches = [value for value in bundles if value["modelRef"] == model and value["templateDigest"] == old_digest]
            if len(matches) != 1 or canonical_digest(matches[0]["resources"]) != old_digest:
                raise ValueError("Current Transfer bundle is not unique and exact")
            bundle = copy.deepcopy(matches[0])
            deployment = next(item for item in bundle["resources"] if item["kind"] == "Deployment")
            adapter = next(item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "adapter")
            if adapter["image"] != OLD_ADAPTER:
                raise ValueError("Transfer adapter changed concurrently; inspect instead of replacing")
            adapter["image"] = ADAPTER
            digest = canonical_digest(bundle["resources"])
            bundle["templateDigest"] = digest
            qualification = envelope["qualifications"][model]
        else:
            resources = [item for item in runtime if item["metadata"]["name"] == model]
            digest = canonical_digest(resources)
            bundle = {"modelRef": model, "runtimeProfile": profile, "templateDigest": digest,
                      "primaryWorkloadName": model, "runtimeContainerName": "vllm",
                      "primaryServiceName": model, "primaryServicePort": 8000, "resources": resources}
            qualification = {"modelRef": model, "runtimeProfile": profile,
                             "artifactManifestDigests": ["sha256:" + record["cache"]["artifact"]["manifest_digest"]],
                             "artifactRevisions": {record["model"]["source"]["revision"]: "sha256:" + record["cache"]["artifact"]["manifest_digest"]},
                             "runtimeImages": [record["runtime"]["image"]["reference"]],
                             "acceleratorClasses": [record["resources"]["gpu"]["class"].lower()],
                             "maxAcceleratorsPerReplica": 1, "scaleToZeroQualified": False,
                             "templateDigests": [], "templateRefs": {}, "templateCacheTiers": {},
                             "openAIQualified": False, "mcpToolName": "infer_" + model.replace("-", "_"),
                             "snapshotDigests": [], "gpuSnapshotBundles": {}, "fastStartRuntimeContracts": [], "fastStartEvidence": []}
            envelope["qualifications"][model] = qualification
        name = model + ".paidf-v1"
        if name in qualification["templateRefs"]:
            raise ValueError("This template revision already exists")
        qualification["templateDigests"].append(digest)
        qualification["templateRefs"][name] = digest
        qualification["templateCacheTiers"][digest] = "Disabled"
        bundles.append(bundle)
        new_bundles.append(bundle)
        selections["models"][model] = entry
        spec = copy.deepcopy(current["spec"])
        spec.update(modelRef=model, artifact={"manifestDigest": "sha256:" + record["cache"]["artifact"]["manifest_digest"], "revision": record["model"]["source"]["revision"]},
                    runtime={"profile": profile, "image": record["runtime"]["image"]["reference"], "templateRef": {"name": name, "digest": digest}})
        spec["placement"]["poolRefs"] = [pool_id]
        spec["availability"].update(minReplicas=1, maxReplicas=1, startupTimeoutSeconds=3600)
        spec["exposure"]["mcpToolName"] = "infer_" + model.replace("-", "_")
        spec["policy"]["allowedPrincipalIds"] = [TENANT]
        spec["rollout"].update(strategy="Recreate", maxSurge=0, maxUnavailable=1)
        proposal = {"name": model, "namespace": "fs2-models", "spec": spec}
        if model == TRANSFER:
            proposal["base_etag"] = current["etag"]
        proposals.append(proposal)
    envelope.pop("revision")
    envelope["revision"] = canonical_digest(envelope)
    typed = InfrastructureEnvelope.model_validate(envelope)
    for proposal, bundle in zip(proposals, new_bundles, strict=True):
        spec = ModelDeploymentSpec.model_validate(proposal["spec"])
        decision = validate_model_deployment(spec, typed)
        if decision.disposition is not ValidationDisposition.ACCEPTED:
            raise ValueError(decision.model_dump(mode="json", by_alias=True))
        typed_bundle = LegacyTemplateBundle.model_validate(bundle)
        pool_id, = proposal["spec"]["placement"]["poolRefs"]
        LegacyManifestRenderer({(proposal["name"], bundle["templateDigest"]): typed_bundle}).render(
            spec, RenderContext(name=proposal["name"], namespace="fs2-models", generation=1,
                                pool=typed.pools[pool_id], eligible_pools=[typed.pools[pool_id]],
                                prometheus_server_address="http://prometheus.fs2-observability.svc:9090", preview=True))
    routes["deployment-runtimes.json"] = canonical_json(selections).decode()
    configmaps, names = [], {}
    for field, data in (("envelope", {"infrastructure-envelope.json": canonical_json(envelope).decode()}),
                        ("bundles", {"renderer-bundles.json": canonical_json(bundles).decode()}), ("routes", routes)):
        name = "fs2-paidf-" + field + "-" + hashlib.sha256(canonical_json(data)).hexdigest()[:16]
        names[field] = name
        configmaps.append({"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
                           "metadata": {"name": name, "namespace": "fs2-system", "labels": {"workload.fs2.nebius/owner": TENANT}}, "data": data})
    if set(selections["models"]) - {"qwen3-6-27b-fp8", "qwen2-5-14b-instruct"} != original_ids:
        raise ValueError("Existing model selection inventory changed")
    return {"configmaps": {"apiVersion": "v1", "kind": "List", "items": configmaps}, "proposals": proposals,
            "values": {"modelController": {"infrastructureEnvelopeConfigMapName": names["envelope"], "rendererBundlesConfigMapName": names["bundles"]}, "catalog": {"leanRoutes": {"configMapName": names["routes"]}}},
            "apply_performed": False, "preserved_models": len(original_ids) - 1}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(**json.loads(args.snapshot.read_text()))
    args.output_directory.mkdir(mode=0o700, exist_ok=False)
    for field in ("configmaps", "proposals", "values"):
        path = args.output_directory / (field + ".json")
        path.write_text(json.dumps(result[field], indent=2) + "\n")
        path.chmod(0o600)
    print(json.dumps({"prepared": True, "apply_performed": False, "preserved_models": result["preserved_models"]}))

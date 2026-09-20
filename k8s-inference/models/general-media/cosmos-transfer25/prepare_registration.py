"""Pure additive registration for the isolated Transfer App; never applies it.

Inputs must be a fresh protected snapshot of the live Helm-selected ConfigMaps.
The caller checks the live release is unchanged before applying the result.
The independently created service account and PVC are retained dependencies.
"""

import argparse
import copy
import hashlib
import json
from pathlib import Path

from fs2_serve.model_deployment import (
    InfrastructureEnvelope,
    LegacyManifestRenderer,
    LegacyTemplateBundle,
    ModelDeploymentSpec,
    RenderContext,
    ValidationDisposition,
    canonical_digest,
    canonical_json,
    validate_model_deployment,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
MODEL = "cosmos-transfer2-5-2b"


def prepare(envelope, bundles, routes, baseline_spec):
    envelope, bundles, routes = copy.deepcopy((envelope, bundles, routes))
    selections = json.loads(routes["deployment-runtimes.json"])
    if MODEL in envelope["qualifications"] or MODEL in selections["models"]:
        raise ValueError("Transfer is already registered; inspect instead of overwriting")
    entry = json.loads((ROOT / "catalog/runtime/deployment-runtimes" / (MODEL + ".json")).read_text())
    record = entry["record"]
    resources = [
        item
        for item in json.loads((HERE / "runtime-20260920.json").read_text())["items"]
        if item["kind"] in {"Deployment", "Service"}
    ]
    template_digest = canonical_digest(resources)
    manifest_digest = "sha256:" + record["cache"]["artifact"]["manifest_digest"]
    runtime_image = record["runtime"]["image"]["reference"]
    tool = "infer_" + MODEL.replace("-", "_")
    bundle = {
        "modelRef": MODEL,
        "runtimeProfile": "nim",
        "templateDigest": template_digest,
        "primaryWorkloadName": MODEL,
        "runtimeContainerName": "nim",
        "primaryServiceName": MODEL,
        "primaryServicePort": 8000,
        "resources": resources,
    }
    LegacyTemplateBundle.model_validate(bundle)
    bundles.append(bundle)
    selections["models"][MODEL] = entry
    envelope["qualifications"][MODEL] = {
        "modelRef": MODEL,
        "runtimeProfile": "nim",
        "artifactManifestDigests": [manifest_digest],
        "artifactRevisions": {record["model"]["source"]["revision"]: manifest_digest},
        "runtimeImages": [runtime_image],
        "acceleratorClasses": ["nvidia-h100-sxm5-80gb"],
        "maxAcceleratorsPerReplica": 1,
        "scaleToZeroQualified": False,
        "templateDigests": [template_digest],
        "templateRefs": {MODEL + ".canary-v1": template_digest},
        "templateCacheTiers": {template_digest: "Disabled"},
        "openAIQualified": False,
        "mcpToolName": tool,
        "snapshotDigests": [],
        "gpuSnapshotBundles": {},
        "fastStartRuntimeContracts": [],
        "fastStartEvidence": [],
    }
    if "video-canary-20260920" not in envelope["tenantIds"]:
        envelope["tenantIds"].append("video-canary-20260920")
    spec = copy.deepcopy(baseline_spec)
    spec.update(
        modelRef=MODEL,
        tenantId="video-canary-20260920",
        artifact={"manifestDigest": manifest_digest, "revision": record["model"]["source"]["revision"]},
        runtime={
            "image": runtime_image,
            "profile": "nim",
            "templateRef": {"name": MODEL + ".canary-v1", "digest": template_digest},
        },
    )
    spec["availability"].update(minReplicas=1, maxReplicas=1)
    spec["placement"]["poolRefs"] = ["h100-ondemand-1x"]
    spec["policy"]["allowedPrincipalIds"] = ["video-canary-20260920"]
    spec["exposure"]["mcpToolName"] = tool
    spec["exposure"]["openAI"] = False
    spec["rollout"].update(strategy="Recreate", maxSurge=0, maxUnavailable=1)
    spec["adoption"] = {"mode": "None"}
    envelope.pop("revision")
    envelope["revision"] = canonical_digest(envelope)
    typed = InfrastructureEnvelope.model_validate(envelope)
    decision = validate_model_deployment(ModelDeploymentSpec.model_validate(spec), typed)
    if decision.disposition is not ValidationDisposition.ACCEPTED:
        raise ValueError(decision.model_dump(mode="json", by_alias=True))
    typed_bundle = LegacyTemplateBundle.model_validate(bundle)
    LegacyManifestRenderer({(MODEL, template_digest): typed_bundle}).render(
        ModelDeploymentSpec.model_validate(spec),
        RenderContext(
            name=MODEL,
            namespace="fs2-models",
            generation=1,
            pool=typed.pools["h100-ondemand-1x"],
            eligible_pools=[typed.pools["h100-ondemand-1x"]],
            prometheus_server_address="http://prometheus.fs2-observability.svc:9090",
            preview=True,
        ),
    )
    configs = []

    def config(prefix, data):
        name = prefix + hashlib.sha256(canonical_json(data)).hexdigest()[:16]
        configs.append(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "immutable": True,
                "metadata": {
                    "name": name,
                    "namespace": "fs2-system",
                    "labels": {"workload.fs2.nebius/owner": "video-canary-20260920"},
                },
                "data": data,
            }
        )
        return name

    envelope_name = config(
        "fs2-transfer25-envelope-", {"infrastructure-envelope.json": canonical_json(envelope).decode()}
    )
    bundles_name = config("fs2-transfer25-bundles-", {"renderer-bundles.json": canonical_json(bundles).decode()})
    routes["deployment-runtimes.json"] = canonical_json(selections).decode()
    routes_name = config("fs2-transfer25-routes-", routes)
    return {
        "configmaps": {"apiVersion": "v1", "kind": "List", "items": configs},
        "values": {
            "modelController": {
                "infrastructureEnvelopeConfigMapName": envelope_name,
                "rendererBundlesConfigMapName": bundles_name,
            },
            "catalog": {"leanRoutes": {"configMapName": routes_name}},
        },
        "proposal": {"name": MODEL, "namespace": "fs2-models", "spec": spec},
        "preserved_models": len(selections["models"]) - 1,
        "apply_performed": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path)
    args = parser.parse_args()
    inputs = json.loads(args.snapshot.read_text())
    result = prepare(**inputs)
    if args.output_directory:
        args.output_directory.mkdir(mode=0o700, exist_ok=False)
        for field in ("configmaps", "values", "proposal"):
            path = args.output_directory / (field + ".json")
            path.write_text(json.dumps(result[field], indent=2) + "\n")
            path.chmod(0o600)
        print(json.dumps({"prepared": True, "preserved_models": result["preserved_models"], "apply_performed": False}))
    else:
        print(json.dumps(result))

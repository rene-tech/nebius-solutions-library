"""Prepare additive voice qualification ConfigMaps for this existing cluster.

Read-only against Kubernetes. Does NOT apply, change infrastructure limits, or
mark public/elastic/snapshot acceptance complete. Preserve every existing model
and pool byte-for-byte at the decoded-object level. New installations use the
catalog/native and model profiles through Terraform; this scoped live overlay
avoids applying unrelated pending infrastructure changes during onboarding.
"""

import argparse
import copy
import hashlib
import json
import subprocess
from pathlib import Path

import yaml

from fs2_serve.model_deployment import (
    InfrastructureEnvelope,
    LegacyTemplateBundle,
    ModelDeploymentSpec,
    ValidationDisposition,
    canonical_digest,
    canonical_json,
    validate_model_deployment,
)

IDS = (
    "parakeet-realtime-eou-120m-v1",
    "magpie-tts-multilingual-357m",
    "diar-streaming-sortformer-4spk-v2-1",
)
ROOT = Path(__file__).resolve().parents[2]


def append_models(envelope, bundles, selections, baseline_spec):
    envelope, bundles, selections = copy.deepcopy((envelope, bundles, selections))
    proposals = []
    for identity in IDS:
        if identity in envelope["qualifications"] or identity in selections["models"]:
            raise ValueError(
                "voice already registered; inspect live state instead of overwriting"
            )
        entry = json.loads(
            (
                ROOT / "catalog/runtime/deployment-runtimes" / (identity + ".json")
            ).read_text()
        )
        record = entry["record"]
        resources = list(yaml.safe_load_all(
            (ROOT / "models/voice-agent/k8s" / (identity + ".yaml")).read_text()
        ))
        digest = canonical_digest(resources)
        manifest = "sha256:" + record["cache"]["artifact"]["manifest_digest"]
        tool = "infer_" + identity.replace("-", "_")
        bundle = {
            "modelRef": identity,
            "runtimeProfile": "custom",
            "templateDigest": digest,
            "primaryWorkloadName": identity,
            "runtimeContainerName": "voice",
            "primaryServiceName": identity,
            "primaryServicePort": 8000,
            "resources": resources,
        }
        LegacyTemplateBundle.model_validate(bundle)
        bundles.append(bundle)
        selections["models"][identity] = entry
        envelope["qualifications"][identity] = {
            "modelRef": identity,
            "runtimeProfile": "custom",
            "artifactManifestDigests": [manifest],
            "artifactRevisions": {record["model"]["source"]["revision"]: manifest},
            "runtimeImages": [record["runtime"]["image"]["reference"]],
            "acceleratorClasses": ["nvidia-l40s-48gb"],
            "maxAcceleratorsPerReplica": 1,
            "scaleToZeroQualified": False,
            "templateDigests": [digest],
            "templateRefs": {identity + ".legacy-v1": digest},
            "templateCacheTiers": {digest: "Disabled"},
            "openAIQualified": False,
            "mcpToolName": tool,
            "snapshotDigests": [],
            "gpuSnapshotBundles": {},
            "fastStartRuntimeContracts": [],
            "fastStartEvidence": [],
        }
        spec = copy.deepcopy(baseline_spec)
        spec.update(
            modelRef=identity,
            artifact={
                "manifestDigest": manifest,
                "revision": record["model"]["source"]["revision"],
            },
            runtime={
                "image": record["runtime"]["image"]["reference"],
                "profile": "custom",
                "templateRef": {"name": identity + ".legacy-v1", "digest": digest},
            },
        )
        spec["availability"].update(minReplicas=1, maxReplicas=2)
        spec["placement"]["poolRefs"] = ["l40s-1x"]
        # Shared platform Apps follow the customer's API-key model grants.
        # Legacy principal allowlists are local to the operator tenant, not
        # customer tenant IDs; putting "rene" here would reject Rene's key.
        spec["policy"]["allowedPrincipalIds"] = []
        spec["exposure"]["mcpToolName"] = tool
        proposals.append({"name": identity, "namespace": "fs2-models", "spec": spec})
    envelope.pop("revision")
    envelope["revision"] = canonical_digest(envelope)
    typed = InfrastructureEnvelope.model_validate(envelope)
    for proposal in proposals:
        decision = validate_model_deployment(
            ModelDeploymentSpec.model_validate(proposal["spec"]), typed
        )
        if decision.disposition is not ValidationDisposition.ACCEPTED:
            raise ValueError(decision.model_dump(mode="json", by_alias=True))
    return envelope, bundles, selections, proposals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "kubeconfig",
        "context",
        "envelope-configmap",
        "bundles-configmap",
        "routes-configmap",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    command = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context]

    def get(kind, name, namespace="fs2-system"):
        return json.loads(
            subprocess.check_output(
                command + ["-n", namespace, "get", kind, name, "-o", "json"]
            )
        )

    base_envelope = get("configmap", args.envelope_configmap)["data"]
    base_bundles = get("configmap", args.bundles_configmap)["data"]
    routes = get("configmap", args.routes_configmap)["data"]
    baseline_spec = get("modeldeployment", "altumage", "fs2-models")["spec"]
    envelope, bundles, selections, proposals = append_models(
        json.loads(base_envelope["infrastructure-envelope.json"]),
        json.loads(base_bundles["renderer-bundles.json"]),
        json.loads(routes["deployment-runtimes.json"]),
        baseline_spec,
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
                    "labels": {"workload.fs2.nebius/owner": "nemotron-voice-20260916"},
                },
                "data": data,
            }
        )
        return name

    envelope_name = config(
        "fs2-voice-envelope-",
        {"infrastructure-envelope.json": canonical_json(envelope).decode()},
    )
    bundles_name = config(
        "fs2-voice-bundles-",
        {"renderer-bundles.json": canonical_json(bundles).decode()},
    )
    routes["deployment-runtimes.json"] = canonical_json(selections).decode()
    routes_name = config("fs2-voice-routes-", routes)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "registration-configmaps.json").write_text(
        json.dumps({"apiVersion": "v1", "kind": "List", "items": configs}, indent=2)
        + "\n"
    )
    (args.output / "registration-values.json").write_text(
        json.dumps(
            {
                "replicaCount": 3,
                "modelController": {
                    "infrastructureEnvelopeConfigMapName": envelope_name,
                    "rendererBundlesConfigMapName": bundles_name,
                },
                "catalog": {"leanRoutes": {"configMapName": routes_name}},
            },
            indent=2,
        )
        + "\n"
    )
    (args.output / "app-proposals.json").write_text(
        json.dumps(proposals, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "new_models": list(IDS),
                "configmaps": [c["metadata"]["name"] for c in configs],
                "existing_models_preserved": len(envelope["qualifications"]) - len(IDS),
                "apply_performed": False,
            }
        )
    )


if __name__ == "__main__":
    main()

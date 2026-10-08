"""Add the two CPU Apps without changing sibling models, pools or customer keys.

Prepare is read-only. Rollout/apply/grant are separate explicit steps. Preserve
the private directory: it contains pre-change manifests and rollback identity.
The public website's intentional domain redirect is not changed by this tool.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

import httpx
import yaml

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

ROOT = Path(__file__).resolve().parents[2]
MODELS = ("admet-ai", "ctoxpred2")
DEPLOYMENTS = ("fs2-serve-control-plane-model-controller", "fs2-serve-control-plane")
CONFIG_KEYS = (
    "infrastructure-envelope.json",
    "renderer-bundles.json",
    "deployment-runtimes.json",
)
EXPECTED_IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane@sha256:597732bbdc89a1aa5d9e7a5d04295090064e14d85891823fd94db87d154dc7e4"


def kubectl(args, *parts):
    return subprocess.check_output(
        ["kubectl", "--context", args.context, *parts], text=True
    )


def get(args, kind, name, namespace="fs2-system"):
    return json.loads(kubectl(args, "-n", namespace, "get", kind, name, "-o", "json"))


def write(directory, name, value):
    path = directory / (name + ".json")
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    path.chmod(0o600)


@contextmanager
def admin(args):
    value = get(args, "secret", "fs2-serve-admin")
    token = base64.b64decode(value["data"]["token"]).decode().strip()
    with httpx.Client(
        base_url=args.origin,
        timeout=120,
        trust_env=False,
        headers={"Origin": args.origin},
    ) as client:
        client.post(
            "/admin/api/v1/session", headers={"Authorization": "Bearer " + token}
        ).raise_for_status()
        try:
            yield client
        finally:
            client.delete("/admin/api/v1/session").raise_for_status()


def prepare(args):
    before = {name: get(args, "deployment", name) for name in DEPLOYMENTS}
    for deployment in before.values():
        assert (
            deployment["spec"]["template"]["spec"]["containers"][0]["image"]
            == EXPECTED_IMAGE
        ), "live source changed; reconcile before release"
    configs = {}
    for volume in before[DEPLOYMENTS[-1]]["spec"]["template"]["spec"]["volumes"]:
        config = volume.get("configMap", {})
        keys = {entry["key"] for entry in config.get("items", [])}
        if keys.intersection(CONFIG_KEYS):
            configs[config["name"]] = get(args, "configmap", config["name"])
    assert len(configs) == 3
    payload = {
        key: json.loads(value)
        for item in configs.values()
        for key, value in item["data"].items()
    }
    envelope = payload[CONFIG_KEYS[0]]
    bundles = payload[CONFIG_KEYS[1]]
    selections = payload[CONFIG_KEYS[2]]
    baseline = get(args, "modeldeployment", "phenoage", "fs2-models")["spec"]
    original = copy.deepcopy((envelope, bundles, selections))
    proposals = []
    for model in MODELS:
        assert (
            model not in envelope["qualifications"]
            and model not in selections["models"]
        ), "already registered; inspect instead of overwriting"
        entry = json.loads(
            (
                ROOT / "catalog/runtime/deployment-runtimes" / f"{model}-cpu.json"
            ).read_text()
        )
        record = entry["record"]
        resources = list(
            yaml.safe_load_all(
                (ROOT / "models/toxicology/k8s" / f"{model}.yaml").read_text()
            )
        )
        digest = canonical_digest(resources)
        image = record["runtime"]["image"]["reference"]
        artifact = "sha256:" + record["cache"]["artifact"]["manifest_digest"]
        revision = record["model"]["source"]["revision"]
        cpu = {
            "cpuMillis": record["resources"]["cpu_millis"],
            "memoryBytes": record["resources"]["memory_bytes"],
        }
        tool = "infer_" + model.replace("-", "_")
        bundles.append(
            {
                "modelRef": model,
                "runtimeProfile": "custom",
                "templateDigest": digest,
                "primaryWorkloadName": model,
                "runtimeContainerName": "runtime",
                "primaryServiceName": model,
                "primaryServicePort": 8000,
                "resources": resources,
            }
        )
        envelope["qualifications"][model] = {
            "modelRef": model,
            "runtimeProfile": "custom",
            "artifactManifestDigests": [artifact],
            "artifactRevisions": {revision: artifact},
            "runtimeImages": [image],
            "acceleratorClasses": ["CPU"],
            "maxAcceleratorsPerReplica": 0,
            "cpuResources": cpu,
            "localQueue": "general-cpu",
            "scaleToZeroQualified": False,
            "templateDigests": [digest],
            "templateRefs": {model + ".legacy-v1": digest},
            "templateCacheTiers": {digest: "Disabled"},
            "openAIQualified": False,
            "mcpToolName": tool,
        }
        spec = copy.deepcopy(baseline)
        spec.update(
            modelRef=model,
            artifact={"manifestDigest": artifact, "revision": revision},
            runtime={
                "image": image,
                "profile": "custom",
                "templateRef": {"name": model + ".legacy-v1", "digest": digest},
            },
        )
        spec["availability"].update(minReplicas=0, maxReplicas=2)
        spec["placement"].update(
            poolRefs=["batch-cpu"], acceleratorsPerReplica=0, cpuResources=cpu
        )
        spec["exposure"]["mcpToolName"] = tool
        spec["policy"]["allowedPrincipalIds"] = []
        proposals.append({"name": model, "namespace": "fs2-models", "spec": spec})
        selections["models"][model] = entry
    # Prove this projection is additive: all pre-existing declarations survive.
    assert envelope["pools"] == original[0]["pools"]
    assert all(
        envelope["qualifications"][key] == value
        for key, value in original[0]["qualifications"].items()
    )
    assert bundles[:-2] == original[1]
    assert all(
        selections["models"][key] == value
        for key, value in original[2]["models"].items()
    )
    envelope.pop("revision")
    envelope["revision"] = canonical_digest(envelope)
    typed = InfrastructureEnvelope.model_validate(envelope)
    renderer = LegacyManifestRenderer(
        {
            (b["modelRef"], b["templateDigest"]): LegacyTemplateBundle.model_validate(b)
            for b in bundles
        }
    )
    for proposal in proposals:
        spec = ModelDeploymentSpec.model_validate(proposal["spec"])
        decision = validate_model_deployment(spec, typed)
        assert decision.disposition is ValidationDisposition.ACCEPTED, (
            decision.model_dump(mode="json")
        )
        pool = typed.pools[decision.admitted_pool_ref]
        renderer.render(
            spec,
            RenderContext(
                name=proposal["name"],
                namespace="fs2-models",
                uid="qualification-not-live",
                generation=1,
                pool=pool,
                eligible_pools=[pool],
                prometheus_server_address="http://prometheus.invalid:9090",
            ),
        )
    replacements = []
    mapping = {}
    for old, item in configs.items():
        data = {key: canonical_json(payload[key]).decode() for key in item["data"]}
        name = "fs2-toxicology-" + canonical_digest(data).split(":")[1][:16]
        mapping[old] = name
        replacements.append(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "immutable": True,
                "metadata": {
                    "name": name,
                    "namespace": "fs2-system",
                    "labels": {"workload.fs2.nebius/owner": "toxicology-20261008"},
                },
                "data": data,
            }
        )
    for name, value in (
        ("before-deployments", before),
        ("before-configmaps", configs),
        ("configmap-mapping", mapping),
        ("proposals", proposals),
        (
            "registration-configmaps",
            {"apiVersion": "v1", "kind": "List", "items": replacements},
        ),
    ):
        write(args.directory, name, value)
    print(
        json.dumps(
            {
                "prepared": list(MODELS),
                "sibling_models_and_pools_preserved": True,
                "live_mutations": False,
            }
        )
    )


def rollout(args):
    assert args.image and "@sha256:" in args.image
    before = json.loads((args.directory / "before-deployments.json").read_text())
    mapping = json.loads((args.directory / "configmap-mapping.json").read_text())
    for name in DEPLOYMENTS:
        assert (
            get(args, "deployment", name)["spec"]["template"]
            == before[name]["spec"]["template"]
        ), "concurrent rollout; stop"
    kubectl(
        args,
        "apply",
        "--dry-run=server",
        "-f",
        str(args.directory / "registration-configmaps.json"),
    )
    kubectl(args, "apply", "-f", str(args.directory / "registration-configmaps.json"))
    receipts = []
    for name in DEPLOYMENTS:
        current = get(args, "deployment", name)
        expected = copy.deepcopy(current["spec"]["template"])
        patch = [
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": current["metadata"]["resourceVersion"],
            }
        ]
        for index, volume in enumerate(expected["spec"]["volumes"]):
            old = volume.get("configMap", {}).get("name")
            if old in mapping:
                volume["configMap"]["name"] = mapping[old]
                patch.append(
                    {
                        "op": "replace",
                        "path": f"/spec/template/spec/volumes/{index}/configMap/name",
                        "value": mapping[old],
                    }
                )
        expected["spec"]["containers"][0]["image"] = args.image
        patch.append(
            {
                "op": "replace",
                "path": "/spec/template/spec/containers/0/image",
                "value": args.image,
            }
        )
        kubectl(
            args,
            "-n",
            "fs2-system",
            "patch",
            "deployment",
            name,
            "--type=json",
            "-p",
            json.dumps(patch),
        )
        after = get(args, "deployment", name)
        assert after["spec"]["template"] == expected, "unexpected mutation"
        receipts.append(
            {
                "deployment": name,
                "patch": patch,
                "template_preserved_except_image_and_additive_config": True,
            }
        )
        write(args.directory, "rollout", receipts)
        print(json.dumps(receipts[-1]), flush=True)


def apply_apps(args):
    receipt = []
    with admin(args) as client:
        for proposal in json.loads((args.directory / "proposals.json").read_text()):
            assert (
                client.get(
                    "/admin/api/v1/model-deployments/" + proposal["name"]
                ).status_code
                == 404
            ), "already exists; reconcile explicitly"
            response = client.post(
                "/admin/api/v1/model-deployments:plan-preview", json=proposal
            )
            response.raise_for_status()
            preview = response.json()["data"]
            assert preview["decision"]["disposition"] == "accepted", preview
            response = client.post(
                "/admin/api/v1/model-deployments:apply",
                json={
                    "preview_id": preview["preview_id"],
                    "proposed_etag": preview["proposed_etag"],
                    "proposal": proposal,
                    "idempotency_key": "toxicology-20261008-" + proposal["name"],
                },
            )
            receipt.append(
                {
                    "model": proposal["name"],
                    "preview": preview,
                    "status": response.status_code,
                    "response": response.json(),
                }
            )
            write(args.directory, "app-creation", receipt)
            response.raise_for_status()
            print(json.dumps({"created": proposal["name"]}), flush=True)


def website(args):
    assert args.image and "@sha256:" in args.image
    before = get(args, "deployment", "scientific-ai-website")
    pod = before["spec"]["template"]["spec"]
    index = next(
        i for i, row in enumerate(pod["containers"]) if row["name"] == "website"
    )
    assert pod["containers"][index]["image"].endswith(
        "@sha256:814c2a87baf58322e23dd7a061b4513971770dc0c4213856675ce74612902c58"
    ), "website changed since source reconciliation"
    write(args.directory, "website-before", before)
    expected = copy.deepcopy(before["spec"])
    expected["template"]["spec"]["containers"][index]["image"] = args.image
    patch = [
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": before["metadata"]["resourceVersion"],
        },
        {
            "op": "replace",
            "path": f"/spec/template/spec/containers/{index}/image",
            "value": args.image,
        },
    ]
    kubectl(
        args,
        "-n",
        "fs2-system",
        "patch",
        "deployment",
        "scientific-ai-website",
        "--type=json",
        "-p",
        json.dumps(patch),
    )
    after = get(args, "deployment", "scientific-ai-website")
    assert after["spec"] == expected, "unexpected website change"
    write(
        args.directory,
        "website-rollout",
        {
            "patch": patch,
            "edge_redirect_sidecar_and_all_other_settings_preserved": True,
        },
    )
    print(
        json.dumps(
            {
                "website_image_updated": args.image,
                "edge_and_other_settings_preserved": True,
            }
        )
    )


def grant(args):
    keyfile = json.loads(args.key_file.read_text())
    key_id = keyfile["key"]["id"]
    with admin(args) as client:
        response = client.get("/admin/api/v1/keys", params={"tenant_id": "system"})
        response.raise_for_status()
        listing = response.json().get("data", response.json())
        key = next(item for item in listing["items"] if item["id"] == key_id)
        assert (key["tenant_id"], key["principal_id"]) == ("system", "qa"), (
            "internal QA identity required"
        )
        record = args.directory / "before-qa-grants.json"
        if args.restore:
            before = json.loads(record.read_text())
            assert set(key["models"]) == set(before["models"]) | set(MODELS), (
                "concurrent key update; reconcile"
            )
            desired = before["models"]
            assert set(key["scopes"]) == set(before["scopes"]) | {"artifacts.write"}, (
                "concurrent scope update; reconcile"
            )
            scopes = before["scopes"]
        else:
            assert not record.exists(), "grant already recorded; reconcile"
            write(args.directory, "before-qa-grants", key)
            desired = sorted(set(key["models"]) | set(MODELS))
            scopes = sorted(set(key["scopes"]) | {"artifacts.write"})
        response = client.patch(
            "/admin/api/v1/keys/" + key_id, json={"models": desired, "scopes": scopes}
        )
        response.raise_for_status()
        print(
            json.dumps(
                {
                    "internal_key": key_id,
                    "models": desired,
                    "customer_keys_unchanged": True,
                    "concurrency_and_expiry_unchanged": True,
                }
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["prepare", "rollout", "apply", "grant", "website"]
    )
    parser.add_argument(
        "--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"
    )
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--image")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    args.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    {
        "prepare": prepare,
        "rollout": rollout,
        "apply": apply_apps,
        "grant": grant,
        "website": website,
    }[args.command](args)

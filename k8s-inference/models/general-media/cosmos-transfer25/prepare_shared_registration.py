"""Prepare the regular shared Transfer App from a fresh live snapshot; no apply.

Remove temporary Qwen route selections immediately. Their drained controller
qualifications remain until archive-backed retirement has completed. Pass
--qwen-retired only after confirming both CRs and owned workloads are gone.
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
REFERENCE_ONLY = {"qwen3-6-27b-fp8", "qwen2-5-14b-instruct"}
TEMPLATE = MODEL + ".shared-v1"


def prepare(snapshot, *, qwen_retired=False):
    envelope, bundles, routes, current = copy.deepcopy(
        [snapshot[key] for key in ("envelope", "bundles", "routes", "current")]
    )
    if current["spec"]["modelRef"] != MODEL:
        raise ValueError("snapshot is not the Cosmos Transfer App")
    selected = json.loads(routes["deployment-runtimes.json"])
    old_ids = set(selected["models"])
    entry = json.loads(
        (ROOT / "catalog/runtime/deployment-runtimes" / (MODEL + ".json")).read_text()
    )
    record = entry["record"]
    if current["spec"]["runtime"]["image"] != record["runtime"]["image"]["reference"]:
        raise ValueError("promotion must not replace the qualified NIM runtime")
    old_digest = current["spec"]["runtime"]["templateRef"]["digest"]
    matches = [
        b
        for b in bundles
        if b["modelRef"] == MODEL and b["templateDigest"] == old_digest
    ]
    if len(matches) != 1 or canonical_digest(matches[0]["resources"]) != old_digest:
        raise ValueError("exact current renderer bundle is unavailable")
    bundle = copy.deepcopy(matches[0])
    for resource in bundle["resources"]:
        metadata = resource["metadata"]
        metadata.get("labels", {}).pop("fs2-serve.nebius.ai/task", None)
        if resource["kind"] == "Deployment":
            metadata = resource["spec"]["template"]["metadata"]
            metadata.get("labels", {}).pop("fs2-serve.nebius.ai/task", None)
            metadata.setdefault("annotations", {})[
                "fs2-serve.nebius.ai/qualification-state"
            ] = "regular-shared-app-customer-qualification-in-progress"
    digest = canonical_digest(bundle["resources"])
    bundle["templateDigest"] = digest
    qualification = envelope["qualifications"][MODEL]
    prior = qualification["templateRefs"].get(TEMPLATE)
    if prior is not None and prior != digest:
        raise ValueError(
            "shared template identity changed; create an explicit new revision"
        )
    if digest not in qualification["templateDigests"]:
        qualification["templateDigests"].append(digest)
        bundles.append(bundle)
    qualification["templateRefs"][TEMPLATE] = digest
    qualification["templateCacheTiers"][digest] = "Disabled"
    for model in REFERENCE_ONLY:
        selected["models"].pop(model, None)
        if qwen_retired:
            envelope["qualifications"].pop(model, None)
    if qwen_retired:
        bundles = [b for b in bundles if b["modelRef"] not in REFERENCE_ONLY]
    selected["models"][MODEL] = entry
    assert set(selected["models"]) == old_ids - REFERENCE_ONLY
    routes["deployment-runtimes.json"] = canonical_json(selected).decode()
    spec = copy.deepcopy(current["spec"])
    spec["runtime"]["templateRef"] = {"name": TEMPLATE, "digest": digest}
    spec["policy"]["allowedPrincipalIds"] = []
    spec["lifecycle"]["desiredState"] = "Enabled"
    envelope.pop("revision")
    envelope["revision"] = canonical_digest(envelope)
    typed = InfrastructureEnvelope.model_validate(envelope)
    proposed = ModelDeploymentSpec.model_validate(spec)
    decision = validate_model_deployment(proposed, typed)
    if decision.disposition is not ValidationDisposition.ACCEPTED:
        raise ValueError(decision.model_dump(mode="json", by_alias=True))
    (pool,) = spec["placement"]["poolRefs"]
    LegacyManifestRenderer(
        {(MODEL, digest): LegacyTemplateBundle.model_validate(bundle)}
    ).render(
        proposed,
        RenderContext(
            name=MODEL,
            namespace="fs2-models",
            generation=1,
            pool=typed.pools[pool],
            eligible_pools=[typed.pools[pool]],
            prometheus_server_address="http://prometheus.fs2-observability.svc:9090",
            preview=True,
        ),
    )
    configs, names = [], {}
    for field, data in (
        (
            "envelope",
            {"infrastructure-envelope.json": canonical_json(envelope).decode()},
        ),
        ("bundles", {"renderer-bundles.json": canonical_json(bundles).decode()}),
        ("routes", routes),
    ):
        name = (
            "fs2-physical-ai-"
            + field
            + "-"
            + hashlib.sha256(canonical_json(data)).hexdigest()[:16]
        )
        names[field] = name
        configs.append(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "immutable": True,
                "metadata": {
                    "name": name,
                    "namespace": "fs2-system",
                    "labels": {"app.kubernetes.io/part-of": "fs2-serve"},
                },
                "data": data,
            }
        )
    return {
        "configmaps": {"apiVersion": "v1", "kind": "List", "items": configs},
        "proposal": {
            "name": MODEL,
            "namespace": "fs2-models",
            "base_etag": current["etag"],
            "spec": spec,
        },
        "values": {
            "modelController": {
                "infrastructureEnvelopeConfigMapName": names["envelope"],
                "rendererBundlesConfigMapName": names["bundles"],
            },
            "catalog": {"leanRoutes": {"configMapName": names["routes"]}},
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--qwen-retired", action="store_true")
    args = parser.parse_args()
    result = prepare(
        json.loads(args.snapshot.read_text()), qwen_retired=args.qwen_retired
    )
    args.output_directory.mkdir(mode=0o700, exist_ok=False)
    for field, data in result.items():
        path = args.output_directory / (field + ".json")
        path.write_text(json.dumps(data, indent=2) + "\n")
        path.chmod(0o600)
    print(
        json.dumps(
            {
                "prepared": True,
                "apply_performed": False,
                "qwen_retired": args.qwen_retired,
                "only_shared_transfer_and_reference_qwen_changed": True,
            }
        )
    )

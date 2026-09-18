"""Stage the qualified CXR template against fresh four-map captures; never apply."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from prepare_candidate import ANNOTATION, MODEL, MODULE, ROOT, TEMPLATE_NAME
from prepare_candidate import candidate_template, proposal

from fs2_serve.model_deployment import canonical_digest, canonical_json

HELPER = ROOT / "acceptance/scientific-runtime-repair-20260918/prepare_promotion.py"
loader = importlib.util.spec_from_file_location("identity_existing_promotion", HELPER)
assert loader and loader.loader
existing = importlib.util.module_from_spec(loader)
sys.modules[loader.name] = existing
loader.loader.exec_module(existing)
PROOF = "d6b235efda90ce5519ee40ecb11e16b010bb959c221d3699fe33ce3c9af0458d"
QUALIFIED_TEMPLATE = (
    "sha256:e84014a61b5cf3ac5eb3247ffb31b1001cd6284ffe092354f08448e4bb31a595"
)


def extend(envelope, bundles, routes, configuration, deployments):
    envelope, bundles, routes, configuration = copy.deepcopy(
        (envelope, bundles, routes, configuration)
    )
    models = set(envelope["qualifications"])
    if len(models) != 20 or not existing.VOICES <= models or MODEL not in models:
        raise ValueError("expected_complete_twenty_model_contract")
    targets = [
        d
        for d in deployments
        if d["metadata"]["name"] == MODEL and d["spec"]["modelRef"] == MODEL
    ]
    if len(targets) != 1:
        raise ValueError("expected_exact_canonical_cxr_modeldeployment")
    target = targets[0]
    old_digest = target["spec"]["runtime"]["templateRef"]["digest"]
    previous = [
        b
        for b in bundles
        if b["modelRef"] == MODEL and b["templateDigest"] == old_digest
    ]
    if len(previous) != 1:
        raise ValueError("expected_exact_original_cxr_template")
    candidate = candidate_template(previous[0])
    if candidate["templateDigest"] != QUALIFIED_TEMPLATE:
        raise ValueError("candidate_differs_from_actual_two_replica_proof")
    qualification = envelope["qualifications"][MODEL]
    if (
        QUALIFIED_TEMPLATE in qualification["templateDigests"]
        or TEMPLATE_NAME in qualification["templateRefs"]
    ):
        raise ValueError("candidate_already_registered_inspect_live_state")
    qualification["templateDigests"].append(QUALIFIED_TEMPLATE)
    qualification["templateRefs"][TEMPLATE_NAME] = QUALIFIED_TEMPLATE
    qualification["templateCacheTiers"][QUALIFIED_TEMPLATE] = qualification[
        "templateCacheTiers"
    ][old_digest]
    bundles.append(candidate)
    proposals = [
        {
            "name": MODEL,
            "namespace": target["metadata"]["namespace"],
            "spec": proposal(target["spec"], candidate),
        }
    ]
    projection = json.loads(routes["qualification-projection.json"])
    rows = [r for r in projection["rows"] if r["model_id"] == MODEL]
    if len(rows) != 1:
        raise ValueError("expected_exact_cxr_qualification_row")
    # Isolated attribution is not public/clinical/cold-start/elastic qualification.
    for key in rows[0]["states"]:
        rows[0]["states"][key] = key in {"registered", "runtime_ready"}
    for key in rows[0]["evidence"]:
        if key != "audited_catalog_sha256":
            rows[0]["evidence"][key] = None
    rows[0]["evidence"]["retained_deployments_sha256"] = PROOF
    routes["qualification-projection.json"] = canonical_json(projection).decode()
    envelope.pop("revision")
    envelope["revision"] = canonical_digest(envelope)
    # Image, weights, public interface and runtime-record identity are unchanged.
    # The mounted admin baseline must still pass real bootstrap validation.
    return envelope, bundles, routes, configuration, proposals


def validate_render(candidate, bundles, proposals):
    envelope = existing.InfrastructureEnvelope.model_validate(candidate)
    selected = proposals[0]
    spec = existing.ModelDeploymentSpec.model_validate(selected["spec"])
    decision = existing.validate_model_deployment(spec, envelope)
    renderer = existing.ControllerFiles(
        infrastructure_envelope=envelope,
        bundles=[existing.LegacyTemplateBundle.model_validate(b) for b in bundles],
    ).renderer()
    plan = renderer.render(
        spec,
        existing.RenderContext(
            name=selected["name"],
            namespace=selected["namespace"],
            generation=1,
            pool=envelope.pools[decision.admitted_pool_ref],
            eligible_pools=[envelope.pools[p] for p in spec.placement.pool_refs],
            prometheus_server_address="http://prometheus.fs2-observability.svc:9090",
            preview=True,
        ),
    )
    checks = []
    for resource in plan.resources:
        if resource.kind != "Deployment":
            continue
        pod = resource.manifest["spec"]["template"]
        container = next(
            c for c in pod["spec"]["containers"] if c["image"] == spec.runtime.image
        )
        uid_env = next(
            e for e in container["env"] if e["name"] == "FS2_RUNTIME_POD_UID"
        )
        if (
            container.get("command")
            or container["args"][-2:] != ["--middleware", MODULE]
            or uid_env.get("valueFrom", {}).get("fieldRef", {}).get("fieldPath")
            != "metadata.uid"
            or pod["metadata"]["annotations"].get(ANNOTATION) != "asgi-v1"
            or any(
                v["name"].startswith("snapshot-")
                for v in pod["spec"].get("volumes", [])
            )
        ):
            raise ValueError("owner_render_identity_or_snapshot_contract_changed")
        checks.append(
            {
                "deployment": resource.manifest["metadata"]["name"],
                "identity_present": True,
                "old_snapshot_restore_absent": True,
            }
        )
    if not checks:
        raise ValueError("owner_runtime_deployment_missing")
    return checks


def source_pin(path, commit):
    relative = "k8s-inference/" + str(path.resolve().relative_to(ROOT))
    raw = subprocess.check_output(
        ["git", "-C", str(ROOT), "show", f"{commit}:{relative}"]
    )  # noqa: S603
    if raw != path.read_bytes():
        raise ValueError("source_differs_from_commit:" + relative)
    return {
        "commit": commit,
        "path": relative,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in (
        "live-configmaps",
        "live-routes",
        "live-admin-configuration",
        "modeldeployments",
        "output",
    ):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    pins = [
        source_pin(Path(__file__), args.source_commit),
        source_pin(HELPER, "b97d1f69b"),
        source_pin(Path(__file__).with_name("prepare_candidate.py"), "dca516ec6"),
        source_pin(ROOT / "models/general-media/fs2_runtime_identity.py", "6e3a832ee"),
    ]
    maps = json.loads(args.live_configmaps.read_bytes())["items"]

    def document(key):
        documents = [
            json.loads(m["data"][key]) for m in maps if key in m.get("data", {})
        ]
        if len(documents) != 1:
            raise ValueError("capture_only_the_exact_mounted_map:" + key)
        return documents[0]

    original = document("infrastructure-envelope.json")
    original_bundles = document("renderer-bundles.json")
    deployments = json.loads(args.modeldeployments.read_bytes())["items"]
    routes = json.loads(args.live_routes.read_bytes())["data"]
    admin = json.loads(
        json.loads(args.live_admin_configuration.read_bytes())["data"][
            "admin-configuration.json"
        ]
    )
    candidate, bundles, routes, admin, proposals = extend(
        original, original_bundles, routes, admin, deployments
    )
    checks = existing.validate_candidate(
        original, candidate, bundles, deployments, proposals
    )
    checks["cxr_owner_render"] = validate_render(candidate, bundles, proposals)
    checks["gateway_bootstrap"] = asyncio.run(
        existing.validate_admin_configuration(
            admin, json.loads(routes["deployment-runtimes.json"])["models"]
        )
    )
    objects = [
        existing.configmap(
            "fs2-science-envelope-",
            {"infrastructure-envelope.json": canonical_json(candidate).decode()},
        ),
        existing.configmap(
            "fs2-science-bundles-",
            {"renderer-bundles.json": canonical_json(bundles).decode()},
        ),
        existing.configmap("fs2-science-routes-", routes),
        existing.configmap(
            "fs2-science-admin-",
            {"admin-configuration.json": canonical_json(admin).decode()},
        ),
    ]
    values = {
        "modelController": {
            "infrastructureEnvelopeConfigMapName": objects[0]["metadata"]["name"],
            "rendererBundlesConfigMapName": objects[1]["metadata"]["name"],
        },
        "catalog": {"leanRoutes": {"configMapName": objects[2]["metadata"]["name"]}},
        "adminConfiguration": {
            "configMapName": objects[3]["metadata"]["name"],
            "sha256": hashlib.sha256(canonical_json(admin)).hexdigest(),
        },
    }
    receipt = {
        "applied": False,
        "source_pins": pins,
        "proof_sha256": PROOF,
        "validation": checks,
        "values": values,
        "preserved_sibling_models": 19,
        "all_previous_templates_and_snapshots_preserved": True,
        "new_snapshot_qualification": False,
        "public_attribution_gate_pending": True,
        "input_sha256": {
            key: hashlib.sha256(getattr(args, key).read_bytes()).hexdigest()
            for key in (
                "live_configmaps",
                "live_routes",
                "live_admin_configuration",
                "modeldeployments",
            )
        },
    }
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    outputs = {
        "configmaps.json": {"apiVersion": "v1", "kind": "List", "items": objects},
        "values.json": values,
        "app-proposals.json": proposals,
        "validation.json": receipt,
        "rollback-app-proposals.json": [
            {
                "name": d["metadata"]["name"],
                "namespace": d["metadata"]["namespace"],
                "spec": d["spec"],
            }
            for d in deployments
            if d["metadata"]["name"] == MODEL
        ],
    }
    for name, value in outputs.items():
        (args.output / name).write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n"
        )
    print(
        json.dumps({"applied": False, "values": values, "template": QUALIFIED_TEMPLATE})
    )


if __name__ == "__main__":
    main()

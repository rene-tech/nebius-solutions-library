"""Stage release-157 -> MolMIM/Qwen contracts; never publish, drain, or apply.

Reuse the existing molecular promotion/bootstrap validators and immutable map
format. All unchanged Apps, previous bundles and snapshot evidence survive.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from parser_candidate import PARSERS, ROOT, TEMPLATE_NAME, parser_template, proposal

from fs2_serve.model_deployment import canonical_digest, canonical_json

HELPER_PATH = ROOT / "acceptance/scientific-runtime-repair-20260918/prepare_promotion.py"
loader = importlib.util.spec_from_file_location("combined_existing_promotion", HELPER_PATH)
assert loader and loader.loader
existing = importlib.util.module_from_spec(loader)
sys.modules[loader.name] = existing
loader.loader.exec_module(existing)

TARGETS = {"molmim", "qwen3-8b"}
OLD_MOLMIM = "fd59b1b45206fe5067033dd7385469ea54e6b670bdd530276e75857f9b459958"
NEW_MOLMIM = "9df0b25cd79c86746f564ca060328811cd39792ce9e6d66ff9f3e1de6547a4cb"
QWEN_RECEIPT = "e21cfdeb55ee35b873ce55c51fff193eaa6c57939dd28bf80fd8b89a92f050b3"


def target(deployments, name):
    rows = [d for d in deployments if d["metadata"]["name"] == name and d["spec"]["modelRef"] == name]
    if len(rows) != 1:
        raise ValueError("expected_exact_canonical_modeldeployment:" + name)
    return rows[0]


def extend(envelope, bundles, route_data, configuration, deployments, molmim):
    envelope, bundles, route_data, configuration = copy.deepcopy((envelope, bundles, route_data, configuration))
    models = set(envelope["qualifications"])
    if len(models) != 20 or not existing.VOICES <= models or not TARGETS <= models:
        raise ValueError("expected_complete_twenty_model_contract")
    runtimes = json.loads(route_data["deployment-runtimes.json"])
    projection = json.loads(route_data["qualification-projection.json"])
    before_runtime = copy.deepcopy(runtimes["models"])
    current = target(deployments, "molmim")
    spec = current["spec"]
    if not spec["runtime"]["image"].endswith("@sha256:" + OLD_MOLMIM):
        raise ValueError("unexpected_molmim_image")
    if spec["cache"]["snapshotPreference"] != "Never" or spec["fastStart"]["level"] != "Off":
        raise ValueError("molmim_requires_existing_conventional_contract")
    image = spec["runtime"]["image"].split("@")[0] + "@sha256:" + NEW_MOLMIM
    if molmim["record"]["runtime"]["image"]["reference"] != image:
        raise ValueError("unexpected_molmim_successor")
    for key in ("model", "resources", "cache", "interface", "startup"):
        if molmim["record"][key] != before_runtime["molmim"]["record"][key]:
            raise ValueError("molmim_nonruntime_contract_changed:" + key)
    flags = molmim["qualification"]["states"]
    if not all(flags[k] for k in ("registered", "runtime_ready", "semantic_qualified")) or any(
        flags[k] for k in ("route_active", "http_mcp_qualified", "cold_start_qualified", "elasticity_qualified")
    ):
        raise ValueError("unexpected_molmim_qualification_claim")
    if image in envelope["qualifications"]["molmim"]["runtimeImages"]:
        raise ValueError("molmim_candidate_already_present")
    envelope["qualifications"]["molmim"]["runtimeImages"].append(image)
    molmim_proposal = {
        "name": current["metadata"]["name"],
        "namespace": current["metadata"]["namespace"],
        "spec": copy.deepcopy(spec),
    }
    molmim_proposal["spec"]["runtime"]["image"] = image
    runtimes["models"]["molmim"] = copy.deepcopy(molmim)

    qwen = target(deployments, "qwen3-8b")
    old_digest = qwen["spec"]["runtime"]["templateRef"]["digest"]
    previous = [b for b in bundles if b["modelRef"] == "qwen3-8b" and b["templateDigest"] == old_digest]
    if len(previous) != 1:
        raise ValueError("expected_exact_qwen_template")
    template = parser_template(previous[0])
    qualification = envelope["qualifications"]["qwen3-8b"]
    if template["templateDigest"] in qualification["templateDigests"] or TEMPLATE_NAME in qualification["templateRefs"]:
        raise ValueError("qwen_candidate_already_present")
    qualification["templateDigests"].append(template["templateDigest"])
    qualification["templateRefs"][TEMPLATE_NAME] = template["templateDigest"]
    qualification["templateCacheTiers"][template["templateDigest"]] = qualification["templateCacheTiers"][old_digest]
    bundles.append(template)
    qwen_proposal = {
        "name": qwen["metadata"]["name"],
        "namespace": qwen["metadata"]["namespace"],
        "spec": proposal(qwen["spec"], template),
    }

    for model_id in TARGETS:
        rows = [r for r in projection["rows"] if r["model_id"] == model_id]
        if len(rows) != 1:
            raise ValueError("expected_exact_qualification_row:" + model_id)
        if model_id == "molmim":
            rows[0].clear()
            rows[0].update(copy.deepcopy(molmim["qualification"]))
        else:
            # Parser pathways passed, but added thinking/JSON-object cases failed.
            # Do not transfer a blanket semantic/public/cold/elastic verdict.
            for key in rows[0]["states"]:
                rows[0]["states"][key] = key in {"registered", "runtime_ready"}
            for key in rows[0]["evidence"]:
                if key != "audited_catalog_sha256":
                    rows[0]["evidence"][key] = None
            rows[0]["evidence"]["retained_deployments_sha256"] = QWEN_RECEIPT

    configuration = existing.rebase_admin_configuration(configuration, before_runtime, {"molmim": molmim})
    envelope.pop("revision")
    envelope["revision"] = canonical_digest(envelope)
    route_data["deployment-runtimes.json"] = canonical_json(runtimes).decode()
    route_data["qualification-projection.json"] = canonical_json(projection).decode()
    return envelope, bundles, route_data, configuration, [molmim_proposal, qwen_proposal]


def source_pin(path, commit):
    path = path.resolve()
    relative = "k8s-inference/" + str(path.relative_to(ROOT))
    git = shutil.which("git")
    if git is None:
        raise ValueError("git_unavailable")
    # Fixed repository read, explicit operator-selected revision; never a shell.
    raw = subprocess.check_output([git, "-C", str(ROOT), "show", f"{commit}:{relative}"])  # noqa: S603
    if raw != path.read_bytes():
        raise ValueError("source_differs_from_pin:" + relative)
    return {"commit": commit, "path": relative, "sha256": hashlib.sha256(raw).hexdigest()}


def validate_qwen_render(candidate, bundles, proposals):
    """Check the actual owner renderer, not only the stored plain template."""
    envelope = existing.InfrastructureEnvelope.model_validate(candidate)
    selected = next(p for p in proposals if p["name"] == "qwen3-8b")
    spec = existing.ModelDeploymentSpec.model_validate(selected["spec"])
    decision = existing.validate_model_deployment(spec, envelope)
    renderer = existing.ControllerFiles(
        infrastructure_envelope=envelope,
        bundles=[existing.LegacyTemplateBundle.model_validate(b) for b in bundles],
    ).renderer()
    plan = renderer.render(spec, existing.RenderContext(
        name=selected["name"], namespace=selected["namespace"], generation=1,
        pool=envelope.pools[decision.admitted_pool_ref],
        eligible_pools=[envelope.pools[p] for p in spec.placement.pool_refs],
        prometheus_server_address="http://prometheus.fs2-observability.svc:9090", preview=True,
    ))
    checks = []
    for resource in plan.resources:
        if resource.kind != "Deployment":
            continue
        pod = resource.manifest["spec"]["template"]["spec"]
        container = next(c for c in pod["containers"] if c["image"] == spec.runtime.image)
        if (container.get("command") or container["args"][-len(PARSERS):] != PARSERS
                or any(v["name"].startswith("snapshot-") for v in pod.get("volumes", []))):
            raise ValueError("qwen_generated_parser_or_snapshot_contract_changed")
        checks.append({"deployment": resource.manifest["metadata"]["name"],
                       "parsers_present": True, "snapshot_wrapper_absent": True})
    if not checks:
        raise ValueError("qwen_generated_deployment_missing")
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("live-configmaps", "live-routes", "live-admin-configuration", "modeldeployments", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    pins = [
        source_pin(HELPER_PATH, "b97d1f69b"),
        source_pin(Path(__file__).with_name("parser_candidate.py"), "5c0c93d1d"),
    ]
    record = ROOT / "catalog/runtime/deployment-runtimes/molmim-portable-h100-20260918.json"
    pins += [source_pin(record, "588d0b4f9"), source_pin(Path(__file__), args.source_commit)]
    maps = json.loads(args.live_configmaps.read_bytes())["items"]

    def document(key):
        return json.loads(next(m["data"][key] for m in maps if key in m.get("data", {})))

    original = document("infrastructure-envelope.json")
    original_bundles = document("renderer-bundles.json")
    deployments = json.loads(args.modeldeployments.read_bytes())["items"]
    routes = json.loads(args.live_routes.read_bytes())["data"]
    admin = json.loads(json.loads(args.live_admin_configuration.read_bytes())["data"]["admin-configuration.json"])
    candidate, bundles, new_routes, new_admin, proposals = extend(
        original, original_bundles, routes, admin, deployments, json.loads(record.read_bytes())
    )
    checks = existing.validate_candidate(original, candidate, bundles, deployments, proposals)
    for check, deployment in zip(checks["current_modeldeployments"], deployments, strict=True):
        check["name"] = deployment["metadata"]["name"]
    checks["qwen_generated_contract"] = validate_qwen_render(candidate, bundles, proposals)
    checks["gateway_bootstrap"] = asyncio.run(
        existing.validate_admin_configuration(new_admin, json.loads(new_routes["deployment-runtimes.json"])["models"])
    )
    if bundles[:-1] != original_bundles:
        raise ValueError("prior_bundle_changed")
    if any(
        candidate["qualifications"][m] != original["qualifications"][m]
        for m in set(original["qualifications"]) - TARGETS
    ):
        raise ValueError("sibling_qualification_changed")
    objects = [
        existing.configmap(
            "fs2-science-envelope-", {"infrastructure-envelope.json": canonical_json(candidate).decode()}
        ),
        existing.configmap("fs2-science-bundles-", {"renderer-bundles.json": canonical_json(bundles).decode()}),
        existing.configmap("fs2-science-routes-", new_routes),
        existing.configmap("fs2-science-admin-", {"admin-configuration.json": canonical_json(new_admin).decode()}),
    ]
    values = {
        "modelController": {
            "infrastructureEnvelopeConfigMapName": objects[0]["metadata"]["name"],
            "rendererBundlesConfigMapName": objects[1]["metadata"]["name"],
        },
        "catalog": {"leanRoutes": {"configMapName": objects[2]["metadata"]["name"]}},
        "adminConfiguration": {
            "configMapName": objects[3]["metadata"]["name"],
            "sha256": hashlib.sha256(canonical_json(new_admin)).hexdigest(),
        },
    }
    rollback_values = {
        "modelController": {
            "infrastructureEnvelopeConfigMapName": next(
                m["metadata"]["name"] for m in maps if "infrastructure-envelope.json" in m.get("data", {})
            ),
            "rendererBundlesConfigMapName": next(
                m["metadata"]["name"] for m in maps if "renderer-bundles.json" in m.get("data", {})
            ),
        },
        "catalog": {"leanRoutes": {"configMapName": json.loads(args.live_routes.read_bytes())["metadata"]["name"]}},
        "adminConfiguration": {
            "configMapName": json.loads(args.live_admin_configuration.read_bytes())["metadata"]["name"],
            "sha256": hashlib.sha256(canonical_json(admin)).hexdigest(),
        },
    }
    files = {
        "live_configmaps": args.live_configmaps,
        "live_routes": args.live_routes,
        "live_admin_configuration": args.live_admin_configuration,
        "modeldeployments": args.modeldeployments,
    }
    receipt = {
        "applied": False,
        "source_commit": args.source_commit,
        "source_pins": pins,
        "values": values,
        "validation": checks,
        "models": sorted(TARGETS),
        "preserved_sibling_models": 18,
        "old_bundles_unchanged": True,
        "voice_count": 5,
        "qwen_semantic_qualification": False,
        "new_public_or_snapshot_qualification": False,
        "input_sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()},
        "configmaps": [
            {"name": m["metadata"]["name"], "data_sha256": hashlib.sha256(canonical_json(m["data"])).hexdigest()}
            for m in objects
        ],
    }
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    outputs = {
        "configmaps.json": {"apiVersion": "v1", "kind": "List", "items": objects},
        "values.json": values,
        "rollback-values.json": rollback_values,
        "app-proposals.json": proposals,
        "validation.json": receipt,
        "rollback-app-proposals.json": [
            {"name": d["metadata"]["name"], "namespace": d["metadata"]["namespace"], "spec": d["spec"]}
            for d in deployments
            if d["metadata"]["name"] in TARGETS
        ],
    }
    for name, value in outputs.items():
        (args.output / name).write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"applied": False, "values": values, "checks": len(checks["current_modeldeployments"])}))


if __name__ == "__main__":
    main()

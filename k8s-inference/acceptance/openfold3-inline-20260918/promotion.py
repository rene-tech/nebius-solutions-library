"""Prepare the exact scientific OpenFold3 successor; never mutate Kubernetes."""
from __future__ import annotations

import argparse
import asyncio
import copy
from dataclasses import asdict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import yaml

from fs2_serve.scientific_batch.adapters.common import runtime_recipe_sha256
from fs2_serve.scientific_batch.canary import run_internal_cpu_canary
from fs2_serve.scientific_batch.catalog_adapter import scientific_plan_from_catalog_profile
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog
from fs2_serve.scientific_batch.scheduling import SchedulingContractResolver
from fs2_serve.scientific_batch.placement import execution_resource_envelope

ROOT = Path(__file__).resolve().parents[2]
MODEL = "openfold3-openbind"
OLD = "sha256:6b15da4b2258c0c385adc1dbc7799493f3768cb4881f7990cb957f2c3b6759e4"
NEW = "sha256:6b883e916c8698195808e1dee0ff603c7db725c2bdb1d7c7b0b1eb1cc1c20743"
IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/openfold3-upstream@" + NEW
EVIDENCE_SHA = "99e1e2672cb010b285d6031e42dd1e7fa903c10e532b942dc9c1444b1ccc62e6"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def rows(document, key, identity):
    result = {row[identity]: row for row in document[key]}
    if len(result) != len(document[key]):
        raise ValueError("duplicate model identity")
    return result


def build(profiles, execution, recipe_sha, measured_at):
    """Rebase only qualification references after proving sibling rows equal."""
    original_profiles, original_execution = copy.deepcopy((profiles, execution))
    profiles, execution = copy.deepcopy((profiles, execution))
    before_profiles = rows(original_profiles, "profiles", "model_id")
    by_profile = rows(profiles, "profiles", "model_id")
    before = rows(original_execution, "models", "model_id")
    after = rows(execution, "models", "model_id")
    if len(before) != 11 or set(before) != set(by_profile) or MODEL not in before:
        raise ValueError("expected exact eleven scientific Apps")
    profile = by_profile[MODEL]
    if profile["execution_identity"]["runtime_image_digest"] != OLD:
        raise ValueError("OpenFold3 baseline image changed")
    if len(after[MODEL]["stages"]) != 2 or any(
        not stage["image"].endswith("@" + OLD) for stage in after[MODEL]["stages"]
    ):
        raise ValueError("unexpected OpenFold3 stage topology/image")
    for old_hash, ids in original_execution.get("qualification_baselines", {}).items():
        if digest({"schema": execution["schema"], "models": [before[mid] for mid in ids]}) != old_hash:
            raise ValueError("captured historical qualification baseline is invalid")
    original_normal_sha = digest({"schema": execution["schema"], "models": execution["models"]})
    for mid, previous in before_profiles.items():
        qualified_map = previous["qualification"]["execution_map_sha256"]
        if qualified_map != original_normal_sha and mid not in original_execution.get("qualification_baselines", {}).get(qualified_map, []):
            raise ValueError("original qualification reference is not bound:" + mid)

    identity = profile["execution_identity"]
    identity.update(runtime_image_digest=NEW, runtime_recipe_sha256=recipe_sha)
    identity["execution_identity_sha256"] = digest({k: v for k, v in identity.items() if k != "execution_identity_sha256"})
    after[MODEL]["execution_identity_sha256"] = identity["execution_identity_sha256"]
    for stage in after[MODEL]["stages"]:
        stage["image"] = IMAGE

    sibling_ids = [row["model_id"] for row in execution["models"] if row["model_id"] != MODEL]
    # An old whole-map hash cannot truthfully describe the changed target row.
    # Retain exact sibling row bytes and derive a new *reference* to that subset;
    # no sibling runtime or acceptance receipt is re-qualified or replaced.
    if any(canonical(before[mid]) != canonical(after[mid]) for mid in sibling_ids):
        raise ValueError("sibling execution changed")
    sibling_hash = digest({"schema": execution["schema"], "models": [after[mid] for mid in sibling_ids]})
    execution["qualification_baselines"] = {
        key: ids for key, ids in original_execution.get("qualification_baselines", {}).items() if MODEL not in ids
    }
    execution["qualification_baselines"][sibling_hash] = sibling_ids
    changes = []
    for mid in sibling_ids:
        qualification = by_profile[mid]["qualification"]
        changes.append({"model_id": mid, "previous_execution_map_sha256": qualification["execution_map_sha256"],
                        "successor_execution_map_sha256": sibling_hash,
                        "unchanged_execution_row_sha256": digest(after[mid])})
        qualification["execution_map_sha256"] = sibling_hash
        expected = copy.deepcopy(before_profiles[mid])
        expected["qualification"]["execution_map_sha256"] = sibling_hash
        if expected != by_profile[mid]:
            raise ValueError("sibling profile changed beyond qualification reference")

    normal_map_sha = digest({"schema": execution["schema"], "models": execution["models"]})
    profile["state"] = "active"
    profile["semantic_validation"]["state"] = "active"
    profile["qualification"].update(
        h100_semantic_receipt_sha256=EVIDENCE_SHA, public_completion_receipt_sha256=None,
        scheduler_eligibility_receipt_sha256=None, execution_map_sha256=normal_map_sha,
        qualified_at=measured_at,
    )
    profile["policy"]["limitations"] += [
        "The inline DataLoader successor retains the OpenBind-0 checkpoint, full input sequences and original "
        "resource limits; three isolated H100 heteromers complete with the original 64 MiB shared-memory mount.",
        "Runtime repair is not structural accuracy qualification: 1ACB/1BRS/2PTC C-alpha complex RMSD was "
        "18.717/14.437/18.882 A with native C-alpha contact recall 0/0.0556/0 under the unchanged MSA-free, "
        "template-free policy. No biological or clinical accuracy guarantee is made.",
        "The new image remains active/unqualified pending exact public completion and scheduler receipts; "
        "old-image public or GPU snapshot qualification is not inherited.",
    ]
    report = {"model_id": MODEL, "previous_image": OLD, "candidate_image": NEW,
              "sibling_count": len(sibling_ids), "sibling_reference_rebase": changes,
              "retained_historical_baselines": original_execution.get("qualification_baselines", {}),
              "original_execution_sha256": digest(original_execution),
              "candidate_execution_sha256": digest(execution),
              "candidate_normal_execution_sha256": normal_map_sha,
              "sibling_projection_sha256": sibling_hash,
              "snapshot_registry_unchanged": execution.get("snapshot_bundles") == original_execution.get("snapshot_bundles"),
              "new_public_or_snapshot_qualification": False}
    return profiles, execution, report


def existing_helpers():
    path = ROOT / "acceptance/ct-evo2-runtime-repair-20260918/prepare.py"
    spec = importlib.util.spec_from_file_location("openfold_existing_startup", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def merge_values(values, execution):
    """Replace the complete subtree; Helm -f deep merge retains stale baselines."""
    result = copy.deepcopy(values)
    result["scientificBatch"]["executionMap"] = copy.deepcopy(execution)
    return result


def validate_helm(values, execution):
    with tempfile.TemporaryDirectory(prefix="fs2-openfold-helm-") as temporary:
        path = Path(temporary) / "values.json"
        path.write_bytes(canonical(values))
        rendered = subprocess.check_output([
            "helm", "template", "fs2-serve-control-plane",
            str(ROOT / "charts/control-plane/fs2-serve-control-plane"), "-n", "fs2-system",
            "-f", str(path), "--show-only", "templates/scientific-execution-map.yaml",
        ])
    cm = yaml.safe_load(rendered)
    raw = cm["data"]["execution-map.json"]
    sha = hashlib.sha256(raw.encode()).hexdigest()
    if json.loads(raw) != execution or cm["metadata"]["annotations"]["fs2-serve.nebius.ai/execution-map-sha256"] != sha:
        raise ValueError("Helm rendered a different scientific execution map")
    return cm, {"configmap": cm["metadata"]["name"], "raw_data_sha256": sha,
                "immutable": cm["immutable"], "model_count": len(execution["models"]),
                "snapshot_bundles_preserved": len(execution.get("snapshot_bundles", {})),
                "helm_render_matches_candidate": True}


def validate_startup(profiles, execution, values, maps, scheduling_path, deployment):
    """Use actual Registry, scientific renderer, scheduler and bootstrap gates."""
    by_name = {item["metadata"]["name"]: item for item in maps["items"]}
    existing = existing_helpers()
    routes = by_name[values["catalog"]["leanRoutes"]["configMapName"]]["data"]
    bindings_name = next(v["configMap"]["name"] for v in deployment["spec"]["template"]["spec"]["volumes"] if v["name"] == "bindings")
    registry = existing.validate_registry(routes, by_name[bindings_name])
    admin = json.loads(by_name[values["adminConfiguration"]["configMapName"]]["data"]["admin-configuration.json"])
    bootstrap = asyncio.run(existing.existing.validate_admin_configuration(
        admin, json.loads(routes["deployment-runtimes.json"])["models"]))
    env = {e["name"]: e["value"] for c in deployment["spec"]["template"]["spec"]["containers"]
           for e in c.get("env", []) if "value" in e}
    with tempfile.TemporaryDirectory(prefix="fs2-openfold-startup-") as temporary:
        directory = Path(temporary)
        catalog = directory / "catalog"
        shutil.copytree(ROOT / "catalog/runtime", catalog)
        (catalog / "contracts/scientific-workload-profiles.json").write_bytes(canonical(profiles))
        execution_path = directory / "execution-map.json"
        execution_path.write_bytes(canonical(execution))
        parsed = ScientificProfileCatalog.load(catalog)
        canary = run_internal_cpu_canary(parsed)
        renderer = FileScientificManifestRenderer(
            path=execution_path, profiles=parsed,
            tools_image=env["FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE"],
            internal_api_url=env["FS2_SCIENTIFIC_BATCH_INTERNAL_API_URL"],
            internal_fallback_api_url=env.get("FS2_SCIENTIFIC_BATCH_INTERNAL_FALLBACK_API_URL"),
            academic_tenant_id=env.get("FS2_SCIENTIFIC_BATCH_ACADEMIC_TENANT_ID"),
            academic_authorization_receipt_sha256=env.get("FS2_SCIENTIFIC_BATCH_ACADEMIC_AUTHORIZATION_RECEIPT_SHA256"),
        )
        scheduling = SchedulingContractResolver.load(
            scheduling_path, expected_sha256=values["scientificBatch"]["schedulingContractSha256"],
            stage_resources={key: execution_resource_envelope(value) for key, value in renderer.executions.items()},
        )
        checked = []
        for profile in parsed.list():
            mid = profile.model_id
            if not renderer.qualification_matches(mid, "sha256:" + profile.value["qualification"]["execution_map_sha256"]):
                raise ValueError("qualification reference unavailable:" + mid)
            plan = scientific_plan_from_catalog_profile(profile.value)
            for service_class in profile.service_classes:
                scheduling.freeze(service_class=service_class, model_id=mid,
                    tenant_id=env.get("FS2_SCIENTIFIC_BATCH_ACADEMIC_TENANT_ID", "offline-preflight"),
                    profile=profile.value, plan=plan, workload_namespace=renderer.workload_namespace(mid))
            checked.append(mid)
        if set(checked) != {r["model_id"] for r in execution["models"]}:
            raise ValueError("an existing scientific App became non-runnable")
        return {"registry": registry, "admin_bootstrap": bootstrap, "cpu_canary": asdict(canary),
                "scientific_renderer_and_scheduler_models": sorted(checked),
                "scope": "Offline real startup/configuration validators; no live availability or public completion claim."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-values", type=Path, required=True)
    parser.add_argument("--baseline-profiles", type=Path, required=True)
    parser.add_argument("--configmaps", type=Path, required=True)
    parser.add_argument("--gateway-deployment", type=Path, required=True)
    parser.add_argument("--scheduling", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.evidence.read_bytes()
    if hashlib.sha256(raw).hexdigest() != EVIDENCE_SHA:
        raise ValueError("exact isolated qualification receipt required")
    evidence = json.loads(raw)
    if evidence["image"] != IMAGE or len(evidence["cases"]) != 3 or not evidence["cleanup"]["all_absent"]:
        raise ValueError("candidate qualification incomplete")
    values = yaml.safe_load(args.baseline_values.read_text())
    original = values["scientificBatch"]["executionMap"]
    if isinstance(original, str):
        original = json.loads(original)
    original_profiles = json.loads(args.baseline_profiles.read_bytes())
    profiles, execution, report = build(original_profiles, original,
        runtime_recipe_sha256(ROOT, MODEL), evidence["recorded_at"])
    validation = validate_startup(profiles, execution, values, json.loads(args.configmaps.read_bytes()),
                                  args.scheduling, json.loads(args.gateway_deployment.read_bytes()))
    complete_values = merge_values(values, execution)
    rendered_map, helm_validation = validate_helm(complete_values, execution)
    report.update(applied=False, startup_validation=validation,
                  helm_validation=helm_validation,
                  evidence_sha256=EVIDENCE_SHA,
                  input_sha256={str(p.name): hashlib.sha256(p.read_bytes()).hexdigest() for p in (
                      args.baseline_values, args.baseline_profiles, args.configmaps, args.gateway_deployment, args.scheduling)})
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    projection = {"schema": "fs2-serve.nebius.ai/scientific-workload-profile-projection/v1",
                  "merge_target": "catalog/runtime/contracts/scientific-workload-profiles.json",
                  "profile": rows(profiles, "profiles", "model_id")[MODEL]}
    documents = {"scientific-workload-profiles.json": profiles,
                 "scientific-execution-map.json": execution, "workload-profile.json": projection,
                 "values.json": complete_values, "validation.json": report,
                 "rendered-execution-map.json": rendered_map,
                 "rollback-values.json": values,
                 "rollback-profiles.json": original_profiles}
    for name, value in documents.items():
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps({"applied": False, "sibling_rows_preserved": report["sibling_count"],
                      "candidate_execution_sha256": report["candidate_execution_sha256"],
                      "startup_validation": "passed"}))


if __name__ == "__main__":
    main()

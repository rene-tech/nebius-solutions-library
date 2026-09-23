"""Prepare an additive onboarding release from captured live values; never apply.

Existing model rows, snapshot bundles and qualification baselines remain exact.
An active onboarding profile is explicitly not a customer-ready qualification.
"""

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(HERE.parent / "runtime"))
from fs2_gromacs.contracts import canonical


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def source_recipe(root):
    sys.path.insert(0, str(root / "components/control-plane/src"))
    from fs2_serve.scientific_batch.adapters.common import _RECIPE_SHARED_PATHS
    paths = set(_RECIPE_SHARED_PATHS) | {
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/gromacs.py",
        "components/control-plane/src/fs2_serve/scientific_batch/gromacs_checkpoints.py",
        "components/control-plane/src/fs2_serve/scientific_batch/gromacs_storage.py",
        "components/control-plane/src/fs2_serve/scientific_batch/gromacs_storage_routes.py",
        "components/control-plane/src/fs2_serve/scientific_batch/workload_routes.py",
        "catalog/runtime/schema/gromacs-workflow-request.schema.json",
        "models/molecular-dynamics/gromacs/runtime/Containerfile",
        "models/molecular-dynamics/gromacs/runtime/requirements.lock",
    }
    paths.update(str(path.relative_to(root)) for path in (HERE.parent / "runtime/fs2_gromacs").glob("*.py"))
    return {"schema": "fs2-serve.nebius.ai/gromacs-runtime-recipe/v1", "files": [
        {"path": path, "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest(),
         "size_bytes": (root / path).stat().st_size} for path in sorted(paths)]}


def runtime_receipt(image, directories):
    tests = []
    for directory in directories:
        record = json.loads((directory / "qualification.json").read_text())
        result = json.loads((directory / "workspace/result.json").read_text())
        if (record["image"] != image or record["worker_exit_code"] != 0 or result["status"] != "succeeded"
                or result["completed_steps"][-1] not in {"energies", "bar-analysis"}):
            raise ValueError("runtime evidence does not prove completion on this exact candidate")
        tests.append({"case": directory.name, "qualification": record,
                      "result_sha256": hashlib.sha256((directory / "workspace/result.json").read_bytes()).hexdigest(),
                      "completed_steps": result["completed_steps"], "file_count": len(result["files"])})
    if not {"h100-ondemand-1x", "l40s-1x"}.issubset({test["qualification"]["pool"] for test in tests}):
        raise ValueError("this initial candidate needs both targeted GPU runtime records")
    return {"schema": "fs2-serve.nebius.ai/gromacs-runtime-qualification/v1", "runtime_image": image,
            "recorded_at": datetime.now(timezone.utc).isoformat(), "tests": tests, "customer_ready": False}


def prepare(values, scheduling_raw, candidate, runtime_image, evidence, recipe_sha256):
    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", runtime_image):
        raise ValueError("runtime image must be immutable")
    if evidence.get("runtime_image") != runtime_image or not evidence.get("tests") or not evidence.get("recorded_at"):
        raise ValueError("runtime evidence must bind this exact image")
    current = values["scientificBatch"]
    if hashlib.sha256(scheduling_raw).hexdigest() != current["schedulingContractSha256"]:
        raise ValueError("scheduler bytes differ from the captured release")
    live = current["executionMap"]
    live = json.loads(live) if isinstance(live, str) else live
    if set(live) - {"schema", "models", "snapshot_bundles", "qualification_baselines"}:
        raise ValueError("unrecognized execution map fields")
    if any(row["model_id"] == "gromacs" for row in live["models"]):
        raise ValueError("GROMACS already exists; prepare an explicit successor")
    profile = copy.deepcopy(candidate)
    if (profile["model_id"], profile["state"], profile["route_exposed"]) != ("gromacs", "candidate-unqualified", False):
        raise ValueError("expected the unrouted GROMACS candidate")
    profile.update(state="active", route_exposed=True)
    profile["source"]["classification"] = "qualified-input"
    profile["interface"]["mcp"]["invocable"] = True
    profile["semantic_validation"]["state"] = "active"
    profile["policy"]["limitations"][0] = (
        "Onboarding release: native single-GPU runtime tested; customer REST/MCP, bucket recovery, "
        "queue/preemption and LibreChat qualification are not yet complete. Not customer-ready."
    )
    identity = profile["execution_identity"]
    identity.update(runtime_image_digest=runtime_image.rsplit("@", 1)[1], runtime_recipe_sha256=recipe_sha256,
                    workload_recipe_sha256=digest(profile["workload"]), artifact_manifest_digest=digest([]))
    identity["execution_identity_sha256"] = digest({k: v for k, v in identity.items() if k != "execution_identity_sha256"})
    baseline = next(row for row in live["models"] if row["model_id"] == "rfdiffusion")
    row = {"model_id": "gromacs", "variant_id": "nvidia-2026-2-single-gpu-v1",
           "execution_identity_sha256": identity["execution_identity_sha256"], "access_profile": "public",
           "workload_namespace": baseline["workload_namespace"], "plan_adapter": copy.deepcopy(baseline["plan_adapter"]),
           "runtime_artifacts": [], "stages": [{
               "stage_id": "workflow", "image": runtime_image, "collector_id": "gromacs-workflow-v1",
               "validator_id": "gromacs-workflow-v1", "environment": {},
               "required_node_labels": {"kubernetes.io/arch": "amd64"},
               "resources": {"requests": {"cpu": "8000m", "memory": "16Gi", "ephemeral_storage": "64Gi"},
                             "limits": {"cpu": "8000m", "memory": "16Gi", "ephemeral_storage": "64Gi"}},
               "active_deadline_seconds": 261000, "termination_grace_seconds": 120,
               "service_account_name": "default", "workspace_uid": 10001, "workspace_gid": 10001,
               "mounts": [{"name": "artifact-workspace", "kind": "artifact-workspace", "claim_name": None,
                           "host_path": None, "mount_path": "/mnt/fs2-scientific", "sub_path": None, "read_only": False}],
           }]}
    desired = copy.deepcopy(live)
    desired.setdefault("qualification_baselines", {})[digest({"schema": live["schema"], "models": live["models"]})] = [
        item["model_id"] for item in live["models"]]
    desired["models"].append(row)
    by_id = {item["model_id"]: item for item in desired["models"]}
    for expected, ids in desired["qualification_baselines"].items():
        if digest({"schema": live["schema"], "models": [by_id[key] for key in ids]}) != expected:
            raise ValueError("existing qualification baseline changed")
    profile["qualification"] = {"h100_semantic_receipt_sha256": digest(evidence),
                                "public_completion_receipt_sha256": None, "scheduler_eligibility_receipt_sha256": None,
                                "execution_map_sha256": digest({"schema": desired["schema"], "models": desired["models"]}),
                                "qualified_at": evidence["recorded_at"]}
    scheduling = json.loads(scheduling_raw)
    pools = profile["resources"]["compatible_pool_ids"]
    if not set(pools).issubset(scheduling["pools"]) or "gromacs" in scheduling["model_eligible_pool_ids"]:
        raise ValueError("new GROMACS pool mapping does not match this deployment")
    scheduling["model_eligible_pool_ids"]["gromacs"] = pools
    raw = canonical(scheduling)
    sha = hashlib.sha256(raw).hexdigest()
    name = current["schedulingContractConfigMapName"].rsplit("-", 1)[0] + "-" + sha[:12]
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name,
          "namespace": current["schedulingContractNamespace"]}, "data": {current["schedulingContractKey"]: raw.decode()}}
    overlay = {"scientificBatch": {"executionMap": desired, "schedulingContractConfigMapName": name,
                                   "schedulingContractSha256": sha}}
    if "scientificArtifacts" in values:
        overlay["scientificArtifacts"] = {"mediaTypes": sorted(set(values["scientificArtifacts"]["mediaTypes"]) |
            {"application/x-tar", "application/vnd.fs2.gromacs-checkpoint+json"})}
    return profile, row, overlay, cm


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--gpu-result", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish-catalog", action="store_true",
                        help="Write the generated onboarding profile/map to this source checkout; never deploy")
    args = parser.parse_args()
    evidence = runtime_receipt(args.runtime_image, args.gpu_result)
    recipe = source_recipe(ROOT)
    profile, row, overlay, cm = prepare(json.loads((args.baseline / "values.json").read_text()),
        (args.baseline / "scheduling.json").read_bytes(), json.loads((HERE / "workload-profile.json").read_text())["profile"],
        args.runtime_image, evidence, digest(recipe))
    args.output.mkdir(parents=True, exist_ok=False)
    for name, value in [("profile.json", profile), ("execution-row.json", row), ("activation.values.json", overlay),
                         ("scheduling.configmap.json", cm), ("runtime-receipt.json", evidence), ("source-recipe.json", recipe)]:
        (args.output / name).write_bytes(canonical(value) + b"\n")
    if args.publish_catalog:
        contracts = ROOT / "catalog/runtime/contracts"
        catalog_path = contracts / "scientific-workload-profiles.json"
        catalog = json.loads(catalog_path.read_text())
        if any(item["model_id"] == "gromacs" for item in catalog["profiles"]):
            raise ValueError("source already contains GROMACS; review an explicit successor")
        source_map = json.loads((contracts / "scientific-execution-map.json").read_text())
        captured_map = json.loads((args.baseline / "values.json").read_text())["scientificBatch"]["executionMap"]
        captured_map = json.loads(captured_map) if isinstance(captured_map, str) else captured_map
        comparable = lambda value: {k: v for k, v in value.items() if k != "snapshot_bundles"}
        if comparable(source_map) != comparable(captured_map):
            raise ValueError("source execution map differs from the captured live release")
        catalog["profiles"].append(profile)
        catalog_path.write_text(json.dumps(catalog, indent=2) + "\n")
        # Environment-local snapshot PVC/configmap identities stay in the live
        # values overlay. Do not bake this cluster's bundle settings into IaC.
        published_map = copy.deepcopy(source_map)
        published_map["models"].append(row)
        published_map["qualification_baselines"] = overlay["scientificBatch"]["executionMap"]["qualification_baselines"]
        (contracts / "scientific-execution-map.json").write_text(
            json.dumps(published_map, indent=2) + "\n")
    print(json.dumps({"state": "active-onboarding", "customer_ready": False,
                      "retained_models": len(overlay["scientificBatch"]["executionMap"]["models"]) - 1}))


if __name__ == "__main__":
    main()

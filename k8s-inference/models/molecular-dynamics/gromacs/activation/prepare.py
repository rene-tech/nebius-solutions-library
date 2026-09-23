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
        "components/control-plane/src/fs2_serve/scientific_batch/artifact_bridge.py",
        "components/control-plane/src/fs2_serve/scientific_artifacts.py",
        "components/control-plane/src/fs2_serve/scientific_batch/workload_routes.py",
        "catalog/runtime/schema/gromacs-workflow-request.schema.json",
        "models/molecular-dynamics/gromacs/runtime/Containerfile",
        "models/molecular-dynamics/gromacs/runtime/Containerfile.mpi",
        "models/molecular-dynamics/gromacs/runtime/Containerfile.plumed",
        "models/molecular-dynamics/gromacs/runtime/requirements-mpi.lock",
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/gromacs_mpi.py",
        "catalog/runtime/schema/gromacs-mpi-workflow-request.schema.json",
        "models/molecular-dynamics/gromacs/runtime/requirements.lock",
    }
    paths.update(
        str(path.relative_to(root))
        for path in (HERE.parent / "runtime/fs2_gromacs").glob("*.py")
    )
    return {
        "schema": "fs2-serve.nebius.ai/gromacs-runtime-recipe/v1",
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest(),
                "size_bytes": (root / path).stat().st_size,
            }
            for path in sorted(paths)
        ],
    }


def runtime_receipt(image, directories, *, model_id="gromacs"):
    tests = []
    for directory in directories:
        record = json.loads((directory / "qualification.json").read_text())
        result = json.loads((directory / "workspace/result.json").read_text())
        if (
            record["image"] != image
            or record["worker_exit_code"] != 0
            or result["status"] != "succeeded"
            or result["completed_steps"][-1]
            not in {"energies", "bar-analysis", "gyration", "check"}
        ):
            raise ValueError(
                "runtime evidence does not prove completion on this exact candidate"
            )
        tests.append(
            {
                "case": directory.name,
                "qualification": record,
                "result_sha256": hashlib.sha256(
                    (directory / "workspace/result.json").read_bytes()
                ).hexdigest(),
                "completed_steps": result["completed_steps"],
                "file_count": len(result["files"]),
            }
        )
    if model_id == "gromacs-mpi":
        for test in tests:
            ranks = test["qualification"].get("ranks", [])
            if (
                len(ranks) != 2
                or len({rank["node"] for rank in ranks}) != 2
                or any(rank["exit_code"] != 0 for rank in ranks)
            ):
                raise ValueError(
                    "MPI evidence requires successful ranks on two distinct nodes"
                )
    else:
        pools = {test["qualification"]["pool"] for test in tests}
        if "l40s-1x" not in pools or not pools.intersection(
            {"h100-ondemand-1x", "h100-reserved-8x"}
        ):
            raise ValueError(
                "The enhanced candidate needs both H100 and L40S runtime records"
            )
    return {
        "schema": "fs2-serve.nebius.ai/gromacs-runtime-qualification/v1",
        "runtime_image": image,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "tests": tests,
        "customer_ready": False,
    }


def prepare(
    values,
    scheduling_raw,
    candidate,
    runtime_image,
    evidence,
    recipe_sha256,
    *,
    replace_existing=False,
):
    model_id = candidate["model_id"]
    if model_id not in {"gromacs", "gromacs-mpi"}:
        raise ValueError(
            "Only explicit GROMACS engine Apps are handled by this onboarding"
        )
    mpi = model_id == "gromacs-mpi"
    collector_id = "gromacs-mpi-workflow-v1" if mpi else "gromacs-workflow-v1"
    variant_id = "upstream-2026-2-mpi-v1" if mpi else "nvidia-2026-2-single-gpu-v1"
    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", runtime_image):
        raise ValueError("runtime image must be immutable")
    if (
        evidence.get("runtime_image") != runtime_image
        or not evidence.get("tests")
        or not evidence.get("recorded_at")
    ):
        raise ValueError("runtime evidence must bind this exact image")
    current = values["scientificBatch"]
    if (
        hashlib.sha256(scheduling_raw).hexdigest()
        != current["schedulingContractSha256"]
    ):
        raise ValueError("scheduler bytes differ from the captured release")
    live = current["executionMap"]
    live = json.loads(live) if isinstance(live, str) else live
    if set(live) - {"schema", "models", "snapshot_bundles", "qualification_baselines"}:
        raise ValueError("unrecognized execution map fields")
    existing = next(
        (row for row in live["models"] if row["model_id"] == model_id), None
    )
    if existing is not None and not replace_existing:
        raise ValueError("GROMACS already exists; prepare an explicit successor")
    if existing is None and replace_existing:
        raise ValueError("a successor requires the captured GROMACS release")
    profile = copy.deepcopy(candidate)
    if (profile["state"], profile["route_exposed"]) != ("candidate-unqualified", False):
        raise ValueError("expected the unrouted GROMACS candidate")
    profile.update(state="active", route_exposed=True)
    profile["source"]["classification"] = "qualified-input"
    profile["interface"]["mcp"]["invocable"] = True
    profile["semantic_validation"]["state"] = "active"
    profile["policy"]["limitations"][0] = (
        "Onboarding release: native engine runtime tested; customer REST/MCP, bucket recovery, "
        "queue/preemption and LibreChat qualification are not yet complete. Not customer-ready."
    )
    identity = profile["execution_identity"]
    identity.update(
        runtime_image_digest=runtime_image.rsplit("@", 1)[1],
        runtime_recipe_sha256=recipe_sha256,
        workload_recipe_sha256=digest(profile["workload"]),
        artifact_manifest_digest=digest([]),
    )
    identity["execution_identity_sha256"] = digest(
        {k: v for k, v in identity.items() if k != "execution_identity_sha256"}
    )
    baseline = next(row for row in live["models"] if row["model_id"] == "rfdiffusion")
    row = {
        "model_id": model_id,
        "variant_id": variant_id,
        "execution_identity_sha256": identity["execution_identity_sha256"],
        "access_profile": "public",
        "workload_namespace": baseline["workload_namespace"],
        "plan_adapter": copy.deepcopy(baseline["plan_adapter"]),
        "runtime_artifacts": [],
        "stages": [
            {
                "stage_id": "workflow",
                "image": runtime_image,
                "collector_id": collector_id,
                "validator_id": collector_id,
                "environment": {},
                "required_node_labels": {"kubernetes.io/arch": "amd64"},
                "resources": {
                    "requests": {
                        "cpu": "8000m",
                        "memory": "16Gi",
                        "ephemeral_storage": "64Gi",
                    },
                    "limits": {
                        "cpu": "8000m",
                        "memory": "16Gi",
                        "ephemeral_storage": "64Gi",
                    },
                },
                "active_deadline_seconds": 261000,
                "termination_grace_seconds": 120,
                "service_account_name": "default",
                "workspace_uid": 10001,
                "workspace_gid": 10001,
                "mounts": [
                    {
                        "name": "artifact-workspace",
                        "kind": "artifact-workspace",
                        "claim_name": None,
                        "host_path": None,
                        "mount_path": "/mnt/fs2-scientific",
                        "sub_path": None,
                        "read_only": False,
                    }
                ],
            }
        ],
    }
    desired = copy.deepcopy(live)
    if replace_existing:
        # A bugfix successor changes only this App's image/identity. Keep the
        # operator's resources, adapter, namespace and other Apps unchanged.
        row = copy.deepcopy(existing)
        if len(row["stages"]) != 1 or row["stages"][0]["stage_id"] != "workflow":
            raise ValueError(
                "unexpected GROMACS stage layout; review the successor explicitly"
            )
        row["execution_identity_sha256"] = identity["execution_identity_sha256"]
        row["stages"][0]["image"] = runtime_image
        desired["models"] = [
            row if item["model_id"] == model_id else item for item in desired["models"]
        ]
    else:
        desired.setdefault("qualification_baselines", {})[
            digest({"schema": live["schema"], "models": live["models"]})
        ] = [item["model_id"] for item in live["models"]]
        desired["models"].append(row)
    by_id = {item["model_id"]: item for item in desired["models"]}
    for expected, ids in desired["qualification_baselines"].items():
        if (
            digest({"schema": live["schema"], "models": [by_id[key] for key in ids]})
            != expected
        ):
            raise ValueError("existing qualification baseline changed")
    profile["qualification"] = {
        "h100_semantic_receipt_sha256": digest(evidence),
        "public_completion_receipt_sha256": None,
        "scheduler_eligibility_receipt_sha256": None,
        "execution_map_sha256": digest(
            {"schema": desired["schema"], "models": desired["models"]}
        ),
        "qualified_at": evidence["recorded_at"],
    }
    scheduling = json.loads(scheduling_raw)
    pools = profile["resources"]["compatible_pool_ids"]
    current_pools = scheduling["model_eligible_pool_ids"].get(model_id)
    if (
        not set(pools).issubset(scheduling["pools"])
        or current_pools is not None
        and (not replace_existing or current_pools != pools)
    ):
        raise ValueError("new GROMACS pool mapping does not match this deployment")
    scheduling["model_eligible_pool_ids"][model_id] = pools
    raw = canonical(scheduling)
    sha = hashlib.sha256(raw).hexdigest()
    name = current["schedulingContractConfigMapName"].rsplit("-", 1)[0] + "-" + sha[:12]
    cm = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": current["schedulingContractNamespace"]},
        "data": {current["schedulingContractKey"]: raw.decode()},
    }
    overlay = {
        "scientificBatch": {
            "executionMap": desired,
            "schedulingContractConfigMapName": name,
            "schedulingContractSha256": sha,
        }
    }
    if "scientificArtifacts" in values:
        overlay["scientificArtifacts"] = {
            "mediaTypes": sorted(
                set(values["scientificArtifacts"]["mediaTypes"])
                | {"application/x-tar", "application/vnd.fs2.gromacs-checkpoint+json"}
            )
        }
    return profile, row, overlay, cm


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument(
        "--model", choices=("gromacs", "gromacs-mpi"), default="gromacs"
    )
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--gpu-result", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--publish-catalog",
        action="store_true",
        help="Write the generated onboarding profile/map to this source checkout; never deploy",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Prepare an explicit GROMACS-only image/recipe successor; preserve other Apps",
    )
    args = parser.parse_args()
    evidence = runtime_receipt(args.runtime_image, args.gpu_result, model_id=args.model)
    recipe = source_recipe(ROOT)
    profile, row, overlay, cm = prepare(
        json.loads((args.baseline / "values.json").read_text()),
        (args.baseline / "scheduling.json").read_bytes(),
        json.loads(
            (
                HERE
                / (
                    "mpi-workload-profile.json"
                    if args.model == "gromacs-mpi"
                    else "workload-profile.json"
                )
            ).read_text()
        )["profile"],
        args.runtime_image,
        evidence,
        digest(recipe),
        replace_existing=args.replace_existing,
    )
    args.output.mkdir(parents=True, exist_ok=False)
    for name, value in [
        ("profile.json", profile),
        ("execution-row.json", row),
        ("activation.values.json", overlay),
        ("scheduling.configmap.json", cm),
        ("runtime-receipt.json", evidence),
        ("source-recipe.json", recipe),
    ]:
        (args.output / name).write_bytes(canonical(value) + b"\n")
    if args.publish_catalog:
        contracts = ROOT / "catalog/runtime/contracts"
        catalog_path = contracts / "scientific-workload-profiles.json"
        catalog = json.loads(catalog_path.read_text())
        existing = any(item["model_id"] == args.model for item in catalog["profiles"])
        if existing and not args.replace_existing:
            raise ValueError(
                "source already contains GROMACS; review an explicit successor"
            )
        if not existing and args.replace_existing:
            raise ValueError("the source catalog must contain the predecessor")
        source_map = json.loads(
            (contracts / "scientific-execution-map.json").read_text()
        )
        captured_map = json.loads((args.baseline / "values.json").read_text())[
            "scientificBatch"
        ]["executionMap"]
        captured_map = (
            json.loads(captured_map) if isinstance(captured_map, str) else captured_map
        )
        comparable = lambda value: {
            k: v for k, v in value.items() if k != "snapshot_bundles"
        }
        if comparable(source_map) != comparable(captured_map):
            raise ValueError(
                "source execution map differs from the captured live release"
            )
        if args.replace_existing:
            catalog["profiles"] = [
                profile if item["model_id"] == args.model else item
                for item in catalog["profiles"]
            ]
        else:
            catalog["profiles"].append(profile)
        catalog_path.write_text(json.dumps(catalog, indent=2) + "\n")
        # Environment-local snapshot PVC/configmap identities stay in the live
        # values overlay. Do not bake this cluster's bundle settings into IaC.
        published_map = copy.deepcopy(source_map)
        if args.replace_existing:
            published_map["models"] = [
                row if item["model_id"] == args.model else item
                for item in published_map["models"]
            ]
        else:
            published_map["models"].append(row)
        published_map["qualification_baselines"] = overlay["scientificBatch"][
            "executionMap"
        ]["qualification_baselines"]
        (contracts / "scientific-execution-map.json").write_text(
            json.dumps(published_map, indent=2) + "\n"
        )
        # The operator's complete inventory also needs the upstream source
        # observation; adding only a callable profile hides the App from admin.
        receipt_path = contracts / "scientific-source-candidate-receipts.json"
        receipt_catalog = json.loads(receipt_path.read_text())
        receipt = json.loads(
            (
                HERE
                / (
                    "mpi-source-candidate-receipt.json"
                    if args.model == "gromacs-mpi"
                    else "source-candidate-receipt.json"
                )
            ).read_text()
        )
        previous = [
            item
            for item in receipt_catalog["receipts"]
            if item["model_id"] == args.model
        ]
        if previous and previous != [receipt]:
            raise ValueError(
                "GROMACS source receipt changed; review the upstream observation"
            )
        if not previous:
            receipt_catalog["receipts"].append(receipt)
            receipt_path.write_text(json.dumps(receipt_catalog, indent=2) + "\n")
    print(
        json.dumps(
            {
                "state": "active-onboarding",
                "customer_ready": False,
                "retained_models": len(
                    overlay["scientificBatch"]["executionMap"]["models"]
                )
                - 1,
            }
        )
    )


if __name__ == "__main__":
    main()

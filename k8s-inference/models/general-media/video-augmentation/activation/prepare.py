"""Prepare one additive video canary; never deploy or requalify sibling Apps."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
MODEL = "physical-ai-video-augmentation"


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def prepare(
    values,
    scheduling_raw,
    candidate,
    runtime_image,
    evidence,
    evidence_sha256,
    recipe_sha256,
):
    if not re.fullmatch(
        r"cr\.eu-north1\.nebius\.cloud/[^\s@]+@sha256:[a-f0-9]{64}", runtime_image
    ):
        raise ValueError("a published immutable Stockholm runtime image is required")
    if (
        evidence.get("runtime_image") != runtime_image
        or evidence.get("live_gpu_generation_tested") is not True
        or evidence.get("worker_runtime_tested") is not True
        or evidence.get("structural_validation_passed") is not True
        or evidence.get("weather_validation_passed") is not True
        or evidence.get("motion_validation_passed") is not True
        or evidence.get("native_operation_status") != "succeeded"
        or not evidence.get("recorded_at")
    ):
        raise ValueError(
            "onboarding evidence must bind this worker and real GPU execution"
        )
    current = values["scientificBatch"]
    if (
        hashlib.sha256(scheduling_raw).hexdigest()
        != current["schedulingContractSha256"]
    ):
        raise ValueError("scheduler bytes differ from captured release")
    live = current["executionMap"]
    if isinstance(live, str):
        live = json.loads(live)
    if set(live) - {"schema", "models", "snapshot_bundles", "qualification_baselines"}:
        raise ValueError("unrecognized live execution fields")
    if MODEL in {item["model_id"] for item in live["models"]}:
        raise ValueError("canary already exists; use an explicitly reviewed successor")
    desired = copy.deepcopy(live)
    profile = copy.deepcopy(candidate)
    assert profile["model_id"] == MODEL and profile["state"] == "candidate-unqualified"
    profile.update(state="active", route_exposed=True)
    profile["source"]["classification"] = "qualified-input"
    profile["interface"]["mcp"]["invocable"] = True
    profile["semantic_validation"]["state"] = "active"
    profile["policy"]["limitations"][0] = (
        "Isolated active onboarding canary, not customer-qualified. "
        "Exact-worker GPU prerequisite is recorded; hosted chat, public parent and bucket cohorts remain required."
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
    # Same established CPU companion, workspace and queue contract; no GPU is
    # allocated to the parent. The native Cosmos child retains its own policy.
    row = copy.deepcopy(
        next(
            item
            for item in live["models"]
            if item["model_id"] == "cosmos3-lerobot-augmentation"
        )
    )
    row.update(
        model_id=MODEL,
        variant_id="paidf-cosmos3-nano-v1",
        execution_identity_sha256=identity["execution_identity_sha256"],
    )
    stage = row["stages"][0]
    stage.update(
        stage_id="augment-videos",
        image=runtime_image,
        collector_id="paidf-video-v1",
        validator_id="paidf-video-v1",
        environment={
            "PAIDF_PROVIDER_URL": "https://api.tokenfactory.nebius.com/v1",
            "PAIDF_VLM_MODEL": "MiniMaxAI/MiniMax-M3",
            "PAIDF_LLM_MODEL": "Qwen/Qwen3-235B-A22B-Instruct-2507",
        },
    )
    prior = {"schema": live["schema"], "models": live["models"]}
    desired.setdefault("qualification_baselines", {})[digest(prior)] = [
        item["model_id"] for item in live["models"]
    ]
    desired["models"].append(row)
    by_id = {item["model_id"]: item for item in desired["models"]}
    if len(by_id) != len(desired["models"]):
        raise ValueError("duplicate execution models")
    for expected, ids in desired["qualification_baselines"].items():
        if (
            digest({"schema": live["schema"], "models": [by_id[key] for key in ids]})
            != expected
        ):
            raise ValueError(
                "an existing qualification baseline no longer reconstructs exactly"
            )
    profile["qualification"] = {
        "h100_semantic_receipt_sha256": evidence_sha256,
        "public_completion_receipt_sha256": None,
        "scheduler_eligibility_receipt_sha256": None,
        "execution_map_sha256": digest(
            {"schema": desired["schema"], "models": desired["models"]}
        ),
        "qualified_at": evidence["recorded_at"],
    }
    scheduling = json.loads(scheduling_raw)
    pools = scheduling["model_eligible_pool_ids"]["cosmos3-nano"]
    if (
        not isinstance(pools, list)
        or not pools
        or MODEL in scheduling["model_eligible_pool_ids"]
    ):
        raise ValueError("expected one new scheduling eligibility row")
    scheduling["model_eligible_pool_ids"][MODEL] = list(pools)
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
    return profile, row, overlay, cm


def source_recipe(root):
    """Bind this coordinator and the shared handoff without rehashing old Apps."""
    sys.path.insert(0, str(root / "components/control-plane/src"))
    from fs2_serve.scientific_batch.adapters.common import _RECIPE_SHARED_PATHS

    paths = set(_RECIPE_SHARED_PATHS) | {
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/video_augmentation.py",
        "components/control-plane/src/fs2_serve/scientific_batch/child_routes.py",
        "catalog/runtime/schema/video-augmentation-request.schema.json",
        "models/general-media/video-augmentation/runtime/Containerfile",
        "models/general-media/lerobot-augmentation/runtime/src/fs2_lerobot_augmentation/cosmos.py",
        "models/general-media/lerobot-augmentation/runtime/src/fs2_lerobot_augmentation/contracts.py",
    }
    paths.update(
        str(path.relative_to(root))
        for path in (HERE.parent / "runtime/fs2_video").glob("*.py")
    )
    return {
        "schema": "fs2-serve.nebius.ai/video-runtime-recipe/v1",
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest(),
                "size_bytes": (root / path).stat().st_size,
            }
            for path in sorted(paths)
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-values", type=Path, required=True)
    parser.add_argument("--baseline-scheduling", type=Path, required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--semantic-receipt", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    recipe = source_recipe(ROOT)
    profile, row, overlay, cm = prepare(
        json.loads(args.baseline_values.read_text()),
        args.baseline_scheduling.read_bytes(),
        json.loads((HERE / "workload-profile.json").read_text())["profile"],
        args.runtime_image,
        json.loads(args.semantic_receipt.read_text()),
        hashlib.sha256(args.semantic_receipt.read_bytes()).hexdigest(),
        digest(recipe),
    )
    args.output_dir.mkdir(parents=True, exist_ok=False)
    for name, value in [
        ("workload-profile.json", profile),
        ("execution-row.json", row),
        ("activation.values.json", overlay),
        ("scheduling.configmap.json", cm),
        ("source-recipe.json", recipe),
    ]:
        (args.output_dir / name).write_bytes(canonical(value) + b"\n")
    print(
        json.dumps(
            {
                "model_id": MODEL,
                "state": profile["state"],
                "customer_ready": False,
                "sibling_rows_preserved": len(
                    overlay["scientificBatch"]["executionMap"]["models"]
                )
                - 1,
                "scheduler_configmap": cm["metadata"]["name"],
            }
        )
    )


if __name__ == "__main__":
    main()

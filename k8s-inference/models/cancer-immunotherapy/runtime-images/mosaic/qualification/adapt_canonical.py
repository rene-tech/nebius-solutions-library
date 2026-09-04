#!/usr/bin/env python3
"""Bind the canonical mosaic plan to accepted localization generations.

``submit_plan.py`` is the historical path: it mounts one task-owned claim at the
artifact root and records, in its own DEVIATIONS list, that the canonical
adapter renders no artifact mount at all. That path is kept for audit and is not
edited here.

This adapter is the canonical one. Every model byte comes from an accepted
localization generation on the public reference-data host plane, mounted
read-only at exactly the path the immutable runtime reads, and the generation
digest is read out of the localization contract rather than supplied by hand.
One marker-admission init container runs per artifact in the same pod that
consumes the bytes, so a rejected marker fails the pod before any GPU work.

The mosaic runtime resolves three fixed locations under ``FS2_ARTIFACT_ROOT``:
``mosaic/boltz/boltz2_conf.ckpt``, ``mosaic/boltz/mols`` and
``mosaic/proteinmpnn``. Those are three separate generations, so the checkpoint
is bound as a single file and its two siblings as directories; the intermediate
``mosaic/boltz`` directory is created by the kubelet and is never itself a
mount, which is what lets a file and a directory sit beside each other without
either being nested inside the other's read-only mount.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LOCK = json.loads((HERE.parent / "image-lock.json").read_text(encoding="utf-8"))

ARTIFACT_ROOT = "/opt/fs2/artifacts"
ADMISSION_ROOT = "/opt/fs2/admission"
PUBLIC_TREE_PREFIX = "scientific-localization/public"
REFERENCE_DATA_HOST_ROOT = "/mnt/fs2-reference-data/data"
REFERENCE_DATA_NODE_LABEL = "storage.fs2.nebius/reference-data"
VERIFIER_MOUNT = "/opt/fs2-localization"
VERIFIER_IMAGE = (
    "docker.io/library/python@sha256:"
    "9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534"
)
TARGET_ARTIFACT_ID = "artifact.mosaic.target.minibinder"

# Where the immutable runtime reads each accepted artifact, relative to
# FS2_ARTIFACT_ROOT, and whether that location is the generation's single entry
# or the generation directory itself.
RUNTIME_BINDINGS = (
    ("mosaic-boltz2-conf", "mosaic/boltz/boltz2_conf.ckpt", "boltz2_conf.ckpt"),
    ("boltzgen-inference-molecules", "mosaic/boltz/mols", None),
    ("mosaic-components", "mosaic/proteinmpnn", None),
)


def _solution_root(start: Path) -> Path:
    relative = Path("catalog/runtime/contracts/scientific-artifact-localization.json")
    for candidate in (start, *start.parents):
        if (candidate / relative).is_file():
            return candidate
    raise SystemExit(f"could not locate {relative} above {start}")


SOLUTION_ROOT = _solution_root(HERE)
LOCALIZATION_CONTRACT = (
    SOLUTION_ROOT / "catalog/runtime/contracts/scientific-artifact-localization.json"
)


def generation_sub_path(artifact_id: str, generation: str) -> str:
    return f"{PUBLIC_TREE_PREFIX}/generations/{artifact_id}/sha256/{generation}"


def accepted_artifacts(contract_path: Path) -> dict[str, dict[str, Any]]:
    document = json.loads(contract_path.read_text(encoding="utf-8"))
    return {item["artifact_id"]: item for item in document["artifacts"]}


def admission_container(
    artifact_id: str, artifact: dict[str, Any], generation: str
) -> dict[str, Any]:
    sub_path = generation_sub_path(artifact_id, generation)
    return {
        "name": f"admit-{artifact_id}"[:63],
        "image": VERIFIER_IMAGE,
        "command": [
            "python3", "-m", "fs2_localization.localization", "marker",
            "--artifact-id", artifact_id,
            "--mount", f"{ADMISSION_ROOT}/{artifact_id}",
            "--expect-generation", generation,
            "--sub-path", sub_path,
            "--expect-visibility", artifact.get("visibility", "public"),
            "--expect-algorithm", artifact["tree"]["inventory_algorithm"],
            "--expect-volume-kind", "host-path",
            "--expect-host-root", REFERENCE_DATA_HOST_ROOT,
        ],
        "env": [
            {"name": "PYTHONPATH", "value": VERIFIER_MOUNT},
            {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
        ],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "readOnlyRootFilesystem": True,
        },
        "resources": {
            "requests": {"cpu": "200m", "memory": "256Mi"},
            "limits": {"cpu": "2", "memory": "2Gi"},
        },
        "volumeMounts": [
            {
                "name": "verifier",
                "mountPath": f"{VERIFIER_MOUNT}/fs2_localization",
                "readOnly": True,
            },
            {
                "name": "trees",
                "mountPath": f"{ADMISSION_ROOT}/{artifact_id}",
                "subPath": sub_path,
                "readOnly": True,
            },
        ],
    }


def adapt(
    job: dict[str, Any],
    *,
    run_claim: str,
    digest: str,
    gpu: bool,
    accepted: dict[str, dict[str, Any]],
    verifier_config_map: str,
    target_config_map: str,
    accelerator_class: str,
    pool_id: str,
    run_sub_path: str,
) -> dict[str, Any]:
    adapted = copy.deepcopy(job)
    spec = adapted["spec"]["template"]["spec"]
    # The canonical batch plane is not deployed on this cluster; the pod already
    # disables token automount and the registry is reachable without a secret.
    spec.pop("serviceAccountName", None)
    spec.pop("imagePullSecrets", None)
    for volume in spec["volumes"]:
        if volume["name"] == "workspace":
            volume["persistentVolumeClaim"]["claimName"] = run_claim
    for mount in spec["containers"][0]["volumeMounts"]:
        if mount["name"] == "workspace":
            # A per-run subdirectory. The runtime refuses a non-empty shard
            # output directory, which is correct, and the shared claim already
            # holds the historical run at the same plan-derived path.
            mount["subPath"] = run_sub_path
    security = spec.setdefault("securityContext", {})
    if "fsGroup" in security:
        # Without this the kubelet chowns the whole claim before any container
        # starts. Measured at 92 s over 51,679 files on the shared claim, which
        # delays marker admission and the GPU stage for no benefit: the run only
        # ever writes under its own subdirectory.
        security["fsGroupChangePolicy"] = "OnRootMismatch"

    container = spec["containers"][0]
    mounts: list[dict[str, Any]] = container["volumeMounts"]
    generations: list[str] = []

    spec["volumes"].append(
        {
            "name": "trees",
            "hostPath": {"path": REFERENCE_DATA_HOST_ROOT, "type": "Directory"},
        }
    )
    init_containers: list[dict[str, Any]] = []
    for artifact_id, relative, entry in RUNTIME_BINDINGS:
        artifact = accepted.get(artifact_id)
        if artifact is None:
            raise SystemExit(f"{artifact_id} is not an accepted localization artifact")
        generation = artifact["tree"]["inventory_sha256"]
        sub_path = generation_sub_path(artifact_id, generation)
        mounts.append(
            {
                "name": "trees",
                "mountPath": f"{ARTIFACT_ROOT}/{relative}",
                "subPath": f"{sub_path}/{entry}" if entry else sub_path,
                "readOnly": True,
            }
        )
        generations.append(f"{artifact_id}={generation}")
        if verifier_config_map:
            init_containers.append(admission_container(artifact_id, artifact, generation))

    if verifier_config_map:
        spec["volumes"].append(
            {"name": "verifier", "configMap": {"name": verifier_config_map}}
        )
        spec["initContainers"] = init_containers

    # The target sequence is request-scoped input, never a localized artifact.
    spec["volumes"].append(
        {"name": "inputs", "configMap": {"name": target_config_map}}
    )
    mounts.append(
        {"name": "inputs", "mountPath": f"{ARTIFACT_ROOT}/inputs", "readOnly": True}
    )

    if gpu:
        # Selected by accelerator class plus the reference-data tree the public
        # plane lives on. The pool id is pinned because Kueue assigns the first
        # flavor whose nominal quota fits and whose node labels are compatible
        # with the pod: inference-h100-1x is listed first and has quota for two
        # GPUs but no Ready node, so an unpinned pod is admitted onto a flavor
        # that cannot schedule and only the autoscaler recovers it, by building
        # a node. Naming the reserved pool makes Kueue choose the flavor whose
        # nodes actually exist. Both pools stay compatible in the profile.
        spec["nodeSelector"] = {
            "accelerator.fs2.nebius/class": accelerator_class,
            "accelerator.fs2.nebius/pool-id": pool_id,
            REFERENCE_DATA_NODE_LABEL: "true",
        }
        spec["tolerations"] = [
            {
                "effect": "NoSchedule",
                "key": "dedicated",
                "operator": "Equal",
                "value": "fs2-inference",
            }
        ]
    else:
        # The CPU stage still has to read the run outputs and reach the public
        # plane, so it lands on a reference-data node and must tolerate that
        # pool's taint. The system node is the only untainted one and runs at
        # over 90 per cent CPU request, so it is not a fallback.
        spec["nodeSelector"] = {REFERENCE_DATA_NODE_LABEL: "true"}
        spec["tolerations"] = [
            {
                "effect": "NoSchedule",
                "key": "workload.fs2.nebius/reference-data",
                "operator": "Equal",
                "value": "true",
            },
            {
                "effect": "NoSchedule",
                "key": "dedicated",
                "operator": "Equal",
                "value": "fs2-inference",
            },
        ]

    environment = [{"name": "FS2_ARTIFACT_ROOT", "value": ARTIFACT_ROOT}]
    if not gpu:
        # validate_output_manifest binds the committed manifest to the admitted
        # image digest, and the canonical Job carries no env that could supply it.
        environment.append({"name": "FS2_RUNTIME_IMAGE_DIGEST", "value": digest})
    container["env"] = environment

    annotations = adapted["metadata"].setdefault("annotations", {})
    annotations["fs2.nebius.ai/localization-generations"] = ",".join(sorted(generations))
    annotations["fs2.nebius.ai/artifact-delivery"] = "canonical-localization-generations"
    adapted["spec"]["template"]["metadata"].setdefault("annotations", {}).update(annotations)
    return adapted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=HERE / "generated-plan.json")
    parser.add_argument("--stage", choices=["design", "aggregate"], required=True)
    parser.add_argument(
        "--run-claim",
        required=True,
        help="writable claim for run outputs only; never an artifact source",
    )
    parser.add_argument(
        "--run-sub-path",
        default="mosaic/canonical-runs",
        help="per-run subdirectory on the run claim; must be empty of prior shard output",
    )
    parser.add_argument("--verifier-config-map", default="")
    parser.add_argument("--target-config-map", required=True)
    parser.add_argument("--accelerator-class", default="nvidia-h100-sxm5-80gb")
    parser.add_argument(
        "--pool-id",
        default="h100-reserved-8x",
        help="accelerator pool whose Kueue flavor has Ready nodes",
    )
    parser.add_argument(
        "--localization-contract", type=Path, default=LOCALIZATION_CONTRACT
    )
    parser.add_argument("--output", default="-")
    args = parser.parse_args(argv)

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    digest = LOCK["image"]["published_digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise SystemExit(f"image lock digest is not immutable: {digest!r}")
    accepted = accepted_artifacts(args.localization_contract)

    rendered = []
    for node in plan["plan"]["nodes"]:
        is_design = node["id"].startswith("design")
        if (args.stage == "design") != is_design:
            continue
        rendered.append(
            adapt(
                node["job"],
                run_claim=args.run_claim,
                digest=digest,
                gpu=is_design,
                accepted=accepted,
                verifier_config_map=args.verifier_config_map,
                target_config_map=args.target_config_map,
                accelerator_class=args.accelerator_class,
                pool_id=args.pool_id,
                run_sub_path=args.run_sub_path,
            )
        )
    if not rendered:
        raise SystemExit(f"no {args.stage} node in {args.plan}")
    document = {"apiVersion": "v1", "kind": "List", "items": rendered}
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        print(text, end="")
    else:
        Path(args.output).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

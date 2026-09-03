#!/usr/bin/env python3
"""Render the production-equivalent native BindCraft semantic acceptance Job.

The run this emits is deliberately not a hand-written smoke Pod. It enters the
image through the outer entrypoint, carries the same argv the model-local
adapter's batch plan builds - including the SHA-256-pinned production advanced
settings and ``default_filters.json`` - and mounts all four trees the image
needs from outside itself.

Where each tree lives is an input, never a constant here: the shared filesystem
that carries them is re-published as its handoff evolves, and a renderer that
hard-coded one layout would emit a Job that quietly reads the wrong bytes. What
*is* constant is where each tree has to land inside the image, because the model
code resolves those paths itself, so the mount paths belong to this file and the
handoff only says which subPath and which immutable identity.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
HANDOFF_SCHEMA = "fs2.nebius.ai/bindcraft-external-tree-handoff/v1"
REQUEST_SCHEMA = "fs2-serve.nebius.ai/scientific-run-request/v1"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/bindcraft-native-pyrosetta-parameters/v1"
ARTIFACT_MANIFEST_SCHEMA = "fs2-serve.nebius.ai/scientific-artifact-manifest/v1"

# The pinned production settings the adapter admits by digest. Kept identical to
# models/structure/runtime/bindcraft-native/batch_adapter.py.
SETTINGS_TEMPLATE = "/opt/bindcraft/settings_advanced/default_4stage_multimer.json"
SETTINGS_SHA256 = "4124733af9dff65fb23e6a5f52b2329fc0d7a4ce5c50b6df225422f77fe467d6"
FILTERS = "/opt/bindcraft/settings_filters/default_filters.json"
FILTERS_SHA256 = "4faeae2ed4a78b82ff8f9c3c763985ff0f0b97ebb9e10072d5d572424bb73206"

# The upstream BindCraft PD-L1 example, which ships inside the image.
DEFAULT_TARGET_PDB = "/opt/bindcraft/example/PDL1.pdb"
DEFAULT_TARGET_SHA256 = "d3c95434dcadf26d005340b15bd92be61e101ed921478c26f2a5550f198e61f6"
DEFAULT_TARGET_BYTES = 74686

ACADEMIC_ASSET_GID = 65532
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DIGEST_REFERENCE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
SUB_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,507}$")


class RenderError(RuntimeError):
    """The requested semantic run could not be rendered."""


def _runtime_contract() -> Any:
    """Load the image's own runtime module so its constants are not duplicated."""

    path = ROOT / "runtime" / "bindcraft_runtime_entrypoint.py"
    spec = importlib.util.spec_from_file_location("fs2_bindcraft_runtime", path)
    if spec is None or spec.loader is None:
        raise RenderError("runtime entrypoint is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONTRACT = _runtime_contract()

# Where each tree has to be mounted for the model code to find it. The MPNN
# roots are colabdesign.mpnn's own package directories, which the image builds
# empty on purpose; AF2 is the directory handed to BindCraft as af_params_dir.
MOUNT_PATH_BY_ROLE = {
    CONTRACT.PYROSETTA_ROLE: "/opt/fs2/academic/pyrosetta-bindcraft/site-packages",
    CONTRACT.AF2_PARAMS_ROLE: "/models/alphafold2",
    CONTRACT.MPNN_VANILLA_ROLE: "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights",
    CONTRACT.MPNN_SOLUBLE_ROLE: "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights_soluble",
}


def canonical(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def load_handoff(path: Path) -> dict[str, Any]:
    """Validate the artifact plane's four-tree handoff before trusting a path."""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != HANDOFF_SCHEMA:
        raise RenderError("external tree handoff schema is unsupported")
    claim = value.get("claim")
    if not isinstance(claim, str) or not claim:
        raise RenderError("external tree handoff names no persistent volume claim")
    declared = value.get("trees")
    if not isinstance(declared, list):
        raise RenderError("external tree handoff declares no trees")
    by_role: dict[str, dict[str, Any]] = {}
    for entry in declared:
        if not isinstance(entry, dict):
            raise RenderError("external tree handoff entry is malformed")
        role = entry.get("role")
        if role not in CONTRACT.REQUIRED_TREE_ROLES:
            raise RenderError(f"external tree handoff declares unsupported role {role!r}")
        if role in by_role:
            raise RenderError(f"external tree handoff declares role {role!r} twice")
        for field in ("artifact_id", "sub_path", "sha256"):
            if not isinstance(entry.get(field), str) or not entry[field]:
                raise RenderError(f"{role}: external tree handoff has no {field}")
        if SHA256.fullmatch(entry["sha256"]) is None:
            raise RenderError(f"{role}: external tree identity must be a lowercase SHA-256")
        if SUB_PATH.fullmatch(entry["sub_path"]) is None or ".." in entry["sub_path"].split("/"):
            raise RenderError(f"{role}: external tree subPath is unsafe")
        by_role[role] = entry
    missing = sorted(CONTRACT.REQUIRED_TREE_ROLES - set(by_role))
    if missing:
        raise RenderError("external tree handoff is missing roles: " + ", ".join(missing))
    licensed = by_role[CONTRACT.PYROSETTA_ROLE]["sha256"]
    if licensed != CONTRACT.PYROSETTA_TREE_MANIFEST_SHA256:
        raise RenderError(
            "handoff PyRosetta tree identity is not the licensed tree this image is built for"
        )
    generation = value.get("generation")
    if not isinstance(generation, str) or CONTRACT.LOCALIZATION_GENERATION.fullmatch(generation) is None:
        raise RenderError("external tree handoff names no immutable localization generation")
    return {"claim": claim, "generation": generation, "trees": by_role}


def localization_marker(handoff: dict[str, Any]) -> dict[str, Any]:
    """Stand in for the marker the shared controller writes per stage.

    This renderer drives the acceptance run directly rather than through the
    controller, so it writes the same marker the controller would and passes the
    same argv, keeping the run on the interface the controller will use.
    """

    return {
        "schema": "fs2.nebius.ai/runtime-localization-marker/v1",
        "generation": handoff["generation"],
        "claim": handoff["claim"],
        "trees": {
            role: {"artifact_id": entry["artifact_id"], "sub_path": entry["sub_path"]}
            for role, entry in sorted(handoff["trees"].items())
        },
    }


def admission_document(handoff: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": CONTRACT.EXTERNAL_TREE_ADMISSION_SCHEMA,
        "generation": handoff["generation"],
        "trees": [
            {
                "role": role,
                "artifact_id": entry["artifact_id"],
                "root": MOUNT_PATH_BY_ROLE[role],
                "sha256": entry["sha256"],
            }
            for role, entry in sorted(handoff["trees"].items())
        ],
    }


def input_manifest(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema": ARTIFACT_MANIFEST_SCHEMA,
        "manifest_id": args.manifest_id,
        "entries": [
            {
                "name": "target_structure",
                "semantic_type": "protein-structure-pdb/v1",
                "artifact": {
                    "artifact_id": args.target_artifact_id,
                    "sha256": args.target_sha256,
                    "size_bytes": args.target_size_bytes,
                    "media_type": "chemical/x-pdb",
                    "compression": "none",
                },
            }
        ],
    }


def request(args: argparse.Namespace, manifest_bytes: bytes) -> dict[str, Any]:
    if args.binder_length_minimum > args.binder_length_maximum:
        raise RenderError("binder length minimum exceeds its maximum")
    return {
        "schema": REQUEST_SCHEMA,
        "operation": "design-binder",
        "service_class": args.service_class,
        "input_manifest": {
            "artifact_id": args.manifest_id,
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "size_bytes": len(manifest_bytes),
            "media_type": "application/vnd.fs2.scientific-manifest+json",
            "compression": "none",
        },
        "parameters": {
            "schema": PARAMETER_SCHEMA,
            "shard_count": 1,
            "base_seed": args.seed,
            "target_chains": [args.target_chain],
            "hotspots": [{"chain": args.target_chain, "residue": residue} for residue in args.hotspot],
            "binder_length": {
                "minimum": args.binder_length_minimum,
                "maximum": args.binder_length_maximum,
            },
            "accepted_designs_per_shard": args.accepted_designs,
            "max_trajectories_per_shard": args.max_trajectories,
        },
        "client_context": {"batch_id": args.batch_id, "display_name": args.display_name},
    }


MARKER_PATH = "/var/run/fs2/runtime-localization.json"


def _stage_command(args: argparse.Namespace, stage: str) -> list[str]:
    """Build one stage's argv exactly as the batch adapter builds it."""

    shards = f"/workspace/runs/{args.run_id}/shards"
    common = [
        # The image ENTRYPOINT, invoked explicitly so the shared external
        # artifact gate and PyRosetta binding run before any model code.
        "python", "/opt/fs2/runtime_entrypoint.py",
        "/opt/fs2/bin/bindcraft-batch", stage,
        "--backend-id", CONTRACT.BACKEND_ID,
        "--request", "/var/run/fs2/request.json",
        "--input-manifest", "/var/run/fs2/input-manifest.json",
    ]
    if stage == "run-trajectory":
        return common + [
            "--settings-template", SETTINGS_TEMPLATE,
            "--settings-sha256", SETTINGS_SHA256,
            "--filters", FILTERS,
            "--filters-sha256", FILTERS_SHA256,
            "--shard-index", str(args.shard_index),
            "--seed", str(args.seed + args.shard_index),
            "--pyrosetta-required",
            "--output", f"{shards}/{args.shard_index:03d}",
            "--runtime-localization-marker", MARKER_PATH,
        ]
    return common + [
        "--shards", shards,
        "--expected-shards", "1",
        "--staging-manifest", f"/workspace/runs/{args.run_id}/output-manifest.json.tmp",
        "--output-manifest", f"/workspace/runs/{args.run_id}/output-manifest.json",
        "--atomic-rename",
        "--runtime-localization-marker", MARKER_PATH,
    ]


def job(args: argparse.Namespace, handoff: dict[str, Any], config_name: str) -> dict[str, Any]:
    digest = args.image.rsplit("@", 1)[1]
    labels = {
        "app.kubernetes.io/name": "fs2-batch",
        "app.kubernetes.io/part-of": "fs2-serve",
        "app.kubernetes.io/managed-by": "fs2-cancer-runtime-semantic-acceptance",
        "fs2.nebius.ai/model-id": "bindcraft",
        "fs2.nebius.ai/task": args.task_id,
        "fs2.nebius.ai/service-class": args.service_class,
    }
    if args.local_queue:
        labels["kueue.x-k8s.io/queue-name"] = args.local_queue
    volume_mounts = [
        {"name": "request", "mountPath": "/var/run/fs2", "readOnly": True},
        {"name": "workspace", "mountPath": "/workspace"},
        {"name": "tmp", "mountPath": "/tmp"},
    ]
    for role, entry in sorted(handoff["trees"].items()):
        volume_mounts.append({
            "name": "external-trees",
            "mountPath": MOUNT_PATH_BY_ROLE[role],
            "subPath": entry["sub_path"],
            "readOnly": True,
        })
    # Both stages pass the shared outer entrypoint, which verifies the AlphaFold2
    # manifest and binds PyRosetta, so both need the same trees and environment.
    env = [
        {"name": "PYTHONPATH", "value": f"{MOUNT_PATH_BY_ROLE[CONTRACT.PYROSETTA_ROLE]}:/opt/bindcraft"},
        {"name": "FS2_RUNTIME_IMAGE_DIGEST", "value": digest},
        {"name": "FS2_BINDCRAFT_EXTERNAL_TREES", "value": "/var/run/fs2/external-trees.json"},
        {"name": "FS2_RUNTIME_LOCALIZATION_MARKER", "value": MARKER_PATH},
        {"name": "FS2_BINDCRAFT_TARGET_PDB", "value": args.target_pdb},
        {"name": "FS2_ARTIFACT_ROOT", "value": MOUNT_PATH_BY_ROLE[CONTRACT.AF2_PARAMS_ROLE]},
        {
            "name": "FS2_ARTIFACT_MANIFEST",
            "value": MOUNT_PATH_BY_ROLE[CONTRACT.AF2_PARAMS_ROLE] + "/manifest.json",
        },
        {"name": "FS2_ARTIFACT_KIND", "value": "bindcraft-af2-params"},
        {"name": "FS2_SOURCE_REVISION", "value": CONTRACT.SOURCE_REVISION},
        {"name": "XLA_PYTHON_CLIENT_PREALLOCATE", "value": "false"},
    ]
    if args.mpnn_weights:
        env.append({"name": "FS2_BINDCRAFT_MPNN_WEIGHTS", "value": args.mpnn_weights})
    security = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "terminationGracePeriodSeconds": 300,
        "nodeSelector": {"accelerator.fs2.nebius/class": args.accelerator_class},
        "tolerations": [
            {"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}
        ],
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "runAsGroup": 10001,
            "supplementalGroups": [ACADEMIC_ASSET_GID],
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        # The design stage runs as the init container and the aggregate as the
        # main one, so both enter the image through its outer entrypoint and
        # share one workspace. Two Jobs could not: the shard output lives on the
        # Pod's own volume, and the aggregate has to read it.
        "initContainers": [{
            "name": "design",
            "image": args.image,
            "imagePullPolicy": "IfNotPresent",
            "command": _stage_command(args, "run-trajectory"),
            "env": env,
            "resources": {
                "requests": {"cpu": "16", "memory": "96Gi", "nvidia.com/gpu": 1},
                "limits": {"cpu": "24", "memory": "128Gi", "nvidia.com/gpu": 1},
            },
            "securityContext": security,
            "volumeMounts": volume_mounts,
        }],
        "containers": [{
            "name": "aggregate",
            "image": args.image,
            "imagePullPolicy": "IfNotPresent",
            "command": _stage_command(args, "aggregate"),
            "env": env,
            # Aggregation is content addressing and an atomic rename; it needs
            # no accelerator and must not hold one while it runs.
            "resources": {
                "requests": {"cpu": "2", "memory": "8Gi"},
                "limits": {"cpu": "4", "memory": "16Gi"},
            },
            "securityContext": security,
            "volumeMounts": volume_mounts,
        }],
        "volumes": [
            {"name": "request", "configMap": {"name": config_name, "defaultMode": 0o444}},
            {"name": "external-trees", "persistentVolumeClaim": {"claimName": handoff["claim"], "readOnly": True}},
            {"name": "workspace", "emptyDir": {"sizeLimit": args.workspace_size}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "32Gi"}},
        ],
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": args.job_name, "namespace": args.namespace, "labels": labels},
        "spec": {
            "suspend": bool(args.local_queue),
            "backoffLimit": 0,
            "activeDeadlineSeconds": args.deadline_seconds,
            "ttlSecondsAfterFinished": 86_400,
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }


def render(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    handoff = load_handoff(Path(args.handoff))
    manifest_bytes = canonical(input_manifest(args)).encode()
    documents = {
        "request.json": canonical(request(args, manifest_bytes)),
        "input-manifest.json": manifest_bytes.decode(),
        "external-trees.json": canonical(admission_document(handoff)),
        "runtime-localization.json": canonical(localization_marker(handoff)),
    }
    config_name = f"fs2-run-{args.run_id}"
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": config_name, "namespace": args.namespace},
        "data": documents,
    }
    return config_map, job(args, handoff, config_name)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--handoff", required=True, help="artifact-plane four-tree handoff JSON")
    root.add_argument("--image", required=True, help="runtime image, pinned by digest")
    root.add_argument("--run-id", required=True)
    root.add_argument("--job-name", required=True)
    root.add_argument("--namespace", default="fs2-academic-poc")
    root.add_argument("--task-id", default="fs2-bindcraft-final-image-h100-successor-r20260903")
    root.add_argument("--accelerator-class", default="nvidia-h100-sxm5-80gb")
    root.add_argument("--local-queue", default="", help="Kueue LocalQueue; empty runs unqueued")
    root.add_argument("--service-class", default="customer-batch")
    root.add_argument("--seed", type=int, default=384856)
    root.add_argument("--shard-index", type=int, default=0)
    root.add_argument("--max-trajectories", type=int, default=30)
    root.add_argument("--accepted-designs", type=int, default=1)
    root.add_argument("--binder-length-minimum", type=int, default=60)
    root.add_argument("--binder-length-maximum", type=int, default=75)
    root.add_argument("--target-chain", default="A")
    root.add_argument(
        "--mpnn-weights",
        choices=("original", "soluble"),
        default="",
        help="ProteinMPNN lane; empty keeps the pinned advanced template's value",
    )
    root.add_argument("--hotspot", type=int, action="append", default=None)
    root.add_argument("--target-pdb", default=DEFAULT_TARGET_PDB)
    root.add_argument("--target-sha256", default=DEFAULT_TARGET_SHA256)
    root.add_argument("--target-size-bytes", type=int, default=DEFAULT_TARGET_BYTES)
    root.add_argument("--target-artifact-id", default="artifact.bindcraft.target.pdl1")
    root.add_argument("--manifest-id", default="manifest.bindcraft.native.pdl1.production")
    root.add_argument("--batch-id", default="batch.bindcraft.native.production")
    root.add_argument("--display-name", default="native PD-L1 production acceptance")
    root.add_argument("--workspace-size", default="64Gi")
    root.add_argument("--deadline-seconds", type=int, default=86_400)
    return root


def main() -> None:
    args = parser().parse_args()
    if args.hotspot is None:
        args.hotspot = [56]
    if DIGEST_REFERENCE.fullmatch(args.image) is None:
        raise RenderError("runtime image must be pinned by digest")
    if SHA256.fullmatch(args.target_sha256) is None:
        raise RenderError("target structure digest must be a lowercase SHA-256")
    config_map, rendered = render(args)
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": [config_map, rendered]}, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (RenderError, OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

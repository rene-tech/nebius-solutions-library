#!/usr/bin/env python3
"""Render the production-equivalent native BindCraft semantic acceptance Job.

The run this emits is deliberately not a hand-written smoke Pod. It enters the
image through the outer entrypoint, carries the same argv the model-local
adapter's batch plan builds - including the SHA-256-pinned production advanced
settings and ``default_filters.json`` - and mounts all four trees the image
needs from outside itself.

Where each tree lives is an input, never a constant here, and the four do not
share a backing store: the three public trees are immutable generations on the
reference-data filesystem reached by hostPath, while only the licensed PyRosetta
tree sits on the private academic claim. So the handoff supplies each tree's own
volume source, subPath, immutable identity, and the node selector that pins a
Pod to a node carrying the public generation. What *is* constant is where each
tree has to land inside the image, because the model code resolves those paths
itself, so the mount paths belong to this file.

One stage per Job. The design stage needs a GPU and the aggregation does not, so
running them in one Pod would hold the accelerator through the CPU-only half.
They are separate Jobs over a durable workspace claim, and the aggregate
re-verifies every handed-off artifact against the digest the design stage
published.
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
HANDOFF_SCHEMA = "fs2-serve.nebius.ai/scientific-localization-binding-handoff/v1"
MODEL_ID = "bindcraft"
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

# A mixed-plane consumer needs both planes' groups, and which it needs follows
# from the planes it actually mounts rather than from a flag. Kept identical to
# the localization renderer on main, which asserts [1000, 65532] for exactly
# this shape.
ACADEMIC_ASSET_GID = 65532
PUBLIC_PLANE_GID = 1000
CANONICAL_PYROSETTA_SUB_PATH = "pyrosetta-bindcraft/site-packages"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DIGEST_REFERENCE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
SUB_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,507}$")
DNS_SUBDOMAIN = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?$")


class RenderError(RuntimeError):
    """The requested semantic run could not be rendered."""


def _runtime_module(name: str, filename: str) -> Any:
    """Load the image's own runtime modules so their constants are not duplicated."""

    spec = importlib.util.spec_from_file_location(name, ROOT / "runtime" / filename)
    if spec is None or spec.loader is None:
        raise RenderError(f"runtime module {filename!r} is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONTRACT = _runtime_module("fs2_bindcraft_runtime", "bindcraft_runtime_entrypoint.py")
IDENTITY = _runtime_module("fs2_tree_identity", "tree_identity.py")

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


def _volume_source(artifact_id: str, value: Any) -> dict[str, Any]:
    """Read one artifact's own volume source from the accepted handoff.

    The four trees do not share a backing store. The three public ones are
    immutable generations on the reference-data host plane; only the licensed
    PyRosetta tree is on the private academic claim. A renderer that assumed one
    claim for all four could not mount three of them.
    """

    if not isinstance(value, dict):
        raise RenderError(f"{artifact_id}: handoff artifact declares no volume")
    kind = value.get("kind")
    sub_path = value.get("sub_path")
    if not isinstance(sub_path, str) or SUB_PATH.fullmatch(sub_path) is None or ".." in sub_path.split("/"):
        raise RenderError(f"{artifact_id}: handoff sub path is unsafe")
    selector = value.get("node_selector", {})
    if not isinstance(selector, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in selector.items()
    ):
        raise RenderError(f"{artifact_id}: handoff node selector must be a string map")
    if kind == "host-path":
        host_root = value.get("host_root")
        if not isinstance(host_root, str) or not host_root.startswith("/") or ".." in host_root.split("/"):
            raise RenderError(f"{artifact_id}: host-path volume needs a safe absolute host root")
        return {"kind": kind, "path": host_root, "sub_path": sub_path, "node_selector": selector}
    if kind == "persistent-volume-claim":
        claim = value.get("claim")
        if not isinstance(claim, str) or DNS_SUBDOMAIN.fullmatch(claim) is None:
            raise RenderError(f"{artifact_id}: claim volume needs a claim name")
        return {"kind": kind, "claim": claim, "sub_path": sub_path, "node_selector": selector}
    raise RenderError(f"{artifact_id}: unsupported handoff volume kind {kind!r}")


def _volume_key(source: dict[str, Any]) -> str:
    return source["path"] if source["kind"] == "host-path" else "pvc:" + source["claim"]


def load_handoff(
    path: Path, *, direct_live_canonical_pyrosetta: bool = False,
) -> dict[str, Any]:
    """Consume the artifact plane's accepted binding handoff.

    This reads the plane's own document rather than a shape invented here, so a
    generation, identity, mount path or plane decided upstream cannot silently
    disagree with what this run mounts.
    """

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != HANDOFF_SCHEMA:
        raise RenderError("localization binding handoff schema is unsupported")
    published = value.get("evidence", {}).get("generations_published")
    if published is not True and not direct_live_canonical_pyrosetta:
        raise RenderError(
            "the handoff reports its generations as not published; rendering a run against "
            "generations that do not exist would mount empty directories"
        )
    declared = value.get("models", {}).get(MODEL_ID)
    if not isinstance(declared, list) or not declared:
        raise RenderError("localization binding handoff binds no BindCraft artifacts")
    by_artifact = {
        artifact["artifact_id"]: artifact
        for artifact in value.get("artifacts", [])
        if isinstance(artifact, dict) and isinstance(artifact.get("artifact_id"), str)
    }
    role_by_mount = {mount: role for role, mount in MOUNT_PATH_BY_ROLE.items()}

    trees: dict[str, dict[str, Any]] = {}
    selector: dict[str, str] = {}
    for artifact_id in declared:
        artifact = by_artifact.get(artifact_id)
        if artifact is None:
            raise RenderError(f"{artifact_id}: bound to BindCraft but absent from the handoff artifacts")
        consumers = [
            consumer
            for consumer in artifact.get("consumers", [])
            if isinstance(consumer, dict) and consumer.get("model_id") == MODEL_ID
        ]
        if len(consumers) != 1:
            raise RenderError(f"{artifact_id}: expected exactly one BindCraft consumer")
        mount_path = consumers[0].get("mount_path")
        role = role_by_mount.get(mount_path)
        if role is None:
            raise RenderError(f"{artifact_id}: mounts {mount_path!r}, which this image does not read")
        if role in trees:
            raise RenderError(f"{role}: bound by more than one artifact")
        identity = artifact.get("tree_identity", {})
        digest = identity.get("inventory_sha256")
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            raise RenderError(f"{artifact_id}: tree identity is not a lowercase SHA-256")
        expected_algorithm = (
            IDENTITY.TREE_MANIFEST_ALGORITHM
            if role in CONTRACT.NESTED_TREE_ROLES
            else IDENTITY.FLAT_INVENTORY_ALGORITHM
        )
        if identity.get("inventory_algorithm") != expected_algorithm:
            raise RenderError(
                f"{artifact_id}: identity algorithm {identity.get('inventory_algorithm')!r} is not the "
                f"{expected_algorithm!r} this runtime verifies for {role}"
            )
        source = _volume_source(artifact_id, artifact.get("volume"))
        selector.update(source["node_selector"])
        # A generation is per artifact and equals that tree's own identity
        # digest; there is no run-level generation token in the accepted
        # contract, so none is invented here.
        declared_generation = str(artifact.get("generation", ""))
        if declared_generation != digest:
            raise RenderError(
                f"{artifact_id}: published generation does not equal its tree identity"
            )
        generation = declared_generation
        if role == CONTRACT.PYROSETTA_ROLE and direct_live_canonical_pyrosetta:
            # The direct H100 acceptance may start as soon as the public
            # generations have terminal markers.  The private tree predates the
            # localization plane and is already delivered at this canonical
            # claim path.  r18 still reads every byte and holds it to the pinned
            # recursive identity before importing PyRosetta, so a missing
            # private promotion marker does not weaken model admission.  Keep
            # this explicit and qualification-only: the catalog handoff must
            # still publish the private generation before route readiness.
            source = dict(source)
            source["sub_path"] = CANONICAL_PYROSETTA_SUB_PATH
            generation = ""
        trees[role] = {"artifact_id": artifact_id, "sha256": digest, "volume": source,
                       "sub_path": source["sub_path"], "generation": generation}

    missing = sorted(CONTRACT.REQUIRED_TREE_ROLES - set(trees))
    if missing:
        raise RenderError("localization binding handoff is missing roles: " + ", ".join(missing))
    licensed = trees[CONTRACT.PYROSETTA_ROLE]
    if licensed["sha256"] != CONTRACT.PYROSETTA_TREE_MANIFEST_SHA256:
        raise RenderError(
            "handoff PyRosetta tree identity is not the licensed tree this image is built for"
        )
    if licensed["volume"]["kind"] != "persistent-volume-claim":
        raise RenderError(
            "the licensed PyRosetta tree must come from the private academic claim, not a public volume"
        )
    for role, entry in trees.items():
        if role != CONTRACT.PYROSETTA_ROLE and entry["volume"] == licensed["volume"]:
            raise RenderError(f"{role}: a public tree must not be served from the private academic claim")
    groups = value.get("supplemental_groups", [])
    if not isinstance(groups, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) and 0 < item < 2**31 for item in groups
    ):
        raise RenderError("handoff supplemental groups must be positive integers")
    for entry in trees.values():
        groups.append(
            PUBLIC_PLANE_GID if entry["volume"]["kind"] == "host-path" else ACADEMIC_ASSET_GID
        )
    return {
        "node_selector": selector,
        "supplemental_groups": sorted(set(groups)),
        "trees": trees,
    }


def localization_marker(handoff: dict[str, Any]) -> dict[str, Any]:
    """Stand in for the marker the shared controller writes per stage.

    This renderer drives the acceptance run directly rather than through the
    controller, so it writes the same marker the controller would and passes the
    same argv, keeping the run on the interface the controller will use.
    """

    return {
        "schema": "fs2.nebius.ai/runtime-localization-marker/v1",
        "trees": {
            role: {
                "artifact_id": entry["artifact_id"],
                "sub_path": entry["sub_path"],
                "volume_kind": entry["volume"]["kind"],
                "generation": entry["generation"],
            }
            for role, entry in sorted(handoff["trees"].items())
        },
    }


def admission_document(handoff: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": CONTRACT.EXTERNAL_TREE_ADMISSION_SCHEMA,
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


def _tree_volumes(handoff: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One Kubernetes volume per distinct backing store, mounted per role."""

    names: dict[str, str] = {}
    volumes: list[dict[str, Any]] = []
    mounts: list[dict[str, Any]] = []
    for role, entry in sorted(handoff["trees"].items()):
        source = entry["volume"]
        key = _volume_key(source)
        name = names.get(key)
        if name is None:
            name = f"trees-{len(names)}"
            names[key] = name
            if source["kind"] == "host-path":
                volumes.append({"name": name, "hostPath": {"path": source["path"], "type": "Directory"}})
            else:
                volumes.append({
                    "name": name,
                    "persistentVolumeClaim": {"claimName": source["claim"], "readOnly": True},
                })
        mounts.append({
            "name": name,
            "mountPath": MOUNT_PATH_BY_ROLE[role],
            "subPath": entry["sub_path"],
            "readOnly": True,
        })
    return volumes, mounts


def job(args: argparse.Namespace, handoff: dict[str, Any], config_name: str) -> dict[str, Any]:
    digest = args.image.rsplit("@", 1)[1]
    design = args.stage == "design"
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
    tree_volumes, tree_mounts = _tree_volumes(handoff)
    volume_mounts = [
        {"name": "request", "mountPath": "/var/run/fs2", "readOnly": True},
        {"name": "workspace", "mountPath": "/workspace"},
        {"name": "tmp", "mountPath": "/tmp"},
        *tree_mounts,
    ]
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
        # The public trees are hostPath generations on the reference-data
        # filesystem, so both stages must land on a node that carries it; the
        # handoff supplies that selector and the design stage adds the GPU class.
        "nodeSelector": (
            {**handoff["node_selector"], "accelerator.fs2.nebius/class": args.accelerator_class}
            if design
            else dict(handoff["node_selector"])
        ),
        "tolerations": [
            {"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}
        ],
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "runAsGroup": 10001,
            "supplementalGroups": handoff["supplemental_groups"],
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        # One stage per Job. Running the design as an init container of the
        # aggregate's Pod kept the GPU allocated for the whole Pod lifetime,
        # including the CPU-only aggregation, so the accelerator sat idle while
        # content addressing ran. Splitting them hands the GPU back at the end of
        # design; the shard output survives on the durable workspace claim and
        # aggregate re-verifies every artifact against the digest design published.
        "containers": [{
            "name": args.stage,
            "image": args.image,
            "imagePullPolicy": "IfNotPresent",
            "command": _stage_command(args, "run-trajectory" if design else "aggregate"),
            "env": env,
            "resources": {
                "requests": {"cpu": "16", "memory": "96Gi", "nvidia.com/gpu": 1},
                "limits": {"cpu": "24", "memory": "128Gi", "nvidia.com/gpu": 1},
            } if design else {
                "requests": {"cpu": "2", "memory": "8Gi"},
                "limits": {"cpu": "4", "memory": "16Gi"},
            },
            "securityContext": security,
            "volumeMounts": volume_mounts,
        }],
        "volumes": [
            {"name": "request", "configMap": {"name": config_name, "defaultMode": 0o444}},
            # Durable, so the design stage's output outlives its Pod and the
            # aggregate Job in a later Pod can read and re-verify it.
            {"name": "workspace", "persistentVolumeClaim": {"claimName": args.workspace_claim}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "32Gi"}},
            *tree_volumes,
        ],
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"{args.job_name}-{args.stage}",
            "namespace": args.namespace,
            "labels": {**labels, "fs2.nebius.ai/stage": args.stage},
        },
        "spec": {
            "suspend": bool(args.local_queue),
            "backoffLimit": 0,
            "activeDeadlineSeconds": args.deadline_seconds,
            "ttlSecondsAfterFinished": 86_400,
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }


def render(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    handoff = load_handoff(
        Path(args.handoff),
        direct_live_canonical_pyrosetta=args.direct_live_canonical_pyrosetta,
    )
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
    root.add_argument(
        "--direct-live-canonical-pyrosetta",
        action="store_true",
        help=(
            "qualification only: mount the fully content-verified canonical private PyRosetta "
            "tree while keeping all public trees on immutable generations; this does not make "
            "the catalog handoff route-ready"
        ),
    )
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
    root.add_argument(
        "--stage",
        required=True,
        choices=("design", "aggregate"),
        help="one stage per Job; render and apply design, wait for it, then render aggregate",
    )
    root.add_argument(
        "--workspace-claim",
        required=True,
        help="durable claim carrying the shard output from the design Job to the aggregate Job",
    )
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

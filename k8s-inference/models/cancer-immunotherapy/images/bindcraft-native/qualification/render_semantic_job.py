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
tree sits on the private academic claim. The public handoff supplies each public
generation's volume, subPath, identity and storage selector. The canonical
localization and academic-assets contracts separately supply the private tree's
external-installed-tree identity and PVC source; no private generation is
inferred from the public handoff. Consumer paths come from the executable
adapter because they are part of its image interface.

One stage per Job. The design stage needs a GPU and the aggregation does not, so
running them in one Pod would hold the accelerator through the CPU-only half.
They are separate Jobs over a durable workspace claim, and the aggregate
re-verifies every handed-off artifact against the digest the design stage
published.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOLUTION_ROOT = ROOT.parents[3]
CONTROL_PLANE_SRC = SOLUTION_ROOT / "components" / "control-plane" / "src"
if str(CONTROL_PLANE_SRC) not in sys.path:
    sys.path.insert(0, str(CONTROL_PLANE_SRC))

# Qualification must exercise the executable adapter that production uses. Do
# not carry a second request translator or argv builder in this image package.
from fs2_serve.scientific_batch.adapters import bindcraft as ADAPTER  # noqa: E402

HANDOFF_SCHEMA = "fs2-serve.nebius.ai/scientific-localization-binding-handoff/v1"
MODEL_ID = "bindcraft"
LOCALIZATION_CONTRACT = (
    SOLUTION_ROOT / "catalog" / "runtime" / "contracts" / "scientific-artifact-localization.json"
)
ACADEMIC_ASSETS_CONTRACT = SOLUTION_ROOT / "academic-assets" / "contracts" / "academic-assets.json"

# These are aliases, not copies: the integrated executable adapter is the one
# source of request translation, argv, settings and filter identities.
SETTINGS_TEMPLATE = ADAPTER.SETTINGS_TEMPLATE
SETTINGS_SHA256 = ADAPTER.SETTINGS_SHA256
FILTERS = ADAPTER.FILTERS
FILTERS_SHA256 = ADAPTER.FILTERS_SHA256

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
# An arbitrary non-owner identity proves access comes from the published
# supplemental-group contract. In particular, never use primary gid 10001:
# that would mask a recurrence of the historical fsGroup ownership damage.
QUALIFICATION_UID = 12345
QUALIFICATION_PRIMARY_GID = 12345
CANONICAL_PYROSETTA_SUB_PATH = "pyrosetta-bindcraft/site-packages"
PYROSETTA_PROBE_ROOT = "/runtime/pyrosetta-bindcraft/site-packages"
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
    role: values[1] for role, values in ADAPTER.EXTERNAL_TREE_ROLES.items()
}
if set(MOUNT_PATH_BY_ROLE) != CONTRACT.REQUIRED_TREE_ROLES:
    raise RuntimeError("the integrated BindCraft adapter and r18 image disagree on external roles")
ADAPTER_WORKSPACE_ROOT = str(
    Path(ADAPTER.run_workspace(MODEL_ID, "qualification", "design-000")).parents[3]
)


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


def _external_pyrosetta_contract(path: Path) -> dict[str, Any]:
    """Read the academic tree from the canonical localization contract.

    This contract, not the historical promotion handoff, owns the fact that
    PyRosetta is already installed by another plane and remains at its source
    sub-path.  In particular it owns no generation or in-generation marker.
    """

    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema")
        != "fs2-serve.nebius.ai/scientific-artifact-localization/v1"
    ):
        raise RenderError("scientific artifact localization contract schema is unsupported")
    artifacts = value.get("artifacts", []) if isinstance(value, dict) else []
    matches = [
        item for item in artifacts
        if isinstance(item, dict) and item.get("artifact_id") == ADAPTER.PYROSETTA_ARTIFACT
    ]
    if len(matches) != 1:
        raise RenderError("localization contract has no unique BindCraft PyRosetta tree")
    artifact = matches[0]
    tree = artifact.get("tree")
    consumers = artifact.get("consumers")
    if (
        artifact.get("transform") != "external-installed-tree"
        or artifact.get("visibility") != "tenant-private"
        or artifact.get("source_sub_path") != CANONICAL_PYROSETTA_SUB_PATH
        or not isinstance(tree, dict)
        or tree.get("inventory_algorithm") != IDENTITY.TREE_MANIFEST_ALGORITHM
        or tree.get("inventory_sha256") != CONTRACT.PYROSETTA_TREE_MANIFEST_SHA256
        or tree.get("entry_count") != ADAPTER.PYROSETTA_ENTRY_COUNT
        or tree.get("directory_count") != 779
        or tree.get("symlink_count") != 0
        or tree.get("total_bytes") != 3_287_122_494
        or tree.get("complete_entry_digests") is not True
        or not isinstance(consumers, list)
        or len(consumers) != 1
        or consumers[0].get("model_id") != MODEL_ID
        or consumers[0].get("mount_path") != ADAPTER.PYROSETTA_PATH
    ):
        raise RenderError("BindCraft PyRosetta external-installed-tree contract drifted")
    return artifact


def _external_pyrosetta_source(path: Path, contract: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema") != "fs2-serve.nebius.ai/academic-assets/v3"
    ):
        raise RenderError("academic assets contract schema is unsupported")
    source = value.get("runtime_cache") if isinstance(value, dict) else None
    if (
        not isinstance(source, dict)
        or source.get("runtime_mount_allowed") is not True
        or source.get("general_shared_cache") is not False
    ):
        raise RenderError("BindCraft PyRosetta academic runtime cache is not privately mountable")
    claim = source.get("pvc_name")
    namespace = source.get("pvc_namespace")
    if (
        not isinstance(claim, str)
        or DNS_SUBDOMAIN.fullmatch(claim) is None
        or not isinstance(namespace, str)
        or DNS_SUBDOMAIN.fullmatch(namespace) is None
    ):
        raise RenderError("BindCraft PyRosetta academic source has no valid claim")
    return {
        "kind": "persistent-volume-claim",
        "claim": claim,
        "namespace": namespace,
        "sub_path": contract["source_sub_path"],
        "node_selector": {},
    }


def load_handoff(
    path: Path,
    *,
    localization_contract: Path = LOCALIZATION_CONTRACT,
    academic_assets_contract: Path = ACADEMIC_ASSETS_CONTRACT,
) -> dict[str, Any]:
    """Consume the artifact plane's accepted binding handoff.

    This reads the plane's own document rather than a shape invented here, so a
    generation, identity, mount path or plane decided upstream cannot silently
    disagree with what this run mounts.
    """

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != HANDOFF_SCHEMA:
        raise RenderError("localization binding handoff schema is unsupported")
    published = value.get("evidence", {}).get("public_generations_published")
    if published is not True:
        raise RenderError(
            "the handoff reports its public generations as not published; rendering a run "
            "against generations that do not exist would mount empty directories"
        )
    external_pyrosetta = _external_pyrosetta_contract(localization_contract)
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
        externally_installed = role == CONTRACT.PYROSETTA_ROLE
        if externally_installed:
            if artifact.get("externally_installed") is not True:
                raise RenderError("BindCraft PyRosetta handoff lost its external-installed-tree identity")
            if digest != external_pyrosetta["tree"]["inventory_sha256"]:
                raise RenderError("BindCraft PyRosetta handoff identity differs from its canonical contract")
            source = _external_pyrosetta_source(academic_assets_contract, external_pyrosetta)
        else:
            source = _volume_source(artifact_id, artifact.get("volume"))
        selector.update(source["node_selector"])
        generation: str | None = None
        if not externally_installed:
            declared_generation = artifact.get("generation")
            if declared_generation != digest:
                raise RenderError(f"{artifact_id}: published generation does not equal its tree identity")
            generation = digest
        trees[role] = {
            "artifact_id": artifact_id,
            "sha256": digest,
            "volume": source,
            "sub_path": source["sub_path"],
            "generation": generation,
            "externally_installed": externally_installed,
            "tree_contract": external_pyrosetta["tree"] if externally_installed else None,
        }

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
        if role != CONTRACT.PYROSETTA_ROLE and entry["volume"]["kind"] != "host-path":
            raise RenderError(f"{role}: a public tree must not be served from the private academic claim")
    groups = value.get("supplemental_groups") or []
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
        "private_namespace": licensed["volume"]["namespace"],
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
            if not entry["externally_installed"]
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


def public_request(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": "design-binder",
        "service_class": args.service_class,
        "input_manifest": {
            "artifact_id": args.target_artifact_id,
            "sha256": args.target_sha256,
            "size_bytes": args.target_size_bytes,
            "media_type": "chemical/x-pdb",
            "compression": "none",
        },
        "parameters": {
            "target": {"chain": args.target_chain, "hotspot_residues": args.hotspot},
            "binder_length": {
                "minimum": args.binder_length_minimum,
                "maximum": args.binder_length_maximum,
            },
            "designs": args.designs,
            "mpnn_lane": args.mpnn_lane,
            "seed": args.seed,
        },
        "client_context": {"batch_id": args.batch_id, "display_name": args.display_name},
    }


REQUEST_FILENAMES = (
    "request.json",
    "input-manifest.json",
    "external-trees.json",
    "runtime-localization.json",
)


def _adapter_inputs(args: argparse.Namespace) -> tuple[Any, Any, dict[str, Any], dict[str, Any]]:
    """Translate through the executable adapter, once, for the qualification shard."""

    try:
        request_value, parameters = ADAPTER._request(public_request(args))  # noqa: SLF001
    except ADAPTER.ScientificAdapterError as exc:
        raise RenderError(str(exc)) from exc
    if parameters.shard_count != 1:
        raise RenderError("direct H100 qualification renders exactly one adapter shard")
    native_manifest = ADAPTER.native_input_manifest(request_value)
    native_request = ADAPTER.native_request(request_value, parameters, shard_index=0)
    return request_value, parameters, native_request, native_manifest


def _stage_workspace(args: argparse.Namespace, stage: str) -> str:
    shard_id = "design-000" if stage == "design" else "aggregate"
    return ADAPTER.run_workspace(MODEL_ID, args.run_id, shard_id)


def _adapter_invocation(args: argparse.Namespace, stage: str) -> tuple[list[str], dict[str, str]]:
    request_value, parameters, _native_request, _native_manifest = _adapter_inputs(args)
    workspace = _stage_workspace(args, stage)
    if stage == "design":
        argv = ADAPTER._design_argv(workspace, 0, parameters.seed(0))  # noqa: SLF001
        environment = ADAPTER._environment(  # noqa: SLF001
            request_value,
            parameters,
            parameters.accepted_designs(0),
            shard_index=0,
            workspace=workspace,
            needs_target=True,
            collector_id=ADAPTER.DESIGN_COLLECTOR_ID,
        )
    else:
        argv = ADAPTER._aggregate_argv(workspace, 1)  # noqa: SLF001
        environment = ADAPTER._environment(  # noqa: SLF001
            request_value,
            parameters,
            parameters.designs,
            shard_index=0,
            workspace=workspace,
            needs_target=False,
            collector_id=ADAPTER.AGGREGATE_COLLECTOR_ID,
        )
    return list(argv), dict(environment)


def _materialize_request_script(args: argparse.Namespace, stage: str) -> str:
    workspace = _stage_workspace(args, stage)
    design_workspace = _stage_workspace(args, "design")
    lines = [
        "from pathlib import Path",
        "import shutil",
        "source = Path('/var/run/fs2-source')",
        f"workspace = Path({workspace!r})",
        "metadata = workspace / '.fs2'",
        "metadata.mkdir(parents=True, exist_ok=True)",
        f"names = {REQUEST_FILENAMES!r}",
        "for name in names:",
        "    target = metadata / name",
        "    target.write_bytes((source / name).read_bytes())",
        "    target.chmod(0o444)",
    ]
    if stage == "design":
        lines.extend([
            "inputs = workspace / 'inputs'",
            "inputs.mkdir(parents=True, exist_ok=True)",
            f"shutil.copyfile({DEFAULT_TARGET_PDB!r}, inputs / 'target_structure.pdb')",
            "(inputs / 'target_structure.pdb').chmod(0o444)",
        ])
    else:
        lines.extend([
            "shards = workspace / 'shards'",
            "shards.mkdir(parents=True, exist_ok=True)",
            f"source_shard = Path({(design_workspace + '/output')!r})",
            "destination = shards / '000'",
            "if destination.exists():",
            "    shutil.rmtree(destination)",
            "shutil.copytree(source_shard, destination)",
        ])
    return "\n".join(lines) + "\n"


def _tree_volumes(
    handoff: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """One Kubernetes volume per distinct backing store, mounted per role."""

    names: dict[str, str] = {}
    names_by_role: dict[str, str] = {}
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
        names_by_role[role] = name
        mounts.append({
            "name": name,
            "mountPath": MOUNT_PATH_BY_ROLE[role],
            "subPath": entry["sub_path"],
            "readOnly": True,
        })
    return volumes, mounts, names_by_role


def _private_tree_probe_script(handoff: dict[str, Any]) -> str:
    """Verify the installed tree through the owning PVC's `/runtime` view."""

    entry = handoff["trees"][CONTRACT.PYROSETTA_ROLE]
    tree = entry["tree_contract"]
    return f"""\
import importlib.util
import json
import os
from pathlib import Path

root = Path({PYROSETTA_PROBE_ROOT!r})
if not root.is_dir() or root.is_symlink() or not os.access(root, os.R_OK | os.X_OK):
    raise SystemExit('private PyRosetta installed tree is absent or unreadable')
spec = importlib.util.spec_from_file_location('fs2_tree_identity', '/opt/fs2/bindcraft/tree_identity.py')
if spec is None or spec.loader is None:
    raise SystemExit('r18 tree identity verifier is unavailable')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
receipt = module.verify_tree(
    root,
    artifact_id={entry['artifact_id']!r},
    expected_tree_manifest_sha256={entry['sha256']!r},
)
directory_count = sum(1 for path in root.rglob('*') if path.is_dir() and not path.is_symlink())
expected = {{
    'tree_manifest_algorithm': {tree['inventory_algorithm']!r},
    'tree_manifest_sha256': {tree['inventory_sha256']!r},
    'file_count': {tree['entry_count']!r},
    'symlink_count': {tree['symlink_count']!r},
    'total_bytes': {tree['total_bytes']!r},
}}
for key, value in expected.items():
    if receipt.get(key) != value:
        raise SystemExit(f'private PyRosetta {{key}} differs from the external-installed-tree contract')
if directory_count != {tree['directory_count']!r}:
    raise SystemExit('private PyRosetta directory_count differs from the external-installed-tree contract')
print(json.dumps({{
    'schema': 'fs2.nebius.ai/bindcraft-private-tree-probe/v1',
    'root': str(root),
    'readable': True,
    'directory_count': directory_count,
    **expected,
}}, sort_keys=True), flush=True)
"""


def job(args: argparse.Namespace, handoff: dict[str, Any], config_name: str) -> dict[str, Any]:
    digest = args.image.rsplit("@", 1)[1]
    design = args.stage == "design"
    private_probe = args.stage == "private-tree-probe"
    labels = {
        "app.kubernetes.io/name": "fs2-batch",
        "app.kubernetes.io/part-of": "fs2-serve",
        "app.kubernetes.io/managed-by": "fs2-cancer-runtime-semantic-acceptance",
        "fs2.nebius.ai/model-id": "bindcraft",
        "fs2.nebius.ai/task": args.task_id,
        "fs2.nebius.ai/service-class": args.service_class,
    }
    tree_volumes, tree_mounts, tree_volume_names = _tree_volumes(handoff)
    if private_probe:
        private_volume_name = tree_volume_names[CONTRACT.PYROSETTA_ROLE]
        private_volume = next(item for item in tree_volumes if item["name"] == private_volume_name)
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": f"{args.job_name}-{args.stage}",
                "namespace": args.namespace,
                "labels": {**labels, "fs2.nebius.ai/stage": args.stage},
            },
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": args.deadline_seconds,
                "ttlSecondsAfterFinished": 86_400,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "enableServiceLinks": False,
                        "nodeSelector": {
                            **handoff["node_selector"],
                            "accelerator.fs2.nebius/class": args.accelerator_class,
                        },
                        "tolerations": [{
                            "key": "dedicated",
                            "operator": "Equal",
                            "value": "fs2-inference",
                            "effect": "NoSchedule",
                        }],
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": QUALIFICATION_UID,
                            "runAsGroup": QUALIFICATION_PRIMARY_GID,
                            "supplementalGroups": handoff["supplemental_groups"],
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [{
                            "name": "verify-private-pyrosetta",
                            "image": args.image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["python", "-c", _private_tree_probe_script(handoff)],
                            "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                            "resources": {
                                "requests": {"cpu": "1", "memory": "1Gi"},
                                "limits": {"cpu": "4", "memory": "4Gi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [{
                                "name": private_volume_name,
                                "mountPath": "/runtime",
                                "readOnly": True,
                            }],
                        }],
                        "volumes": [private_volume],
                    },
                },
            },
        }
    command, adapter_environment = _adapter_invocation(args, args.stage)
    workspace = _stage_workspace(args, args.stage)
    marker_path = f"{workspace}/.fs2/runtime-localization.json"
    volume_mounts = [
        {"name": "workspace", "mountPath": ADAPTER_WORKSPACE_ROOT},
        {"name": "tmp", "mountPath": "/tmp"},
        *tree_mounts,
    ]
    # Both stages pass the shared outer entrypoint, which verifies the AlphaFold2
    # manifest and binds PyRosetta, so both need the same trees and environment.
    adapter_environment.update({
        "FS2_RUNTIME_IMAGE_DIGEST": digest,
        "FS2_RUNTIME_LOCALIZATION_MARKER": marker_path,
        "FS2_ARTIFACT_MANIFEST": MOUNT_PATH_BY_ROLE[CONTRACT.AF2_PARAMS_ROLE] + "/manifest.json",
        "FS2_ARTIFACT_KIND": "bindcraft-af2-params",
        "XLA_PYTHON_CLIENT_PREALLOCATE": "false",
    })
    env = [{"name": name, "value": value} for name, value in sorted(adapter_environment.items())]
    security = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    workspace_prepare_script = (
        "from pathlib import Path\n\n"
        f"run = Path({workspace!r})\n"
        "run.mkdir(parents=True, exist_ok=True)\n"
        "run.chmod(0o777)\n"
    )
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
            "runAsUser": QUALIFICATION_UID,
            "runAsGroup": QUALIFICATION_PRIMARY_GID,
            "supplementalGroups": handoff["supplemental_groups"],
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        # ConfigMap key projections are symlinks.  The runtime rejects a
        # symlinked localization marker or external-tree admission document so
        # an attacker cannot redirect either after admission. Materialize the
        # four bounded control files into the exact adapter workspace first;
        # the model process then reads regular, read-only files without
        # weakening that gate.
        "initContainers": [
            {
                # The mounted-filesystem provisioner creates a root-owned 0755
                # volume and squashes chown. Prepare only this task's run
                # directory as root and chmod that directory; never chown and
                # never set fsGroup on the Pod, which would also mutate the
                # academic model claim.
                "name": "prepare-workspace",
                "image": args.image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["python", "-c", workspace_prepare_script],
                "resources": {
                    "requests": {"cpu": "50m", "memory": "64Mi"},
                    "limits": {"cpu": "1", "memory": "256Mi"},
                },
                "securityContext": {
                    **security, "runAsNonRoot": False, "runAsUser": 0, "runAsGroup": 0,
                },
                "volumeMounts": [{"name": "workspace", "mountPath": ADAPTER_WORKSPACE_ROOT}],
            },
            {
                "name": "verify-private-pyrosetta",
                "image": args.image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["python", "-c", _private_tree_probe_script(handoff)],
                "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                "resources": {
                    "requests": {"cpu": "1", "memory": "1Gi"},
                    "limits": {"cpu": "4", "memory": "4Gi"},
                },
                "securityContext": security,
                # Mount the owning academic claim at its canonical root. The
                # probe therefore verifies /runtime/pyrosetta-bindcraft/site-packages,
                # exactly as the academic-assets contract specifies, without a
                # generated subPath or marker.
                "volumeMounts": [{
                    "name": tree_volume_names[CONTRACT.PYROSETTA_ROLE],
                    "mountPath": "/runtime",
                    "readOnly": True,
                }],
            },
            {
                "name": "materialize-request",
                "image": args.image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["python", "-c", _materialize_request_script(args, args.stage)],
                "resources": {
                    "requests": {"cpu": "50m", "memory": "64Mi"},
                    "limits": {"cpu": "1", "memory": "256Mi"},
                },
                "securityContext": security,
                "volumeMounts": [
                    {"name": "request-source", "mountPath": "/var/run/fs2-source", "readOnly": True},
                    {"name": "workspace", "mountPath": ADAPTER_WORKSPACE_ROOT},
                ],
            },
        ],
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
            "command": command,
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
            {"name": "request-source", "configMap": {"name": config_name, "defaultMode": 0o444}},
            # Durable, so the design stage's output outlives its Pod and the
            # aggregate Job in a later Pod can read and re-verify it.
            {"name": "workspace", "persistentVolumeClaim": {"claimName": args.workspace_claim}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "32Gi"}},
            *tree_volumes,
        ],
    }
    if not design:
        # The H100 design-stage probe verifies the owning PVC's /runtime view.
        # Aggregation still passes through r18's outer gate, which re-verifies
        # the consumer mount, but need not scan the same 3.29 GB a third time.
        pod_spec["initContainers"] = [
            item
            for item in pod_spec["initContainers"]
            if item["name"] != "verify-private-pyrosetta"
        ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"{args.job_name}-{args.stage}",
            "namespace": args.namespace,
            "labels": {**labels, "fs2.nebius.ai/stage": args.stage},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": args.deadline_seconds,
            "ttlSecondsAfterFinished": 86_400,
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }


def render(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    handoff = load_handoff(
        Path(args.handoff),
        localization_contract=Path(args.localization_contract),
        academic_assets_contract=Path(args.academic_assets_contract),
    )
    if args.namespace != handoff["private_namespace"]:
        raise RenderError("qualification namespace cannot mount the academic PyRosetta claim")
    _request_value, _parameters, native_request, native_manifest = _adapter_inputs(args)
    documents = {
        "request.json": ADAPTER._canonical_bytes(native_request).decode("ascii"),  # noqa: SLF001
        "input-manifest.json": ADAPTER._canonical_bytes(native_manifest).decode("ascii"),  # noqa: SLF001
        "external-trees.json": canonical(admission_document(handoff)),
        "runtime-localization.json": canonical(localization_marker(handoff)),
    }
    config_name = f"fs2-run-{args.run_id}"
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": config_name,
            "namespace": args.namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "fs2-cancer-runtime-semantic-acceptance",
                "fs2.nebius.ai/model-id": MODEL_ID,
                "fs2.nebius.ai/task": args.task_id,
            },
        },
        "data": documents,
    }
    return config_map, job(args, handoff, config_name)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--handoff", required=True, help="artifact-plane four-tree handoff JSON")
    root.add_argument(
        "--localization-contract",
        default=str(LOCALIZATION_CONTRACT),
        help="canonical scientific artifact localization contract",
    )
    root.add_argument(
        "--academic-assets-contract",
        default=str(ACADEMIC_ASSETS_CONTRACT),
        help="academic-assets runtime PVC contract",
    )
    root.add_argument("--image", required=True, help="runtime image, pinned by digest")
    root.add_argument("--run-id", required=True)
    root.add_argument("--job-name", required=True)
    root.add_argument("--namespace", default="fs2-academic-poc")
    root.add_argument("--task-id", default="fs2-bindcraft-h100-codex-successor-r20260903")
    root.add_argument("--accelerator-class", default="nvidia-h100-sxm5-80gb")
    root.add_argument("--service-class", default="customer-batch")
    root.add_argument("--seed", type=int, default=384856)
    root.add_argument("--designs", type=int, default=1)
    root.add_argument("--binder-length-minimum", type=int, default=60)
    root.add_argument("--binder-length-maximum", type=int, default=75)
    root.add_argument("--target-chain", default="A")
    root.add_argument("--mpnn-lane", choices=("vanilla", "soluble"), default="vanilla")
    root.add_argument("--hotspot", type=int, action="append", default=None)
    root.add_argument("--target-sha256", default=DEFAULT_TARGET_SHA256)
    root.add_argument("--target-size-bytes", type=int, default=DEFAULT_TARGET_BYTES)
    root.add_argument("--target-artifact-id", default="artifact.bindcraft.target.pdl1")
    root.add_argument("--batch-id", default="batch.bindcraft.native.production")
    root.add_argument("--display-name", default="native PD-L1 production acceptance")
    root.add_argument(
        "--stage",
        required=True,
        choices=("private-tree-probe", "design", "aggregate"),
        help="render the private contract probe or one adapter-backed execution stage",
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

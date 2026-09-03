#!/usr/bin/env python3
"""Render the regional staging and on-node verification workloads.

Two workloads come out of one contract:

``stage``
    A CPU Job that downloads each declared archive, proves its digest, expands
    it into a per-artifact directory on a shared regional volume, and writes a
    localization receipt beside it. The archive is never left in the tree.

``qualify``
    A GPU Job that mounts those trees read-only at the exact paths the runtime
    contract names, re-verifies each one on the node it will actually run on,
    and then makes the real model runtime read them.

Nothing here hardcodes a project, region, registry, cluster, storage class, or
GPU pool: every one of those is an argument, and the artifact identities come
from the checked-in localization contract.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPO_ROOT / "components/control-plane/src/fs2_serve/scientific_batch/adapters"
DEFAULT_CONTRACT = REPO_ROOT / "catalog/runtime/contracts/scientific-artifact-localization.json"


def _localization() -> Any:
    """Load the very module the staged workloads run.

    The handoff publishes the marker digest a consumer will pin, so it must be
    produced by the same code that writes the marker into the generation. A
    second implementation here could drift, and the drift would only show up as
    a failed admission on a node.
    """

    spec = importlib.util.spec_from_file_location("fs2_localization_render", PACKAGE_ROOT / "localization.py")
    assert spec is not None and spec.loader is not None
    # The module uses a relative import, so it has to load as a package for its
    # own spec and __package__ to agree.
    spec.submodule_search_locations = []
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("fs2_localization_render", module)
    primitives = importlib.util.spec_from_file_location("fs2_localization_render_primitives", PACKAGE_ROOT / "primitives.py")
    assert primitives is not None and primitives.loader is not None
    primitives_module = importlib.util.module_from_spec(primitives)
    primitives.loader.exec_module(primitives_module)
    sys.modules["fs2_localization_render.primitives"] = primitives_module
    module.__package__ = "fs2_localization_render"
    spec.loader.exec_module(module)
    return module

# The verifier is mounted as a tiny package so the staging and qualification
# workloads run the same code the control plane runs, not a copy of it.
PACKAGE_NAME = "fs2_localization"
PACKAGE_MOUNT = "/opt/fs2-localization"
CONTRACT_MOUNT = f"{PACKAGE_MOUNT}/{PACKAGE_NAME}/localization-contract.json"
TREE_ROOT = "/trees"
GENERATIONS_DIR = "generations"
DIGEST_DIR = "sha256"
# The marker is written inside the generation and travels with it, so there is
# exactly one authority for what a mount contains and no separate path to mount.
RUNTIME_MARKER_NAME = ".fs2-runtime-tree.json"
RECEIPTS_DIR = ".receipts"
# A qualification pod reads the shared volume and must not write to it: it runs
# as the runtime image's own account, which is a guest in the claim's group, and
# its receipt is evidence about one node rather than shared artifact state.
QUALIFY_RECEIPT_DIR = "/scratch"

LABEL_PREFIX = "fs2-serve.nebius.ai"

# Staging never needs root. The account is an argument because the right one is
# a property of the volume, not of this tool: a claim shared with other tenants'
# assets is group-owned and setgid, and writing to it means joining that group.
DEFAULT_RUNTIME_UID = 10001
# The public model-artifact host root is owned by 1000:1000. The academic claim
# is a different volume with a different owner, and using its 65532 here would
# write a tree the public plane's own account cannot read.
PUBLIC_PLANE_UID = 1000
# One specific claim, whose ownership is a property of that volume rather than a
# rule about claims in general. Its root is setgid and group-writable by 65532,
# so a writer joins that group and must not set fsGroup: Kubernetes applies
# fsGroup to the whole volume rather than the sub-path a pod mounts, and this one
# also holds PyRosetta and AlphaFold 3. Any other claim gets no implicit
# ownership from this tool, because guessing wrong on a customer volume either
# fails to write or rewrites somebody else's files.
ACADEMIC_NAMESPACE = "fs2-academic-poc"
ACADEMIC_CLAIM = "academic-assets-runtime-rwx"
ACADEMIC_ASSET_GID = 65532
# Only nodes carrying this label mount the public host root, so a Job that omits
# it can be scheduled somewhere the directory simply is not there.
REFERENCE_DATA_NODE_LABEL = "storage.fs2.nebius/reference-data"
# The only generation state a renderer can establish from a contract alone.
RENDERED_BINDING_STATE = "rendered"


def pod_security_context(*, uid: int, gid: int, supplemental: tuple[int, ...], fs_group: int | None) -> dict[str, Any]:
    """Build the pod security context for one volume's ownership model.

    ``fs_group`` is deliberately optional and defaults to unset. Kubernetes
    applies fsGroup ownership to the whole volume, not just the sub-path a pod
    mounts, so setting it on a claim that also holds another tenant's assets
    would recursively rewrite their ownership. On a setgid group-writable claim
    the correct answer is to join the group instead.
    """

    context: dict[str, Any] = {
        "runAsUser": uid,
        "runAsGroup": gid,
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    if supplemental:
        context["supplementalGroups"] = list(supplemental)
    if fs_group is not None:
        context["fsGroup"] = fs_group
        # A molecule dictionary is tens of thousands of files; recursively
        # chowning it on every mount costs minutes for no benefit once the
        # volume root already agrees.
        context["fsGroupChangePolicy"] = "OnRootMismatch"
    return context


def load_contract(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != "fs2-serve.nebius.ai/scientific-artifact-localization/v1":
        raise SystemExit(f"{path} is not a scientific artifact localization contract")
    return document


def selected_artifacts(document: dict[str, Any], artifact_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    by_id = {item["artifact_id"]: item for item in document["artifacts"]}
    missing = sorted(set(artifact_ids) - set(by_id))
    if missing:
        raise SystemExit(f"contract does not declare {missing}")
    return [by_id[artifact_id] for artifact_id in artifact_ids]


def artifact_root(tree_prefix: str, artifact_id: str) -> str:
    """The per-artifact root a generation is published under."""

    return "/".join(part for part in (tree_prefix, GENERATIONS_DIR, artifact_id) if part)


def generation_sub_path(tree_prefix: str, artifact_id: str, generation: str) -> str:
    """Where one immutable generation lives inside the claim.

    ``<prefix>/generations/<artifact_id>/sha256/<tree digest>``. The digest is
    part of the path, so a different tree is a different mount and an existing
    generation is never rewritten in place; the algorithm is a path segment so a
    future digest lands beside this one rather than over it.
    """

    return f"{artifact_root(tree_prefix, artifact_id)}/{DIGEST_DIR}/{generation}"


def labels(run_id: str, role: str) -> dict[str, str]:
    return {
        f"{LABEL_PREFIX}/component": "artifact-localization",
        f"{LABEL_PREFIX}/run-id": run_id,
        f"{LABEL_PREFIX}/role": role,
    }


def verifier_config_map(
    name: str,
    namespace: str,
    run_id: str,
    contract: Path,
    probe_files: tuple[Path, ...] = (),
) -> dict[str, Any]:
    data = {
        "__init__.py": '"""Localization verifier delivered to the cluster."""\n',
        "primitives.py": (PACKAGE_ROOT / "primitives.py").read_text(encoding="utf-8"),
        "localization.py": (PACKAGE_ROOT / "localization.py").read_text(encoding="utf-8"),
        "localization-contract.json": contract.read_text(encoding="utf-8"),
    }
    for probe in probe_files:
        if probe.name in data:
            raise SystemExit(f"probe {probe.name} would shadow a verifier module")
        data[probe.name] = probe.read_text(encoding="utf-8")
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace, "labels": labels(run_id, "verifier")},
        "immutable": True,
        "data": data,
    }


def tree_claim(name: str, namespace: str, run_id: str, storage_class: str, size: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": namespace, "labels": labels(run_id, "tree-store")},
        "spec": {
            "accessModes": ["ReadWriteMany"],
            "storageClassName": storage_class,
            "resources": {"requests": {"storage": size}},
        },
    }


def tree_volume(plane: dict[str, Any], name: str = "trees") -> dict[str, Any]:
    """The Kubernetes volume for whichever plane holds these generations.

    The public model-artifact plane is a Terraform-managed host directory that
    every labelled node mounts, so it is a hostPath and a node selector; the
    licensed plane is a namespaced claim. Rendering one as the other produces a
    Job that cannot bind.
    """

    if plane["kind"] == "host-path":
        return {"name": name, "hostPath": {"path": plane["host_root"], "type": "Directory"}}
    if plane["kind"] == "persistent-volume-claim":
        return {"name": name, "persistentVolumeClaim": {"claimName": plane["claim"]}}
    raise SystemExit(f"unsupported storage plane kind {plane['kind']!r}")


def plane_localizer_arguments(plane: dict[str, Any], namespace: str) -> list[str]:
    """Tell the localizer which plane it is publishing onto, exactly."""

    if plane["kind"] == "host-path":
        return ["--volume-kind", "host-path", "--host-root", plane["host_root"]]
    return ["--volume-kind", "persistent-volume-claim", "--namespace", namespace, "--claim", plane["claim"]]


def _verifier_volumes(
    config_map: str,
    plane: dict[str, Any],
    tree_prefix: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Mount the verifier and the tree store.

    ``tree_prefix`` keeps these trees inside their own subtree of a volume that
    already holds other assets, so a shared volume never becomes a shared
    namespace. It is carried in the paths this tool writes, not as a ``subPath``:
    a subPath that does not exist yet cannot be mounted, and on the very first
    run of a new prefix it never does, so mounting one would deadlock the run
    that was supposed to create it. The owning root is mounted instead and every
    path below is prefixed, which also keeps a hostPath plane — where nothing
    creates a missing subPath for us — working the same way as a claim.
    """

    volumes = [
        {"name": "verifier", "configMap": {"name": config_map}},
        tree_volume(plane),
        {"name": "scratch", "emptyDir": {}},
    ]
    mounts = [
        {"name": "verifier", "mountPath": f"{PACKAGE_MOUNT}/{PACKAGE_NAME}", "readOnly": True},
        {"name": "trees", "mountPath": TREE_ROOT},
        {"name": "scratch", "mountPath": "/scratch"},
    ]
    return volumes, mounts


def tree_path(tree_prefix: str, *parts: str) -> str:
    """An in-container path under the mounted volume root."""

    return "/".join([TREE_ROOT, *(part for part in (tree_prefix, *parts) if part)])


def stage_job(
    *,
    name: str,
    namespace: str,
    run_id: str,
    image: str,
    python: str,
    config_map: str,
    plane: dict[str, Any],
    artifacts: list[dict[str, Any]],
    node_selector: dict[str, str],
    tolerations: list[dict[str, Any]],
    resources: dict[str, Any],
    security_context: dict[str, Any],
    tree_prefix: str = "",
) -> dict[str, Any]:
    volumes, mounts = _verifier_volumes(config_map, plane, tree_prefix)
    receipts = tree_path(tree_prefix, RECEIPTS_DIR)
    steps: list[dict[str, Any]] = []
    # Two artifacts can share one upstream archive; fetch it once and let the
    # later step localize from the copy already on disk.
    fetched: dict[str, str] = {}
    for artifact in artifacts:
        artifact_id = artifact["artifact_id"]
        if artifact["transform"] == "external-installed-tree":
            # Another plane installed and owns these bytes. Staging them here
            # would duplicate a licensed tree and move the identity its owner
            # published, so this tool only ever verifies it in place.
            raise SystemExit(f"{artifact_id} is an externally installed tree and must not be staged")
        digest = artifact["archive"]["sha256"]
        scratch = f"/scratch/{digest}-{artifact['archive']['filename']}"
        source = ["--fetch-archive-to", scratch] if digest not in fetched else ["--archive", fetched[digest]]
        fetched.setdefault(digest, scratch)
        generation = artifact["tree"]["inventory_sha256"]
        sub_path = generation_sub_path(tree_prefix, artifact_id, generation)
        steps.append(
            {
                "name": f"stage-{artifact_id}"[:63],
                "image": image,
                "command": [
                    python,
                    "-m",
                    f"{PACKAGE_NAME}.localization",
                    "stage",
                    "--contract",
                    CONTRACT_MOUNT,
                    "--artifact-id",
                    artifact_id,
                    *source,
                    # The tool stages into a private temporary generation under
                    # this root, so the rename that publishes the verified bytes
                    # stays on one filesystem and the mounted path can never be
                    # rewritten afterwards. The marker is written inside the
                    # generation, so no second path is passed or mounted.
                    "--artifact-root",
                    tree_path(tree_prefix, GENERATIONS_DIR, artifact_id),
                    "--sub-path",
                    sub_path,
                    *plane_localizer_arguments(plane, namespace),
                    "--visibility",
                    artifact.get("visibility", "public"),
                    "--receipt",
                    tree_path(tree_prefix, RECEIPTS_DIR, f"{artifact_id}.stage.json"),
                ],
                "env": [
                    {"name": "PYTHONPATH", "value": PACKAGE_MOUNT},
                    {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                    {"name": "HOME", "value": "/scratch"},
                    {"name": "TMPDIR", "value": "/scratch"},
                ],
                "volumeMounts": mounts,
                "resources": resources,
            }
        )
    prepare = {
        "name": "prepare",
        "image": image,
        "command": [
            python,
            "-c",
            # Group-writable so another member of the claim's group can add a
            # receipt later without needing the account that staged first.
            # The prefix is created here, by the run that owns it, because a
            # volume subPath that does not exist yet cannot be mounted.
            f"import os; os.makedirs({receipts!r}, mode=0o775, exist_ok=True); "
            f"os.chmod({receipts!r}, 0o775)",
        ],
        "volumeMounts": mounts,
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
    }
    report = {
        "name": "report",
        "image": image,
        "command": [
            python,
            "-c",
            "import json,os,sys;"
            f"paths=sorted(os.listdir({receipts!r}));"
            f"docs=[json.load(open(os.path.join({receipts!r},p))) for p in paths];"
            "print(json.dumps(docs, indent=2, sort_keys=True));"
            "sys.exit(0 if docs and all(d['state']=='verified' for d in docs) else 1)",
        ],
        "volumeMounts": mounts,
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": labels(run_id, "stage")},
        "spec": {
            "backoffLimit": 1,
            "ttlSecondsAfterFinished": 86400,
            "template": {
                "metadata": {"labels": labels(run_id, "stage")},
                "spec": {
                    "restartPolicy": "Never",
                    "nodeSelector": node_selector,
                    "tolerations": tolerations,
                    "securityContext": security_context,
                    "initContainers": [prepare, *steps],
                    "containers": [report],
                    "volumes": volumes,
                },
            },
        },
    }


def _visibility(artifact: dict[str, Any]) -> str:
    return artifact.get("visibility", "public")


def _tree_volume_name(visibility: str) -> str:
    """One volume per plane, named for the plane rather than for a run."""

    return "trees" if visibility == "public" else "trees-private"


def _expected_plane_arguments(plane: dict[str, Any], namespace: str) -> list[str]:
    """What admission must be told to require of this artifact's plane."""

    if plane["kind"] == "host-path":
        return ["--expect-volume-kind", "host-path", "--expect-host-root", plane["host_root"]]
    return [
        "--expect-volume-kind",
        "persistent-volume-claim",
        "--expect-namespace",
        namespace,
        "--expect-claim",
        plane["claim"],
    ]


def qualify_job(
    *,
    name: str,
    namespace: str,
    run_id: str,
    model_id: str,
    image: str,
    python: str,
    config_map: str,
    planes: dict[str, dict[str, Any]],
    artifacts: list[dict[str, Any]],
    probe: list[str],
    queue: str | None,
    node_selector: dict[str, str],
    tolerations: list[dict[str, Any]],
    gpu_resource: str,
    gpu_count: int,
    security_context: dict[str, Any],
    resources: dict[str, Any],
    tree_prefix: str = "",
    private_tree_prefix: str = "",
) -> dict[str, Any]:
    """Mount every tree one model reads, each from the plane that holds it.

    A consumer like BindCraft reads four trees at once, three public and one
    licensed, and they do not live on the same volume. Rendering them all from a
    single plane would either mount the licensed tree from public storage or
    look for three public trees on the academic claim; the first crosses a
    licence boundary silently, which is the failure that must never happen.
    """

    used = sorted({_visibility(item) for item in artifacts})
    missing = [item for item in used if item not in planes]
    if missing:
        raise SystemExit(f"no storage plane was given for {missing}; a mixed-plane consumer needs each one")

    # Qualification can join multiple storage planes in one Pod.  Placement
    # and Unix access therefore have to be derived from every selected
    # artifact, not from the CLI's single --plane value (which describes
    # stage/promotion writes).  In particular, BindCraft reads public trees
    # owned by 1000:1000 and a private PyRosetta tree group-owned by 65532.
    node_selector = dict(node_selector)
    security_context = json.loads(json.dumps(security_context))
    supplemental_groups = list(security_context.get("supplementalGroups", []))
    if "public" in used:
        selected_value = node_selector.get(REFERENCE_DATA_NODE_LABEL)
        if selected_value not in {None, "true"}:
            raise SystemExit(
                f"a public artifact requires {REFERENCE_DATA_NODE_LABEL}=true; "
                f"the requested selector used {selected_value!r}"
            )
        node_selector[REFERENCE_DATA_NODE_LABEL] = "true"
        supplemental_groups.append(PUBLIC_PLANE_UID)
    if "tenant-private" in used:
        private_plane = planes["tenant-private"]
        if namespace == ACADEMIC_NAMESPACE and private_plane.get("claim") == ACADEMIC_CLAIM:
            supplemental_groups.append(ACADEMIC_ASSET_GID)
    if supplemental_groups:
        security_context["supplementalGroups"] = sorted(set(supplemental_groups))

    volumes = [
        {"name": "verifier", "configMap": {"name": config_map}},
        {"name": "scratch", "emptyDir": {}},
    ]
    mounts = [
        {"name": "verifier", "mountPath": f"{PACKAGE_MOUNT}/{PACKAGE_NAME}", "readOnly": True},
        {"name": "scratch", "mountPath": "/scratch"},
    ]
    for visibility in used:
        volumes.append(tree_volume(planes[visibility], name=_tree_volume_name(visibility)))

    prefixes = {"public": tree_prefix, "tenant-private": private_tree_prefix or tree_prefix}
    marker_digests = {}
    for item in artifacts:
        visibility = _visibility(item)
        marker_digests[item["artifact_id"]] = marker_digest_for(
            item,
            plane=planes[visibility],
            namespace=namespace,
            sub_path=generation_sub_path(
                prefixes[visibility], item["artifact_id"], item["tree"]["inventory_sha256"]
            ),
        )

    runtime_mounts = list(mounts)
    for artifact in artifacts:
        visibility = _visibility(artifact)
        generation = artifact["tree"]["inventory_sha256"]
        for mount_path in artifact["tree"]["mount_paths"]:
            runtime_mounts.append(
                {
                    "name": _tree_volume_name(visibility),
                    "mountPath": mount_path,
                    "subPath": generation_sub_path(prefixes[visibility], artifact["artifact_id"], generation),
                    "readOnly": True,
                }
            )
    verify_steps = [
        {
            "name": f"verify-{artifact['artifact_id']}"[:63],
            "image": image,
            # Admit the mount from the marker inside it. The generation is named
            # by the digest of its own content, so a start-up gate does not have
            # to rehash gigabytes to know which bytes it received; the recursive
            # count is the structural cross-check that the mount is that shape.
            "command": [
                python,
                "-m",
                f"{PACKAGE_NAME}.localization",
                "marker",
                "--artifact-id",
                artifact["artifact_id"],
                "--mount",
                artifact["tree"]["mount_paths"][0],
                "--expect-generation",
                artifact["tree"]["inventory_sha256"],
                "--sub-path",
                generation_sub_path(
                    prefixes[_visibility(artifact)],
                    artifact["artifact_id"],
                    artifact["tree"]["inventory_sha256"],
                ),
                # Bytes that are right in a place or a licence that is wrong are
                # still wrong, so admission pins the plane, the visibility and
                # the algorithm alongside the digest.
                "--expect-manifest-digest",
                marker_digests[artifact["artifact_id"]],
                "--expect-visibility",
                artifact.get("visibility", "public"),
                "--expect-algorithm",
                artifact["tree"]["inventory_algorithm"],
                *_expected_plane_arguments(planes[_visibility(artifact)], namespace),
            ],
            "env": [
                {"name": "PYTHONPATH", "value": PACKAGE_MOUNT},
                {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
            ],
            "volumeMounts": runtime_mounts,
            "resources": resources,
        }
        for artifact in artifacts
    ]
    probe_resources = json.loads(json.dumps(resources))
    if gpu_count:
        probe_resources["requests"][gpu_resource] = str(gpu_count)
        probe_resources["limits"][gpu_resource] = str(gpu_count)
    metadata_labels = labels(run_id, f"qualify-{model_id}")
    if queue:
        metadata_labels["kueue.x-k8s.io/queue-name"] = queue
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": metadata_labels},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 86400,
            **({"suspend": True} if queue else {}),
            "template": {
                "metadata": {"labels": metadata_labels},
                "spec": {
                    "restartPolicy": "Never",
                    "nodeSelector": node_selector,
                    "tolerations": tolerations,
                    "securityContext": security_context,
                    "initContainers": verify_steps,
                    "containers": [
                        {
                            "name": "probe",
                            "image": image,
                            "command": probe,
                            "env": [
                                {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                                {"name": "HOME", "value": "/scratch"},
                                {"name": "TMPDIR", "value": "/scratch"},
                                {"name": "FS2_TREE_RECEIPTS", "value": QUALIFY_RECEIPT_DIR},
                                *[
                                    {"name": "FS2_NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}}
                                ],
                            ],
                            "volumeMounts": runtime_mounts,
                            "resources": probe_resources,
                        }
                    ],
                    "volumes": volumes,
                },
            },
        },
    }


def _resolved_volume(volume: dict[str, Any], sub_path: str) -> dict[str, Any]:
    """Address one generation on whichever plane holds it.

    The two planes are addressed differently and the difference is real: the
    public model-artifact plane is a Terraform-managed host directory that every
    node mounts and is selected by node label, while the licensed tree lives in a
    namespaced claim. Flattening them into one shape would name a claim that does
    not exist, or a host path that is not mounted.
    """

    resolved: dict[str, Any] = {
        "kind": volume["kind"],
        "plane": volume["plane"],
        # Whether the volume itself exists, which is a different question from
        # whether anything has been published into it.
        "plane_state": volume["plane_state"],
        "sub_path": sub_path,
        "read_only": True,
        "immutable": True,
        # What is known about the generation at that path. "rendered" means the
        # path and identity are derived from the contract and nothing has been
        # published there yet.
        "binding_state": volume["binding_state"],
    }
    if volume["kind"] == "host-path":
        resolved["host_root"] = volume["host_root"]
        resolved["host_path"] = f"{volume['host_root']}/{sub_path}"
        resolved["node_selector"] = dict(volume["node_selector"])
    else:
        resolved["namespace"] = volume["namespace"]
        resolved["claim"] = volume["claim"]
    return resolved


def binding_handoff(
    *,
    public_volume: dict[str, Any],
    private_volume: dict[str, Any],
    tree_prefix: str,
    private_tree_prefix: str,
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Describe exactly how a consumer mounts each localized tree.

    Every value here is derived from the contract, so a consumer that follows
    this handoff and a control-plane preflight cannot disagree about what a
    mount is supposed to contain.

    Storage authority is per artifact, not per run. Public bytes belong on the
    dedicated public reference plane; a licensed tree belongs only on the
    tenant-private academic claim. Putting public artifacts on the academic
    claim would freeze a licensed, tenant-scoped volume into the role of general
    artifact storage, which is exactly the separation this split protects.
    """

    localization = _localization()
    entries = []
    for artifact in artifacts:
        tree = artifact["tree"]
        generation = tree["inventory_sha256"]
        contract = localization.LocalizationContract.parse(artifact)
        private = contract.visibility == "tenant-private"
        volume = private_volume if private else public_volume
        prefix = private_tree_prefix if private else tree_prefix
        # Every runtime binding is content addressed, licensed trees included. A
        # producer's install path is mutable: it is where the bytes were built,
        # not a name that can only ever mean these bytes. Binding a runtime to it
        # would let the tree change underneath an admitted workload.
        sub_path = generation_sub_path(prefix, artifact["artifact_id"], generation)
        marker = marker_document(
            artifact,
            plane=volume,
            namespace=volume.get("namespace", ""),
            sub_path=sub_path,
        )
        entry: dict[str, Any] = {
            "artifact_id": artifact["artifact_id"],
            "generation": generation,
            "visibility": contract.visibility,
            "externally_installed": contract.externally_installed,
            "volume": _resolved_volume(volume, sub_path),
            "marker": {
                # One authority, sealed inside the generation by the same rename
                # that publishes it. The reserved name is excluded from every
                # inventory algorithm, so sealing it cannot move the digest the
                # producing plane published.
                "in_generation": True,
                "path": f"{sub_path}/{RUNTIME_MARKER_NAME}",
                "relative_path": RUNTIME_MARKER_NAME,
                "schema": marker["schema"],
                "manifest_digest": localization.marker_sha256(marker),
                "document": marker,
            },
            "mounts": [{"mount_path": path, "read_only": True} for path in tree["mount_paths"]],
            "consumers": artifact["consumers"],
            "archive_provenance": {
                "filename": artifact["archive"]["filename"],
                "sha256": artifact["archive"]["sha256"],
                "bytes": artifact["archive"]["bytes"],
                "source_revision": artifact["archive"]["source_revision"],
                "license_id": artifact["archive"]["license_id"],
            },
            "tree_identity": {
                "entry_count": tree["entry_count"],
                "directory_count": tree.get("directory_count", 0),
                "total_bytes": tree["total_bytes"],
                "inventory_algorithm": tree["inventory_algorithm"],
                "inventory_sha256": tree["inventory_sha256"],
            },
            "generated_entries": tree.get("generated_entries", []),
        }
        if contract.externally_installed:
            # Where the producing plane built the bytes. It is an input to the
            # promotion and a provenance record, never something a runtime binds.
            entry["promoted_from"] = {
                "kind": private_volume["kind"],
                "namespace": private_volume["namespace"],
                "claim": private_volume["claim"],
                "sub_path": contract.source_sub_path,
                "owner": "academic-assets",
                "mutable": True,
                "runtime_bindable": False,
            }
        entries.append(entry)
    by_model: dict[str, list[str]] = {}
    for entry in entries:
        for consumer in entry["consumers"]:
            by_model.setdefault(consumer["model_id"], []).append(entry["artifact_id"])
    return {
        "schema": "fs2-serve.nebius.ai/scientific-localization-binding-handoff/v1",
        "scope": "poc",
        "note": (
            "Every runtime binding is an immutable generation at "
            "<prefix>/generations/<artifact_id>/sha256/<tree digest>, so a consumer binds "
            "content rather than a mutable path. The marker named here lives inside that "
            "generation and is its single admission authority; manifest_digest is the "
            "SHA-256 of exactly the bytes of document. Storage authority is per artifact: "
            "public bytes belong on the dedicated public reference plane and a licensed "
            "tree only on the tenant-private academic claim."
        ),
        "evidence": {
            "state": "rendered",
            "generations_published": False,
            "promotion_receipts": [],
            "node_probes": [],
            "meaning": (
                "Every path, identity and marker digest in this document is derived from the "
                "checked-in contract by the same code that writes a marker. None of it has "
                "been published yet: no promotion or staging Job has run for these paths, so "
                "nothing exists at any sub_path named here. plane_state describes the volume, "
                "which does exist; binding_state describes the generation, which does not. "
                "Treat this as the exact interface to build against, never as a report that "
                "the bytes are in place."
            ),
            "pyrosetta_note": (
                "The installed tree at the academic claim's pyrosetta-bindcraft/site-packages "
                "predates this work and is the promotion input. It is a mutable install path, "
                "not an immutable generation, and its existence is not evidence that the "
                "content-addressed generation named here has been published."
            ),
            "promotes_to_next_state_when": (
                "a terminal promotion receipt exists per artifact, at which point binding_state "
                "becomes 'promoted'; a node probe that admits the mount makes it 'qualified'."
            ),
        },
        "volumes": {"public": public_volume, "tenant-private": private_volume},
        "models": {model: sorted(ids) for model, ids in sorted(by_model.items())},
        "artifacts": entries,
    }


def marker_document(
    artifact: dict[str, Any], *, plane: dict[str, Any], namespace: str, sub_path: str
) -> dict[str, Any]:
    """The exact marker a promotion will seal into this generation.

    Produced by the module that actually writes it, so a digest a rendered Job
    pins and a digest a node computes cannot come from two implementations.
    """

    localization = _localization()
    contract = localization.LocalizationContract.parse(artifact)
    tree = artifact["tree"]
    return localization.generation_marker(
        artifact_id=contract.artifact_id,
        artifact_kind=contract.artifact_kind,
        generation=tree["inventory_sha256"],
        entry_count=tree["entry_count"],
        directory_count=tree.get("directory_count", 0),
        symlink_count=tree.get("symlink_count"),
        total_bytes=tree["total_bytes"],
        inventory_algorithm=tree["inventory_algorithm"],
        sub_path=sub_path,
        volume_kind=plane["kind"],
        namespace=namespace if plane["kind"] == "persistent-volume-claim" else "",
        claim=plane.get("claim", "") if plane["kind"] == "persistent-volume-claim" else "",
        host_root=plane.get("host_root", "") if plane["kind"] == "host-path" else "",
        visibility=contract.visibility,
        archive=contract.archive,
        generated_entries=contract.tree.generated_entries,
        consumer_paths=contract.tree.mount_paths,
    )


def marker_digest_for(
    artifact: dict[str, Any], *, plane: dict[str, Any], namespace: str, sub_path: str
) -> str:
    return _localization().marker_sha256(
        marker_document(artifact, plane=plane, namespace=namespace, sub_path=sub_path)
    )


def promote_job(
    *,
    name: str,
    namespace: str,
    run_id: str,
    artifact: dict[str, Any],
    image: str,
    python: str,
    config_map: str,
    plane: dict[str, Any],
    source_claim: str,
    tree_prefix: str,
    node_selector: dict[str, str],
    tolerations: list[dict[str, Any]],
    resources: dict[str, Any],
    security_context: dict[str, Any],
) -> dict[str, Any]:
    """Publish a tree another plane installed as an immutable generation.

    The claim is mounted **once**, at its own root, and both the installed tree
    and the generation root are addressed beneath that single mount. Mounting it
    twice would put the source and the destination in separate mount namespaces,
    and ``os.link`` across those returns EXDEV even though the bytes share one
    filesystem, so a promotion that looks zero-copy would quietly write a second
    full copy of a multi-gigabyte tree onto a volume with no room for it.

    The source is therefore not protected by a read-only mount any more, so it is
    protected by the tool instead: the promotion only ever reads it, refuses any
    source file that is writable, and never chmods a shared inode.
    """

    artifact_id = artifact["artifact_id"]
    if artifact["transform"] != "external-installed-tree":
        raise SystemExit(f"{artifact_id} is staged from its archive, not promoted from an installed tree")
    generation = artifact["tree"]["inventory_sha256"]
    sub_path = generation_sub_path(tree_prefix, artifact_id, generation)
    receipts = tree_path(tree_prefix, RECEIPTS_DIR)
    if plane["kind"] != "persistent-volume-claim" or source_claim != plane["claim"]:
        # Sharing bytes by link requires one mount, which requires one claim.
        raise SystemExit(
            f"a zero-copy promotion needs the installed tree and its generation on one mount: "
            f"source claim {source_claim!r} and destination claim "
            f"{plane.get('claim', plane['kind'])!r} differ, which would force a full copy"
        )
    volumes = [
        {"name": "verifier", "configMap": {"name": config_map}},
        tree_volume(plane),
        {"name": "scratch", "emptyDir": {}},
    ]
    mounts = [
        {"name": "verifier", "mountPath": f"{PACKAGE_MOUNT}/{PACKAGE_NAME}", "readOnly": True},
        {"name": "trees", "mountPath": TREE_ROOT},
        {"name": "scratch", "mountPath": "/scratch"},
    ]
    source = f"{TREE_ROOT}/{artifact['source_sub_path']}"
    command = [
        python,
        "-m",
        f"{PACKAGE_NAME}.localization",
        "promote",
        "--contract",
        CONTRACT_MOUNT,
        "--artifact-id",
        artifact_id,
        "--promote-from",
        source,
        "--artifact-root",
        tree_path(tree_prefix, GENERATIONS_DIR, artifact_id),
        "--sub-path",
        sub_path,
        *plane_localizer_arguments(plane, namespace),
        "--visibility",
        artifact.get("visibility", "tenant-private"),
        "--receipt",
        tree_path(tree_prefix, RECEIPTS_DIR, f"{artifact_id}.promote.json"),
    ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": labels(run_id, "promote")},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 86400,
            "template": {
                "metadata": {"labels": labels(run_id, "promote")},
                "spec": {
                    "restartPolicy": "Never",
                    "enableServiceLinks": False,
                    "automountServiceAccountToken": False,
                    "nodeSelector": node_selector,
                    "tolerations": tolerations,
                    "securityContext": security_context,
                    "initContainers": [
                        {
                            "name": "receipts",
                            "image": image,
                            "command": [
                                python,
                                "-c",
                                f"import os; os.makedirs({receipts!r}, mode=0o775, exist_ok=True); "
                                f"os.chmod({receipts!r}, 0o775)",
                            ],
                            "volumeMounts": mounts,
                            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
                        }
                    ],
                    "containers": [
                        {
                            "name": f"promote-{artifact_id}"[:63],
                            "image": image,
                            "command": command,
                            "env": [
                                {"name": "PYTHONPATH", "value": PACKAGE_MOUNT},
                                {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                                {"name": "HOME", "value": "/scratch"},
                            ],
                            "volumeMounts": mounts,
                            "resources": resources,
                        }
                    ],
                    "volumes": volumes,
                },
            },
        },
    }


def inventory_job(
    *,
    name: str,
    namespace: str,
    run_id: str,
    artifact_id: str,
    image: str,
    python: str,
    config_map: str,
    claim: str,
    source_sub_path: str,
    marker_prefix: str,
    expect_bytes: int | None,
    cross_references: list[str],
    algorithm: str,
    node_selector: dict[str, str],
    tolerations: list[dict[str, Any]],
    resources: dict[str, Any],
    security_context: dict[str, Any],
) -> dict[str, Any]:
    """Record an identity for a tree another plane staged, without touching it.

    The source is mounted read-only at its canonical sub-path and nothing is
    written into it: an installed tree that another plane already identifies by
    its own digest must not gain files, or that identity moves. The marker is
    written into this tool's own prefix instead.
    """

    generation = "$(FS2_GENERATION)"  # resolved by the container, not the renderer
    del generation
    volumes = [
        {"name": "verifier", "configMap": {"name": config_map}},
        {"name": "source", "persistentVolumeClaim": {"claimName": claim, "readOnly": True}},
        {"name": "markers", "persistentVolumeClaim": {"claimName": claim}},
    ]
    mounts = [
        {"name": "verifier", "mountPath": f"{PACKAGE_MOUNT}/{PACKAGE_NAME}", "readOnly": True},
        {"name": "source", "mountPath": "/source", "subPath": source_sub_path, "readOnly": True},
        # Mounted at its own root, not at marker_prefix: on the first run that
        # prefix does not exist, and a subPath that does not exist cannot be
        # mounted, so mounting one would deadlock the run meant to create it.
        {"name": "markers", "mountPath": "/markers"},
    ]
    command = [
        python,
        "-m",
        f"{PACKAGE_NAME}.localization",
        "inventory",
        "--artifact-id",
        artifact_id,
        "--mount",
        "/source",
        "--sub-path",
        source_sub_path,
        "--namespace",
        namespace,
        "--claim",
        claim,
        "--visibility",
        "tenant-private",
        "--algorithm",
        algorithm,
        "--marker",
        f"/markers/{marker_prefix}/{artifact_id}.json" if marker_prefix else f"/markers/{artifact_id}.json",
    ]
    if expect_bytes is not None:
        command += ["--expect-bytes", str(expect_bytes)]
    for reference in cross_references:
        command += ["--cross-reference", reference]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": labels(run_id, "inventory")},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 86400,
            "template": {
                "metadata": {"labels": labels(run_id, "inventory")},
                "spec": {
                    "restartPolicy": "Never",
                    "enableServiceLinks": False,
                    "automountServiceAccountToken": False,
                    "nodeSelector": node_selector,
                    "tolerations": tolerations,
                    "securityContext": security_context,
                    "initContainers": [
                        {
                            "name": "markers",
                            "image": image,
                            "command": [
                                python,
                                "-c",
                                f"import os; os.makedirs({'/markers/' + marker_prefix if marker_prefix else '/markers'!r}, "
                                f"mode=0o775, exist_ok=True)",
                            ],
                            "volumeMounts": mounts,
                            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
                        }
                    ],
                    "containers": [
                        {
                            "name": "inventory",
                            "image": image,
                            "command": command,
                            "env": [
                                {"name": "PYTHONPATH", "value": PACKAGE_MOUNT},
                                {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                                {"name": "HOME", "value": "/tmp"},
                            ],
                            "volumeMounts": mounts,
                            "resources": resources,
                        }
                    ],
                    "volumes": volumes,
                },
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("stage", "promote", "qualify", "handoff", "inventory"))
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--artifact-id", action="append", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--run-id", default="", help="required for stage and qualify")
    parser.add_argument("--image", default="", help="digest-pinned runtime image reference")
    parser.add_argument("--python", default="python")
    parser.add_argument("--claim", required=True)
    parser.add_argument("--config-map", default="", help="required for stage and qualify")
    parser.add_argument(
        "--storage-class",
        help="render a new claim with this class; omit to use a claim that already exists",
    )
    parser.add_argument("--storage-size", default="16Gi")
    parser.add_argument(
        "--tree-prefix",
        default="",
        help="subtree of the claim these trees live under, for a claim shared with other assets",
    )
    parser.add_argument(
        "--plane",
        default="persistent-volume-claim",
        choices=("persistent-volume-claim", "host-path"),
        help="which storage plane this run writes generations to",
    )
    parser.add_argument(
        "--host-root",
        default="/mnt/fs2-reference-data/data",
        help="host-path plane: the Terraform-managed public model-artifact host root",
    )
    parser.add_argument(
        "--source-claim",
        help="promote: claim holding the tree another plane installed",
    )
    parser.add_argument(
        "--private-tree-prefix",
        default="scientific-localization/private",
        help="subtree of the tenant-private claim that licensed generations live under",
    )
    parser.add_argument(
        "--public-host-root",
        default="/mnt/fs2-reference-data/data",
        help="handoff: Terraform-managed public model-artifact host root, mounted on every labelled node",
    )
    parser.add_argument(
        "--public-node-selector",
        action="append",
        default=["storage.fs2.nebius/reference-data=true"],
        metavar="KEY=VALUE",
        help="handoff: how a consumer lands on a node that mounts the public host root",
    )
    parser.add_argument(
        "--public-plane-state",
        default="provisioned",
        choices=("declared", "provisioned"),
        help="handoff: whether the public host plane itself exists and is mounted",
    )
    parser.add_argument(
        "--private-plane-state",
        default="provisioned",
        choices=("declared", "provisioned"),
        help="handoff: whether the tenant-private claim itself exists and is bound",
    )
    # There is deliberately no --binding-state. Everything this renderer knows is
    # derived from the contract, so the only state it can truthfully report is
    # "rendered". A flag that let a caller write "promoted" or "qualified" while
    # the same document carried no receipts and no probes would let the CLI
    # synthesize a readiness nobody established, which is the exact class of
    # claim this task has already been blocked for. Those states arrive together
    # with the code that ingests and validates a terminal promotion receipt per
    # artifact and a node probe, not before.
    parser.add_argument("--model-id", help="qualify only: which runtime is being proven")
    parser.add_argument("--probe", action="append", help="qualify only: model-side probe argv")
    parser.add_argument(
        "--probe-file",
        action="append",
        default=[],
        type=Path,
        help="qualify only: extra script delivered beside the verifier",
    )
    parser.add_argument("--queue", help="qualify only: Kueue LocalQueue name")
    parser.add_argument("--gpu-resource", default="nvidia.com/gpu")
    parser.add_argument(
        "--gpu-count",
        type=int,
        default=1,
        help="qualify only; 0 verifies a mount without holding an accelerator",
    )
    parser.add_argument("--visibility", default="public", choices=("public", "tenant-private"))
    parser.add_argument(
        "--algorithm",
        default="fs2-tree-manifest/v1",
        choices=("fs2-tree-inventory/v2", "fs2-tree-manifest/v1"),
        help="inventory: which identity algorithm measures the tree",
    )
    parser.add_argument("--source-sub-path", help="inventory: the tree's canonical sub-path in the claim")
    parser.add_argument("--marker-prefix", default="", help="inventory: where this tool writes its marker")
    parser.add_argument("--expect-bytes", type=int, help="inventory: fail unless the tree holds exactly this many")
    parser.add_argument("--cross-reference", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--run-as-user", type=int, help=f"defaults to {PUBLIC_PLANE_UID} on the public host plane")
    parser.add_argument("--run-as-group", type=int, help=f"defaults to {PUBLIC_PLANE_UID} on the public host plane")
    parser.add_argument("--supplemental-group", action="append", type=int, default=[])
    parser.add_argument(
        "--fs-group",
        type=int,
        help="only for a claim this workload owns outright; never on a shared claim",
    )
    parser.add_argument("--cpu-request", default="500m")
    parser.add_argument("--cpu-limit", default="2")
    parser.add_argument("--memory-request", default="2Gi")
    parser.add_argument("--memory-limit", default="8Gi")
    parser.add_argument("--node-selector", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--toleration", action="append", default=[], metavar="KEY=VALUE:EFFECT")
    options = parser.parse_args(argv)

    # Which storage plane this run writes to. Public model artifacts live on the
    # Terraform-managed reference-data host root that every labelled node mounts;
    # the licensed tree lives in a namespaced claim.
    if options.plane == "host-path":
        plane: dict[str, Any] = {"kind": "host-path", "host_root": options.host_root}
        if options.mode in {"stage", "promote", "qualify"}:
            selected = dict(item.split("=", 1) for item in options.node_selector)
            if selected.get(REFERENCE_DATA_NODE_LABEL) != "true":
                raise SystemExit(
                    f"the public host plane is mounted only on nodes labelled "
                    f"{REFERENCE_DATA_NODE_LABEL}=true; add --node-selector {REFERENCE_DATA_NODE_LABEL}=true"
                )
    else:
        if not options.claim:
            raise SystemExit("a claim-backed plane requires --claim")
        plane = {"kind": "persistent-volume-claim", "claim": options.claim}

    # A volume's owner is a property of the volume, not of this tool. The public
    # host root is owned by 1000:1000; the academic claim is group-writable by
    # 65532, which a writer joins as a supplemental group.
    host_plane = options.plane == "host-path"
    default_uid = PUBLIC_PLANE_UID if host_plane else DEFAULT_RUNTIME_UID
    run_as_user = options.run_as_user if options.run_as_user is not None else default_uid
    run_as_group = options.run_as_group if options.run_as_group is not None else default_uid
    supplemental = list(options.supplemental_group)
    academic_claim = (
        not host_plane and options.namespace == ACADEMIC_NAMESPACE and options.claim == ACADEMIC_CLAIM
    )
    if academic_claim:
        if not supplemental:
            supplemental = [ACADEMIC_ASSET_GID]
        if options.fs_group is not None:
            raise SystemExit(
                f"fsGroup rewrites ownership of the whole {ACADEMIC_CLAIM} claim, not the sub-path this "
                "run mounts, and that claim also holds another tenant's assets; join the group with "
                f"--supplemental-group {ACADEMIC_ASSET_GID} instead"
            )
    elif options.mode != "qualify" and not host_plane and not supplemental and options.fs_group is None:
        raise SystemExit(
            "a claim's ownership is a property of that volume, and this tool will not guess it. "
            "Pass --supplemental-group to join the group that owns the claim, or --fs-group if the "
            "workload owns the volume outright"
        )

    # A qualification mounts every tree one model reads, and those can live on
    # different planes, so both are resolved rather than one.
    qualify_planes = {
        "public": {"kind": "host-path", "host_root": options.host_root},
        "tenant-private": {"kind": "persistent-volume-claim", "claim": options.claim},
    }

    security_context_early = pod_security_context(
        uid=run_as_user,
        gid=run_as_group,
        supplemental=tuple(supplemental),
        fs_group=options.fs_group,
    )
    if options.mode == "inventory":
        if not options.source_sub_path or not options.marker_prefix:
            raise SystemExit("inventory requires --source-sub-path and --marker-prefix")
        if "@sha256:" not in options.image:
            raise SystemExit("--image must be an immutable digest reference")
        json.dump(
            {
                "apiVersion": "v1",
                "kind": "List",
                "items": [
                    verifier_config_map(options.config_map, options.namespace, options.run_id, options.contract),
                    inventory_job(
                        name=f"fs2-localize-inventory-{options.run_id}"[:63],
                        namespace=options.namespace,
                        run_id=options.run_id,
                        artifact_id=options.artifact_id[0],
                        image=options.image,
                        python=options.python,
                        config_map=options.config_map,
                        claim=options.claim,
                        source_sub_path=options.source_sub_path,
                        marker_prefix=options.marker_prefix,
                        expect_bytes=options.expect_bytes,
                        cross_references=options.cross_reference,
                        algorithm=options.algorithm,
                        node_selector=dict(item.split("=", 1) for item in options.node_selector),
                        tolerations=[],
                        resources={
                            "requests": {"cpu": options.cpu_request, "memory": options.memory_request},
                            "limits": {"cpu": options.cpu_limit, "memory": options.memory_limit},
                        },
                        security_context=security_context_early,
                    ),
                ],
            },
            sys.stdout,
            indent=2,
            sort_keys=True,
        )
        sys.stdout.write("\n")
        return 0

    document = load_contract(options.contract)
    artifacts = selected_artifacts(document, tuple(options.artifact_id))
    if options.mode == "handoff":
        json.dump(
            binding_handoff(
                public_volume={
                    "kind": "host-path",
                    "plane": "reference-data-host",
                    "host_root": options.public_host_root,
                    "node_selector": dict(
                        item.split("=", 1) for item in options.public_node_selector
                    ),
                    "plane_state": options.public_plane_state,
                    "binding_state": RENDERED_BINDING_STATE,
                },
                private_volume={
                    "kind": "persistent-volume-claim",
                    "plane": "tenant-private-academic",
                    "namespace": options.namespace,
                    "claim": options.claim,
                    "plane_state": options.private_plane_state,
                    "binding_state": RENDERED_BINDING_STATE,
                },
                tree_prefix=options.tree_prefix,
                private_tree_prefix=options.private_tree_prefix,
                artifacts=artifacts,
            ),
            sys.stdout,
            indent=2,
            sort_keys=True,
        )
        sys.stdout.write("\n")
        return 0
    if not options.run_id or not options.config_map:
        raise SystemExit(f"{options.mode} requires --run-id and --config-map")
    if "@sha256:" not in options.image:
        raise SystemExit("--image must be an immutable digest reference")
    security_context = pod_security_context(
        uid=run_as_user,
        gid=run_as_group,
        supplemental=tuple(supplemental),
        fs_group=options.fs_group,
    )
    node_selector = dict(item.split("=", 1) for item in options.node_selector)
    tolerations = []
    for raw in options.toleration:
        key_value, _, effect = raw.partition(":")
        key, _, value = key_value.partition("=")
        tolerations.append({"key": key, "operator": "Equal", "value": value, "effect": effect or "NoSchedule"})

    items: list[dict[str, Any]] = [
        verifier_config_map(
            options.config_map,
            options.namespace,
            options.run_id,
            options.contract,
            tuple(options.probe_file),
        )
    ]
    if options.mode == "promote":
        if len(artifacts) != 1:
            raise SystemExit("promote takes exactly one --artifact-id")
        if not options.source_claim:
            raise SystemExit("promote requires --source-claim")
        items.append(
            promote_job(
                name=f"fs2-localize-promote-{options.run_id}"[:63],
                namespace=options.namespace,
                run_id=options.run_id,
                artifact=artifacts[0],
                image=options.image,
                python=options.python,
                config_map=options.config_map,
                plane=plane,
                source_claim=options.source_claim,
                tree_prefix=options.private_tree_prefix,
                node_selector=node_selector,
                tolerations=tolerations,
                resources={
                    "requests": {"cpu": options.cpu_request, "memory": options.memory_request},
                    "limits": {"cpu": options.cpu_limit, "memory": options.memory_limit},
                },
                security_context=security_context,
            )
        )
    elif options.mode == "stage":
        if options.storage_class:
            items.append(
                tree_claim(
                    options.claim, options.namespace, options.run_id, options.storage_class, options.storage_size
                )
            )
        items.append(
            stage_job(
                name=f"fs2-localize-stage-{options.run_id}",
                namespace=options.namespace,
                run_id=options.run_id,
                image=options.image,
                python=options.python,
                config_map=options.config_map,
                plane=plane,
                artifacts=artifacts,
                node_selector=node_selector,
                tolerations=tolerations,
                resources={
                    "requests": {"cpu": options.cpu_request, "memory": options.memory_request},
                    "limits": {"cpu": options.cpu_limit, "memory": options.memory_limit},
                },
                security_context=security_context,
                tree_prefix=options.tree_prefix,
            )
        )
    else:
        if not options.model_id or not options.probe:
            raise SystemExit("qualify requires --model-id and --probe")
        items.append(
            qualify_job(
                name=f"fs2-localize-qualify-{options.model_id}-{options.run_id}"[:63],
                namespace=options.namespace,
                run_id=options.run_id,
                model_id=options.model_id,
                image=options.image,
                python=options.python,
                config_map=options.config_map,
                planes=qualify_planes,
                artifacts=artifacts,
                probe=options.probe,
                queue=options.queue,
                node_selector=node_selector,
                tolerations=tolerations,
                gpu_resource=options.gpu_resource,
                gpu_count=options.gpu_count,
                security_context=security_context,
                private_tree_prefix=options.private_tree_prefix,
                resources={
                    "requests": {"cpu": options.cpu_request, "memory": options.memory_request},
                    "limits": {"cpu": options.cpu_limit, "memory": options.memory_limit},
                },
                tree_prefix=options.tree_prefix,
            )
        )
    json.dump({"apiVersion": "v1", "kind": "List", "items": items}, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

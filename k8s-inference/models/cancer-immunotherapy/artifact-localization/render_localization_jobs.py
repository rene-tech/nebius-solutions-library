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
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPO_ROOT / "components/control-plane/src/fs2_serve/scientific_batch/adapters"
DEFAULT_CONTRACT = REPO_ROOT / "catalog/runtime/contracts/scientific-artifact-localization.json"

# The verifier is mounted as a tiny package so the staging and qualification
# workloads run the same code the control plane runs, not a copy of it.
PACKAGE_NAME = "fs2_localization"
PACKAGE_MOUNT = "/opt/fs2-localization"
CONTRACT_MOUNT = f"{PACKAGE_MOUNT}/{PACKAGE_NAME}/localization-contract.json"
TREE_ROOT = "/trees"
RECEIPT_DIR = f"{TREE_ROOT}/.receipts"
# A qualification pod reads the shared volume and must not write to it: it runs
# as the runtime image's own account, which is a guest in the claim's group, and
# its receipt is evidence about one node rather than shared artifact state.
QUALIFY_RECEIPT_DIR = "/scratch"

LABEL_PREFIX = "fs2-serve.nebius.ai"

# Staging never needs root. The account is an argument because the right one is
# a property of the volume, not of this tool: a claim shared with other tenants'
# assets is group-owned and setgid, and writing to it means joining that group.
DEFAULT_RUNTIME_UID = 10001


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


def _verifier_volumes(
    config_map: str,
    claim: str,
    tree_prefix: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Mount the verifier and the tree store.

    ``tree_prefix`` keeps these trees inside their own subtree of a claim that
    already holds other tenants' assets, so a shared volume never becomes a
    shared namespace.
    """

    volumes = [
        {"name": "verifier", "configMap": {"name": config_map}},
        {"name": "trees", "persistentVolumeClaim": {"claimName": claim}},
        {"name": "scratch", "emptyDir": {}},
    ]
    tree_mount: dict[str, Any] = {"name": "trees", "mountPath": TREE_ROOT}
    if tree_prefix:
        tree_mount["subPath"] = tree_prefix
    mounts = [
        {"name": "verifier", "mountPath": f"{PACKAGE_MOUNT}/{PACKAGE_NAME}", "readOnly": True},
        tree_mount,
        {"name": "scratch", "mountPath": "/scratch"},
    ]
    return volumes, mounts


def stage_job(
    *,
    name: str,
    namespace: str,
    run_id: str,
    image: str,
    python: str,
    config_map: str,
    claim: str,
    artifacts: list[dict[str, Any]],
    node_selector: dict[str, str],
    tolerations: list[dict[str, Any]],
    resources: dict[str, Any],
    security_context: dict[str, Any],
    tree_prefix: str = "",
) -> dict[str, Any]:
    volumes, mounts = _verifier_volumes(config_map, claim, tree_prefix)
    steps: list[dict[str, Any]] = []
    # Two artifacts can share one upstream archive; fetch it once and let the
    # later step localize from the copy already on disk.
    fetched: dict[str, str] = {}
    for artifact in artifacts:
        artifact_id = artifact["artifact_id"]
        digest = artifact["archive"]["sha256"]
        scratch = f"/scratch/{digest}-{artifact['archive']['filename']}"
        source = ["--fetch-archive-to", scratch] if digest not in fetched else ["--archive", fetched[digest]]
        fetched.setdefault(digest, scratch)
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
                    "--mount",
                    f"{TREE_ROOT}/{artifact_id}",
                    "--receipt",
                    f"{RECEIPT_DIR}/{artifact_id}.stage.json",
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
            f"import os; os.makedirs({RECEIPT_DIR!r}, mode=0o775, exist_ok=True); "
            f"os.chmod({RECEIPT_DIR!r}, 0o775)",
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
            f"paths=sorted(os.listdir({RECEIPT_DIR!r}));"
            f"docs=[json.load(open(os.path.join({RECEIPT_DIR!r},p))) for p in paths];"
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


def qualify_job(
    *,
    name: str,
    namespace: str,
    run_id: str,
    model_id: str,
    image: str,
    python: str,
    config_map: str,
    claim: str,
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
) -> dict[str, Any]:
    volumes, mounts = _verifier_volumes(config_map, claim, tree_prefix)
    runtime_mounts = list(mounts)
    for artifact in artifacts:
        for mount_path in artifact["tree"]["mount_paths"]:
            runtime_mounts.append(
                {
                    "name": "trees",
                    "mountPath": mount_path,
                    "subPath": f"{tree_prefix}/{artifact['artifact_id']}".lstrip("/"),
                    "readOnly": True,
                }
            )
    verify_steps = [
        {
            "name": f"verify-{artifact['artifact_id']}"[:63],
            "image": image,
            "command": [
                python,
                "-m",
                f"{PACKAGE_NAME}.localization",
                "verify",
                "--contract",
                CONTRACT_MOUNT,
                "--artifact-id",
                artifact["artifact_id"],
                "--mount",
                artifact["tree"]["mount_paths"][0],
                "--receipt",
                f"{QUALIFY_RECEIPT_DIR}/{artifact['artifact_id']}.{model_id}-node.json",
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


def binding_handoff(
    *,
    namespace: str,
    claim: str,
    tree_prefix: str,
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Describe exactly how a consumer mounts each localized tree.

    Every value here is derived from the contract, so a consumer that follows
    this handoff and a control-plane preflight cannot disagree about what a
    mount is supposed to contain.
    """

    entries = []
    for artifact in artifacts:
        tree = artifact["tree"]
        sub_path = f"{tree_prefix}/{artifact['artifact_id']}".lstrip("/")
        entries.append(
            {
                "artifact_id": artifact["artifact_id"],
                "volume": {
                    "namespace": namespace,
                    "claim": claim,
                    "sub_path": sub_path,
                    "read_only": True,
                },
                "mounts": [
                    {"mount_path": path, "read_only": True} for path in tree["mount_paths"]
                ],
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
                    "total_bytes": tree["total_bytes"],
                    "inventory_algorithm": tree["inventory_algorithm"],
                    "inventory_sha256": tree["inventory_sha256"],
                },
                "generated_entries": tree.get("generated_entries", []),
            }
        )
    return {
        "schema": "fs2-serve.nebius.ai/scientific-localization-binding-handoff/v1",
        "scope": "poc",
        "note": (
            "These trees live under one sub-path of a claim that also holds tenant-private "
            "academic assets. The sub-path keeps them separable; it is not a global cache."
        ),
        "artifacts": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("stage", "qualify", "handoff"))
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
    parser.add_argument("--run-as-user", type=int, default=DEFAULT_RUNTIME_UID)
    parser.add_argument("--run-as-group", type=int, default=DEFAULT_RUNTIME_UID)
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

    document = load_contract(options.contract)
    artifacts = selected_artifacts(document, tuple(options.artifact_id))
    if options.mode == "handoff":
        json.dump(
            binding_handoff(
                namespace=options.namespace,
                claim=options.claim,
                tree_prefix=options.tree_prefix,
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
        uid=options.run_as_user,
        gid=options.run_as_group,
        supplemental=tuple(options.supplemental_group),
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
    if options.mode == "stage":
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
                claim=options.claim,
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
                claim=options.claim,
                artifacts=artifacts,
                probe=options.probe,
                queue=options.queue,
                node_selector=node_selector,
                tolerations=tolerations,
                gpu_resource=options.gpu_resource,
                gpu_count=options.gpu_count,
                security_context=security_context,
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

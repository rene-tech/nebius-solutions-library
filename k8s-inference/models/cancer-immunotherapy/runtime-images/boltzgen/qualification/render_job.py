#!/usr/bin/env python3
"""Compile the real adapter and render an offline, Kueue-managed H100 Job."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path
from typing import Any, NamedTuple
from uuid import UUID

HERE = Path(__file__).resolve().parent
MODEL_ROOT = HERE.parent
SOLUTION_ROOT = HERE.parents[4]
CONTROL_PLANE = SOLUTION_ROOT / "components/control-plane/src"
sys.path.insert(0, str(CONTROL_PLANE))

from fs2_serve.scientific_batch import ScientificInputArtifact, profile_from_catalog  # noqa: E402
from fs2_serve.scientific_batch.adapters import boltzgen  # noqa: E402

TASK = "fs2-boltzgen-h100-codex-successor-r20260903"
NAMESPACE = "fs2-models"
LOCAL_QUEUE = "inference-models"
ACCELERATOR_CLASS = "nvidia-h100-sxm5-80gb"
POOL_IDS = ("h100-1x", "h100-reserved-8x")
ATTEMPT_NUMBERS = {"cold": 4, "prepared": 2}
PROFILE_PATH = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json"


class InputBundle(NamedTuple):
    raw_tar: bytes
    campaign_payload: bytes
    scientific_manifest: dict[str, Any]
    scientific_manifest_payload: bytes
    input_artifacts: tuple[ScientificInputArtifact, ...]


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def input_tar(yaml_payload: bytes, target_payload: bytes) -> bytes:
    contents = {
        "5J89-chain-A.cif": target_payload,
        "design-specs/pdl1-face.yaml": yaml_payload,
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(contents):
            info = tarfile.TarInfo(name)
            info.size = len(contents[name])
            info.mode = 0o444
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            archive.addfile(info, io.BytesIO(contents[name]))
    return buffer.getvalue()


def gzip_payload(payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, filename="", mode="wb", compresslevel=9, mtime=0) as archive:
        archive.write(payload)
    return buffer.getvalue()


def compile_input_bundle(yaml_payload: bytes, target_payload: bytes) -> tuple[dict[str, Any], InputBundle]:
    raw_tar = input_tar(yaml_payload, target_payload)
    campaign_payload = gzip_payload(raw_tar)
    manifest = json.loads((HERE / "manifest-template.json").read_text(encoding="utf-8"))
    entries = manifest.get("entries")
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise SystemExit("manifest-template.json must contain exactly one entry")
    entry = entries[0]
    if entry.get("name") != boltzgen.CAMPAIGN_INPUT_ID:
        raise SystemExit("manifest-template.json must name the campaign-input artifact")
    if entry.get("semantic_type") != boltzgen.CAMPAIGN_INPUT_SEMANTIC_TYPE:
        raise SystemExit("manifest-template.json has the wrong campaign semantic type")
    campaign_pointer = entry.get("artifact")
    if not isinstance(campaign_pointer, dict):
        raise SystemExit("manifest-template.json campaign entry has no artifact pointer")
    if (
        campaign_pointer.get("media_type") != boltzgen.CAMPAIGN_INPUT_MEDIA_TYPE
        or campaign_pointer.get("compression") != "gzip"
    ):
        raise SystemExit("manifest-template.json campaign artifact must declare application/gzip")
    campaign_pointer["sha256"] = sha256(campaign_payload)
    campaign_pointer["size_bytes"] = len(campaign_payload)

    manifest_payload = canonical_json(manifest)
    request = json.loads((HERE / "request-template.json").read_text(encoding="utf-8"))
    manifest_pointer = request.get("input_manifest")
    if not isinstance(manifest_pointer, dict):
        raise SystemExit("request-template.json has no input_manifest pointer")
    if (
        manifest_pointer.get("media_type") != boltzgen.INPUT_MANIFEST_MEDIA_TYPE
        or manifest_pointer.get("compression") != "none"
    ):
        raise SystemExit("request-template.json input_manifest must declare canonical JSON")
    if manifest_pointer.get("artifact_id") == campaign_pointer.get("artifact_id"):
        raise SystemExit("campaign payload and scientific manifest must use distinct artifact IDs")
    manifest_pointer["sha256"] = sha256(manifest_payload)
    manifest_pointer["size_bytes"] = len(manifest_payload)
    verified_campaign = ScientificInputArtifact(
        logical_artifact_id=entry["name"],
        semantic_type=entry["semantic_type"],
        artifact_id=UUID(campaign_pointer["artifact_id"]),
        digest=f"sha256:{campaign_pointer['sha256']}",
        size_bytes=campaign_pointer["size_bytes"],
        media_type=campaign_pointer["media_type"],
        compression=campaign_pointer["compression"],
    )
    return request, InputBundle(
        raw_tar=raw_tar,
        campaign_payload=campaign_payload,
        scientific_manifest=manifest,
        scientific_manifest_payload=manifest_payload,
        input_artifacts=(verified_campaign,),
    )


def compile_plan(
    *, scenario: str, target: Path, lock: dict[str, Any], pool_id: str = "h100-1x"
) -> tuple[dict[str, object], bytes, bytes, InputBundle]:
    target_payload = target.read_bytes()
    target_contract = lock["input"]
    if (
        len(target_payload) != target_contract["projected_bytes"]
        or sha256(target_payload) != target_contract["projected_sha256"]
    ):
        raise SystemExit("--target does not match the projected PD-L1 identity")
    yaml_payload = (HERE / "pdl1-face.yaml").read_bytes()
    request, input_bundle = compile_input_bundle(yaml_payload, target_payload)
    catalog = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    profile = profile_from_catalog(catalog, "boltzgen")
    operation_id = f"boltzgen-qual-{scenario}-r20260903"
    execution = boltzgen.compile_run(
        profile,
        request,
        operation_id=operation_id,
        input_artifacts=input_bundle.input_artifacts,
    )
    configure = execution.invocation("configure", "pdl1-face")
    design = execution.invocation("design", "pdl1-face")
    adapter_path = SOLUTION_ROOT / lock["adapter"]["repository_path"]
    if sha256(adapter_path.read_bytes()) != lock["adapter"]["source_sha256"]:
        raise SystemExit("checked-in BoltzGen adapter differs from image-lock.json")
    expected_workspace = configure.working_directory
    if design.working_directory != expected_workspace or not expected_workspace.startswith(
        "/mnt/fs2-scientific/work/boltzgen/"
    ):
        raise SystemExit("adapter workspace contract changed")
    plan: dict[str, object] = {
        "schema": "fs2-serve.nebius.ai/boltzgen-h100-generated-plan/v1",
        "scenario": scenario,
        "operation_id": operation_id,
        "batch_id": f"boltzgen-pdl1-{scenario}-r20260903",
        "attempt_id": f"attempt-{scenario}-{ATTEMPT_NUMBERS[scenario]:02d}",
        "request": request,
        "request_sha256": execution.request_sha256,
        "scientific_manifest": input_bundle.scientific_manifest,
        "input_artifact_sha256": sha256(input_bundle.campaign_payload),
        "input_artifact_bytes": len(input_bundle.campaign_payload),
        "input_manifest_sha256": sha256(input_bundle.scientific_manifest_payload),
        "input_manifest_bytes": len(input_bundle.scientific_manifest_payload),
        "configure_argv": list(configure.argv),
        "design_argv": list(design.argv),
        "environment": dict(design.environment),
        "working_directory": expected_workspace,
        "runtime_artifacts": list(design.runtime_artifacts),
        "runtime_trees": [
            {
                "artifact_id": tree.artifact_id,
                "mount_path": tree.mount_path,
                "archive_sha256": tree.archive_sha256,
                "tree_inventory_sha256": tree.tree_inventory_sha256,
                "entry_count": tree.entry_count,
            }
            for tree in design.runtime_trees
        ],
        "scheduling": {
            "local_queue": LOCAL_QUEUE,
            "cluster_queue": "inference-accelerators",
            "workload_priority_class": "batch",
            "workload_priority_value": -100,
            "accelerator_class": ACCELERATOR_CLASS,
            "pool_id": pool_id,
            "capacity_type": "preemptible" if pool_id == "h100-1x" else "regular-capacity-block",
            "resource_flavor": f"inference-{pool_id}",
            "gpu_count": 1,
        },
    }
    return plan, yaml_payload, target_payload, input_bundle


def render(
    *,
    scenario: str,
    target: Path,
    node_name: str | None,
    pool_id: str = "h100-1x",
    lock_path: Path = MODEL_ROOT / "image-lock.json",
) -> dict[str, object]:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    checkpoints = lock["artifacts"]["boltzgen-checkpoints"]
    molecules = lock["artifacts"]["boltzgen-inference-molecules"]
    for name, contract in (("checkpoints", checkpoints), ("molecules", molecules)):
        if not contract.get("generation") or not contract.get("marker_sha256"):
            raise SystemExit(f"{name} generation is not pinned in image-lock.json")
    if pool_id not in lock["cluster"]["allowed_pool_ids"] or pool_id not in POOL_IDS:
        raise SystemExit(f"unsupported H100 pool {pool_id!r}")
    plan, yaml_payload, target_payload, _input_bundle = compile_plan(
        scenario=scenario, target=target, lock=lock, pool_id=pool_id
    )
    name = f"fs2-boltzgen-pdl1-{scenario}-r20260903"
    code_name = f"{name}-code"
    input_name = f"{name}-input"
    labels = {
        "app.kubernetes.io/name": "fs2-boltzgen-qualification",
        "app.kubernetes.io/managed-by": "fs2-boltzgen-qualification",
        "app.kubernetes.io/part-of": "fs2-serve",
        "fs2.nebius.ai/task": TASK,
        "fs2.nebius.ai/model-id": "boltzgen",
        "fs2.nebius.ai/scenario": scenario,
        "fs2.nebius.ai/operation-id": plan["operation_id"],
        "fs2.nebius.ai/batch-id": plan["batch_id"],
        "fs2.nebius.ai/attempt-id": plan["attempt_id"],
        "kueue.x-k8s.io/queue-name": LOCAL_QUEUE,
        "kueue.x-k8s.io/priority-class": "batch",
    }
    annotations = {
        "fs2.nebius.ai/source-revision": lock["source"]["revision"],
        "fs2.nebius.ai/runtime-image-digest": lock["image"]["digest"],
        "fs2.nebius.ai/checkpoint-generation": checkpoints["generation"],
        "fs2.nebius.ai/molecule-generation": molecules["generation"],
        "fs2.nebius.ai/adapter-source-sha256": lock["adapter"]["source_sha256"],
        "fs2.nebius.ai/workload-priority-value": str(plan["scheduling"]["workload_priority_value"]),
    }
    selector = {
        "accelerator.fs2.nebius/class": ACCELERATOR_CLASS,
        "accelerator.fs2.nebius/pool-id": pool_id,
        "storage.fs2.nebius/reference-data": "true",
    }
    if node_name:
        selector["kubernetes.io/hostname"] = node_name
    image = f"{lock['image']['repository']}@{lock['image']['digest']}"
    generation_prefix = "/mnt/fs2-reference-data/data/scientific-localization/public/generations"
    pod_labels = {**labels, "job-name": name}
    pod_spec: dict[str, object] = {
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 90,
        "nodeSelector": selector,
        "tolerations": [
            {
                "key": "dedicated",
                "operator": "Equal",
                "value": "fs2-inference",
                "effect": "NoSchedule",
            }
        ],
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "runAsGroup": 10001,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "qualify",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": [
                    "python",
                    "/opt/fs2/qualification/runtime_driver.py",
                    "--lock",
                    "/opt/fs2/qualification/image-lock.json",
                    "--plan",
                    "/opt/fs2/qualification/generated-plan.json",
                    "--validator",
                    "/opt/fs2/qualification/validate_result.py",
                    "--input-root",
                    "/opt/fs2/input",
                ],
                "env": [{"name": key, "value": value} for key, value in plan["environment"].items()]
                + [
                    {"name": "HOME", "value": "/tmp/home"},
                    {"name": "HF_HOME", "value": "/tmp/huggingface"},
                    {"name": "XDG_CACHE_HOME", "value": "/tmp/xdg"},
                    {"name": "MPLCONFIGDIR", "value": "/tmp/matplotlib"},
                    {"name": "TRITON_HOME", "value": "/tmp/triton"},
                    {"name": "FS2_ATTEMPT_ID", "value": plan["attempt_id"]},
                    {"name": "FS2_POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
                    {"name": "FS2_POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
                    {"name": "FS2_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
                    {"name": "FS2_NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
                ],
                "resources": {
                    "requests": {"cpu": "8", "memory": "48Gi", "nvidia.com/gpu": 1},
                    "limits": {"cpu": "16", "memory": "64Gi", "nvidia.com/gpu": 1},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [
                    {"name": "code", "mountPath": "/opt/fs2/qualification", "readOnly": True},
                    {"name": "input", "mountPath": "/opt/fs2/input", "readOnly": True},
                    {"name": "checkpoints", "mountPath": checkpoints["mount_path"], "readOnly": True},
                    {"name": "molecules", "mountPath": molecules["mount_path"], "readOnly": True},
                    {"name": "workspace", "mountPath": "/mnt/fs2-scientific"},
                    {"name": "tmp", "mountPath": "/tmp"},
                    {"name": "dshm", "mountPath": "/dev/shm"},
                ],
            }
        ],
        "volumes": [
            {"name": "code", "configMap": {"name": code_name}},
            {"name": "input", "configMap": {"name": input_name}},
            {
                "name": "checkpoints",
                "hostPath": {
                    "path": f"{generation_prefix}/boltzgen-checkpoints/sha256/{checkpoints['generation']}",
                    "type": "Directory",
                },
            },
            {
                "name": "molecules",
                "hostPath": {
                    "path": f"{generation_prefix}/boltzgen-inference-molecules/sha256/{molecules['generation']}",
                    "type": "Directory",
                },
            },
            {"name": "workspace", "emptyDir": {"sizeLimit": "20Gi"}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "32Gi"}},
            {"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "8Gi"}},
        ],
    }
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": code_name, "namespace": NAMESPACE, "labels": labels},
                "data": {
                    "runtime_driver.py": (HERE / "runtime_driver.py").read_text(encoding="utf-8"),
                    "validate_result.py": (HERE / "validate_result.py").read_text(encoding="utf-8"),
                    "image-lock.json": json.dumps(lock, indent=2, sort_keys=True) + "\n",
                    "generated-plan.json": json.dumps(plan, indent=2, sort_keys=True) + "\n",
                },
            },
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": input_name, "namespace": NAMESPACE, "labels": labels},
                "data": {"pdl1-face.yaml": yaml_payload.decode("utf-8")},
                "binaryData": {"5J89-chain-A.cif": base64.b64encode(target_payload).decode("ascii")},
            },
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
                "spec": {
                    "podSelector": {"matchLabels": pod_labels},
                    "policyTypes": ["Ingress", "Egress"],
                    "ingress": [],
                    "egress": [],
                },
            },
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {
                    "name": name,
                    "namespace": NAMESPACE,
                    "labels": labels,
                    "annotations": annotations,
                },
                "spec": {
                    "suspend": True,
                    "backoffLimit": 0,
                    "activeDeadlineSeconds": 7200,
                    "ttlSecondsAfterFinished": 86400,
                    "template": {
                        "metadata": {"labels": pod_labels, "annotations": annotations},
                        "spec": pod_spec,
                    },
                },
            },
        ],
        "qualification_plan": plan,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("cold", "prepared"), required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--node-name")
    parser.add_argument("--pool-id", choices=POOL_IDS, default="h100-1x")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--plan-output", type=Path)
    arguments = parser.parse_args()
    payload = render(
        scenario=arguments.scenario,
        target=arguments.target,
        node_name=arguments.node_name,
        pool_id=arguments.pool_id,
    )
    plan = payload.pop("qualification_plan")
    document = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if arguments.output:
        arguments.output.write_text(document, encoding="utf-8")
    else:
        print(document, end="")
    if arguments.plan_output:
        arguments.plan_output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

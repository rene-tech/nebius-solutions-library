#!/usr/bin/env python3
"""Render a contract-backed receipt Job for the existing private AF3 object."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "alphafold3/contracts/af3-parameter-binding.json"

VERIFY = r'''#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from datetime import datetime, timezone

contract_bytes = Path("/verification/af3-parameter-binding.json").read_bytes()
contract = json.loads(contract_bytes)
artifact = contract["artifact"]
delivery = contract["delivery"]
path = Path("/models/af3.bin.zst")
status = path.stat()
if not path.is_file() or path.is_symlink():
    raise SystemExit("private AlphaFold3 parameter mount is not a regular file")
if status.st_size != artifact["size_bytes"]:
    raise SystemExit("private AlphaFold3 parameter size differs from contract")
if stat.S_IMODE(status.st_mode) != int(delivery["permissions"]["file_mode"], 8):
    raise SystemExit("private AlphaFold3 parameter mode differs from contract")
if status.st_gid != delivery["permissions"]["asset_gid"]:
    raise SystemExit("private AlphaFold3 parameter group differs from contract")
digest = hashlib.sha256()
count = 0
with path.open("rb") as stream:
    magic = stream.read(4)
    digest.update(magic)
    count += len(magic)
    for chunk in iter(lambda: stream.read(8 << 20), b""):
        digest.update(chunk)
        count += len(chunk)
actual = digest.hexdigest()
if magic.hex() != artifact["magic_hex"] or actual != artifact["sha256"] or count != artifact["size_bytes"]:
    raise SystemExit("private AlphaFold3 parameter content differs from contract")
receipt = {
    "schema": "fs2.nebius.ai/academic-private-file-localization-receipt/v1",
    "state": "verified",
    "observed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "artifact_id": artifact["artifact_id"],
    "content_identity": {
        "kind": artifact["content_identity_kind"],
        "sha256": actual,
        "size_bytes": count,
        "magic_hex": magic.hex(),
    },
    "source_contract": {
        "contract": contract["contract"],
        "contract_version": contract["contract_version"],
        "sha256": hashlib.sha256(contract_bytes).hexdigest(),
    },
    "physical_binding": {
        "volume_kind": "persistent-volume-claim",
        "claim_namespace": delivery["claim_namespace"],
        "claim": delivery["claim"],
        "claim_uid": os.environ["FS2_CLAIM_UID"],
        "sub_path": delivery["supported_modes"][0]["source_sub_path"],
        "mount_path": delivery["supported_modes"][0]["consumer_path"],
        "read_only": True,
        "duplicates_bytes": False,
    },
    "access_binding": {
        "authorization_document_sha256": os.environ["FS2_AUTHORIZATION_DOCUMENT_SHA256"],
        "authorization_is_localization_receipt": False,
        "supplemental_group": delivery["permissions"]["asset_gid"],
        "fs_group": None,
    },
    "observation": {
        "cluster": "k8s-inference-h100",
        "project_id": "project-e00rene",
        "region": "eu-north1",
        "job": os.environ["FS2_JOB_NAME"],
        "pod": os.environ["FS2_POD_NAME"],
        "pod_uid": os.environ["FS2_POD_UID"],
        "node": os.environ["FS2_NODE_NAME"],
        "runtime_uid": os.getuid(),
        "runtime_gid": os.getgid(),
        "runtime_groups": sorted(os.getgroups()),
        "file_uid": status.st_uid,
        "file_gid": status.st_gid,
        "file_mode": format(stat.S_IMODE(status.st_mode), "04o"),
        "verification": "full-file-sha256",
    },
}
payload = json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
print("FS2_AF3_PARAMETER_LOCALIZATION_RECEIPT " + payload.rstrip("\n"), flush=True)
print("FS2_AF3_PARAMETER_LOCALIZATION_RECEIPT_DIGEST sha256:" + hashlib.sha256(payload.encode()).hexdigest(), flush=True)
'''


def render(args: argparse.Namespace) -> dict[str, object]:
    contract_text = CONTRACT.read_text(encoding="utf-8")
    contract = json.loads(contract_text)
    artifact = contract["artifact"]
    delivery = contract["delivery"]
    token = artifact["sha256"][:12]
    name = f"secondary-af3-parameter-receipt-{token}"
    config_name = f"{name}-verification"
    labels = {
        "app.kubernetes.io/name": "academic-private-artifact-verification",
        "app.kubernetes.io/part-of": "fs2-serve",
        "app.kubernetes.io/managed-by": "fs2-secondary-h100-qualification",
        "fs2.nebius.ai/artifact-id": artifact["artifact_id"],
        "fs2.nebius.ai/offline-validation": "true",
        "fs2.nebius.ai/task": args.task_id,
    }
    field = lambda field_path: {"valueFrom": {"fieldRef": {"fieldPath": field_path}}}
    env = [
        {"name": "FS2_CLAIM_UID", "value": args.claim_uid},
        {
            "name": "FS2_AUTHORIZATION_DOCUMENT_SHA256",
            "value": args.authorization_document_sha256,
        },
        {"name": "FS2_JOB_NAME", "value": name},
        {"name": "FS2_POD_NAME", **field("metadata.name")},
        {"name": "FS2_POD_UID", **field("metadata.uid")},
        {"name": "FS2_NODE_NAME", **field("spec.nodeName")},
    ]
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": config_name, "namespace": args.namespace, "labels": labels},
                "data": {
                    "verify.py": VERIFY,
                    "af3-parameter-binding.json": contract_text,
                },
            },
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {
                    "name": name,
                    "namespace": args.namespace,
                    "labels": labels,
                    "annotations": {
                        "fs2.nebius.ai/content-digest": f"sha256:{artifact['sha256']}",
                        "fs2.nebius.ai/claim-uid": args.claim_uid,
                    },
                },
                "spec": {
                    "backoffLimit": 0,
                    "activeDeadlineSeconds": 1800,
                    "ttlSecondsAfterFinished": 604800,
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "restartPolicy": "Never",
                            "automountServiceAccountToken": False,
                            "enableServiceLinks": False,
                            "nodeSelector": {
                                "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb",
                                "accelerator.fs2.nebius/pool-id": "h100-reserved-8x",
                                "storage.fs2.nebius/reference-data": "true",
                            },
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
                                "runAsUser": 12345,
                                "runAsGroup": 12345,
                                "supplementalGroups": [delivery["permissions"]["asset_gid"]],
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "containers": [
                                {
                                    "name": "verify",
                                    "image": args.image,
                                    "imagePullPolicy": "IfNotPresent",
                                    "command": ["python3", "/verification/verify.py"],
                                    "env": env,
                                    "resources": {
                                        "requests": {"cpu": "500m", "memory": "512Mi"},
                                        "limits": {"cpu": "2", "memory": "2Gi"},
                                    },
                                    "securityContext": {
                                        "allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]},
                                    },
                                    "volumeMounts": [
                                        {
                                            "name": "verification",
                                            "mountPath": "/verification",
                                            "readOnly": True,
                                        },
                                        {
                                            "name": "model-dir",
                                            "mountPath": "/models",
                                        },
                                        {
                                            "name": "parameters",
                                            "mountPath": delivery["supported_modes"][0]["consumer_path"],
                                            "subPath": delivery["supported_modes"][0]["source_sub_path"],
                                            "readOnly": True,
                                        },
                                    ],
                                }
                            ],
                            "volumes": [
                                {"name": "verification", "configMap": {"name": config_name}},
                                {"name": "model-dir", "emptyDir": {"sizeLimit": "16Mi"}},
                                {
                                    "name": "parameters",
                                    "persistentVolumeClaim": {
                                        "claimName": delivery["claim"],
                                        "readOnly": True,
                                    },
                                },
                            ],
                        },
                    },
                },
            },
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--claim-uid", required=True)
    parser.add_argument("--authorization-document-sha256", required=True)
    parser.add_argument("--namespace", default="fs2-academic-poc")
    parser.add_argument(
        "--image",
        default="cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane@sha256:069c7ceb9e5e910a7c3f764687b861ff3e461445f46f4d0e7469c23fcbf60b83",
    )
    parser.add_argument(
        "--task-id", default="fs2-secondary-scientific-h100-qualification-r20260904"
    )
    args = parser.parse_args()
    print(json.dumps(render(args), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

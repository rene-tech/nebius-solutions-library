#!/usr/bin/env python3
"""Render one exact-artifact H100 semantic qualification Job.

The renderer consumes live, content-addressed binding evidence and refuses to
render a model whose artifact set is incomplete.  It is intentionally limited
to task-owned, route-free Jobs; it does not create model services or mutate the
controller configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINDINGS = (
    ROOT / "evidence/live-h100-20260904/artifact-bindings.partial.json"
)
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
MODEL_ID = "openfold3-openbind"
RUNTIME_ID = "openfold3"
SEED = 101
RAW_INPUT = {
    "queries": {
        "qualification": {
            "chains": [
                {
                    "chain_ids": ["A"],
                    "molecule_type": "protein",
                    "sequence": "MKTIIALSYIFCLVFADYKDDDDK",
                }
            ]
        }
    }
}

VERIFY_OUTPUTS = r'''#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time

import torch
from result_contract import validate_confidence_envelope


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


parser = argparse.ArgumentParser()
parser.add_argument("--runtime-id", required=True)
parser.add_argument("--output-dir", type=Path, required=True)
parser.add_argument("--seeds", required=True)
parser.add_argument("--samples-per-seed", type=int, required=True)
parser.add_argument("--image", required=True)
parser.add_argument("--runtime-marker", type=Path, required=True)
args = parser.parse_args()

seeds = [int(item) for item in args.seeds.split(",")]
confidence_path = args.output_dir / "confidence.json"
confidence = json.loads(confidence_path.read_text(encoding="utf-8"))
validated = validate_confidence_envelope(
    args.output_dir,
    confidence,
    expected_runtime_id=args.runtime_id,
    expected_seeds=seeds,
    expected_samples_per_seed=args.samples_per_seed,
)
structures = []
for result in validated["results"]:
    path = args.output_dir / result["structure"]["filename"]
    atoms = sum(
        1
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.lstrip().startswith(("ATOM ", "HETATM "))
    )
    if atoms < 10:
        raise SystemExit(f"semantic structure has fewer than 10 atom records: {path}")
    structures.append(
        {
            "path": str(path.relative_to(args.output_dir)),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "atom_records": atoms,
        }
    )

started = float(os.environ["FS2_QUALIFICATION_STARTED_EPOCH"])
result = {
    "schema": "fs2.nebius.ai/structure-secondary-h100-semantic-result/v1",
    "status": "passed",
    "runtime_id": args.runtime_id,
    "image": args.image,
    "source_commit": os.environ["FS2_QUALIFICATION_SOURCE_COMMIT"],
    "operation_id": os.environ["FS2_OPERATION_ID"],
    "attempt_id": os.environ["FS2_ATTEMPT_ID"],
    "accelerator": {
        "device_name": torch.cuda.get_device_name(0),
        "compute_capability": list(torch.cuda.get_device_capability(0)),
        "torch": torch.__version__,
        "cuda_runtime": torch.version.cuda,
    },
    "runtime_identity": {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "supplemental_groups": sorted(os.getgroups()),
    },
    "duration_seconds": round(time.time() - started, 3),
    "confidence": {
        "path": "confidence.json",
        "sha256": sha256_file(confidence_path),
        "result_count": len(validated["results"]),
    },
    "runtime_localization_marker_sha256": sha256_file(args.runtime_marker),
    "structures": structures,
}
destination = args.output_dir.parent / "qualification-result.json"
destination.write_text(
    json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
print("FS2_QUALIFICATION_RESULT " + json.dumps(result, sort_keys=True, separators=(",", ":")))
'''


class RenderError(RuntimeError):
    """The requested run cannot be bound to complete immutable artifacts."""


def canonical(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _load_bindings(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != "fs2.nebius.ai/structure-secondary-live-artifact-bindings/v1":
        raise RenderError("unsupported live artifact binding schema")
    model = document.get("models", {}).get(MODEL_ID)
    if not isinstance(model, dict) or model.get("artifact_binding_state") != "complete":
        raise RenderError(f"{MODEL_ID} does not have a complete live artifact binding")
    artifacts = model.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise RenderError(f"{MODEL_ID} must bind exactly two runtime artifacts")
    return document, model


def _runtime_marker(model: dict[str, Any], token: str) -> dict[str, Any]:
    artifacts = []
    for item in model["artifacts"]:
        for field in ("content_digest", "localization_receipt_digest"):
            if not isinstance(item.get(field), str) or SHA256.fullmatch(item[field]) is None:
                raise RenderError(f"{item.get('artifact_id')}: invalid {field}")
        receipt = item["localization_receipt_digest"].removeprefix("sha256:")
        artifacts.append(
            {
                "artifact_id": item["artifact_id"],
                "mount_path": item["mount_path"],
                "content_digest": item["content_digest"],
                "localization_receipt_digest": item["localization_receipt_digest"],
                "sub_path": None,
                "expected_manifest_sha256": None,
                "readiness_receipt_sha256": receipt,
                "authorization_receipt_sha256": None,
            }
        )
    return {
        "schema": "fs2-serve.nebius.ai/runtime-localization-marker/v1",
        "operation_id": str(uuid5(NAMESPACE_URL, f"fs2-secondary/{token}/operation")),
        "attempt_id": str(uuid5(NAMESPACE_URL, f"fs2-secondary/{token}/attempt")),
        "tenant_id": "qualification",
        "model_id": MODEL_ID,
        "variant_id": model["variant_id"],
        "stage_id": model["stage_id"],
        "artifacts": artifacts,
    }


def _command(
    *, image: str, raw_sha256: str, token: str, source_commit: str
) -> str:
    prepared_id = f"openfold3-prepared-{token}"
    raw_id = f"openfold3-raw-{token}"
    return f"""\
set -euo pipefail
mkdir -p /work/prepared /outputs/semantic
test "$(sha256sum /var/run/fs2-source/raw-input.json | cut -d' ' -f1)" = "{raw_sha256}"
/usr/local/bin/fs2-run-openfold3 prepare \\
  --input-manifest /var/run/fs2-source/raw-input.json \\
  --query-json /work/prepared/query.json \\
  --base-runner-yaml /opt/fs2/runtime/openfold3/runner-base.yaml \\
  --runner-yaml /work/prepared/runner.yaml \\
  --provenance-marker /work/prepared/provenance.json \\
  --handoff-tar /work/prepared/handoff.tar.zst \\
  --output-artifact-id {prepared_id} \\
  --raw-input-artifact-id {raw_id} \\
  --raw-input-sha256 {raw_sha256} \\
  --msa-mode none --model-seeds {SEED} --offline
export FS2_QUALIFICATION_STARTED_EPOCH="$(date +%s.%N)"
/usr/local/bin/fs2-run-openfold3 predict \\
  --query-json /work/prepared/query.json \\
  --provenance-marker /work/prepared/provenance.json \\
  --input-artifact-id {prepared_id} \\
  --expected-raw-input-artifact-id {raw_id} \\
  --expected-raw-input-sha256 {raw_sha256} \\
  --output-dir /outputs/semantic \\
  --checkpoint /models/openfold3/of3-ob-2025-06-30-174k.pt \\
  --ccd-path /databases/openfold3/components.bcif \\
  --runner-yaml /work/prepared/runner.yaml \\
  --base-runner-yaml /opt/fs2/runtime/openfold3/runner-base.yaml \\
  --num-diffusion-samples 1 --num-model-seeds 1 --model-seeds {SEED} \\
  --msa-mode none --use-templates false \\
  --runtime-localization-marker /var/run/fs2-runtime-localization.json
PYTHONPATH=/opt/fs2 python /var/run/fs2-source/verify_outputs.py \\
  --runtime-id {RUNTIME_ID} --output-dir /outputs/semantic \\
  --seeds {SEED} --samples-per-seed 1 --image {image} \\
  --runtime-marker /var/run/fs2-runtime-localization.json
"""


def render(args: argparse.Namespace) -> dict[str, Any]:
    bindings, model = _load_bindings(args.bindings)
    image = model.get("image")
    if not isinstance(image, str) or "@" not in image or SHA256.fullmatch(image.rsplit("@", 1)[1]) is None:
        raise RenderError("model image must be pinned by SHA-256 digest")
    token_material = image + "\n" + "\n".join(
        item["localization_receipt_digest"] for item in model["artifacts"]
    )
    token = hashlib.sha256(token_material.encode()).hexdigest()[:12]
    name = f"secondary-openfold3-{token}"
    config_name = f"{name}-input"
    raw_text = canonical(RAW_INPUT)
    raw_sha256 = hashlib.sha256(raw_text.encode()).hexdigest()
    marker = _runtime_marker(model, token)
    source_commit = bindings["source_commit"]

    mounts = []
    for item in model["artifacts"]:
        sub_path = item.get("object_sub_path")
        if not isinstance(sub_path, str) or sub_path.startswith("/") or ".." in sub_path.split("/"):
            raise RenderError(f"{item.get('artifact_id')}: unsafe physical object subPath")
        mounts.append(
            {
                "name": "public-artifacts",
                "mountPath": item["mount_path"],
                "subPath": sub_path,
                "readOnly": True,
            }
        )
    labels = {
        "app.kubernetes.io/name": "structure-secondary-qualification",
        "app.kubernetes.io/part-of": "fs2-serve",
        "app.kubernetes.io/managed-by": "fs2-secondary-h100-qualification",
        "fs2.nebius.ai/model-id": MODEL_ID,
        "fs2.nebius.ai/offline-validation": "true",
        "fs2.nebius.ai/task": args.task_id,
    }
    environment = [
        {"name": "FS2_OPERATION_ID", "value": marker["operation_id"]},
        {"name": "FS2_ATTEMPT_ID", "value": marker["attempt_id"]},
        {"name": "FS2_TENANT_ID", "value": marker["tenant_id"]},
        {"name": "FS2_VARIANT_ID", "value": marker["variant_id"]},
        {"name": "FS2_STAGE_ID", "value": marker["stage_id"]},
        {
            "name": "FS2_RUNTIME_LOCALIZATION_MARKER",
            "value": "/var/run/fs2-runtime-localization.json",
        },
        {"name": "FS2_QUALIFICATION_SOURCE_COMMIT", "value": source_commit},
        {"name": "FS2_NETWORK_MODE", "value": "offline"},
        {"name": "HF_HUB_OFFLINE", "value": "1"},
        {"name": "TRANSFORMERS_OFFLINE", "value": "1"},
        {"name": "HOME", "value": "/work/home"},
        {"name": "TMPDIR", "value": "/tmp"},
        {"name": "XDG_CACHE_HOME", "value": "/cache/openfold3/xdg"},
        {"name": "TRITON_CACHE_DIR", "value": "/cache/openfold3/triton"},
        {
            "name": "TORCH_EXTENSIONS_DIR",
            "value": "/cache/openfold3/torch-extensions",
        },
    ]
    volumes = [
        {
            "name": "public-artifacts",
            "hostPath": {"path": bindings["public_storage"]["host_path"], "type": "Directory"},
        },
        {"name": "input", "configMap": {"name": config_name, "defaultMode": 292}},
        {"name": "work", "emptyDir": {"sizeLimit": "16Gi"}},
        {"name": "outputs", "emptyDir": {"sizeLimit": "16Gi"}},
        {"name": "cache", "emptyDir": {"sizeLimit": "32Gi"}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "16Gi"}},
    ]
    main_mounts = [
        {"name": "input", "mountPath": "/var/run/fs2-source", "readOnly": True},
        {
            "name": "input",
            "mountPath": "/var/run/fs2-runtime-localization.json",
            "subPath": "runtime-localization.json",
            "readOnly": True,
        },
        {"name": "work", "mountPath": "/work"},
        {"name": "outputs", "mountPath": "/outputs"},
        {"name": "cache", "mountPath": "/cache"},
        {"name": "tmp", "mountPath": "/tmp"},
        *mounts,
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
                    "raw-input.json": raw_text,
                    "runtime-localization.json": canonical(marker),
                    "verify_outputs.py": VERIFY_OUTPUTS,
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
                        "fs2.nebius.ai/image-digest": image.rsplit("@", 1)[1],
                        "fs2.nebius.ai/source-commit": source_commit,
                    },
                },
                "spec": {
                    "backoffLimit": 0,
                    "activeDeadlineSeconds": 7200,
                    "ttlSecondsAfterFinished": 604800,
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "restartPolicy": "Never",
                            "automountServiceAccountToken": False,
                            "enableServiceLinks": False,
                            "terminationGracePeriodSeconds": 120,
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
                                "runAsUser": 10001,
                                "runAsGroup": 10001,
                                "fsGroup": 10001,
                                "fsGroupChangePolicy": "OnRootMismatch",
                                "supplementalGroups": [1000],
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "containers": [
                                {
                                    "name": "semantic",
                                    "image": image,
                                    "imagePullPolicy": "IfNotPresent",
                                    "args": [
                                        "/bin/bash",
                                        "-lc",
                                        _command(
                                            image=image,
                                            raw_sha256=raw_sha256,
                                            token=token,
                                            source_commit=source_commit,
                                        ),
                                    ],
                                    "env": environment,
                                    "resources": {
                                        "requests": {
                                            "cpu": "8",
                                            "memory": "64Gi",
                                            "nvidia.com/gpu": "1",
                                        },
                                        "limits": {
                                            "cpu": "16",
                                            "memory": "96Gi",
                                            "nvidia.com/gpu": "1",
                                        },
                                    },
                                    "securityContext": {
                                        "allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]},
                                    },
                                    "volumeMounts": main_mounts,
                                }
                            ],
                            "volumes": volumes,
                        },
                    },
                },
            },
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bindings", type=Path, default=DEFAULT_BINDINGS)
    parser.add_argument("--namespace", default="fs2-academic-poc")
    parser.add_argument(
        "--task-id", default="fs2-secondary-scientific-h100-qualification-r20260904"
    )
    args = parser.parse_args()
    print(json.dumps(render(args), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

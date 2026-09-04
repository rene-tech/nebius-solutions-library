#!/usr/bin/env python3
"""Render exact-artifact H100 qualification Jobs for ESMFold2 and Protenix.

This renderer is intentionally route-free.  It consumes the live artifact
binding receipt, refuses incomplete or mutable identities, and emits only a
task-owned ConfigMap and one finite semantic Job.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from render_semantic_job import RenderError, SHA256, VERIFY_OUTPUTS, canonical


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINDINGS = ROOT / "evidence/live-h100-20260904/artifact-bindings.partial.json"
PROTENIX_FIXTURE = (
    ROOT.parents[3] / "model-artifacts/smoke/protenix-v2-minimal.json"
)
SEED = 101
SOURCE_REVISIONS = {
    "esmfold2": "827ec128e4cdaf80f7d6f95fb367a08980b34918",
    "esmfold2-fast": "827ec128e4cdaf80f7d6f95fb367a08980b34918",
    "protenix-v2": "2475421477ab414b571149ad4a875c390ff8a35d",
}
MODEL_ARTIFACT_DIRS = {
    "esmfold2": "/models/esmfold2",
    "esmfold2-fast": "/models/esmfold2-fast",
}
ESM_INPUT = {
    "sequences": [
        {
            "id": "A",
            "sequence": "MKTAYIAKQRQISFVKSHFSRQDILDLWIYHTQGYFP",
            "type": "protein",
        }
    ]
}
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _load_bindings(path: Path, model_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != "fs2.nebius.ai/structure-secondary-live-artifact-bindings/v1":
        raise RenderError("unsupported live artifact binding schema")
    model = document.get("models", {}).get(model_id)
    if not isinstance(model, dict) or model.get("artifact_binding_state") != "complete":
        raise RenderError(f"{model_id} does not have a complete live artifact binding")
    artifacts = model.get("artifacts")
    expected_count = 1 if model_id == "protenix-v2" else 3
    if not isinstance(artifacts, list) or len(artifacts) != expected_count:
        raise RenderError(f"{model_id} must bind exactly {expected_count} runtime artifacts")
    return document, model


def _marker(
    model_id: str,
    model: dict[str, Any],
    token: str,
    stage_id: str,
) -> dict[str, Any]:
    artifacts = []
    for item in model["artifacts"]:
        for field in ("content_digest", "localization_receipt_digest"):
            if not isinstance(item.get(field), str) or SHA256.fullmatch(item[field]) is None:
                raise RenderError(f"{item.get('artifact_id')}: invalid {field}")
        manifest = item.get("expected_manifest_sha256")
        if manifest is not None and (
            not isinstance(manifest, str) or HEX_SHA256.fullmatch(manifest) is None
        ):
            raise RenderError(f"{item.get('artifact_id')}: invalid expected manifest SHA-256")
        artifacts.append(
            {
                "artifact_id": item["artifact_id"],
                "mount_path": item["mount_path"],
                "content_digest": item["content_digest"],
                "localization_receipt_digest": item["localization_receipt_digest"],
                "sub_path": None,
                "expected_manifest_sha256": manifest,
                "readiness_receipt_sha256": item[
                    "localization_receipt_digest"
                ].removeprefix("sha256:"),
                "authorization_receipt_sha256": None,
            }
        )
    return {
        "schema": "fs2-serve.nebius.ai/runtime-localization-marker/v1",
        "operation_id": str(
            uuid5(NAMESPACE_URL, f"fs2-secondary/{model_id}/{token}/operation")
        ),
        "attempt_id": str(
            uuid5(NAMESPACE_URL, f"fs2-secondary/{model_id}/{token}/{stage_id}")
        ),
        "tenant_id": "qualification",
        "model_id": model_id,
        "variant_id": model["variant_id"],
        "stage_id": stage_id,
        "artifacts": artifacts,
    }


def _export_marker(marker: dict[str, Any], path: str) -> str:
    return (
        f"export FS2_OPERATION_ID={marker['operation_id']} "
        f"FS2_ATTEMPT_ID={marker['attempt_id']} FS2_TENANT_ID=qualification "
        f"FS2_VARIANT_ID={marker['variant_id']} FS2_STAGE_ID={marker['stage_id']} "
        f"FS2_RUNTIME_LOCALIZATION_MARKER={path}"
    )


def _esm_command(
    model_id: str,
    image: str,
    raw_sha256: str,
    token: str,
    marker: dict[str, Any],
    source_commit: str,
) -> str:
    prepared_id = f"{model_id}-prepared-{token}"
    raw_id = f"{model_id}-raw-{token}"
    marker_path = "/var/run/fs2-runtime-fold.json"
    return f"""\
set -euo pipefail
mkdir -p /work/prepared /outputs/semantic /cache/esm
test "$(sha256sum /var/run/fs2-source/raw-input.json | cut -d' ' -f1)" = "{raw_sha256}"
export FS2_QUALIFICATION_STARTED_EPOCH="$(date +%s.%N)"
{_export_marker(marker, marker_path)}
/usr/local/bin/fs2-run-esmfold2 prepare-input \
  --input-manifest /var/run/fs2-source/raw-input.json \
  --output /work/prepared/request.json \
  --output-artifact-id {prepared_id} \
  --raw-input-artifact-id {raw_id} \
  --raw-input-sha256 {raw_sha256} \
  --variant {model_id} --source-revision {SOURCE_REVISIONS[model_id]} \
  --mode single-sequence --seed {SEED}
/usr/local/bin/fs2-run-esmfold2 fold \
  --request /work/prepared/request.json --output-dir /outputs/semantic \
  --model-dir {MODEL_ARTIFACT_DIRS[model_id]} \
  --esmc-dir /models/esmc-6b --ccd-path /databases/esmfold2/ccd.pkl \
  --hardware-mode h100 --esmc-precision bf16 --smoke --seed {SEED} \
  --complex-id fs2-secondary-{model_id} --variant {model_id} \
  --runtime-localization-marker {marker_path} \
  --input-artifact-id {prepared_id} \
  --expected-raw-input-artifact-id {raw_id} \
  --expected-raw-input-sha256 {raw_sha256} \
  --expected-mode single-sequence --source-revision {SOURCE_REVISIONS[model_id]}
PYTHONPATH=/opt/fs2 python /var/run/fs2-source/verify_outputs.py \
  --runtime-id {model_id} --output-dir /outputs/semantic \
  --seeds {SEED} --samples-per-seed 1 --image {image} \
  --runtime-marker {marker_path}
"""


def _protenix_command(
    image: str,
    raw_sha256: str,
    token: str,
    prep_marker: dict[str, Any],
    pred_marker: dict[str, Any],
    source_commit: str,
) -> str:
    prepared_id = f"protenix-v2-prepared-{token}"
    raw_id = f"protenix-v2-raw-{token}"
    prep_path = "/var/run/fs2-runtime-prepare-data.json"
    pred_path = "/var/run/fs2-runtime-sample-structure.json"
    return f"""\
set -euo pipefail
mkdir -p /work/prepared /outputs/semantic /cache/protenix/triton /cache/protenix/cueq
test "$(sha256sum /var/run/fs2-source/raw-input.json | cut -d' ' -f1)" = "{raw_sha256}"
export FS2_QUALIFICATION_STARTED_EPOCH="$(date +%s.%N)"
{_export_marker(prep_marker, prep_path)}
/usr/local/bin/fs2-run-protenix prep \
  --input /var/run/fs2-source/raw-input.json \
  --output-dir /work/prepared/upstream \
  --processed-json /work/prepared/processed.json \
  --provenance-marker /work/prepared/provenance.json \
  --handoff-tar /work/prepared/handoff.tar.zst \
  --output-artifact-id {prepared_id} \
  --raw-input-artifact-id {raw_id} --raw-input-sha256 {raw_sha256} \
  --msa-mode none --reference-root /models/protenix-v2 \
  --reference-manifest /models/protenix-v2/manifest.json \
  --runtime-localization-marker {prep_path}
{_export_marker(pred_marker, pred_path)}
/usr/local/bin/fs2-run-protenix pred \
  --input /work/prepared/processed.json \
  --input-marker /work/prepared/provenance.json \
  --input-artifact-id {prepared_id} \
  --expected-raw-input-artifact-id {raw_id} \
  --expected-raw-input-sha256 {raw_sha256} \
  --output-dir /outputs/semantic \
  --checkpoint /models/protenix-v2/checkpoint/protenix-v2.pt \
  --common-dir /models/protenix-v2/common --msa-mode none \
  --seeds {SEED} --sample-count 1 --disable-templates --disable-rna-msa \
  --runtime-localization-marker {pred_path}
PYTHONPATH=/opt/fs2 python /var/run/fs2-source/verify_outputs.py \
  --runtime-id protenix-v2 --output-dir /outputs/semantic \
  --seeds {SEED} --samples-per-seed 1 --image {image} \
  --runtime-marker {pred_path}
"""


def render(args: argparse.Namespace) -> dict[str, Any]:
    document, model = _load_bindings(args.bindings, args.model)
    image = model.get("image")
    if (
        not isinstance(image, str)
        or "@" not in image
        or SHA256.fullmatch(image.rsplit("@", 1)[1]) is None
    ):
        raise RenderError("model image must be pinned by SHA-256 digest")
    token_material = args.model + "\n" + image + "\n" + "\n".join(
        item["localization_receipt_digest"] for item in model["artifacts"]
    )
    token = hashlib.sha256(token_material.encode()).hexdigest()[:12]
    name = f"secondary-{args.model}-{token}"
    config_name = f"{name}-input"
    if args.model == "protenix-v2":
        raw_text = PROTENIX_FIXTURE.read_text(encoding="utf-8")
    else:
        raw_text = canonical(ESM_INPUT)
    raw_sha256 = hashlib.sha256(raw_text.encode()).hexdigest()

    stage_markers: dict[str, dict[str, Any]]
    if args.model == "protenix-v2":
        stage_markers = {
            "prepare-data": _marker(args.model, model, token, "prepare-data"),
            "sample-structure": _marker(
                args.model, model, token, "sample-structure"
            ),
        }
        command = _protenix_command(
            image,
            raw_sha256,
            token,
            stage_markers["prepare-data"],
            stage_markers["sample-structure"],
            document["source_commit"],
        )
    else:
        stage_markers = {"fold": _marker(args.model, model, token, "fold")}
        command = _esm_command(
            args.model,
            image,
            raw_sha256,
            token,
            stage_markers["fold"],
            document["source_commit"],
        )

    artifact_mounts = []
    for item in model["artifacts"]:
        sub_path = item.get("object_sub_path")
        if (
            not isinstance(sub_path, str)
            or sub_path.startswith("/")
            or ".." in sub_path.split("/")
        ):
            raise RenderError(f"{item.get('artifact_id')}: unsafe physical object subPath")
        artifact_mounts.append(
            {
                "name": "public-artifacts",
                "mountPath": item["mount_path"],
                "subPath": sub_path,
                "readOnly": True,
            }
        )
    marker_mounts = [
        {
            "name": "input",
            "mountPath": f"/var/run/fs2-runtime-{stage}.json",
            "subPath": f"runtime-{stage}.json",
            "readOnly": True,
        }
        for stage in stage_markers
    ]
    labels = {
        "app.kubernetes.io/name": "structure-secondary-qualification",
        "app.kubernetes.io/part-of": "fs2-serve",
        "app.kubernetes.io/managed-by": "fs2-secondary-h100-qualification",
        "fs2.nebius.ai/model-id": args.model,
        "fs2.nebius.ai/offline-validation": "true",
        "fs2.nebius.ai/task": args.task_id,
    }
    cache_root = "/cache/protenix" if args.model == "protenix-v2" else "/cache/esm"
    environment = [
        {"name": "FS2_QUALIFICATION_SOURCE_COMMIT", "value": document["source_commit"]},
        {"name": "FS2_NETWORK_MODE", "value": "offline"},
        {"name": "HF_HUB_OFFLINE", "value": "1"},
        {"name": "TRANSFORMERS_OFFLINE", "value": "1"},
        {"name": "HOME", "value": "/work/home"},
        {"name": "TMPDIR", "value": "/tmp"},
        {"name": "XDG_CACHE_HOME", "value": f"{cache_root}/xdg"},
        {"name": "TRITON_CACHE_DIR", "value": f"{cache_root}/triton"},
        {"name": "CUEQ_TRITON_CACHE_DIR", "value": f"{cache_root}/cueq"},
        {"name": "TORCH_EXTENSIONS_DIR", "value": f"{cache_root}/torch-extensions"},
    ]
    config_data = {
        "raw-input.json": raw_text,
        "verify_outputs.py": VERIFY_OUTPUTS,
        **{
            f"runtime-{stage}.json": canonical(marker)
            for stage, marker in stage_markers.items()
        },
    }
    volumes = [
        {
            "name": "public-artifacts",
            "hostPath": {"path": document["public_storage"]["host_path"], "type": "Directory"},
        },
        {"name": "input", "configMap": {"name": config_name, "defaultMode": 292}},
        {"name": "work", "emptyDir": {"sizeLimit": "16Gi"}},
        {"name": "outputs", "emptyDir": {"sizeLimit": "32Gi"}},
        {"name": "cache", "emptyDir": {"sizeLimit": "32Gi"}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "16Gi"}},
    ]
    main_mounts = [
        {"name": "input", "mountPath": "/var/run/fs2-source", "readOnly": True},
        *marker_mounts,
        {"name": "work", "mountPath": "/work"},
        {"name": "outputs", "mountPath": "/outputs"},
        {"name": "cache", "mountPath": "/cache"},
        {"name": "tmp", "mountPath": "/tmp"},
        *artifact_mounts,
    ]
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": config_name, "namespace": args.namespace, "labels": labels},
                "data": config_data,
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
                        "fs2.nebius.ai/source-commit": document["source_commit"],
                    },
                },
                "spec": {
                    "backoffLimit": 0,
                    "activeDeadlineSeconds": 14400,
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
                                    "args": ["/bin/bash", "-lc", command],
                                    "env": environment,
                                    "resources": {
                                        "requests": {
                                            "cpu": "8",
                                            "memory": "64Gi",
                                            "nvidia.com/gpu": "1",
                                        },
                                        "limits": {
                                            "cpu": "16",
                                            "memory": "112Gi",
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
    parser.add_argument(
        "--model", choices=("esmfold2", "esmfold2-fast", "protenix-v2"), required=True
    )
    parser.add_argument("--bindings", type=Path, default=DEFAULT_BINDINGS)
    parser.add_argument("--namespace", default="fs2-academic-poc")
    parser.add_argument(
        "--task-id", default="fs2-secondary-scientific-h100-qualification-r20260904"
    )
    args = parser.parse_args()
    print(json.dumps(render(args), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

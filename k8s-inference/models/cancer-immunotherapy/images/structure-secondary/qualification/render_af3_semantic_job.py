#!/usr/bin/env python3
"""Render the route-free exact-digest AlphaFold 3 H100 semantic Job."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINDINGS = ROOT / "evidence/live-h100-20260904/artifact-bindings.partial.json"
PARAMETER_RECEIPT = (
    ROOT
    / "evidence/live-h100-20260904/alphafold3-parameter-localization-receipt.json"
)
FOLD_INPUT = ROOT.parents[1] / "fast-start-campaign/af3-fold-input.json"
SHA256 = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")


class RenderError(RuntimeError):
    """The live AF3 binding is incomplete or mutable."""


VERIFY_RESULT = r'''#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


output = Path("/output")
params = json.loads((output / "params-load-receipt.json").read_text(encoding="utf-8"))
inference = json.loads((output / "inference-receipt.json").read_text(encoding="utf-8"))
if params.get("mode") != "params-load" or params.get("status") != "PASS":
    raise SystemExit("AlphaFold 3 parameter-load receipt is not PASS")
if inference.get("mode") != "inference" or inference.get("status") != "PASS":
    raise SystemExit("AlphaFold 3 inference receipt is not PASS")
execution = inference.get("execution", {})
if execution.get("exit_code") != 0 or execution.get("terminal_state") != "succeeded":
    raise SystemExit("AlphaFold 3 upstream inference did not terminate successfully")
expected_parameter = os.environ["FS2_AF3_PARAMETER_SHA256"]
for receipt in (params, inference):
    parameter = receipt.get("parameters", {})
    if parameter.get("sha256") != expected_parameter or not parameter.get("deep_verified"):
        raise SystemExit("AlphaFold 3 receipt lacks the exact deep-verified parameter identity")
localization_path = Path("/var/run/fs2-source/parameter-localization-receipt.json")
if sha256_file(localization_path) != os.environ["FS2_AF3_LOCALIZATION_RECEIPT_SHA256"]:
    raise SystemExit("AlphaFold 3 localization receipt differs from the frozen binding")
cif_files = []
for path in sorted((output / "inference").rglob("*.cif")):
    text = path.read_text(encoding="utf-8", errors="replace")
    atoms = sum(
        1
        for line in text.splitlines()
        if line.startswith(("ATOM ", "HETATM ")) or line.lstrip().startswith(("ATOM ", "HETATM "))
    )
    cif_files.append(
        {
            "path": str(path.relative_to(output)),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "atom_records": atoms,
        }
    )
if not cif_files or max(item["bytes"] for item in cif_files) < 1000:
    raise SystemExit("AlphaFold 3 produced no substantive CIF structure")
result = {
    "schema": "fs2.nebius.ai/alphafold3-h100-semantic-qualification/v1",
    "terminal_state": "PASS",
    "image": os.environ["FS2_AF3_IMAGE"],
    "parameter_content_sha256": expected_parameter,
    "parameter_localization_receipt_sha256": sha256_file(localization_path),
    "runtime_identity": {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "supplemental_groups": sorted(os.getgroups()),
    },
    "duration_seconds": round(time.time() - float(os.environ["FS2_QUALIFICATION_STARTED_EPOCH"]), 3),
    "params_load": {
        "receipt_sha256": sha256_file(output / "params-load-receipt.json"),
        "semantic": params.get("semantic"),
        "devices": params.get("devices"),
    },
    "inference": {
        "receipt_sha256": sha256_file(output / "inference-receipt.json"),
        "execution": execution,
        "structures": cif_files,
    },
}
(output / "qualification-result.json").write_text(
    json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
print("FS2_QUALIFICATION_RESULT " + json.dumps(result, sort_keys=True, separators=(",", ":")))
'''


def _load(path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    bindings = json.loads(path.read_text(encoding="utf-8"))
    if bindings.get("schema") != "fs2.nebius.ai/structure-secondary-live-artifact-bindings/v1":
        raise RenderError("unsupported live artifact bindings schema")
    model = bindings.get("alphafold3_external_bindings")
    if not isinstance(model, dict) or model.get("artifact_binding_state") != (
        "complete-exact-identities-separate-contracts"
    ):
        raise RenderError("AlphaFold 3 live artifact bindings are incomplete")
    parameters = model.get("parameters")
    if not isinstance(parameters, dict):
        raise RenderError("AlphaFold 3 parameter binding is absent")
    for field in ("content_digest", "localization_receipt_digest"):
        if not isinstance(parameters.get(field), str) or not SHA256.fullmatch(parameters[field]):
            raise RenderError(f"AlphaFold 3 parameter {field} is not immutable")
    image = model.get("image")
    if not isinstance(image, str) or "@" not in image or not SHA256.fullmatch(image.rsplit("@", 1)[1]):
        raise RenderError("AlphaFold 3 image is not digest pinned")
    receipt = json.loads(PARAMETER_RECEIPT.read_text(encoding="utf-8"))
    receipt_digest = hashlib.sha256(PARAMETER_RECEIPT.read_bytes()).hexdigest()
    if receipt_digest != parameters["localization_receipt_digest"].removeprefix("sha256:"):
        raise RenderError("committed AlphaFold 3 parameter receipt differs from the live binding")
    return bindings, model, receipt


def render(args: argparse.Namespace) -> dict[str, Any]:
    _bindings, model, _parameter_receipt = _load(args.bindings)
    parameters = model["parameters"]
    image = model["image"]
    token = hashlib.sha256(
        (image + "\n" + parameters["content_digest"] + "\n" + parameters["localization_receipt_digest"]).encode()
    ).hexdigest()[:12]
    name = f"secondary-alphafold3-{token}"
    input_name = f"{name}-input"
    labels = {
        "app.kubernetes.io/name": "structure-secondary-qualification",
        "app.kubernetes.io/part-of": "fs2-serve",
        "app.kubernetes.io/managed-by": "fs2-secondary-h100-qualification",
        "fs2.nebius.ai/model-id": "alphafold3",
        "fs2.nebius.ai/offline-validation": "true",
        "fs2.nebius.ai/task": args.task_id,
    }
    command = """\
set -euo pipefail
mkdir -p /cache/alphafold3/jax /cache/alphafold3/triton /cache/alphafold3/xdg /scratch/home /output/inference
export FS2_QUALIFICATION_STARTED_EPOCH="$(date +%s.%N)"
/alphafold3_venv/bin/python3 /opt/fs2/af3_runtime.py params-load \\
  --parameter-path /models/af3.bin.zst --deep-verify \\
  --receipt /output/params-load-receipt.json
/alphafold3_venv/bin/python3 /opt/fs2/af3_runtime.py inference \\
  --json-path /var/run/fs2-source/fold-input.json \\
  --parameter-path /models/af3.bin.zst --output-dir /output/inference \\
  --flash-attention triton --deep-verify \\
  --extra-arg=--num_diffusion_samples=1 \\
  --receipt /output/inference-receipt.json
/alphafold3_venv/bin/python3 /var/run/fs2-source/verify-result.py
"""
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": input_name, "namespace": args.namespace, "labels": labels},
                "data": {
                    "fold-input.json": FOLD_INPUT.read_text(encoding="utf-8"),
                    "parameter-localization-receipt.json": PARAMETER_RECEIPT.read_text(
                        encoding="utf-8"
                    ),
                    "verify-result.py": VERIFY_RESULT,
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
                        "fs2.nebius.ai/parameter-content-digest": parameters["content_digest"],
                        "fs2.nebius.ai/localization-receipt-digest": parameters["localization_receipt_digest"],
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
                            "terminationGracePeriodSeconds": 120,
                            "nodeSelector": {
                                "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb",
                                "accelerator.fs2.nebius/pool-id": "h100-reserved-8x",
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
                                "supplementalGroups": [65532],
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "containers": [
                                {
                                    "name": "semantic",
                                    "image": image,
                                    "imagePullPolicy": "IfNotPresent",
                                    "command": ["/bin/bash", "-lc", command],
                                    "workingDir": "/app/alphafold",
                                    "env": [
                                        {"name": "FS2_AF3_IMAGE", "value": image},
                                        {"name": "FS2_AF3_PARAMETER_PATH", "value": "/models/af3.bin.zst"},
                                        {"name": "FS2_AF3_PARAMETER_SHA256", "value": parameters["content_digest"].removeprefix("sha256:")},
                                        {"name": "FS2_AF3_LOCALIZATION_RECEIPT_SHA256", "value": parameters["localization_receipt_digest"].removeprefix("sha256:")},
                                        {"name": "FS2_AF3_REFERENCE_MOUNT", "value": "/reference-data"},
                                        {"name": "HOME", "value": "/scratch/home"},
                                        {"name": "TMPDIR", "value": "/scratch"},
                                        {"name": "XDG_CACHE_HOME", "value": "/cache/alphafold3/xdg"},
                                        {"name": "JAX_COMPILATION_CACHE_DIR", "value": "/cache/alphafold3/jax"},
                                        {"name": "TRITON_CACHE_DIR", "value": "/cache/alphafold3/triton"},
                                    ],
                                    "resources": {
                                        "requests": {"cpu": "8", "memory": "64Gi", "nvidia.com/gpu": "1"},
                                        "limits": {"cpu": "16", "memory": "112Gi", "nvidia.com/gpu": "1"},
                                    },
                                    "securityContext": {
                                        "allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]},
                                    },
                                    "volumeMounts": [
                                        {"name": "input", "mountPath": "/var/run/fs2-source", "readOnly": True},
                                        {"name": "parameters", "mountPath": parameters["mount_path"], "subPath": parameters["sub_path"], "readOnly": True},
                                        {"name": "cache", "mountPath": "/cache"},
                                        {"name": "scratch", "mountPath": "/scratch"},
                                        {"name": "output", "mountPath": "/output"},
                                    ],
                                }
                            ],
                            "volumes": [
                                {"name": "input", "configMap": {"name": input_name, "defaultMode": 292}},
                                {"name": "parameters", "persistentVolumeClaim": {"claimName": parameters["claim"], "readOnly": True}},
                                {"name": "cache", "emptyDir": {"sizeLimit": "32Gi"}},
                                {"name": "scratch", "emptyDir": {"sizeLimit": "32Gi"}},
                                {"name": "output", "emptyDir": {"sizeLimit": "16Gi"}},
                            ],
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
    print(json.dumps(render(parser.parse_args()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

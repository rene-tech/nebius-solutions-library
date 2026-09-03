#!/usr/bin/env python3
"""Render the deterministic three-variant Proteina-Complexa H100 plan.

The plan is pure data: one ConfigMap carrying the runtime entrypoint and the
three request documents, plus one Job per variant.  Rendering is separated from
submission so the exact manifests can be reviewed, diffed and re-rendered
without touching a cluster.

Every Job is shell-free.  ``command`` is an argv list whose first element is
the interpreter and whose second element is the runtime entrypoint; no shell,
no ``-c`` string and no wrapper script is involved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
IMAGE_LOCK = HERE.parent / "image-lock.json"
ENTRYPOINT = HERE.parent / "runtime_entrypoint.py"

NAMESPACE = "fs2-academic-poc"
OWNER_TASK = "fs2-complexa-final-h100-qualification-r20260903"
ARTIFACT_CLAIM = "academic-assets-runtime-rwx"
ARTIFACT_SUBPATH = "scientific-ingestion/fs2-proteina-complexa-r20260903/staging"
OUTPUT_CLAIM = "fs2-cxq-out-r20260903"
ENTRYPOINT_PATH = "/opt/fs2/complexa/runtime_entrypoint.py"

VARIANT_TASKS = {
    "protein": "02_PDL1",
    "ligand": "39_7V11_LIGAND",
    "ame": "M0024_1nzy_og",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lock() -> dict[str, Any]:
    return json.loads(IMAGE_LOCK.read_text(encoding="utf-8"))


def request_document(variant: str, run_id: str, arguments: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema": "fs2-serve.nebius.ai/proteina-complexa-batch-request/v1",
        "run_id": run_id,
        "variant": variant,
        "task_name": VARIANT_TASKS[variant],
        "samples": arguments.samples,
        "batch_size": arguments.batch_size,
        "nsteps": arguments.nsteps,
        "seed": arguments.seed,
        "reward_model": "none",
        "search_algorithm": "single-pass",
        "verify_content_digests": arguments.verify_digests,
    }


def render(arguments: argparse.Namespace) -> dict[str, Any]:
    lock = _lock()
    image = lock["image"]
    digest = arguments.image or image.get("published_digest")
    if not digest:
        raise SystemExit(
            "no image digest: pass --image or record image.published_digest in image-lock.json"
        )
    reference = (
        digest
        if "/" in digest
        else f"{image['registry']}/{image['repository']}@{digest}"
    )

    entrypoint_source = ENTRYPOINT.read_text(encoding="utf-8")
    requests = {
        variant: request_document(variant, f"{arguments.run_prefix}-{variant}", arguments)
        for variant in VARIANT_TASKS
    }

    config_map: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"{arguments.run_prefix}-contract",
            "namespace": NAMESPACE,
            "labels": {"fs2.nebius.ai/owner-task": OWNER_TASK},
        },
        "data": {
            "runtime_entrypoint.py": entrypoint_source,
            **{f"request-{variant}.json": json.dumps(document, indent=2) + "\n"
               for variant, document in requests.items()},
        },
    }

    jobs = []
    for variant in VARIANT_TASKS:
        name = f"{arguments.run_prefix}-{variant}"
        artifact_id = f"complexa-{variant}"
        jobs.append(
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {
                    "name": name,
                    "namespace": NAMESPACE,
                    "labels": {
                        "fs2.nebius.ai/owner-task": OWNER_TASK,
                        "fs2.nebius.ai/model": "proteina-complexa",
                        "fs2.nebius.ai/variant": variant,
                    },
                },
                "spec": {
                    "backoffLimit": 0,
                    "ttlSecondsAfterFinished": 86400,
                    "template": {
                        "metadata": {
                            "labels": {
                                "fs2.nebius.ai/owner-task": OWNER_TASK,
                                "fs2.nebius.ai/variant": variant,
                            }
                        },
                        "spec": {
                            "restartPolicy": "Never",
                            # Capability-driven placement: the accelerator class
                            # label, not a hard-coded H100-only node name.
                            "nodeSelector": {"nebius.com/gpu": "true"},
                            "tolerations": [
                                {
                                    "key": "dedicated",
                                    "operator": "Equal",
                                    "value": "fs2-inference",
                                    "effect": "NoSchedule",
                                }
                            ],
                            "containers": [
                                {
                                    "name": "complexa",
                                    "image": reference,
                                    "imagePullPolicy": "IfNotPresent",
                                    "workingDir": "/workspace",
                                    "command": [
                                        "python",
                                        ENTRYPOINT_PATH,
                                        "run",
                                        "--request",
                                        f"/opt/fs2/complexa/request-{variant}.json",
                                        "--output-root",
                                        f"/workspace/{variant}",
                                        "--cache-level",
                                        arguments.cache_level,
                                    ],
                                    "env": [
                                        {"name": "HOME", "value": "/tmp/fs2-home"},
                                        {"name": "HF_HOME", "value": "/tmp/fs2-home/huggingface"},
                                        {"name": "XDG_CACHE_HOME", "value": "/tmp/fs2-home/xdg"},
                                        {"name": "MPLCONFIGDIR", "value": "/tmp/fs2-home/matplotlib"},
                                        {"name": "NUMBA_CACHE_DIR", "value": "/tmp/fs2-home/numba"},
                                        {"name": "TRITON_CACHE_DIR", "value": "/tmp/fs2-home/triton"},
                                        {"name": "DATA_PATH", "value": "/opt/fs2/source/assets"},
                                        {"name": "RF3_EXEC_PATH", "value": "/opt/venv/bin/rf3"},
                                        {
                                            "name": "RF3_CKPT_PATH",
                                            "value": (
                                                "/opt/fs2/artifacts/rosettafold3/"
                                                "rf3_foundry_01_24_latest_remapped.ckpt"
                                            ),
                                        },
                                        {"name": "PYTHONUNBUFFERED", "value": "1"},
                                    ],
                                    "resources": {
                                        "requests": {
                                            "cpu": str(arguments.cpu),
                                            "memory": arguments.memory,
                                            "nvidia.com/gpu": "1",
                                        },
                                        "limits": {
                                            "cpu": str(arguments.cpu),
                                            "memory": arguments.memory,
                                            "nvidia.com/gpu": "1",
                                        },
                                    },
                                    "volumeMounts": [
                                        {
                                            "name": "checkpoints",
                                            "mountPath": f"/opt/fs2/artifacts/{artifact_id}",
                                            "subPath": f"{ARTIFACT_SUBPATH}/{artifact_id}",
                                            "readOnly": True,
                                        },
                                        {
                                            "name": "checkpoints",
                                            "mountPath": "/opt/fs2/artifacts/rosettafold3",
                                            "subPath": f"{ARTIFACT_SUBPATH}/rosettafold3-checkpoint",
                                            "readOnly": True,
                                        },
                                        {
                                            "name": "contract",
                                            "mountPath": "/opt/fs2/complexa",
                                            "readOnly": True,
                                        },
                                        {
                                            "name": "outputs",
                                            "mountPath": "/workspace",
                                            "subPath": arguments.run_prefix,
                                        },
                                        {"name": "cache", "mountPath": "/tmp"},
                                    ],
                                }
                            ],
                            "volumes": [
                                {
                                    "name": "checkpoints",
                                    "persistentVolumeClaim": {
                                        "claimName": ARTIFACT_CLAIM,
                                        "readOnly": True,
                                    },
                                },
                                {
                                    "name": "contract",
                                    "configMap": {
                                        "name": f"{arguments.run_prefix}-contract",
                                        "defaultMode": 0o555,
                                    },
                                },
                                {
                                    "name": "outputs",
                                    "persistentVolumeClaim": {"claimName": OUTPUT_CLAIM},
                                },
                                {"name": "cache", "emptyDir": {"sizeLimit": "32Gi"}},
                            ],
                        },
                    },
                },
            }
        )

    return {
        "schema": "fs2.nebius.ai/proteina-complexa-qualification-plan/v1",
        "owner_task": OWNER_TASK,
        "rendered_from": {
            "image_lock": str(IMAGE_LOCK.relative_to(IMAGE_LOCK.parents[1])),
            "entrypoint_sha256": _sha256(ENTRYPOINT),
            "image_reference": reference,
        },
        "namespace": NAMESPACE,
        "run_prefix": arguments.run_prefix,
        "requests": requests,
        "manifests": [config_map, *jobs],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=None, help="image digest or full reference")
    parser.add_argument("--run-prefix", default="fs2-cxq-r1")
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--nsteps", type=int, default=400)
    parser.add_argument("--seed", type=int, default=20260903)
    parser.add_argument("--cpu", type=int, default=8)
    parser.add_argument("--memory", default="96Gi")
    parser.add_argument(
        "--cache-level",
        default="image-local",
        choices=["cold", "image-local", "artifact-local", "warm", "unknown"],
    )
    parser.add_argument("--verify-digests", action="store_true")
    parser.add_argument("--output", default=str(HERE / "generated-plan.json"))
    arguments = parser.parse_args()

    plan = render(arguments)
    Path(arguments.output).write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "written": arguments.output,
                "manifests": len(plan["manifests"]),
                "image": plan["rendered_from"]["image_reference"],
                "entrypoint_sha256": plan["rendered_from"]["entrypoint_sha256"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

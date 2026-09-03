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

# Checkpoints come from the immutable public generations the ingestion
# successor promoted onto the shared reference-data host plane in its terminal
# run r20260903b. The plane root is mounted and the generation carried in the
# subPath, which is the convention the localization foundation established; the
# prefix is never itself a subPath. Complexa and RosettaFold3 are both public
# and host-path, so no tenant-private plane is involved and no public byte ever
# lands on the academic claim.
HOST_ROOT = "/mnt/fs2-reference-data/data"
HOST_PLANE_NODE_LABEL = "storage.fs2.nebius/reference-data"
GENERATION_ROOT = "scientific-localization/public/generations"
PUBLIC_PLANE_GID = 1000

# Only the run's own outputs are written, and only to this task-owned claim.
OUTPUT_CLAIM = "fs2-cxq-out-r20260903"
ENTRYPOINT_PATH = "/opt/fs2/complexa/runtime_entrypoint.py"
REQUEST_MOUNT = "/opt/fs2/requests"
RF3_ARTIFACT_ID = "rosettafold3-checkpoint"

VARIANT_TASKS = {
    "protein": "02_PDL1",
    "ligand": "39_7V11_LIGAND",
    "ame": "M0024_1nzy_og",
}

# The upstream default reward model differs per pipeline: the protein binder
# pipeline scores with AlphaFold2 through ColabDesign, while the ligand and AME
# pipelines score and evaluate with RosettaFold3. The exact AlphaFold2 generation
# is published and recorded in image-lock.json, but this accepted image's baked
# admission code handles the fs2-tree-inventory/v2 Complexa/RF3 trees, not the
# fs2-flat-tree-inventory/v1 AlphaFold2 tree. Keep the protein reward path closed
# until that distinct admission path is exercised; availability is not runtime
# qualification.
RF3_REWARD_VARIANTS = ("ligand", "ame")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lock() -> dict[str, Any]:
    return json.loads(IMAGE_LOCK.read_text(encoding="utf-8"))


def _generations() -> dict[str, dict[str, Any]]:
    """The pinned generations, read from the entrypoint that also verifies them.

    Loading them from one place is what keeps the mount the plan renders and the
    generation the runtime admits from ever drifting apart.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("complexa_entrypoint", ENTRYPOINT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.GENERATIONS


def generation_sub_path(artifact_id: str, generation: str) -> str:
    return f"{GENERATION_ROOT}/{artifact_id}/sha256/{generation}"


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
        "reward_model": arguments.reward_model,
        "search_algorithm": (
            "single-pass" if arguments.reward_model == "none" else arguments.search_algorithm
        ),
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
    # Refuse a mutable tag.  A tag reference here would silently defeat the
    # digest pinning this lock exists to guarantee, and the run receipt would
    # then record that tag as though it were pinned.
    if "@sha256:" not in digest and not digest.startswith("sha256:"):
        raise SystemExit(
            f"--image must be digest-pinned (sha256:... or repo@sha256:...), got {digest!r}"
        )
    reference = (
        digest
        if "/" in digest
        else f"{image['registry']}/{image['repository']}@{digest}"
    )

    entrypoint_source = ENTRYPOINT.read_text(encoding="utf-8")
    selected = arguments.variant or list(VARIANT_TASKS)
    unknown = [name for name in selected if name not in VARIANT_TASKS]
    if unknown:
        raise SystemExit(f"unknown variant(s): {unknown}; known are {sorted(VARIANT_TASKS)}")
    if arguments.reward_model == "upstream-default":
        unprovable = [name for name in selected if name not in RF3_REWARD_VARIANTS]
        if unprovable:
            raise SystemExit(
                "the upstream default reward model for "
                f"{unprovable} is AlphaFold2, not RosettaFold3. Its exact generation "
                "is published, but the accepted image has not admitted or exercised "
                "that fs2-flat-tree-inventory/v1 reward dependency; restrict the run "
                f"to {list(RF3_REWARD_VARIANTS)} to prove RosettaFold3"
            )
    requests = {
        variant: request_document(variant, f"{arguments.run_prefix}-{variant}", arguments)
        for variant in selected
    }

    # The requests always arrive through a ConfigMap mounted at /opt/fs2/requests.
    # The entrypoint itself normally comes from the image. It is only overlaid
    # from the ConfigMap when --entrypoint-source configmap is passed, which is
    # how the contract was proven against a predecessor digest that had no
    # entrypoint baked in. Overlaying it onto /opt/fs2/complexa shadows the
    # baked-in file, so the two modes are mutually exclusive by construction.
    overlay = arguments.entrypoint_source == "configmap"
    data = {
        f"request-{variant}.json": json.dumps(document, indent=2) + "\n"
        for variant, document in requests.items()
    }
    if overlay:
        data["runtime_entrypoint.py"] = entrypoint_source

    config_map: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"{arguments.run_prefix}-contract",
            "namespace": NAMESPACE,
            "labels": {"fs2.nebius.ai/owner-task": OWNER_TASK},
        },
        "data": data,
    }

    # read-only belongs on the mount, never on the claim. A PersistentVolumeClaim
    # marked readOnly attaches the whole volume read-only, which is how an
    # earlier revision made its own output mount fail with EROFS.
    generations = _generations()
    for artifact_id in list(VARIANT_TASKS) + [RF3_ARTIFACT_ID]:
        key = artifact_id if artifact_id == RF3_ARTIFACT_ID else f"complexa-{artifact_id}"
        if key not in generations:
            raise SystemExit(f"no generation is pinned for {key}")

    jobs = []
    for variant in selected:
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
                            # The RWX volume root is root-owned, so the non-root
                            # runtime user cannot create its output directory
                            # without a supplemental group. OnRootMismatch keeps
                            # this from becoming a recursive ownership walk over
                            # the ~31 GiB checkpoint tree on every cold start.
                            "securityContext": {
                                # Only the output claim needs this. hostPath is
                                # not an fsGroup-managed volume type, so the
                                # 670 GiB shared plane is never walked, and
                                # OnRootMismatch keeps even the claim cheap.
                                "fsGroup": 10001,
                                "fsGroupChangePolicy": "OnRootMismatch",
                                # The public plane is owned by uid/gid 1000.
                                "supplementalGroups": [PUBLIC_PLANE_GID],
                            },
                            # Capability-driven for the accelerator, plus the
                            # storage label the host plane requires. Neither
                            # names a node: an H100 is selected by having a GPU,
                            # not by being called H100.
                            "nodeSelector": {
                                "nebius.com/gpu": "true",
                                HOST_PLANE_NODE_LABEL: "true",
                            },
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
                                        f"{REQUEST_MOUNT}/request-{variant}.json",
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
                                                f"/opt/fs2/artifacts/{RF3_ARTIFACT_ID}/"
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
                                            "name": "trees",
                                            "mountPath": f"/opt/fs2/artifacts/{artifact_id}",
                                            "subPath": generation_sub_path(
                                                artifact_id, generations[artifact_id]["generation"]
                                            ),
                                            "readOnly": True,
                                        },
                                        {
                                            "name": "trees",
                                            "mountPath": f"/opt/fs2/artifacts/{RF3_ARTIFACT_ID}",
                                            "subPath": generation_sub_path(
                                                RF3_ARTIFACT_ID,
                                                generations[RF3_ARTIFACT_ID]["generation"],
                                            ),
                                            "readOnly": True,
                                        },
                                        {
                                            "name": "contract",
                                            "mountPath": REQUEST_MOUNT,
                                            "readOnly": True,
                                        },
                                        *(
                                            [
                                                {
                                                    "name": "contract",
                                                    "mountPath": ENTRYPOINT_PATH,
                                                    "subPath": "runtime_entrypoint.py",
                                                    "readOnly": True,
                                                }
                                            ]
                                            if overlay
                                            else []
                                        ),
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
                                    "name": "trees",
                                    "hostPath": {"path": HOST_ROOT, "type": "Directory"},
                                },
                                {
                                    "name": "outputs",
                                    "persistentVolumeClaim": {"claimName": OUTPUT_CLAIM},
                                },
                                {
                                    "name": "contract",
                                    "configMap": {
                                        "name": f"{arguments.run_prefix}-contract",
                                        "defaultMode": 0o555,
                                    },
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
            "entrypoint_source": arguments.entrypoint_source,
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
    # Default ON.  The in-image tree inventory identifies each file by length
    # and CRC32, which is forgeable by construction: a same-length payload with
    # a matching CRC32 is solved for directly, not searched.  The SHA-256 pass
    # this flag enables is the only cryptographic content check in the runtime,
    # so a plan must have to opt *out* of it, never silently omit it.
    parser.add_argument(
        "--verify-digests", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument(
        "--reward-model",
        default="none",
        choices=["none", "upstream-default"],
        help="'none' isolates the Complexa score model; 'upstream-default' runs the "
        "pipeline's own reward model, which is RosettaFold3 for ligand and AME and "
        "AlphaFold2 through ColabDesign for protein",
    )
    parser.add_argument(
        "--search-algorithm",
        default="best-of-n",
        help="search algorithm when a reward model is enabled",
    )
    parser.add_argument(
        "--variant",
        action="append",
        default=None,
        help="restrict the plan to these variants; repeatable",
    )
    parser.add_argument(
        "--entrypoint-source",
        default="baked",
        choices=["baked", "configmap"],
        help="'baked' runs the entrypoint from the image; 'configmap' overlays this "
        "checkout's copy, which is only for proving the contract against a digest "
        "that has no entrypoint baked in",
    )
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

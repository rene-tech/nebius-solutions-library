#!/usr/bin/env python3
"""Render the H100 semantic qualification Job for a published RFdiffusion digest.

The accelerator is selected by class alone, through a single parameterised label.
There is no pool id, no capacity-source pin and no device-name admission check, so
the same renderer targets any GPU family the platform exposes. H100 is only the
default value of ``--accelerator-class``.

The image is always consumed by immutable digest; a mutable tag is refused.

Artifact delivery is mixed-plane. Each ``--plane NAME=CLAIM[:SUBPATH]`` becomes its
own read-only mount at ``<artifact-root>/NAME``, so a request may draw the checkpoint
from one plane and a target structure from another without either plane having to
host the other's bytes. Manifest paths are then relative to the artifact root and
begin with the plane name, which is what lets the same image bind a public plane and
a licence-restricted plane in one run. One shared claim mounted at a task-owned
subPath is the older single-plane shape and is not assumed here.

``--generation-plane NAME=ARTIFACT_ID`` is the canonical form and the one an
activation run must use. It reads the artifact's accepted generation out of the
localization contract and mounts exactly that immutable generation directory
read-only from the public reference-data host plane, so the generation digest
can never be passed in by hand. A run that binds a canonical generation is the
only run whose evidence may claim a localized artifact; ``--plane`` remains for
the historical task-owned claim and claims nothing about localization.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LOCK = json.loads((HERE.parent / "image-lock.json").read_text(encoding="utf-8"))

DIGEST_REFERENCE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")

ARTIFACT_MOUNT = "/opt/fs2/artifacts"
INPUT_ARTIFACT_MOUNT = "/opt/fs2/inputs"
WORKSPACE_MOUNT = "/workspace"
REQUEST_MOUNT = "/var/run/fs2"

# The public plane the localization foundation publishes into. These are the
# foundation's own values, not this runtime's choice, which is why they are
# asserted against the contract rather than accepted from the command line.
# The marker verifier is a stdlib-only module, so it runs in the plain upstream
# Python image by digest rather than needing a runtime of its own.
VERIFIER_IMAGE = (
    "docker.io/library/python@sha256:"
    "9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534"
)
REFERENCE_DATA_HOST_ROOT = "/mnt/fs2-reference-data/data"
REFERENCE_DATA_NODE_LABEL = "storage.fs2.nebius/reference-data"
PUBLIC_TREE_PREFIX = "scientific-localization/public"
def _solution_root(start: Path) -> Path:
    """Walk up to the solution root rather than counting directory levels."""
    relative = Path("catalog/runtime/contracts/scientific-artifact-localization.json")
    for candidate in (start, *start.parents):
        if (candidate / relative).is_file():
            return candidate
    raise SystemExit(f"could not locate {relative} above {start}")


SOLUTION_ROOT = _solution_root(HERE)
LOCALIZATION_CONTRACT = (
    SOLUTION_ROOT / "catalog/runtime/contracts/scientific-artifact-localization.json"
)


def generation_sub_path(artifact_id: str, generation: str) -> str:
    return f"{PUBLIC_TREE_PREFIX}/generations/{artifact_id}/sha256/{generation}"


def parse_generation_plane(spec: str, contract_path: Path) -> tuple[str, str, str]:
    """``NAME=ARTIFACT_ID`` -> (name, artifact_id, generation).

    The generation is resolved from the accepted localization contract, so a run
    cannot bind a digest that no accepted artifact declares.
    """
    if "=" not in spec:
        raise SystemExit(f"--generation-plane must be NAME=ARTIFACT_ID, got {spec!r}")
    name, _, artifact_id = spec.partition("=")
    if not name or not artifact_id:
        raise SystemExit(f"--generation-plane must name both a plane and an artifact, got {spec!r}")
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name):
        raise SystemExit(f"plane name must be a DNS label, got {name!r}")
    document = json.loads(contract_path.read_text(encoding="utf-8"))
    for artifact in document["artifacts"]:
        if artifact["artifact_id"] != artifact_id:
            continue
        if artifact.get("visibility", "public") != "public":
            raise SystemExit(
                f"{artifact_id} is not public, so it does not live on the reference-data host plane"
            )
        return name, artifact_id, artifact["tree"]["inventory_sha256"]
    raise SystemExit(
        f"{artifact_id} is not an accepted localization artifact in {contract_path}"
    )


def _image(reference: str) -> str:
    if not DIGEST_REFERENCE.match(reference):
        raise SystemExit(
            f"runtime image must be pinned by digest (repository@sha256:...), got {reference!r}"
        )
    return reference


def parse_plane(spec: str) -> tuple[str, str, str]:
    """``NAME=CLAIM[:SUBPATH]`` -> (name, claim, sub_path)."""
    if "=" not in spec:
        raise SystemExit(f"--plane must be NAME=CLAIM[:SUBPATH], got {spec!r}")
    name, _, remainder = spec.partition("=")
    claim, _, sub_path = remainder.partition(":")
    if not name or not claim:
        raise SystemExit(f"--plane must name both a plane and a claim, got {spec!r}")
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name):
        raise SystemExit(f"plane name must be a DNS label, got {name!r}")
    return name, claim, sub_path


def parse_input_plane(spec: str) -> tuple[str, str]:
    """``NAME=CONFIGMAP`` -> (name, config_map)."""
    if "=" not in spec:
        raise SystemExit(f"--input-plane must be NAME=CONFIGMAP, got {spec!r}")
    name, _, config_map = spec.partition("=")
    if not name or not config_map:
        raise SystemExit(f"--input-plane must name both a plane and a ConfigMap, got {spec!r}")
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name):
        raise SystemExit(f"plane name must be a DNS label, got {name!r}")
    return name, config_map


def render(
    *,
    name: str,
    namespace: str,
    image: str,
    accelerator_class: str,
    local_queue: str,
    planes: list[tuple[str, str, str]],
    generation_planes: list[tuple[str, str, str]],
    input_planes: list[tuple[str, str]],
    verifier_config_map: str,
    run_claim: str,
    run_sub_path: str,
    request_config_map: str,
    cache_level: str,
    checkpoint_artifact_id: str,
    gpu_count: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    labels = {
        "app.kubernetes.io/managed-by": "fs2-serve-models",
        "app.kubernetes.io/name": "fs2-rfdiffusion-qualification",
        "app.kubernetes.io/part-of": "fs2-serve",
        "fs2.nebius.ai/model-id": "rfdiffusion",
        "fs2.nebius.ai/owner-task": LOCK["owner_task"],
    }
    if local_queue:
        labels["kueue.x-k8s.io/queue-name"] = local_queue

    node_selector = {"accelerator.fs2.nebius/class": accelerator_class}
    if generation_planes:
        # The public plane is a host path, so the accelerator alone is not enough:
        # the node must also carry the reference-data tree.
        node_selector[REFERENCE_DATA_NODE_LABEL] = "true"

    annotations = {
        "fs2.nebius.ai/adapter-id": LOCK["adapter"]["adapter_id"],
        "fs2.nebius.ai/source-revision": LOCK["source"]["revision"],
        "fs2.nebius.ai/source-tag": LOCK["source"]["tag"],
        "fs2.nebius.ai/checkpoint-sha256": LOCK["external_artifacts"][0]["sha256"],
        "fs2.nebius.ai/artifact-planes": ",".join(
            p[0] for p in (*planes, *generation_planes, *input_planes)
        ),
    }
    if generation_planes:
        # The exact immutable generations this run bound, so the receipt and the
        # Job agree without anyone having to trust a rendered file.
        annotations["fs2.nebius.ai/localization-generations"] = ",".join(
            f"{artifact_id}={generation}" for _, artifact_id, generation in generation_planes
        )

    artifact_mounts = []
    plane_volumes = []
    seen_claims: dict[str, str] = {}
    for index, (plane_name, claim, sub_path) in enumerate(planes):
        volume_name = seen_claims.get(claim)
        if volume_name is None:
            volume_name = f"plane-{index}"
            seen_claims[claim] = volume_name
            plane_volumes.append(
                {"name": volume_name, "persistentVolumeClaim": {"claimName": claim}}
            )
        mount = {
            "name": volume_name,
            "mountPath": f"{ARTIFACT_MOUNT}/{plane_name}",
            "readOnly": True,
        }
        if sub_path:
            mount["subPath"] = sub_path
        artifact_mounts.append(mount)

    for index, (plane_name, config_map) in enumerate(input_planes):
        # Request-scoped input bytes, not model artifacts: a target structure is
        # supplied with the run and is never claimed as a localized artifact.
        volume_name = f"input-{index}"
        plane_volumes.append({"name": volume_name, "configMap": {"name": config_map}})
        artifact_mounts.append(
            {
                "name": volume_name,
                "mountPath": f"{INPUT_ARTIFACT_MOUNT}/{plane_name}",
                "readOnly": True,
            }
        )

    if generation_planes:
        plane_volumes.append(
            {
                "name": "trees",
                "hostPath": {"path": REFERENCE_DATA_HOST_ROOT, "type": "Directory"},
            }
        )
        for plane_name, artifact_id, generation in generation_planes:
            artifact_mounts.append(
                {
                    "name": "trees",
                    "mountPath": f"{ARTIFACT_MOUNT}/{plane_name}",
                    "subPath": generation_sub_path(artifact_id, generation),
                    "readOnly": True,
                }
            )

    init_containers: list[dict[str, Any]] = []
    if generation_planes and verifier_config_map:
        # Admission runs in the same pod that consumes the bytes, so the marker
        # the run trusted and the marker a reviewer reads are the same object on
        # the same node. A rejected marker fails the pod before any GPU work.
        contract = json.loads(LOCALIZATION_CONTRACT.read_text(encoding="utf-8"))
        declared = {item["artifact_id"]: item for item in contract["artifacts"]}
        plane_volumes.append(
            {"name": "verifier", "configMap": {"name": verifier_config_map}}
        )
        for plane_name, artifact_id, generation in generation_planes:
            artifact = declared[artifact_id]
            init_containers.append(
                {
                    "name": f"admit-{plane_name}"[:63],
                    "image": VERIFIER_IMAGE,
                    "command": [
                        "python3", "-m", "fs2_localization.localization", "marker",
                        "--artifact-id", artifact_id,
                        "--mount", f"{ARTIFACT_MOUNT}/{plane_name}",
                        "--expect-generation", generation,
                        "--sub-path", generation_sub_path(artifact_id, generation),
                        "--expect-visibility", artifact.get("visibility", "public"),
                        "--expect-algorithm", artifact["tree"]["inventory_algorithm"],
                        "--expect-volume-kind", "host-path",
                        "--expect-host-root", REFERENCE_DATA_HOST_ROOT,
                    ],
                    "env": [
                        {"name": "PYTHONPATH", "value": "/opt/fs2-localization"},
                        {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                    ],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "readOnlyRootFilesystem": True,
                    },
                    "resources": {
                        "requests": {"cpu": "200m", "memory": "256Mi"},
                        "limits": {"cpu": "2", "memory": "2Gi"},
                    },
                    "volumeMounts": [
                        {
                            "name": "verifier",
                            "mountPath": "/opt/fs2-localization/fs2_localization",
                            "readOnly": True,
                        },
                        {
                            "name": "trees",
                            "mountPath": f"{ARTIFACT_MOUNT}/{plane_name}",
                            "subPath": generation_sub_path(artifact_id, generation),
                            "readOnly": True,
                        },
                    ],
                }
            )

    if not generation_planes and not planes:
        raise SystemExit("RFdiffusion qualification requires one checkpoint artifact plane")
    checkpoint_plane = generation_planes[0][0] if generation_planes else planes[0][0]
    checkpoint_root = f"{ARTIFACT_MOUNT}/{checkpoint_plane}"
    command = [
        "python", "/opt/fs2/runtime_entrypoint.py", "run",
        "--request", f"{REQUEST_MOUNT}/request.json",
        "--input-manifest", f"{REQUEST_MOUNT}/input-manifest.json",
        "--artifact-root", checkpoint_root,
        "--input-artifact-root", INPUT_ARTIFACT_MOUNT,
        "--checkpoint-artifact-id", checkpoint_artifact_id,
        "--output", f"{WORKSPACE_MOUNT}/{name}",
        "--scratch", "/tmp/fs2-rfdiffusion",
        "--cache-level", cache_level,
        "--timeout-seconds", str(timeout_seconds),
    ]

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": labels, "annotations": annotations},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": timeout_seconds + 1800,
            "ttlSecondsAfterFinished": 86400,
            "suspend": bool(local_queue),
            "template": {
                "metadata": {"labels": labels, "annotations": annotations},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "terminationGracePeriodSeconds": 90,
                    "nodeSelector": node_selector,
                    "tolerations": [
                        {"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}
                    ],
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "initContainers": init_containers,
                    "containers": [
                        {
                            "name": "diffuse",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": command,
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "readOnlyRootFilesystem": True,
                            },
                            "resources": {
                                "requests": {"cpu": "8", "memory": "48Gi", "nvidia.com/gpu": gpu_count},
                                "limits": {"cpu": "16", "memory": "64Gi", "nvidia.com/gpu": gpu_count},
                            },
                            "volumeMounts": [
                                {"name": "request", "mountPath": REQUEST_MOUNT, "readOnly": True},
                                *artifact_mounts,
                                {"name": "workspace", "mountPath": WORKSPACE_MOUNT, "subPath": run_sub_path},
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "request", "configMap": {"name": request_config_map}},
                        # readOnly belongs on the mount, never on the claim: a read-only
                        # claim marks the whole CSI attachment read-only and the
                        # workspace mount would lose write access with it.
                        *plane_volumes,
                        {"name": "workspace", "persistentVolumeClaim": {"claimName": run_claim}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "16Gi"}},
                    ],
                },
            },
        },
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--namespace", default="fs2-models")
    parser.add_argument("--runtime-image", required=True, help="repository@sha256:...")
    parser.add_argument("--accelerator-class", default="nvidia-h100-sxm5-80gb")
    parser.add_argument("--local-queue", default="", help="empty runs the Job unqueued")
    parser.add_argument(
        "--plane",
        action="append",
        default=[],
        metavar="NAME=CLAIM[:SUBPATH]",
        help="read-only artifact plane mounted at <artifact-root>/NAME; repeatable",
    )
    parser.add_argument(
        "--generation-plane",
        action="append",
        default=[],
        metavar="NAME=ARTIFACT_ID",
        help=(
            "canonical form: mount the artifact's accepted localization generation "
            "read-only from the public reference-data host plane at "
            "<artifact-root>/NAME; repeatable"
        ),
    )
    parser.add_argument(
        "--input-plane",
        action="append",
        default=[],
        metavar="NAME=CONFIGMAP",
        help=(
            "request-scoped input bytes mounted read-only at <artifact-root>/NAME; "
            "these are run inputs, never localized model artifacts; repeatable"
        ),
    )
    parser.add_argument(
        "--localization-contract",
        type=Path,
        default=LOCALIZATION_CONTRACT,
        help="accepted scientific artifact localization contract",
    )
    parser.add_argument(
        "--verifier-config-map",
        default="",
        help=(
            "ConfigMap carrying the fs2_localization package; when set, one marker "
            "admission init container runs per canonical generation plane"
        ),
    )
    parser.add_argument(
        "--run-claim",
        required=True,
        help="writable claim for run outputs only; it is never an artifact source",
    )
    parser.add_argument("--run-sub-path", default="rfdiffusion/runs")
    parser.add_argument("--request-config-map", required=True)
    parser.add_argument("--checkpoint-artifact-id", default="artifact.rfdiffusion.base-ckpt")
    parser.add_argument("--cache-level", default="cold-registry-pull")
    parser.add_argument("--gpu-count", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--output", default="-")
    args = parser.parse_args(argv)

    if not args.plane and not args.generation_plane:
        raise SystemExit(
            "at least one --generation-plane NAME=ARTIFACT_ID or --plane NAME=CLAIM[:SUBPATH] "
            "is required; artifact delivery is mixed-plane and no default single claim is assumed"
        )
    job = render(
        name=args.name,
        namespace=args.namespace,
        image=_image(args.runtime_image),
        accelerator_class=args.accelerator_class,
        local_queue=args.local_queue,
        planes=[parse_plane(spec) for spec in args.plane],
        generation_planes=[
            parse_generation_plane(spec, args.localization_contract)
            for spec in args.generation_plane
        ],
        input_planes=[parse_input_plane(spec) for spec in args.input_plane],
        verifier_config_map=args.verifier_config_map,
        run_claim=args.run_claim,
        run_sub_path=args.run_sub_path,
        request_config_map=args.request_config_map,
        cache_level=args.cache_level,
        checkpoint_artifact_id=args.checkpoint_artifact_id,
        gpu_count=args.gpu_count,
        timeout_seconds=args.timeout_seconds,
    )
    payload = json.dumps(job, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        sys.stdout.write(payload)
    else:
        Path(args.output).write_text(payload, encoding="utf-8")
        print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

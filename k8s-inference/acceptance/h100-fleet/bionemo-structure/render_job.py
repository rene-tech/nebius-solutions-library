#!/usr/bin/env python3
"""Render isolated H100 cohort qualification Jobs.

The runtime is a native Kubernetes restartable init sidecar.  The unmodified
strict semantic validator is the Job's main container, so Kubernetes stops the
server after validation and the Job has an ordinary terminal status.  No
Service or production Deployment is created by this harness.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any


TASK = "fs2-h100-fleet-bionemo-structure-r20260907"


@dataclass(frozen=True)
class Profile:
    image: str
    validator_args: tuple[str, ...]
    user: int
    cpu_request: str
    cpu_limit: str
    memory_request: str
    memory_limit: str
    gpu: int = 1
    cache_mount: str | None = None
    runtime_env: tuple[tuple[str, str], ...] = ()


PROFILES = {
    "boltz2": Profile(
        image=(
            "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/"
            "h100-fleet/boltz2@sha256:93d5fae96d87dff930206cf331f170b73f2369067f0b32169e0008f0db320a90"
        ),
        validator_args=(),
        user=1000,
        cpu_request="12",
        cpu_limit="32",
        memory_request="96Gi",
        memory_limit="192Gi",
        cache_mount="/models",
        runtime_env=(
            ("HOME", "/models/home"),
            ("HF_HOME", "/models/huggingface"),
            ("HF_HUB_CACHE", "/models/huggingface/hub"),
            ("HF_XET_CACHE", "/models/huggingface/xet"),
        ),
    ),
    "genmol": Profile(
        image=(
            "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/"
            "h100-fleet/genmol@sha256:7a89a6f254e5a56dad391b8707315f8abd77d7138cacd585c706f63440463aaf"
        ),
        validator_args=("--request-file", "/validator/requests-qed-logp.json"),
        user=1000,
        cpu_request="8",
        cpu_limit="16",
        memory_request="48Gi",
        memory_limit="96Gi",
        cache_mount="/models",
        runtime_env=(
            ("HOME", "/models/home"),
            ("HF_HOME", "/models/huggingface"),
            ("HF_HUB_CACHE", "/models/huggingface/hub"),
            ("HF_XET_CACHE", "/models/huggingface/xet"),
        ),
    ),
    "molmim": Profile(
        image=(
            "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/"
            "h100-fleet/molmim@sha256:fd59b1b45206fe5067033dd7385469ea54e6b670bdd530276e75857f9b459958"
        ),
        validator_args=("--request-file", "/validator/request-cmaes-qed.json"),
        user=1000,
        cpu_request="8",
        cpu_limit="16",
        memory_request="24Gi",
        memory_limit="64Gi",
    ),
    "msa-search-pdb70": Profile(
        image=(
            "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/"
            "h100-fleet/msa-search@sha256:64bd29bc83915a145ac5d55ec9c6de1178bc2be2f2d9fdc1b789b9b6a5c78136"
        ),
        validator_args=("--request-file", "/validator/request-pdb70.json"),
        user=65532,
        cpu_request="2",
        cpu_limit="8",
        memory_request="2Gi",
        memory_limit="8Gi",
        gpu=0,
        cache_mount="/cache",
    ),
    "msa-search-pdb70-local": Profile(
        image=(
            "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/"
            "h100-fleet/msa-search-pdb70@sha256:"
            "f6e514e8773142f381971698d10047d834fbc0d09b6c331cd469685bc2b7ce85"
        ),
        validator_args=("--request-file", "/validator/request-pdb70.json"),
        user=65532,
        cpu_request="2",
        cpu_limit="8",
        memory_request="2Gi",
        memory_limit="8Gi",
        gpu=0,
        cache_mount="/cache",
    ),
    "diffdock": Profile(
        image=(
            "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/"
            "h100-fleet/diffdock@sha256:471db264f4c544e1798c090f13b6cf94b3fbd304fb2c91e9f63a3dd33c9d81d7"
        ),
        validator_args=("--request-file", "/validator/1ubq-aspirin-request.json"),
        user=10001,
        cpu_request="8",
        cpu_limit="24",
        memory_request="64Gi",
        memory_limit="200Gi",
        runtime_env=(("FS2_PORT", "8000"),),
    ),
    "proteinmpnn": Profile(
        image=(
            "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/"
            "h100-fleet/proteinmpnn@sha256:f27dda10178fb799dc8f75c2b7ced00643a6281fd3e37e95cd353700e6d62e38"
        ),
        validator_args=("--request-file", "/validator/1ubq-request.json"),
        user=10001,
        cpu_request="4",
        cpu_limit="16",
        memory_request="16Gi",
        memory_limit="128Gi",
        runtime_env=(("FS2_PORT", "8000"),),
    ),
    "openfold2": Profile(
        image=(
            "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/"
            "h100-fleet/openfold2@sha256:9fc70e781b18f4f547da237e2eb81387df3c38d092bb44c46145db6347e7e164"
        ),
        validator_args=(),
        user=10001,
        cpu_request="8",
        cpu_limit="32",
        memory_request="64Gi",
        memory_limit="192Gi",
    ),
}


def render(args: argparse.Namespace) -> dict[str, Any]:
    profile = PROFILES[args.model]
    attempt_suffix = f"-a{args.attempt:02d}" if args.attempt > 1 else ""
    name = f"fs2-h100-{args.model}-r{args.repetition:02d}{attempt_suffix}"
    resources: dict[str, dict[str, str]] = {
        "requests": {"cpu": profile.cpu_request, "memory": profile.memory_request},
        "limits": {"cpu": profile.cpu_limit, "memory": profile.memory_limit},
    }
    if profile.gpu:
        resources["requests"]["nvidia.com/gpu"] = str(profile.gpu)
        resources["limits"]["nvidia.com/gpu"] = str(profile.gpu)

    runtime_mounts = [{"name": "dshm", "mountPath": "/dev/shm"}]
    volumes: list[dict[str, Any]] = [
        {
            "name": "validator-source",
            "configMap": {"name": args.validator_configmap, "defaultMode": 292},
        },
        {"name": "validator", "emptyDir": {"sizeLimit": "2Mi"}},
        # Keep each attempt's receipts on its Pod-owned filesystem. A shared
        # RWX root changes ownership between the adapters' deliberately
        # different non-root UIDs; completed Pods are collected before this
        # test harness removes them.
        {"name": "evidence", "emptyDir": {"sizeLimit": "16Mi"}},
        {"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "16Gi"}},
    ]
    if profile.cache_mount:
        if not args.cache_pvc:
            raise ValueError(f"{args.model} requires --cache-pvc")
        runtime_mounts.append({"name": "model-cache", "mountPath": profile.cache_mount})
        volumes.append(
            {
                "name": "model-cache",
                "persistentVolumeClaim": {"claimName": args.cache_pvc},
            }
        )

    validator_args = [
        "/validator/validate.py",
        "--base-url",
        "http://127.0.0.1:8000",
        "--receipt-dir",
        f"/evidence/{name}",
        "--run-id",
        f"{args.model}-r{args.repetition:02d}-a",
        "--run-id",
        f"{args.model}-r{args.repetition:02d}-b",
        "--ready-timeout",
        "1800",
        "--timeout",
        "1800",
        *profile.validator_args,
    ]
    pod_spec: dict[str, Any] = {
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "restartPolicy": "Never",
        "activeDeadlineSeconds": 7200,
        "terminationGracePeriodSeconds": 30,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": profile.user,
            "runAsGroup": profile.user,
            "fsGroup": profile.user,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "initContainers": [
            {
                "name": "materialize-validator",
                "image": profile.image,
                "imagePullPolicy": "IfNotPresent",
                "command": [
                    "/bin/sh",
                    "-ec",
                    "cp -L /validator-source/* /validator/ && test -f /validator/validate.py",
                ],
                "resources": {
                    "requests": {"cpu": "10m", "memory": "16Mi"},
                    "limits": {"cpu": "1", "memory": "128Mi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [
                    {
                        "name": "validator-source",
                        "mountPath": "/validator-source",
                        "readOnly": True,
                    },
                    {"name": "validator", "mountPath": "/validator"},
                ],
            },
            {
                "name": "runtime",
                "restartPolicy": "Always",
                "image": profile.image,
                "imagePullPolicy": "IfNotPresent",
                "env": [
                    {"name": key, "value": value} for key, value in profile.runtime_env
                ],
                "resources": resources,
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": runtime_mounts,
            }
        ],
        "containers": [
            {
                "name": "validator",
                "image": profile.image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["python3", *validator_args],
                "resources": {
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "2", "memory": "4Gi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [
                    {"name": "validator", "mountPath": "/validator", "readOnly": True},
                    {"name": "evidence", "mountPath": "/evidence"},
                ],
            }
        ],
        "volumes": volumes,
        "tolerations": [],
    }
    if profile.gpu:
        pod_spec["nodeSelector"] = {
            "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb"
        }
        pod_spec["tolerations"].append(
            {
                "key": "dedicated",
                "operator": "Equal",
                "value": "fs2-inference",
                "effect": "NoSchedule",
            }
        )
    else:
        pod_spec["nodeSelector"] = {"capacity.fs2.nebius/pool": "general-cpu"}
        pod_spec["tolerations"].append(
            {
                "key": "workload.fs2.nebius/general-cpu",
                "operator": "Equal",
                "value": "true",
                "effect": "NoSchedule",
            }
        )

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": args.namespace,
            "labels": {
                "app.kubernetes.io/part-of": "fs2-h100-fleet-qualification",
                "fs2.nebius.ai/test-only": "true",
                "fs2.nebius.ai/task": TASK,
                "fs2.nebius.ai/model-id": args.model,
                "fs2.nebius.ai/repetition": str(args.repetition),
            },
            "annotations": {"fs2.nebius.ai/runtime-image": profile.image},
        },
        "spec": {
            "backoffLimit": 0,
            "template": {
                "metadata": {
                    "labels": {
                        "fs2.nebius.ai/test-only": "true",
                        "fs2.nebius.ai/task": TASK,
                        "fs2.nebius.ai/model-id": args.model,
                    }
                },
                "spec": pod_spec,
            },
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=sorted(PROFILES), required=True)
    parser.add_argument("--repetition", type=int, choices=range(1, 4), required=True)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--validator-configmap", required=True)
    # Deprecated compatibility flag. Receipts are Pod-local and must be
    # collected before task-owned Pods are deleted.
    parser.add_argument("--evidence-pvc")
    parser.add_argument("--cache-pvc")
    parser.add_argument("--namespace", default="fs2-models")
    args = parser.parse_args()
    print(json.dumps(render(args), indent=2))


if __name__ == "__main__":
    main()

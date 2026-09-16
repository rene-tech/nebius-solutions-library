#!/usr/bin/env python3
"""Render task-owned public classifier resources; never mutates shared services."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAMESPACE = "fs2-models"
PREFIX = "fs2-mindguard-r20260916"


def render(model_id: str, replicas: int = 1, node: str | None = None) -> dict:
    lock = json.loads((ROOT / "public-models.lock.json").read_text())
    model = lock["models"][model_id]
    name = f"{PREFIX}-{model_id.removeprefix('mindguard-')}"
    labels = {"app.kubernetes.io/name": name, "fs2.nebius/task": PREFIX}
    model_path = f"/models/{model_id}/{model['revision']}"
    image = lock["runtime_image"]
    security = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
    init_code = (
        "import os; from huggingface_hub import snapshot_download; "
        "snapshot_download(repo_id=os.environ['MODEL_REPO'], revision=os.environ['MODEL_REVISION'], "
        "local_dir=os.environ['MODEL_PATH'], allow_patterns=['*.json','*.jinja','*.txt','*.safetensors'])"
    )
    container = {
        "name": "vllm", "image": image,
        "args": [model_path, "--served-model-name", model_id, "--host", "0.0.0.0", "--port", "8000",
                 "--dtype", lock["dtype"], "--max-model-len", str(lock["max_model_len"]),
                 "--tensor-parallel-size", "1", "--gpu-memory-utilization", "0.80",
                 "--generation-config", "vllm", "--chat-template", model_path + "/chat_template.jinja",
                 "--enforce-eager"],
        "env": [{"name": key, "value": value} for key, value in {
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "VLLM_NO_USAGE_STATS": "1",
            "HF_HOME": "/runtime-cache/huggingface", "VLLM_CACHE_ROOT": "/runtime-cache/vllm",
            "TRITON_CACHE_DIR": "/runtime-cache/triton", "XDG_CACHE_HOME": "/runtime-cache/xdg",
        }.items()],
        "resources": {"requests": {"cpu": "4", "memory": "32Gi", "nvidia.com/gpu": "1"},
                      "limits": {"cpu": "12", "memory": "64Gi", "nvidia.com/gpu": "1"}},
        "ports": [{"name": "http", "containerPort": 8000}],
        "startupProbe": {"httpGet": {"path": "/health", "port": "http"},
                         "periodSeconds": 10, "failureThreshold": 180},
        "readinessProbe": {"httpGet": {"path": "/health", "port": "http"}, "periodSeconds": 5},
        "livenessProbe": {"tcpSocket": {"port": "http"}, "periodSeconds": 20},
        "securityContext": security,
        "volumeMounts": [{"name": "models", "mountPath": "/models", "readOnly": True},
                         {"name": "runtime-cache", "mountPath": "/runtime-cache"},
                         {"name": "shm", "mountPath": "/dev/shm"}],
    }
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
         "metadata": {"name": PREFIX + "-cache", "namespace": NAMESPACE, "labels": labels},
         "spec": {"accessModes": ["ReadWriteMany"], "storageClassName": "csi-mounted-fs-path-sc",
                  "resources": {"requests": {"storage": "96Gi"}}}},
        {"apiVersion": "apps/v1", "kind": "Deployment",
         "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
         "spec": {"replicas": replicas, "revisionHistoryLimit": 2, "strategy": {"type": "Recreate"},
                  "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
                  "template": {
                      "metadata": {"labels": labels, "annotations": {
                          "fs2.nebius/model-revision": model["revision"],
                          "fs2.nebius/model-role": "safety-classifier",
                          "fs2.nebius/enforcement": "observe"}},
                      "spec": {
                          "automountServiceAccountToken": False, "enableServiceLinks": False,
                          "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                                              "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
                          "nodeSelector": {"accelerator.fs2.nebius/class": "nvidia-l40s-48gb",
                                           "capacity.fs2.nebius/type": "regular",
                                           **({"kubernetes.io/hostname": node} if node else {})},
                          "tolerations": [{"key": "dedicated", "operator": "Equal",
                                           "value": "fs2-inference", "effect": "NoSchedule"}],
                          "initContainers": [{
                              "name": "hydrate", "image": image, "command": ["python3", "-c", init_code],
                              "env": [{"name": "MODEL_REPO", "value": model["repo_id"]},
                                      {"name": "MODEL_REVISION", "value": model["revision"]},
                                      {"name": "MODEL_PATH", "value": model_path},
                                      {"name": "HF_HOME", "value": "/models/.huggingface"},
                                      {"name": "HF_HUB_DISABLE_TELEMETRY", "value": "1"},
                                      {"name": "HF_TOKEN", "valueFrom": {"secretKeyRef": {
                                          "name": PREFIX + "-hf", "key": "token"}}}],
                              "resources": {"requests": {"cpu": "1", "memory": "2Gi"},
                                            "limits": {"cpu": "4", "memory": "8Gi"}},
                              "securityContext": security,
                              "volumeMounts": [{"name": "models", "mountPath": "/models"}]}],
                          "containers": [container],
                          "volumes": [{"name": "models", "persistentVolumeClaim": {
                              "claimName": PREFIX + "-cache"}},
                                      {"name": "runtime-cache", "emptyDir": {"sizeLimit": "12Gi"}},
                                      {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "8Gi"}}],
                      }} }},
        {"apiVersion": "v1", "kind": "Service",
         "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
         "spec": {"selector": {"app.kubernetes.io/name": name},
                  "ports": [{"name": "http", "port": 8000, "targetPort": "http"}]}},
    ]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=["mindguard-4b", "mindguard-8b"])
    parser.add_argument("--replicas", type=int, choices=[0, 1], default=1)
    parser.add_argument("--node", help="pin repeated hardware comparisons to an existing L40S node")
    args = parser.parse_args()
    print(json.dumps(render(args.model, args.replicas, args.node), indent=2))

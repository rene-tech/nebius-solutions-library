"""Render portable task-owned resident voice deployments; never apply them."""

import argparse
import json
import re

MODELS = {
    "parakeet-realtime-eou-120m-v1",
    "magpie-tts-multilingual-357m",
    "diar-streaming-sortformer-4spk-v2-1",
}


def render(
    *,
    model,
    image,
    namespace,
    name,
    selectors=None,
    pull_secret=None,
    replicas=1,
    artifact_config=None,
):
    if model not in MODELS:
        raise ValueError("unknown model")
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("image must be immutable digest")
    for identifier in (namespace, name):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,61}[a-z0-9]", identifier):
            raise ValueError("invalid resource name")
    if replicas < 0 or replicas > 8:
        raise ValueError("replicas must be between 0 and 8")
    labels = {
        "app.kubernetes.io/name": name,
        "app.kubernetes.io/part-of": "fs2-serve",
        "app.kubernetes.io/component": "model-runtime",
        "fs2-serve.nebius.ai/model-id": model,
        "workload.fs2.nebius/owner": "voice-agent-20260916",
        "fs2.voice/model": model,
    }
    pod = {
        "automountServiceAccountToken": False,
        "terminationGracePeriodSeconds": 1900,
        "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001},
        "nodeSelector": selectors or {},
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
                "name": "voice",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "env": [
                    {"name": "FS2_VOICE_MODEL", "value": model},
                    {"name": "OMP_NUM_THREADS", "value": "2"},
                ],
                "ports": [{"name": "http", "containerPort": 8000}],
                "resources": {
                    "requests": {
                        "cpu": "2",
                        "memory": "16Gi",
                        "ephemeral-storage": "16Gi",
                        "nvidia.com/gpu": "1",
                    },
                    "limits": {
                        "cpu": "4",
                        "memory": "32Gi",
                        "ephemeral-storage": "32Gi",
                        "nvidia.com/gpu": "1",
                    },
                },
                "startupProbe": {
                    "httpGet": {"path": "/readyz", "port": "http"},
                    "periodSeconds": 5,
                    "failureThreshold": 180,
                },
                "readinessProbe": {
                    "httpGet": {"path": "/readyz", "port": "http"},
                    "periodSeconds": 3,
                },
                "livenessProbe": {
                    "httpGet": {"path": "/healthz", "port": "http"},
                    "periodSeconds": 30,
                    "failureThreshold": 5,
                },
                "lifecycle": {
                    "preStop": {
                        "exec": {
                            "command": [
                                "python",
                                "-c",
                                "import json,time,urllib.request; "
                                "urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/drain',method='POST'),timeout=5); "
                                "deadline=time.monotonic()+1850; "
                                "exec(\"while time.monotonic()<deadline:\\n try:\\n  r=urllib.request.urlopen('http://127.0.0.1:8000/metrics',timeout=5).read().decode()\\n  if 'fs2_voice_occupied 0.0' in r: break\\n except Exception: break\\n time.sleep(1)\"); time.sleep(5)",
                            ]
                        }
                    }
                },
                "volumeMounts": [{"name": "cache", "mountPath": "/cache"}],
            }
        ],
        "volumes": [{"name": "cache", "emptyDir": {"sizeLimit": "16Gi"}}],
    }
    if pull_secret:
        pod["imagePullSecrets"] = [{"name": pull_secret}]
    if artifact_config:
        pod["containers"][0]["env"].append(
            {
                "name": "FS2_SPEECH_ARTIFACT_HOSTS",
                "valueFrom": {
                    "configMapKeyRef": {
                        "name": artifact_config,
                        "key": "artifact-hosts",
                    }
                },
            }
        )
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name, "namespace": namespace, "labels": labels},
                "spec": {
                    "replicas": replicas,
                    "strategy": {
                        "type": "RollingUpdate",
                        "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
                    },
                    "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
                    "template": {"metadata": {"labels": labels}, "spec": pod},
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": name, "namespace": namespace, "labels": labels},
                "spec": {
                    "selector": {"app.kubernetes.io/name": name},
                    "ports": [{"name": "http", "port": 8000, "targetPort": "http"}],
                },
            },
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("model", "image", "namespace", "name"):
        parser.add_argument("--" + arg, required=True)
    parser.add_argument("--node-selector", action="append", default=[])
    parser.add_argument("--pull-secret")
    parser.add_argument("--artifact-config")
    parser.add_argument("--replicas", type=int, default=1)
    args = parser.parse_args()
    print(
        json.dumps(
            render(
                model=args.model,
                image=args.image,
                namespace=args.namespace,
                name=args.name,
                selectors=dict(item.split("=", 1) for item in args.node_selector),
                pull_secret=args.pull_secret,
                replicas=args.replicas,
                artifact_config=args.artifact_config,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

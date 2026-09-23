"""Mirror the pinned public NVIDIA runtime using existing project credentials.

Credentials are passed directly to Kubernetes, never written to the repository
or printed. This creates only task-owned CPU Job/Secret objects. Delete those
exact resources after the copy has completed.
"""

import base64
import json
import subprocess

KUBE = [
    "kubectl",
    "--kubeconfig",
    "/home/tux/secure-handoff/cosmos-stockholm-sandbox2-20260917.kubeconfig",
    "--context",
    "fs2-remediation-sandbox2",
    "-n",
    "fs2-models",
]
NAME = "fs2-gromacs-r20260923-mirror"
SOURCE = "nvcr.io/nvidia/gromacs@sha256:0e52e3ae971453898956379952b9ea606f5400cbdb4d439773ecbae4d8f5ad59"
HOST = "cr.eu-north1.nebius.cloud"
TARGET = HOST + "/e00akg9ndpx77eaexh/fs2-platform/gromacs-ngc:v2026.2-r20260923"


def main():
    secret = json.loads(
        subprocess.check_output(KUBE + ["get", "secret", "wan2-ngc-pull", "-o", "json"])
    )
    config = json.loads(base64.b64decode(secret["data"][".dockerconfigjson"]))
    credential = json.loads(
        subprocess.check_output(
            ["docker-credential-nebius", "get"],
            input=(HOST + "\n").encode(),
        )
    )
    config["auths"][HOST] = {
        "auth": base64.b64encode(
            (credential["Username"] + ":" + credential["Secret"]).encode()
        ).decode()
    }
    objects = [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": NAME, "namespace": "fs2-models"},
            "type": "Opaque",
            "stringData": {"config.json": json.dumps(config)},
        },
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": NAME, "namespace": "fs2-models"},
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 900,
                "template": {
                    "metadata": {
                        "labels": {"scientific-ai.nebius.com/task": "gromacs-r20260923"}
                    },
                    "spec": {
                        "automountServiceAccountToken": False,
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "copy",
                                "image": "gcr.io/go-containerregistry/crane@sha256:e78770b31258a3846f878036d9c1f63fbe4c871f9f56990bf77fd95c013e3c1b",
                                "command": ["crane"],
                                "args": ["copy", SOURCE, TARGET],
                                "env": [{"name": "DOCKER_CONFIG", "value": "/auth"}],
                                "volumeMounts": [
                                    {
                                        "name": "auth",
                                        "mountPath": "/auth",
                                        "readOnly": True,
                                    }
                                ],
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "128Mi"},
                                    "limits": {"cpu": "1", "memory": "1Gi"},
                                },
                            }
                        ],
                        "volumes": [{"name": "auth", "secret": {"secretName": NAME}}],
                    },
                },
            },
        },
    ]
    subprocess.run(
        KUBE + ["apply", "-f", "-"],
        input=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "List",
                "items": objects,
            }
        ).encode(),
        check=True,
    )
    print(json.dumps({"source": SOURCE, "target": TARGET, "job": NAME}))


if __name__ == "__main__":
    main()

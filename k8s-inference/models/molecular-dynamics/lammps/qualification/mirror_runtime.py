"""Mirror the exact NGC amd64 candidate with the existing cluster auth route.

No credentials are printed or persisted locally. This borrows GROMACS's reviewed
credential path and creates only the named task-owned CPU Job and Secret.
"""

import base64
import json
import subprocess

KUBE = ["kubectl", "--kubeconfig", "/home/tux/secure-handoff/cosmos-stockholm-sandbox2-20260917.kubeconfig", "--context", "fs2-remediation-sandbox2", "-n", "fs2-models"]
NAME = "fs2-lammps-r20260923-mirror"
SOURCE = "nvcr.io/nvidia/lammps@sha256:d8a0076dfe84fcbc98db05531993c1cd9deb964050b9c122b92655dc3685d731"
HOST = "cr.eu-north1.nebius.cloud"
TARGET = HOST + "/e00akg9ndpx77eaexh/fs2-platform/lammps-ngc:stable-22jul2025-r20260923"


def main():
    source_secret = json.loads(subprocess.check_output(KUBE + ["get", "secret", "wan2-ngc-pull", "-o", "json"]))
    config = json.loads(base64.b64decode(source_secret["data"][".dockerconfigjson"]))
    credential = json.loads(subprocess.check_output(["docker-credential-nebius", "get"], input=(HOST + "\n").encode()))
    config["auths"][HOST] = {"auth": base64.b64encode((credential["Username"] + ":" + credential["Secret"]).encode()).decode()}
    secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": NAME, "labels": {"scientific-ai.nebius.com/task": "lammps-r20260923"}}, "type": "Opaque", "stringData": {"config.json": json.dumps(config)}}
    job = {
        "apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": NAME},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 1800, "template": {
            "metadata": {"labels": {"scientific-ai.nebius.com/task": "lammps-r20260923"}},
            "spec": {"automountServiceAccountToken": False, "restartPolicy": "Never",
                "containers": [{"name": "copy", "image": "gcr.io/go-containerregistry/crane@sha256:e78770b31258a3846f878036d9c1f63fbe4c871f9f56990bf77fd95c013e3c1b", "command": ["crane"], "args": ["copy", SOURCE, TARGET], "env": [{"name": "DOCKER_CONFIG", "value": "/auth"}], "volumeMounts": [{"name": "auth", "mountPath": "/auth", "readOnly": True}], "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}}}],
                "volumes": [{"name": "auth", "secret": {"secretName": NAME}}]}}}}
    for obj in (secret, job):
        raw = json.dumps(obj).encode()
        subprocess.run(KUBE + ["apply", "--dry-run=client", "-f", "-"], input=raw, check=True)
        subprocess.run(KUBE + ["apply", "-f", "-"], input=raw, check=True)
    print(json.dumps({"source": SOURCE, "target": TARGET, "job": NAME, "secret": NAME}))


if __name__ == "__main__":
    main()

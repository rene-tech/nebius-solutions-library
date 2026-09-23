"""Read-only, private capture of the current release before additive activation."""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    kube = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context]
    helm = ["helm", "--kubeconfig", args.kubeconfig, "--kube-context", args.context]
    values = json.loads(subprocess.check_output(helm + ["-n", "fs2-system", "get", "values",
        "fs2-serve-control-plane", "-o", "json"]))
    config = values["scientificBatch"]
    cm = json.loads(subprocess.check_output(kube + ["-n", config["schedulingContractNamespace"],
        "get", "configmap", config["schedulingContractConfigMapName"], "-o", "json"]))
    scheduling = cm["data"][config["schedulingContractKey"]].encode()
    if hashlib.sha256(scheduling).hexdigest() != config["schedulingContractSha256"]:
        raise ValueError("live scheduling bytes differ from their release digest")
    deployment = json.loads(subprocess.check_output(kube + ["-n", "fs2-system", "get", "deployment",
        "fs2-serve-control-plane", "-o", "json"]))
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    for name, content in [("values.json", json.dumps(values).encode()), ("scheduling.json", scheduling),
                          ("deployment.json", json.dumps(deployment).encode())]:
        with os.fdopen(os.open(args.output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
            handle.write(content)
    execution = config["executionMap"]
    if isinstance(execution, str):
        execution = json.loads(execution)
    print(json.dumps({"image": image, "release_capture": str(args.output),
                      "execution_models": [row["model_id"] for row in execution["models"]],
                      "scheduling_sha256": config["schedulingContractSha256"]}))


if __name__ == "__main__":
    main()

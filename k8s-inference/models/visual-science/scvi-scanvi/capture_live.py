"""Capture actual deployment configuration, including changes after Helm."""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    kube = ["kubectl", "--context", args.context, "-n", "fs2-system"]
    values = json.loads(
        subprocess.check_output(
            [
                "helm",
                "--kube-context",
                args.context,
                "-n",
                "fs2-system",
                "get",
                "values",
                "fs2-serve-control-plane",
                "-o",
                "json",
            ]
        )
    )
    deployment = json.loads(
        subprocess.check_output(
            [*kube, "get", "deployment", "fs2-serve-control-plane", "-o", "json"]
        )
    )
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env = {entry["name"]: entry.get("value") for entry in container["env"]}
    volumes = {entry["name"]: entry for entry in pod["volumes"]}
    config = values["scientificBatch"]

    def config_bytes(volume):
        item = volumes[volume]["configMap"]
        obj = json.loads(
            subprocess.check_output(
                [*kube, "get", "configmap", item["name"], "-o", "json"]
            )
        )
        key = item["items"][0]["key"]
        return item["name"], key, obj["data"][key].encode()

    name, key, scheduling = config_bytes("scientific-batch-scheduling")
    digest = hashlib.sha256(scheduling).hexdigest()
    if digest != env["FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_SHA256"]:
        raise ValueError("Actual scheduling bytes disagree with the deployed digest")
    _, _, execution = config_bytes("scientific-batch-execution")
    config.update(
        schedulingContractConfigMapName=name,
        schedulingContractKey=key,
        schedulingContractSha256=digest,
        executionMap=json.loads(execution),
    )
    values["image"]["digest"] = container["image"].split("@")[1]
    for name, content in (
        ("values.json", json.dumps(values).encode()),
        ("scheduling.json", scheduling),
        ("execution.json", execution),
        ("deployment.json", json.dumps(deployment).encode()),
    ):
        (args.output / name).write_bytes(content)
    print(
        json.dumps(
            {
                "image": container["image"],
                "scheduling_sha256": digest,
                "models": len(config["executionMap"]["models"]),
                "capture": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()

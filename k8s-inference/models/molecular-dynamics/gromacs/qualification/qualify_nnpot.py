"""Run the pinned upstream NNPot force tests on a real H100 (not customer acceptance)."""

import argparse
import json
from pathlib import Path
import subprocess

from run_cluster_workflow import KUBE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--pool",
        choices=("h100-ondemand-1x", "h100-reserved-8x"),
        default="h100-ondemand-1x",
    )
    args = parser.parse_args()
    if "@sha256:" not in args.image:
        raise ValueError("Pin the tested build image")
    args.output.mkdir(parents=True, exist_ok=False)
    name = "fs2-gromacs-nnpot-upstream-r20260923"
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": "fs2-models",
            "labels": {"scientific-ai.nebius.com/task": "gromacs-r20260923"},
        },
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "activeDeadlineSeconds": 3600,
            "nodeSelector": {"accelerator.fs2.nebius/pool-id": args.pool},
            "tolerations": [
                {
                    "key": "dedicated",
                    "operator": "Equal",
                    "value": "fs2-inference",
                    "effect": "NoSchedule",
                }
            ],
            "securityContext": {
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "fsGroup": 10001,
            },
            "containers": [
                {
                    "name": "test",
                    "image": args.image,
                    "command": ["sleep", "3600"],
                    "workingDir": "/work",
                    "volumeMounts": [
                        {"name": "work", "mountPath": "/work"},
                        {
                            "name": "test-output",
                            "mountPath": "/build/gromacs/src/gromacs/applied_forces/nnpot/tests/Testing/Temporary",
                        },
                    ],
                    "resources": {
                        "requests": {
                            "cpu": "8",
                            "memory": "16Gi",
                            "nvidia.com/gpu": "1",
                        },
                        "limits": {"cpu": "8", "memory": "16Gi", "nvidia.com/gpu": "1"},
                    },
                }
            ],
            "volumes": [
                {"name": "work", "emptyDir": {}},
                {"name": "test-output", "emptyDir": {}},
            ],
        },
    }
    subprocess.run(
        KUBE + ["create", "-f", "-"], input=json.dumps(pod).encode(), check=True
    )
    subprocess.run(
        KUBE + ["wait", "--for=condition=Ready", "pod/" + name, "--timeout=600s"],
        check=True,
    )
    subprocess.run(
        KUBE + ["cp", "--no-preserve", str(args.binary), name + ":/work/nnpot-test"],
        check=True,
    )
    with (args.output / "upstream-tests.log").open("wb") as log:
        completed = subprocess.run(
            KUBE
            + [
                "exec",
                name,
                "--",
                "env",
                "LD_LIBRARY_PATH=/build/gromacs/lib:/opt/libtorch/lib:/usr/local/cuda/lib64",
                "/work/nnpot-test",
                "--gtest_output=xml:/work/result.xml",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    subprocess.run(
        KUBE + ["cp", name + ":/work/result.xml", str(args.output / "result.xml")],
        check=False,
    )
    (args.output / "receipt.json").write_text(
        json.dumps(
            {
                "image": args.image,
                "exit_code": completed.returncode,
                "pod": name,
                "customer_path_tested": False,
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps({"exit_code": completed.returncode, "pod": name}))


if __name__ == "__main__":
    main()

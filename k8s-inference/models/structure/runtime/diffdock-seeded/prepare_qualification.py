"""Prepare (never apply) one bounded isolated H100 reproducibility Pod.

Use six source-backed benchmark complexes with two explicit seeds and four
poses each. Retain original case provenance and request bytes in a gzip-mounted
ConfigMap; the public serving Deployment and customer keys are not changed.
"""
import argparse
import base64
import copy
import gzip
import hashlib
import json
from pathlib import Path
import subprocess

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    args = parser.parse_args()
    if "@sha256:" not in args.image:
        raise ValueError("Use the immutable candidate image digest")
    manifest = json.loads(args.manifest.read_text())
    originals = [case for case in manifest["cases"] if case["model_id"] == "diffdock"
                 and case["arguments"]["random_seed"] == 7 and case["arguments"]["num_poses"] == 4]
    if len(originals) != 6:
        raise ValueError("Expected the six original source-backed DiffDock complexes")
    cases = []
    for original in originals:
        for seed in (19, 23):
            case = copy.deepcopy(original)
            case["source_case_id"] = original["case_id"]
            case["source_manifest_sha256"] = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
            case["case_id"] = original["case_id"].replace("-s7-", f"-s{seed}-")
            case["arguments"]["random_seed"] = seed
            cases.append(case)
    command = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context,
               "-n", "fs2-models", "get", "deployment", "diffdock-b300-burst-h100-1x", "-o", "json"]
    deployment = json.loads(subprocess.check_output(command, text=True))
    old = deployment["spec"]["template"]["spec"]
    raw = (json.dumps(cases, sort_keys=True) + "\n").encode()
    packed = gzip.compress(raw, mtime=0)
    name = args.name
    labels = {"app.kubernetes.io/name": "fs2-diffdock-seed-qualification", "fs2.nebius.ai/qualification": name}
    config = {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
              "metadata": {"name": name, "namespace": "fs2-models", "labels": labels},
              "data": {"qualify.py": Path(__file__).with_name("qualify.py").read_text()},
              "binaryData": {"cases.json.gz": base64.b64encode(packed).decode()}}
    spec = {"restartPolicy": "Never", "automountServiceAccountToken": False,
            "activeDeadlineSeconds": 1800,
            "securityContext": old["securityContext"], "nodeSelector": old["nodeSelector"],
            "tolerations": old["tolerations"],
            "containers": [{"name": "qualifier", "image": args.image,
                "command": ["python3", "/qualification/qualify.py", "--cases", "/qualification/cases.json.gz", "--output", "/tmp/outputs"],
                "resources": old["containers"][0]["resources"],
                "volumeMounts": [{"name": "qualification", "mountPath": "/qualification", "readOnly": True},
                                 {"name": "tmp", "mountPath": "/tmp"}]}],
            "volumes": [{"name": "qualification", "configMap": {"name": name}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "16Gi"}}]}
    if old.get("imagePullSecrets"):
        spec["imagePullSecrets"] = old["imagePullSecrets"]
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "fs2-models", "labels": labels}, "spec": spec}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "cases.json").write_bytes(raw)
    (args.output / "manifest.yaml").write_text(yaml.safe_dump_all([config, pod], sort_keys=False))
    receipt = {"image": args.image, "case_count": len(cases), "expected_inference_calls": len(cases) * 2,
               "cases_sha256": hashlib.sha256(raw).hexdigest(), "compressed_input_bytes": len(packed),
               "qualification_code_sha256": hashlib.sha256(config["data"]["qualify.py"].encode()).hexdigest(),
               "node_selector": spec["nodeSelector"], "resources": spec["containers"][0]["resources"], "applied": False}
    (args.output / "preparation.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()

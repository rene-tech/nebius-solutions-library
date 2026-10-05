"""Image-only website metadata rollout, preserving the current availability spec."""

import argparse
import copy
import json
import subprocess
from pathlib import Path

from remove_hot_floors import kubectl

EXPECTED = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/scientific-ai-website@sha256:bfc86922a3b209a3a95e25c489a702f2498e13ad1e427b12f2d5695a7300bece"
DESIRED = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/scientific-ai-website@sha256:af757125d4da9361f0510e9249642f6caf4e227d310e1b0e2261ea0e3ec8ee9b"


def main(args):
    args.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = "after" if args.verify_only else "before"
    check = subprocess.run(["node", str(args.routing_check)], check=True, text=True, capture_output=True)
    receipt = json.loads(check.stdout)
    assert receipt["ok"] is True
    (args.directory / ("routing-" + stage + ".json")).write_text(check.stdout)
    deployment = json.loads(kubectl(args.context, "-n", "fs2-system", "get", "deployment",
                                   "scientific-ai-website", "-o", "json"))
    assert deployment["spec"]["replicas"] == 2
    assert deployment["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["name"] == "website"
    if args.verify_only:
        assert container["image"] == DESIRED
        assert deployment["status"]["readyReplicas"] == 2
        assert deployment["status"]["updatedReplicas"] == 2
        expected_template = json.loads((args.directory / "website-before.json").read_text())["spec"]["template"]
        expected_template["spec"]["containers"][0]["image"] = DESIRED
        assert deployment["spec"]["template"] == expected_template
        print(json.dumps({"ready": 2, "public_routing": "passed", "image": DESIRED,
                          "only_image_changed": True}))
        return
    assert container["image"] == EXPECTED
    (args.directory / "website-before.json").write_text(json.dumps(deployment, indent=2) + "\n")
    patch = [
        {"op": "test", "path": "/metadata/resourceVersion", "value": deployment["metadata"]["resourceVersion"]},
        {"op": "test", "path": "/spec/template/spec/containers/0/image", "value": EXPECTED},
        {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": DESIRED},
    ]
    kubectl(args.context, "-n", "fs2-system", "patch", "deployment", "scientific-ai-website",
            "--type=json", "-p", json.dumps(patch))
    after = json.loads(kubectl(args.context, "-n", "fs2-system", "get", "deployment",
                              "scientific-ai-website", "-o", "json"))
    expected_spec = copy.deepcopy(deployment["spec"])
    expected_spec["template"]["spec"]["containers"][0]["image"] = DESIRED
    assert after["spec"] == expected_spec
    receipt = {"source_commit": "c45eeab", "previous": EXPECTED, "desired": DESIRED,
               "patch": patch, "only_image_changed": True}
    (args.directory / "website-rollout.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--routing-check", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    main(parser.parse_args())

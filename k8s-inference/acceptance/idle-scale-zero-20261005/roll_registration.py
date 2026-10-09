"""Roll only immutable controller envelope/bundle references, preserving images."""

import argparse
import copy
import json
import subprocess
from pathlib import Path

import httpx

from remove_hot_floors import kubectl


def main(args):
    configs = json.loads((args.directory / "registration-configmaps.json").read_text())
    names = {next(iter(c["data"])): c["metadata"]["name"] for c in configs["items"]}
    command = ["kubectl", "--context", args.context]
    config_apply = ["apply", "--server-side", "--field-manager=fs2-idle-registration",
                    "-f", str(args.directory / "registration-configmaps.json")]
    subprocess.run(command + config_apply + ["--dry-run=server"], check=True)
    httpx.get(args.origin + "/readyz", timeout=15).raise_for_status()
    subprocess.run(command + config_apply, check=True)
    receipt = []
    for name in ("fs2-serve-control-plane-model-controller", "fs2-serve-control-plane"):
        before = json.loads(kubectl(args.context, "-n", "fs2-system", "get", "deployment", name, "-o", "json"))
        operations = [{"op": "test", "path": "/metadata/resourceVersion", "value": before["metadata"]["resourceVersion"]}]
        expected = copy.deepcopy(before["spec"]["template"])
        for index, volume in enumerate(before["spec"]["template"]["spec"]["volumes"]):
            if volume.get("configMap", {}).get("name") in args.previous:
                key = volume["configMap"]["items"][0]["key"]
                path = f"/spec/template/spec/volumes/{index}/configMap/name"
                operations.append({"op": "replace", "path": path, "value": names[key]})
                expected["spec"]["volumes"][index]["configMap"]["name"] = names[key]
        if len(operations) != 3:
            raise RuntimeError("both expected original ConfigMaps must match; no partial rollout")
        (args.directory / (name + "-before.json")).write_text(json.dumps(before, indent=2) + "\n")
        kubectl(args.context, "-n", "fs2-system", "patch", "deployment", name, "--type=json", "-p", json.dumps(operations))
        after = json.loads(kubectl(args.context, "-n", "fs2-system", "get", "deployment", name, "-o", "json"))
        if after["spec"]["template"] != expected:
            raise RuntimeError("unexpected shared deployment template change")
        row = {"deployment": name, "previous_resource_version": before["metadata"]["resourceVersion"],
               "after_resource_version": after["metadata"]["resourceVersion"],
               "images_unchanged": [c["image"] for c in after["spec"]["template"]["spec"]["containers"]],
               "only_two_configmap_refs_changed": True, "patch": operations}
        receipt.append(row)
        print(json.dumps(row), flush=True)
    (args.directory / "rollout.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--previous", nargs=2, required=True)
    main(parser.parse_args())

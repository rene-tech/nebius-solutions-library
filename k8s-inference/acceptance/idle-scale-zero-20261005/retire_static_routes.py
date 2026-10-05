"""Remove redundant lean overlays only after matching managed Apps reconcile.

The static overlay labels its own binding lean-live-verified, which cannot be
used as canonical qualification by the managed route binder. Keep the exact
deployment-runtime selection and every unrelated static route unchanged.
"""

import argparse
import copy
import hashlib
import json
import subprocess
from pathlib import Path

from remove_hot_floors import kubectl


def main(args):
    args.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    before = json.loads(kubectl(args.context, "-n", "fs2-system", "get", "configmap", args.previous, "-o", "json"))
    routes = json.loads(before["data"]["lean-routes.json"])
    selected = set(args.models)
    if {r["model_id"] for r in routes["routes"] if r["model_id"] in selected} != selected:
        raise RuntimeError("all exact static routes must exist before cutover")
    for name in sorted(selected):
        model = json.loads(kubectl(args.context, "-n", "fs2-models", "get", "modeldeployment", name, "-o", "json"))
        if model["status"]["phase"] not in {"Cold", "Ready", "Desired", "Loading"} or not model["status"].get("endpoint"):
            raise RuntimeError("managed endpoint is not reconciled: " + name)
    routes["routes"] = [r for r in routes["routes"] if r["model_id"] not in selected]
    data = copy.deepcopy(before["data"])
    data["lean-routes.json"] = json.dumps(routes, sort_keys=True, separators=(",", ":")) + "\n"
    name = "fs2-idle-routes-" + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]
    config = {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
              "metadata": {"name": name, "namespace": "fs2-system"}, "data": data}
    (args.directory / "before-routes.json").write_text(json.dumps(before, indent=2) + "\n")
    path = args.directory / "managed-routes-configmap.json"
    path.write_text(json.dumps(config, indent=2) + "\n")
    base = ["kubectl", "--context", args.context]
    apply = ["apply", "--server-side", "--field-manager=fs2-idle-registration", "-f", str(path)]
    subprocess.run(base + apply + ["--dry-run=server"], check=True)
    subprocess.run(base + apply, check=True)
    if args.prepare_only:
        print(json.dumps({"configmap": name, "removed_static_overlays": sorted(selected),
                          "deployment_runtime_records_unchanged": True, "api_template_changed": False}))
        return
    deployment = json.loads(kubectl(args.context, "-n", "fs2-system", "get", "deployment", "fs2-serve-control-plane", "-o", "json"))
    expected = copy.deepcopy(deployment["spec"]["template"])
    patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": deployment["metadata"]["resourceVersion"]}]
    for index, volume in enumerate(deployment["spec"]["template"]["spec"]["volumes"]):
        if volume.get("configMap", {}).get("name") == args.previous:
            patch.append({"op": "replace", "path": f"/spec/template/spec/volumes/{index}/configMap/name", "value": name})
            expected["spec"]["volumes"][index]["configMap"]["name"] = name
    if len(patch) != 2:
        raise RuntimeError("exact one shared route volume must match")
    (args.directory / "api-before-route-cutover.json").write_text(json.dumps(deployment, indent=2) + "\n")
    kubectl(args.context, "-n", "fs2-system", "patch", "deployment", "fs2-serve-control-plane", "--type=json", "-p", json.dumps(patch))
    after = json.loads(kubectl(args.context, "-n", "fs2-system", "get", "deployment", "fs2-serve-control-plane", "-o", "json"))
    if after["spec"]["template"] != expected:
        raise RuntimeError("unrelated shared template changed")
    receipt = {"removed_static_overlays": sorted(selected), "configmap": name, "patch": patch,
               "deployment_runtime_records_unchanged": True, "only_one_reference_changed": True}
    (args.directory / "route-cutover.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--prepare-only", action="store_true")
    main(parser.parse_args())

"""Change only the scientific companion image on the captured live baseline.

Do not replay older Helm values: the live API/model routes have independent
releases. Retain a Helm values overlay and exact inverse patch for reconciliation.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

CONTEXT = "nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"
NAME = "fs2-serve-control-plane"
REPO = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane"
OLD = REPO + "@sha256:719ec336ef93e582f3031735dba974b61ea97f1c4ce47e630fe18eae9e9cd77c"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--expected-current", default=OLD)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    a = parser.parse_args()
    if not all(re.fullmatch(re.escape(REPO) + r"@sha256:[a-f0-9]{64}", image)
               for image in (a.image, a.expected_current)):
        raise ValueError("Require exact digest in the existing regional repository")
    os.umask(0o077)
    a.output.mkdir(parents=True, exist_ok=True)
    kube = ["kubectl", "--context", CONTEXT, "--request-timeout=30s", "-n", "fs2-system"]
    def get():
        return json.loads(subprocess.check_output(kube + ["get", "deployment", NAME, "-o", "json"]))
    def save(name, value):
        (a.output / name).write_text(json.dumps(value, indent=2) + "\n")
    if not a.apply:
        value = get()
        template = value["spec"]["template"]
        ci, ei = next((i, j) for i, c in enumerate(template["spec"]["containers"])
                      for j, e in enumerate(c["env"]) if e["name"] == "FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE")
        if template["spec"]["containers"][ci]["env"][ei]["value"] != a.expected_current:
            raise ValueError("Collector baseline changed; review it before activation")
        path = f"/spec/template/spec/containers/{ci}/env/{ei}/value"
        patch = [{"op": "test", "path": "/spec/template", "value": template},
                 {"op": "replace", "path": path, "value": a.image}]
        save("before.json", value)
        save("patch.json", patch)
        save("rollback.json", [{"op": "test", "path": path, "value": a.image},
                               {"op": "replace", "path": path, "value": a.expected_current}])
        save("helm-overlay.json", {"scientificBatch": {"toolsImage": a.image}})
        result = subprocess.check_output(kube + ["patch", "deployment", NAME, "--type=json",
            "--patch-file", str(a.output / "patch.json"), "--dry-run=server", "-o", "json"])
        save("dry-run.json", json.loads(result))
        print("Scoped collector patch prepared and server-side validated")
    else:
        patch = json.loads((a.output / "patch.json").read_text())
        if patch[-1]["value"] != a.image:
            raise ValueError("Requested image differs from prepared patch")
        subprocess.run(kube + ["patch", "deployment", NAME, "--type=json",
                              "--patch-file", str(a.output / "patch.json")], check=True)
        save("after.json", get())


if __name__ == "__main__":
    main()

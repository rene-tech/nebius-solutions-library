"""Render/apply only the chart's read-only node inventory RBAC."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import yaml

CONTEXT = "nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"
NAME = "fs2-serve-control-plane-model-controller-pool-reader"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[2]
    values = subprocess.check_output([
        "helm", "--kube-context", CONTEXT, "get", "values",
        "fs2-serve-control-plane", "-n", "fs2-system", "-o", "json",
    ])
    # The retained Helm revision predates separately qualified additive DB
    # migrations. Render only RBAC with this source's contract defaults; this
    # does not run a migration or apply any sibling resource or old values.
    values = json.loads(values)
    defaults = yaml.safe_load((root / "charts/control-plane/fs2-serve-control-plane/values.yaml").read_text())
    values.setdefault("migration", {})["releaseContract"] = defaults["migration"]["releaseContract"]
    rendered = subprocess.check_output([
        "helm", "template", "fs2-serve-control-plane",
        str(root / "charts/control-plane/fs2-serve-control-plane"),
        "-n", "fs2-system", "-f", "-", "--show-only", "templates/model-controller-rbac.yaml",
    ], input=json.dumps(values).encode())
    selected = [item for item in yaml.safe_load_all(rendered)
                if item and item.get("metadata", {}).get("name") == NAME]
    assert {item["kind"] for item in selected} == {"ClusterRole", "ClusterRoleBinding"}
    role = next(item for item in selected if item["kind"] == "ClusterRole")
    assert role["rules"] == [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["list"]}]
    path = args.output / "pool-reader.yaml"
    path.write_text(yaml.safe_dump_all(selected))
    command = ["kubectl", "--context", CONTEXT, "apply", "-f", str(path)]
    subprocess.run([*command, "--dry-run=server"], check=True)
    if args.apply:
        subprocess.run(command, check=True)
    print(json.dumps({"objects": 2, "applied": args.apply}))


if __name__ == "__main__":
    main()

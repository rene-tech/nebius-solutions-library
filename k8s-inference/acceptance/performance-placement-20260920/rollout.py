"""Preserve current Helm configuration while rolling out exact benchmark images."""

import argparse
import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts/control-plane/fs2-serve-control-plane"
RELEASE = "fs2-serve-control-plane"
REGISTRY = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/"


def output(command):
    return subprocess.check_output(command, stderr=subprocess.PIPE)


def private(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
        file.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("kubeconfig", "context", "directory", "cp-digest", "admin-digest", "admin-sbom", "commit", "tree"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--expected-revision", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    directory = Path(args.directory)
    helm = ["helm", "--kubeconfig", args.kubeconfig, "--kube-context", args.context, "-n", "fs2-system"]
    current = json.loads(output(helm + ["history", RELEASE, "--max", "1", "-o", "json"]))[-1]
    assert current["revision"] == args.expected_revision and current["status"] == "deployed", "live_release_changed"
    live = json.loads(output(helm + ["get", "values", RELEASE, "--all", "-o", "json"]))
    if args.apply:
        assert live == json.loads((directory / "previous-values.private.json").read_bytes()), "live_values_changed"
        receipt = json.loads((directory / "plan.json").read_bytes())
        values = directory / "candidate-values.private.json"
        assert hashlib.sha256(values.read_bytes()).hexdigest() == receipt["values_sha256"]
        assert receipt["previous_revision"] == current["revision"]
        # Exact schema manifests require forward-compatible recovery, not --atomic rollback.
        subprocess.run(helm + ["upgrade", RELEASE, str(CHART), "-f", str(values), "--wait=watcher",
                               "--wait-for-jobs", "--timeout", "20m"], check=True)
        return
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    private(directory / "previous-values.private.json", json.dumps(live).encode())
    private(directory / "previous-manifest.private.yaml", output(helm + ["get", "manifest", RELEASE]))
    candidate = copy.deepcopy(live)
    old_image = candidate["image"]["repository"] + "@" + candidate["image"]["digest"]
    new_image = REGISTRY + RELEASE + "@" + args.cp_digest
    changed = []

    def replace(value, path=()):
        if isinstance(value, dict):
            for key, child in value.items():
                if child == old_image:
                    value[key] = new_image
                    changed.append(".".join(path + (key,)))
                else:
                    replace(child, path + (key,))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                replace(child, path + (str(index),))

    replace(candidate)
    candidate["image"]["digest"] = args.cp_digest
    candidate["migration"]["releaseContract"] = yaml.safe_load((CHART / "values.yaml").read_text())["migration"]["releaseContract"]
    candidate["adminConsole"]["image"]["digest"] = args.admin_digest
    candidate["adminConsole"]["provenance"].update(sourceCommit=args.commit, sourceTree=args.tree, sbomSha256=args.admin_sbom)
    values = json.dumps(candidate, indent=2).encode()
    private(directory / "candidate-values.private.json", values)
    manifest = output(helm + ["template", RELEASE, str(CHART), "-f", str(directory / "candidate-values.private.json")])
    private(directory / "candidate-manifest.private.yaml", manifest)
    before = list(yaml.safe_load_all((directory / "previous-manifest.private.yaml").read_bytes()))
    after = list(yaml.safe_load_all(manifest))
    def identity(item):
        return item["kind"], item["metadata"].get("namespace"), item["metadata"]["name"]
    old = {identity(item): item for item in before if item}
    new = {identity(item): item for item in after if item and "helm.sh/hook" not in item["metadata"].get("annotations", {})}
    # Helm omits the completed migration Job from the retained release manifest.
    allowed_added = {("Job", None, RELEASE + "-migrate")}
    assert old.keys() <= new.keys() and new.keys() - old.keys() <= allowed_added, "unexpected_resource_inventory_change"
    diff = [key for key in old if old[key] != new[key]]
    assert all(key[0] in {"Deployment", "ConfigMap", "CronJob", "StatefulSet"} for key in diff), "unexpected_resource_kind_change"
    receipt = {"previous_revision": current["revision"], "cp_digest": args.cp_digest, "admin_digest": args.admin_digest,
               "values_sha256": hashlib.sha256(values).hexdigest(), "changed_resources": diff,
               "tools_image_references": changed, "automatic_placement": False}
    private(directory / "plan.json", json.dumps(receipt, indent=2).encode())
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()

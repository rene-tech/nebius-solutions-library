#!/usr/bin/env python3
"""Guarded Helm preparation/apply; private release values never enter evidence.

Preserves every live value except the explicit image, migration contract and
tenant-retirement schema contract. Does not provision or resize any model/node.
"""

import argparse
import copy
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts/control-plane/fs2-serve-control-plane"
RELEASE = "fs2-serve-control-plane"
REPOSITORY = (
    "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane"
)


def output(command):
    return subprocess.check_output(command, stderr=subprocess.PIPE)


def private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(data)


def main(args):
    helm = [
        "helm",
        "--kubeconfig",
        args.kubeconfig,
        "--kube-context",
        args.context,
        "-n",
        "fs2-system",
    ]
    current = json.loads(output(helm + ["history", RELEASE, "-o", "json"]))[-1]
    assert (
        current["revision"] == args.expected_revision
        and current["status"] == "deployed"
    ), "live_release_changed"
    if args.apply:
        receipt = json.loads((args.directory / "plan.json").read_bytes())
        assert receipt["previous_revision"] == current["revision"]
        values = args.directory / "candidate-values.json"
        assert (
            hashlib.sha256(values.read_bytes()).hexdigest() == receipt["values_sha256"]
        )
        assert json.loads(
            output(helm + ["get", "values", RELEASE, "--all", "-o", "json"])
        ) == json.loads((args.directory / "previous-values.json").read_bytes())
        subprocess.run(
            helm
            + [
                "upgrade",
                RELEASE,
                str(CHART),
                "-f",
                str(values),
                "--wait=watcher",
                "--wait-for-jobs",
                # Migration 0034 advances the exact schema manifest. Recovery
                # must use a compatible forward fix, not the old schema-33 image.
                "--timeout",
                "20m",
            ],
            check=True,
        )
        return
    args.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    original = output(helm + ["get", "values", RELEASE, "--all", "-o", "json"])
    live = json.loads(original)
    assert live["image"]["digest"] == args.previous_digest, "live_image_changed"
    private(args.directory / "previous-values.json", original)
    private(
        args.directory / "previous-manifest.yaml",
        output(helm + ["get", "manifest", RELEASE]),
    )
    candidate = copy.deepcopy(live)
    candidate["image"]["repository"] = REPOSITORY
    candidate["image"]["digest"] = args.digest
    candidate["migration"]["releaseContract"] = yaml.safe_load(
        (CHART / "values.yaml").read_text()
    )["migration"]["releaseContract"]
    # The current chart exposes tools image through runtimeEnv, not a chart key.
    changed = []

    def replace_tools(value, path=()):
        if isinstance(value, dict):
            for key, child in value.items():
                if (
                    isinstance(child, str)
                    and child == REPOSITORY + "@" + args.previous_digest
                ):
                    value[key] = REPOSITORY + "@" + args.digest
                    changed.append(".".join(path + (key,)))
                else:
                    replace_tools(child, path + (key,))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                replace_tools(child, path + (str(index),))

    replace_tools(candidate)
    data = (json.dumps(candidate, indent=2) + "\n").encode()
    private(args.directory / "candidate-values.json", data)
    rendered = output(
        helm
        + [
            "template",
            RELEASE,
            str(CHART),
            "-f",
            str(args.directory / "candidate-values.json"),
        ]
    )
    private(args.directory / "candidate-manifest.yaml", rendered)
    before = list(
        yaml.safe_load_all((args.directory / "previous-manifest.yaml").read_text())
    )
    after = list(yaml.safe_load_all(rendered))

    def resources(items):
        return {(o["kind"], o["metadata"]["name"]): o for o in items if o}

    old, new = resources(before), resources(after)
    # Hooks (the migration Job) are absent from helm get manifest.
    assert set(old) <= set(new), "resource_removal_requires_review"
    receipt = {
        "at": datetime.now(UTC).isoformat(),
        "release": RELEASE,
        "previous_revision": current["revision"],
        "previous_digest": args.previous_digest,
        "candidate_digest": args.digest,
        "values_sha256": hashlib.sha256(data).hexdigest(),
        "tools_image_paths": changed,
        "starter_pack": candidate["customerStorage"]["starterPack"],
        "changed_resources": [
            f"{k}/{name}" for k, name in new if new[(k, name)] != old.get((k, name))
        ],
    }
    (args.directory / "plan.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--expected-revision", type=int, required=True)
    parser.add_argument("--previous-digest", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    main(parser.parse_args())

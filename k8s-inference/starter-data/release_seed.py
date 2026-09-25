#!/usr/bin/env python3
"""Render/apply only starter-pack pins using the exact currently installed chart.

Private release data stay in --output. No historical worktree chart, application
image, model revision, cloud limit or unrelated setting is promoted here.
"""

from __future__ import annotations

import argparse
import base64
import copy
import gzip
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

import yaml


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def extract_chart(chart, root):
    root.mkdir(parents=True, exist_ok=False)
    (root / "Chart.yaml").write_text(yaml.safe_dump(chart["metadata"], sort_keys=False))
    (root / "values.yaml").write_text(
        yaml.safe_dump(chart.get("values", {}), sort_keys=False)
    )
    for item in [*chart.get("templates", []), *chart.get("files", [])]:
        relative = PurePosixPath(item["name"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid_chart_member")
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(item["data"]))
    if chart.get("schema"):
        (root / "values.schema.json").write_bytes(base64.b64decode(chart["schema"]))
    for child in chart.get("dependencies", []):
        extract_chart(child, root / "charts" / child["metadata"]["name"])


def resources(content):
    result = {}
    for resource in yaml.safe_load_all(content):
        if not resource:
            continue
        identity = (
            resource["kind"],
            resource["metadata"].get("namespace", "fs2-system"),
            resource["metadata"]["name"],
        )
        if identity in result:
            raise ValueError("duplicate_rendered_resource")
        result[identity] = resource
    return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--canary-tenant", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", args.image) or not re.fullmatch(
        r"[a-f0-9]{64}", args.manifest_sha256
    ):
        raise ValueError("immutable_image_and_manifest_required")
    args.output.mkdir(parents=True, exist_ok=False)
    name = "fs2-serve-control-plane"
    helm = [
        "helm",
        "--kubeconfig",
        args.kubeconfig,
        "--kube-context",
        args.context,
        "-n",
        "fs2-system",
    ]
    kube = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        args.context,
        "--request-timeout=30s",
        "-n",
        "fs2-system",
    ]
    history = json.loads(
        subprocess.check_output(helm + ["history", name, "--max", "1", "-o", "json"])
    )
    if len(history) != 1 or history[0]["status"] != "deployed":
        raise ValueError("release_not_stable")
    revision = history[0]["revision"]
    secret = json.loads(
        subprocess.check_output(
            kube
            + ["get", "secret", f"sh.helm.release.v1.{name}.v{revision}", "-o", "json"]
        )
    )
    raw = gzip.decompress(base64.b64decode(base64.b64decode(secret["data"]["release"])))
    release = json.loads(raw)
    write(args.output / "release-private.json", release)
    chart = args.output / "chart"
    extract_chart(release["chart"], chart)
    values = copy.deepcopy(release["config"])
    previous = copy.deepcopy(values["customerStorage"]["starterPack"])
    desired = values["customerStorage"]["starterPack"]
    desired.update(
        enabled=True,
        image=args.image,
        manifestSha256=args.manifest_sha256,
        tenants=args.canary_tenant,
    )
    write(args.output / "values-private.json", values)
    rendered = subprocess.check_output(
        helm
        + [
            "template",
            name,
            str(chart),
            "--is-upgrade",
            "-f",
            str(args.output / "values-private.json"),
        ]
    )
    (args.output / "rendered-private.yaml").write_bytes(rendered)
    prior_manifests = [
        release["manifest"],
        *(hook["manifest"] for hook in release.get("hooks", [])),
    ]
    old, new = resources("\n---\n".join(prior_manifests)), resources(rendered)
    if set(old) != set(new):
        raise ValueError("seed_update_changed_resource_set")
    changes = [key for key in old if old[key] != new[key]]
    if changes != [("Deployment", "fs2-system", name)]:
        write(args.output / "unexpected-changes.json", changes)
        raise ValueError("seed_update_changed_unrelated_resource")

    # Strip only the seed init container and seed environment before comparison.
    def without_seed(resource):
        obj = copy.deepcopy(resource)
        spec = obj["spec"]["template"]["spec"]
        spec["initContainers"] = [
            c
            for c in spec.get("initContainers", [])
            if c["name"] != "install-starter-pack"
        ]
        for container in spec["containers"]:
            container["env"] = [
                e
                for e in container.get("env", [])
                if not e["name"].startswith("FS2_USER_STORAGE_STARTER_PACK_")
            ]
        return obj

    key = changes[0]
    if without_seed(old[key]) != without_seed(new[key]):
        raise ValueError("seed_update_changed_unrelated_gateway_fields")
    receipt = {
        "previous_revision": revision,
        "previous_seed": previous,
        "next_seed": desired,
        "chart_sha256": hashlib.sha256(
            json.dumps(release["chart"], sort_keys=True).encode()
        ).hexdigest(),
        "changed_resources": changes,
        "unrelated_gateway_fields_unchanged": True,
        "applied": False,
    }
    write(args.output / "render-receipt.json", receipt)
    # Helm's server dry-run validates release operations without printing values.
    dry = subprocess.run(
        helm
        + [
            "upgrade",
            name,
            str(chart),
            "-f",
            str(args.output / "values-private.json"),
            "--dry-run=server",
        ],
        capture_output=True,
        check=False,
    )
    (args.output / "dry-run-private.txt").write_bytes(dry.stdout + dry.stderr)
    if dry.returncode:
        raise ValueError("helm_seed_dry_run_failed")
    if args.apply:
        current = json.loads(
            subprocess.check_output(
                helm + ["history", name, "--max", "1", "-o", "json"]
            )
        )
        if current != history:
            raise ValueError("live_release_changed_recapture_instead_of_overwrite")
        applied = subprocess.run(
            helm
            + [
                "upgrade",
                name,
                str(chart),
                "-f",
                str(args.output / "values-private.json"),
                "--wait",
                "--timeout",
                "10m",
                "--rollback-on-failure",
            ],
            capture_output=True,
            check=False,
        )
        (args.output / "apply-private.txt").write_bytes(applied.stdout + applied.stderr)
        if applied.returncode:
            raise ValueError("helm_seed_rollout_failed")
        receipt["applied"] = True
        receipt["new_revision"] = json.loads(
            subprocess.check_output(
                helm + ["history", name, "--max", "1", "-o", "json"]
            )
        )[0]["revision"]
    write(args.output / "receipt.json", receipt)
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()

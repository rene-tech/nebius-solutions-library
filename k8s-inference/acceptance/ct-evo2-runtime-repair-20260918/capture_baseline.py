"""Read-only four-map and owner capture; never inspect Secret values or apply."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--expected-release", type=int, default=159)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    kubectl, helm = shutil.which("kubectl"), shutil.which("helm")
    if not kubectl or not helm:
        raise ValueError("kubectl and helm are required")
    common = [kubectl, "--kubeconfig", str(args.kubeconfig), "--context", args.context]

    def read(namespace, resource, *names):
        command = [*common, "-n", namespace, "get", resource, *names, "-o", "json"]
        return json.loads(subprocess.check_output(command))  # noqa: S603 - fixed read-only kubectl argv

    def release():
        command = [
            helm,
            "--kubeconfig",
            str(args.kubeconfig),
            "--kube-context",
            args.context,
            "status",
            "fs2-serve-control-plane",
            "-n",
            "fs2-system",
            "-o",
            "json",
        ]
        status = json.loads(subprocess.check_output(command))  # noqa: S603 - fixed Helm status, explicit context
        result = {
            "name": status["name"],
            "namespace": status["namespace"],
            "version": status["version"],
            "status": status["info"]["status"],
            "last_deployed": status["info"]["last_deployed"],
        }
        if result["version"] != args.expected_release or result["status"] != "deployed":
            raise ValueError("unexpected live release; recapture after coordinated deployment")
        return result

    before = release()
    controllers = read(
        "fs2-system", "deployment", "fs2-serve-control-plane", "fs2-serve-control-plane-model-controller"
    )
    serving = next(item for item in controllers["items"] if item["metadata"]["name"] == "fs2-serve-control-plane")
    volume_names = {
        volume["name"]: volume["configMap"]["name"]
        for volume in serving["spec"]["template"]["spec"]["volumes"]
        if "configMap" in volume
    }
    roles = {
        name: volume_names[name]
        for name in ("model-controller-envelope", "model-controller-bundles", "lean-routes", "admin-configuration")
    }
    controller = next(item for item in controllers["items"] if item is not serving)
    owner_volumes = {
        volume["name"]: volume["configMap"]["name"]
        for volume in controller["spec"]["template"]["spec"]["volumes"]
        if "configMap" in volume
    }
    if (
        owner_volumes["infrastructure-envelope"] != roles["model-controller-envelope"]
        or owner_volumes["renderer-bundles"] != roles["model-controller-bundles"]
    ):
        raise ValueError("gateway/controller mounted contracts differ")
    maps = {role: read("fs2-system", "configmap", name) for role, name in roles.items()}
    owners = read("fs2-models", "modeldeployments")
    after = release()
    if before != after:
        raise ValueError("release changed during capture")
    output = {
        "live-configmaps.json": {"apiVersion": "v1", "kind": "List", "items": list(maps.values())},
        "live-routes.json": maps["lean-routes"],
        "live-admin-configuration.json": maps["admin-configuration"],
        "modeldeployments.json": owners,
        "controllers.json": controllers,
    }
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    hashes = {}
    for filename, value in output.items():
        raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
        (args.output / filename).write_bytes(raw)
        hashes[filename] = hashlib.sha256(raw).hexdigest()
    receipt = {
        "captured_at": datetime.now(UTC).isoformat(),
        "applied": False,
        "release": after,
        "context": args.context,
        "configmaps": roles,
        "sha256": hashes,
    }
    (args.output / "capture.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"applied": False, "release": after["version"], "models": len(owners["items"]), "maps": roles}))


if __name__ == "__main__":
    main()

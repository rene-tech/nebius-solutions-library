"""Capture the public catalog and non-secret hardware inventory for a campaign."""

import argparse
import base64
import json
import os
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx


def admin_client(kubeconfig, context, origin):
    # Cluster workers receive a read-only Secret mount, never a kubeconfig.
    token_file = os.environ.get("FS2_BENCHMARK_ADMIN_TOKEN_FILE")
    if token_file:
        token = Path(token_file).read_text().strip()
    else:
        secret = json.loads(subprocess.check_output([
            "kubectl", "--kubeconfig", kubeconfig, "--context", context,
            "-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json",
        ], stderr=subprocess.PIPE))
        token = base64.b64decode(secret["data"]["token"]).decode().strip()
    client = httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=90, trust_env=False)
    try:
        response = client.post("/admin/api/v1/session", headers={"Authorization": "Bearer " + token})
        response.raise_for_status()
    except Exception:
        client.close()
        raise
    return client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=False)
    with closing(admin_client(args.kubeconfig, args.context, args.origin)) as client:
        for name, path in {"inventory": "model-inventory", "apps": "apps", "capacity": "capacity/summary"}.items():
            response = client.get("/admin/api/v1/" + path)
            response.raise_for_status()
            (args.directory / (name + ".json")).write_text(json.dumps(response.json(), indent=2))
    kube = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context]
    nodes = json.loads(subprocess.check_output(kube + ["get", "nodes", "-o", "json"]))
    hardware = [{
        "node": n["metadata"]["name"], "labels": n["metadata"].get("labels", {}),
        "allocatable": n["status"].get("allocatable", {}),
        "node_info": n["status"].get("nodeInfo", {}),
    } for n in nodes["items"]]
    (args.directory / "hardware.json").write_text(json.dumps(hardware, indent=2))
    inventory = json.loads((args.directory / "inventory.json").read_text())["data"]
    print(json.dumps({"observed_at": datetime.now(UTC).isoformat(), "inventory": inventory}, indent=2))


if __name__ == "__main__":
    main()

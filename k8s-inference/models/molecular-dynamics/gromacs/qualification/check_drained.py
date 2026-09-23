"""Read-only deployment preflight: never change a GROMACS profile mid-operation."""

import argparse
import base64
import json
import subprocess

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    args = parser.parse_args()
    secret = json.loads(
        subprocess.check_output(
            [
                "kubectl",
                "--kubeconfig",
                args.kubeconfig,
                "--context",
                args.context,
                "-n",
                "fs2-system",
                "get",
                "secret",
                "fs2-serve-admin",
                "-o",
                "json",
            ]
        )
    )
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    origin = "https://89.169.99.188"
    with httpx.Client(
        base_url=origin, headers={"origin": origin}, timeout=30, trust_env=False
    ) as client:
        response = client.post(
            "/admin/api/v1/session", headers={"authorization": "Bearer " + token}
        )
        response.raise_for_status()
        live = []
        for model in ("gromacs", "gromacs-mpi"):
            for status in ("queued", "activating", "running"):
                response = client.get(
                    "/admin/api/v1/operations",
                    params={"model_id": model, "status": status, "limit": 200},
                )
                response.raise_for_status()
                rows = response.json()["data"]["items"]
                live.extend(
                    {"id": row["id"], "model_id": model, "status": status}
                    for row in rows
                )
        client.delete("/admin/api/v1/session").raise_for_status()
    print(json.dumps({"drained": not live, "nonterminal_operations": live}))
    if live:
        raise SystemExit("Wait for these runs before changing their execution identity")


if __name__ == "__main__":
    main()

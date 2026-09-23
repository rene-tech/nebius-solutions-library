"""Inspect/issue/revoke a native MD qualification key; never print credentials.

The key belongs to an existing enabled user in the existing internal tenant.
No customer grants, quotas, bucket policies, or prior keys are changed.
"""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

import httpx

KEY_NAME = "native-md-qualification-20260923"
KEY_NAMES = {KEY_NAME, "amber-qualification-20260923"}
MODELS = ["gromacs", "gromacs-mpi", "lammps", "namd", "amber"]


def private(path, value):
    with path.open("x") as handle:
        path.chmod(0o600)
        json.dump(value, handle, indent=2)
        handle.write("\n")


def result(response):
    if response.is_error:
        raise RuntimeError(
            f"HTTP {response.status_code} {response.request.url.path}; "
            f"request_id={response.headers.get('x-request-id', 'unavailable')}"
        )
    return response.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "issue", "revoke"])
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--tenant", default="rene")
    parser.add_argument("--source-key", default="internal-test")
    parser.add_argument("--key-name", choices=sorted(KEY_NAMES), default=KEY_NAME)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--model", choices=MODELS, action="append")
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    if args.receipt.exists() or (
        args.action == "issue" and (not args.key_file or args.key_file.exists())
    ):
        raise ValueError("Use new private receipt/key paths.")
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
    with httpx.Client(
        base_url=args.origin,
        timeout=60,
        trust_env=False,
        headers={"origin": args.origin},
    ) as client:
        result(
            client.post(
                "/admin/api/v1/session", headers={"authorization": "Bearer " + token}
            )
        )
        rows = result(
            client.get("/admin/api/v1/keys", params={"tenant_id": args.tenant})
        )["data"]["items"]
        sources = [
            row
            for row in rows
            if row["name"] == args.source_key and not row["revoked_at"]
        ]
        if len(sources) != 1:
            raise ValueError(
                "Expected one active source key; inspect the internal tenant without changing policy."
            )
        source = sources[0]
        users = result(
            client.get("/admin/api/v1/users", params={"tenant_id": args.tenant})
        )["data"]["items"]
        owner = next(
            row for row in users if row["principal_id"] == source["principal_id"]
        )
        if not owner["enabled"] or owner["app_ids"] is not None:
            raise ValueError(
                "Expected an enabled internal user with existing all-App access."
            )
        record = {
            "action": args.action,
            "at": datetime.now(timezone.utc).isoformat(),
            "tenant_id": args.tenant,
            "principal_id": source["principal_id"],
            "source_key_id": source["id"],
            "source_key_changed": False,
            "owner_id": owner["id"],
        }
        if args.action == "inspect":
            record["policy"] = {
                key: source.get(key)
                for key in (
                    "scopes",
                    "models",
                    "max_concurrency",
                    "request_budget",
                    "gpu_seconds_budget",
                )
            }
        elif args.action == "issue":
            payload = {
                key: source[key]
                for key in ("tenant_id", "principal_id", "scopes", "max_concurrency")
            }
            payload.update(
                name=args.key_name,
                models=args.model or ["gromacs"],
                scopes=sorted(set(source["scopes"]) | {"artifacts.write"}),
                expires_at=(
                    datetime.now(timezone.utc) + timedelta(hours=12)
                ).isoformat(),
            )
            issued = result(client.post("/admin/api/v1/keys", json=payload))["data"]
            private(
                args.key_file,
                {"secret": issued["secret"], "key": issued["key"], "disposable": True},
            )
            record["token_id"] = issued["key"]["id"]
        else:
            value = json.loads(args.key_file.read_text())
            if (
                not value.get("disposable")
                or value["key"]["name"] not in KEY_NAMES | {"gromacs-qualification-20260923"}
            ):
                raise ValueError("Only this task-owned disposable key can be revoked.")
            revoked = result(client.delete("/admin/api/v1/keys/" + value["key"]["id"]))[
                "data"
            ]
            if not revoked["revoked_at"]:
                raise RuntimeError("Revocation did not persist.")
            record["token_id"] = value["key"]["id"]
            record["revoked_at"] = revoked["revoked_at"]
        client.delete("/admin/api/v1/session")
    private(args.receipt, record)
    print(json.dumps(record))


if __name__ == "__main__":
    main()

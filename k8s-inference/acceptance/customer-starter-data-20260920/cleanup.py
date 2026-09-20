#!/usr/bin/env python3
"""Revoke only this task's idle test keys/users; retain buckets and all evidence."""

import argparse
import base64
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx

OWNERS = {
    ("fs2-starter-data-acceptance-20260920", "seed-canary"),
    ("fs2-starter-data-acceptance-20260920", "shared-peer"),
    ("fs2-starter-private-20260920", "private-canary"),
    ("fs2-starter-private-20260920", "private-new-canary"),
}


def main(args):
    accesses = [json.loads(path.read_bytes()) for path in args.key_files]
    assert len(accesses) == 3 and len({a["key_id"] for a in accesses}) == 3
    assert all((a["tenant_id"], a["principal_id"]) in OWNERS for a in accesses)
    assert len({a["origin"] for a in accesses}) == 1
    origin = accesses[0]["origin"]
    if not args.check_only:
        for access in accesses:
            response = httpx.get(
                origin + "/v1/me",
                headers={"authorization": "Bearer " + access["secret"]},
                timeout=60,
            )
            response.raise_for_status()
            current = response.json()
            assert (
                current["tenant_id"] == access["tenant_id"]
                and current["principal_id"] == access["principal_id"]
            )
            assert current["available_slots"] == current["max_concurrency"], (
                "test_operations_still_active"
            )
    token = (
        base64.b64decode(
            json.loads(
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
            )["data"]["token"]
        )
        .decode()
        .strip()
    )
    report = {
        "at": datetime.now(UTC).isoformat(),
        "key_ids": [a["key_id"] for a in accesses],
        "users": [],
        "buckets_retained": True,
        "cloud_quotas_unchanged": True,
    }
    with httpx.Client(base_url=origin, headers={"origin": origin}, timeout=60) as admin:
        admin.post(
            "/admin/api/v1/session", headers={"authorization": "Bearer " + token}
        ).raise_for_status()
        response = admin.get("/admin/api/v1/users?limit=1000")
        response.raise_for_status()
        users = [
            u
            for u in response.json()["data"]["items"]
            if (u["tenant_id"], u["principal_id"]) in OWNERS
        ]
        assert len(users) == 4
        if not args.check_only:
            for access in accesses:
                admin.delete(
                    "/admin/api/v1/keys/" + access["key_id"]
                ).raise_for_status()
            for user in users:
                admin.patch(
                    "/admin/api/v1/users/" + user["id"], json={"enabled": False}
                ).raise_for_status()
        for user in users:
            response = admin.get(f"/admin/api/v1/users/{user['id']}/storage")
            response.raise_for_status()
            storage = response.json()["data"]
            report["users"].append(
                {
                    "id": user["id"],
                    "tenant_id": user["tenant_id"],
                    "principal_id": user["principal_id"],
                    "storage_state": storage["state"],
                }
            )
            if args.check_only:
                assert not user["enabled"] and storage["state"] == "disabled"
    for access in accesses:
        response = httpx.get(
            origin + "/v1/me",
            headers={"authorization": "Bearer " + access["secret"]},
            timeout=60,
        )
        assert response.status_code in {401, 403}, "test_key_still_authorized"
    report["keys_rejected"] = True
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "keys_revoked": 3,
                "users_disabled": 4,
                "storage_disabled_confirmed": args.check_only,
                "buckets_retained": True,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--key-files", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check-only", action="store_true")
    main(parser.parse_args())

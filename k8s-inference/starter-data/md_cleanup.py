#!/usr/bin/env python3
"""Retire only idle MD starter test identities, retaining buckets and evidence."""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx2 as httpx
import run_example as runner
from md_acceptance import PRINCIPAL, TENANTS, admin_session, checked


def main(args):
    os.umask(0o077)
    access = json.loads(args.access.read_bytes())
    if (
        access["tenant_id"] not in TENANTS
        or access["principal_id"] != PRINCIPAL
        or not access.get("disposable")
    ):
        raise ValueError("disposable_md_test_identity_required")
    args.origin = access["origin"]
    with httpx.Client(
        base_url=args.origin,
        timeout=60,
        trust_env=False,
        headers={"authorization": "Bearer " + access["secret"]},
    ) as customer:
        if not args.check_only:
            me = checked(customer.get("/v1/me"))
            if (me["tenant_id"], me["principal_id"]) != (
                access["tenant_id"],
                PRINCIPAL,
            ):
                raise ValueError("caller_identity_changed")
            cursor = None
            for _ in range(50):
                params = {"limit": 200}
                if cursor:
                    params["cursor"] = cursor
                page = checked(customer.get("/v1/operations", params=params))
                if any(
                    op["status"]
                    not in {"succeeded", "failed", "cancelled", "expired", "preempted"}
                    for op in page["data"]
                ):
                    raise ValueError("test_operations_still_active")
                cursor = page.get("next_cursor")
                if not cursor:
                    break
            else:
                raise ValueError("operation_inventory_incomplete")
        with admin_session(args) as admin:
            users = checked(
                admin.get(
                    "/admin/api/v1/users", params={"tenant_id": access["tenant_id"]}
                )
            )["data"]["items"]
            if len(users) != 1 or users[0]["id"] != access["user_id"]:
                raise ValueError("test_owner_inventory_changed")
            if not args.check_only:
                admin.delete(
                    "/admin/api/v1/keys/" + access["key_id"]
                ).raise_for_status()
                checked(
                    admin.patch(
                        "/admin/api/v1/users/" + access["user_id"],
                        json={"enabled": False},
                    )
                )
            storage = checked(
                admin.get(f"/admin/api/v1/users/{access['user_id']}/storage")
            )["data"]
            if args.check_only and (
                users[0]["enabled"] or storage["state"] != "disabled"
            ):
                raise ValueError("test_identity_not_fully_retired")
        if customer.get("/v1/me").status_code not in (401, 403):
            raise ValueError("test_key_still_authorized")
    report = {
        "at": datetime.now(UTC).isoformat(),
        "tenant_id": access["tenant_id"],
        "key_id": access["key_id"],
        "key_rejected": True,
        "storage_state": storage["state"],
        "bucket_retained": True,
        "customer_identity_or_quota_changes": False,
    }
    runner.save(args.output, report)
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--check-only", action="store_true")
    main(parser.parse_args())

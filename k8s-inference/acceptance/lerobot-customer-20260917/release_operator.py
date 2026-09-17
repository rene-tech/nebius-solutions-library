#!/usr/bin/env python3
"""Scoped LeRobot release actions; the customer client never needs this script.

Issue a disposable robotics key with Timothy's unchanged limits, the two
required Apps and dataset upload scope. After acceptance, apply only these
required grants to his existing key.
Credentials stay in memory or a private key file, never in receipts/stdout.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stockholm-customer-20260917"))
from collect_live import check, kube, policy, private_json, write_private  # noqa: E402

APP = "cosmos3-lerobot-augmentation"
MODELS = {"cosmos3-nano", APP}
PREFIX = "robotics-lerobot-canary-"


def source_policy(rows: list[dict]) -> dict:
    matches = [row for row in rows if row["name"] == "timmothy-cosmos3" and not row["revoked_at"]]
    check(len(matches) == 1, "exact_active_timothy_key_required")
    row = matches[0]
    check(row["tenant_id"] == "robotics", "wrong_source_tenant")
    check(set(row["models"]) in ({"cosmos3-nano"}, MODELS), "unexpected_source_models")
    check(row["max_concurrency"] == 1, "unexpected_source_concurrency")
    return row


def successful(response: httpx.Response) -> dict | list:
    # Error bodies may carry user data; preserve the correlation ID only.
    if response.is_error:
        raise RuntimeError(
            f"HTTP {response.status_code} {response.request.method} {response.request.url.path}; "
            f"request_id={response.headers.get('x-request-id', 'unavailable')}"
        )
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "issue", "revoke", "grant"))
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    check(not args.receipt.exists(), "receipt_exists")
    if args.action in {"issue", "revoke"}:
        check(args.key_file is not None, "key_file_required")
    if args.action == "issue":
        check(not args.key_file.exists(), "key_file_exists")
    secret = kube(args.kubeconfig, args.context, "-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json")
    admin_token = base64.b64decode(secret["data"]["token"]).decode().strip()
    with httpx.Client(base_url=args.origin, timeout=45, trust_env=False, headers={"origin": args.origin}) as client:
        admin_headers = {"authorization": "Bearer " + admin_token}
        successful(client.post("/admin/api/v1/session", headers=admin_headers))
        rows = successful(client.get("/admin/api/v1/keys", params={"tenant_id": "robotics"}))["data"]["items"]
        check(isinstance(rows, list), "invalid_token_list")
        source = source_policy(rows)
        intended = dict(
            policy(source), models=sorted(MODELS), scopes=sorted(set(source["scopes"]) | {"artifacts.write"})
        )
        receipt = {
            "at": datetime.now(UTC).isoformat(),
            "action": args.action,
            "source_token_id": source["id"],
            "before_policy": policy(source),
        }
        if args.action == "inspect":
            receipt["intended_canary_policy"] = intended
        elif args.action == "issue":
            name = PREFIX + str(uuid4())
            payload = dict(
                intended, principal_id=name, name=name, expires_at=(datetime.now(UTC) + timedelta(hours=12)).isoformat()
            )
            issued = successful(client.post("/admin/api/v1/keys", json=payload))["data"]
            check(isinstance(issued, dict) and issued.get("secret"), "token_missing")
            write_private(
                args.key_file,
                {
                    "schema": "fs2-customer-key/v1",
                    "disposable": True,
                    "token_id": issued["key"]["id"],
                    "principal_id": name,
                    "secret": issued["secret"],
                    "policy": intended,
                },
            )
            receipt.update(token_id=issued["key"]["id"], policy=intended, existing_keys_changed=False)
        elif args.action == "revoke":
            key = private_json(args.key_file)
            check(key.get("disposable") is True, "disposable_key_required")
            matches = [row for row in rows if row["id"] == key["token_id"]]
            check(len(matches) == 1 and matches[0]["name"].startswith(PREFIX), "canary_identity_mismatch")
            revoked = successful(client.delete("/admin/api/v1/keys/" + key["token_id"]))["data"]
            check(isinstance(revoked, dict) and revoked.get("revoked_at"), "revocation_missing")
            # Expected authentication failure is the acceptance assertion here.
            response = client.get("/v1/models", headers={"authorization": "Bearer " + key["secret"]})
            check(response.status_code == 401, "revoked_key_still_usable")
            receipt.update(token_id=key["token_id"], revoked_at=revoked["revoked_at"], public_status=401)
        else:
            successful(
                client.patch(
                    "/admin/api/v1/keys/" + source["id"],
                    json={"models": intended["models"], "scopes": intended["scopes"]},
                )
            )
            after_rows = successful(client.get("/admin/api/v1/keys", params={"tenant_id": "robotics"}))["data"]["items"]
            after = source_policy(after_rows)
            check(after["id"] == source["id"] and after["fingerprint"] == source["fingerprint"], "key_identity_changed")
            check(policy(after) == intended, "unexpected_policy_change")
            receipt.update(after_policy=policy(after), key_rotated=False)
        client.delete("/admin/api/v1/session")
    write_private(args.receipt, receipt, (admin_token,))
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()

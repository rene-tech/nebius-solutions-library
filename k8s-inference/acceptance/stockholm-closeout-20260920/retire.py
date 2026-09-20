#!/usr/bin/env python3
"""Explicit Stockholm closeout operator; test a disposable tenant first.

No cloud resources or shared App settings are deleted. Secrets stay in protected
files or memory. Retirement preserves historical token/operation relations.
"""

import argparse
import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import httpx

ORIGIN = "https://89.169.99.188"
CANARY = "stockholm-closeout-check-20260920"


def private(path, data):
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
    ) as out:
        json.dump(data, out, indent=2)


def ok(response):
    if response.is_error:
        raise RuntimeError(
            f"{response.request.method} {response.request.url.path}: HTTP {response.status_code}; "
            f"request_id={response.headers.get('x-request-id')}"
        )
    return response.json()


def retained_identities(client, tenant):
    selected = {}
    for resource, fields in {
        "keys": (
            "id",
            "tenant_id",
            "principal_id",
            "name",
            "models",
            "scopes",
            "max_concurrency",
            "request_budget",
            "gpu_seconds_budget",
            "expires_at",
            "revoked_at",
            "state",
            "rate_limit_requests",
            "rate_window_seconds",
        ),
        "users": (
            "id",
            "tenant_id",
            "principal_id",
            "display_name",
            "kind",
            "team",
            "enabled",
            "academic_eligible",
            "app_ids",
        ),
    }.items():
        page = ok(client.get("/admin/api/v1/" + resource, params={"limit": 1000}))[
            "data"
        ]
        assert not page.get("truncated", False) and not page.get("next_cursor"), (
            "identity inventory truncated"
        )
        selected[resource] = sorted(
            [
                {f: row.get(f) for f in fields}
                for row in page["items"]
                if row["tenant_id"] != tenant
            ],
            key=lambda r: r["id"],
        )
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["canary", "retire-stockholm"])
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume-canary", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(
            json.dumps(
                {
                    "action": args.action,
                    "tenant": CANARY if args.action == "canary" else "stockholm",
                    "mutation": False,
                }
            )
        )
        return
    os.umask(0o077)
    assert not args.resume_canary or args.action == "canary"
    args.directory.mkdir(mode=0o700, parents=True, exist_ok=args.resume_canary)
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
            ],
            stderr=subprocess.PIPE,
        )
    )
    admin = base64.b64decode(secret["data"]["token"]).decode().strip()
    with httpx.Client(
        base_url=ORIGIN, headers={"Origin": ORIGIN}, timeout=90, trust_env=False
    ) as client:
        ok(
            client.post(
                "/admin/api/v1/session", headers={"Authorization": "Bearer " + admin}
            )
        )
        tenant = CANARY if args.action == "canary" else "stockholm"
        if args.action == "canary":
            if args.resume_canary:
                issued = json.loads(
                    (args.directory / "canary-key.private.json").read_bytes()
                )
                owners = ok(
                    client.get("/admin/api/v1/users", params={"tenant_id": tenant})
                )["data"]["items"]
                assert len(owners) == 1 and owners[0]["principal_id"] == "check"
                user = owners[0]
                original = json.loads((args.directory / "admitted.json").read_bytes())
                old_headers = {"Authorization": "Bearer " + issued["secret"]}
                terminal = ok(
                    client.get("/v1/operations/" + original["id"], headers=old_headers)
                )
                assert terminal["status"] == "succeeded"
                old_result = ok(
                    client.get(
                        "/v1/operations/" + original["id"] + "/result",
                        headers=old_headers,
                    )
                )
                private(
                    args.directory / "first-attempt-reasoning-budget.json",
                    {
                        "operation": terminal,
                        "result": old_result,
                        "accepted_as_semantic_pass": False,
                        "reason": "32-token test budget produced no final content",
                    },
                )
            else:
                # Prevent automatic cloud bucket provisioning for this short-lived test.
                ok(
                    client.put(
                        f"/admin/api/v1/tenants/{tenant}/storage",
                        json={"mode": "disabled", "quota_bytes": 5000000000},
                    )
                )
                user = ok(
                    client.post(
                        "/admin/api/v1/users",
                        json={
                            "tenant_id": tenant,
                            "principal_id": "check",
                            "display_name": "Disposable retirement verification",
                            "kind": "service",
                        },
                    )
                )["data"]
                issued = ok(
                    client.post(
                        f"/admin/api/v1/users/{user['id']}/keys",
                        json={
                            "tenant_id": tenant,
                            "principal_id": "check",
                            "name": "retirement-check",
                            "models": ["qwen3-8b"],
                            "scopes": [
                                "catalog.read",
                                "inference.invoke",
                                "operations.read",
                                "operations.result",
                            ],
                            "max_concurrency": 1,
                            "request_budget": 2,
                            "expires_at": (
                                datetime.now(UTC) + timedelta(hours=1)
                            ).isoformat(),
                        },
                    )
                )["data"]
                private(args.directory / "canary-key.private.json", issued)
            headers = {"Authorization": "Bearer " + issued["secret"]}
            assert client.get("/v1/models", headers=headers).status_code == 200
            # One actual retained model operation; no new model configuration.
            suffix = "-r2" if args.resume_canary else ""
            response = client.post(
                "/v1/chat/completions",
                headers={
                    **headers,
                    "Idempotency-Key": "stockholm-closeout-qwen-20260920" + suffix,
                    "x-fs2-wait-seconds": "0",
                },
                json={
                    "model": "qwen3-8b",
                    "messages": [
                        {
                            "role": "user",
                            "content": "Reply with the word READY only. /no_think",
                        }
                    ],
                    "max_completion_tokens": 1024,
                    "temperature": 0,
                },
            )
            operation = ok(response)
            operation_id = operation["id"]
            private(args.directory / ("admitted" + suffix + ".json"), operation)
            deadline = time.monotonic() + 600
            while operation["status"] not in {
                "succeeded",
                "failed",
                "cancelled",
                "preempted",
                "expired",
            }:
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        "canary operation still active; no retirement performed"
                    )
                time.sleep(3)
                operation = ok(
                    client.get("/v1/operations/" + operation_id, headers=headers)
                )
            assert operation["status"] == "succeeded", (
                "canary inference failed; evidence retained"
            )
            result = ok(
                client.get(
                    "/v1/operations/" + operation_id + "/result", headers=headers
                )
            )
            private(
                args.directory / "result.json",
                {
                    "operation": operation,
                    "result": result,
                    "user": user,
                    "key_id": issued["key"]["id"],
                },
            )
            assert "READY" in (result["choices"][0]["message"]["content"] or "")
            archive_sha = hashlib.sha256(
                (args.directory / "result.json").read_bytes()
            ).hexdigest()
            expected_users = expected_keys = 1
        else:
            assert args.archive and (args.archive / "manifest.json").is_file()
            manifest = json.loads((args.archive / "manifest.json").read_bytes())
            # Verify all compressed evidence and copied files against the completed manifest.
            for name, expected in manifest["files_sha256"].items():
                path = (args.archive / name).resolve()
                assert args.archive.resolve() in path.parents
                with path.open("rb") as stream:
                    assert (
                        hashlib.file_digest(stream, "sha256").hexdigest() == expected
                    ), "archive checksum mismatch"
            expected_users = manifest["counts"]["fs2_inference_users"]
            expected_keys = manifest["counts"]["fs2_tokens"]
            assert (expected_users, expected_keys) == (20, 22), (
                "unexpected Stockholm identity scope"
            )
            archive_sha = hashlib.sha256(
                (args.archive / "manifest.json").read_bytes()
            ).hexdigest()
        before_users = ok(
            client.get(
                "/admin/api/v1/users", params={"tenant_id": tenant, "limit": 1000}
            )
        )["data"]
        before_keys = ok(
            client.get(
                "/admin/api/v1/keys", params={"tenant_id": tenant, "limit": 1000}
            )
        )["data"]
        private(
            args.directory / "before.json", {"users": before_users, "keys": before_keys}
        )
        retained_before = retained_identities(client, tenant)
        private(args.directory / "retained-identities-before.json", retained_before)
        result = ok(
            client.request(
                "DELETE",
                "/admin/api/v1/tenants/" + tenant,
                json={
                    "archive_sha256": archive_sha,
                    "expected_users": expected_users,
                    "expected_keys": expected_keys,
                },
            )
        )["data"]
        private(args.directory / "retirement.json", result)
        users = ok(
            client.get(
                "/admin/api/v1/users", params={"tenant_id": tenant, "limit": 1000}
            )
        )["data"]
        keys = ok(
            client.get(
                "/admin/api/v1/keys", params={"tenant_id": tenant, "limit": 1000}
            )
        )["data"]
        assert users["items"] == [] and keys["items"] == []
        retained_after = retained_identities(client, tenant)
        private(args.directory / "retained-identities-after.json", retained_after)
        receipt = {
            "tenant": tenant,
            "retirement": result,
            "users_visible": 0,
            "keys_visible": 0,
            "other_tenant_identity_policy_unchanged": retained_before == retained_after,
            "readyz_http": client.get("/readyz").status_code,
            "admin_http": client.get("/admin/").status_code,
        }
        if args.action == "canary":
            receipt["public_auth_after_retirement"] = client.get(
                "/v1/models", headers=headers
            ).status_code
            assert receipt["public_auth_after_retirement"] == 401
            receipt["operation_id"] = operation_id
        assert receipt["readyz_http"] == receipt["admin_http"] == 200
        private(args.directory / "verified.json", receipt)
        print(json.dumps(receipt))


if __name__ == "__main__":
    main()

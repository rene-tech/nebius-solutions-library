#!/usr/bin/env python3
"""Explicit provisioning and bounded public acceptance for shared customer models.

Run only after the release manager authorizes live execution. The retained Kopra
key is never revoked. Acceptance submits one original synthetic CPU prediction;
all remaining requests are discovery, replay, reads or expected access denials.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import stat
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, uuid5

import httpx
from mcp.shared.exceptions import MCPError

AGING = Path(__file__).resolve().parents[1] / "aging-20260908"
sys.path.insert(0, str(AGING))
import public_apps as shared  # noqa: E402

SCOPES = sorted(
    {
        "catalog.read",
        "inference.invoke",
        "mcp.invoke",
        "operations.read",
        "operations.result",
        "operations.cancel",
        "operations.acknowledge",
        "artifacts.write",
    }
)
KEY_NAME = "kopra-customer-default"
OWNER = {"tenant_id": "kopra", "principal_id": "kopra"}
USER_ID = str(shared.owner_id(**OWNER))
REQUIRED_MODELS = {"phenoage", "bindcraft", "alphafold3"}


def require(condition, code):
    shared.check(condition, code)


def comparison_tenant(value):
    require(
        isinstance(value, str) and bool(value.strip()) and value != OWNER["tenant_id"],
        "comparison_tenant_must_be_existing_and_not_kopra",
    )
    return value


def private_write(path, value):
    """Exclusive, durable, owner-only output; never replace an existing key."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_key(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600, "key_file_requires_mode_0600")
        value = json.load(stream)
    require(value.get("schema") == "fs2-customer-key/v1", "key_file_schema")
    require(value.get("owner") == OWNER and value.get("name") == KEY_NAME, "key_file_owner_or_name")
    require(isinstance(value.get("secret"), str), "key_file_missing_secret")
    parsed_id, _ = shared.TokenService._parse(value["secret"])
    require(str(parsed_id) == value.get("key_id"), "key_file_secret_identity")
    return value


@contextmanager
def key_lock(path):
    """Serialize provisioning on this host; API inventory handles earlier runs."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def key_request(name, owner, models, *, disposable=False):
    result = {"name": name, **owner, "models": models, "scopes": SCOPES, "max_concurrency": 1}
    if disposable:
        result["expires_at"] = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    return result


def validate_retained_metadata(key):
    require(all(key.get(field) == value for field, value in OWNER.items()), "retained_key_wrong_owner")
    require(key.get("name") == KEY_NAME and key.get("state") == "active", "retained_key_not_active")
    require(set(key.get("models", [])) == {"*"}, "retained_key_not_wildcard")
    require(set(key.get("scopes", [])) == set(SCOPES), "retained_key_scopes_differ")


def ensure_user(admin, trace, *, create):
    listing = shared.admin_call(admin, trace, "GET", "/admin/api/v1/users?tenant_id=kopra&limit=1000")
    require(not listing.get("truncated"), "user_inventory_truncated")
    matches = [row for row in listing["items"] if row["principal_id"] == OWNER["principal_id"]]
    require(len(matches) <= 1, "duplicate_kopra_owner")
    if matches:
        user = matches[0]
        require(user["id"] == USER_ID and user["tenant_id"] == "kopra", "kopra_owner_identity")
        require(user["enabled"], "kopra_owner_disabled")
        if user["display_name"] != "Kopra":
            require(create, "kopra_display_name_requires_provisioning")
            user = shared.admin_call(
                admin, trace, "PATCH", f"/admin/api/v1/users/{USER_ID}", payload={"display_name": "Kopra"}
            )
        return user
    require(create, "kopra_owner_requires_provisioning")
    return shared.admin_call(
        admin,
        trace,
        "POST",
        "/admin/api/v1/users",
        expected=(201,),
        payload={**OWNER, "display_name": "Kopra", "kind": "service"},
    )


def retained_key(admin, trace, path, origin, *, create):
    """Never silently duplicate, rotate, change or revoke a retained customer key."""
    with key_lock(path):
        saved = read_key(path) if path.exists() or path.is_symlink() else None
        keys = shared.admin_call(admin, trace, "GET", f"/admin/api/v1/users/{USER_ID}/keys")["items"]
        matches = [key for key in keys if key.get("name") == KEY_NAME]
        require(len(matches) <= 1, "multiple_named_kopra_keys_requires_operator")
        if matches:
            key = matches[0]
            validate_retained_metadata(key)
            require(saved is not None, "existing_kopra_key_secret_missing_no_duplicate_created")
            require(saved["key_id"] == key["id"] and saved["origin"] == origin, "saved_key_does_not_match_platform")
            return saved, False
        require(saved is None, "saved_key_missing_from_platform_no_replacement_created")
        require(create, "kopra_key_requires_provisioning")
        disclosure = shared.admin_call(
            admin,
            trace,
            "POST",
            f"/admin/api/v1/users/{USER_ID}/keys",
            expected=(201,),
            disclosure=True,
            payload=key_request(KEY_NAME, OWNER, ["*"]),
        )
        key = disclosure["key"]
        # Persist the one-time value before any further check/network request.
        saved = {
            "schema": "fs2-customer-key/v1",
            "origin": origin,
            "owner": OWNER,
            "name": KEY_NAME,
            "user_id": USER_ID,
            "key_id": key["id"],
            "secret": disclosure["secret"],
            "created_at": shared.now(),
        }
        private_write(path, saved)
        validate_retained_metadata(key)
        read_key(path)
        return saved, True


def public_client(origin, token):
    return httpx.Client(
        base_url=origin, timeout=60, trust_env=False, headers={"authorization": "Bearer " + token, "origin": origin}
    )


async def mcp_denied(origin, token, trace, name, arguments):
    """Require a real MCP tool denial, not a transport failure or empty result."""
    async with shared.httpx2.AsyncClient(
        timeout=60,
        trust_env=False,
        headers={"authorization": "Bearer " + token, "origin": origin},
    ) as transport:
        async with shared.Client(
            shared.streamable_http_client(origin + "/mcp", http_client=transport),
            mode=shared.MCP_PROTOCOL_VERSION,
        ) as client:
            started = shared.now()
            try:
                raw = await client.call_tool(name, arguments)
            except MCPError as error:
                trace.record(
                    "mcp-denied",
                    {
                        "started_at": started,
                        "completed_at": shared.now(),
                        "tool": name,
                        "rpc_error": error.error.model_dump(mode="json"),
                    },
                )
                require(
                    error.code == -32602
                    and error.message
                    in {
                        "model or protocol is outside token policy",
                        "operation not found",
                    },
                    "mcp_unexpected_rpc_error",
                )
                return
            trace.record(
                "mcp-denied",
                {
                    "started_at": started,
                    "completed_at": shared.now(),
                    "tool": name,
                    "response": raw.model_dump(mode="json", by_alias=True),
                },
            )
            require(raw.is_error is True, "mcp_expected_denial")
            messages = [getattr(item, "text", "") for item in raw.content]
            require(
                any(
                    message in {"model or protocol is outside token policy", "operation not found"}
                    for message in messages
                ),
                "mcp_unexpected_tool_error",
            )


def discover(public, origin, token, trace, *, expected=None):
    http_models, _ = shared.exchange(public, trace, "GET", "/v1/models")
    mcp_models = asyncio.run(shared.mcp_call(origin, token, trace, "list_models", {}))
    http_ids = {item["id"] for item in http_models["data"]}
    mcp_ids = {item["id"] for item in mcp_models["data"]}
    require(http_ids == mcp_ids, "serving_http_mcp_inventory_differs")
    scientific_http, _ = shared.exchange(public, trace, "GET", "/v1/scientific-models")
    scientific_mcp = asyncio.run(shared.mcp_call(origin, token, trace, "list_scientific_models", {}))
    scientific_http_ids = {item["model_id"] for item in scientific_http["data"]}
    scientific_mcp_ids = {item["model_id"] for item in scientific_mcp["data"]}
    require(scientific_http_ids == scientific_mcp_ids, "scientific_http_mcp_inventory_differs")
    # Protocol-specific discovery is not a separate customer access policy.
    # Keep public clone identities; do not collapse them to their model source.
    all_ids = http_ids | scientific_http_ids
    if expected is not None:
        require(all_ids == set(expected), "restricted_catalog_grant")
    return {
        "http": http_models,
        "mcp": mcp_models,
        "serving_model_ids": sorted(http_ids),
        "scientific_http": scientific_http,
        "scientific_mcp": scientific_mcp,
        "scientific_model_ids": sorted(scientific_http_ids),
        "model_ids": sorted(all_ids),
    }


def verify_platform_inventory(apps, model_ids):
    expected = {item["public_model_id"] for item in apps if item["enabled"]}
    require(REQUIRED_MODELS <= model_ids and expected <= model_ids, "available_platform_models_missing")
    return expected


def original_fixture():
    request = shared.clinical_payload(1)
    declaration = json.loads((shared.ROOT / "catalog/runtime/native/phenoage.json").read_bytes())
    require(
        shared.digest(request) == declaration["semantic_requests"]["requests"][0]["payload_sha256"],
        "original_fixture_changed",
    )
    return request


def infer_one(public, admin, trace, origin, saved, app_id, args, evidence):
    request = original_fixture()
    body = {"operation": "predict-age", "payload": request}
    idem = "customer-access-" + str(uuid5(NAMESPACE_URL, str(args.output.resolve())))
    started = time.monotonic()
    _, headers = shared.exchange(
        public,
        trace,
        "POST",
        "/v1/models/phenoage:invoke",
        payload=body,
        expected=(200, 202),
        headers={
            "Idempotency-Key": idem,
            "x-fs2-wait-seconds": "0",
            "x-fs2-deadline-seconds": str(args.timeout_seconds),
        },
    )
    operation_id = headers["x-fs2-operation-id"]
    evidence["operation_id"] = operation_id
    private_write(
        args.output / "accepted.json",
        {
            "operation_id": operation_id,
            "at": shared.now(),
            "idempotency_key": idem,
            "request_sha256": shared.digest(request),
        },
    )
    # Keep durable acceptance even if a replay or later response fails.
    _, replay = shared.exchange(
        public,
        trace,
        "POST",
        "/v1/models/phenoage:invoke",
        payload=body,
        expected=(200, 202),
        headers={"Idempotency-Key": idem, "x-fs2-wait-seconds": "0"},
    )
    require(
        replay["x-fs2-operation-id"] == operation_id and replay["x-fs2-idempotent-replay"] == "true", "replay_identity"
    )
    terminal, observations, transitions = shared.await_terminal(
        public,
        admin,
        trace,
        f"/admin/api/v1/apps/{app_id}",
        operation_id,
        args.timeout_seconds,
    )
    result, _ = shared.exchange(public, trace, "GET", f"/v1/operations/{operation_id}/result")
    validation = shared.validate_result("phenoage", 0, request, result)
    mcp_result = asyncio.run(
        shared.mcp_call(origin, saved["secret"], trace, "get_operation_result", {"operation_id": operation_id})
    )
    require(mcp_result["operation"]["id"] == operation_id and mcp_result["result"] == result, "http_mcp_result_parity")
    evidence["inference"] = {
        "terminal": terminal,
        "validation": validation,
        "result": result,
        "transitions": transitions,
        "app_observations": observations,
        "client_seconds": time.monotonic() - started,
        "accepted_to_terminal_seconds": (
            datetime.fromisoformat(terminal["completed_at"]) - datetime.fromisoformat(terminal["accepted_at"])
        ).total_seconds(),
    }
    return operation_id


def restricted_checks(admin, trace, origin, args, operation_id, evidence):
    name = "customer-access-disposable-" + str(uuid5(NAMESPACE_URL, str(args.output.resolve())))
    require(bool(args.comparison_principal.strip()), "comparison_principal_required")
    owner = {"tenant_id": comparison_tenant(args.comparison_tenant), "principal_id": args.comparison_principal}
    key_id = token = None
    try:
        disclosure = shared.admin_call(
            admin,
            trace,
            "POST",
            "/admin/api/v1/keys",
            expected=(201,),
            disclosure=True,
            payload=key_request(name, owner, ["qwen3-8b"], disposable=True),
        )
        key_id, token = disclosure["key"]["id"], disclosure["secret"]
        evidence["disposable_key_id"] = key_id
        with public_client(origin, token) as public:
            discover(public, origin, token, trace, expected={"qwen3-8b"})
            _, denied = shared.exchange(
                public,
                trace,
                "POST",
                "/v1/models/phenoage:invoke",
                payload={"operation": "predict-age", "payload": original_fixture()},
                expected=(403, 404),
                headers={"Idempotency-Key": name + "-denied", "x-fs2-wait-seconds": "0"},
            )
            require("x-fs2-operation-id" not in denied, "denied_request_created_operation")
            asyncio.run(
                mcp_denied(
                    origin,
                    token,
                    trace,
                    "invoke_model",
                    {
                        "model_id": "phenoage",
                        "protocol": "native",
                        "payload": original_fixture(),
                        "idempotency_key": name + "-denied-mcp",
                        "wait_seconds": 0,
                    },
                )
            )
            # Grant the same model before checking data isolation, so model
            # denial cannot masquerade as successful customer result isolation.
            shared.admin_call(admin, trace, "PATCH", f"/admin/api/v1/keys/{key_id}", payload={"models": ["phenoage"]})
            discover(public, origin, token, trace, expected={"phenoage"})
            for suffix in ("", "/result"):
                shared.exchange(public, trace, "GET", f"/v1/operations/{operation_id}{suffix}", expected=(403, 404))
            for tool in ("get_operation", "get_operation_result"):
                asyncio.run(mcp_denied(origin, token, trace, tool, {"operation_id": operation_id}))
        evidence["different_customer_same_model_result_isolation"] = True
    finally:
        if key_id is not None:
            key = shared.admin_call(admin, trace, "DELETE", f"/admin/api/v1/keys/{key_id}")
            require(key["state"] == "revoked", "disposable_key_revoke_unconfirmed")
            with public_client(origin, token) as revoked:
                shared.exchange(revoked, trace, "GET", "/v1/models", expected=(401,))
            evidence["disposable_key_revoked"] = True


def accept(admin, trace, origin, saved, args, evidence):
    apps = shared.admin_call(admin, trace, "GET", "/admin/api/v1/apps?limit=1000")
    require(not apps.get("next_cursor"), "apps_inventory_truncated")
    pheno = next(item for item in apps["items"] if item["public_model_id"] == "phenoage")
    with public_client(origin, saved["secret"]) as public:
        evidence["discovery"] = discover(public, origin, saved["secret"], trace)
        ids = set(evidence["discovery"]["model_ids"])
        expected = verify_platform_inventory(apps["items"], ids)
        # Inference credentials must not become a console session.
        shared.exchange(public, trace, "GET", "/admin/api/v1/context", expected=(401, 403))
        evidence["available_app_ids"] = sorted(expected)
        operation_id = infer_one(public, admin, trace, origin, saved, pheno["app_id"], args, evidence)
        restricted_checks(admin, trace, origin, args, operation_id, evidence)
    window = "?" + urlencode({"from": evidence["started_at"], "to": shared.now()})
    detail = shared.admin_call(admin, trace, "GET", f"/admin/api/v1/users/{USER_ID}" + window)
    require(
        detail["user"]["tenant_id"] == "kopra" and detail["user"]["principal_id"] == "kopra", "usage_owner_identity"
    )
    require(
        detail["user"]["usage"]["requests"] >= 1 and detail["user"]["usage"]["succeeded"] >= 1,
        "user_usage_not_attributed",
    )
    run = shared.admin_call(admin, trace, "GET", f"/admin/api/v1/apps/{pheno['app_id']}/runs/{operation_id}")
    require(
        run["operation"]["tenant_id"] == "kopra" and run["operation"]["principal_id"] == "kopra", "run_owner_identity"
    )
    require(run["operation"]["id"] == operation_id, "attributed_run_identity")
    evidence["user_usage"] = detail["user"]
    evidence["attributed_run"] = run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("provision", "accept"))
    parser.add_argument("--access-bundle", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--release", required=True)
    parser.add_argument("--comparison-tenant", help="Existing non-Kopra tenant; required for accept only")
    parser.add_argument(
        "--comparison-principal",
        default="terraform-bootstrap-client",
        help="Existing comparison owner; only the uniquely named disposable key is created/revoked",
    )
    parser.add_argument("--timeout-seconds", type=int, default=1020)
    args = parser.parse_args()
    logging.basicConfig(level=logging.CRITICAL)
    os.umask(0o077)
    require(60 <= args.timeout_seconds <= 3600, "bounded_timeout_required")
    if args.mode == "accept":
        comparison_tenant(args.comparison_tenant)
        require(bool(args.comparison_principal.strip()), "comparison_principal_required")
    require(not args.key_file.resolve().is_relative_to(shared.ROOT.parent), "key_file_must_be_outside_repository")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    access = json.loads(args.access_bundle.read_bytes())
    origin = access["endpoints"]["inference_base_url"].removesuffix("/v1")
    trace = shared.Trace(args.output)
    evidence = {
        "mode": args.mode,
        "release": args.release,
        "started_at": shared.now(),
        "outcome": "failed",
        "retained_key_revoked": False,
        "model_settings_writes": 0,
        "cluster_writes": 0,
    }
    logged_in = False
    with httpx.Client(base_url=origin, timeout=60, trust_env=False, headers={"origin": origin}) as admin:
        try:
            shared.exchange(
                admin,
                trace,
                "POST",
                "/admin/api/v1/session",
                headers={
                    "authorization": "Bearer " + access["credentials"]["admin_bootstrap_token"],
                },
            )
            logged_in = True
            ensure_user(admin, trace, create=args.mode == "provision")
            saved, created = retained_key(admin, trace, args.key_file, origin, create=args.mode == "provision")
            evidence.update(user_id=USER_ID, retained_key_id=saved["key_id"], retained_key_created=created)
            if args.mode == "accept":
                accept(admin, trace, origin, saved, args, evidence)
            evidence["outcome"] = "passed"
        except Exception as error:
            evidence["error_type"] = type(error).__name__
            if isinstance(error, AssertionError):
                evidence["error_code"] = str(error)
        finally:
            if logged_in:
                try:
                    shared.exchange(admin, trace, "DELETE", "/admin/api/v1/session", expected=(204,))
                    evidence["admin_logged_out"] = True
                except Exception as error:
                    evidence.update(outcome="failed", logout_error=type(error).__name__)
            evidence["completed_at"] = shared.now()
            private_write(args.output / "outcome.json", evidence)
    # Only a credential-free status is printed, never HTTP payloads or secrets.
    print(json.dumps({key: evidence[key] for key in ("mode", "outcome", "release", "completed_at")}))
    return 0 if evidence["outcome"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

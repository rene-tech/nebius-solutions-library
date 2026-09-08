#!/usr/bin/env python3
"""Bounded ordinary-key academic acceptance; prepare offline unless --execute.

Reuse the accepted model fixtures and scientific-fleet client unchanged. The
retained Kopra key owns every upload/submission/read. Admin authentication is
used only for attribution reads. Another existing customer key performs reads
only, proving same-model access does not grant Kopra's operation or artifacts.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "acceptance/scientific-fleet"))
import customer_access as customer  # noqa: E402
import run_acceptance as public  # noqa: E402
import run_fleet_acceptance as fleet  # noqa: E402
import run_scenario_acceptance as scenarios  # noqa: E402

MODELS = ("bindcraft", "alphafold3")
BASE_CLIENT = public.PublicApiClient
now = customer.shared.now


def require(condition, code):
    if not condition:
        raise public.AcceptanceError(code)


def write(path, value):
    public._assert_redacted(value)
    public._write_receipt(path, value, overwrite=False)


def prepare(run_id, endpoint):
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", run_id), "run_id_invalid")
    inputs = {item.model_id: item for item in fleet.discover_inputs(ROOT)}
    rows = []
    for model in MODELS:
        fixture = inputs[model]
        config = public.RunConfig(endpoint, ROOT, fixture.path, Path("unused"), f"{run_id}.{model}")
        model_id, request, declarations, _ = scenarios.prepare_scenario(config, {"id": model, "model_id": model})
        require(
            model_id == model and request["service_class"] == "customer-batch",
            "fixture_changed",
        )
        # Validate the complete immutable declaration chain without uploading.
        manifest = next(item for item in declarations if item.role == "request-input-manifest")
        public._verify_declared_bytes(request["input_manifest"], manifest.data)
        public._entry_inputs(
            json.loads(manifest.data),
            [item for item in declarations if item.role == "manifest-artifact"],
        )
        rows.append(
            {
                "model_id": model,
                "fixture": fixture.relative_path,
                "fixture_sha256": fixture.sha256,
                "request_sha256": hashlib.sha256(public._canonical_json(request)).hexdigest(),
                "service_class": request["service_class"],
                "inputs": [
                    {
                        "path": str(item.path.relative_to(ROOT)),
                        "role": item.role,
                        "sha256": hashlib.sha256(item.data).hexdigest(),
                        "size_bytes": len(item.data),
                    }
                    for item in declarations
                ],
            }
        )
    return {
        "schema": "fs2-customer-scientific-plan/v1",
        "run_id": run_id,
        "models": rows,
        "operation_count": 2,
        "parallel_clients": 1,
        "fixture_changes": False,
        "model_settings_writes": 0,
        "cluster_writes": 0,
        "scope": "Existing technical acceptance fixtures only; no new biological optimization or clinical claim.",
    }


def trace_client(path):
    class TracingClient(BASE_CLIENT):
        def request(self, method, route, **kwargs):
            started, clock = now(), time.monotonic()
            event = {"started_at": started, "method": method, "path": route}
            try:
                response = super().request(method, route, **kwargs)
                event.update(
                    status=response.status,
                    response_bytes=len(response.body),
                    response_sha256=hashlib.sha256(response.body).hexdigest(),
                    content_type=response.headers.get("content-type"),
                )
                if route.endswith(":submit") or route.startswith("/v1/operations/"):
                    try:
                        document = json.loads(response.body)
                        operation = document.get("operation", {})
                        event["operation"] = {key: operation.get(key) for key in ("id", "model_id", "status", "reused")}
                    except (ValueError, AttributeError):
                        event["invalid_json"] = True
                return response
            except Exception as error:
                event["error_type"] = type(error).__name__
                raise
            finally:
                event.update(completed_at=now(), duration_seconds=time.monotonic() - clock)
                public._assert_redacted(event)
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event, sort_keys=True) + "\n")

    return TracingClient


def discover(client):
    response = public._json_response(client.request("GET", "/v1/scientific-models"), 200, "scientific_discovery")
    ids = {item["model_id"] for item in response["data"]}
    require(set(MODELS) <= ids, "academic_models_not_discoverable")
    return sorted(ids)


def assert_owner(run, model, operation_id, metadata):
    operation = run["operation"]
    require(
        operation["id"] == operation_id and operation["model_id"] == model,
        "admin_operation_identity",
    )
    require(
        operation["tenant_id"] == customer.OWNER["tenant_id"]
        and operation["principal_id"] == customer.OWNER["principal_id"],
        "admin_operation_owner",
    )
    require(
        operation["api_key_prefix"] == metadata["prefix"],
        "admin_operation_access_identity",
    )
    require(operation["status"] == "succeeded", "admin_operation_not_succeeded")
    return {
        "operation_id": operation_id,
        "model_id": model,
        **customer.OWNER,
        "access_id": metadata["id"],
        "matched_prefix": True,
        "status": operation["status"],
    }


def isolation(client, row):
    # Same-model discovery is required before these denials count as isolation.
    discover(client)
    paths = [
        f"/v1/operations/{row['operation_id']}",
        f"/v1/operations/{row['operation_id']}/result",
    ]
    paths.extend(f"/v1/artifacts/{item['artifact_id']}/content" for item in row["downloaded_artifacts"])
    checks = []
    for path in paths:
        response = client.request("GET", path)
        require(response.status in {403, 404}, "cross_customer_data_not_denied")
        checks.append({"path": path, "status": response.status})
    return checks


def execute(args, plan):
    require(
        re.fullmatch(r"[0-9a-f]{40}", args.deployed_source or ""),
        "exact_deployed_source_required",
    )
    require(
        args.key_file is not None and args.access_bundle is not None,
        "private_access_paths_required",
    )
    require(
        not args.output.resolve().is_relative_to(ROOT.parent),
        "private_output_must_be_outside_repository",
    )
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    write(args.output / "plan.json", plan)
    evidence = {
        "schema": "fs2-customer-scientific-acceptance/v1",
        "outcome": "failed",
        "deployed_source": args.deployed_source,
        "started_at": now(),
        "rows": [],
        "model_settings_writes": 0,
        "cluster_writes": 0,
        "retained_access_unchanged": True,
        "source_scope": "Release identity supplied by the release manager after its rollout gate.",
        "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "delegated_client_sha256": hashlib.sha256(Path(scenarios.__file__).read_bytes()).hexdigest(),
    }
    trace = customer.shared.Trace(args.output)
    signed_in = False
    try:
        saved = customer.read_key(args.key_file)
        require(saved["origin"] == args.endpoint, "retained_access_origin_mismatch")
        access = json.loads(args.access_bundle.read_bytes())
        require(
            access["endpoints"]["inference_base_url"].removesuffix("/v1") == args.endpoint,
            "admin_origin_mismatch",
        )
        other = access["credentials"]["inference_access_token"]
        require(other != saved["secret"], "isolation_requires_distinct_customer_access")
        with httpx.Client(
            base_url=args.endpoint,
            timeout=60,
            trust_env=False,
            headers={"origin": args.endpoint},
        ) as admin:
            try:
                customer.shared.exchange(
                    admin,
                    trace,
                    "POST",
                    "/admin/api/v1/session",
                    headers={"authorization": "Bearer " + access["credentials"]["admin_bootstrap_token"]},
                )
                signed_in = True
                keys = customer.shared.admin_call(admin, trace, "GET", f"/admin/api/v1/users/{customer.USER_ID}/keys")[
                    "items"
                ]
                metadata = next(item for item in keys if item["id"] == saved["key_id"])
                customer.validate_retained_metadata(metadata)
                apps = customer.shared.admin_call(admin, trace, "GET", "/admin/api/v1/apps?limit=1000")
                require(not apps.get("next_cursor"), "app_inventory_truncated")
                app_ids = {item["public_model_id"]: item["app_id"] for item in apps["items"]}
                customer_client = trace_client(args.output / "owner-reads.jsonl")(args.endpoint, saved["secret"])
                other_client = trace_client(args.output / "other-customer-reads.jsonl")(args.endpoint, other)
                evidence["ordinary_scopes"] = metadata["scopes"]
                evidence["owner"] = {**customer.OWNER, "access_id": saved["key_id"]}
                evidence["discovered_models"] = discover(customer_client)
                evidence["other_customer_discovered_models"] = discover(other_client)
                for selected in plan["models"]:
                    model = selected["model_id"]
                    config = public.RunConfig(
                        args.endpoint,
                        ROOT,
                        ROOT / selected["fixture"],
                        args.output / f"{model}.json",
                        f"{args.run_id}.{model}",
                        timeout_seconds=args.timeout_seconds,
                    )
                    public.PublicApiClient = trace_client(args.output / f"{model}.http.jsonl")
                    try:
                        row = scenarios.run_scenario(config, {"id": model, "model_id": model}, saved["secret"])
                    finally:
                        public.PublicApiClient = BASE_CLIENT
                    evidence["rows"].append(row)
                    write(args.output / f"{model}.summary.json", row)
                    require(row["outcome"] == "passed", "scientific_case_failed")
                    receipt = json.loads(config.receipt_path.read_bytes())
                    attempts = [item for stage in receipt["queue"]["observed_stages"] for item in stage["attempts"]]
                    require(
                        attempts and all(item["resource_released"] for item in attempts),
                        "resources_not_released",
                    )
                    operation_id = row["operation_id"]
                    result = public._json_response(
                        customer_client.request("GET", f"/v1/operations/{operation_id}/result"),
                        200,
                        "result_parity",
                    )
                    mcp = asyncio.run(
                        customer.shared.mcp_call(
                            args.endpoint,
                            saved["secret"],
                            trace,
                            "get_scientific_result",
                            {"operation_id": operation_id},
                        )
                    )
                    require(mcp == result, "scientific_http_mcp_result_mismatch")
                    run = customer.shared.admin_call(
                        admin,
                        trace,
                        "GET",
                        f"/admin/api/v1/apps/{app_ids[model]}/runs/{operation_id}",
                    )
                    row["owner_attribution"] = assert_owner(run, model, operation_id, metadata)
                    row["other_customer_denials"] = isolation(other_client, row)
                    row["mcp_result_parity"] = True
                    row["resources_released"] = True
                    write(args.output / f"{model}.verified.json", row)
                    print(
                        json.dumps(
                            {
                                "model": model,
                                "operation_id": operation_id,
                                "outcome": "passed",
                            }
                        ),
                        flush=True,
                    )
                query = urlencode({"from": evidence["started_at"], "to": now()})
                user = customer.shared.admin_call(
                    admin,
                    trace,
                    "GET",
                    f"/admin/api/v1/users/{customer.USER_ID}?{query}",
                )["user"]
                require(
                    all(user[field] == value for field, value in customer.OWNER.items()),
                    "usage_owner_mismatch",
                )
                require(
                    user["usage"]["scientific_requests"] == 2,
                    "logical_scientific_count_mismatch",
                )
                require(user["usage"]["succeeded"] >= 2, "successful_usage_not_attributed")
                # The private admin trace retains the full DTO. The compact
                # receipt deliberately selects counts, not unrelated metering.
                evidence["owner_usage"] = {
                    field: user["usage"][field]
                    for field in (
                        "requests",
                        "scientific_requests",
                        "succeeded",
                        "failed",
                        "cancelled",
                        "pending",
                        "running",
                    )
                }
                evidence["outcome"] = "passed"
            finally:
                if signed_in:
                    customer.shared.exchange(admin, trace, "DELETE", "/admin/api/v1/session", expected=(204,))
                    evidence["admin_logged_out"] = True
    except Exception as error:
        evidence["outcome"] = "failed"
        evidence["error_type"] = type(error).__name__
        if isinstance(error, public.AcceptanceError):
            evidence["error_code"] = error.code
    finally:
        evidence["completed_at"] = now()
        write(args.output / "outcome.json", evidence)
    print(json.dumps({key: evidence[key] for key in ("outcome", "completed_at", "deployed_source")}))
    return 0 if evidence["outcome"] == "passed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Requires the release manager's explicit live GO.",
    )
    parser.add_argument("--endpoint", default="https://89.169.99.188")
    parser.add_argument("--run-id", default="customer-access-scientific-r01")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--access-bundle", type=Path)
    parser.add_argument("--deployed-source")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args(argv)
    require(60 <= args.timeout_seconds <= 3600, "timeout_outside_bound")
    logging.basicConfig(level=logging.CRITICAL)
    os.umask(0o077)
    plan = prepare(args.run_id, args.endpoint)
    if not args.execute:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    require(args.output is not None, "fresh_private_output_required")
    return execute(args, plan)


if __name__ == "__main__":
    raise SystemExit(main())

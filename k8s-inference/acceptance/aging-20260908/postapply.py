#!/usr/bin/env python3
"""Read-only native qualification rollout check; execute only after release GO."""

import argparse
import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from public_apps import MODELS, ROOT, Trace, admin_call, check, digest, exchange, mcp_call, now, wait_zero, write

APP_FIELDS = ("app_id", "app_revision", "display_name", "academic_required", "execution_mode")
REVISION_FIELDS = (
    "namespace",
    "name",
    "tenant_id",
    "revision",
    "etag",
    "spec",
    "action",
    "created_at",
    "created_by",
    "previous_revision",
)
PROMOTED = ("route_active", "http_mcp_qualified", "cold_start_qualified", "elasticity_qualified")


def saved_settings(model, before, after):
    for field in APP_FIELDS:
        check(after[field] == before[field], "saved_app_changed_" + field)
    check(after["serving"] is not None, "managed_serving_revision_missing")
    for field in REVISION_FIELDS:
        check(after["serving"][field] == before["serving"][field], "saved_deployment_changed_" + field)
    spec = after["serving"]["spec"]
    availability = spec["availability"]
    check(spec["modelRef"] == model, "saved_canonical_model_changed")
    check(availability["minReplicas"] == 0 and availability["maxReplicas"] == 1, "saved_replica_limits_changed")
    check(availability["idleSeconds"] == availability["cooldownSeconds"] == 300, "saved_idle_timers_changed")
    return {
        "app_id": after["app_id"],
        "app_revision": after["app_revision"],
        "deployment_name": after["serving"]["name"],
        "deployment_revision": after["serving"]["revision"],
        "etag": after["serving"]["etag"],
        "saved_spec_sha256": digest(spec),
        "saved_policy_identity_and_revisions_unchanged": True,
        "availability": availability,
        "placement": spec["placement"],
        "runtime": spec["runtime"],
    }


def qualified_metadata(model, http_model, mcp_model, option, settings):
    variant_id = json.loads((ROOT / "catalog/runtime/native" / f"{model}.json").read_bytes())["variant_id"]
    for surface in (http_model, mcp_model):
        check(surface["id"] == model and surface["enabled"], "native_discovery_identity")
        check("native" in surface["capabilities"], "native_capability_missing")
        check(surface["active_runtime"]["variant_id"] == variant_id, "native_variant_changed")
        check(surface["revision"] == "dynamic:" + settings["serving"]["etag"], "discovery_revision_changed")
        check(
            surface["qualification"]["authority"] == "explicit-deployment-runtime-record",
            "qualification_authority_changed",
        )
        states = surface["qualification"]["states"]
        check(all(states.get(flag) is True for flag in PROMOTED), "public_qualification_not_promoted")
        check(
            all(states.get(flag) is True for flag in ("registered", "runtime_ready", "semantic_qualified")),
            "prior_qualification_regressed",
        )
    check(option["model_ref"] == model, "configuration_model_changed")
    check(option["scale_to_zero_qualified"] is True, "configuration_scale_to_zero_unqualified")
    check(option["scale_to_zero_warning"] is None, "configuration_stale_scale_warning")
    runtime = settings["serving"]["spec"]["runtime"]
    check(option["default_spec"]["runtime"]["image"] == runtime["image"], "configuration_image_changed")
    check(option["default_spec"]["runtime"]["profile"] == runtime["profile"], "configuration_profile_changed")
    return {
        "http_discovery": http_model,
        "mcp_discovery": mcp_model,
        "configuration": {
            key: option[key]
            for key in (
                "model_ref",
                "scale_to_zero_qualified",
                "scale_to_zero_warning",
                "gpu_snapshot_choices",
                "fast_start_qualified_level",
            )
        },
        "qualified_variant_id": variant_id,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-bundle", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--public-receipt", type=Path, required=True)
    parser.add_argument("--release", required=True)
    args = parser.parse_args()
    check(not args.public_receipt.exists(), "public_receipt_must_be_fresh")
    logging.basicConfig(level=logging.CRITICAL)
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    trace = Trace(args.output)
    baseline = {model: json.loads((args.baseline / model / "outcome.json").read_bytes()) for model in MODELS}
    check(
        all(item["outcome"] == "passed" and item["test_key_revoked"] for item in baseline.values()),
        "baseline_not_qualified",
    )
    access = json.loads(args.access_bundle.read_bytes())
    origin = access["endpoints"]["inference_base_url"].removesuffix("/v1")
    result = {
        "schema": "fs2-aging-postqualification-readonly/v1",
        "release": args.release,
        "started_at": now(),
        "outcome": "failed",
        "models": {},
        "inference_submissions": 0,
        "settings_writes": 0,
        "keys_created": 0,
    }
    with (
        httpx.Client(base_url=origin, timeout=60, trust_env=False, headers={"origin": origin}) as admin,
        httpx.Client(
            base_url=origin,
            timeout=60,
            trust_env=False,
            headers={"origin": origin, "authorization": "Bearer " + access["credentials"]["inference_access_token"]},
        ) as public,
    ):
        try:
            exchange(
                admin,
                trace,
                "POST",
                "/admin/api/v1/session",
                headers={"authorization": "Bearer " + access["credentials"]["admin_bootstrap_token"]},
            )
            current = {}
            for model in MODELS:
                before = baseline[model]["final_settings"]
                after = admin_call(admin, trace, "GET", f"/admin/api/v1/apps/{before['app_id']}/settings")
                current[model] = after
                result["models"][model] = saved_settings(model, before, after)
            options = admin_call(admin, trace, "GET", "/admin/api/v1/model-deployments:capabilities")[
                "configuration_options"
            ]
            http_models, _ = exchange(public, trace, "GET", "/v1/models")
            mcp_models = asyncio.run(
                mcp_call(origin, access["credentials"]["inference_access_token"], trace, "list_models", {})
            )
            for model in MODELS:
                selected = [option for option in options if option["model_ref"] == model]
                check(len(selected) == 1, "native_configuration_ambiguous")
                http_selected = [item for item in http_models["data"] if item["id"] == model]
                mcp_selected = [item for item in mcp_models["data"] if item["id"] == model]
                check(len(http_selected) == len(mcp_selected) == 1, "native_discovery_missing_or_ambiguous")
                result["models"][model].update(
                    qualified_metadata(model, http_selected[0], mcp_selected[0], selected[0], current[model])
                )
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = {
                    model: workers.submit(
                        wait_zero,
                        admin,
                        trace,
                        f"/admin/api/v1/apps/{current[model]['app_id']}",
                        time.monotonic() + 90,
                        "postapply",
                    )
                    for model in MODELS
                }
                for model, future in futures.items():
                    observed = future.result()
                    result["models"][model]["cold_zero_confirmed_twice"] = {
                        "at": observed["at"],
                        "status": observed["summary"]["status"],
                        "containers": observed["containers"]["total"],
                    }
            result["outcome"] = "passed"
        except Exception as error:
            result.update(
                error_type=type(error).__name__, error_code=str(error) if isinstance(error, AssertionError) else None
            )
        finally:
            try:
                exchange(admin, trace, "DELETE", "/admin/api/v1/session", expected=(204,))
                result["admin_session_logged_out"] = True
            except Exception as error:
                result.update(outcome="failed", logout_error=type(error).__name__)
            result["completed_at"] = now()
            write(args.output / "outcome.json", result)
            write(args.public_receipt, result)
    print(
        json.dumps(
            {
                key: result.get(key)
                for key in (
                    "outcome",
                    "release",
                    "started_at",
                    "completed_at",
                    "error_code",
                    "admin_session_logged_out",
                )
            }
        )
    )
    return 0 if result["outcome"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

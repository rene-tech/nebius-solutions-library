#!/usr/bin/env python3
"""Check the complete fleet through public discovery and the admin API.

This read-only check is not an inference or GPU-restore qualification. It reads
the private Terraform access bundle locally and never includes credentials in
the report. The fixed expected inventory prevents a disabled model from
silently disappearing from acceptance.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    expected = json.loads(Path(__file__).with_name("expected-models.json").read_bytes())
    access = json.loads(args.credential_bundle.read_bytes())
    origin = access["endpoints"]["inference_base_url"].removesuffix("/v1")
    checks: dict[str, bool] = {}
    with httpx.Client(base_url=origin, timeout=60) as client:
        discovered = {}
        for name, path, token_key, expected_key in (
            ("serving", "/v1/models", "inference_access_token", "serving_model_ids"),
            ("scientific", "/v1/scientific-models", "scientific_access_token", "scientific_model_ids"),
        ):
            response = client.get(path, headers={
                "authorization": "Bearer " + access["credentials"][token_key],
            })
            response.raise_for_status()
            discovered[name] = sorted(row.get("id", row.get("model_id")) for row in response.json()["data"])
            checks[name + "_discovery_complete"] = set(expected[expected_key]) <= set(discovered[name])
        client.headers["origin"] = origin
        response = client.post("/admin/api/v1/session", headers={
            "authorization": "Bearer " + access["credentials"]["admin_bootstrap_token"],
        })
        response.raise_for_status()
        try:
            response = client.get("/admin/api/v1/model-inventory")
            response.raise_for_status()
            inventory = response.json()["data"]
        finally:
            client.delete("/admin/api/v1/session").raise_for_status()
    by_id = {row["model_id"]: row for row in inventory["items"]}
    required = set(expected["serving_model_ids"] + expected["scientific_model_ids"])
    checks["admin_inventory_complete"] = required <= set(by_id)
    checks["all_required_models_configured"] = all(by_id.get(model, {}).get("configured") for model in required)
    checks["scientific_projection_available"] = inventory["scientific_projection_available"]
    selected_fields = (
        "model_id", "availability", "configured", "serving_state", "batch_readiness",
        "ready_replicas", "desired_replicas", "runtime_image_digest", "management_path",
        "gpu_snapshot", "snapshot_evidence_scope", "snapshot_selectable", "snapshot_bundle_ids",
        "snapshot_normal_startup", "snapshot_restore_startup", "snapshot_reason",
    )
    report = {
        "schema": "fs2-serve.nebius.ai/public-fleet-inventory-check/v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "origin": origin,
        "checks": checks,
        "passed": all(checks.values()),
        "discovery": discovered,
        "required_model_count": len(required),
        "inventory": [{key: row.get(key) for key in selected_fields} for row in inventory["items"]],
        "scope": "Discovery/configuration only; readiness, inference and actual restore need separate receipts.",
    }
    os.umask(0o077)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"passed": report["passed"], "checks": checks, "receipt": str(args.output)}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

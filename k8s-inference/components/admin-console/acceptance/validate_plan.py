#!/usr/bin/env python3
"""Fail-closed validation for the FS2 admin-console design contract."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ROUTE_IDS = {
    "overview",
    "models",
    "model-detail",
    "model-deployments",
    "model-deployment-new",
    "model-deployment-detail",
    "operations",
    "operation-detail",
    "access",
    "capacity",
    "observability",
    "configuration",
    "audit",
}
EXPECTED_STATUS_VALUES = {
    "hot",
    "loading",
    "queued",
    "cold",
    "unhealthy",
    "unsupported",
    "unknown",
}
REQUIRED_FIELDS = {
    "model.hotness",
    "request.throughput",
    "token.throughput",
    "latency.ttft",
    "request.errors",
    "operations.history",
    "principal.inventory",
    "api_keys",
    "queue.kueue",
    "capacity.gpu_inventory",
    "capacity.gpu_utilization",
    "audit",
    "logs",
    "traces",
    "alerts",
}
PROTECTED_IDENTIFIERS = {
    "private-cluster-example",
    "private-node-pool-example",
}


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def derive_status(observation: dict[str, Any]) -> str:
    """Reference implementation for the documented status precedence."""

    supported = observation.get("catalog_supported")
    if supported is False:
        return "unsupported"
    if supported is not True or observation.get("sources_fresh") is not True:
        return "unknown"
    if observation.get("health_failure") is True:
        return "unhealthy"

    phase = observation.get("activation_phase")
    desired = observation.get("desired_replicas")
    ready = observation.get("ready_replicas")
    queued = observation.get("queued_operations")
    if not all(
        isinstance(value, int) and value >= 0 for value in (desired, ready, queued)
    ):
        return "unknown"

    if phase in {"claimed", "starting", "restoring"} or (
        desired > 0 and ready < desired
    ):
        return "loading"
    if desired > 0 and ready > 0:
        return "hot"
    if phase == "queued" or queued > 0:
        return "queued"
    if phase == "none" and desired == 0 and ready == 0:
        return "cold"
    return "unknown"


def _duplicates(values: list[str]) -> set[str]:
    return {value for value in values if values.count(value) > 1}


def _public_ipv4_strings(document: str) -> list[str]:
    matches: list[str] = []
    for candidate in re.findall(
        r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", document
    ):
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.is_global:
            matches.append(candidate)
    return matches


def validate_contracts(
    plan: dict[str, Any], inventory: dict[str, Any], fixtures: list[dict[str, Any]]
) -> list[str]:
    errors: list[str] = []

    if plan.get("schema_version") != "fs2.admin-console.plan/v1":
        errors.append("unexpected plan schema_version")
    if inventory.get("schema_version") != "fs2.admin-console.live-inventory/v1":
        errors.append("unexpected inventory schema_version")
    if plan.get("api_prefix") != "/admin/api/v1":
        errors.append("admin BFF must use /admin/api/v1")

    routes = plan.get("routes", [])
    route_ids = [route.get("id") for route in routes]
    route_paths = [route.get("path") for route in routes]
    if set(route_ids) != EXPECTED_ROUTE_IDS:
        errors.append("route set does not cover the required information architecture")
    if duplicate_ids := _duplicates(route_ids):
        errors.append(f"duplicate route ids: {sorted(duplicate_ids)}")
    if duplicate_paths := _duplicates(route_paths):
        errors.append(f"duplicate route paths: {sorted(duplicate_paths)}")
    for route in routes:
        if not isinstance(route.get("path"), str) or not route["path"].startswith(
            "/admin"
        ):
            errors.append(f"route outside /admin: {route.get('id')}")
        if not route.get("components"):
            errors.append(f"route has no component plan: {route.get('id')}")
        for endpoint in route.get("bff", []):
            if not endpoint.startswith("/admin/api/v1/"):
                errors.append(
                    f"route bypasses versioned BFF: {route.get('id')} -> {endpoint}"
                )

    boundary = plan.get("browser_boundary", {})
    if boundary.get("same_origin_bff_only") is not True:
        errors.append("browser is not constrained to the same-origin BFF")
    for credential_flag in (
        "browser_cluster_credentials",
        "browser_database_credentials",
        "browser_raw_api_keys",
    ):
        if boundary.get(credential_flag) is not False:
            errors.append(f"unsafe browser credential flag: {credential_flag}")
    if boundary.get("one_time_secret_response_cache_policy") != "no-store":
        errors.append("one-time secret responses must use no-store")

    source_rows = plan.get("data_sources", [])
    source_ids = [source.get("id") for source in source_rows]
    if _duplicates(source_ids):
        errors.append("data source ids must be unique")
    if any(source.get("browser_direct") is not False for source in source_rows):
        errors.append("all infrastructure data sources must be server-side")
    known_sources = set(source_ids)

    field_rows = plan.get("field_matrix", [])
    field_ids = {row.get("id") for row in field_rows}
    if not REQUIRED_FIELDS.issubset(field_ids):
        errors.append(
            f"required field rows missing: {sorted(REQUIRED_FIELDS - field_ids)}"
        )
    for row in field_rows:
        unknown_sources = set(row.get("sources", [])) - known_sources
        if unknown_sources:
            errors.append(
                f"field {row.get('id')} references unknown sources: {sorted(unknown_sources)}"
            )
        if not row.get("availability") or not row.get("semantics"):
            errors.append(f"field {row.get('id')} lacks availability/semantics")

    status_contract = plan.get("status_contract", {})
    if set(status_contract.get("values", [])) != EXPECTED_STATUS_VALUES:
        errors.append("status values are incomplete")
    if status_contract.get("precedence") != [
        "unsupported",
        "unknown",
        "unhealthy",
        "loading",
        "hot",
        "queued",
        "cold",
    ]:
        errors.append("status precedence changed without fixture review")
    if status_contract.get("unknown_is_not_zero") is not True:
        errors.append("unknown must not be rendered as zero")
    for fixture in fixtures:
        actual = derive_status(fixture.get("input", {}))
        if actual != fixture.get("expected"):
            errors.append(
                f"status fixture {fixture.get('name')!r}: expected {fixture.get('expected')}, got {actual}"
            )
    covered_statuses = {fixture.get("expected") for fixture in fixtures}
    if covered_statuses != EXPECTED_STATUS_VALUES:
        errors.append("status fixtures do not cover all status values")

    inventory_components = {
        row["id"]: row for row in inventory.get("observability", [])
    }
    launches = {row["id"]: row for row in plan.get("observability_launches", [])}
    for component_id in ("alertmanager", "tempo"):
        if inventory_components.get(component_id, {}).get("state") != "absent":
            errors.append(f"inventory no longer proves {component_id} absent")
        if launches.get(component_id, {}).get("enabled") is not False:
            errors.append(f"absent component is launchable: {component_id}")
    for component_id in ("dcgm", "kueue", "opentelemetry"):
        if launches.get(component_id, {}).get("enabled") is not False:
            errors.append(
                f"component without verified UI/data path is launchable: {component_id}"
            )
        if not launches.get(component_id, {}).get("reason"):
            errors.append(f"disabled component lacks reason: {component_id}")
    for component_id in ("grafana", "prometheus", "loki"):
        if inventory_components.get(component_id, {}).get("state") != "healthy":
            errors.append(f"launchable component is not healthy: {component_id}")
        if launches.get(component_id, {}).get("enabled") is not True:
            errors.append(f"verified UI is not launchable: {component_id}")

    vertical_slice = plan.get("vertical_slice", {})
    if vertical_slice.get("read_only") is not True:
        errors.append("first vertical slice must remain read-only")
    if set(vertical_slice.get("route_ids", [])) != {
        "overview",
        "models",
        "model-detail",
    }:
        errors.append("first vertical slice scope changed")
    for endpoint in vertical_slice.get("bff_endpoints", []):
        method, separator, path = endpoint.partition(" ")
        if method != "GET" or not separator or not path.startswith("/admin/api/v1/"):
            errors.append(
                f"vertical-slice endpoint is not a read-only BFF GET: {endpoint}"
            )

    component_contract = json.dumps(
        {
            "routes": routes,
            "vertical_slice": vertical_slice,
            "context": plan.get("context_contract"),
        },
        sort_keys=True,
    )
    for hard_coded_gpu in ("B300", "H100", "H200", "B200", "GB300", "RTX6000"):
        if hard_coded_gpu in component_contract:
            errors.append(
                f"GPU family hard-coded into component contract: {hard_coded_gpu}"
            )

    serialized = json.dumps({"plan": plan, "inventory": inventory}, sort_keys=True)
    for protected in PROTECTED_IDENTIFIERS:
        if protected in serialized:
            errors.append(f"protected resource identifier recorded: {protected}")
    if public_ips := _public_ipv4_strings(serialized):
        errors.append(
            f"public IPv4 address recorded in contract: {sorted(set(public_ips))}"
        )
    if re.search(
        r"(?i)(authorization:\s*bearer|-----BEGIN [A-Z ]*PRIVATE KEY-----)", serialized
    ):
        errors.append("credential material pattern recorded in contract")

    scope = inventory.get("scope", {})
    if scope.get("kube_context") != "public-export-fixture":
        errors.append("inventory was not constrained to the allowed context")
    if scope.get("access") != "read-only" or scope.get("cluster_mutations") != 0:
        errors.append("inventory is not read-only")
    for false_receipt in (
        "protected_cluster_queried",
        "secrets_queried",
        "public_endpoint_recorded",
    ):
        if scope.get(false_receipt) is not False:
            errors.append(f"unsafe inventory receipt: {false_receipt}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan", type=Path, default=ROOT / "contracts" / "admin-console-plan.json"
    )
    parser.add_argument(
        "--inventory", type=Path, default=ROOT / "acceptance" / "inventory.fixture.json"
    )
    parser.add_argument(
        "--fixtures", type=Path, default=ROOT / "acceptance" / "status-cases.json"
    )
    args = parser.parse_args()

    errors = validate_contracts(
        load_json(args.plan), load_json(args.inventory), load_json(args.fixtures)
    )
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print("PASS: admin-console plan and synthetic inventory contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Emit a value-suppressed acceptance receipt for one deployed public surface.

The probe reads the owner-only Terraform access bundle, exercises the public
HTTPS edge, the authenticated admin backend, both MCP catalogs, the HTTP
scientific discovery route, the OpenAI-compatible catalog, and one real chat
completion, then compares the Kubernetes release and Kueue objects against the
exact expected source and image identities. Every credential is used only in
memory: the receipt carries identities, counts, status codes, and booleans and
never a token, cookie, presigned handle, or generated model text.

Run it from the control-plane environment, which provides ``httpx``,
``httpx2`` and ``mcp``; the offline tests need only the standard library.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import ssl
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA = "fs2-serve.nebius.ai/live-h100-qualified-platform-surface-acceptance/v2"
EXPECTATIONS_SCHEMA = "fs2-serve.nebius.ai/live-surface-expectations/v1"
BUNDLE_SCHEMA = "fs2-serve.nebius.ai/access-bundle/v1"
REQUIRED_ENDPOINTS = frozenset(
    {
        "admin_portal_url",
        "alertmanager_url",
        "grafana_url",
        "inference_base_url",
        "mcp_url",
        "tempo_explore_url",
    }
)
REQUIRED_CREDENTIALS = frozenset(
    {
        "admin_bootstrap_token",
        "inference_access_token",
        "mcp_inference_token",
        "scientific_access_token",
    }
)
EXPECTATION_LIST_FIELDS = (
    "general_model_ids",
    "openai_model_ids",
    "scientific_model_ids",
    "general_token_excluded_scientific_model_ids",
    "observability_components",
    "cluster_queues",
    "resource_flavors",
    "workload_priority_classes",
    "required_mcp_tools",
)
TERMINAL_OPERATION_STATUSES = frozenset({"succeeded", "failed", "cancelled", "preempted", "expired"})
Check = tuple[dict[str, Any], bool]


class AcceptanceInputError(ValueError):
    """An input is malformed; nothing was probed."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        raise AcceptanceInputError("endpoint URL has no scheme or host")
    return f"{parsed.scheme}://{parsed.netloc}"


def load_expectations(path: Path) -> dict[str, Any]:
    """Load and validate the deployment expectations without touching the network."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AcceptanceInputError(f"expectations file is unreadable: {error}") from None
    if not isinstance(value, dict) or value.get("schema") != EXPECTATIONS_SCHEMA:
        raise AcceptanceInputError("expectations file does not carry the live-surface expectations schema")
    for field in EXPECTATION_LIST_FIELDS:
        items = value.get(field)
        if not isinstance(items, list) or not all(isinstance(item, str) and item for item in items):
            raise AcceptanceInputError(f"expectations field {field!r} must be a list of non-empty strings")
        if len(set(items)) != len(items):
            raise AcceptanceInputError(f"expectations field {field!r} repeats an identifier")
    if not set(value["general_token_excluded_scientific_model_ids"]) <= set(value["scientific_model_ids"]):
        raise AcceptanceInputError("excluded scientific models must be a subset of the scientific catalog")
    if not set(value["openai_model_ids"]) <= set(value["general_model_ids"]):
        raise AcceptanceInputError("OpenAI-listed models must be a subset of the general catalog")
    if not isinstance(value.get("mcp_protocol_version"), str):
        raise AcceptanceInputError("expectations must name the MCP protocol version")
    probe = value.get("chat_probe")
    if probe is not None:
        if not isinstance(probe, dict):
            raise AcceptanceInputError("chat_probe must be an object")
        if probe.get("model_id") not in value["openai_model_ids"]:
            raise AcceptanceInputError("chat_probe.model_id must be an OpenAI-listed model")
        marker = probe.get("marker")
        if not isinstance(marker, str) or not marker.isascii() or len(marker) < 8 or " " in marker:
            raise AcceptanceInputError("chat_probe.marker must be a single ASCII token of at least 8 characters")
        max_tokens = probe.get("max_tokens", 512)
        if not isinstance(max_tokens, int) or not 16 <= max_tokens <= 4096:
            raise AcceptanceInputError("chat_probe.max_tokens must be an integer between 16 and 4096")
        overrides = probe.get("request_overrides", {})
        if not isinstance(overrides, dict) or {"model", "messages", "stream"} & set(overrides):
            raise AcceptanceInputError("chat_probe.request_overrides cannot replace model, messages, or stream")
        timeout = probe.get("poll_timeout_seconds", 600)
        if not isinstance(timeout, int | float) or not 1 <= timeout <= 7200:
            raise AcceptanceInputError("chat_probe.poll_timeout_seconds must be between 1 and 7200")
    return value


def read_bundle(path: Path) -> tuple[dict[str, Any], int, bool]:
    """Return the bundle document with its mode and current-user ownership."""

    if path.is_symlink():
        raise AcceptanceInputError("access bundle path must not be a symlink")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise AcceptanceInputError("access bundle must be a regular file")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise AcceptanceInputError("access bundle is not valid JSON") from None
    if not isinstance(bundle, dict):
        raise AcceptanceInputError("access bundle is not an object")
    return bundle, stat.S_IMODE(info.st_mode), info.st_uid == os.geteuid()


def secret_values(bundle: Mapping[str, Any]) -> tuple[str, ...]:
    """Every credential string in the bundle, so a receipt can be proven free of them."""

    values: list[str] = []
    credentials = bundle.get("credentials", {})
    if isinstance(credentials, Mapping):
        for value in credentials.values():
            if isinstance(value, str) and value:
                values.append(value)
            elif isinstance(value, Mapping):
                values.extend(item for item in value.values() if isinstance(item, str) and item)
    return tuple(values)


def evaluate_bundle(bundle: Mapping[str, Any], mode: int, owner_is_current_user: bool) -> Check:
    endpoints = bundle.get("endpoints", {})
    credentials = bundle.get("credentials", {})
    cluster = bundle.get("cluster", {})
    endpoint_fields = sorted(
        name for name in REQUIRED_ENDPOINTS if isinstance(endpoints, Mapping) and isinstance(endpoints.get(name), str)
    )
    grafana = credentials.get("grafana", {}) if isinstance(credentials, Mapping) else {}
    credential_fields_complete = (
        isinstance(credentials, Mapping)
        and all(isinstance(credentials.get(name), str) and bool(credentials[name]) for name in REQUIRED_CREDENTIALS)
        and isinstance(grafana, Mapping)
        and all(isinstance(grafana.get(name), str) and bool(grafana[name]) for name in ("username", "password"))
    )
    cluster_complete = isinstance(cluster, Mapping) and all(
        isinstance(cluster.get(name), str) and bool(cluster[name])
        for name in ("cluster_id", "cluster_name", "kube_context", "project_id", "region")
    )
    mcp_access = bundle.get("mcp_access", {})
    scopes = sorted(mcp_access.get("scopes", [])) if isinstance(mcp_access, Mapping) else []
    shared_token = isinstance(credentials, Mapping) and credentials.get("mcp_inference_token") == credentials.get(
        "inference_access_token"
    )
    distinct_scientific_token = isinstance(credentials, Mapping) and credentials.get("scientific_access_token") not in {
        None,
        "",
        credentials.get("inference_access_token"),
        credentials.get("admin_bootstrap_token"),
    }
    evidence = {
        "cluster_identity_complete": cluster_complete,
        "credential_fields_complete": credential_fields_complete,
        "distinct_scientific_token": distinct_scientific_token,
        "endpoint_fields_complete": endpoint_fields,
        "mcp_scopes": scopes,
        "mode": f"{mode:04o}",
        "owner_is_current_user": owner_is_current_user,
        "schema": bundle.get("schema"),
        "shared_mcp_inference_token": shared_token,
    }
    passed = (
        mode == 0o600
        and owner_is_current_user
        and bundle.get("schema") == BUNDLE_SCHEMA
        and cluster_complete
        and credential_fields_complete
        and set(endpoint_fields) == REQUIRED_ENDPOINTS
        and shared_token
        and distinct_scientific_token
    )
    return evidence, passed


def evaluate_tls(protocol: str | None, cipher: str | None, not_after: str | None, san_count: int) -> Check:
    evidence = {
        "cipher": cipher,
        "normal_trust_verified": True,
        "not_after": not_after,
        "protocol": protocol,
        "subject_alt_name_count": san_count,
    }
    return evidence, protocol == "TLSv1.3" and san_count >= 1


def evaluate_public_pages(statuses: Mapping[str, int]) -> Check:
    return dict(sorted(statuses.items())), bool(statuses) and all(value == 200 for value in statuses.values())


def image_digest(resource: Mapping[str, Any]) -> str:
    containers = resource["spec"]["template"]["spec"]["containers"]
    image = containers[0]["image"]
    if "@" not in image:
        raise AcceptanceInputError("image is not digest qualified")
    return str(image.rsplit("@", 1)[1])


def deployment_ready(resource: Mapping[str, Any]) -> tuple[bool, int]:
    desired = int(resource["spec"].get("replicas", 1))
    status_value = resource.get("status", {})
    fields = ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")
    ready = all(int(status_value.get(field, 0)) == desired for field in fields)
    ready = ready and int(status_value.get("observedGeneration", 0)) >= int(resource["metadata"]["generation"])
    return ready, desired


def daemonset_ready(resource: Mapping[str, Any]) -> tuple[bool, int, int]:
    status_value = resource.get("status", {})
    desired = int(status_value.get("desiredNumberScheduled", 0))
    ready = int(status_value.get("numberReady", 0))
    complete = (
        desired > 0
        and ready == desired
        and int(status_value.get("updatedNumberScheduled", 0)) == desired
        and int(status_value.get("numberAvailable", 0)) == desired
        and int(status_value.get("observedGeneration", 0)) >= int(resource["metadata"]["generation"])
    )
    return complete, desired, ready


def evaluate_kubernetes_release(
    deployments: Mapping[str, Mapping[str, Any]],
    observer: Mapping[str, Any],
    *,
    control_plane_digest: str,
    admin_console_digest: str,
) -> Check:
    expected = {
        "fs2-serve-control-plane": control_plane_digest,
        "fs2-serve-control-plane-admin-console": admin_console_digest,
        "fs2-serve-control-plane-model-controller": control_plane_digest,
    }
    rows: list[dict[str, Any]] = []
    passed = True
    for name, expected_digest in expected.items():
        resource = deployments.get(name)
        if resource is None:
            rows.append({"digest": None, "name": name, "ready": 0})
            passed = False
            continue
        ready, desired = deployment_ready(resource)
        digest = image_digest(resource)
        rows.append({"digest": digest, "name": name, "ready": desired if ready else 0})
        passed = passed and ready and digest == expected_digest
    observer_complete, observer_desired, observer_ready = daemonset_ready(observer)
    observer_digest = image_digest(observer)
    passed = passed and observer_complete and observer_digest == control_plane_digest
    evidence = {
        "deployments": rows,
        "gpu_allocation_observer": {"desired": observer_desired, "digest": observer_digest, "ready": observer_ready},
        "ready_release_pods": {
            "admin-console": rows[1]["ready"],
            "gateway": rows[0]["ready"],
            "model-controller": rows[2]["ready"],
        },
    }
    return evidence, passed


def active_condition(resource: Mapping[str, Any]) -> bool:
    return any(
        condition.get("type") == "Active" and condition.get("status") == "True"
        for condition in resource.get("status", {}).get("conditions", [])
    )


def evaluate_kueue(
    cluster_queues: Mapping[str, Mapping[str, Any]],
    local_queues: list[Mapping[str, Any]],
    flavors: set[str],
    priorities: set[str],
    expectations: Mapping[str, Any],
) -> Check:
    expected_queues = set(expectations["cluster_queues"])
    expected_flavors = set(expectations["resource_flavors"])
    expected_priorities = set(expectations["workload_priority_classes"])
    evidence = {
        "all_cluster_queues_active": all(active_condition(item) for item in cluster_queues.values()),
        "all_local_queues_active": all(active_condition(item) for item in local_queues),
        "cluster_queue_count": len(cluster_queues),
        "local_queue_count": len(local_queues),
        "local_queue_namespaces": sorted({str(item["metadata"]["namespace"]) for item in local_queues}),
        "required_cluster_queues": sorted(expected_queues),
        "required_resource_flavors": sorted(expected_flavors),
        "resource_flavor_count": len(flavors),
        "scientific_priority_classes": sorted(expected_priorities),
    }
    passed = (
        set(cluster_queues) == expected_queues
        and bool(cluster_queues)
        and evidence["all_cluster_queues_active"]
        and flavors == expected_flavors
        and expected_priorities <= priorities
        and bool(local_queues)
        and evidence["all_local_queues_active"]
    )
    return evidence, passed


def evaluate_admin(
    *,
    session_status: int,
    cookie_round_trip: bool,
    delete_status: int,
    context_value: Mapping[str, Any],
    models_value: Mapping[str, Any],
    scientific_value: Mapping[str, Any],
    capacity_value: Mapping[str, Any],
    observability_value: Mapping[str, Any],
    expectations: Mapping[str, Any],
) -> Check:
    general_ids: set[str] = set()
    unknown_or_unsupported: list[str] = []
    admin_gpu_classes: dict[str, str] = {}
    for item in models_value.get("items", []):
        identity = item.get("identity", {})
        runtime = item.get("runtime", {})
        identifier = identity.get("id") or item.get("id")
        state_value = runtime.get("state") or item.get("state")
        if isinstance(identifier, str):
            general_ids.add(identifier)
            if isinstance(identity.get("gpu_class"), str):
                admin_gpu_classes[identifier] = identity["gpu_class"]
            if state_value in {"unknown", "unsupported"}:
                unknown_or_unsupported.append(identifier)
    readiness_counts = {"blocked": 0, "candidate": 0, "qualified": 0, "unknown": 0}
    scientific_ids: set[str] = set()
    for item in scientific_value.get("items", []):
        identifier = item.get("model_id")
        readiness = item.get("readiness")
        if isinstance(identifier, str):
            scientific_ids.add(identifier)
        if readiness in readiness_counts:
            readiness_counts[readiness] += 1
    components = observability_value.get("components", [])
    component_ids = {item.get("id") for item in components if isinstance(item.get("id"), str)}
    launches_enabled = bool(components) and all(
        item.get("installed") is True
        and item.get("health") == "healthy"
        and item.get("data_present") is True
        and item.get("launch", {}).get("enabled") is True
        for item in components
    )
    node_scaler = capacity_value.get("node_scaler", {})
    scaler_available = node_scaler.get("state") == "available"
    expected_general = set(expectations["general_model_ids"])
    expected_scientific = set(expectations["scientific_model_ids"])
    evidence = {
        "all_observability_launches_enabled": launches_enabled,
        "cluster_queue_count": len(capacity_value.get("kueue", {}).get("cluster_queues", [])),
        "concrete_model_ids": sorted(general_ids),
        "gpu_classes": dict(sorted(admin_gpu_classes.items())),
        "local_queue_count": len(capacity_value.get("kueue", {}).get("local_queues", [])),
        "model_count": len(general_ids),
        "node_pool_count": len(capacity_value.get("node_pools", [])),
        "node_scaler": {
            "available": scaler_available,
            "configured": node_scaler.get("configured"),
            "healthy": node_scaler.get("healthy"),
            "provider": node_scaler.get("provider"),
        },
        "observability_components": sorted(component_ids),
        "scientific_model_count": len(scientific_ids),
        "scientific_readiness": readiness_counts,
        "server_authoritative_context": context_value.get("server_authoritative"),
        "session_cookie_round_trip": session_status == 200 and cookie_round_trip and delete_status == 204,
        "unknown_or_unsupported_models": sorted(unknown_or_unsupported),
    }
    passed = (
        general_ids == expected_general
        and not unknown_or_unsupported
        and scientific_ids == expected_scientific
        and readiness_counts == {"blocked": 0, "candidate": 0, "qualified": len(expected_scientific), "unknown": 0}
        and not scientific_value.get("projection_issues")
        and component_ids == set(expectations["observability_components"])
        and launches_enabled
        and scaler_available
        and node_scaler.get("configured") is True
        and node_scaler.get("healthy") is True
        and context_value.get("server_authoritative") is True
        and bool(evidence["session_cookie_round_trip"])
    )
    return evidence, passed


def model_ids(value: Mapping[str, Any]) -> set[str]:
    identifiers: set[str] = set()
    for item in value.get("data", []):
        if not isinstance(item, Mapping):
            continue
        identifier = item.get("id") or item.get("model_id")
        if isinstance(identifier, str):
            identifiers.add(identifier)
    return identifiers


def evaluate_mcp(
    projection: Mapping[str, Any],
    *,
    expected_general: set[str],
    expected_scientific: set[str],
    expectations: Mapping[str, Any],
    excluded: set[str] | None = None,
) -> Check:
    scientific = set(projection["scientific_model_ids"])
    required_tools = set(expectations["required_mcp_tools"])
    evidence: dict[str, Any] = {
        "model_ids": sorted(projection["model_ids"]),
        "private_zero_ttl_discovery": projection["ttl_ms"] == 0 and projection["cache_scope"] == "private",
        "protocol_version": projection["protocol_version"],
        "required_tools": sorted(required_tools & set(projection["tools"])),
        "scientific_model_count": len(scientific),
        "scientific_model_ids": sorted(scientific),
    }
    if excluded is not None:
        evidence["licensed_models_excluded"] = sorted(excluded)
    passed = (
        set(projection["model_ids"]) == expected_general
        and scientific == expected_scientific
        and bool(evidence["private_zero_ttl_discovery"])
        and projection["protocol_version"] == expectations["mcp_protocol_version"]
        and required_tools <= set(projection["tools"])
    )
    return evidence, passed


def evaluate_openai_catalog(
    listing: Mapping[str, Any],
    status_code: int,
    *,
    admin_gpu_classes: Mapping[str, str],
    expectations: Mapping[str, Any],
) -> Check:
    """The OpenAI catalog must name the same accelerator class the admin identity names."""

    rows: dict[str, dict[str, Any]] = {}
    for item in listing.get("data", []):
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            continue
        rows[item["id"]] = {
            "admin_gpu_class": admin_gpu_classes.get(item["id"]),
            "enabled": item.get("enabled"),
            "gpu_class": item.get("gpu_class"),
            "operations": sorted(item.get("operations", [])),
        }
    consistent = all(
        row["admin_gpu_class"] is not None and row["gpu_class"] == row["admin_gpu_class"] for row in rows.values()
    )
    evidence = {
        "gpu_class_matches_admin_identity": consistent,
        "model_ids": sorted(rows),
        "models": dict(sorted(rows.items())),
        "status": status_code,
    }
    passed = (
        status_code == 200
        and set(rows) == set(expectations["openai_model_ids"])
        and consistent
        and all(row["enabled"] is True for row in rows.values())
    )
    return evidence, passed


def evaluate_http_scientific_discovery(
    scientific_listing: Mapping[str, Any],
    scientific_status: int,
    general_listing: Mapping[str, Any],
    general_status: int,
    *,
    expectations: Mapping[str, Any],
) -> Check:
    """``GET /v1/scientific-models`` mirrors the two MCP catalogs exactly."""

    expected_all = set(expectations["scientific_model_ids"])
    excluded = set(expectations["general_token_excluded_scientific_model_ids"])
    scientific_ids = model_ids(scientific_listing)
    general_ids = model_ids(general_listing)
    complete_rows = all(
        isinstance(item, Mapping)
        and isinstance(item.get("operations"), list)
        and bool(item["operations"])
        and isinstance(item.get("service_classes"), list)
        and bool(item["service_classes"])
        and isinstance(item.get("parameter_schema"), str)
        and isinstance(item.get("execution_identity_sha256"), str)
        for item in scientific_listing.get("data", [])
    )
    evidence = {
        "general_token_model_ids": sorted(general_ids),
        "general_token_status": general_status,
        "licensed_models_excluded": sorted(expected_all - general_ids),
        "rows_carry_operations_service_classes_and_schema": complete_rows,
        "scientific_token_model_ids": sorted(scientific_ids),
        "scientific_token_status": scientific_status,
    }
    passed = (
        scientific_status == 200
        and general_status == 200
        and scientific_ids == expected_all
        and general_ids == expected_all - excluded
        and complete_rows
    )
    return evidence, passed


def completion_text(document: Mapping[str, Any]) -> str:
    choices = document.get("choices", [])
    if not choices or not isinstance(choices[0], Mapping):
        return ""
    message = choices[0].get("message", {})
    content = message.get("content") if isinstance(message, Mapping) else None
    return content if isinstance(content, str) else ""


def evaluate_chat(
    *,
    status_code: int,
    document: Mapping[str, Any],
    marker: str,
    model_id: str,
    elapsed_seconds: float,
    terminal_status: str | None,
    operation_id_present: bool,
) -> Check:
    """Judge one real completion without copying the generated text into the receipt."""

    text = completion_text(document)
    usage = document.get("usage", {}) if isinstance(document.get("usage"), Mapping) else {}
    completion_tokens = usage.get("completion_tokens")
    choices = document.get("choices", [])
    finish_reason = choices[0].get("finish_reason") if choices and isinstance(choices[0], Mapping) else None
    evidence = {
        "completion_tokens": completion_tokens,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "finish_reason": finish_reason,
        "marker_present": marker in text,
        "model": document.get("model"),
        "operation_id_present": operation_id_present,
        "status": status_code,
        "terminal_status": terminal_status,
    }
    passed = (
        status_code == 200
        and document.get("model") == model_id
        and marker in text
        and isinstance(completion_tokens, int)
        and completion_tokens > 0
        and operation_id_present
        and terminal_status == "succeeded"
    )
    return evidence, passed


def build_receipt(
    checks: Mapping[str, Check],
    *,
    started_at: str,
    completed_at: str,
    target: Mapping[str, str],
    expectations_sha256: str,
) -> dict[str, Any]:
    failures = sorted(name for name, (_, passed) in checks.items() if not passed)
    return {
        "checks": {
            name: {"evidence": evidence, "status": "PASS" if passed else "FAIL"}
            for name, (evidence, passed) in sorted(checks.items())
        },
        "completed_at": completed_at,
        "expectations_sha256": expectations_sha256,
        "failures": failures,
        "schema": SCHEMA,
        "started_at": started_at,
        "status": "PASS" if not failures else "FAIL",
        "target": dict(sorted(target.items())),
    }


def assert_value_free(receipt: Mapping[str, Any], secrets: tuple[str, ...]) -> None:
    rendered = json.dumps(receipt, sort_keys=True)
    for value in secrets:
        if value and value in rendered:
            raise AcceptanceInputError("receipt would contain a credential value; refusing to write it")


def write_receipt(path: Path, receipt: Mapping[str, Any], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise AcceptanceInputError(f"receipt already exists: {path}; pass --overwrite to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


# --- live collection -------------------------------------------------------------


def kubectl_json(kubeconfig: Path, context: str, *arguments: str) -> dict[str, Any]:
    completed = subprocess.run(  # noqa: S603 - fixed executable, operator-supplied identifiers only.
        ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context, *arguments, "-o", "json"],  # noqa: S607
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=60,
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise AcceptanceInputError("kubectl response is not an object")
    return value


def collect_tls(host: str) -> tuple[str | None, str | None, str | None, int]:
    tls_context = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=15) as raw_socket:
        with tls_context.wrap_socket(raw_socket, server_hostname=host) as tls_socket:
            certificate = tls_socket.getpeercert() or {}
            cipher = tls_socket.cipher()
            return (
                tls_socket.version(),
                cipher[0] if cipher else None,
                certificate.get("notAfter"),
                len(certificate.get("subjectAltName", ())),
            )


def result_payload(result: Any) -> dict[str, Any]:
    value = getattr(result, "structured_content", None)
    if isinstance(value, dict):
        if set(value) == {"result"} and isinstance(value["result"], dict):
            return value["result"]
        return value
    for item in getattr(result, "content", []):
        text_value = getattr(item, "text", None)
        if not isinstance(text_value, str):
            continue
        try:
            decoded = json.loads(text_value)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            return decoded
    return {}


async def mcp_projection(endpoint: str, token: str, protocol: str) -> dict[str, Any]:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(
        headers={"authorization": f"Bearer {token}", "origin": origin(endpoint)},
        follow_redirects=False,
        trust_env=False,
        verify=True,
        timeout=30,
    ) as http_client:
        async with Client(streamable_http_client(endpoint, http_client=http_client), mode=protocol) as client:
            listing = await client.list_tools()
            general = result_payload(await client.call_tool("list_models", {}))
            scientific = result_payload(await client.call_tool("list_scientific_models", {}))
            return {
                "cache_scope": listing.cache_scope,
                "model_ids": sorted(model_ids(general)),
                "protocol_version": client.protocol_version,
                "scientific_model_ids": sorted(model_ids(scientific)),
                "tools": sorted(tool.name for tool in listing.tools),
                "ttl_ms": listing.ttl_ms,
            }


def chat_probe(
    client: Any,
    *,
    token: str,
    probe: Mapping[str, Any],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Check:
    marker = str(probe["marker"])
    model_id = str(probe["model_id"])
    body: dict[str, Any] = {
        "model": model_id,
        "messages": [{"role": "user", "content": f"Reply with exactly the token {marker} and nothing else."}],
        "max_tokens": int(probe.get("max_tokens", 512)),
        "temperature": 0,
    }
    body.update(probe.get("request_overrides", {}))
    headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}
    started = clock()
    response = client.post("/v1/chat/completions", headers=headers, json=body)
    operation_id = response.headers.get("x-fs2-operation-id")
    document: dict[str, Any] = {}
    terminal_status: str | None = None
    try:
        document = response.json() if response.content else {}
    except ValueError:
        document = {}
    status_code = response.status_code
    if status_code == 202 and operation_id:
        # The gateway bounded its synchronous wait; the operation continues
        # durably and the same result is retrievable once it is terminal.
        deadline = started + float(probe.get("poll_timeout_seconds", 600))
        while clock() < deadline:
            status = client.get(f"/v1/operations/{operation_id}", headers=headers)
            state = status.json().get("status") if status.status_code == 200 else None
            if state in TERMINAL_OPERATION_STATUSES:
                terminal_status = str(state)
                break
            sleep(2)
        if terminal_status == "succeeded":
            result = client.get(f"/v1/operations/{operation_id}/result", headers=headers)
            status_code = result.status_code
            document = result.json() if result.status_code == 200 else {}
    elif status_code == 200 and operation_id:
        status = client.get(f"/v1/operations/{operation_id}", headers=headers)
        terminal_status = str(status.json().get("status")) if status.status_code == 200 else None
    return evaluate_chat(
        status_code=status_code,
        document=document,
        marker=marker,
        model_id=model_id,
        elapsed_seconds=clock() - started,
        terminal_status=terminal_status,
        operation_id_present=bool(operation_id),
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    import hashlib

    import httpx

    expectations = load_expectations(args.expectations)
    expectations_sha256 = hashlib.sha256(args.expectations.read_bytes()).hexdigest()
    bundle, bundle_mode, bundle_owner = read_bundle(args.bundle)
    secrets = secret_values(bundle)
    started_at = utc_now()
    checks: dict[str, Check] = {}
    checks["terraform_output_bundle"] = evaluate_bundle(bundle, bundle_mode, bundle_owner)
    if not checks["terraform_output_bundle"][1]:
        raise AcceptanceInputError("access bundle is incomplete or not owner-only; refusing to probe with it")
    endpoints = bundle["endpoints"]
    credentials = bundle["credentials"]
    public_origin = origin(endpoints["admin_portal_url"])
    host = urlsplit(public_origin).hostname
    if host is None:
        raise AcceptanceInputError("public endpoint has no host")

    checks["tls_normal_trust"] = evaluate_tls(*collect_tls(host))

    grafana_auth = (credentials["grafana"]["username"], credentials["grafana"]["password"])
    public_targets: dict[str, tuple[str, tuple[str, str] | None]] = {
        "admin": (endpoints["admin_portal_url"], None),
        "alertmanager_url": (endpoints["alertmanager_url"], grafana_auth),
        "grafana_api_health": (endpoints["grafana_url"].rstrip("/") + "/api/health", grafana_auth),
        "readyz": (public_origin + "/readyz", None),
        "tempo_explore_url": (endpoints["tempo_explore_url"], grafana_auth),
    }
    statuses: dict[str, int] = {}
    with httpx.Client(verify=True, timeout=30, follow_redirects=True, trust_env=False) as client:
        for name, (url, auth) in public_targets.items():
            statuses[name] = client.get(url, auth=auth).status_code
    checks["public_pages"] = evaluate_public_pages(statuses)

    deployments_value = kubectl_json(args.kubeconfig, args.context, "-n", "fs2-system", "get", "deployment")
    deployments = {item["metadata"]["name"]: item for item in deployments_value["items"]}
    observer = kubectl_json(
        args.kubeconfig, args.context, "-n", "fs2-system", "get", "daemonset", "fs2-serve-control-plane-gpu-observer"
    )
    checks["kubernetes_release"] = evaluate_kubernetes_release(
        deployments,
        observer,
        control_plane_digest=args.control_plane_digest,
        admin_console_digest=args.admin_console_digest,
    )

    cluster_queues_value = kubectl_json(args.kubeconfig, args.context, "get", "clusterqueue")
    local_queues_value = kubectl_json(args.kubeconfig, args.context, "get", "localqueue", "-A")
    flavors_value = kubectl_json(args.kubeconfig, args.context, "get", "resourceflavor")
    priorities_value = kubectl_json(args.kubeconfig, args.context, "get", "workloadpriorityclass")
    checks["kueue"] = evaluate_kueue(
        {item["metadata"]["name"]: item for item in cluster_queues_value["items"]},
        list(local_queues_value["items"]),
        {item["metadata"]["name"] for item in flavors_value["items"]},
        {item["metadata"]["name"] for item in priorities_value["items"]},
        expectations,
    )

    with httpx.Client(base_url=public_origin, verify=True, timeout=30, trust_env=False) as client:
        session_response = client.post(
            "/admin/api/v1/session",
            headers={"authorization": "Bearer " + credentials["admin_bootstrap_token"]},
        )
        cookie_round_trip = bool(client.cookies.get("__Host-fs2_admin_session"))

        def admin_data(path: str) -> dict[str, Any]:
            response = client.get(path)
            response.raise_for_status()
            value = response.json().get("data")
            if not isinstance(value, dict):
                raise AcceptanceInputError("admin payload is not an object")
            return value

        context_value = admin_data("/admin/api/v1/context")
        models_value = admin_data("/admin/api/v1/models")
        scientific_value = admin_data("/admin/api/v1/scientific-models")
        capacity_value = admin_data("/admin/api/v1/capacity")
        observability_value = admin_data("/admin/api/v1/observability")
        delete_status = client.delete("/admin/api/v1/session").status_code
    checks["admin_backend_fully_qualified"] = evaluate_admin(
        session_status=session_response.status_code,
        cookie_round_trip=cookie_round_trip,
        delete_status=delete_status,
        context_value=context_value,
        models_value=models_value,
        scientific_value=scientific_value,
        capacity_value=capacity_value,
        observability_value=observability_value,
        expectations=expectations,
    )
    admin_gpu_classes = checks["admin_backend_fully_qualified"][0]["gpu_classes"]

    expected_general = set(expectations["general_model_ids"])
    expected_scientific = set(expectations["scientific_model_ids"])
    excluded = set(expectations["general_token_excluded_scientific_model_ids"])
    protocol = str(expectations["mcp_protocol_version"])
    general_mcp, scientific_mcp = asyncio.run(
        _both(
            mcp_projection(endpoints["mcp_url"], credentials["mcp_inference_token"], protocol),
            mcp_projection(endpoints["mcp_url"], credentials["scientific_access_token"], protocol),
        )
    )
    checks["general_mcp_scoped_catalog"] = evaluate_mcp(
        general_mcp,
        expected_general=expected_general,
        expected_scientific=expected_scientific - excluded,
        expectations=expectations,
        excluded=excluded,
    )
    checks["scientific_mcp_complete_catalog"] = evaluate_mcp(
        scientific_mcp,
        expected_general=set(),
        expected_scientific=expected_scientific,
        expectations=expectations,
    )

    with httpx.Client(base_url=public_origin, verify=True, timeout=120, trust_env=False) as client:
        general_headers = {"authorization": "Bearer " + credentials["inference_access_token"]}
        scientific_headers = {"authorization": "Bearer " + credentials["scientific_access_token"]}
        openai_listing = client.get("/v1/models", headers=general_headers)
        checks["openai_catalog"] = evaluate_openai_catalog(
            openai_listing.json() if openai_listing.status_code == 200 else {},
            openai_listing.status_code,
            admin_gpu_classes=admin_gpu_classes,
            expectations=expectations,
        )
        scientific_http = client.get("/v1/scientific-models", headers=scientific_headers)
        general_http = client.get("/v1/scientific-models", headers=general_headers)
        checks["http_scientific_discovery"] = evaluate_http_scientific_discovery(
            scientific_http.json() if scientific_http.status_code == 200 else {},
            scientific_http.status_code,
            general_http.json() if general_http.status_code == 200 else {},
            general_http.status_code,
            expectations=expectations,
        )
        probe = expectations.get("chat_probe")
        if probe is not None:
            checks["openai_chat_semantic"] = chat_probe(
                client, token=credentials["inference_access_token"], probe=probe
            )

    receipt = build_receipt(
        checks,
        started_at=started_at,
        completed_at=utc_now(),
        target={
            "admin_console_digest": args.admin_console_digest,
            "control_plane_digest": args.control_plane_digest,
            "endpoint_host": host,
            "source_commit": args.source_commit,
        },
        expectations_sha256=expectations_sha256,
    )
    assert_value_free(receipt, secrets)
    return receipt


async def _both(*awaitables: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    first, second = await asyncio.gather(*awaitables)
    return first, second


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", type=Path, required=True, help="owner-only inference-stack output bundle")
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True, help="kubeconfig context of the deployed cluster")
    parser.add_argument("--expectations", type=Path, required=True, help="deployment expectations JSON")
    parser.add_argument("--source-commit", required=True, help="exact deployed Git commit")
    parser.add_argument("--control-plane-digest", required=True, help="deployed control-plane OCI index digest")
    parser.add_argument("--admin-console-digest", required=True, help="deployed admin-console OCI digest")
    parser.add_argument("--receipt", type=Path, help="mode-0600 receipt path; stdout when omitted")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing receipt")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for name in ("source_commit",):
        value = getattr(args, name)
        if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
            raise AcceptanceInputError(f"--{name.replace('_', '-')} must be a full lowercase commit SHA")
    for name in ("control_plane_digest", "admin_console_digest"):
        value = getattr(args, name)
        if not value.startswith("sha256:") or len(value) != 71:
            raise AcceptanceInputError(f"--{name.replace('_', '-')} must be a full sha256: digest")
    receipt = run(args)
    if args.receipt is not None:
        write_receipt(args.receipt, receipt, overwrite=args.overwrite)
        print(json.dumps({"receipt": str(args.receipt), "status": receipt["status"], "failures": receipt["failures"]}))
    else:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AcceptanceInputError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None

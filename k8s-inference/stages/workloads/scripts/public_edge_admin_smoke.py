#!/usr/bin/env python3
"""Smoke the public admin and Grafana endpoints without a port-forward."""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TOKEN_BYTES = 16 * 1024
ADMIN_SCHEMA = "fs2.admin-api/v1"
REQUIRED_OBSERVABILITY_COMPONENTS = frozenset(
    {"grafana", "prometheus", "loki", "otel", "dcgm", "kueue", "keda"}
)


class SmokeError(RuntimeError):
    """A concise public-edge smoke failure."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str


def normalize_origin(endpoint: str) -> str:
    """Accept the public origin or its admin URL and return the HTTPS origin."""
    try:
        parsed = urlsplit(endpoint.strip())
        port = parsed.port
    except ValueError as exc:
        raise SmokeError("endpoint is not a valid HTTPS URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/admin"}
    ):
        raise SmokeError("endpoint must be an HTTPS origin or its /admin/ URL")
    if port is not None and not 1 <= port <= 65535:
        raise SmokeError("endpoint port is invalid")
    return urlunsplit(("https", parsed.netloc, "", "", ""))


def read_token(path: Path) -> str:
    """Read one non-empty token without ever returning it in an error message."""
    try:
        if path.stat().st_size > MAX_TOKEN_BYTES:
            raise SmokeError("admin token file is too large")
        token = path.read_text(encoding="utf-8").strip()
    except SmokeError:
        raise
    except (OSError, UnicodeError) as exc:
        raise SmokeError("admin token file could not be read") from exc
    if not token:
        raise SmokeError("admin token file is empty")
    return token


class HttpsTransport:
    """Small verified-HTTPS client with bounded response bodies."""

    def __init__(
        self,
        origin: str,
        *,
        timeout_seconds: float,
        ca_file: Path | None = None,
    ) -> None:
        self.origin = origin
        self.timeout_seconds = timeout_seconds
        try:
            context = ssl.create_default_context(
                cafile=str(ca_file) if ca_file is not None else None
            )
        except (OSError, ssl.SSLError) as exc:
            raise SmokeError("TLS CA file could not be loaded") from exc
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context)
        )

    @staticmethod
    def _read(response: object) -> bytes:
        body = response.read(MAX_RESPONSE_BYTES + 1)  # type: ignore[attr-defined]
        if len(body) > MAX_RESPONSE_BYTES:
            raise SmokeError("public endpoint response exceeds 8 MiB")
        return body

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        request = urllib.request.Request(
            self.origin + path,
            method=method,
            headers=dict(headers or {}),
            data=body,
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                return HttpResponse(
                    status=response.status,
                    headers={
                        name.lower(): value for name, value in response.headers.items()
                    },
                    body=self._read(response),
                    url=response.geturl(),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status=exc.code,
                headers={name.lower(): value for name, value in exc.headers.items()},
                body=self._read(exc),
                url=exc.geturl(),
            )
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise SmokeError(
                f"{path} could not be reached over verified HTTPS"
            ) from exc


def _json_object(response: HttpResponse, label: str) -> dict[str, object]:
    if response.status != 200:
        raise SmokeError(f"{label} returned HTTP {response.status}")
    try:
        value = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeError(f"{label} did not return JSON") from exc
    if not isinstance(value, dict):
        raise SmokeError(f"{label} did not return a JSON object")
    return value


def _admin_envelope(
    response: HttpResponse, label: str
) -> tuple[dict[str, object], dict[str, int]]:
    envelope = _json_object(response, label)
    meta = envelope.get("meta")
    data = envelope.get("data")
    if not isinstance(meta, dict) or not isinstance(data, dict):
        raise SmokeError(f"{label} did not return an admin envelope")
    if meta.get("schema_version") != ADMIN_SCHEMA:
        raise SmokeError(f"{label} returned an unsupported admin schema")

    source_counts: dict[str, int] = {}
    sources = meta.get("sources")
    if not isinstance(sources, list) or not sources:
        raise SmokeError(f"{label} did not report its data sources")
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("state"), str):
            raise SmokeError(f"{label} returned an invalid data-source record")
        state = str(source["state"])
        source_counts[state] = source_counts.get(state, 0) + 1
    if any(state != "available" for state in source_counts):
        raise SmokeError(f"{label} reports an unavailable data source")
    return data, source_counts


def _items(data: Mapping[str, object], label: str) -> list[object]:
    items = data.get("items")
    if not isinstance(items, list):
        raise SmokeError(f"{label} did not return an item list")
    return items


def _validate_admin_html(response: HttpResponse) -> None:
    content_type = response.headers.get("content-type", "").lower()
    document = response.body.lower()
    if response.status != 200:
        raise SmokeError(f"admin portal returned HTTP {response.status}")
    if "text/html" not in content_type or not (
        b"<html" in document or b"<!doctype html" in document
    ):
        raise SmokeError("admin portal did not return HTML")


def _session_cookie(response: HttpResponse) -> tuple[str, dict[str, int]]:
    _, sources = _admin_envelope(response, "admin session")
    cookie_header = response.headers.get("set-cookie", "")
    cookie = cookie_header.split(";", 1)[0].strip()
    if not cookie.startswith("__Host-fs2_admin_session="):
        raise SmokeError("admin session did not return its session cookie")
    return cookie, sources


def _admin_get(
    transport: HttpsTransport,
    cookie: str,
    path: str,
    label: str,
) -> tuple[dict[str, object], dict[str, int]]:
    return _admin_envelope(
        transport.request(
            path,
            headers={
                "Accept": "application/json",
                "Cookie": cookie,
                "Origin": transport.origin,
            },
        ),
        label,
    )


def _capacity_summary(data: Mapping[str, object]) -> dict[str, object]:
    node_pools = data.get("node_pools")
    kueue = data.get("kueue")
    autoscaling = data.get("autoscaling")
    node_scaler = data.get("node_scaler")
    if not isinstance(node_pools, dict) or not isinstance(
        node_pools.get("items"), list
    ):
        raise SmokeError("admin capacity did not return node pools")
    if not isinstance(kueue, dict):
        raise SmokeError("admin capacity did not return queue data")
    cluster_queues = kueue.get("cluster_queues")
    local_queues = kueue.get("local_queues")
    if not isinstance(cluster_queues, list) or not isinstance(local_queues, list):
        raise SmokeError("admin capacity did not return cluster and local queues")
    if not isinstance(autoscaling, dict) or not isinstance(node_scaler, dict):
        raise SmokeError("admin capacity did not return scaling data")
    hpa = autoscaling.get("hpa")
    keda = autoscaling.get("keda")
    if (
        node_pools.get("state") != "available"
        or kueue.get("state") != "available"
        or not isinstance(hpa, dict)
        or hpa.get("state") != "available"
        or not isinstance(keda, dict)
        or keda.get("state") != "available"
        or node_scaler.get("state") != "available"
        or node_scaler.get("configured") is not True
        or node_scaler.get("healthy") is not True
    ):
        raise SmokeError("admin capacity reports unavailable scaling or queue data")
    return {
        "node_pools": len(node_pools["items"]),
        "node_pool_state": node_pools.get("state"),
        "cluster_queues": len(cluster_queues),
        "local_queues": len(local_queues),
        "queue_state": kueue.get("state"),
        "node_scaler_state": node_scaler.get("state"),
    }


def _observability_summary(data: Mapping[str, object]) -> dict[str, object]:
    components = data.get("components")
    if not isinstance(components, list):
        raise SmokeError("admin observability did not return components")
    states: dict[str, int] = {}
    observed_ids: set[str] = set()
    for component in components:
        if (
            not isinstance(component, dict)
            or not isinstance(component.get("id"), str)
            or not isinstance(component.get("health"), str)
        ):
            raise SmokeError("admin observability returned an invalid component")
        component_id = str(component["id"])
        health = str(component["health"])
        observed_ids.add(component_id)
        states[health] = states.get(health, 0) + 1
        if component_id in REQUIRED_OBSERVABILITY_COMPONENTS and health != "healthy":
            raise SmokeError(
                f"required observability component {component_id} is not healthy"
            )
        if (
            health != "healthy"
            and component.get("reason") != "component is configured as not installed"
        ):
            raise SmokeError(f"observability component {component_id} is unhealthy")
    missing = sorted(REQUIRED_OBSERVABILITY_COMPONENTS - observed_ids)
    if missing:
        raise SmokeError(
            "admin observability is missing required components: " + ", ".join(missing)
        )
    return {"components": len(components), "health": states}


def _grafana_summary(transport: HttpsTransport) -> dict[str, object]:
    """Accept either Grafana's public health response or its native login page."""
    health = transport.request(
        "/admin/observability/grafana/api/health",
        headers={"Accept": "application/json"},
    )
    if health.status == 200:
        try:
            value = json.loads(health.body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            value = None
        if isinstance(value, dict) and value.get("database") == "ok":
            return {
                "mode": "health",
                "database": "ok",
                "version": value.get("version")
                if isinstance(value.get("version"), str)
                else None,
            }

    login = transport.request(
        "/admin/observability/grafana/login",
        headers={"Accept": "text/html"},
    )
    content_type = login.headers.get("content-type", "").lower()
    document = login.body.lower()
    if (
        login.status != 200
        or "text/html" not in content_type
        or b"grafana" not in document
        or not (b"<html" in document or b"<!doctype html" in document)
    ):
        raise SmokeError("Grafana health and native login routes are unavailable")
    return {"mode": "login", "database": None, "version": None}


def run_smoke(transport: HttpsTransport, admin_token: str) -> dict[str, object]:
    """Run read-only admin views and a native Grafana reachability check."""
    _validate_admin_html(transport.request("/admin/", headers={"Accept": "text/html"}))

    session_response = transport.request(
        "/admin/api/v1/session",
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {admin_token}",
            "Origin": transport.origin,
        },
        body=b"",
    )
    cookie, session_sources = _session_cookie(session_response)
    try:
        current_session, current_sources = _admin_get(
            transport, cookie, "/admin/api/v1/session", "current admin session"
        )
        if not isinstance(current_session.get("principal"), dict):
            raise SmokeError("current admin session did not return a principal")

        models, model_sources = _admin_get(
            transport, cookie, "/admin/api/v1/models?limit=256", "admin models"
        )
        model_items = _items(models, "admin models")
        if not isinstance(models.get("total"), int):
            raise SmokeError("admin models did not return a total")

        capacity, capacity_sources = _admin_get(
            transport, cookie, "/admin/api/v1/capacity", "admin capacity"
        )
        observability, observability_sources = _admin_get(
            transport,
            cookie,
            "/admin/api/v1/observability",
            "admin observability",
        )
        principals, principal_sources = _admin_get(
            transport,
            cookie,
            "/admin/api/v1/principals?limit=200",
            "admin users",
        )
        api_keys, api_key_sources = _admin_get(
            transport, cookie, "/admin/api/v1/keys?limit=200", "admin API keys"
        )
        grafana = _grafana_summary(transport)
    except Exception:
        try:
            transport.request(
                "/admin/api/v1/session",
                method="DELETE",
                headers={"Cookie": cookie, "Origin": transport.origin},
            )
        except Exception:
            pass
        raise

    logout = transport.request(
        "/admin/api/v1/session",
        method="DELETE",
        headers={"Cookie": cookie, "Origin": transport.origin},
    )
    if logout.status != 204:
        raise SmokeError(f"admin logout returned HTTP {logout.status}")

    return {
        "schema": "fs2-serve.nebius.ai/public-edge-admin-smoke/v1",
        "status": "PASS",
        "origin": transport.origin,
        "tls": {"verified": True},
        "admin_portal": {"html": True},
        "admin_api": {
            "session": {"authenticated": True, "sources": session_sources},
            "current_session": {"sources": current_sources},
            "models": {"items": len(model_items), "sources": model_sources},
            "capacity_and_queues": {
                **_capacity_summary(capacity),
                "sources": capacity_sources,
            },
            "observability": {
                **_observability_summary(observability),
                "sources": observability_sources,
            },
            "users": {
                "items": len(_items(principals, "admin users")),
                "sources": principal_sources,
            },
            "api_keys": {
                "items": len(_items(api_keys, "admin API keys")),
                "sources": api_key_sources,
            },
        },
        "grafana": grafana,
        "session_closed": True,
    }


def _redacted(message: str, secret: str | None) -> str:
    if secret:
        return message.replace(secret, "<redacted>")
    return message


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint",
        required=True,
        help="public HTTPS origin or admin_web_interface_url Terraform output",
    )
    parser.add_argument("--admin-token-file", type=Path, required=True)
    parser.add_argument(
        "--ca-file",
        type=Path,
        help="optional CA bundle while retaining certificate verification",
    )
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)

    token: str | None = None
    try:
        if not 1 <= args.timeout_seconds <= 300:
            raise SmokeError("timeout-seconds must be from 1 through 300")
        origin = normalize_origin(args.endpoint)
        token = read_token(args.admin_token_file)
        result = run_smoke(
            HttpsTransport(
                origin,
                timeout_seconds=args.timeout_seconds,
                ca_file=args.ca_file,
            ),
            token,
        )
    except SmokeError as exc:
        print(
            f"public-edge admin smoke failed: {_redacted(str(exc), token)}",
            file=sys.stderr,
        )
        return 1
    except (
        Exception
    ) as exc:  # Keep arbitrary exception detail from reflecting credentials.
        print(
            "public-edge admin smoke failed: "
            + _redacted(f"unexpected {type(exc).__name__}", token),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

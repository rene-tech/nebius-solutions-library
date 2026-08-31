#!/usr/bin/env python3
"""Run a bounded same-origin acceptance through two loopback port-forwards."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import socket
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID, uuid4

BIND_ADDRESS = "127.0.0.1"
DEFAULT_CONTROL_PORT = 18080
DEFAULT_ADMIN_PORT = 18081
DEFAULT_PROXY_PORT = 18082
CONTROL_PORT = DEFAULT_CONTROL_PORT
ADMIN_PORT = DEFAULT_ADMIN_PORT
PROXY_PORT = DEFAULT_PROXY_PORT
APPLICATION_ORIGIN = f"http://localhost:{PROXY_PORT}"
CONTROL_SERVICE = "fs2-serve-control-plane"
ADMIN_SERVICE = "fs2-serve-control-plane-admin-console"
SERVICE_PORT = 8080
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
PRIVATE_FILE_MAX_BYTES = 16 * 1024
PROTEINMPNN_SEMANTIC_REQUEST_MAX_BYTES = 96 * 1024
ACCEPTANCE_TIMEOUT_SECONDS = 7200
ACCEPTANCE_GPU_SECONDS_BUDGET = 7200
OPEN_RUNTIME_SCHEMA = "fs2-serve.nebius.ai/open-runtime-response/v1"
PROTEINMPNN_REVISION = "8907e6671bfbfc92303b5f79c4b5e6ce47cdef57"
PROTEINMPNN_AMINO_ACIDS = frozenset("ACDEFGHIKLMNPQRSTVWY")
PROTEINMPNN_ORACLE = "proteinmpnn-open-runtime-v1"
PROTEINMPNN_MCP_TOOL_BASE = "infer_proteinmpnn"
PROTEINMPNN_MCP_PROTOCOL = "native"
PROTEINMPNN_MCP_TOOL = (
    f"{PROTEINMPNN_MCP_TOOL_BASE}_{PROTEINMPNN_MCP_PROTOCOL.replace('-', '_')}"
)
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def configure_local_ports(
    control_plane: int,
    admin_console: int,
    operator_proxy: int,
) -> None:
    """Bind the process to one validated Terraform port-forward tuple."""
    ports = (control_plane, admin_console, operator_proxy)
    if any(isinstance(port, bool) or not 1024 <= port <= 65535 for port in ports):
        raise ValueError("local ports must be whole TCP ports from 1024 through 65535")
    if len(set(ports)) != len(ports):
        raise ValueError(
            "control-plane, admin-console, and operator-proxy ports must differ"
        )

    global APPLICATION_ORIGIN, CONTROL_PORT, ADMIN_PORT, PROXY_PORT
    CONTROL_PORT = control_plane
    ADMIN_PORT = admin_console
    PROXY_PORT = operator_proxy
    APPLICATION_ORIGIN = f"http://localhost:{PROXY_PORT}"


def upstream_port(path: str) -> int:
    """Route browser assets to the console and API/MCP traffic to control."""
    if path == "/healthz":
        return ADMIN_PORT
    if path == "/admin" or (
        path.startswith("/admin/")
        and path != "/admin/api"
        and not path.startswith("/admin/api/")
    ):
        return ADMIN_PORT
    return CONTROL_PORT


class SameOriginProxy(BaseHTTPRequestHandler):
    """Small, non-caching reverse proxy for the reviewed loopback contract."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._forward()

    def do_POST(self) -> None:  # noqa: N802
        self._forward()

    def do_PUT(self) -> None:  # noqa: N802
        self._forward()

    def do_PATCH(self) -> None:  # noqa: N802
        self._forward()

    def do_DELETE(self) -> None:  # noqa: N802
        self._forward()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._forward()

    def log_message(self, _format: str, *args: object) -> None:
        del args

    def _body(self) -> bytes:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("request body exceeds the bounded proxy limit")
        return self.rfile.read(length) if length else b""

    def _forward(self) -> None:
        try:
            body = self._body()
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in HOP_BY_HOP | {"host", "content-length"}
            }
            headers["Host"] = f"localhost:{PROXY_PORT}"
            headers["Content-Length"] = str(len(body))
            connection = http.client.HTTPConnection(
                BIND_ADDRESS,
                upstream_port(self.path),
                timeout=30,
            )
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ValueError("response body exceeds the bounded proxy limit")
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in HOP_BY_HOP | {"content-length"}:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            connection.close()
        except (OSError, http.client.HTTPException, ValueError):
            self.send_error(502, "loopback upstream unavailable")


def checked_private_file(
    path: Path,
    label: str,
    *,
    max_bytes: int = PRIVATE_FILE_MAX_BYTES,
) -> str:
    resolved = path.resolve(strict=True)
    mode = stat.S_IMODE(resolved.stat().st_mode)
    if mode != 0o600:
        raise ValueError(f"{label} must have mode 0600")
    value = resolved.read_text(encoding="utf-8").strip()
    if not value or len(value.encode()) > max_bytes:
        raise ValueError(f"{label} is empty or exceeds {max_bytes // 1024} KiB")
    return value


def port_forward_command(kubeconfig: Path, context: str, service: str, local_port: int) -> list[str]:
    return [
        "kubectl",
        "--kubeconfig",
        str(kubeconfig),
        "--context",
        context,
        "--namespace",
        "fs2-system",
        "port-forward",
        "--address",
        BIND_ADDRESS,
        f"service/{service}",
        f"{local_port}:{SERVICE_PORT}",
    ]


def wait_for_port(processes: list[subprocess.Popen[bytes]], port: int, deadline: float) -> None:
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in processes):
            raise RuntimeError("kubectl port-forward exited before acceptance")
        try:
            with socket.create_connection((BIND_ADDRESS, port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"loopback port {port} did not become ready")


def request(
    path: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, Mapping[str, str], bytes]:
    call = urllib.request.Request(  # noqa: S310 - exact constant HTTP loopback origin
        APPLICATION_ORIGIN + path,
        method=method,
        headers=dict(headers or {}),
        data=body,
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - exact constant HTTP loopback origin
            call, timeout=30
        ) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise RuntimeError("acceptance response exceeds 128 MiB")
            return response.status, response.headers, payload
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{path} returned HTTP {exc.code}") from exc


def decoded_json(payload: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        events = [
            line.removeprefix(b"data:").strip()
            for line in payload.splitlines()
            if line.startswith(b"data:")
        ]
        try:
            value = json.loads(events[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{label} returned an invalid JSON envelope") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} returned a non-object JSON envelope")
    return value


def issue_acceptance_pat(admin_token: str, model_id: str) -> tuple[str, str]:
    payload = json.dumps(
        {
            "principal_id": "terraform-internal-edge-acceptance",
            "tenant_id": "terraform-acceptance",
            "scopes": [
                "catalog.read",
                "inference.invoke",
                "mcp.invoke",
                "operations.read",
                "operations.result",
            ],
            "models": [model_id],
            "request_budget": 100,
            # One reviewed ProteinMPNN request may occupy one GPU for the
            # complete two-hour preemptible scale-from-zero deadline.
            "gpu_seconds_budget": ACCEPTANCE_GPU_SECONDS_BUDGET,
            "max_concurrency": 1,
        },
        separators=(",", ":"),
    ).encode()
    status, _, response = request(
        "/admin/v1/tokens",
        method="POST",
        headers={
            "Authorization": f"Bearer {admin_token}",
            "Content-Type": "application/json",
            "Origin": APPLICATION_ORIGIN,
        },
        body=payload,
    )
    issued = decoded_json(response, "PAT issuance")
    token = issued.get("token")
    token_id = issued.get("id")
    try:
        parsed_id = UUID(str(token_id))
    except ValueError:
        parsed_id = None
    if (
        status != 200
        or not isinstance(token, str)
        or not token.startswith("fs2_pat_")
        or not isinstance(token_id, str)
        or parsed_id is None
        or str(parsed_id) != token_id
    ):
        raise RuntimeError("control plane did not issue the bounded acceptance PAT")
    return token, token_id


def revoke_acceptance_pat(admin_token: str, token_id: str) -> None:
    status, _, response = request(
        f"/admin/v1/tokens/{token_id}",
        method="DELETE",
        headers={
            "Authorization": f"Bearer {admin_token}",
            "Origin": APPLICATION_ORIGIN,
        },
    )
    revoked = decoded_json(response, "PAT revocation")
    if (
        status != 200
        or revoked.get("id") != token_id
        or not isinstance(revoked.get("revoked_at"), str)
        or not revoked["revoked_at"]
    ):
        raise RuntimeError("control plane did not revoke the acceptance PAT")


def _mcp_rpc(
    pat: str,
    *,
    request_id: str | None,
    method: str,
    params: dict[str, object] | None,
    protocol_version: str | None,
    modern: bool,
    session_id: str | None = None,
) -> tuple[int, Mapping[str, str], dict[str, object] | None]:
    headers = {
        "Authorization": f"Bearer {pat}",
        "Origin": APPLICATION_ORIGIN,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if protocol_version is not None:
        headers["MCP-Protocol-Version"] = protocol_version
    if modern:
        headers["MCP-Method"] = method
    if session_id is not None:
        headers["MCP-Session-Id"] = session_id
    payload: dict[str, object] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        payload["id"] = request_id
    if params is not None:
        payload["params"] = params
    status, response_headers, response = request(
        "/mcp",
        method="POST",
        headers=headers,
        body=json.dumps(payload, separators=(",", ":")).encode(),
    )
    if request_id is None:
        if status not in {200, 202, 204} or response.strip():
            raise RuntimeError(f"MCP {method} notification was not accepted")
        return status, response_headers, None
    envelope = decoded_json(response, f"MCP {method}")
    if status != 200 or envelope.get("id") != request_id or "error" in envelope:
        raise RuntimeError(f"MCP {method} failed")
    return status, response_headers, envelope


def _required_model_tool(envelope: dict[str, object], label: str) -> int:
    result = envelope.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list) or not tools:
        raise RuntimeError(f"MCP {label} tools/list is empty")
    names = {
        item.get("name")
        for item in tools
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if PROTEINMPNN_MCP_TOOL not in names:
        raise RuntimeError(f"MCP {label} tools/list lacks the ProteinMPNN tool")
    return len(tools)


def mcp_list_tools(pat: str) -> dict[str, object]:
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {
            "name": "fs2-internal-edge-acceptance",
            "version": "1",
        },
    }
    _, _, discover = _mcp_rpc(
        pat,
        request_id="discover-1",
        method="server/discover",
        params={"_meta": meta},
        protocol_version="2026-07-28",
        modern=True,
    )
    result = discover.get("result") if discover is not None else None
    versions = (
        result.get("supportedVersions") or result.get("supported_versions")
        if isinstance(result, dict)
        else None
    )
    if (
        versions != ["2026-07-28"]
        or result.get("ttlMs") != 0
        or result.get("cacheScope") != "private"
    ):
        raise RuntimeError("MCP discover did not negotiate the reviewed modern version")

    _, initialize_headers, initialized = _mcp_rpc(
        pat,
        request_id="initialize-1",
        method="initialize",
        params={
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {
                "name": "fs2-internal-edge-acceptance",
                "version": "1",
            },
        },
        protocol_version=None,
        modern=False,
    )
    initialize_result = initialized.get("result") if initialized is not None else None
    server_info = (
        initialize_result.get("serverInfo")
        if isinstance(initialize_result, dict)
        else None
    )
    if (
        not isinstance(initialize_result, dict)
        or initialize_result.get("protocolVersion") != "2025-11-25"
        or not isinstance(initialize_result.get("capabilities"), dict)
        or not isinstance(server_info, dict)
        or not isinstance(server_info.get("name"), str)
        or not server_info["name"]
        or not isinstance(server_info.get("version"), str)
        or not server_info["version"]
    ):
        raise RuntimeError("MCP initialize did not negotiate the reviewed legacy version")
    session_id = initialize_headers.get("MCP-Session-Id")
    _mcp_rpc(
        pat,
        request_id=None,
        method="notifications/initialized",
        params=None,
        protocol_version="2025-11-25",
        modern=False,
        session_id=session_id,
    )
    _, _, legacy_listing = _mcp_rpc(
        pat,
        request_id="legacy-tools-1",
        method="tools/list",
        params={},
        protocol_version="2025-11-25",
        modern=False,
        session_id=session_id,
    )
    if legacy_listing is None:
        raise RuntimeError("MCP initialized tools/list lacks a response")
    legacy_tool_count = _required_model_tool(legacy_listing, "initialized")

    _, _, modern_listing = _mcp_rpc(
        pat,
        request_id="modern-tools-1",
        method="tools/list",
        params={"_meta": meta},
        protocol_version="2026-07-28",
        modern=True,
    )
    if modern_listing is None:
        raise RuntimeError("MCP modern tools/list lacks a response")
    modern_result = modern_listing.get("result")
    if (
        not isinstance(modern_result, dict)
        or modern_result.get("ttlMs") != 0
        or modern_result.get("cacheScope") != "private"
    ):
        raise RuntimeError("MCP modern tools/list lacks private zero-TTL cache policy")
    modern_tool_count = _required_model_tool(modern_listing, "modern")
    return {
        "modern_discover_version": "2026-07-28",
        "legacy_initialize_version": "2025-11-25",
        "initialized_notification": True,
        "required_tool": PROTEINMPNN_MCP_TOOL,
        "legacy_tool_count": legacy_tool_count,
        "modern_tool_count": modern_tool_count,
        "private_zero_ttl": True,
    }


def semantic_request(value: object) -> dict[str, object]:
    fields = {"model_id", "operation", "payload", "response_assertions"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(
            "semantic request must contain exactly model_id, operation, payload, "
            "and response_assertions"
        )
    model_id = value.get("model_id")
    operation = value.get("operation")
    payload = value.get("payload")
    assertions = value.get("response_assertions")
    expected_assertions = {
        "kind": PROTEINMPNN_ORACLE,
        "revision": PROTEINMPNN_REVISION,
        "native_length": 76,
        "sequence_count": 1,
    }
    if model_id != "proteinmpnn" or operation != "design-protein":
        raise ValueError("semantic request must select the reviewed small-model oracle")
    if not isinstance(payload, dict) or set(payload) != {
        "input_pdb",
        "num_seq_per_target",
        "random_seed",
    }:
        raise ValueError("ProteinMPNN payload has the wrong shape")
    if (
        not isinstance(payload.get("input_pdb"), str)
        or not payload["input_pdb"].startswith("HEADER")
        or "\nEND" not in payload["input_pdb"]
        or payload.get("num_seq_per_target") != 1
        or isinstance(payload.get("random_seed"), bool)
        or not isinstance(payload.get("random_seed"), int)
        or not 0 <= payload["random_seed"] <= 2**31 - 1
    ):
        raise ValueError("ProteinMPNN payload is outside the reviewed bounds")
    if assertions != expected_assertions:
        raise ValueError("response_assertions do not select the exact reviewed oracle")
    if len(json.dumps(value, separators=(",", ":")).encode()) > 1024 * 1024:
        raise ValueError("semantic request exceeds 1 MiB")
    return value


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RuntimeError(f"{label} is not numeric")
    result = float(value)
    if not math.isfinite(result):
        raise RuntimeError(f"{label} is not finite")
    return result


def validate_proteinmpnn_result(
    value: object,
    spec: dict[str, object],
) -> dict[str, object]:
    """Apply the retained ProteinMPNN open-runtime semantic oracle.

    This mirrors the response invariants in
    ``benchmarks/remaining_models_cold_semantic_validator.py`` while emitting
    only structural, payload-free facts in the operator receipt.
    """

    required = {
        "backend_id",
        "model",
        "output",
        "request_id",
        "revision",
        "schema",
        "timings",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise RuntimeError("semantic response has the wrong open-runtime envelope")
    if (
        value.get("schema") != OPEN_RUNTIME_SCHEMA
        or value.get("revision") != PROTEINMPNN_REVISION
        or not isinstance(value.get("backend_id"), str)
        or not value["backend_id"]
        or not isinstance(value.get("model"), str)
        or not value["model"]
        or not isinstance(value.get("request_id"), str)
        or not value["request_id"]
        or not isinstance(value.get("timings"), dict)
        or not isinstance(value.get("output"), dict)
    ):
        raise RuntimeError("semantic response identity changed")
    model_seconds = _finite(value["timings"].get("model_seconds"), "model_seconds")
    total_seconds = _finite(value["timings"].get("total_seconds"), "total_seconds")
    if model_seconds < 0 or total_seconds < model_seconds:
        raise RuntimeError("semantic response timing order is invalid")

    payload = spec["payload"]
    assertions = spec["response_assertions"]
    if not isinstance(payload, dict) or not isinstance(assertions, dict):
        raise RuntimeError("validated semantic request is unavailable")
    output = value["output"]
    sequences = output.get("sequences")
    native_length = output.get("native_length")
    if (
        native_length != assertions["native_length"]
        or not isinstance(sequences, list)
        or len(sequences) != assertions["sequence_count"]
        or len(sequences) != payload["num_seq_per_target"]
        or not isinstance(sequences[0], dict)
    ):
        raise RuntimeError("ProteinMPNN output has the wrong shape")
    sequence = sequences[0].get("sequence")
    if not isinstance(sequence, str):
        raise RuntimeError("ProteinMPNN sequence is missing")
    normalized = sequence.replace("/", "")
    if (
        len(normalized) != native_length
        or not normalized.isascii()
        or set(normalized) - PROTEINMPNN_AMINO_ACIDS
    ):
        raise RuntimeError("ProteinMPNN sequence failed length/alphabet validation")
    _finite(sequences[0].get("score"), "ProteinMPNN score")
    _finite(sequences[0].get("global_score"), "ProteinMPNN global score")
    return {
        "oracle": assertions["kind"],
        "envelope_schema": OPEN_RUNTIME_SCHEMA,
        "request_bound": True,
        "native_length": native_length,
        "sequence_count": len(sequences),
        "canonical_sequence": True,
        "finite_scores": True,
    }


def run_semantic(
    pat: str,
    spec: dict[str, object],
    deadline: float,
) -> dict[str, object]:
    model_id = spec["model_id"]
    body = json.dumps(
        {"operation": spec["operation"], "payload": spec["payload"]},
        separators=(",", ":"),
    ).encode()
    auth = {
        "Authorization": f"Bearer {pat}",
        "Origin": APPLICATION_ORIGIN,
    }
    remaining_seconds = int(deadline - time.monotonic())
    if remaining_seconds < 1:
        raise TimeoutError("semantic invocation exceeded the bounded acceptance timeout")
    status, headers, response = request(
        f"/v1/models/{model_id}:invoke",
        method="POST",
        headers={
            **auth,
            "Content-Type": "application/json",
            "Idempotency-Key": f"fs2-internal-edge-{uuid4()}",
            "x-fs2-wait-seconds": "0",
            "x-fs2-deadline-seconds": str(remaining_seconds),
        },
        body=body,
    )
    value = decoded_json(response, "semantic invocation")
    if status == 200:
        return validate_proteinmpnn_result(value, spec)
    if status != 202:
        raise RuntimeError(f"semantic invocation returned HTTP {status}")
    operation_id = headers.get("X-Fs2-Operation-Id") or value.get("id")
    if not isinstance(operation_id, str):
        raise RuntimeError("semantic invocation lacks an operation ID")
    while time.monotonic() < deadline:
        poll_status, _, poll_payload = request(
            f"/v1/operations/{operation_id}", headers=auth
        )
        current = decoded_json(poll_payload, "semantic operation")
        state = current.get("status")
        if poll_status != 200 or not isinstance(state, str):
            raise RuntimeError("semantic operation status is invalid")
        if state == "succeeded":
            if (
                current.get("id") != operation_id
                or current.get("model_id") != model_id
                or current.get("operation") != spec["operation"]
                or current.get("semantic_outcome") != "protocol_valid"
            ):
                raise RuntimeError("semantic operation terminal identity changed")
            result_status, _, result_payload = request(
                f"/v1/operations/{operation_id}/result", headers=auth
            )
            result = decoded_json(result_payload, "semantic result")
            if result_status != 200:
                raise RuntimeError("semantic result retrieval failed")
            return validate_proteinmpnn_result(result, spec)
        if state in {"failed", "cancelled", "preempted", "expired"}:
            raise RuntimeError("semantic operation did not succeed")
        time.sleep(1)
    raise TimeoutError("semantic operation exceeded the bounded acceptance timeout")


def accept(
    admin_token: str,
    semantic: dict[str, object],
    deadline: float,
) -> dict[str, object]:
    status, _, _ = request("/healthz")
    if status != 200:
        raise RuntimeError("admin-console health failed")

    status, headers, page = request("/admin/")
    if status != 200 or "text/html" not in headers.get("Content-Type", "").lower():
        raise RuntimeError("admin static route did not return HTML")
    if b"<html" not in page.lower() and b"<!doctype html" not in page.lower():
        raise RuntimeError("admin static route returned an unexpected document")

    status, _, _ = request("/readyz")
    if status != 200:
        raise RuntimeError("control-plane readiness failed")

    model_id = semantic["model_id"]
    if not isinstance(model_id, str):
        raise RuntimeError("validated semantic model ID is unavailable")
    pat, token_id = issue_acceptance_pat(admin_token, model_id)
    try:
        status, _, models_payload = request(
            "/v1/models",
            headers={
                "Authorization": f"Bearer {pat}",
                "Origin": APPLICATION_ORIGIN,
            },
        )
        models = decoded_json(models_payload, "model catalog").get("data")
        if (
            status != 200
            or not isinstance(models, list)
            or not any(
                isinstance(item, dict) and item.get("id") == model_id
                for item in models
            )
        ):
            raise RuntimeError("authenticated model catalog lacks the acceptance model")
        mcp_result = mcp_list_tools(pat)
        semantic_result = run_semantic(pat, semantic, deadline)

        session_headers = {
            "Authorization": f"Bearer {admin_token}",
            "Origin": APPLICATION_ORIGIN,
        }
        status, headers, _ = request(
            "/admin/api/v1/session",
            method="POST",
            headers=session_headers,
            body=b"",
        )
        set_cookie = headers.get("Set-Cookie", "")
        cookie = set_cookie.split(";", 1)[0]
        if status != 200 or not cookie.startswith("__Host-fs2_admin_session="):
            raise RuntimeError("admin API did not issue the reviewed operator session")

        status, _, overview = request(
            "/admin/api/v1/overview",
            headers={"Cookie": cookie, "Origin": APPLICATION_ORIGIN},
        )
        if status != 200 or not isinstance(json.loads(overview), dict):
            raise RuntimeError("admin overview acceptance failed")
    finally:
        revoke_acceptance_pat(admin_token, token_id)
    return {
        "schema": "fs2-serve.nebius.ai/internal-edge-acceptance/v1",
        "status": "PASS",
        "origin": APPLICATION_ORIGIN,
        "admin_static": True,
        "admin_api": True,
        "admin_health": True,
        "control_ready": True,
        "models": len(models),
        "mcp": mcp_result,
        "acceptance_pat_revoked": True,
        "semantic_inference": {
            "model_id": semantic["model_id"],
            "operation": semantic["operation"],
            **semantic_result,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--admin-token-file", type=Path, required=True)
    parser.add_argument("--semantic-request-file", type=Path, required=True)
    parser.add_argument(
        "--control-plane-local-port",
        type=int,
        default=DEFAULT_CONTROL_PORT,
        help="port_forward_contract control_plane_local_port",
    )
    parser.add_argument(
        "--admin-console-local-port",
        type=int,
        default=DEFAULT_ADMIN_PORT,
        help="port_forward_contract admin_console_local_port",
    )
    parser.add_argument(
        "--operator-proxy-port",
        type=int,
        default=DEFAULT_PROXY_PORT,
        help="port_forward_contract operator_proxy_port",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=ACCEPTANCE_TIMEOUT_SECONDS,
    )
    args = parser.parse_args()
    if re.fullmatch(r"fs2-disposable-[a-z][a-z0-9]{5,11}", args.context) is None:
        raise ValueError("context must be the exact run-scoped disposable context")
    if not 300 <= args.timeout_seconds <= ACCEPTANCE_TIMEOUT_SECONDS:
        raise ValueError("timeout-seconds must be from 300 through 7200")
    configure_local_ports(
        args.control_plane_local_port,
        args.admin_console_local_port,
        args.operator_proxy_port,
    )
    checked_private_file(args.kubeconfig, "kubeconfig")
    admin_token = checked_private_file(args.admin_token_file, "admin token file")
    semantic = semantic_request(
        json.loads(
            checked_private_file(
                args.semantic_request_file,
                "semantic request file",
                max_bytes=PROTEINMPNN_SEMANTIC_REQUEST_MAX_BYTES,
            )
        )
    )

    processes: list[subprocess.Popen[bytes]] = []
    server: ThreadingHTTPServer | None = None
    server_thread: threading.Thread | None = None
    deadline = time.monotonic() + args.timeout_seconds
    try:
        for service, port in (
            (CONTROL_SERVICE, CONTROL_PORT),
            (ADMIN_SERVICE, ADMIN_PORT),
        ):
            processes.append(
                subprocess.Popen(  # noqa: S603
                    port_forward_command(args.kubeconfig.resolve(), args.context, service, port),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={**os.environ, "KUBECONFIG": str(args.kubeconfig.resolve())},
                )
            )
        wait_for_port(processes, CONTROL_PORT, deadline)
        wait_for_port(processes, ADMIN_PORT, deadline)
        server = ThreadingHTTPServer((BIND_ADDRESS, PROXY_PORT), SameOriginProxy)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        print(json.dumps(accept(admin_token, semantic, deadline), sort_keys=True))
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5)
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()

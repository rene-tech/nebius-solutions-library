"""Pinned client for the provider-native effective-authority snapshot API.

The installed artifact is measured by the signed custody receipt and must be
root-owned and non-writable.  It performs two-way TLS directly to the provider
authority API, pins the live server leaf, supplies a nonce, and emits only the
provider observation envelope consumed by ``inference-stack``.  Credentials,
raw certificates and response headers are never written to stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import secrets
import ssl
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_TRANSACTION = re.compile(r"^[a-z][a-z0-9]{5,31}:[1-9][0-9]*:[a-f0-9]{32}$")


class ProviderAuthorityExportError(RuntimeError):
    """The provider-native authority observation failed closed."""


def _private_file(environment_name: str) -> Path:
    path = Path(os.environ.get(environment_name, ""))
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_mode & 0o077
    ):
        raise ProviderAuthorityExportError(
            f"{environment_name} must be an absolute mode-0600 regular file"
        )
    return path


def _provider_endpoint() -> str:
    value = os.environ.get("FS2_PROVIDER_AUTHORITY_API_URL", "").rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderAuthorityExportError(
            "FS2_PROVIDER_AUTHORITY_API_URL must be an origin-only HTTPS URL"
        )
    return value


def _tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context(
        cafile=str(_private_file("FS2_PROVIDER_AUTHORITY_API_CA"))
    )
    context.load_cert_chain(
        str(_private_file("FS2_PROVIDER_AUTHORITY_API_CLIENT_CERT")),
        str(_private_file("FS2_PROVIDER_AUTHORITY_API_CLIENT_KEY")),
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _pinned_connection(
    origin: str, context: ssl.SSLContext
) -> tuple[http.client.HTTPSConnection, str]:
    parsed = urlsplit(origin)
    expected = os.environ.get("FS2_PROVIDER_AUTHORITY_API_SERVER_CERT_SHA256", "")
    if _SHA256.fullmatch(expected) is None:
        raise ProviderAuthorityExportError(
            "provider authority server certificate digest is not pinned"
        )
    try:
        connection = http.client.HTTPSConnection(
            str(parsed.hostname), parsed.port or 443, timeout=30, context=context
        )
        connection.connect()
        certificate = (
            connection.sock.getpeercert(binary_form=True)
            if connection.sock is not None
            else None
        )
    except (OSError, http.client.HTTPException) as exc:
        raise ProviderAuthorityExportError(
            "provider authority TLS endpoint is unavailable"
        ) from exc
    if not certificate or hashlib.sha256(certificate).hexdigest() != expected:
        connection.close()
        raise ProviderAuthorityExportError(
            "provider authority TLS leaf differs from the root-enrolled digest"
        )
    return connection, expected


def _snapshot(
    *, project_id: str, cluster_id: str, freeze_transaction_id: str
) -> dict[str, Any]:
    if re.fullmatch(r"project-[a-z0-9]+", project_id) is None:
        raise ProviderAuthorityExportError("project ID is malformed")
    if re.fullmatch(r"mk8scluster-[a-z0-9]+", cluster_id) is None:
        raise ProviderAuthorityExportError("cluster ID is malformed")
    if _TRANSACTION.fullmatch(freeze_transaction_id) is None:
        raise ProviderAuthorityExportError("freeze transaction ID is malformed")
    origin = _provider_endpoint()
    context = _tls_context()
    connection, server_certificate_sha256 = _pinned_connection(origin, context)
    challenge = secrets.token_hex(32)
    try:
        connection.request(
            "POST",
            "/v1/effective-authority/snapshot",
            body=json.dumps(
                {
                    "schema": "fs2-serve.nebius.ai/provider-control-plane-authority-request/v1",
                    "challenge": challenge,
                    "project_id": project_id,
                    "cluster_id": cluster_id,
                    "freeze_transaction_id": freeze_transaction_id,
                    "required_projection": "effective-iam-network-runtime-and-freeze",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        content = response.read(16 * 1024 * 1024 + 1)
        status = response.status
        content_type = response.getheader("content-type", "").split(";", 1)[0]
    except (OSError, http.client.HTTPException) as exc:
        raise ProviderAuthorityExportError(
            "provider authority API request failed closed"
        ) from exc
    finally:
        connection.close()
    if (
        status != 200
        or content_type != "application/json"
        or len(content) > 16 * 1024 * 1024
    ):
        raise ProviderAuthorityExportError(
            "provider authority API response failed closed"
        )
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderAuthorityExportError(
            "provider authority API response is not JSON"
        ) from exc
    if (
        not isinstance(document, dict)
        or set(document) != {
            "schema",
            "challenge",
            "observation",
            "provider_response_sha256",
        }
        or document.get("schema")
        != "nebius.ai/effective-control-plane-authority-response/v1"
        or document.get("challenge") != challenge
        or not isinstance(document.get("observation"), dict)
        or _SHA256.fullmatch(str(document.get("provider_response_sha256", "")))
        is None
    ):
        raise ProviderAuthorityExportError(
            "provider authority API response identity is incomplete"
        )
    observation = dict(document["observation"])
    snapshot = observation.get("snapshot")
    provider_freeze = (
        snapshot.get("provider_mutation_freeze")
        if isinstance(snapshot, Mapping)
        else None
    )
    completeness_token = (
        snapshot.get("completeness_token")
        if isinstance(snapshot, Mapping)
        else None
    )
    if (
        observation.get("schema")
        != "fs2-serve.nebius.ai/provider-control-plane-authority-observation/v1"
        or not isinstance(snapshot, dict)
        or snapshot.get("project_id") != project_id
        or snapshot.get("cluster_id") != cluster_id
        or str(snapshot.get("provider_api_endpoint", "")).rstrip("/") != origin
        or snapshot.get("authority_api_server_certificate_sha256")
        != server_certificate_sha256
        or not isinstance(provider_freeze, Mapping)
        or provider_freeze.get("transaction_id")
        != freeze_transaction_id
        or not isinstance(completeness_token, Mapping)
        or completeness_token.get("provider_response_sha256")
        != document["provider_response_sha256"]
    ):
        raise ProviderAuthorityExportError(
            "provider authority observation is outside the requested boundary"
        )
    return observation


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--project-id", required=True)
    snapshot.add_argument("--cluster-id", required=True)
    snapshot.add_argument("--freeze-transaction-id", required=True)
    snapshot.add_argument("--output", choices=["json"], required=True)
    arguments = parser.parse_args()
    if arguments.command != "snapshot":
        raise ProviderAuthorityExportError("unsupported exporter command")
    observation = _snapshot(
        project_id=arguments.project_id,
        cluster_id=arguments.cluster_id,
        freeze_transaction_id=arguments.freeze_transaction_id,
    )
    json.dump(observation, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

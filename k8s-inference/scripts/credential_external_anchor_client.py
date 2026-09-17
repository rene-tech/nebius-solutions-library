#!/usr/bin/env python3
"""mTLS client for an independent append-only credential evidence anchor."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import ssl
import stat
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

CONFIG_PATH = Path("/etc/fs2-credential-authority/external-anchor-client.json")
MAX_RESPONSE_BYTES = 1024 * 1024
HEX64 = re.compile(r"^[0-9a-f]{64}$")
READ_ONLY_OPERATIONS = frozenset(
    {
        "custody-snapshot",
        "planned-generation-admission",
        "artifact-inventory",
        "consumer-readiness",
        "credential-inventory",
        "rotation-readiness",
        "viewer-handoff-inventory",
        "ciphertext-migration",
        "authentication-continuity",
        "release-identity",
        "backend-custody",
    }
)


class AnchorClientError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def config() -> dict[str, Any]:
    if CONFIG_PATH.is_symlink() or not CONFIG_PATH.is_file():
        raise AnchorClientError("external anchor configuration is absent")
    metadata = CONFIG_PATH.stat()
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise AnchorClientError("external anchor configuration must be root-owned mode 0600")
    document = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    required = {
        "schema",
        "url",
        "ca_file",
        "client_certificate_file",
        "client_private_key_file",
        "file_sha256",
    }
    parsed = urlsplit(document.get("url", "")) if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or set(document) != required
        or document.get("schema")
        != "fs2-serve.nebius.ai/external-anchor-client/v1"
        or parsed is None
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/v1/credential-evidence"
        or not isinstance(document.get("file_sha256"), dict)
    ):
        raise AnchorClientError("external anchor configuration is malformed")
    paths = [
        Path(document[field])
        for field in ("ca_file", "client_certificate_file", "client_private_key_file")
    ]
    if set(document["file_sha256"]) != {str(path) for path in paths}:
        raise AnchorClientError("external anchor TLS files are not all pinned")
    for path in paths:
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise AnchorClientError("external anchor TLS file is absent or unsafe")
        file_metadata = path.stat()
        if file_metadata.st_uid != 0 or stat.S_IMODE(file_metadata.st_mode) & 0o022:
            raise AnchorClientError("external anchor TLS file is writable")
        if file_sha256(path) != document["file_sha256"][str(path)]:
            raise AnchorClientError("external anchor TLS file differs from its pin")
    return document


def main() -> int:
    request = json.loads(sys.stdin.read())
    if (
        not isinstance(request, dict)
        or request.get("schema")
        != "fs2-serve.nebius.ai/external-evidence-anchor-request/v1"
        or request.get("action") not in {"reserve", "commit"}
    ):
        raise AnchorClientError("external anchor request is malformed")
    if request["action"] == "reserve":
        required = {
            "schema",
            "action",
            "evidence_id",
            "operation",
            "request_sha256",
            "payload_sha256",
            "policy_sha256",
        }
        if (
            set(request) != required
            or request.get("operation") not in READ_ONLY_OPERATIONS
            or any(
                HEX64.fullmatch(str(request.get(field, ""))) is None
                for field in ("request_sha256", "payload_sha256", "policy_sha256")
            )
        ):
            raise AnchorClientError("external anchor reservation is malformed")
        try:
            uuid.UUID(str(request.get("evidence_id")))
        except (ValueError, TypeError, AttributeError) as error:
            raise AnchorClientError("external anchor evidence ID is invalid") from error
    else:
        reservation = request.get("reservation")
        record = request.get("record")
        if (
            set(request) != {"schema", "action", "reservation", "record"}
            or not isinstance(reservation, dict)
            or set(reservation)
            != {
                "schema",
                "reservation_id",
                "log_id",
                "endpoint",
                "entry_index",
                "sequence",
                "prior_checkpoint_sha256",
                "expires_at",
            }
            or reservation.get("schema")
            != "fs2-serve.nebius.ai/external-evidence-reservation/v2"
            or not isinstance(record, dict)
            or set(record) != {"schema", "claim_sha256", "producer_signature"}
            or record.get("schema")
            != "fs2-serve.nebius.ai/credential-evidence-record/v1"
            or HEX64.fullmatch(str(record.get("claim_sha256", ""))) is None
            or not isinstance(record.get("producer_signature"), str)
            or not record["producer_signature"]
        ):
            raise AnchorClientError("external anchor commit is malformed")
    settings = config()
    parsed = urlsplit(settings["url"])
    context = ssl.create_default_context(cafile=settings["ca_file"])
    context.load_cert_chain(
        settings["client_certificate_file"], settings["client_private_key_file"]
    )
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    connection = http.client.HTTPSConnection(
        parsed.hostname, parsed.port or 443, context=context, timeout=30
    )
    try:
        connection.request(
            "POST",
            parsed.path,
            body=encoded,
            headers={"Content-Type": "application/json", "Content-Length": str(len(encoded))},
        )
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    finally:
        connection.close()
    if response.status != 200 or len(raw) > MAX_RESPONSE_BYTES:
        raise AnchorClientError("external anchor rejected the evidence request")
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise AnchorClientError("external anchor response is malformed")
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AnchorClientError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error

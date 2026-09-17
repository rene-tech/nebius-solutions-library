#!/usr/bin/env python3
"""Collect Secret identities through Kubernetes PartialObjectMetadata only.

The bearer token is read from an inherited descriptor, never argv or an
environment variable.  The caller must obtain it with a TokenRequest bound to
the immutable external-custody anchor Secret.  The API request advertises only
PartialObjectMetadataList; a full Secret or any payload-bearing field fails
closed before an artifact is emitted.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import ssl
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCHEMA = "fs2-serve.nebius.ai/sai07-secret-metadata/v1"
MEDIA_TYPE = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
AUDIENCE = "https://kubernetes.default.svc"
ANCHOR_NAMESPACE = "fs2-system"
ANCHOR_NAME = "fs2-pod-security-token-anchor"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class MetadataError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def b64url_json(segment: str, label: str) -> dict[str, Any]:
    try:
        payload = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        value = json.loads(payload)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MetadataError(f"TokenRequest {label} is malformed") from error
    if not isinstance(value, dict):
        raise MetadataError(f"TokenRequest {label} is not an object")
    return value


def read_token(descriptor: int) -> str:
    try:
        before = os.fstat(descriptor)
    except OSError as error:
        raise MetadataError("token descriptor is unavailable") from error
    if not stat.S_ISREG(before.st_mode) or before.st_size < 32 or before.st_size > 32768:
        raise MetadataError("token descriptor is not a bounded regular descriptor")
    os.lseek(descriptor, 0, os.SEEK_SET)
    token = os.read(descriptor, before.st_size + 1)
    after = os.fstat(descriptor)
    if len(token) != before.st_size or (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
    ):
        raise MetadataError("token changed during its descriptor-fenced read")
    try:
        value = token.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise MetadataError("token is not ASCII") from error
    if len(value.split(".")) != 3:
        raise MetadataError("token is not a compact JWT")
    return value


def validate_token(
    token: str,
    *,
    service_account_namespace: str,
    service_account_name: str,
    service_account_uid: str,
    anchor_uid: str,
) -> str:
    header_segment, claims_segment, _signature = token.split(".")
    header = b64url_json(header_segment, "header")
    claims = b64url_json(claims_segment, "claims")
    if header.get("alg") in {None, "none"}:
        raise MetadataError("TokenRequest JWT has no signing algorithm")
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience
    now = int(dt.datetime.now(dt.UTC).timestamp())
    issued_at = claims.get("iat")
    expires_at = claims.get("exp")
    if (
        audiences != [AUDIENCE]
        or not isinstance(issued_at, int)
        or isinstance(issued_at, bool)
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or issued_at > now + 30
        or expires_at <= now
        or expires_at - issued_at > 600
    ):
        raise MetadataError("TokenRequest audience or lifetime differs from the ten-minute contract")
    kubernetes = claims.get("kubernetes.io")
    if not isinstance(kubernetes, dict):
        raise MetadataError("TokenRequest lacks Kubernetes bound claims")
    service_account = kubernetes.get("serviceaccount")
    secret = kubernetes.get("secret")
    if (
        kubernetes.get("namespace") != service_account_namespace
        or service_account != {"name": service_account_name, "uid": service_account_uid}
        or secret != {"name": ANCHOR_NAME, "uid": anchor_uid}
        or claims.get("sub")
        != f"system:serviceaccount:{service_account_namespace}:{service_account_name}"
    ):
        raise MetadataError("TokenRequest is not bound to the exact reader and anchor Secret")
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti:
        raise MetadataError("TokenRequest JWT has no JTI")
    return hashlib.sha256(jti.encode()).hexdigest()


def read_ca(path: Path) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise MetadataError("CA path must be absolute without parent traversal")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024:
            raise MetadataError("CA is not a bounded regular file")
        payload = os.read(descriptor, before.st_size + 1)
        if len(payload) != before.st_size:
            raise MetadataError("CA changed during its descriptor-fenced read")
        return payload
    finally:
        os.close(descriptor)


def request_metadata(api_server: str, ca_bytes: bytes, token: str, namespace: str) -> dict[str, Any]:
    if not api_server.startswith("https://") or urllib.parse.urlsplit(api_server).query:
        raise MetadataError("API server must be an HTTPS origin without a query")
    context = ssl.create_default_context(cadata=ca_bytes.decode("ascii"))
    base_url = api_server.rstrip("/") + f"/api/v1/namespaces/{urllib.parse.quote(namespace, safe='')}/secrets"
    items: list[object] = []
    resource_version: str | None = None
    continuation = ""
    for _page in range(100):
        query = {"limit": "500", **({"continue": continuation} if continuation else {})}
        request = urllib.request.Request(
            base_url + "?" + urllib.parse.urlencode(query),
            headers={"Accept": MEDIA_TYPE, "Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, context=context, timeout=15) as response:
                content_type = response.headers.get_content_type()
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.URLError as error:
            raise MetadataError("metadata-only Kubernetes request failed") from error
        if content_type != "application/json" or len(payload) > MAX_RESPONSE_BYTES:
            raise MetadataError("metadata-only response type or size is invalid")
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MetadataError("metadata-only response is not JSON") from error
        if (
            not isinstance(value, dict)
            or value.get("apiVersion") != "meta.k8s.io/v1"
            or value.get("kind") != "PartialObjectMetadataList"
            or not isinstance(value.get("metadata"), dict)
            or not isinstance(value.get("items"), list)
        ):
            raise MetadataError("API did not honor PartialObjectMetadataList content negotiation")
        page_resource_version = value["metadata"].get("resourceVersion")
        if not isinstance(page_resource_version, str) or not page_resource_version:
            raise MetadataError("metadata-only page lacks resourceVersion")
        if resource_version is None:
            resource_version = page_resource_version
        elif resource_version != page_resource_version:
            raise MetadataError("metadata-only pagination changed resourceVersion")
        items.extend(value["items"])
        continuation = value["metadata"].get("continue", "")
        if not isinstance(continuation, str):
            raise MetadataError("metadata-only continuation token is malformed")
        if not continuation:
            return {
                "apiVersion": "meta.k8s.io/v1",
                "kind": "PartialObjectMetadataList",
                "metadata": {"resourceVersion": resource_version},
                "items": items,
            }
    raise MetadataError("metadata-only inventory exceeded the 100-page bound")


def normalize(collection: dict[str, Any], namespace: str, service_accounts: set[str]) -> dict[str, Any]:
    if collection.get("apiVersion") != "meta.k8s.io/v1" or collection.get("kind") != "PartialObjectMetadataList":
        raise MetadataError("API did not honor PartialObjectMetadataList content negotiation")
    if set(collection) - {"apiVersion", "kind", "metadata", "items"}:
        raise MetadataError("metadata-only response contains unexpected top-level fields")
    metadata = collection.get("metadata")
    items = collection.get("items")
    if not isinstance(metadata, dict) or not isinstance(items, list):
        raise MetadataError("metadata-only collection structure is invalid")
    resource_version = metadata.get("resourceVersion")
    if not isinstance(resource_version, str) or not resource_version:
        raise MetadataError("metadata-only collection lacks resourceVersion")
    normalized: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"apiVersion", "kind", "metadata"}:
            raise MetadataError("Secret response contains non-metadata fields")
        if item["apiVersion"] != "v1" or item["kind"] != "Secret" or not isinstance(item["metadata"], dict):
            raise MetadataError("Secret metadata item has an unexpected identity")
        item_metadata = item["metadata"]
        annotations = item_metadata.get("annotations") or {}
        if not isinstance(annotations, dict):
            raise MetadataError("Secret metadata annotations are malformed")
        service_account_name = annotations.get("kubernetes.io/service-account.name")
        if service_account_name not in service_accounts:
            continue
        identity = {
            "namespace": str(item_metadata.get("namespace", "")),
            "name": str(item_metadata.get("name", "")),
            "uid": str(item_metadata.get("uid", "")),
            "resource_version": str(item_metadata.get("resourceVersion", "")),
            "service_account_name": str(service_account_name),
        }
        if identity["namespace"] != namespace or not all(identity.values()):
            raise MetadataError("Secret metadata identity is incomplete")
        normalized.append(identity)
    normalized.sort(key=lambda item: (item["service_account_name"], item["name"]))
    return {
        "schema": SCHEMA,
        "media_type": MEDIA_TYPE,
        "namespace": namespace,
        "collection_resource_version": resource_version,
        "items": normalized,
        "items_sha256": hashlib.sha256(canonical(normalized)).hexdigest(),
        "item_count": len(normalized),
        "contains_secret_payload": False,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--api-server", required=True)
    result.add_argument("--ca-file", type=Path, required=True)
    result.add_argument("--token-fd", type=int, required=True)
    result.add_argument("--namespace", default="fs2-models")
    result.add_argument("--reader-service-account-namespace", default="fs2-system")
    result.add_argument("--reader-service-account-name", default="fs2-pod-security-metadata-reader")
    result.add_argument("--reader-service-account-uid", required=True)
    result.add_argument("--anchor-uid", required=True)
    result.add_argument("--service-account", action="append", default=[])
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.namespace != "fs2-models" or not args.service_account:
            raise MetadataError("the exact fs2-models namespace and a non-empty ServiceAccount set are required")
        token = read_token(args.token_fd)
        jti_sha256 = validate_token(
            token,
            service_account_namespace=args.reader_service_account_namespace,
            service_account_name=args.reader_service_account_name,
            service_account_uid=args.reader_service_account_uid,
            anchor_uid=args.anchor_uid,
        )
        collection = request_metadata(args.api_server, read_ca(args.ca_file), token, args.namespace)
        artifact = normalize(collection, args.namespace, set(args.service_account))
        artifact["reader_service_account_uid"] = args.reader_service_account_uid
        artifact["token_bound_object_ref"] = {
            "api_version": "v1",
            "kind": "Secret",
            "namespace": ANCHOR_NAMESPACE,
            "name": ANCHOR_NAME,
            "uid": args.anchor_uid,
        }
        artifact["token_jti_sha256"] = jti_sha256
        artifact["observed_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (MetadataError, OSError, UnicodeDecodeError) as error:
        print(f"SAI-07 metadata-only inventory rejected: {error}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical(artifact))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

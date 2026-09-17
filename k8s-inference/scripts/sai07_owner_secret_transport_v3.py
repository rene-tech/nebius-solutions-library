#!/usr/bin/env python3
"""Metadata-only Secret bootstrap transport for the SAI-07 v3 executor.

The external owner token is accepted only through an inherited descriptor and
must be a ten-minute, Kubernetes-API-audience JWT for the repository-pinned
owner username.  Secret reads always request PartialObjectMetadataList.  The
only Secret write is an atomic typed POST of the exact empty immutable anchor,
and its response must be PartialObjectMetadata.  A full Secret response is
rejected before it can be decoded by this process.

This module is source only while the v3 trust lock is blocked.  It performs no
request merely by being imported.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from collect_sai07_secret_metadata import (
    ANCHOR_NAME,
    ANCHOR_NAMESPACE,
    AUDIENCE,
    MAX_RESPONSE_BYTES,
    MetadataError,
    b64url_json,
    canonical,
    read_ca,
    read_token,
)

METADATA_LIST_MEDIA_TYPE = (
    "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
)
METADATA_MEDIA_TYPE = "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"


class OwnerTransportError(ValueError):
    pass


def validate_owner_token(token: str, expected_username: str) -> tuple[str, str]:
    """Validate bounded claims; live SelfSubjectReview authenticates the JWT."""

    try:
        header_segment, claims_segment, _signature = token.split(".")
    except ValueError as error:
        raise OwnerTransportError("external owner token is not a compact JWT") from error
    header = b64url_json(header_segment, "external owner header")
    claims = b64url_json(claims_segment, "external owner claims")
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience
    issued_at = claims.get("iat")
    expires_at = claims.get("exp")
    now = int(dt.datetime.now(dt.UTC).timestamp())
    if (
        header.get("alg") in {None, "none"}
        or audiences != [AUDIENCE]
        or not isinstance(issued_at, int)
        or isinstance(issued_at, bool)
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or issued_at > now + 30
        or expires_at <= now
        or expires_at - issued_at > 600
        or claims.get("sub") != expected_username
    ):
        raise OwnerTransportError(
            "external owner JWT identity, audience, algorithm, or lifetime differs"
        )
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti:
        raise OwnerTransportError("external owner JWT omits its one-time identifier")
    return hashlib.sha256(jti.encode()).hexdigest(), jti


class OwnerApi:
    def __init__(self, api_server: str, ca_file: Path, token_fd: int) -> None:
        parsed = urllib.parse.urlsplit(api_server)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise OwnerTransportError("owner API server must be an HTTPS origin")
        if token_fd < 3:
            raise OwnerTransportError(
                "external owner token must use a dedicated inherited descriptor"
            )
        try:
            ca_text = read_ca(ca_file).decode("ascii")
            self.token = read_token(token_fd)
        except (MetadataError, UnicodeDecodeError) as error:
            raise OwnerTransportError(str(error)) from error
        self.origin = api_server.rstrip("/")
        self.context = ssl.create_default_context(cadata=ca_text)

    def request(
        self,
        path: str,
        *,
        method: str,
        body: dict[str, Any] | None = None,
        accept: str = "application/json",
        allow_conflict: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        if not path.startswith("/") or ".." in urllib.parse.urlsplit(path).path.split("/"):
            raise OwnerTransportError("Kubernetes API path is malformed")
        headers = {"Accept": accept, "Authorization": f"Bearer {self.token}"}
        payload = None
        if body is not None:
            payload = canonical(body)
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.origin + path,
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=30) as response:
                status = response.status
                content_type = response.headers.get("Content-Type", "")
                response_bytes = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            if allow_conflict and error.code == 409:
                return 409, {}
            raise OwnerTransportError(
                f"Kubernetes {method} request failed with HTTP {error.code}"
            ) from error
        except urllib.error.URLError as error:
            raise OwnerTransportError("Kubernetes owner request failed") from error
        if len(response_bytes) > MAX_RESPONSE_BYTES:
            raise OwnerTransportError("Kubernetes owner response exceeds the bounded limit")
        if accept in {METADATA_LIST_MEDIA_TYPE, METADATA_MEDIA_TYPE}:
            expected = "as=PartialObjectMetadata"
            if accept == METADATA_LIST_MEDIA_TYPE:
                expected = "as=PartialObjectMetadataList"
            if expected.lower() not in content_type.lower():
                raise OwnerTransportError(
                    "Kubernetes did not honor metadata-only content negotiation"
                )
        elif not content_type.lower().startswith("application/json"):
            raise OwnerTransportError("Kubernetes owner response is not JSON")
        try:
            value = json.loads(response_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OwnerTransportError("Kubernetes owner response is invalid JSON") from error
        if not isinstance(value, dict):
            raise OwnerTransportError("Kubernetes owner response is not an object")
        return status, value

    def self_subject_review(self) -> dict[str, Any]:
        _, result = self.request(
            "/apis/authentication.k8s.io/v1beta1/selfsubjectreviews",
            method="POST",
            body={
                "apiVersion": "authentication.k8s.io/v1beta1",
                "kind": "SelfSubjectReview",
                "spec": {},
            },
        )
        return result

    def anchor_metadata(self) -> dict[str, str] | None:
        query = urllib.parse.urlencode(
            {"fieldSelector": f"metadata.name={ANCHOR_NAME}", "limit": "2"}
        )
        _, collection = self.request(
            f"/api/v1/namespaces/{ANCHOR_NAMESPACE}/secrets?{query}",
            method="GET",
            accept=METADATA_LIST_MEDIA_TYPE,
        )
        if (
            set(collection) != {"apiVersion", "items", "kind", "metadata"}
            or collection.get("apiVersion") != "meta.k8s.io/v1"
            or collection.get("kind") != "PartialObjectMetadataList"
            or not isinstance(collection.get("metadata"), dict)
            or not isinstance(collection.get("items"), list)
        ):
            raise OwnerTransportError("anchor inventory is not metadata-only")
        if collection["metadata"].get("continue") not in (None, ""):
            raise OwnerTransportError("exact-name anchor inventory unexpectedly paginated")
        selected = []
        for item in collection["items"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"apiVersion", "kind", "metadata"}
                or item.get("apiVersion") != "v1"
                or item.get("kind") != "Secret"
                or not isinstance(item.get("metadata"), dict)
            ):
                raise OwnerTransportError("anchor response contains Secret payload fields")
            metadata = item["metadata"]
            if metadata.get("name") == ANCHOR_NAME:
                selected.append(metadata)
        if len(selected) > 1:
            raise OwnerTransportError("token anchor identity is duplicated")
        if not selected:
            return None
        return self._anchor_identity(selected[0])

    @staticmethod
    def _anchor_identity(metadata: dict[str, Any]) -> dict[str, str]:
        labels = metadata.get("labels")
        annotations = metadata.get("annotations")
        expected_labels = {
            "security.fs2.nebius.ai/custody-owner": "external",
            "security.fs2.nebius.ai/role": "token-anchor",
        }
        required_annotation = "security.fs2.nebius.ai/custody-epoch-sha256"
        if (
            metadata.get("namespace") != ANCHOR_NAMESPACE
            or metadata.get("name") != ANCHOR_NAME
            or labels != expected_labels
            or not isinstance(annotations, dict)
            or set(annotations) != {required_annotation}
            or metadata.get("ownerReferences") not in (None, [])
            or metadata.get("finalizers") not in (None, [])
        ):
            raise OwnerTransportError("token anchor metadata differs from its exact contract")
        uid = metadata.get("uid")
        resource_version = metadata.get("resourceVersion")
        epoch_sha256 = annotations.get(required_annotation)
        if not all(isinstance(value, str) and value for value in (uid, resource_version)):
            raise OwnerTransportError("token anchor metadata identity is incomplete")
        if (
            not isinstance(epoch_sha256, str)
            or len(epoch_sha256) != 64
            or any(character not in "0123456789abcdef" for character in epoch_sha256)
        ):
            raise OwnerTransportError("token anchor epoch digest is malformed")
        return {
            "custody_epoch_sha256": epoch_sha256,
            "resource_version": resource_version,
            "uid": uid,
        }

    def ensure_empty_immutable_anchor(self, custody_epoch_sha256: str) -> dict[str, str]:
        existing = self.anchor_metadata()
        if existing is not None:
            if existing["custody_epoch_sha256"] != custody_epoch_sha256:
                raise OwnerTransportError("existing token anchor belongs to another custody epoch")
            return {**existing, "created": "false"}
        manifest = {
            "apiVersion": "v1",
            "data": {},
            "immutable": True,
            "kind": "Secret",
            "metadata": {
                "annotations": {
                    "security.fs2.nebius.ai/custody-epoch-sha256": custody_epoch_sha256,
                },
                "labels": {
                    "security.fs2.nebius.ai/custody-owner": "external",
                    "security.fs2.nebius.ai/role": "token-anchor",
                },
                "name": ANCHOR_NAME,
                "namespace": ANCHOR_NAMESPACE,
            },
            "type": "Opaque",
        }
        query = urllib.parse.urlencode({"fieldValidation": "Strict"})
        status, response = self.request(
            f"/api/v1/namespaces/{ANCHOR_NAMESPACE}/secrets?{query}",
            method="POST",
            body=manifest,
            accept=METADATA_MEDIA_TYPE,
            allow_conflict=True,
        )
        if status == 409:
            existing = self.anchor_metadata()
            if existing is None or existing["custody_epoch_sha256"] != custody_epoch_sha256:
                raise OwnerTransportError("token-anchor create raced with another identity")
            return {**existing, "created": "false"}
        if (
            set(response) != {"apiVersion", "kind", "metadata"}
            or response.get("apiVersion") != "meta.k8s.io/v1"
            or response.get("kind") != "PartialObjectMetadata"
            or not isinstance(response.get("metadata"), dict)
        ):
            raise OwnerTransportError("token-anchor POST returned Secret payload fields")
        created = self._anchor_identity(response["metadata"])
        if created["custody_epoch_sha256"] != custody_epoch_sha256:
            raise OwnerTransportError("created token anchor has another custody epoch")
        reread = self.anchor_metadata()
        if reread != created:
            raise OwnerTransportError("token anchor changed after atomic creation")
        return {**created, "created": "true"}

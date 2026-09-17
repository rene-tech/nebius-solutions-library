#!/usr/bin/env python3
"""Fail-closed admission webhook for release writes and debug attach.

The VAP covers injection and RBAC shape. Kubernetes CONNECT admission carries
PodAttachOptions rather than the Pod, so this companion performs
the live Pod/cluster lookup needed to bind attach to the signed Pod UID and
tenant. It is deliberately read-only and uses only the projected ServiceAccount
token, CA, and Python's standard library.
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
API = "https://kubernetes.default.svc"
REGISTRY_NAMESPACE = "fs2-system"
REGISTRY_NAME = "fs2-debug-sessions"
ALLOWLIST_NAME = "fs2-image-provenance-allowlist"
REVIEWED_RESOURCES_NAME = "fs2-reviewed-resources"
TENANT_ANNOTATION = "security.fs2.nebius.ai/tenant"
MAX_BODY = 1 << 20
_release_principals = tuple(
    principal
    for principal in os.environ.get("RELEASE_PRINCIPALS", "").splitlines()
    if principal
)
if (
    not _release_principals
    or len(_release_principals) != len(set(_release_principals))
    or any(
        not principal.startswith("system:serviceaccount:")
        for principal in _release_principals
    )
):
    raise RuntimeError("RELEASE_PRINCIPALS is absent or malformed")
RELEASE_PRINCIPALS = frozenset(_release_principals)


def _get(path: str) -> dict:
    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    request = urllib.request.Request(
        API + path,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    context = ssl.create_default_context(cafile=str(CA_PATH))
    with urllib.request.urlopen(request, context=context, timeout=3) as response:
        return json.loads(response.read(MAX_BODY + 1))


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return parsed


def _canonical_object_sha256(value: dict) -> str:
    stable = json.loads(json.dumps(value))
    metadata = stable.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("admission object lacks metadata")
    for field in (
        "creationTimestamp", "deletionGracePeriodSeconds",
        "deletionTimestamp", "generation", "managedFields",
        "resourceVersion", "selfLink", "uid",
    ):
        metadata.pop(field, None)
    stable.pop("status", None)
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _authorize_release_mutation(request: dict) -> tuple[bool, str] | None:
    """Return a decision for release callers; None for non-release callers."""
    operation = str(request.get("operation", ""))
    if operation not in ("CREATE", "UPDATE", "DELETE"):
        return None
    principal = str((request.get("userInfo") or {}).get("username", ""))
    if principal not in RELEASE_PRINCIPALS:
        return None
    allowlist = _get(
        "/api/v1/namespaces/" + REGISTRY_NAMESPACE
        + "/configmaps/" + ALLOWLIST_NAME
    )
    allowlist_data = allowlist.get("data") or {}
    release_principals = set(
        str(allowlist_data.get("release-principals", "")).splitlines()
    )
    if release_principals != RELEASE_PRINCIPALS:
        return False, "release allow-list principals differ from the install pin"
    if allowlist_data.get("reviewed-resource-registry") != REVIEWED_RESOURCES_NAME:
        return False, "release allow-list does not pin the reviewed registry"
    nonce = str(allowlist_data.get("release-session-nonce", ""))
    registry = _get(
        "/api/v1/namespaces/" + REGISTRY_NAMESPACE
        + "/configmaps/" + REVIEWED_RESOURCES_NAME
    )
    data = registry.get("data") or {}
    meta = str(data.get("meta." + nonce, "")).split("|")
    if len(meta) != 6:
        return False, "current release session has no owner-reviewed plan"
    cluster_uid, source_commit, source_tree, authorization, expires, _plan_sha = meta
    plan_expires = _parse_time(expires)
    release_expires = _parse_time(
        str(allowlist_data.get("release-expires-at", ""))
    )
    if (
        source_commit != str(allowlist_data.get("reviewed-source-commit", ""))
        or source_tree != str(allowlist_data.get("reviewed-source-tree", ""))
        or authorization
        != str(allowlist_data.get("release-authorization-sha256", ""))
        or plan_expires > release_expires
        or datetime.now(UTC) >= plan_expires
    ):
        return False, "owner-reviewed plan does not equal the current release session"
    cluster_namespace = _get("/api/v1/namespaces/kube-system")
    if cluster_uid != str((cluster_namespace.get("metadata") or {}).get("uid", "")):
        return False, "owner-reviewed plan targets a different cluster"
    subject = request.get("oldObject") if operation == "DELETE" else request.get("object")
    if not isinstance(subject, dict):
        return False, "release mutation carries no admission object"
    annotations = (subject.get("metadata") or {}).get("annotations") or {}
    expected_annotations = {
        "security.fs2.nebius.ai/source-commit": source_commit,
        "security.fs2.nebius.ai/source-tree": source_tree,
        "security.fs2.nebius.ai/release-authorization": authorization,
        "security.fs2.nebius.ai/release-session": nonce,
    }
    if any(str(annotations.get(key, "")) != value
           for key, value in expected_annotations.items()):
        return False, "release object lacks the current reviewed-source binding"
    resource = request.get("resource") or {}
    old_metadata = (request.get("oldObject") or {}).get("metadata") or {}
    identity = {
        "operation": operation,
        "api_group": str(resource.get("group", "")),
        "api_version": str(resource.get("version", "")),
        "resource": str(resource.get("resource", "")),
        "subresource": str(request.get("subResource", "") or ""),
        "namespace": str(request.get("namespace", "")),
        "name": str(request.get("name", "")),
        "object_sha256": _canonical_object_sha256(subject),
        "old_uid": str(old_metadata.get("uid", "")) if operation != "CREATE" else "",
        "old_resource_version": (
            str(old_metadata.get("resourceVersion", ""))
            if operation != "CREATE" else ""
        ),
    }
    matches = []
    prefix = "grant." + nonce + "."
    for key, row in data.items():
        if not key.startswith(prefix):
            continue
        try:
            grant = json.loads(row)
        except json.JSONDecodeError:
            continue
        if grant == identity:
            matches.append(key)
    if len(matches) != 1:
        return False, "mutation does not equal one owner-reviewed canonical grant"
    return True, "owner-reviewed release mutation"


def authorize(review: dict) -> tuple[bool, str]:
    request = review.get("request") or {}
    release_decision = _authorize_release_mutation(request)
    if release_decision is not None:
        return release_decision
    if (
        request.get("operation") != "CONNECT"
        or request.get("subResource") != "attach"
    ):
        return True, "not a governed debug CONNECT request"
    namespace = str(request.get("namespace", ""))
    pod_name = str(request.get("name", ""))
    principal = str((request.get("userInfo") or {}).get("username", ""))
    container = str((request.get("object") or {}).get("container", ""))
    if not all((namespace, pod_name, principal, container)):
        return False, "CONNECT request lacks namespace, pod, principal, or container"

    registry = _get(
        "/api/v1/namespaces/"
        + REGISTRY_NAMESPACE
        + "/configmaps/"
        + REGISTRY_NAME
    )
    allowlist = _get(
        "/api/v1/namespaces/"
        + REGISTRY_NAMESPACE
        + "/configmaps/"
        + ALLOWLIST_NAME
    )
    pod = _get(
        "/api/v1/namespaces/"
        + urllib.parse.quote(namespace, safe="")
        + "/pods/"
        + urllib.parse.quote(pod_name, safe="")
    )
    cluster_namespace = _get("/api/v1/namespaces/kube-system")
    data = registry.get("data") or {}
    allowed_images = set(
        str((allowlist.get("data") or {}).get("allowed-images", "")).splitlines()
    )
    pod_metadata = pod.get("metadata") or {}
    pod_uid = str(pod_metadata.get("uid", ""))
    tenant = str((pod_metadata.get("annotations") or {}).get(TENANT_ANNOTATION, ""))
    cluster_uid = str((cluster_namespace.get("metadata") or {}).get("uid", ""))
    ephemeral = {
        str(item.get("name", "")): str(item.get("image", ""))
        for item in (pod.get("spec") or {}).get("ephemeralContainers") or []
    }
    matches = []
    for key, value in data.items():
        if not key.startswith("active."):
            continue
        nonce = key[7:]
        fields = str(value).split("|")
        if len(fields) != 10 or "retired." + nonce in data:
            continue
        (
            row_cluster,
            row_namespace,
            row_pod,
            row_uid,
            row_container,
            row_tenant,
            row_image,
            row_expires,
            _row_sha,
            row_principal,
        ) = fields
        try:
            unexpired = datetime.now(UTC) < _parse_time(row_expires)
        except ValueError:
            unexpired = False
        if (
            unexpired
            and row_cluster == cluster_uid
            and row_namespace == namespace
            and row_pod == pod_name
            and row_uid == pod_uid
            and row_container == container
            and row_tenant == tenant
            and row_principal == principal
            and ephemeral.get(container) == row_image
            and row_image in allowed_images
        ):
            matches.append(nonce)
    if len(matches) != 1:
        return False, "no single active session matches cluster/Pod UID/tenant/container"
    return True, "active owner-governed debug session"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        uid = ""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > MAX_BODY:
                raise ValueError("invalid AdmissionReview size")
            review = json.loads(self.rfile.read(length))
            uid = str((review.get("request") or {}).get("uid", ""))
            allowed, message = authorize(review)
        except (ValueError, OSError, json.JSONDecodeError, urllib.error.URLError) as error:
            allowed, message = False, "debug authority unavailable: " + type(error).__name__
        response = {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {
                "uid": uid,
                "allowed": allowed,
                "status": {"message": message},
            },
        }
        payload = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(os.environ["TLS_CERT_FILE"], os.environ["TLS_KEY_FILE"])
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

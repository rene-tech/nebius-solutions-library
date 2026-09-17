#!/usr/bin/env python3
"""HTTPS activation and AdmissionReview endpoint for reconciler cutover."""

from __future__ import annotations

import http.server
import hashlib
import json
import os
import sqlite3
import ssl
import threading
import time
from pathlib import Path

from daemonset_fence_policy import canonical
from storage_reconciler_cutover_policy import allows
from storage_reconciler_cutover_executor import KubernetesAPI, execute
from storage_reconciler_cutover_runtime import load_verified_state

MAX_BYTES = 2 * 1024 * 1024
TLS_CERTIFICATE = "/var/run/fs2-storage-cutover-tls/tls.crt"
TLS_PRIVATE_KEY = "/var/run/fs2-storage-cutover-tls/tls.key"
STATE_DATABASE = Path("/var/lib/fs2-storage-cutover/decisions.sqlite3")


def _request(review: object) -> dict[str, object]:
    if not isinstance(review, dict) or not isinstance(review.get("request"), dict):
        raise ValueError("AdmissionReview request is absent")
    value = review["request"]
    if not isinstance(value.get("uid"), str) or not value["uid"]:
        raise ValueError("AdmissionReview UID is absent")
    resource = value.get("resource") or {}
    user = value.get("userInfo") or {}
    return {
        "review_uid": value.get("uid", ""),
        "operation": value.get("operation"),
        "resource": resource.get("resource"),
        "namespace": value.get("namespace", ""),
        "name": value.get("name", ""),
        "username": user.get("username"),
        "uid": user.get("uid"),
        "groups": sorted(user.get("groups") or []),
        "object": value.get("object") or {},
        "old_object": value.get("oldObject") or {},
    }


def _record(request: dict[str, object], state: dict[str, object], allowed: bool) -> None:
    request_sha256 = hashlib.sha256(canonical(request)).hexdigest()
    source = request["old_object"] if request["operation"] == "DELETE" else request["object"]
    if not isinstance(source, dict):
        raise ValueError("cutover AdmissionReview object is malformed")
    metadata = source.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("cutover object metadata is malformed")
    annotations = metadata.get("annotations") or {}
    if not isinstance(annotations, dict):
        raise ValueError("cutover annotations are malformed")
    transition_id = str(annotations.get("fs2.nebius.ai/storage-cutover-transition-sha256", ""))
    with sqlite3.connect(STATE_DATABASE, isolation_level=None) as database:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA synchronous=FULL")
        database.execute(
            """CREATE TABLE IF NOT EXISTS decisions (
            review_uid TEXT PRIMARY KEY, state_head_sha256 TEXT NOT NULL,
            request_sha256 TEXT NOT NULL, allowed INTEGER NOT NULL CHECK(allowed IN (0,1)))"""
        )
        database.execute(
            """CREATE TABLE IF NOT EXISTS transition_operations (
            transition_id TEXT NOT NULL, operation TEXT NOT NULL, resource TEXT NOT NULL,
            namespace TEXT NOT NULL, name TEXT NOT NULL, request_sha256 TEXT NOT NULL,
            PRIMARY KEY(transition_id,operation,resource,namespace,name))"""
        )
        database.execute("BEGIN IMMEDIATE")
        prior = database.execute(
            "SELECT state_head_sha256,request_sha256,allowed FROM decisions WHERE review_uid=?",
            (request["review_uid"],),
        ).fetchone()
        expected = (state["head_sha256"], request_sha256, int(allowed))
        if prior is not None and prior != expected:
            database.execute("ROLLBACK")
            raise ValueError("cutover AdmissionReview UID replay differs")
        if allowed and transition_id:
            consumed = database.execute(
                "SELECT request_sha256 FROM transition_operations WHERE transition_id=? AND operation=? AND resource=? AND namespace=? AND name=?",
                (
                    transition_id,
                    request["operation"],
                    request["resource"],
                    request["namespace"],
                    request["name"],
                ),
            ).fetchone()
            if consumed is not None and consumed[0] != request_sha256:
                database.execute("ROLLBACK")
                raise ValueError("cutover transition operation was already consumed")
            database.execute(
                "INSERT OR IGNORE INTO transition_operations VALUES(?,?,?,?,?,?)",
                (
                    transition_id,
                    request["operation"],
                    request["resource"],
                    request["namespace"],
                    request["name"],
                    request_sha256,
                ),
            )
        database.execute(
            "INSERT OR IGNORE INTO decisions VALUES(?,?,?,?)",
            (request["review_uid"], state["head_sha256"], request_sha256, int(allowed)),
        )
        database.execute("COMMIT")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "fs2-storage-reconciler-cutover"

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/v1/storage-reconciler/activation":
            self.send_error(404)
            return
        try:
            state = load_verified_state()
            payload = canonical(state["activation_envelope"])
        except Exception:
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        review_uid = ""
        allowed = False
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/validate-storage-reconciler" or not 0 < length <= MAX_BYTES:
                raise ValueError("bounded cutover AdmissionReview required")
            review = json.loads(self.rfile.read(length))
            request = _request(review)
            review_uid = str(request["review_uid"])
            state = load_verified_state()
            allowed = allows(request, state)
            _record(request, state, allowed)
        except Exception:
            allowed = False
        payload = canonical(
            {
                "apiVersion": "admission.k8s.io/v1",
                "kind": "AdmissionReview",
                "response": {
                    "uid": review_uid,
                    "allowed": allowed,
                    "status": {"message": "signed storage reconciler cutover fence"},
                },
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def _execute_signed_cutovers(api: KubernetesAPI) -> None:
    while True:
        try:
            state = load_verified_state()
            if state["transition"]["phase"] in {
                "QUIESCE_PREDECESSOR",
                "ACTIVATE_SUCCESSOR",
                "ROLLBACK_QUIESCE",
                "ROLLBACK_ACTIVATE",
            }:
                execute(api, state)
        except Exception:
            # The admission/activation paths remain available and fail closed.
            # A later identical signed epoch retries the idempotent CAS; no
            # alternate transition or best-effort mutation is attempted.
            pass
        time.sleep(2)


def main() -> None:
    if os.getuid() == 0:
        raise RuntimeError("cutover service refuses to run as root")
    STATE_DATABASE.parent.mkdir(mode=0o700, parents=False, exist_ok=True)
    api = KubernetesAPI(os.environ.get("FS2_CUTOVER_KUBERNETES_API", ""))
    threading.Thread(
        target=_execute_signed_cutovers,
        args=(api,),
        name="storage-reconciler-cutover-executor",
        daemon=True,
    ).start()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 8444), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(TLS_CERTIFICATE, TLS_PRIVATE_KEY)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

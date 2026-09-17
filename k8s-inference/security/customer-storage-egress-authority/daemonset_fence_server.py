#!/usr/bin/env python3
"""Fail-closed AdmissionReview server for the external DaemonSet fence."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import sqlite3
import ssl
from pathlib import Path
from typing import Any

from daemonset_fence_policy import allows, canonical
from daemonset_fence_runtime import load_verified_state

STATE_DATABASE = Path("/var/lib/fs2-daemonset-fence/decisions.sqlite3")
TLS_CERTIFICATE = Path("/var/run/fs2-daemonset-fence-tls/tls.crt")
TLS_PRIVATE_KEY = Path("/var/run/fs2-daemonset-fence-tls/tls.key")
MAX_REVIEW_BYTES = 2 * 1024 * 1024


def _request(review: object) -> dict[str, Any]:
    if not isinstance(review, dict) or set(review) != {"apiVersion", "kind", "request"}:
        raise ValueError("AdmissionReview fields differ")
    value = review.get("request")
    if not isinstance(value, dict) or not isinstance(value.get("uid"), str) or not value["uid"]:
        raise ValueError("AdmissionReview request is absent")
    resource = value.get("resource") or {}
    user = value.get("userInfo") or {}
    return {
        "review_uid": value["uid"],
        "dry_run": value.get("dryRun") is True,
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


def _transition_token(request: dict[str, Any]) -> str:
    source = request["old_object"] if request["operation"] == "DELETE" else request["object"]
    annotations = (source.get("metadata") or {}).get("annotations") or {}
    return str(annotations.get("security.fs2.nebius.ai/daemonset-transition-sha256", ""))


def _record_decision(request: dict[str, Any], state: dict[str, Any], allowed: bool) -> None:
    """Atomically fence a transition token to one canonical operation body.

    Admission retries for the same operation body are idempotent.  Reuse of a
    signed token for a different body is denied before a decision is returned.
    The signed ledger, not this database, advances readiness or completion.
    """

    mutation = {
        key: item for key, item in request.items() if key not in {"review_uid", "dry_run"}
    }
    request_sha256 = hashlib.sha256(canonical(mutation)).hexdigest()
    token = _transition_token(request)
    with sqlite3.connect(STATE_DATABASE, isolation_level=None) as database:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA synchronous=FULL")
        database.execute(
            """CREATE TABLE IF NOT EXISTS admission_decisions (
            review_uid TEXT PRIMARY KEY, state_head_sha256 TEXT NOT NULL,
            request_sha256 TEXT NOT NULL, allowed INTEGER NOT NULL CHECK(allowed IN (0,1)))"""
        )
        database.execute(
            """CREATE TABLE IF NOT EXISTS transition_consumption (
            transition_id TEXT NOT NULL, operation TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            PRIMARY KEY(transition_id, operation))"""
        )
        database.execute("BEGIN IMMEDIATE")
        prior = database.execute(
            "SELECT state_head_sha256,request_sha256,allowed FROM admission_decisions WHERE review_uid=?",
            (request["review_uid"],),
        ).fetchone()
        if prior is not None and prior != (
            state["head_sha256"],
            request_sha256,
            int(allowed),
        ):
            database.execute("ROLLBACK")
            raise ValueError("AdmissionReview UID was replayed with different bytes")
        if token and allowed:
            consumed = database.execute(
                "SELECT request_sha256 FROM transition_consumption WHERE transition_id=? AND operation=?",
                (token, request["operation"]),
            ).fetchone()
            if consumed is not None and consumed[0] != request_sha256:
                database.execute("ROLLBACK")
                raise ValueError("DaemonSet transition was already consumed by different bytes")
            database.execute(
                "INSERT OR IGNORE INTO transition_consumption(transition_id,operation,request_sha256) VALUES(?,?,?)",
                (token, request["operation"], request_sha256),
            )
        database.execute(
            "INSERT OR IGNORE INTO admission_decisions(review_uid,state_head_sha256,request_sha256,allowed) VALUES(?,?,?,?)",
            (request["review_uid"], state["head_sha256"], request_sha256, int(allowed)),
        )
        database.execute("COMMIT")


def evaluate(review: object) -> tuple[str, bool]:
    """Evaluate one review; dry-run is deliberately side-effect free."""

    request = _request(review)
    state = load_verified_state()
    allowed = allows(request, verified_state=state)
    if not request["dry_run"]:
        _record_decision(request, state, allowed)
    return request["review_uid"], allowed


class AdmissionHandler(http.server.BaseHTTPRequestHandler):
    server_version = "fs2-daemonset-fence"

    def do_POST(self) -> None:  # noqa: N802
        review_uid = ""
        allowed = False
        message = "request denied by the external DaemonSet fence"
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/validate" or not 0 < length <= MAX_REVIEW_BYTES:
                raise ValueError("bounded /validate AdmissionReview required")
            payload = self.rfile.read(length)
            review = json.loads(payload)
            review_uid, allowed = evaluate(review)
            message = "authorized by exact signed DaemonSet transition state" if allowed else message
        except Exception:
            allowed = False
            message = "external DaemonSet fence verification failed closed"
        response = canonical(
            {
                "apiVersion": "admission.k8s.io/v1",
                "kind": "AdmissionReview",
                "response": {
                    "uid": review_uid,
                    "allowed": allowed,
                    "status": {"message": message},
                },
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    if os.getuid() == 0:
        raise RuntimeError("DaemonSet fence refuses to run as root")
    STATE_DATABASE.parent.mkdir(mode=0o700, parents=False, exist_ok=True)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 8443), AdmissionHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(TLS_CERTIFICATE, TLS_PRIVATE_KEY)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

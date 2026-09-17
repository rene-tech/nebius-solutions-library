"""Fail-closed authorization for broker-scoped request-debug reads."""

from __future__ import annotations

import base64
import binascii
import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Request

from .admin import AdminProblemError


TRUST_SCHEMA = "fs2-serve.nebius.ai/trusted-internal-debug-activation-issuers/v1"
TOKEN_SCHEMA = "fs2-serve.nebius.ai/request-debug-proxy-authorization/v1"
AUDIENCE = "fs2-request-debug-read"
MAX_TOKEN_BYTES = 32 * 1024
MAX_ACTIVATION = timedelta(days=7)
MAX_REQUEST_GRANT = timedelta(seconds=30)
MAX_TERMINAL_DELIVERY = timedelta(minutes=5)
SESSION_EVENT_SCHEMA = "fs2-serve.nebius.ai/request-debug-session-event/v1"
ACTIVATION_TOMBSTONE_SCHEMA = (
    "fs2-serve.nebius.ai/request-debug-activation-tombstone/v1"
)
TERMINAL_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/internal-proxy-terminal/v1"


class RequestDebugSessionRegistry:
    """Append-only current/revoked state shared by capture and read paths."""

    def __init__(self, pool: Any | None = None) -> None:
        self.pool = pool
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._revoked_activations: dict[str, dict[str, Any]] = {}
        self._nonces: set[str] = set()
        self._lock = asyncio.Lock()
        self.event_verifier: Callable[[object], Mapping[str, Any]] | None = None

    @staticmethod
    def _event_row(
        envelope: Mapping[str, Any], payload: Mapping[str, Any], payload_sha256: str
    ) -> dict[str, Any]:
        return {
            **payload,
            "app_id": UUID(str(payload["app_id"])),
            "event_at": _timestamp(payload["event_at"], "debug session event_at"),
            "activation_expires_at": _timestamp(
                payload["activation_expires_at"], "debug activation expires_at"
            ),
            "payload_sha256": payload_sha256,
            "issuer_id": payload["issuer"]["id"],
            "key_id": payload["issuer"]["key_id"],
            "signature": envelope["signature"],
            "signed_envelope": dict(envelope),
            "signed_envelope_sha256": hashlib.sha256(_canonical_bytes(envelope)).hexdigest(),
            "terminal_teardown_receipt": payload["terminal_teardown_receipt"],
        }

    @staticmethod
    def _successor(previous: Mapping[str, Any] | None, row: Mapping[str, Any]) -> bool:
        if previous is None:
            return (
                row["event_type"] == "activated"
                and row["sequence"] == 1
                and row["revocation_epoch"] == 0
                and row["prior_event_sha256"] is None
                and row["terminal_teardown_receipt"] is None
            )
        immutable = (
            "activation_payload_sha256",
            "broker_id",
            "cluster_id",
            "deployment_id",
            "tenant_id",
            "model_id",
            "app_id",
            "activation_expires_at",
            "issuer_id",
            "key_id",
        )
        return (
            previous["event_type"] != "revoked"
            and row["sequence"] == previous["sequence"] + 1
            and row["event_at"] > previous["event_at"]
            and row["prior_event_sha256"] == previous["signed_envelope_sha256"]
            and all(row[field] == previous[field] for field in immutable)
            and (
                (
                    row["event_type"] == "heartbeat"
                    and row["revocation_epoch"] == previous["revocation_epoch"]
                    and row["terminal_teardown_receipt"] is None
                )
                or (
                    row["event_type"] == "revoked"
                    and row["revocation_epoch"] == previous["revocation_epoch"] + 1
                    and row["terminal_teardown_receipt"] is not None
                )
            )
        )

    def _verified_history(
        self, rows: list[Mapping[str, Any]]
    ) -> Mapping[str, Any] | None:
        if not rows or self.event_verifier is None:
            return None
        previous: Mapping[str, Any] | None = None
        try:
            for raw in rows:
                normalized = self._verified_projection(raw)
                if normalized is None or not self._successor(previous, normalized):
                    return None
                previous = normalized
        except (AdminProblemError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return dict(previous) if previous is not None else None

    def _verified_projection(
        self, raw: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        if self.event_verifier is None:
            return None
        try:
            row = dict(raw)
            envelope = row.get("signed_envelope")
            if isinstance(envelope, str):
                envelope = json.loads(envelope)
            payload = self.event_verifier(envelope)
            normalized = self._event_row(
                envelope,
                payload,
                str(row.get("payload_sha256")),
            )
            compared = (
                "activation_expires_at",
                "activation_payload_sha256",
                "app_id",
                "broker_id",
                "cluster_id",
                "deployment_id",
                "event_at",
                "event_type",
                "issuer_id",
                "key_id",
                "model_id",
                "payload_sha256",
                "prior_event_sha256",
                "revocation_epoch",
                "sequence",
                "session_id",
                "signature",
                "signed_envelope_sha256",
                "tenant_id",
                "terminal_teardown_receipt",
            )
            if (
                _canonical_bytes(envelope)
                != _canonical_bytes(row.get("signed_envelope"))
                or any(normalized[field] != row.get(field) for field in compared)
            ):
                return None
            return normalized
        except (AdminProblemError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    async def append(
        self,
        envelope: Mapping[str, Any],
        payload: Mapping[str, Any],
        payload_sha256: str,
        *,
        audit_store: Any | None = None,
        audit: Mapping[str, Any] | None = None,
    ) -> str:
        row = self._event_row(envelope, payload, payload_sha256)
        event_digest = str(row["signed_envelope_sha256"])
        if self.pool is None:
            async with self._lock:
                history = self._events.setdefault(str(row["session_id"]), [])
                if row["activation_payload_sha256"] in self._revoked_activations:
                    raise AdminProblemError(
                        409,
                        "debug_activation_terminal",
                        "a tombstoned debug activation cannot append another event",
                    )
                if any(item["payload_sha256"] == payload_sha256 for item in history):
                    return event_digest
                previous = history[-1] if history else None
                if not self._successor(previous, row):
                    raise AdminProblemError(
                        409, "debug_session_conflict", "debug session event is not the next append-only transition"
                    )
                if row["event_type"] == "activated":
                    if row["activation_payload_sha256"] in self._revoked_activations:
                        raise AdminProblemError(
                            409,
                            "debug_activation_reused",
                            "a revoked debug activation cannot start another session",
                        )
                    for events in self._events.values():
                        if not events:
                            continue
                        if events[0]["activation_payload_sha256"] == row[
                            "activation_payload_sha256"
                        ]:
                            raise AdminProblemError(
                                409,
                                "debug_activation_reused",
                                "a signed debug activation cannot start another session",
                            )
                        current = events[-1]
                        if (
                            current["event_type"] != "revoked"
                            and current["activation_payload_sha256"]
                            not in self._revoked_activations
                            and current["activation_expires_at"] > row["event_at"]
                            and (current["tenant_id"], current["model_id"], current["app_id"])
                            == (row["tenant_id"], row["model_id"], row["app_id"])
                        ):
                            raise AdminProblemError(
                                409, "debug_session_overlap", "the signed scope already has a current activation"
                            )
                history.append(row)
                try:
                    if audit_store is not None and audit is not None:
                        await audit_store.append_audit_event(**dict(audit))
                except BaseException:
                    history.pop()
                    raise
            return event_digest
        if audit_store is not None and getattr(audit_store, "pool", None) is not self.pool:
            raise AdminProblemError(
                503,
                "debug_audit_unavailable",
                "debug authorization audit does not share the state transaction",
            )
        async with self.pool.acquire() as connection, connection.transaction():
            scope_lock = "\x1f".join(
                (str(row["tenant_id"]), str(row["model_id"]), str(row["app_id"]))
            )
            for lock_value in (
                str(row["session_id"]),
                str(row["activation_payload_sha256"]),
                scope_lock,
            ):
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                    lock_value,
                )
            tombstoned = await connection.fetchval(
                """
                SELECT 1 FROM fs2_request_debug_activation_revocations
                 WHERE activation_payload_sha256=$1
                """,
                row["activation_payload_sha256"],
            )
            if tombstoned:
                raise AdminProblemError(
                    409,
                    "debug_activation_terminal",
                    "a tombstoned debug activation cannot append another event",
                )
            duplicate = await connection.fetchval(
                "SELECT 1 FROM fs2_request_debug_activation_events WHERE payload_sha256=$1",
                payload_sha256,
            )
            if duplicate:
                return event_digest
            previous = await connection.fetchrow(
                "SELECT * FROM fs2_request_debug_activation_events WHERE session_id=$1 ORDER BY sequence DESC LIMIT 1 FOR UPDATE",
                row["session_id"],
            )
            if not self._successor(dict(previous) if previous is not None else None, row):
                raise AdminProblemError(
                    409, "debug_session_conflict", "debug session event is not the next append-only transition"
                )
            if row["event_type"] == "activated":
                overlap = await connection.fetchval(
                    """
                    SELECT 1 FROM (
                      SELECT DISTINCT ON (session_id) *
                      FROM fs2_request_debug_activation_events
                      ORDER BY session_id,sequence DESC
                    ) current
                    WHERE event_type!='revoked' AND activation_expires_at>$1
                      AND tenant_id=$2 AND model_id=$3 AND app_id=$4
                      AND NOT EXISTS (
                        SELECT 1
                          FROM fs2_request_debug_activation_revocations revoked
                         WHERE revoked.activation_payload_sha256=
                               current.activation_payload_sha256
                      )
                    LIMIT 1
                    """,
                    row["event_at"], row["tenant_id"], row["model_id"], row["app_id"],
                )
                if overlap:
                    raise AdminProblemError(
                        409, "debug_session_overlap", "the signed scope already has a current activation"
                    )
            await connection.execute(
                """
                INSERT INTO fs2_request_debug_activation_events
                (session_id,sequence,event_type,event_at,activation_expires_at,
                 activation_payload_sha256,broker_id,cluster_id,deployment_id,
                 tenant_id,model_id,app_id,revocation_epoch,payload_sha256,
                 prior_event_sha256,issuer_id,key_id,signature,signed_envelope,
                 signed_envelope_sha256,terminal_teardown_receipt)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,
                       $15,$16,$17,$18,$19,$20,$21)
                """,
                row["session_id"], row["sequence"], row["event_type"], row["event_at"],
                row["activation_expires_at"], row["activation_payload_sha256"],
                row["broker_id"], row["cluster_id"], row["deployment_id"],
                row["tenant_id"], row["model_id"], row["app_id"],
                row["revocation_epoch"], payload_sha256, row["prior_event_sha256"],
                row["issuer_id"], row["key_id"], row["signature"],
                json.dumps(row["signed_envelope"], separators=(",", ":"), sort_keys=True),
                row["signed_envelope_sha256"],
                json.dumps(row["terminal_teardown_receipt"], separators=(",", ":"), sort_keys=True)
                if row["terminal_teardown_receipt"] is not None
                else None,
            )
            if audit_store is not None and audit is not None:
                audit_on_connection = getattr(
                    audit_store, "append_audit_event_on_connection", None
                )
                if not callable(audit_on_connection):
                    raise AdminProblemError(
                        503,
                        "debug_audit_unavailable",
                        "debug authorization audit is not transaction-aware",
                    )
                await audit_on_connection(connection, **dict(audit))
        return event_digest

    async def revoke_activation(
        self,
        envelope: Mapping[str, Any],
        payload: Mapping[str, Any],
        payload_sha256: str,
        *,
        audit_store: Any | None = None,
        audit: Mapping[str, Any] | None = None,
    ) -> str:
        """Append one broker-signed activation tombstone without deleting evidence."""

        activation_sha256 = str(payload["activation_payload_sha256"])
        row = {
            **payload,
            "app_id": UUID(str(payload["app_id"])),
            "revoked_at": _timestamp(
                payload["revoked_at"], "debug activation revoked_at"
            ),
            "issuer_id": payload["issuer"]["id"],
            "key_id": payload["issuer"]["key_id"],
            "signature": envelope["signature"],
            "signed_envelope": dict(envelope),
            "signed_envelope_sha256": hashlib.sha256(
                _canonical_bytes(envelope)
            ).hexdigest(),
            "payload_sha256": payload_sha256,
        }
        if self.pool is None:
            async with self._lock:
                roots = [
                    events[0]
                    for events in self._events.values()
                    if events
                    and events[0]["activation_payload_sha256"] == activation_sha256
                ]
                if len(roots) != 1:
                    raise AdminProblemError(
                        409,
                        "debug_revocation_conflict",
                        "debug activation does not have one retained session root",
                    )
                if any(
                    row[field] != roots[0][field]
                    for field in ("tenant_id", "model_id", "app_id")
                ):
                    raise AdminProblemError(
                        403,
                        "debug_scope_mismatch",
                        "debug tombstone differs from the retained activation root",
                    )
                history = self._events[str(roots[0]["session_id"])]
                current = history[-1]
                if (
                    row["session_id"] != roots[0]["session_id"]
                    or row["sequence"] != current["sequence"]
                    or current["event_type"] == "revoked"
                ):
                    raise AdminProblemError(
                        409,
                        "debug_revocation_conflict",
                        "debug tombstone does not bind the exact current session event",
                    )
                previous = self._revoked_activations.get(activation_sha256)
                if previous is not None:
                    if previous["signed_envelope_sha256"] != row["signed_envelope_sha256"]:
                        raise AdminProblemError(
                            409,
                            "debug_revocation_conflict",
                            "debug activation already has a different tombstone",
                        )
                    return str(row["signed_envelope_sha256"])
                self._revoked_activations[activation_sha256] = row
                try:
                    if audit_store is not None and audit is not None:
                        await audit_store.append_audit_event(**dict(audit))
                except BaseException:
                    del self._revoked_activations[activation_sha256]
                    raise
            return str(row["signed_envelope_sha256"])
        if audit_store is not None and getattr(audit_store, "pool", None) is not self.pool:
            raise AdminProblemError(
                503,
                "debug_audit_unavailable",
                "debug revocation audit does not share the state transaction",
            )
        async with self.pool.acquire() as connection, connection.transaction():
            root = await connection.fetchrow(
                """
                SELECT session_id,tenant_id,model_id,app_id
                  FROM fs2_request_debug_activation_roots
                 WHERE activation_payload_sha256=$1
                """,
                activation_sha256,
            )
            if root is not None:
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                    root["session_id"],
                )
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                activation_sha256,
            )
            locked_root = await connection.fetchrow(
                """
                SELECT session_id,tenant_id,model_id,app_id
                  FROM fs2_request_debug_activation_roots
                 WHERE activation_payload_sha256=$1
                """,
                activation_sha256,
            )
            if root is None and locked_root is not None:
                raise AdminProblemError(
                    409,
                    "debug_revocation_conflict",
                    "debug activation root changed while revocation was serialized",
                )
            if locked_root is None:
                raise AdminProblemError(
                    409,
                    "debug_revocation_conflict",
                    "debug activation does not have one retained session root",
                )
            if root is not None and (
                locked_root is None
                or locked_root["session_id"] != root["session_id"]
            ):
                raise AdminProblemError(
                    409,
                    "debug_revocation_conflict",
                    "debug activation root changed while revocation was serialized",
                )
            if locked_root is not None and any(
                row[field] != locked_root[field]
                for field in ("tenant_id", "model_id", "app_id")
            ):
                raise AdminProblemError(
                    403,
                    "debug_scope_mismatch",
                    "debug tombstone differs from the retained activation root",
                )
            if locked_root is not None:
                current = await connection.fetchrow(
                    """
                    SELECT sequence,event_type
                      FROM fs2_request_debug_activation_events
                     WHERE session_id=$1
                     ORDER BY sequence DESC LIMIT 1 FOR UPDATE
                    """,
                    locked_root["session_id"],
                )
                if (
                    current is None
                    or row["session_id"] != locked_root["session_id"]
                    or row["sequence"] != current["sequence"]
                    or current["event_type"] == "revoked"
                ):
                    raise AdminProblemError(
                        409,
                        "debug_revocation_conflict",
                        "debug tombstone does not bind the exact current session event",
                    )
            if locked_root is not None:
                scope_lock = "\x1f".join(
                    (
                        str(locked_root["tenant_id"]),
                        str(locked_root["model_id"]),
                        str(locked_root["app_id"]),
                    )
                )
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                    scope_lock,
                )
            previous = await connection.fetchrow(
                """
                SELECT terminal_envelope_sha256
                  FROM fs2_request_debug_activation_revocations
                 WHERE activation_payload_sha256=$1
                """,
                activation_sha256,
            )
            if previous is not None:
                if previous["terminal_envelope_sha256"] != row["signed_envelope_sha256"]:
                    raise AdminProblemError(
                        409,
                        "debug_revocation_conflict",
                        "debug activation already has a different tombstone",
                    )
                return str(row["signed_envelope_sha256"])
            await connection.execute(
                """
                INSERT INTO fs2_request_debug_activation_revocations
                    (activation_payload_sha256,session_id,sequence,revocation_epoch,
                     revoked_at,reason,source_schema,issuer_id,key_id,signature,
                     signed_envelope,terminal_envelope_sha256,terminal_teardown_receipt)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                """,
                activation_sha256,
                row["session_id"],
                row["sequence"],
                row["revocation_epoch"],
                row["revoked_at"],
                row["reason"],
                ACTIVATION_TOMBSTONE_SCHEMA,
                row["issuer_id"],
                row["key_id"],
                row["signature"],
                json.dumps(
                    row["signed_envelope"], separators=(",", ":"), sort_keys=True
                ),
                row["signed_envelope_sha256"],
                json.dumps(
                    row["terminal_teardown_receipt"],
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
            if audit_store is not None and audit is not None:
                audit_on_connection = getattr(
                    audit_store, "append_audit_event_on_connection", None
                )
                if not callable(audit_on_connection):
                    raise AdminProblemError(
                        503,
                        "debug_audit_unavailable",
                        "debug revocation audit is not transaction-aware",
                    )
                await audit_on_connection(connection, **dict(audit))
        return str(row["signed_envelope_sha256"])

    async def _latest(self, session_id: str) -> Mapping[str, Any] | None:
        if self.pool is None:
            async with self._lock:
                history = self._events.get(session_id, [])
                latest = dict(history[-1]) if history else None
                if (
                    latest is not None
                    and latest["activation_payload_sha256"]
                    in self._revoked_activations
                ):
                    latest = None
            return self._verified_projection(latest) if latest is not None else None
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT * FROM fs2_request_debug_session_current
                   WHERE session_id=$1 AND event_type!='revoked'
                     AND activation_expires_at>clock_timestamp()
                     AND NOT EXISTS (
                       SELECT 1 FROM fs2_request_debug_activation_revocations revoked
                        WHERE revoked.activation_payload_sha256=
                              fs2_request_debug_session_current.activation_payload_sha256
                     )""",
                session_id,
            )
        return self._verified_projection(dict(row)) if row is not None else None

    @staticmethod
    def _current_matches(
        current: Mapping[str, Any] | None, expected: Mapping[str, Any]
    ) -> bool:
        return (
            current is not None
            and current["event_type"] != "revoked"
            and current["activation_expires_at"] > datetime.now(UTC)
            and all(current[field] == value for field, value in expected.items())
        )

    @staticmethod
    def _grant_expected(payload: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "activation_expires_at": _timestamp(
                payload["activation_expires_at"], "debug activation expires_at"
            ),
            "activation_payload_sha256": payload["activation_payload_sha256"],
            "broker_id": payload["broker_id"],
            "cluster_id": payload["cluster_id"],
            "deployment_id": payload["deployment_id"],
            "tenant_id": payload["tenant_id"],
            "model_id": payload["model_id"],
            "app_id": UUID(str(payload["app_id"])),
            "revocation_epoch": payload["revocation_epoch"],
            "sequence": payload["event_sequence"],
        }

    async def authorize_and_consume(
        self,
        payload: Mapping[str, Any],
        operation: Callable[[Any | None], Awaitable[Any]],
    ) -> Any:
        """Linearize one non-replayable read with append-only revocation."""
        expires_at = _timestamp(payload["expires_at"], "debug grant expires_at")
        activation_expires_at = _timestamp(
            payload["activation_expires_at"], "debug activation expires_at"
        )
        deadline = min(expires_at, activation_expires_at)
        expected = self._grant_expected(payload)
        nonce_sha256 = hashlib.sha256(str(payload["request_nonce"]).encode()).hexdigest()
        session_id = str(payload["session_id"])
        if self.pool is None:
            async with self._lock:
                history = self._events.get(session_id, [])
                current = self._verified_projection(dict(history[-1])) if history else None
                if (
                    current is not None
                    and current["activation_payload_sha256"]
                    in self._revoked_activations
                ):
                    current = None
                if not self._current_matches(current, expected) or nonce_sha256 in self._nonces:
                    raise AdminProblemError(
                        403,
                        "debug_session_inactive",
                        "debug session is expired, revoked, or replayed",
                    )
                remaining = (deadline - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise AdminProblemError(
                        403, "debug_session_inactive", "debug grant expired before read"
                    )
                try:
                    result = await asyncio.wait_for(operation(None), timeout=remaining)
                except TimeoutError as exc:
                    raise AdminProblemError(
                        403, "debug_session_inactive", "debug grant expired during read"
                    ) from exc
                if datetime.now(UTC) >= deadline:
                    raise AdminProblemError(
                        403, "debug_session_inactive", "debug grant expired during read"
                    )
                self._nonces.add(nonce_sha256)
                return result
        async def database_read() -> Any:
            async with self.pool.acquire() as connection, connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", session_id
                )
                row = await connection.fetchrow(
                    """SELECT * FROM fs2_request_debug_session_current
                       WHERE session_id=$1 AND event_type!='revoked'
                         AND activation_expires_at>clock_timestamp()
                         AND NOT EXISTS (
                           SELECT 1 FROM fs2_request_debug_activation_revocations revoked
                            WHERE revoked.activation_payload_sha256=
                                  fs2_request_debug_session_current.activation_payload_sha256
                         )""",
                    session_id,
                )
                current = self._verified_projection(dict(row)) if row is not None else None
                if not self._current_matches(current, expected):
                    raise AdminProblemError(
                        403,
                        "debug_session_inactive",
                        "debug session is expired, revoked, or replayed",
                    )
                inserted = await connection.execute(
                    """
                    INSERT INTO fs2_request_debug_authorization_nonces
                        (nonce_sha256,session_id,expires_at)
                    VALUES($1,$2,$3) ON CONFLICT DO NOTHING
                    """,
                    nonce_sha256,
                    session_id,
                    expires_at,
                )
                if inserted != "INSERT 0 1":
                    raise AdminProblemError(
                        403,
                        "debug_session_inactive",
                        "debug session is expired, revoked, or replayed",
                    )
                result = await operation(connection)
                still_valid = await connection.fetchval(
                    "SELECT clock_timestamp() < $1::timestamptz",
                    deadline,
                )
                if still_valid is not True:
                    raise AdminProblemError(
                        403, "debug_session_inactive", "debug grant expired during read"
                    )
                return result

        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise AdminProblemError(
                403, "debug_session_inactive", "debug grant expired before read"
            )
        try:
            return await asyncio.wait_for(database_read(), timeout=remaining)
        except TimeoutError as exc:
            raise AdminProblemError(
                403, "debug_session_inactive", "debug grant expired during read"
            ) from exc

    async def persist_if_current(
        self,
        *,
        store: Any,
        exchange: Any,
        expected: Mapping[str, Any],
        audit_store: Any | None,
        audit: Mapping[str, Any],
    ) -> bool:
        """Commit one bounded capture and its audit under the session lock."""
        session_id = str(expected["session_id"])
        exact = {
            field: expected[field]
            for field in (
                "activation_payload_sha256",
                "activation_started_at",
                "app_id",
                "broker_id",
                "cluster_id",
                "deployment_id",
                "model_id",
                "revocation_epoch",
                "sequence",
                "session_id",
                "signed_envelope_sha256",
                "tenant_id",
            )
        }
        if self.pool is None:
            async with self._lock:
                history = self._events.get(session_id, [])
                current = self._verified_projection(dict(history[-1])) if history else None
                if (
                    current is not None
                    and current["activation_payload_sha256"]
                    in self._revoked_activations
                ):
                    current = None
                if current is not None:
                    current = {
                        **current,
                        "activation_started_at": history[0]["event_at"],
                    }
                if not self._current_matches(current, exact):
                    return False
                await store.record(exchange)
                if audit_store is not None:
                    await audit_store.append_audit_event(**dict(audit))
                return True
        if getattr(store, "pool", None) is not self.pool or (
            audit_store is not None and getattr(audit_store, "pool", None) is not self.pool
        ):
            return False
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", session_id
            )
            row = await connection.fetchrow(
                """
                SELECT current.*, genesis.event_at AS activation_started_at
                  FROM fs2_request_debug_session_current current
                 JOIN fs2_request_debug_activation_events genesis
                    ON genesis.session_id=current.session_id AND genesis.sequence=1
                 WHERE current.session_id=$1
                   AND NOT EXISTS (
                     SELECT 1 FROM fs2_request_debug_activation_revocations revoked
                      WHERE revoked.activation_payload_sha256=current.activation_payload_sha256
                   )
                """,
                session_id,
            )
            current = self._verified_projection(dict(row)) if row is not None else None
            if current is not None:
                current = {
                    **current,
                    "activation_started_at": row["activation_started_at"],
                }
            if not self._current_matches(current, exact):
                return False
            record_on_connection = getattr(store, "record_on_connection", None)
            audit_on_connection = getattr(audit_store, "append_audit_event_on_connection", None)
            if not callable(record_on_connection) or (
                audit_store is not None and not callable(audit_on_connection)
            ):
                return False
            await record_on_connection(connection, exchange)
            if audit_store is not None:
                await audit_on_connection(connection, **dict(audit))
            return True

    async def _capture_scope(
        self,
        tenant_id: str,
        model_id: str | None,
        *,
        admitted_at: datetime | None = None,
    ) -> Mapping[str, Any] | None:
        now = datetime.now(UTC)
        if self.pool is None:
            async with self._lock:
                candidates = [
                    (dict(events[-1]), events[0]["event_at"])
                    for events in self._events.values()
                    if events
                    and events[-1]["activation_payload_sha256"]
                    not in self._revoked_activations
                ]
        else:
            async with self.pool.acquire() as connection:
                fetched = await connection.fetch(
                    """
                    SELECT current.*, genesis.event_at AS activation_started_at
                      FROM fs2_request_debug_session_current current
                      JOIN fs2_request_debug_activation_events genesis
                        ON genesis.session_id=current.session_id AND genesis.sequence=1
                     WHERE current.event_type!='revoked'
                       AND current.activation_expires_at>clock_timestamp()
                       AND current.tenant_id=$1
                       AND ($2::text IS NULL OR current.model_id=$2)
                       AND NOT EXISTS (
                         SELECT 1 FROM fs2_request_debug_activation_revocations revoked
                          WHERE revoked.activation_payload_sha256=current.activation_payload_sha256
                       )
                    """,
                    tenant_id, model_id,
                )
            candidates = [
                (dict(row), row["activation_started_at"])
                for row in fetched
            ]
        rows = []
        for raw, activation_started_at in candidates:
            current = self._verified_projection(raw)
            if (
                current is not None
                and current["event_type"] != "revoked"
                and current["activation_expires_at"] > now
                and current["tenant_id"] == tenant_id
                and (model_id is None or current["model_id"] == model_id)
                and (
                    admitted_at is None
                    or activation_started_at
                    <= admitted_at
                    < current["activation_expires_at"]
                )
            ):
                rows.append(
                    {
                        **current,
                        "activation_started_at": activation_started_at,
                    }
                )
        if len(rows) != 1:
            return None
        return dict(rows[0])

    async def capture_scope(
        self,
        tenant_id: str,
        model_id: str,
        *,
        admitted_at: datetime | None = None,
    ) -> Mapping[str, Any] | None:
        return await self._capture_scope(
            tenant_id,
            model_id,
            admitted_at=admitted_at,
        )

    async def capture_candidate(self, tenant_id: str) -> Mapping[str, Any] | None:
        """Return one current tenant scope before any request bytes are observed."""
        return await self._capture_scope(tenant_id, None)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _decode_b64url(value: object, label: str, expected_bytes: int | None = None) -> bytes:
    if not isinstance(value, str) or not value or len(value) > MAX_TOKEN_BYTES:
        raise AdminProblemError(403, "debug_activation_invalid", f"{label} is malformed")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise AdminProblemError(403, "debug_activation_invalid", f"{label} is malformed") from exc
    if expected_bytes is not None and len(decoded) != expected_bytes:
        raise AdminProblemError(403, "debug_activation_invalid", f"{label} is malformed")
    return decoded


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise AdminProblemError(403, "debug_activation_invalid", f"{label} is malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdminProblemError(403, "debug_activation_invalid", f"{label} is malformed") from exc
    if parsed.tzinfo is None or parsed.microsecond != 0:
        raise AdminProblemError(403, "debug_activation_invalid", f"{label} is malformed")
    return parsed


class RequestDebugAuthorization:
    """Verify one non-exported broker assertion on every debug read."""

    def __init__(
        self,
        trust_json: str,
        *,
        expected_cluster_id: str,
        expected_deployment_id: str,
        sessions: RequestDebugSessionRegistry,
    ) -> None:
        try:
            trust = json.loads(trust_json)
        except json.JSONDecodeError as exc:
            raise ValueError("request-debug proxy trust is malformed") from exc
        if (
            not isinstance(trust, dict)
            or set(trust) != {"brokers", "issuers", "schema"}
            or trust.get("schema") != TRUST_SCHEMA
            or not isinstance(trust.get("issuers"), list)
            or not isinstance(trust.get("brokers"), list)
            or _canonical_bytes(trust).decode("utf-8") != trust_json
        ):
            raise ValueError("request-debug proxy trust is not canonical v1")
        issuers: dict[tuple[str, str], Mapping[str, Any]] = {}
        broker_fields = {
            "cluster_ids",
            "config_path",
            "config_sha256",
            "executable_path",
            "executable_sha256",
            "id",
            "isolation_mode",
            "key_id",
            "listener_modes",
            "modes",
            "namespace_policy_sha256",
            "network_namespace_owner_uid",
            "peer_credential_mode",
            "peer_gid",
            "peer_uid",
            "public_key",
            "role",
            "runtime_review_sha256",
            "scope_policy_sha256",
            "socket_path",
        }
        for raw in trust["brokers"]:
            if (
                not isinstance(raw, dict)
                or set(raw) != broker_fields
                or raw.get("role") != "internal-proxy-session-broker"
                or not isinstance(raw.get("id"), str)
                or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", raw["id"]) is None
                or raw.get("modes") != ["debug", "ordinary"]
                or raw.get("listener_modes")
                != ["debug-read-only", "ordinary-authenticated"]
                or raw.get("isolation_mode")
                != "private-network-namespace+broker-owned-proxy"
                or raw.get("network_namespace_owner_uid") != 0
                or raw.get("peer_uid") != 0
                or not isinstance(raw.get("cluster_ids"), list)
                or raw["cluster_ids"] != sorted(set(raw["cluster_ids"]))
                or any(
                    not isinstance(raw.get(field), str)
                    or re.fullmatch(r"[a-f0-9]{64}", raw[field]) is None
                    or raw[field] == "0" * 64
                    for field in (
                        "config_sha256",
                        "executable_sha256",
                        "namespace_policy_sha256",
                        "runtime_review_sha256",
                        "scope_policy_sha256",
                    )
                )
            ):
                raise ValueError("request-debug proxy issuer is malformed")
            try:
                public_key = _decode_b64url(
                    raw.get("public_key"), "issuer public key", 32
                )
            except AdminProblemError as exc:
                raise ValueError("request-debug proxy issuer key is malformed") from exc
            expected_key_id = "sha256:" + hashlib.sha256(public_key).hexdigest()
            identity = (str(raw.get("id")), str(raw.get("key_id")))
            if raw.get("key_id") != expected_key_id or identity in issuers:
                raise ValueError("request-debug proxy issuer identity is malformed")
            issuers[identity] = {
                **raw,
                "broker_id": raw["id"],
                "decoded_public_key": public_key,
            }
        self._issuers = issuers
        self.expected_cluster_id = expected_cluster_id
        self.expected_deployment_id = expected_deployment_id
        self.sessions = sessions
        self.sessions.event_verifier = self._reopen_session_event

    def _verify_signed_envelope(
        self, envelope: object, *, schema: str, label: str
    ) -> tuple[dict[str, Any], Mapping[str, Any], str]:
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"payload", "payload_sha256", "schema", "signature"}
            or envelope.get("schema") != schema
            or not isinstance(envelope.get("payload"), dict)
        ):
            raise AdminProblemError(403, "debug_activation_invalid", f"{label} is malformed")
        payload = envelope["payload"]
        payload_raw = _canonical_bytes(payload)
        payload_sha256 = hashlib.sha256(payload_raw).hexdigest()
        if envelope["payload_sha256"] != payload_sha256:
            raise AdminProblemError(403, "debug_activation_invalid", f"{label} digest differs")
        issuer = payload.get("issuer")
        if not isinstance(issuer, dict) or set(issuer) != {"id", "key_id"}:
            raise AdminProblemError(403, "debug_activation_invalid", f"{label} issuer is malformed")
        authority = self._issuers.get((str(issuer["id"]), str(issuer["key_id"])))
        if authority is None:
            raise AdminProblemError(403, "debug_activation_untrusted", f"{label} is untrusted")
        signature = _decode_b64url(envelope["signature"], f"{label} signature", 64)
        try:
            Ed25519PublicKey.from_public_bytes(authority["decoded_public_key"]).verify(
                signature,
                _canonical_bytes(
                    {
                        "payload": payload,
                        "payload_sha256": payload_sha256,
                        "schema": schema,
                    }
                ),
            )
        except InvalidSignature as exc:
            raise AdminProblemError(403, "debug_activation_invalid", f"{label} signature is invalid") from exc
        return payload, authority, payload_sha256

    @staticmethod
    def _terminal_receipt_is_exact(
        receipt: object, payload: Mapping[str, Any]
    ) -> bool:
        return (
            isinstance(receipt, dict)
            and set(receipt)
            == {
                "backend_authorization_revoked",
                "children_reaped",
                "connections_closed",
                "expires_at",
                "listener_closed",
                "namespace_destroyed",
                "raw_transports_closed",
                "reason",
                "schema",
                "session_id",
                "teardown_at",
            }
            and receipt.get("schema") == TERMINAL_RECEIPT_SCHEMA
            and receipt.get("session_id") == payload.get("session_id")
            and receipt.get("expires_at") == payload.get("activation_expires_at")
            and receipt.get("teardown_at") == payload.get("event_at")
            and receipt.get("backend_authorization_revoked") is True
            and receipt.get("children_reaped") is True
            and receipt.get("connections_closed") is True
            and receipt.get("listener_closed") is True
            and receipt.get("namespace_destroyed") is True
            and receipt.get("raw_transports_closed") is True
            and isinstance(receipt.get("reason"), str)
            and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", receipt["reason"]) is not None
        )

    def _validate_session_event(
        self,
        envelope: object,
        *,
        require_fresh: bool,
    ) -> tuple[dict[str, Any], str]:
        payload, authority, payload_sha256 = self._verify_signed_envelope(
            envelope, schema=SESSION_EVENT_SCHEMA, label="debug session event"
        )
        if set(payload) != {
            "activation_expires_at",
            "activation_payload_sha256",
            "app_id",
            "broker_id",
            "cluster_id",
            "deployment_id",
            "event_at",
            "event_type",
            "issuer",
            "model_id",
            "prior_event_sha256",
            "revocation_epoch",
            "schema",
            "sequence",
            "session_id",
            "tenant_id",
            "terminal_teardown_receipt",
        }:
            raise AdminProblemError(
                403,
                "debug_activation_invalid",
                "debug session event scope is malformed",
            )
        event_at = _timestamp(payload["event_at"], "debug session event_at")
        activation_expires_at = _timestamp(
            payload["activation_expires_at"], "debug activation expires_at"
        )
        now = datetime.now(UTC)
        try:
            UUID(str(payload["app_id"]))
        except ValueError as exc:
            raise AdminProblemError(
                403, "debug_activation_invalid", "debug session App is malformed"
            ) from exc
        event_type = payload["event_type"]
        sequence = payload["sequence"]
        revocation_epoch = payload["revocation_epoch"]
        prior = payload["prior_event_sha256"]
        terminal = payload["terminal_teardown_receipt"]
        genesis = event_type == "activated" and sequence == 1
        successor = event_type in {"heartbeat", "revoked"} and sequence > 1
        if (
            payload["schema"] != SESSION_EVENT_SCHEMA
            or authority["broker_id"] != payload["broker_id"]
            or payload["cluster_id"] != self.expected_cluster_id
            or payload["deployment_id"] != self.expected_deployment_id
            or payload["cluster_id"] not in authority["cluster_ids"]
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
            or not isinstance(revocation_epoch, int)
            or isinstance(revocation_epoch, bool)
            or revocation_epoch < 0
            or (require_fresh and abs((now - event_at).total_seconds()) > 30)
            or (
                event_type != "revoked"
                and event_at > activation_expires_at
            )
            or (
                event_type == "revoked"
                and event_at > activation_expires_at + MAX_TERMINAL_DELIVERY
            )
            or activation_expires_at - event_at > MAX_ACTIVATION
            or re.fullmatch(
                r"[a-f0-9]{64}", str(payload["activation_payload_sha256"])
            )
            is None
            or payload["activation_payload_sha256"] == "0" * 64
            or re.fullmatch(r"[a-f0-9]{32,64}", str(payload["session_id"])) is None
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(payload["tenant_id"])
            )
            is None
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}", str(payload["model_id"])
            )
            is None
            or (genesis and (prior is not None or revocation_epoch != 0 or terminal is not None))
            or (
                successor
                and (
                    not isinstance(prior, str)
                    or re.fullmatch(r"[a-f0-9]{64}", prior) is None
                    or prior == "0" * 64
                )
            )
            or (not genesis and not successor)
            or (event_type == "heartbeat" and (revocation_epoch != 0 or terminal is not None))
            or (
                event_type == "revoked"
                and (
                    revocation_epoch < 1
                    or not self._terminal_receipt_is_exact(terminal, payload)
                )
            )
        ):
            raise AdminProblemError(
                403,
                "debug_scope_mismatch",
                "debug session event is outside this deployment",
            )
        return payload, payload_sha256

    def _reopen_session_event(self, envelope: object) -> Mapping[str, Any]:
        payload, _payload_sha256 = self._validate_session_event(
            envelope, require_fresh=False
        )
        return payload

    async def accept_session_event(
        self,
        envelope: object,
        *,
        app_public_model: Callable[[UUID], Awaitable[str]],
        audit_store: Any | None = None,
    ) -> tuple[Mapping[str, Any], str]:
        payload, payload_sha256 = self._validate_session_event(
            envelope, require_fresh=True
        )
        if (
            payload["event_type"] == "activated"
            and await app_public_model(UUID(str(payload["app_id"])))
            != payload["model_id"]
        ):
            raise AdminProblemError(
                403, "debug_scope_mismatch", "debug session App and public model do not match"
            )
        if not isinstance(envelope, dict):
            raise AdminProblemError(
                403, "debug_activation_invalid", "debug session event is malformed"
            )
        event_digest = await self.sessions.append(
            envelope,
            payload,
            payload_sha256,
            audit_store=audit_store,
            audit={
                "actor": str(payload["broker_id"]),
                "tenant_id": str(payload["tenant_id"]),
                "token_id": None,
                "action": f"request.debug.{payload['event_type']}",
                "target_type": "request_debug_session",
                "target_id": str(payload["session_id"]),
                "outcome": "succeeded",
                "detail": {
                    "activation_payload_sha256": str(
                        payload["activation_payload_sha256"]
                    ),
                    "app_id": str(payload["app_id"]),
                    "deployment_id": str(payload["deployment_id"]),
                    "model_id": str(payload["model_id"]),
                    "revocation_epoch": int(payload["revocation_epoch"]),
                    "sequence": int(payload["sequence"]),
                },
            },
        )
        return payload, event_digest

    def _validate_activation_tombstone(
        self, envelope: object
    ) -> tuple[dict[str, Any], str]:
        payload, authority, payload_sha256 = self._verify_signed_envelope(
            envelope,
            schema=ACTIVATION_TOMBSTONE_SCHEMA,
            label="debug activation tombstone",
        )
        if set(payload) != {
            "activation_expires_at",
            "activation_payload_sha256",
            "app_id",
            "broker_id",
            "cluster_id",
            "deployment_id",
            "issuer",
            "model_id",
            "reason",
            "revocation_epoch",
            "revoked_at",
            "schema",
            "sequence",
            "session_id",
            "tenant_id",
            "terminal_teardown_receipt",
        }:
            raise AdminProblemError(
                403,
                "debug_activation_invalid",
                "debug activation tombstone scope is malformed",
            )
        revoked_at = _timestamp(
            payload["revoked_at"], "debug activation revoked_at"
        )
        activation_expires_at = _timestamp(
            payload["activation_expires_at"], "debug activation expires_at"
        )
        try:
            UUID(str(payload["app_id"]))
        except ValueError as exc:
            raise AdminProblemError(
                403,
                "debug_activation_invalid",
                "debug activation tombstone App is malformed",
            ) from exc
        if (
            payload["schema"] != ACTIVATION_TOMBSTONE_SCHEMA
            or authority["broker_id"] != payload["broker_id"]
            or payload["cluster_id"] != self.expected_cluster_id
            or payload["deployment_id"] != self.expected_deployment_id
            or payload["cluster_id"] not in authority["cluster_ids"]
            or abs((datetime.now(UTC) - revoked_at).total_seconds()) > 30
            or revoked_at > activation_expires_at + MAX_TERMINAL_DELIVERY
            or payload["revocation_epoch"] != 1
            or not isinstance(payload["sequence"], int)
            or isinstance(payload["sequence"], bool)
            or payload["sequence"] < 1
            or re.fullmatch(r"[a-f0-9]{32,64}", str(payload["session_id"])) is None
            or re.fullmatch(
                r"[a-f0-9]{64}", str(payload["activation_payload_sha256"])
            )
            is None
            or payload["activation_payload_sha256"] == "0" * 64
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(payload["tenant_id"])
            )
            is None
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}", str(payload["model_id"])
            )
            is None
            or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", str(payload["reason"]))
            is None
            or not isinstance(payload["terminal_teardown_receipt"], dict)
            or payload["terminal_teardown_receipt"].get("reason")
            != payload["reason"]
            or not self._terminal_receipt_is_exact(
                payload["terminal_teardown_receipt"],
                {
                    **payload,
                    "event_at": payload["revoked_at"],
                },
            )
        ):
            raise AdminProblemError(
                403,
                "debug_scope_mismatch",
                "debug activation tombstone is outside this deployment",
            )
        return payload, payload_sha256

    async def accept_activation_tombstone(
        self,
        envelope: object,
        *,
        audit_store: Any | None = None,
    ) -> tuple[Mapping[str, Any], str]:
        payload, payload_sha256 = self._validate_activation_tombstone(envelope)
        if not isinstance(envelope, dict):
            raise AdminProblemError(
                403,
                "debug_activation_invalid",
                "debug activation tombstone is malformed",
            )
        event_digest = await self.sessions.revoke_activation(
            envelope,
            payload,
            payload_sha256,
            audit_store=audit_store,
            audit={
                "actor": str(payload["broker_id"]),
                "tenant_id": str(payload["tenant_id"]),
                "token_id": None,
                "action": "request.debug.disable",
                "target_type": "request_debug_activation",
                "target_id": str(payload["activation_payload_sha256"]),
                "outcome": "succeeded",
                "detail": {
                    "app_id": str(payload["app_id"]),
                    "deployment_id": str(payload["deployment_id"]),
                    "model_id": str(payload["model_id"]),
                    "reason": str(payload["reason"]),
                    "revocation_epoch": int(payload["revocation_epoch"]),
                },
            },
        )
        return payload, event_digest

    async def authorize(
        self,
        request: Request,
        *,
        app_id: UUID | None,
        public_model_id: str | None,
    ) -> Mapping[str, Any]:
        encoded_values = request.headers.getlist("x-fs2-debug-authorization")
        if len(encoded_values) != 1:
            raise AdminProblemError(
                403,
                "debug_activation_required",
                "request-debug reads require one active signed debug grant",
            )
        encoded = encoded_values[0]
        if "," in encoded:
            raise AdminProblemError(403, "debug_activation_invalid", "debug grant is ambiguous")
        token_raw = _decode_b64url(encoded, "debug grant")
        channel_values = request.headers.getlist("x-fs2-debug-channel-binding")
        if len(channel_values) != 1 or "," in channel_values[0]:
            raise AdminProblemError(
                403,
                "debug_activation_untrusted",
                "debug grant requires one broker-private channel binding",
            )
        channel_binding = _decode_b64url(
            channel_values[0], "debug channel binding", 32
        )
        try:
            envelope = json.loads(token_raw)
        except json.JSONDecodeError as exc:
            raise AdminProblemError(403, "debug_activation_invalid", "debug grant is malformed") from exc
        payload, authority, _payload_sha256 = self._verify_signed_envelope(
            envelope, schema=TOKEN_SCHEMA, label="debug grant"
        )
        if set(payload) != {
            "activation_expires_at",
            "activation_payload_sha256",
            "app_id",
            "audience",
            "broker_id",
            "channel_binding_sha256",
            "cluster_id",
            "deployment_id",
            "event_sequence",
            "expires_at",
            "issued_at",
            "issuer",
            "http_method",
            "methods",
            "model_id",
            "request_nonce",
            "request_target_sha256",
            "revocation_epoch",
            "schema",
            "session_id",
            "scope_policy_sha256",
            "tenant_id",
        }:
            raise AdminProblemError(403, "debug_activation_invalid", "debug grant scope is malformed")
        if (
            authority["broker_id"] != payload["broker_id"]
            or payload["cluster_id"] != self.expected_cluster_id
            or payload["deployment_id"] != self.expected_deployment_id
            or payload["cluster_id"] not in authority["cluster_ids"]
            or payload["scope_policy_sha256"] != authority["scope_policy_sha256"]
        ):
            raise AdminProblemError(403, "debug_activation_untrusted", "debug grant is untrusted")
        issued_at = _timestamp(payload["issued_at"], "debug grant issued_at")
        expires_at = _timestamp(payload["expires_at"], "debug grant expires_at")
        activation_expires_at = _timestamp(
            payload["activation_expires_at"], "debug activation expires_at"
        )
        now = datetime.now(UTC)
        raw_path = request.scope.get("raw_path")
        if not isinstance(raw_path, bytes):
            raw_path = request.url.path.encode("utf-8")
        raw_query = request.scope.get("query_string", b"")
        if not isinstance(raw_query, bytes):
            raw_query = b""
        request_target_sha256 = hashlib.sha256(
            request.method.encode("ascii") + b"\n" + raw_path + b"\n" + raw_query
        ).hexdigest()
        if (
            payload["schema"] != TOKEN_SCHEMA
            or payload["audience"] != AUDIENCE
            or payload["methods"] != ["GET", "HEAD"]
            or payload["http_method"] != request.method
            or request.method not in payload["methods"]
            or payload["request_target_sha256"] != request_target_sha256
            or expires_at > activation_expires_at
            or not issued_at <= now < expires_at
            or expires_at - issued_at > MAX_REQUEST_GRANT
            or not isinstance(payload["activation_payload_sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", payload["activation_payload_sha256"]) is None
            or payload["activation_payload_sha256"] == "0" * 64
            or not isinstance(payload["cluster_id"], str)
            or re.fullmatch(r"mk8scluster-[a-z0-9]+", payload["cluster_id"]) is None
            or not isinstance(payload["scope_policy_sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", payload["scope_policy_sha256"]) is None
            or payload["scope_policy_sha256"] == "0" * 64
            or re.fullmatch(r"[a-f0-9]{64}", str(payload["channel_binding_sha256"])) is None
            or payload["channel_binding_sha256"] == "0" * 64
            or payload["channel_binding_sha256"]
            != hashlib.sha256(channel_binding).hexdigest()
            or not isinstance(payload["session_id"], str)
            or re.fullmatch(r"[a-f0-9]{32,64}", payload["session_id"]) is None
            or re.fullmatch(r"[a-f0-9]{32,128}", str(payload["request_nonce"])) is None
            or not isinstance(payload["event_sequence"], int)
            or isinstance(payload["event_sequence"], bool)
            or payload["event_sequence"] < 1
            or not isinstance(payload["revocation_epoch"], int)
            or isinstance(payload["revocation_epoch"], bool)
            or payload["revocation_epoch"] < 0
            or not isinstance(payload["tenant_id"], str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", payload["tenant_id"]) is None
            or not isinstance(payload["model_id"], str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}", payload["model_id"]) is None
            or (app_id is not None and payload["app_id"] != str(app_id))
            or (public_model_id is not None and payload["model_id"] != public_model_id)
        ):
            raise AdminProblemError(403, "debug_scope_mismatch", "request is outside its signed scope")
        try:
            UUID(str(payload["app_id"]))
        except ValueError as exc:
            raise AdminProblemError(403, "debug_scope_mismatch", "debug App scope is malformed") from exc
        return payload

    async def execute(
        self,
        payload: Mapping[str, Any],
        operation: Callable[[Any | None], Awaitable[Any]],
    ) -> Any:
        """Run the materialized read while revocation is serialized."""
        return await self.sessions.authorize_and_consume(payload, operation)

"""External, signed activation fence for generational storage reconcilers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import ssl
import stat
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class ReconcilerQuiesced(RuntimeError):
    """The external owner has not activated this immutable generation."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: object, *args: object, **kwargs: object) -> None:
        raise ValueError("activation authority redirects are forbidden")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _read_regular(path: Path, maximum: int = 64 * 1024) -> bytes:
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        before = os.fstat(descriptor)
        payload = os.read(descriptor, maximum + 1)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not before.st_size
            or before.st_size > maximum
            or len(payload) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ValueError("activation trust file changed during bounded read")
        return payload
    finally:
        os.close(descriptor)


def _bound_receipt(value: object, schema: str, cluster_id: str) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "receipt_id",
        "issuer",
        "observed_at",
        "valid_until",
        "cluster_id",
        "subject_generations",
        "object_identities",
        "observation_source",
        "outcome",
        "detail",
    }:
        return False
    issuer = value.get("issuer")
    try:
        observed_at = datetime.fromisoformat(
            str(value.get("observed_at")).replace("Z", "+00:00")
        )
        valid_until = datetime.fromisoformat(
            str(value.get("valid_until")).replace("Z", "+00:00")
        )
    except ValueError:
        return False
    detail = value.get("detail")
    semantic_result = {
        "fs2-serve.nebius.ai/storage-reconciler-zero-inflight-observation/v1": (
            isinstance(detail, dict)
            and detail.get("nonterminal_provider_operations") == 0
        ),
        "fs2-serve.nebius.ai/storage-reconciler-schema-compatibility/v1": (
            isinstance(detail, dict) and detail.get("compatible") is True
        ),
        "fs2-serve.nebius.ai/storage-reconciler-provider-continuity/v1": (
            isinstance(detail, dict) and detail.get("continuous") is True
        ),
    }.get(schema, False)
    now = datetime.now(UTC)
    object_identities = value.get("object_identities")
    return bool(
        value.get("schema") == schema
        and value.get("receipt_id")
        == hashlib.sha256(
            _canonical({key: item for key, item in value.items() if key != "receipt_id"})
        ).hexdigest()
        and value.get("cluster_id") == cluster_id
        and value.get("outcome") == "PASS"
        and isinstance(issuer, dict)
        and set(issuer) == {"username", "uid", "groups"}
        and issuer.get("username")
        and issuer.get("uid")
        and issuer.get("groups") == sorted(set(issuer.get("groups") or []))
        and isinstance(value.get("object_identities"), list)
        and object_identities
        and all(
            isinstance(item, dict)
            and set(item) == {"kind", "id", "resource_version", "sha256"}
            and all(item.get(field) for field in ("kind", "id", "resource_version"))
            and isinstance(item.get("sha256"), str)
            and len(item["sha256"]) == 64
            for item in object_identities
        )
        and len({(item["kind"], item["id"]) for item in object_identities})
        == len(object_identities)
        and isinstance(value.get("subject_generations"), list)
        and value["subject_generations"]
        and value["subject_generations"]
        == sorted(set(value["subject_generations"]))
        and isinstance(value.get("observation_source"), str)
        and value["observation_source"]
        and observed_at.tzinfo is not None
        and valid_until.tzinfo is not None
        and observed_at.astimezone(UTC) <= now < valid_until.astimezone(UTC)
        and (valid_until - observed_at).total_seconds() <= 900
        and semantic_result
    )


class StorageReconcilerActivationFence:
    def __init__(
        self,
        *,
        endpoint: str,
        generation: str,
        cluster_id: str,
        authority_manifest_sha256: str,
        image_digest: str,
        cutover_receipt_sha256: str,
        public_key_file: Path,
        ca_file: Path,
        minimum_epoch: int,
    ) -> None:
        self.endpoint = endpoint
        self.generation = generation
        self.cluster_id = cluster_id
        self.authority_manifest_sha256 = authority_manifest_sha256
        self.image_digest = image_digest
        self.registry_anchor_sha256 = cutover_receipt_sha256
        self.public_key_file = public_key_file
        self.ca_file = ca_file
        self.minimum_epoch = minimum_epoch
        self.observed_epoch = 0
        self.observed_head = ""
        self.observed_predecessor_head = ""

    def _fetch(self) -> dict[str, Any]:
        context = ssl.create_default_context(cadata=_read_regular(self.ca_file).decode("ascii"))
        request = urllib.request.Request(
            self.endpoint,
            headers={
                "Accept": "application/json",
                "User-Agent": "fs2-storage-reconciler-activation/1",
            },
            method="GET",
        )
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context), _NoRedirect()
        )
        with opener.open(request, timeout=5) as response:
            if response.geturl() != self.endpoint:
                raise ValueError("activation authority endpoint changed during retrieval")
            if response.status != 200 or response.headers.get_content_type() != "application/json":
                raise ValueError("activation authority returned a non-canonical response")
            payload = response.read(128 * 1024 + 1)
        if not payload or len(payload) > 128 * 1024:
            raise ValueError("activation authority response is absent or oversized")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("activation authority response is not an object")
        return value

    def _verify(self, envelope: dict[str, Any]) -> dict[str, Any]:
        if set(envelope) != {"body", "payload_sha256", "signature"} or not isinstance(
            envelope.get("body"), dict
        ):
            raise ValueError("activation envelope fields differ")
        body = envelope["body"]
        fields = {
            "schema",
            "cluster_id",
            "activation_epoch",
            "predecessor_epoch",
            "target_generation",
            "target_image_digest",
            "authority_manifest_sha256",
            "cutover_receipt_sha256",
            "phase",
            "valid_from",
            "valid_until",
            "rollback",
            "predecessor_head_sha256",
            "ledger_anchor_sha256",
            "state_head_sha256",
            "source_bundle_sha256",
            "enforcer_image_digest",
            "provider_drain_intent",
            "provider_drain_intent_sha256",
            "provider_drain_receipt",
            "provider_drain_receipt_sha256",
        }
        payload_sha256 = hashlib.sha256(_canonical(body)).hexdigest()
        if (
            set(body) != fields
            or body.get("schema")
            != "fs2-serve.nebius.ai/storage-reconciler-activation/v1"
            or envelope.get("payload_sha256") != payload_sha256
            or body.get("cluster_id") != self.cluster_id
            or body.get("authority_manifest_sha256")
            != self.authority_manifest_sha256
            or body.get("phase") not in {"ACTIVE", "DRAINING", "QUIESCED"}
            or not isinstance(body.get("activation_epoch"), int)
            or body["activation_epoch"] < self.minimum_epoch
            or not isinstance(body.get("predecessor_epoch"), int)
            or body["predecessor_epoch"] != body["activation_epoch"] - 1
            or (
                body.get("phase") in {"ACTIVE", "DRAINING"}
                and (
                    not isinstance(body.get("target_generation"), str)
                    or not isinstance(body.get("target_image_digest"), str)
                    or body["target_image_digest"] != self.image_digest
                )
            )
            or (
                body.get("phase") == "QUIESCED"
                and (
                    body.get("target_generation") is not None
                    or body.get("target_image_digest") is not None
                )
            )
            or not isinstance(body.get("cutover_receipt_sha256"), str)
            or len(body["cutover_receipt_sha256"]) != 64
            or body.get("ledger_anchor_sha256") != self.registry_anchor_sha256
            or not isinstance(body.get("state_head_sha256"), str)
            or len(body["state_head_sha256"]) != 64
            or not isinstance(body.get("source_bundle_sha256"), str)
            or len(body["source_bundle_sha256"]) != 64
            or not isinstance(body.get("enforcer_image_digest"), str)
            or "@sha256:" not in body["enforcer_image_digest"]
        ):
            raise ValueError("activation contract identity differs")
        drain_intent = body.get("provider_drain_intent")
        drain_receipt = body.get("provider_drain_receipt")
        try:
            drain_requested_at = (
                datetime.fromisoformat(
                    str((drain_intent or {}).get("requested_at")).replace("Z", "+00:00")
                )
                if isinstance(drain_intent, dict)
                else None
            )
            drain_deadline_at = (
                datetime.fromisoformat(
                    str((drain_intent or {}).get("deadline_at")).replace("Z", "+00:00")
                )
                if isinstance(drain_intent, dict)
                else None
            )
        except ValueError:
            drain_requested_at = None
            drain_deadline_at = None
        if drain_intent is not None and (
            not isinstance(drain_intent, dict)
            or set(drain_intent)
            != {
                "schema",
                "drain_id",
                "reconciler_generation",
                "requested_at",
                "deadline_at",
                "max_provider_action_seconds",
                "termination_grace_seconds",
                "activation_epoch",
                "activation_state_head_sha256",
                "transition_id",
            }
            or drain_intent.get("schema")
            != "fs2-serve.nebius.ai/storage-reconciler-provider-drain-intent/v1"
            or body.get("provider_drain_intent_sha256")
            != hashlib.sha256(_canonical(drain_intent)).hexdigest()
            or (
                body.get("phase") == "DRAINING"
                and drain_intent.get("reconciler_generation")
                != body.get("target_generation")
            )
            or (
                body.get("phase") == "DRAINING"
                and drain_intent.get("activation_epoch") != body["activation_epoch"]
            )
            or (
                body.get("phase") == "DRAINING"
                and drain_intent.get("activation_state_head_sha256")
                != body["state_head_sha256"]
            )
            or (
                body.get("phase") == "DRAINING"
                and drain_intent.get("transition_id")
                != body["cutover_receipt_sha256"]
            )
            or drain_intent.get("max_provider_action_seconds") != 120
            or not isinstance(drain_intent.get("termination_grace_seconds"), int)
            or not 120 < drain_intent["termination_grace_seconds"] <= 600
            or drain_requested_at is None
            or drain_deadline_at is None
            or drain_requested_at.tzinfo is None
            or drain_deadline_at.tzinfo is None
            or (drain_deadline_at - drain_requested_at).total_seconds()
            != drain_intent["termination_grace_seconds"]
        ):
            raise ValueError("activation drain intent differs from its signed epoch")
        if body["phase"] == "DRAINING" and (
            drain_intent is None
            or drain_receipt is not None
            or body.get("provider_drain_receipt_sha256") is not None
            or drain_requested_at.astimezone(UTC) > datetime.now(UTC)
            or drain_deadline_at.astimezone(UTC) <= datetime.now(UTC)
        ):
            raise ValueError("draining activation lacks an uncompleted signed intent")
        if drain_receipt is not None and (
            not isinstance(drain_receipt, dict)
            or set(drain_receipt)
            != {
                "schema",
                "receipt_id",
                "issuer",
                "observed_at",
                "valid_until",
                "cluster_id",
                "drain_id",
                "reconciler_generation",
                "activation_epoch",
                "activation_state_head_sha256",
                "transition_id",
                "database_receipt",
                "database_receipt_sha256",
                "provider_operation_ledger_head_sha256",
                "provider_operation_ids",
                "nonterminal_provider_operations",
            }
            or drain_receipt.get("schema")
            != "fs2-serve.nebius.ai/storage-reconciler-provider-drain-attestation/v1"
            or drain_receipt.get("receipt_id")
            != hashlib.sha256(
                _canonical(
                    {key: item for key, item in drain_receipt.items() if key != "receipt_id"}
                )
            ).hexdigest()
            or body.get("provider_drain_receipt_sha256")
            != hashlib.sha256(_canonical(drain_receipt)).hexdigest()
            or drain_intent is None
            or drain_receipt.get("drain_id") != drain_intent.get("drain_id")
            or drain_receipt.get("cluster_id") != self.cluster_id
            or drain_receipt.get("reconciler_generation")
            != drain_intent.get("reconciler_generation")
            or drain_receipt.get("activation_epoch")
            != drain_intent.get("activation_epoch")
            or drain_receipt.get("activation_state_head_sha256")
            != drain_intent.get("activation_state_head_sha256")
            or drain_receipt.get("transition_id") != drain_intent.get("transition_id")
            or not isinstance(drain_receipt.get("database_receipt"), dict)
            or set(drain_receipt["database_receipt"])
            != {
                "schema",
                "drain_id",
                "reconciler_generation",
                "activation_epoch",
                "activation_state_head_sha256",
                "transition_id",
                "operation_cutoff_at",
                "nonterminal_provider_operations",
                "queued_user_actions",
                "queued_audit_actions",
                "operations",
                "postgres_jsonb_receipt_sha256",
            }
            or drain_receipt.get("database_receipt_sha256")
            != hashlib.sha256(_canonical(drain_receipt["database_receipt"])).hexdigest()
            or drain_receipt["database_receipt"].get("schema")
            != "fs2-serve.nebius.ai/storage-reconciler-provider-drain-receipt/v1"
            or drain_receipt["database_receipt"].get("drain_id")
            != drain_intent.get("drain_id")
            or drain_receipt["database_receipt"].get("reconciler_generation")
            != drain_intent.get("reconciler_generation")
            or drain_receipt["database_receipt"].get("activation_epoch")
            != drain_receipt.get("activation_epoch")
            or drain_receipt["database_receipt"].get(
                "activation_state_head_sha256"
            )
            != drain_receipt.get("activation_state_head_sha256")
            or drain_receipt["database_receipt"].get("transition_id")
            != drain_receipt.get("transition_id")
            or drain_receipt["database_receipt"].get(
                "nonterminal_provider_operations"
            )
            != 0
            or not isinstance(
                drain_receipt["database_receipt"].get("queued_user_actions"), int
            )
            or drain_receipt["database_receipt"]["queued_user_actions"] < 0
            or not isinstance(
                drain_receipt["database_receipt"].get("queued_audit_actions"), int
            )
            or drain_receipt["database_receipt"]["queued_audit_actions"] < 0
            or not isinstance(
                drain_receipt["database_receipt"].get("operations"), list
            )
            or any(
                not isinstance(operation, dict)
                or set(operation)
                != {
                    "operation_id",
                    "provider_idempotency_id",
                    "operation_kind",
                    "target_identity_sha256",
                    "status",
                    "superseded_by",
                    "provider_operations",
                }
                or not UUID_RE.fullmatch(str(operation.get("operation_id", "")))
                or not UUID_RE.fullmatch(
                    str(operation.get("provider_idempotency_id", ""))
                )
                or not isinstance(operation.get("operation_kind"), str)
                or not operation["operation_kind"]
                or not SHA256_RE.fullmatch(
                    str(operation.get("target_identity_sha256", ""))
                )
                or operation.get("status")
                not in {"succeeded", "failed_terminal", "superseded"}
                or (
                    operation.get("status") == "superseded"
                    and not UUID_RE.fullmatch(
                        str(operation.get("superseded_by", ""))
                    )
                )
                or (
                    operation.get("status") != "superseded"
                    and operation.get("superseded_by") is not None
                )
                or not isinstance(operation.get("provider_operations"), list)
                or any(
                    not isinstance(attempt, dict)
                    or set(attempt)
                    != {"provider_operation_id", "status", "provider_code"}
                    or not isinstance(attempt.get("provider_operation_id"), str)
                    or not attempt["provider_operation_id"]
                    or attempt.get("status")
                    not in {
                        "succeeded",
                        "failed_terminal",
                        "superseded_indeterminate",
                    }
                    for attempt in operation.get("provider_operations", [])
                )
                for operation in drain_receipt["database_receipt"]["operations"]
            )
            or drain_receipt.get("provider_operation_ledger_head_sha256")
            != hashlib.sha256(
                _canonical(drain_receipt["database_receipt"]["operations"])
            ).hexdigest()
            or drain_receipt.get("provider_operation_ids")
            != sorted(
                str(operation.get("operation_id", ""))
                for operation in drain_receipt["database_receipt"]["operations"]
            )
            or not isinstance(drain_receipt.get("provider_operation_ids"), list)
            or drain_receipt["provider_operation_ids"]
            != sorted(set(drain_receipt["provider_operation_ids"]))
            or any(
                not isinstance(value, str) or not UUID_RE.fullmatch(value)
                for value in drain_receipt["provider_operation_ids"]
            )
            or not isinstance(drain_receipt.get("issuer"), dict)
            or set(drain_receipt["issuer"]) != {"username", "uid", "groups"}
            or not drain_receipt["issuer"].get("username")
            or not drain_receipt["issuer"].get("uid")
            or drain_receipt["issuer"].get("groups")
            != sorted(set(drain_receipt["issuer"].get("groups") or []))
            or not isinstance(drain_receipt.get("database_receipt_sha256"), str)
            or len(drain_receipt["database_receipt_sha256"]) != 64
            or not isinstance(
                drain_receipt.get("provider_operation_ledger_head_sha256"), str
            )
            or len(drain_receipt["provider_operation_ledger_head_sha256"]) != 64
            or drain_receipt.get("nonterminal_provider_operations") != 0
        ):
            raise ValueError("activation provider-drain receipt is not exact and zero-terminal")
        if drain_receipt is not None:
            try:
                receipt_observed_at = datetime.fromisoformat(
                    str(drain_receipt["observed_at"]).replace("Z", "+00:00")
                )
                receipt_valid_until = datetime.fromisoformat(
                    str(drain_receipt["valid_until"]).replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ValueError("provider-drain receipt validity is not RFC3339") from exc
            now = datetime.now(UTC)
            if (
                receipt_observed_at.tzinfo is None
                or receipt_valid_until.tzinfo is None
                or receipt_observed_at.astimezone(UTC) > now
                or receipt_valid_until.astimezone(UTC) <= now
                or (receipt_valid_until - receipt_observed_at).total_seconds() > 900
            ):
                raise ValueError("provider-drain receipt is stale, future-dated, or unbounded")
        if drain_intent is None and any(
            body.get(field) is not None
            for field in (
                "provider_drain_intent_sha256",
                "provider_drain_receipt",
                "provider_drain_receipt_sha256",
            )
        ):
            raise ValueError("activation carries drain evidence without an intent")
        key = serialization.load_pem_public_key(_read_regular(self.public_key_file))
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("activation authority key is not Ed25519")
        try:
            key.verify(
                base64.b64decode(str(envelope["signature"]), validate=True),
                _canonical(body),
            )
        except (InvalidSignature, TypeError, ValueError) as exc:
            raise ValueError("activation authority signature is invalid") from exc
        try:
            valid_from = datetime.fromisoformat(
                str(body["valid_from"]).replace("Z", "+00:00")
            )
            valid_until = datetime.fromisoformat(
                str(body["valid_until"]).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError("activation validity is not RFC3339") from exc
        now = datetime.now(UTC)
        if (
            valid_from.tzinfo is None
            or valid_until.tzinfo is None
            or valid_from.astimezone(UTC) > now
            or valid_until.astimezone(UTC) <= now
        ):
            raise ValueError("activation authority response is not currently valid")
        rollback = body.get("rollback")
        if rollback is not None and (
            not isinstance(rollback, dict)
            or set(rollback)
            != {
                "from_epoch",
                "successor_quiesced",
                "successor_quiescence",
                "successor_quiescence_sha256",
                "zero_inflight_actions_receipt",
                "zero_inflight_actions_receipt_sha256",
                "schema_compatibility_receipt",
                "schema_compatibility_receipt_sha256",
                "provider_continuity_receipt",
                "provider_continuity_receipt_sha256",
            }
            or rollback.get("from_epoch") != body["predecessor_epoch"]
            or not isinstance(rollback.get("successor_quiesced"), bool)
            or not _bound_receipt(
                rollback.get("zero_inflight_actions_receipt"),
                "fs2-serve.nebius.ai/storage-reconciler-zero-inflight-observation/v1",
                self.cluster_id,
            )
            or not _bound_receipt(
                rollback.get("schema_compatibility_receipt"),
                "fs2-serve.nebius.ai/storage-reconciler-schema-compatibility/v1",
                self.cluster_id,
            )
            or not _bound_receipt(
                rollback.get("provider_continuity_receipt"),
                "fs2-serve.nebius.ai/storage-reconciler-provider-continuity/v1",
                self.cluster_id,
            )
            or any(
                rollback.get(hash_field)
                != hashlib.sha256(_canonical(rollback[receipt_field])).hexdigest()
                for receipt_field, hash_field in (
                    ("zero_inflight_actions_receipt", "zero_inflight_actions_receipt_sha256"),
                    ("schema_compatibility_receipt", "schema_compatibility_receipt_sha256"),
                    ("provider_continuity_receipt", "provider_continuity_receipt_sha256"),
                )
            )
            or (
                rollback["successor_quiesced"]
                and (
                    not isinstance(rollback.get("successor_quiescence"), dict)
                    or not isinstance(rollback.get("successor_quiescence_sha256"), str)
                    or len(rollback["successor_quiescence_sha256"]) != 64
                )
            )
            or (
                not rollback["successor_quiesced"]
                and (
                    rollback.get("successor_quiescence") is not None
                    or rollback.get("successor_quiescence_sha256") is not None
                )
            )
        ):
            raise ValueError("rollback lacks exact quiescence and compatibility criteria")
        return body

    async def _observe(self) -> dict[str, Any]:
        body = self._verify(await asyncio.to_thread(self._fetch))
        epoch = int(body["activation_epoch"])
        predecessor_head = str(body["predecessor_head_sha256"])
        if self.observed_epoch == 0 and body["ledger_anchor_sha256"] != self.registry_anchor_sha256:
            raise ReconcilerQuiesced("activation authority registry anchor differs")
        if epoch < self.observed_epoch:
            raise ReconcilerQuiesced("activation authority attempted an epoch rollback")
        if (
            epoch == self.observed_epoch
            and self.observed_predecessor_head
            and predecessor_head != self.observed_predecessor_head
        ):
            raise ReconcilerQuiesced("activation authority forked the observed epoch chain")
        if (
            epoch > self.observed_epoch
            and self.observed_head
            and predecessor_head != self.observed_head
        ):
            raise ReconcilerQuiesced("activation authority did not extend the observed epoch chain")
        self.observed_epoch = epoch
        self.observed_predecessor_head = predecessor_head
        self.observed_head = str(hashlib.sha256(_canonical(body)).hexdigest())
        return body

    async def admit_provider_operation(self) -> dict[str, Any]:
        """Return the exact active epoch which durably admits a cloud mutation."""

        body = await self._observe()
        if body["phase"] != "ACTIVE":
            raise ReconcilerQuiesced("the signed cutover epoch is draining or quiesced")
        if body["target_generation"] != self.generation:
            raise ReconcilerQuiesced("a different immutable reconciler generation is active")
        return {
            "reconciler_generation": self.generation,
            "activation_epoch": body["activation_epoch"],
            "activation_state_head_sha256": body["state_head_sha256"],
            "transition_id": body["cutover_receipt_sha256"],
        }

    async def current_drain_intent(self) -> dict[str, Any] | None:
        body = await self._observe()
        intent = body.get("provider_drain_intent")
        if (
            body["phase"] in {"DRAINING", "QUIESCED"}
            and isinstance(intent, dict)
            and intent.get("reconciler_generation") == self.generation
            and (
                body["phase"] == "QUIESCED"
                or body["target_generation"] == self.generation
            )
        ):
            # The scale-to-zero preStop runs in the first QUIESCED epoch, after
            # the database receipt already exists. Re-open the same immutable
            # intent so the hook observes that receipt immediately; it must not
            # wait for an ACTIVE/DRAINING target which no longer exists.
            return dict(intent)
        return None

    async def completed_shutdown_receipt(self) -> dict[str, Any] | None:
        """Return exact signed zero-inflight evidence for a scale-to-zero hook."""

        body = await self._observe()
        if body["phase"] != "QUIESCED":
            return None
        intent = body.get("provider_drain_intent")
        receipt = body.get("provider_drain_receipt")
        if (
            isinstance(intent, dict)
            and intent.get("reconciler_generation") == self.generation
            and isinstance(receipt, dict)
            and receipt.get("nonterminal_provider_operations") == 0
        ):
            return dict(receipt)
        rollback = body.get("rollback")
        rollback_receipt = (
            rollback.get("zero_inflight_actions_receipt")
            if isinstance(rollback, dict)
            else None
        )
        if (
            isinstance(rollback_receipt, dict)
            and self.generation
            in set(rollback_receipt.get("subject_generations") or [])
            and (rollback_receipt.get("detail") or {}).get(
                "nonterminal_provider_operations"
            )
            == 0
        ):
            # _verify() already checked the receipt body, issuer, freshness,
            # object identities, semantic outcome, digest, and signed envelope.
            return dict(rollback_receipt)
        return None

    async def assert_active(self) -> None:
        body = await self._observe()
        if body["phase"] != "ACTIVE":
            raise ReconcilerQuiesced("the signed cutover epoch has no active reconciler")
        if body["target_generation"] != self.generation:
            raise ReconcilerQuiesced("a different immutable reconciler generation is active")

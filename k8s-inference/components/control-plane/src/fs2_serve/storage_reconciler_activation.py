"""External, signed activation fence for generational storage reconcilers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import ssl
import stat
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


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
            or body.get("phase") not in {"ACTIVE", "QUIESCED"}
            or not isinstance(body.get("activation_epoch"), int)
            or body["activation_epoch"] < self.minimum_epoch
            or not isinstance(body.get("predecessor_epoch"), int)
            or body["predecessor_epoch"] != body["activation_epoch"] - 1
            or (
                body.get("phase") == "ACTIVE"
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
                "zero_inflight_actions_receipt_sha256",
                "schema_compatibility_receipt_sha256",
                "provider_continuity_receipt_sha256",
            }
            or rollback.get("from_epoch") != body["predecessor_epoch"]
            or not isinstance(rollback.get("successor_quiesced"), bool)
            or any(
                not isinstance(rollback.get(field), str)
                or len(rollback[field]) != 64
                for field in {
                    "zero_inflight_actions_receipt_sha256",
                    "schema_compatibility_receipt_sha256",
                    "provider_continuity_receipt_sha256",
                }
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

    async def assert_active(self) -> None:
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
        if body["phase"] == "QUIESCED":
            raise ReconcilerQuiesced("the signed cutover epoch has no active reconciler")
        if body["target_generation"] != self.generation:
            raise ReconcilerQuiesced("a different immutable reconciler generation is active")

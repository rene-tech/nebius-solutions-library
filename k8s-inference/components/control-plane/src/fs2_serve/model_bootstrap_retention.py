"""Apply-time verification for retained model-bootstrap Kubernetes objects."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .release_identity import ReleaseIdentityTrust

RECEIPT_TYPE = "fs2-model-bootstrap-retention+jws"
RECEIPT_SCHEMA = "fs2-serve.nebius.ai/model-bootstrap-retention-receipt/v1"
GENERATION = re.compile(r"^[a-f0-9]{32}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")


class BootstrapRetentionError(ValueError):
    """The live retained generation differs from its signed, pinned contract."""


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise BootstrapRetentionError("document contains duplicate fields")
        value[key] = item
    return value


def _json_object(raw: str | bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BootstrapRetentionError(f"{label} is malformed") from error
    if not isinstance(value, dict):
        raise BootstrapRetentionError(f"{label} is not an object")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _decode_segment(value: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise BootstrapRetentionError("receipt is malformed")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (TypeError, ValueError) as error:
        raise BootstrapRetentionError("receipt is malformed") from error


def _required_environment(name: str, pattern: re.Pattern[str] | None = None) -> str:
    value = os.environ.get(name, "")
    if not value or (pattern is not None and pattern.fullmatch(value) is None):
        raise BootstrapRetentionError(f"{name} is invalid")
    return value


def _optional_environment(name: str, pattern: re.Pattern[str] | None = None) -> str:
    value = os.environ.get(name, "")
    if value and pattern is not None and pattern.fullmatch(value) is None:
        raise BootstrapRetentionError(f"{name} is invalid")
    return value


def _uuid(value: str, label: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise BootstrapRetentionError(f"{label} is not a canonical UID") from error
    if str(parsed) != value:
        raise BootstrapRetentionError(f"{label} is not a canonical UID")
    return value


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise BootstrapRetentionError(f"{label} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BootstrapRetentionError(f"{label} is invalid") from error
    if parsed.utcoffset() is None:
        raise BootstrapRetentionError(f"{label} is not timezone-aware")
    return parsed


class _KubernetesReader:
    def __init__(self) -> None:
        host = _required_environment("KUBERNETES_SERVICE_HOST")
        port = _required_environment("KUBERNETES_SERVICE_PORT_HTTPS")
        token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
        try:
            self._token = token_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise BootstrapRetentionError("cannot read the projected verifier token") from error
        if not self._token:
            raise BootstrapRetentionError("the projected verifier token is empty")
        self._base_url = f"https://{host}:{port}"
        self._context = ssl.create_default_context(cafile=str(ca_path))

    def get(self, path: str, label: str) -> dict[str, Any]:
        request = urllib.request.Request(
            self._base_url + path,
            headers={"Accept": "application/json", "Authorization": f"Bearer {self._token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10, context=self._context) as response:
                if response.status != 200:
                    raise BootstrapRetentionError(f"{label} lookup returned an unexpected status")
                return _json_object(response.read(), label)
        except urllib.error.HTTPError as error:
            raise BootstrapRetentionError(f"{label} lookup was refused") from error
        except urllib.error.URLError as error:
            raise BootstrapRetentionError(f"{label} lookup failed") from error


def _resource(reader: _KubernetesReader, *, kind: str, name: str) -> dict[str, Any]:
    quoted = urllib.parse.quote(name, safe="")
    if kind == "configmap":
        path = f"/api/v1/namespaces/fs2-system/configmaps/{quoted}"
    elif kind == "job":
        path = f"/apis/batch/v1/namespaces/fs2-system/jobs/{quoted}"
    else:  # pragma: no cover - internal closed call set
        raise BootstrapRetentionError("unsupported Kubernetes resource kind")
    value = reader.get(path, f"{kind} {name}")
    metadata = value.get("metadata")
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != name
        or metadata.get("namespace") != "fs2-system"
    ):
        raise BootstrapRetentionError(f"{kind} identity differs")
    return value


def _match_resource(value: dict[str, Any], *, uid: str, digest: str, label: str) -> None:
    metadata = value.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("uid") != uid:
        raise BootstrapRetentionError(f"{label} UID differs")
    if _sha256(_canonical_json(value)) != digest:
        raise BootstrapRetentionError(f"{label} full-object digest differs")


def _trust_key_set_sha256(trust: ReleaseIdentityTrust) -> str:
    records = sorted(
        _canonical_json(
            {
                "issuer": issuer.issuer,
                "key_id": key.key_id,
                "public_key_base64": key.public_key_base64,
            }
        ).decode()
        for issuer in trust.issuers
        for key in issuer.keys
    )
    return _sha256(_canonical_json(records))


def _verify_receipt(
    compact: str,
    trust: ReleaseIdentityTrust,
    expected: dict[str, object],
) -> None:
    if not compact or len(compact) > 8192:
        raise BootstrapRetentionError("receipt size is invalid")
    parts = compact.split(".")
    if len(parts) != 3:
        raise BootstrapRetentionError("receipt is malformed")
    protected = _json_object(_decode_segment(parts[0]), "receipt protected header")
    claims = _json_object(_decode_segment(parts[1]), "receipt claims")
    if (
        set(protected) != {"alg", "kid", "typ"}
        or protected.get("alg") != "EdDSA"
        or protected.get("typ") != RECEIPT_TYPE
        or not isinstance(protected.get("kid"), str)
    ):
        raise BootstrapRetentionError("receipt protected header is invalid")
    required_claims = {
        "schema",
        "issuer",
        "subject",
        "audience",
        "authorization_closure_sha256",
        "issued_at",
        "generation",
        "phase",
        "identity_sha256",
        "config_map_uid",
        "config_map_object_sha256",
        "job_uid",
        "job_object_sha256",
        "release_assertion_id",
        "release_assertion_fingerprint",
        "release_receipt_consumed_at",
    }
    if set(claims) != required_claims or claims.get("schema") != RECEIPT_SCHEMA:
        raise BootstrapRetentionError("receipt claim shape is invalid")
    matches = [issuer for issuer in trust.issuers if issuer.issuer == claims.get("issuer")]
    if len(matches) != 1:
        raise BootstrapRetentionError("receipt issuer is not uniquely trusted")
    issuer = matches[0]
    if (
        claims.get("subject") != issuer.workload_subject
        or claims.get("audience") != issuer.audience
        or claims.get("authorization_closure_sha256") != issuer.authorization_closure_sha256
        or "models.bootstrap" not in {capability.value for capability in issuer.capabilities}
    ):
        raise BootstrapRetentionError("receipt authority differs from trust policy")
    keys = [key for key in issuer.keys if key.key_id == protected["kid"]]
    if len(keys) != 1:
        raise BootstrapRetentionError("receipt key is not uniquely trusted")
    raw_key = _decode_segment(keys[0].public_key_base64)
    if len(raw_key) != 32:
        raise BootstrapRetentionError("receipt key length is invalid")
    try:
        Ed25519PublicKey.from_public_bytes(raw_key).verify(
            _decode_segment(parts[2]), f"{parts[0]}.{parts[1]}".encode("ascii")
        )
    except InvalidSignature as error:
        raise BootstrapRetentionError("receipt signature is invalid") from error
    for key, value in expected.items():
        if claims.get(key) != value:
            raise BootstrapRetentionError(f"receipt claim {key} differs")
    assertion_id = str(claims.get("release_assertion_id"))
    _uuid(assertion_id, "receipt assertion")
    if (
        not isinstance(claims.get("release_assertion_fingerprint"), str)
        or DIGEST.fullmatch(claims["release_assertion_fingerprint"]) is None
    ):
        raise BootstrapRetentionError("receipt assertion fingerprint is invalid")
    issued_at = _timestamp(claims.get("issued_at"), "receipt issuance")
    if expected["phase"] == "terminal":
        consumed_at = _timestamp(
            claims.get("release_receipt_consumed_at"),
            "assertion consumption",
        )
        if issued_at < consumed_at:
            raise BootstrapRetentionError("receipt predates assertion consumption")
    elif claims.get("release_receipt_consumed_at") is not None:
        raise BootstrapRetentionError("configmap receipt claims assertion consumption")


def verify_live_retention() -> None:
    generation = _required_environment("FS2_BOOTSTRAP_GENERATION", GENERATION)
    phase = _required_environment("FS2_BOOTSTRAP_RECEIPT_PHASE")
    if phase not in {"configmap", "terminal"}:
        raise BootstrapRetentionError("receipt phase is invalid")
    expected_identity = _required_environment("FS2_BOOTSTRAP_IDENTITY_SHA256", DIGEST)
    config_map_uid = _uuid(
        _required_environment("FS2_BOOTSTRAP_CONFIG_MAP_UID"),
        "bootstrap ConfigMap",
    )
    config_map_digest = _required_environment(
        "FS2_BOOTSTRAP_CONFIG_MAP_OBJECT_SHA256",
        DIGEST,
    )
    receipt_uid = _uuid(
        _required_environment("FS2_BOOTSTRAP_RECEIPT_UID"),
        "retention receipt",
    )
    receipt_digest = _required_environment(
        "FS2_BOOTSTRAP_RECEIPT_OBJECT_SHA256",
        DIGEST,
    )
    trust_uid = _uuid(
        _required_environment("FS2_BOOTSTRAP_TRUST_CONFIG_MAP_UID"),
        "trust ConfigMap",
    )
    trust_digest = _required_environment(
        "FS2_BOOTSTRAP_TRUST_CONFIG_MAP_SHA256",
        DIGEST,
    )
    trust_json_digest = _required_environment("FS2_BOOTSTRAP_TRUST_JSON_SHA256", DIGEST)
    trust_key_set_digest = _required_environment(
        "FS2_BOOTSTRAP_TRUST_KEY_SET_SHA256",
        DIGEST,
    )
    job_uid = _optional_environment("FS2_BOOTSTRAP_JOB_UID")
    job_digest = _optional_environment("FS2_BOOTSTRAP_JOB_OBJECT_SHA256", DIGEST)
    if phase == "terminal":
        job_uid = _uuid(job_uid, "bootstrap Job")
        if not job_digest:
            raise BootstrapRetentionError("terminal receipt Job digest is absent")
    elif job_uid or job_digest:
        raise BootstrapRetentionError("configmap receipt unexpectedly carries a Job")

    reader = _KubernetesReader()
    trust_object = _resource(
        reader,
        kind="configmap",
        name="fs2-serve-release-identity-trust",
    )
    _match_resource(
        trust_object,
        uid=trust_uid,
        digest=trust_digest,
        label="trust ConfigMap",
    )
    trust_data = trust_object.get("data")
    if not isinstance(trust_data, dict) or set(trust_data) != {"trust.json"}:
        raise BootstrapRetentionError("trust ConfigMap shape is invalid")
    trust_json = trust_data["trust.json"]
    if not isinstance(trust_json, str) or _sha256(trust_json.encode()) != trust_json_digest:
        raise BootstrapRetentionError("trust document digest differs")
    try:
        trust = ReleaseIdentityTrust.model_validate(_json_object(trust_json, "trust document"))
    except ValueError as error:
        raise BootstrapRetentionError("trust document is invalid") from error
    if _trust_key_set_sha256(trust) != trust_key_set_digest:
        raise BootstrapRetentionError("trust key-set digest differs")

    config_map = _resource(
        reader,
        kind="configmap",
        name=f"fs2-model-bootstrap-{generation}",
    )
    _match_resource(
        config_map,
        uid=config_map_uid,
        digest=config_map_digest,
        label="bootstrap ConfigMap",
    )
    config_data = config_map.get("data")
    if not isinstance(config_data, dict) or "bootstrap-identity.json" not in config_data:
        raise BootstrapRetentionError("bootstrap ConfigMap identity is absent")
    identity = _json_object(config_data["bootstrap-identity.json"], "bootstrap identity")
    if _sha256(_canonical_json(identity)) != expected_identity:
        raise BootstrapRetentionError("bootstrap identity digest differs")

    receipt = _resource(
        reader,
        kind="configmap",
        name=f"fs2-model-bootstrap-receipt-{generation}-{phase}",
    )
    _match_resource(
        receipt,
        uid=receipt_uid,
        digest=receipt_digest,
        label="retention receipt",
    )
    receipt_data = receipt.get("data")
    if not isinstance(receipt_data, dict) or set(receipt_data) != {"receipt.jws"}:
        raise BootstrapRetentionError("retention receipt shape is invalid")

    if phase == "terminal":
        job = _resource(reader, kind="job", name=f"fs2-model-bootstrap-{generation}")
        _match_resource(job, uid=job_uid, digest=job_digest, label="bootstrap Job")

    _verify_receipt(
        str(receipt_data["receipt.jws"]),
        trust,
        {
            "generation": generation,
            "phase": phase,
            "identity_sha256": expected_identity,
            "config_map_uid": config_map_uid,
            "config_map_object_sha256": config_map_digest,
            "job_uid": None if phase == "configmap" else job_uid,
            "job_object_sha256": None if phase == "configmap" else job_digest,
        },
    )
    sys.stdout.write(f"verified retained model-bootstrap generation {generation} phase {phase}\n")


def main() -> None:
    try:
        verify_live_retention()
    except (BootstrapRetentionError, OSError, TypeError, ValueError) as error:
        sys.stderr.write(f"model-bootstrap retention verification refused: {error}\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

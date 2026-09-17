from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fs2_serve_catalog.artifacts import canonical_bytes
from fs2_serve_catalog.attestations import (
    create_signed_attestation,
    public_key_id,
    public_key_value,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "stages/workloads/scripts/verify_runtime_security_authorization.py"
SPEC = importlib.util.spec_from_file_location("runtime_security_authorization", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


def _write_canonical(path: Path, value: object) -> str:
    raw = canonical_bytes(value)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def authorization_packet(
    tmp_path: Path,
) -> tuple[dict[str, str], dict[str, object], dict[str, object]]:
    authorization_id = "runtime-review"
    kind = "runtime-compatibility"
    model_id = "mosaic"
    subject_schema = "fs2-serve.nebius.ai/runtime-security-compatibility/v2"
    subject_sha256 = hashlib.sha256(b"compatibility").hexdigest()
    evidence = {
        "schema": "fs2-serve.nebius.ai/runtime-security-evidence/v1",
        "authorization_id": authorization_id,
        "kind": kind,
        "model_id": model_id,
        "subject_schema": subject_schema,
        "subject_sha256": subject_sha256,
        "decision": "accepted",
        "reviewer_role": "independent-platform-security",
    }
    evidence_path = tmp_path / "evidence.json"
    evidence_sha256 = _write_canonical(evidence_path, evidence)
    private_key = Ed25519PrivateKey.generate()
    key_id = public_key_id(private_key.public_key())
    session_id = "sha256:" + hashlib.sha256(b"runtime-review-session").hexdigest()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    authority = {
        "schema": "fs2-serve.nebius.ai/platform-security-authority/v1",
        "authority": "independent-platform-security",
        "session_id": session_id,
        "activated_at": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trusted_attestors": {key_id: public_key_value(private_key.public_key())},
    }
    attestation = create_signed_attestation(
        private_key=private_key,
        session_id=session_id,
        nonce="sha256:" + hashlib.sha256(b"runtime-review-nonce").hexdigest(),
        issued_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        kind=kind,
        subject_schema=subject_schema,
        subject_digest="sha256:" + subject_sha256,
        model_id=model_id,
        claims={
            "authorization_id": authorization_id,
            "decision": "accepted",
            "reviewer_role": "independent-platform-security",
            "evidence_sha256": evidence_sha256,
        },
    )
    attestation_path = tmp_path / "attestation.json"
    attestation_sha256 = _write_canonical(attestation_path, attestation)
    return {
        "authorization_id": authorization_id,
        "kind": kind,
        "model_id": model_id,
        "subject_schema": subject_schema,
        "subject_sha256": subject_sha256,
        "evidence_path": str(evidence_path),
        "evidence_sha256": evidence_sha256,
        "attestation_path": str(attestation_path),
        "attestation_sha256": attestation_sha256,
    }, attestation, authority


def test_external_runtime_authorization_requires_external_signature_and_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    query, _, authority = authorization_packet(tmp_path)
    monkeypatch.setattr(
        VERIFIER,
        "_authority_document",
        lambda: (authority, canonical_bytes(authority)),
    )
    result = VERIFIER.verify(query)
    assert result["verified"] == "true"
    assert result["authorization_id"] == "runtime-review"
    assert hashlib.sha256(result["evidence_json"].encode()).hexdigest() == query[
        "evidence_sha256"
    ]
    assert hashlib.sha256(result["attestation_json"].encode()).hexdigest() == query[
        "attestation_sha256"
    ]
    assert result["session_id"] == authority["session_id"]
    assert json.loads(result["trusted_attestors_json"]) == authority["trusted_attestors"]

    forged = dict(query)
    forged["subject_sha256"] = "f" * 64
    with pytest.raises(VERIFIER.VerificationError):
        VERIFIER.verify(forged)


def test_external_runtime_authorization_rejects_tampered_attestation_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    query, attestation, authority = authorization_packet(tmp_path)
    monkeypatch.setattr(
        VERIFIER,
        "_authority_document",
        lambda: (authority, canonical_bytes(authority)),
    )
    attestation["claims"]["decision"] = "accepted-without-review"
    query["attestation_sha256"] = _write_canonical(
        Path(query["attestation_path"]), attestation
    )
    with pytest.raises(VERIFIER.VerificationError, match="subject or claims differ"):
        VERIFIER.verify(query)


def test_external_runtime_authorization_rejects_caller_selected_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    query, _, authority = authorization_packet(tmp_path)
    monkeypatch.setattr(
        VERIFIER,
        "_authority_document",
        lambda: (authority, canonical_bytes(authority)),
    )
    query["trust_roots_path"] = str(tmp_path / "caller-root.json")
    query["session_id"] = authority["session_id"]
    with pytest.raises(VERIFIER.VerificationError, match="query fields differ"):
        VERIFIER.verify(query)

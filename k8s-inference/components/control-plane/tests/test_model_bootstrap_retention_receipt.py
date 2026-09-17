from __future__ import annotations

import base64
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
VERIFIER = REPOSITORY_ROOT / "stages/workloads/scripts/verify_model_bootstrap_receipt.py"


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def fixture(
    *, phase: str = "terminal"
) -> tuple[dict[str, str], Ed25519PrivateKey, dict[str, object]]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    generation = "1" * 32
    consumed_at = datetime.now(UTC).replace(microsecond=0)
    claims: dict[str, object] = {
        "schema": "fs2-serve.nebius.ai/model-bootstrap-retention-receipt/v1",
        "issuer": "release-authority",
        "subject": "workload/release",
        "audience": "fs2-admin-release",
        "authorization_closure_sha256": "2" * 64,
        "issued_at": (consumed_at + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "generation": generation,
        "phase": phase,
        "identity_sha256": "3" * 64,
        "config_map_uid": "11111111-1111-4111-8111-111111111111",
        "config_map_object_sha256": "4" * 64,
        "job_uid": "22222222-2222-4222-8222-222222222222" if phase == "terminal" else None,
        "job_object_sha256": "5" * 64 if phase == "terminal" else None,
        "release_assertion_id": str(uuid4()),
        "release_assertion_fingerprint": "6" * 64,
        "release_receipt_consumed_at": (
            consumed_at.isoformat().replace("+00:00", "Z") if phase == "terminal" else None
        ),
    }
    trust = {
        "schema": "fs2-serve.nebius.ai/release-identity-trust/v1",
        "issuers": [
            {
                "issuer": "release-authority",
                "workload_subject": "workload/release",
                "audience": "fs2-admin-release",
                "authorization_closure_sha256": "2" * 64,
                "capabilities": ["models.bootstrap"],
                "keys": [{"key_id": "key-1", "public_key_base64": encoded(public_key)}],
            }
        ],
    }
    protected_segment = encoded(
        json.dumps(
            {"alg": "EdDSA", "kid": "key-1", "typ": "fs2-model-bootstrap-retention+jws"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    payload_segment = encoded(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    signature = encoded(private_key.sign(f"{protected_segment}.{payload_segment}".encode("ascii")))
    query = {
        "receipt_jws": f"{protected_segment}.{payload_segment}.{signature}",
        "trust_json": json.dumps(trust, sort_keys=True, separators=(",", ":")),
        "generation": generation,
        "phase": phase,
        "identity_sha256": "3" * 64,
        "config_map_uid": "11111111-1111-4111-8111-111111111111",
        "config_map_object_sha256": "4" * 64,
        "job_uid": "22222222-2222-4222-8222-222222222222" if phase == "terminal" else "",
        "job_object_sha256": "5" * 64 if phase == "terminal" else "",
    }
    return query, private_key, claims


def invoke(query: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed repository verifier and test-owned JSON
        [sys.executable, str(VERIFIER)],
        input=json.dumps(query),
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("phase", ["configmap", "terminal"])
def test_release_signed_receipt_binds_expected_phase_and_uids(phase: str) -> None:
    query, _, _ = fixture(phase=phase)
    result = invoke(query)
    assert result.returncode == 0, result.stderr
    verified = json.loads(result.stdout)
    assert verified["valid"] == "true"
    assert verified["generation"] == query["generation"]
    assert verified["config_map_uid"] == query["config_map_uid"]
    assert verified["job_uid"] == query["job_uid"]


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("config_map_uid", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        ("config_map_object_sha256", "d" * 64),
        ("job_uid", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        ("job_object_sha256", "f" * 64),
        ("identity_sha256", "e" * 64),
    ],
)
def test_replaced_or_changed_history_cannot_reuse_terminal_receipt(
    field: str, replacement: str
) -> None:
    query, _, _ = fixture()
    query[field] = replacement
    result = invoke(query)
    assert result.returncode == 1
    assert "does not match the observed object" in result.stderr
    assert result.stdout == ""


def test_receipt_payload_or_signature_tampering_is_refused() -> None:
    query, _, _ = fixture()
    protected, payload, signature = query["receipt_jws"].split(".")
    tampered_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
    query["receipt_jws"] = f"{protected}.{payload}.{tampered_signature}"
    result = invoke(query)
    assert result.returncode == 1
    assert "signature is invalid" in result.stderr


def test_malformed_capability_shape_cannot_become_retention_authority() -> None:
    query, _, _ = fixture()
    trust = json.loads(query["trust_json"])
    trust["issuers"][0]["capabilities"] = "models.bootstrap"
    query["trust_json"] = json.dumps(trust, sort_keys=True, separators=(",", ":"))
    result = invoke(query)
    assert result.returncode == 1
    assert "lacks the exact bootstrap capability" in result.stderr


def test_configmap_phase_cannot_claim_terminal_job_or_consumption() -> None:
    query, private_key, claims = fixture(phase="configmap")
    claims["release_receipt_consumed_at"] = datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    protected = query["receipt_jws"].split(".")[0]
    payload = encoded(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    signature = encoded(private_key.sign(f"{protected}.{payload}".encode("ascii")))
    query["receipt_jws"] = f"{protected}.{payload}.{signature}"
    result = invoke(query)
    assert result.returncode == 1
    assert "cannot claim assertion consumption" in result.stderr

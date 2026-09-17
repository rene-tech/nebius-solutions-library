from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fs2_serve.access_models import ReleaseIdentityCapability, ReleaseIdentityPurpose
from fs2_serve.release_identity import (
    RELEASE_ASSERTION_TYPE,
    ReleaseIdentityIssuer,
    ReleaseIdentityKey,
    ReleaseIdentityTrust,
    ReleaseIdentityVerifier,
)


def _segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


@dataclass(frozen=True)
class ReleaseAuthorityFixture:
    verifier: ReleaseIdentityVerifier
    private_key: Ed25519PrivateKey
    issuer: str = "https://release-authority.test.invalid"
    subject: str = "serviceaccount:test-release-automation"
    key_id: str = "release-test-v1"
    closure: str = "1" * 64

    @classmethod
    def create(cls) -> ReleaseAuthorityFixture:
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        authority = cls(
            verifier=ReleaseIdentityVerifier(
                ReleaseIdentityTrust(
                    schema="fs2-serve.nebius.ai/release-identity-trust/v1",
                    issuers=(
                        ReleaseIdentityIssuer(
                            issuer="https://release-authority.test.invalid",
                            workload_subject="serviceaccount:test-release-automation",
                            audience="fs2-admin-release",
                            authorization_closure_sha256="1" * 64,
                            capabilities=frozenset(ReleaseIdentityCapability),
                            keys=(
                                ReleaseIdentityKey(
                                    key_id="release-test-v1",
                                    public_key_base64=_segment(public_key),
                                ),
                            ),
                        ),
                    ),
                )
            ),
            private_key=private_key,
        )
        return authority

    def bearer(
        self,
        capability: ReleaseIdentityCapability,
        *,
        operator: dict[str, Any] | None = None,
        assertion_id: str | None = None,
        now: datetime | None = None,
        resource_sha256: str | None = None,
        resource_generation: str | None = None,
    ) -> str:
        issued = now or datetime.now(UTC)
        purpose = (
            ReleaseIdentityPurpose.OPERATOR_ENROLLMENT
            if capability is ReleaseIdentityCapability.OPERATOR_ENROLL
            else ReleaseIdentityPurpose.ADMIN_AUTOMATION
        )
        payload = {
            "schema": "fs2-serve.nebius.ai/release-identity-assertion/v1",
            "assertion_id": assertion_id or str(uuid4()),
            "session_id": f"release-session-{uuid4()}",
            "issuer": self.issuer,
            "subject": self.subject,
            "audience": "fs2-admin-release",
            "purpose": str(purpose),
            "capabilities": [str(capability)],
            "issued_at": issued.isoformat(),
            "not_before": issued.isoformat(),
            "expires_at": (issued + timedelta(minutes=2)).isoformat(),
            "session_issued_at": (issued - timedelta(minutes=1)).isoformat(),
            "session_expires_at": (issued + timedelta(minutes=30)).isoformat(),
            "credential_kind": "workload_identity_session",
            "human_principal_allowed": False,
            "interactive_login_allowed": False,
            "impersonation_allowed": False,
            "authorization_closure_sha256": self.closure,
            "resource_sha256": resource_sha256,
            "resource_generation": resource_generation,
            "operator": operator,
        }
        protected = _segment(
            json.dumps(
                {"alg": "EdDSA", "kid": self.key_id, "typ": RELEASE_ASSERTION_TYPE},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        encoded_payload = _segment(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        signing_input = f"{protected}.{encoded_payload}"
        return f"{signing_input}.{_segment(self.private_key.sign(signing_input.encode()))}"

    def auth(self, capability: ReleaseIdentityCapability, **kwargs: Any) -> dict[str, str]:
        return {"authorization": f"Bearer {self.bearer(capability, **kwargs)}"}

"""Verify S2's projected pod identity; never trust a caller-supplied pod name.

Kubernetes signs the token and S2 reports the currently authorized attempt.
Both must agree on the pod, region, namespace, service account and audience.
The configured JWKS endpoints are operator-owned, never taken from a token.
Offline verification does not prove immediate Pod/ServiceAccount deletion;
the online S2 attempt check and short-lived object URLs remain necessary.
"""

from __future__ import annotations

import jwt
from pydantic import Field, model_validator

from .broker import https_origin
from .contracts import Contract, Identifier


class Issuer(Contract):
    issuer: str
    jwks_url: str

    @model_validator(mode="after")
    def trusted_urls(self):
        https_origin(self.issuer)
        https_origin(self.jwks_url)
        return self


class WorkloadIdentity(Contract):
    audience: str = Field(min_length=8)
    issuers: dict[Identifier, Issuer] = Field(min_length=1)


class PodVerifier:
    def __init__(self, config: WorkloadIdentity):
        self.config = config
        self.keys = {
            region: jwt.PyJWKClient(issuer.jwks_url, cache_keys=False, lifespan=300, timeout=5)
            for region, issuer in config.issuers.items()
        }

    def require_current(self, token: str, *, current: dict, namespace: str, identity_ref: str) -> None:
        cluster, pod = current.get("cluster"), current.get("attempt_id")
        identity = (current.get("execution") or {}).get("identity") or {}
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 32768
            or cluster not in self.keys
            or not pod
            or identity.get("ref") != identity_ref
            or identity.get("service_account") != "identity-" + identity_ref
            or identity.get("audience") != self.config.audience
        ):
            raise PermissionError("the attempt has no approved projected workload identity")
        try:
            key = self.keys[cluster].get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.config.issuers[cluster].issuer,
                audience=self.config.audience,
                subject=f"system:serviceaccount:{namespace}:identity-{identity_ref}",
                options={"require": ["iss", "aud", "sub", "exp", "iat", "nbf"]},
            )
        except jwt.PyJWKClientConnectionError:
            raise ConnectionError("workload identity keys are temporarily unavailable") from None
        except (jwt.PyJWTError, ValueError):
            raise PermissionError("invalid projected workload identity") from None
        bound = claims.get("kubernetes.io")
        if not isinstance(bound, dict):
            raise PermissionError("a pod-bound workload identity is required")
        bound_pod, service_account = bound.get("pod"), bound.get("serviceaccount")
        if (
            bound.get("namespace") != namespace
            or not isinstance(bound_pod, dict)
            or bound_pod.get("name") != pod
            or not bound_pod.get("uid")
            or not isinstance(service_account, dict)
            or service_account.get("name") != "identity-" + identity_ref
            or not service_account.get("uid")
        ):
            raise PermissionError("projected identity is not bound to the current attempt")

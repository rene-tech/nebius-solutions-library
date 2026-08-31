"""Value-suppressed fixtures for securely pre-created NGC Secrets."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from fs2_serve_catalog.artifacts import canonical_bytes


def digest(label: str, *, image: bool = False) -> str:
    value = hashlib.sha256(label.encode()).hexdigest()
    return "sha256:" + value if image else value


def make_ngc_materialization(
    ngc_resources: Mapping[str, Mapping[str, Any]],
    *,
    materialized_at: str = "2026-08-26T22:00:00Z",
) -> dict[str, Any]:
    """Return a signed-receipt subject without retaining any Secret value."""

    fingerprint = digest("fresh-platform-ngc-key")
    secret_shapes = {
        "fs2-models/ngc-pull-secret": (
            "kubernetes.io/dockerconfigjson",
            [".dockerconfigjson"],
        ),
        "fs2-models/ngc-runtime-secret": ("Opaque", ["NGC_API_KEY"]),
    }
    server_observation = {
        "method": "authenticated-kubernetes-apiserver-get",
        "observed_at": materialized_at,
        "api_server_identity_sha256": digest("target-kubernetes-api-server"),
        "observer_principal_sha256": digest("fs2-model-evidence-observer-principal"),
        "values_recorded": False,
        "metadata_fields": [
            "apiVersion",
            "kind",
            "metadata.name",
            "metadata.namespace",
            "metadata.resourceVersion",
            "metadata.uid",
            "type",
            "data-key-set",
        ],
    }
    return {
        "schema": "fs2-serve.nebius.ai/ngc-credential-materialization/v3",
        "status": "fresh-platform-key-precreated-and-observed",
        "materialized_at": materialized_at,
        "valid_until": "2026-08-27T23:00:00Z",
        "platform_owner": "fs2-serve-platform",
        "delivery_mode": "securely-pre-created-existing-kubernetes-secrets",
        "key_origin": "new-platform-owned-secure-injection",
        "validity_status": "verified-current",
        "compromise_review_status": "no-known-exposure",
        "issuer_receipt_sha256": digest("unit-fresh-ngc-secure-injection"),
        "server_observation_sha256": hashlib.sha256(
            canonical_bytes(server_observation)
        ).hexdigest(),
        "credential_generation_sha256": digest("unit-fresh-ngc-generation"),
        "key_fingerprint_sha256": fingerprint,
        "server_observation": server_observation,
        "optional_backend_eligibility_receipt": None,
        "values_suppressed": True,
        "legacy_ngc_secret_copied": False,
        "legacy_plaintext_rotation_source_used": False,
        "legacy_phase_7c_hmac_reused": False,
        "exposed_evo_bearer_reused": False,
        "secrets": [
            {
                "requirement_id": requirement_id,
                "api_version": ngc_resources[requirement_id].get("api_version", "v1"),
                "kind": ngc_resources[requirement_id].get("kind", "Secret"),
                "namespace": ngc_resources[requirement_id]["namespace"],
                "name": ngc_resources[requirement_id]["name"],
                "uid": ngc_resources[requirement_id]["uid"],
                "resource_version": ngc_resources[requirement_id]["resource_version"],
                "secret_type": ngc_resources[requirement_id].get(
                    "secret_type", secret_shapes[requirement_id][0]
                ),
                "data_keys": ngc_resources[requirement_id].get(
                    "data_keys", secret_shapes[requirement_id][1]
                ),
                "observed_at": materialized_at,
                "key_fingerprint_sha256": fingerprint,
            }
            for requirement_id in (
                "fs2-models/ngc-pull-secret",
                "fs2-models/ngc-runtime-secret",
            )
        ],
    }

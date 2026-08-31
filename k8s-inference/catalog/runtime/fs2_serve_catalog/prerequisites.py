#!/usr/bin/env python3
"""Value-suppressed binding for externally bootstrapped Kubernetes resources."""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .artifacts import canonical_bytes
from .loader import (
    Catalog,
    CatalogError,
    _exact,
    _list,
    _positive_int,
    _text,
    strong_sha256,
)


OBSERVED_PREREQUISITES_SCHEMA = "fs2-serve.nebius.ai/observed-prerequisites/v4"
NGC_MATERIALIZATION_SCHEMA = "fs2-serve.nebius.ai/ngc-credential-materialization/v3"
NGC_SECRET_REQUIREMENT_IDS = (
    "fs2-models/ngc-pull-secret",
    "fs2-models/ngc-runtime-secret",
)
K8S_UID = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27,}$")
RESOURCE_VERSION = re.compile(r"^[1-9][0-9]*$")
UTC_SECONDS = re.compile(r"^20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


@dataclass(frozen=True)
class PrerequisiteBinding:
    """Validated metadata-only observations for prerequisite resource identities."""

    resources: Mapping[str, Mapping[str, Any]]
    ngc_credential_materialization: Mapping[str, Any] | None

    def require(self, requirement_ids: Iterable[str]) -> None:
        missing = sorted(set(requirement_ids) - set(self.resources))
        if missing:
            raise CatalogError(f"runtime prerequisites are absent: {missing}")

    def resource(self, requirement_id: str) -> dict[str, Any]:
        try:
            return copy.deepcopy(dict(self.resources[requirement_id]))
        except KeyError as exc:
            raise CatalogError(f"runtime prerequisite is not bound: {requirement_id}") from exc


def bind_runtime_prerequisites(
    catalog: Catalog,
    observed: Any,
    *,
    required_ids: Iterable[str] | None = None,
) -> PrerequisiteBinding:
    """Validate resource identity/state without accepting any credential value."""

    required = tuple(required_ids or catalog.runtime_prerequisites.keys())
    document = _exact(
        observed,
        {
            "schema",
            "values_suppressed",
            "legacy_ngc_secret_copied",
            "legacy_plaintext_rotation_source_used",
            "legacy_phase_7c_hmac_reused",
            "exposed_evo_bearer_reused",
            "ngc_credential_materialization",
            "resources",
        },
        "observed runtime prerequisites",
    )
    if document["schema"] != OBSERVED_PREREQUISITES_SCHEMA:
        raise CatalogError("unsupported observed prerequisite schema")
    if document["values_suppressed"] is not True:
        raise CatalogError("prerequisite observation must suppress all resource values")
    if document["legacy_ngc_secret_copied"] is not False:
        raise CatalogError("legacy NGC Secret copying is forbidden")
    if document["legacy_plaintext_rotation_source_used"] is not False:
        raise CatalogError("legacy plaintext rotation sources are forbidden")
    if document["legacy_phase_7c_hmac_reused"] is not False:
        raise CatalogError("legacy Phase-7c HMAC reuse is forbidden")
    if document["exposed_evo_bearer_reused"] is not False:
        raise CatalogError("exposed Evo bearer reuse is forbidden")
    bound: dict[str, Mapping[str, Any]] = {}
    order: list[str] = []
    for index, raw in enumerate(_list(document["resources"], "observed resources")):
        item = _exact(
            raw,
            {
                "id",
                "api_version",
                "kind",
                "namespace",
                "name",
                "uid",
                "resource_version",
                "state",
                "secret_type",
                "data_keys",
                "access_modes",
                "capacity_bytes",
            },
            f"observed resources[{index}]",
        )
        requirement_id = _text(item["id"], "observed prerequisite ID")
        assert requirement_id is not None
        try:
            expected = catalog.prerequisite(requirement_id).to_dict()
        except CatalogError as exc:
            raise CatalogError("observed inventory contains an unowned prerequisite") from exc
        for key in ("api_version", "kind", "namespace", "name"):
            if item[key] != expected[key]:
                raise CatalogError("observed prerequisite identity differs from its contract")
        uid = _text(item["uid"], "observed prerequisite UID")
        resource_version = _text(
            item["resource_version"], "observed prerequisite resource version"
        )
        if uid is None or K8S_UID.fullmatch(uid) is None:
            raise CatalogError("observed prerequisite lacks an exact Kubernetes UID")
        if resource_version is None or RESOURCE_VERSION.fullmatch(resource_version) is None:
            raise CatalogError("observed prerequisite lacks an exact resourceVersion")
        keys = _list(item["data_keys"], "observed Secret data keys")
        modes = _list(item["access_modes"], "observed PVC access modes")
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise CatalogError("observed Secret keys must be sorted and unique")
        if modes != sorted(modes) or len(modes) != len(set(modes)):
            raise CatalogError("observed PVC access modes must be sorted and unique")
        if expected["kind"] == "Secret":
            if (
                item["state"] != "present"
                or item["secret_type"] != expected["secret_type"]
                or keys != expected["required_keys"]
                or modes
                or item["capacity_bytes"] is not None
            ):
                raise CatalogError("observed Secret metadata does not satisfy its contract")
        elif expected["kind"] == "PersistentVolumeClaim":
            capacity = _positive_int(item["capacity_bytes"], "observed PVC capacity")
            if (
                item["state"] != "Bound"
                or item["secret_type"] is not None
                or keys
                or modes != expected["access_modes"]
                or capacity is None
                or capacity < expected["minimum_capacity_bytes"]
            ):
                raise CatalogError("observed PVC metadata does not satisfy its contract")
        else:
            if (
                item["state"] != "present"
                or item["secret_type"] is not None
                or keys
                or modes
                or item["capacity_bytes"] is not None
            ):
                raise CatalogError("observed ServiceAccount metadata does not satisfy its contract")
        if requirement_id in bound:
            raise CatalogError("observed prerequisite IDs must be unique")
        order.append(requirement_id)
        bound[requirement_id] = MappingProxyType(copy.deepcopy(item))
    if order != sorted(order):
        raise CatalogError("observed prerequisites must be canonically sorted")
    requested_ngc_ids = set(required) & set(NGC_SECRET_REQUIREMENT_IDS)
    if requested_ngc_ids and requested_ngc_ids != set(NGC_SECRET_REQUIREMENT_IDS):
        raise CatalogError("NGC workloads must bind both platform Secret identities")
    raw_materialization = document["ngc_credential_materialization"]
    materialization = (
        _validate_ngc_materialization(raw_materialization, bound)
        if requested_ngc_ids
        else None
    )
    if not requested_ngc_ids and raw_materialization is not None:
        raise CatalogError("non-NGC prerequisites may not claim NGC credential materialization")
    binding = PrerequisiteBinding(
        resources=MappingProxyType(bound),
        ngc_credential_materialization=materialization,
    )
    binding.require(required)
    return binding


def _utc_seconds(value: Any, label: str) -> datetime:
    text = _text(value, label)
    if text is None or UTC_SECONDS.fullmatch(text) is None:
        raise CatalogError(f"{label} must use whole RFC3339 UTC seconds")
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise CatalogError(f"{label} is invalid") from exc


def _validate_ngc_server_observation(
    value: Any, materialized_at: datetime
) -> dict[str, Any]:
    observation = _exact(
        value,
        {
            "method",
            "observed_at",
            "api_server_identity_sha256",
            "observer_principal_sha256",
            "values_recorded",
            "metadata_fields",
        },
        "NGC Secret server observation",
    )
    if (
        observation["method"] != "authenticated-kubernetes-apiserver-get"
        or observation["values_recorded"] is not False
        or observation["metadata_fields"]
        != [
            "apiVersion",
            "kind",
            "metadata.name",
            "metadata.namespace",
            "metadata.resourceVersion",
            "metadata.uid",
            "type",
            "data-key-set",
        ]
    ):
        raise CatalogError("NGC Secrets were not observed through the metadata-only API path")
    if _utc_seconds(observation["observed_at"], "NGC server observation time") != materialized_at:
        raise CatalogError("NGC Secret server observation time differs from materialization")
    strong_sha256(observation["api_server_identity_sha256"], "Kubernetes API server identity")
    strong_sha256(observation["observer_principal_sha256"], "Secret observer principal")
    return observation


def _validate_ngc_materialization(
    value: Any,
    resources: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    item = _exact(
        value,
        {
            "schema",
            "status",
            "materialized_at",
            "valid_until",
            "platform_owner",
            "delivery_mode",
            "key_origin",
            "validity_status",
            "compromise_review_status",
            "issuer_receipt_sha256",
            "server_observation_sha256",
            "credential_generation_sha256",
            "key_fingerprint_sha256",
            "server_observation",
            "optional_backend_eligibility_receipt",
            "values_suppressed",
            "legacy_ngc_secret_copied",
            "legacy_plaintext_rotation_source_used",
            "legacy_phase_7c_hmac_reused",
            "exposed_evo_bearer_reused",
            "secrets",
        },
        "NGC credential materialization",
    )
    if (
        item["schema"] != NGC_MATERIALIZATION_SCHEMA
        or item["status"] != "fresh-platform-key-precreated-and-observed"
        or item["platform_owner"] != "fs2-serve-platform"
        or item["delivery_mode"] != "securely-pre-created-existing-kubernetes-secrets"
        or item["key_origin"] != "new-platform-owned-secure-injection"
        or item["validity_status"] != "verified-current"
        or item["compromise_review_status"] != "no-known-exposure"
        or item["values_suppressed"] is not True
        or item["legacy_ngc_secret_copied"] is not False
        or item["legacy_plaintext_rotation_source_used"] is not False
        or item["legacy_phase_7c_hmac_reused"] is not False
        or item["exposed_evo_bearer_reused"] is not False
        or item["optional_backend_eligibility_receipt"] is not None
    ):
        raise CatalogError("NGC credential materialization is not fresh and platform-owned")
    materialized_at = _utc_seconds(item["materialized_at"], "NGC materialization time")
    valid_until = _utc_seconds(item["valid_until"], "NGC credential valid-until time")
    if valid_until <= materialized_at:
        raise CatalogError("NGC credential validity does not follow materialization")
    observation = _validate_ngc_server_observation(
        item["server_observation"], materialized_at
    )
    for field in (
        "issuer_receipt_sha256",
        "server_observation_sha256",
        "credential_generation_sha256",
        "key_fingerprint_sha256",
    ):
        strong_sha256(item[field], f"NGC {field}")
    if item["server_observation_sha256"] != hashlib.sha256(
        canonical_bytes(observation)
    ).hexdigest():
        raise CatalogError("NGC server observation digest does not bind its exact subject")
    secrets = _list(item["secrets"], "NGC materialized Secrets", nonempty=True)
    if [secret.get("requirement_id") for secret in secrets] != list(
        NGC_SECRET_REQUIREMENT_IDS
    ):
        raise CatalogError("NGC materialization must bind both target Secrets in order")
    for index, raw_secret in enumerate(secrets):
        secret = _exact(
            raw_secret,
            {
                "requirement_id",
                "api_version",
                "kind",
                "namespace",
                "name",
                "uid",
                "resource_version",
                "secret_type",
                "data_keys",
                "observed_at",
                "key_fingerprint_sha256",
            },
            f"NGC materialized Secrets[{index}]",
        )
        requirement_id = NGC_SECRET_REQUIREMENT_IDS[index]
        observed = resources.get(requirement_id)
        if observed is None or any(
            secret[field] != observed[field]
            for field in (
                "api_version",
                "kind",
                "namespace",
                "name",
                "uid",
                "resource_version",
                "secret_type",
                "data_keys",
            )
        ):
            raise CatalogError("NGC materialization Secret identity differs from observation")
        if secret["requirement_id"] != requirement_id:
            raise CatalogError("NGC materialization Secret requirement differs")
        if _utc_seconds(secret["observed_at"], "NGC Secret observation time") != materialized_at:
            raise CatalogError("NGC Secret observation does not match materialization")
        if secret["key_fingerprint_sha256"] != item["key_fingerprint_sha256"]:
            raise CatalogError("NGC Secrets do not carry the same fresh key generation")
    return MappingProxyType(copy.deepcopy(item))

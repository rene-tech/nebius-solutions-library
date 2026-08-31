#!/usr/bin/env python3
"""Signed live promotion overlay for source-only model runtime variants.

The static model-variant index deliberately has no route authority.  This
module is the only bridge from one static variant to a gateway route: it
reopens signed supply, runtime, cohort, semantic and review subjects and then
intersects them with the canonical base record and one normal serving binding.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .artifacts import ArtifactManifest, canonical_bytes
from .attestations import raw_public_key, raw_public_key_id, raw_signature
from .consumer import ServingBinding, ServingBindings
from .evidence import EvidenceStore
from .loader import (
    Catalog,
    CatalogError,
    ModelVariant,
    SemanticRequestContract,
    _boolean,
    _enum,
    _exact,
    _list,
    _load_json,
    _positive_int,
    _text,
    strong_sha256,
)


MODEL_VARIANT_PROMOTIONS_SCHEMA = "fs2-serve.nebius.ai/model-variant-promotions/v4"
MODEL_VARIANT_SUPPLY_SCHEMA = "fs2-serve.nebius.ai/model-variant-supply-receipt/v5"
MODEL_VARIANT_SUPPLY_OBJECT_SCHEMA = "fs2-serve.nebius.ai/model-variant-supply-object/v1"
MODEL_VARIANT_LICENSE_ARTIFACT_SCHEMA = "fs2-serve.nebius.ai/model-variant-license-artifact/v1"
MODEL_VARIANT_ATTESTOR_POLICY_SCHEMA = "fs2-serve.nebius.ai/model-variant-attestor-policy/v1"
MODEL_VARIANT_RUNTIME_TUPLE_SCHEMA = (
    "fs2-serve.nebius.ai/model-variant-runtime-tuple/v1"
)
MODEL_VARIANT_SEMANTIC_SCHEMA = "fs2-serve.nebius.ai/model-variant-semantic-receipt/v2"
MODEL_VARIANT_COHORT_SCHEMA = "fs2-serve.nebius.ai/model-variant-cohort/v3"
MODEL_VARIANT_QUALIFICATION_SCHEMA = (
    "fs2-serve.nebius.ai/model-variant-qualification-receipt/v5"
)
MODEL_VARIANT_LIFECYCLE_SCHEMA = (
    "fs2-serve.nebius.ai/model-variant-lifecycle-receipt/v1"
)
MODEL_VARIANT_BACKEND_READINESS_SCHEMA = (
    "fs2-serve.nebius.ai/model-variant-backend-readiness-receipt/v2"
)
MODEL_VARIANT_K8S_OBSERVATION_SCHEMA = (
    "fs2-serve.nebius.ai/model-variant-kubernetes-observation/v1"
)
MODEL_VARIANT_COLD_BOUNDARY_SCHEMA = (
    "fs2-serve.nebius.ai/model-variant-cold-boundary-receipt/v1"
)
MODEL_VARIANT_PREEMPTION_SCHEMA = (
    "fs2-serve.nebius.ai/model-variant-preemption-receipt/v1"
)
MODEL_VARIANT_REVIEW_SCHEMA = "fs2-serve.nebius.ai/model-variant-review-receipt/v4"

K8S_UID = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27,}$")
DRIVER_VERSION = re.compile(r"^[0-9]{3}\.[0-9]{2,3}\.[0-9]{2}$")
CUDA_VERSION = re.compile(r"^(?P<major>[0-9]+)\.(?P<minor>[0-9]+)(?:\.[0-9]+)?$")
KERNEL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.$:@/+\-]{3,255}$")
MAX_DURATION_ERROR_SECONDS = 0.001

REQUIRED_ATTESTOR_ROLES = (
    "artifact",
    "supply",
    "supply-signature",
    "supply-provenance",
    "supply-sbom",
    "supply-scan",
    "license",
    "runtime",
    "semantic",
    "cohort",
    "cold-boundary",
    "backend-readiness",
    "preemption",
    "lifecycle",
    "qualification",
    "review",
)

MAX_ATTEMPTS = 10_000
MAX_DURATION_SECONDS = 86_400.0
MAX_FAILURE_RATE = 0.10


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CatalogError("immutable JSON object contains a duplicate key")
        value[key] = item
    return value


def _utc(value: Any, label: str) -> datetime:
    text = _text(value, label)
    assert text is not None
    if not text.endswith("Z"):
        raise CatalogError(f"{label} must use UTC with a Z suffix")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except (ValueError, OverflowError) as exc:
        raise CatalogError(f"{label} is not an RFC3339 UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc or parsed.microsecond:
        raise CatalogError(f"{label} must use whole UTC seconds")
    return parsed


def _digest(value: Any, label: str, *, image: bool = False) -> str:
    return strong_sha256(value, label, image=image)


def _bounded_number(
    value: Any, label: str, *, minimum: float = 0.0, maximum: float = 1.0
) -> float:
    if isinstance(value, bool) or type(value) not in {int, float}:
        raise CatalogError(f"{label} must be a finite numeric value")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise CatalogError(f"{label} must be a finite numeric value") from exc
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise CatalogError(f"{label} is outside its closed bound")
    return number


def _bounded_count(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CatalogError(f"{label} must be an integer")
    if value < minimum or value > MAX_ATTEMPTS:
        raise CatalogError(f"{label} is outside its closed bound")
    return value


def _model_digest(catalog: Catalog, model_id: str) -> str:
    return catalog.model(model_id).digest


def _variant_source(variant: ModelVariant) -> tuple[dict[str, Any], dict[str, Any]]:
    value = variant.to_dict()
    return value["source"], value["runtime"]


def _receipt_validity(
    store: EvidenceStore,
    *,
    kind: str,
    digest: str,
    valid_until: Any,
    label: str,
) -> str:
    expires = _utc(valid_until, f"{label} valid_until")
    if expires <= store.now():
        raise CatalogError(f"{label} is expired")
    outer = _utc(store.valid_until(), f"{label} signed-attestation expiry")
    if expires > outer:
        raise CatalogError(f"{label} outlives its signed attestation")
    if expires < store.attestation_issued_at(kind, digest):
        raise CatalogError(f"{label} expires before it was signed")
    return valid_until


def _validate_attestor_policy(
    value: Mapping[str, Any] | None,
    trusted_attestors: Mapping[str, str],
) -> tuple[dict[str, tuple[str, ...]], str]:
    policy = _exact(
        value,
        {"schema", "principals", "separation"},
        "variant attestor policy",
    )
    if policy["schema"] != MODEL_VARIANT_ATTESTOR_POLICY_SCHEMA:
        raise CatalogError("variant attestor policy schema is unsupported")
    separation = _exact(
        policy["separation"],
        {"unique_key_per_role", "unique_group_per_role"},
        "variant attestor separation",
    )
    if separation != {"unique_key_per_role": True, "unique_group_per_role": True}:
        raise CatalogError("variant attestor policy must separate every exact role and group")
    principals = policy["principals"]
    if not isinstance(principals, Mapping) or set(principals) != set(REQUIRED_ATTESTOR_ROLES):
        raise CatalogError("variant attestor policy must pin one principal for every role")
    normalized: dict[str, tuple[str, ...]] = {}
    key_ids: set[str] = set()
    groups: set[str] = set()
    for role in REQUIRED_ATTESTOR_ROLES:
        principal = _exact(
            principals[role],
            {"role", "group", "enabled", "key_id", "public_key"},
            f"variant attestor principal {role}",
        )
        if principal["role"] != role or principal["enabled"] is not True:
            raise CatalogError("variant attestor principal is disabled or names another role")
        group = _text(principal["group"], f"variant attestor principal {role} group")
        assert group is not None
        if group != f"fs2-serve-variant-{role}":
            raise CatalogError("variant attestor principal group is not the exact role group")
        derived = raw_public_key_id(
            principal["public_key"], f"variant attestor principal {role} public key"
        )
        if principal["key_id"] != derived or trusted_attestors.get(derived) != principal["public_key"]:
            raise CatalogError("variant attestor principal key is not canonically trusted")
        if derived in key_ids or group in groups:
            raise CatalogError("variant attestor roles/groups must use distinct principals")
        key_ids.add(derived)
        groups.add(group)
        normalized[role] = (derived,)
    if set(trusted_attestors) != key_ids:
        raise CatalogError("variant trust set contains a principal outside the exact role policy")
    serializable = {
        "schema": policy["schema"],
        "principals": {role: dict(principals[role]) for role in REQUIRED_ATTESTOR_ROLES},
        "separation": dict(separation),
    }
    return normalized, hashlib.sha256(canonical_bytes(serializable)).hexdigest()


def _assert_role(
    store: EvidenceStore,
    policy: Mapping[str, tuple[str, ...]],
    *,
    role: str,
    kind: str,
    digest: str,
) -> str:
    key_id = store.attestation_key_id(kind, digest)
    if key_id not in policy[role]:
        raise CatalogError(f"variant {role} evidence was signed by a foreign role")
    return key_id


def _validate_artifact(
    store: EvidenceStore, digest: str, variant: ModelVariant
) -> ArtifactManifest:
    manifest = store.artifact(digest, variant.exposed_model_id)
    source, _ = _variant_source(variant)
    expected_identity = source["artifact"]
    if source["license"]["state"] == "blocked" or source["entitlement"] == "blocked":
        raise CatalogError("variant source license or entitlement remains blocked")
    files = [
        {"path": item.path, "bytes": item.bytes, "sha256": item.sha256}
        for item in manifest.files
    ]
    if (
        manifest.model_id != variant.exposed_model_id
        or manifest.kind != "weights"
        or manifest.source_revision != source["revision"]
        or manifest.license_state != "verified"
        or manifest.entitlement_state not in {"not-required", "verified"}
        or manifest.retention != "retained-platform"
    ):
        raise CatalogError("variant artifact is not a verified retained exact-revision weight set")
    expected_hashes = expected_identity["expected_content_sha256"]
    if expected_hashes and sorted(item.sha256 for item in manifest.files) != sorted(
        expected_hashes
    ):
        raise CatalogError("variant artifact file identities differ from static discovery")
    if (
        expected_identity["expected_file_count"] is not None
        and len(manifest.files) != expected_identity["expected_file_count"]
    ):
        raise CatalogError("variant artifact file count differs from static discovery")
    if (
        expected_identity["expected_bytes"] is not None
        and manifest.expanded_bytes != expected_identity["expected_bytes"]
    ):
        raise CatalogError("variant artifact bytes differ from static discovery")
    if (
        expected_identity["manifest_sha256"] is not None
        and manifest.digest != expected_identity["manifest_sha256"]
    ):
        raise CatalogError("variant artifact manifest differs from static discovery")
    store.assert_claims(
        "artifacts",
        digest,
        {
            "variant_id": variant.variant_id,
            "variant_digest": variant.digest,
            "source_revision": source["revision"],
            "artifact_content_digest": manifest.content_digest,
            "artifact_file_count": len(files),
            "artifact_expanded_bytes": manifest.expanded_bytes,
            "artifact_file_inventory_sha256": hashlib.sha256(
                canonical_bytes(files)
            ).hexdigest(),
        },
    )
    return manifest


def _canonical_b64(value: Any, label: str) -> bytes:
    text = _text(value, label)
    assert text is not None
    try:
        decoded = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise CatalogError(f"{label} is not canonical base64") from exc
    if base64.b64encode(decoded).decode("ascii") != text:
        raise CatalogError(f"{label} is not canonical base64")
    return decoded


def _dsse_pae(payload_type: str, payload: bytes) -> bytes:
    return (
        b"DSSEv1 "
        + str(len(payload_type)).encode()
        + b" "
        + payload_type.encode()
        + b" "
        + str(len(payload)).encode()
        + b" "
        + payload
    )


def _oci_subject(statement: Mapping[str, Any], image: Mapping[str, Any], label: str) -> None:
    subjects = _list(statement.get("subject"), f"{label} subjects", nonempty=True)
    if len(subjects) != 1:
        raise CatalogError(f"{label} must name exactly one OCI subject")
    subject = _exact(subjects[0], {"name", "digest"}, f"{label} OCI subject")
    digests = _exact(subject["digest"], {"sha256"}, f"{label} OCI digest")
    if subject["name"] != image["repository"] or digests["sha256"] != image["digest"][7:]:
        raise CatalogError(f"{label} names another OCI repository or image digest")


def _validate_supply_object(
    store: EvidenceStore,
    digest: str,
    *,
    subject_kind: str,
    variant: ModelVariant,
    image: Mapping[str, Any],
    source_revision: str,
    build_identity_sha256: str,
    build: Mapping[str, Any],
    policy: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    value = _exact(
        store.raw_object(
            "variant-supply-objects",
            digest,
            MODEL_VARIANT_SUPPLY_OBJECT_SCHEMA,
            variant.exposed_model_id,
        )[0],
        {
            "schema",
            "object_kind",
            "variant_id",
            "variant_digest",
            "observed_at",
            "payload",
            "valid_until",
        },
        "model variant supply object",
    )
    if (
        value["object_kind"] != subject_kind
        or value["variant_id"] != variant.variant_id
        or value["variant_digest"] != variant.digest
    ):
        raise CatalogError("variant supply object names another static variant")
    observed_at = _utc(value["observed_at"], f"variant {subject_kind} observed_at")
    if observed_at > store.attestation_issued_at("variant-supply-objects", digest):
        raise CatalogError("variant supply object was attested before observation")
    payload = value["payload"]
    if subject_kind == "signature":
        payload = _exact(
            payload,
            {
                "format",
                "payload_type",
                "payload",
                "signatures",
                "signer_key_id",
                "issuer_identity_sha256",
                "trust_policy_sha256",
            },
            "variant Cosign/DSSE bundle",
        )
        if (
            payload["format"] != "cosign-dsse-bundle"
            or payload["payload_type"] != "application/vnd.in-toto+json"
        ):
            raise CatalogError("variant signature format is unsupported")
        role = "supply-signature"
        signatures = _list(payload["signatures"], "variant DSSE signatures", nonempty=True)
        if len(signatures) != 1:
            raise CatalogError("variant DSSE bundle must have one exact trusted signature")
        signature = _exact(signatures[0], {"key_id", "sig"}, "variant DSSE signature")
        role_key = policy[role][0]
        if signature["key_id"] != role_key or payload["signer_key_id"] != role_key:
            raise CatalogError("variant DSSE signature uses another role principal")
        statement_bytes = _canonical_b64(payload["payload"], "variant DSSE payload")
        try:
            statement = json.loads(statement_bytes, object_pairs_hook=_reject_duplicate_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CatalogError("variant DSSE payload is not strict JSON") from exc
        if canonical_bytes(statement) != statement_bytes:
            raise CatalogError("variant DSSE payload bytes are not canonical JSON")
        try:
            Ed25519PublicKey.from_public_bytes(
                raw_public_key(
                    store.trusted_attestors[role_key], "variant DSSE trusted signer"
                )
            ).verify(
                raw_signature(signature["sig"], "variant DSSE signature"),
                _dsse_pae(payload["payload_type"], statement_bytes),
            )
        except (InvalidSignature, KeyError) as exc:
            raise CatalogError("variant DSSE signature/trust verification failed") from exc
        statement = _exact(
            statement,
            {"_type", "subject", "predicateType", "predicate"},
            "variant DSSE statement",
        )
        if (
            statement["_type"] != "https://in-toto.io/Statement/v1"
            or statement["predicateType"] != "https://cosign.sigstore.dev/attestation/v1"
        ):
            raise CatalogError("variant DSSE statement type is unsupported")
        _oci_subject(statement, image, "variant DSSE statement")
        predicate = _exact(
            statement["predicate"],
            {"source_revision", "oci_subject_sha256", "build_identity_sha256"},
            "variant DSSE predicate",
        )
        if predicate != {
            "source_revision": source_revision,
            "oci_subject_sha256": image["oci_subject_sha256"],
            "build_identity_sha256": build_identity_sha256,
        }:
            raise CatalogError("variant DSSE predicate names another immutable build")
        for key in ("issuer_identity_sha256", "trust_policy_sha256"):
            _digest(payload[key], f"variant DSSE {key}")
    elif subject_kind == "provenance":
        statement = _exact(
            payload,
            {"_type", "subject", "predicateType", "predicate"},
            "variant SLSA statement",
        )
        if (
            statement["_type"] != "https://in-toto.io/Statement/v1"
            or statement["predicateType"] != "https://slsa.dev/provenance/v1"
        ):
            raise CatalogError("variant provenance predicate is unsupported")
        role = "supply-provenance"
        _oci_subject(statement, image, "variant SLSA statement")
        predicate = _exact(
            statement["predicate"],
            {"buildDefinition", "runDetails"},
            "variant SLSA predicate",
        )
        build_definition = _exact(
            predicate["buildDefinition"],
            {"buildType", "externalParameters", "resolvedDependencies"},
            "variant SLSA build definition",
        )
        external = _exact(
            build_definition["externalParameters"],
            {"source_repository", "source_revision", "source_tree_sha256"},
            "variant SLSA external parameters",
        )
        dependencies = _list(
            build_definition["resolvedDependencies"],
            "variant SLSA resolved dependencies",
            nonempty=True,
        )
        run_details = _exact(
            predicate["runDetails"],
            {"builder", "metadata"},
            "variant SLSA run details",
        )
        builder = _exact(run_details["builder"], {"id"}, "variant SLSA builder")
        metadata = _exact(
            run_details["metadata"],
            {"invocationId", "startedOn", "finishedOn"},
            "variant SLSA metadata",
        )
        if (
            external != {
                "source_repository": variant.to_dict()["source"]["repository"],
                "source_revision": source_revision,
                "source_tree_sha256": build["source_tree_sha256"],
            }
            or build_definition["buildType"] != build["build_type"]
            or builder["id"] != build["builder_identity_sha256"]
            or dependencies
            != [
                {"uri": external["source_repository"], "digest": {"gitCommit": source_revision}},
                {"uri": "fs2://build-materials", "digest": {"sha256": build["materials_sha256"]}},
            ]
            or _utc(metadata["startedOn"], "variant SLSA startedOn")
            > _utc(metadata["finishedOn"], "variant SLSA finishedOn")
            or _utc(metadata["finishedOn"], "variant SLSA finishedOn") > observed_at
        ):
            raise CatalogError("variant SLSA provenance is detached from its source/build")
        _digest(metadata["invocationId"], "variant SLSA invocation ID")
    elif subject_kind == "sbom":
        document = _exact(
            payload,
            {
                "spdxVersion",
                "dataLicense",
                "SPDXID",
                "name",
                "documentNamespace",
                "creationInfo",
                "packages",
            },
            "variant SPDX document",
        )
        if document["spdxVersion"] != "SPDX-2.3" or document["dataLicense"] != "CC0-1.0":
            raise CatalogError("variant SBOM predicate is unsupported")
        role = "supply-sbom"
        creation = _exact(
            document["creationInfo"],
            {"created", "creators"},
            "variant SPDX creation info",
        )
        creators = _list(creation["creators"], "variant SPDX creators", nonempty=True)
        if (
            _utc(creation["created"], "variant SPDX created") > observed_at
            or any(not isinstance(item, str) or not item.startswith("Tool: ") for item in creators)
            or not isinstance(document["documentNamespace"], str)
            or not document["documentNamespace"].startswith("https://")
        ):
            raise CatalogError("variant SPDX metadata is invalid or post-observation")
        packages = _list(document["packages"], "variant SPDX packages", nonempty=True)
        if len(packages) != 1:
            raise CatalogError("variant SPDX document must bind one OCI package")
        package = _exact(
            packages[0],
            {"SPDXID", "name", "versionInfo", "downloadLocation", "checksums", "externalRefs"},
            "variant SPDX package",
        )
        checksums = _list(package["checksums"], "variant SPDX checksums", nonempty=True)
        refs = _list(package["externalRefs"], "variant SPDX external refs", nonempty=True)
        if (
            package["name"] != image["repository"]
            or package["versionInfo"] != image["digest"]
            or checksums != [{"algorithm": "SHA256", "checksumValue": image["digest"][7:]}]
            or refs
            != [{
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": f"pkg:oci/{image['repository']}@{image['digest']}",
            }]
        ):
            raise CatalogError("variant SPDX package names another OCI subject")
    elif subject_kind == "scan":
        payload = _exact(
            payload,
            {
                "schema",
                "image_repository",
                "image_digest",
                "oci_subject_sha256",
                "scanner",
                "scanner_database_sha256",
                "scanner_database_valid_until",
                "scanned_at",
                "critical_findings",
                "high_findings",
            },
            "variant scan statement",
        )
        if (
            payload["schema"] != "fs2-serve.nebius.ai/container-scan/v1"
            or payload["image_repository"] != image["repository"]
            or payload["image_digest"] != image["digest"]
            or payload["oci_subject_sha256"] != image["oci_subject_sha256"]
            or _bounded_count(payload["critical_findings"], "variant critical findings") != 0
            or _bounded_count(payload["high_findings"], "variant high findings") != 0
            or _utc(
                payload["scanner_database_valid_until"],
                "variant scanner database validity",
            )
            <= store.now()
            or _utc(payload["scanned_at"], "variant scan time") > observed_at
        ):
            raise CatalogError("variant container scan is stale or has blocking findings")
        if (
            not isinstance(payload["scanner"], str)
            or re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", payload["scanner"]) is None
        ):
            raise CatalogError("variant container scanner image is not immutable")
        _digest(payload["scanner_database_sha256"], "variant scanner database")
        role = "supply-scan"
    else:
        raise CatalogError("variant supply subject kind is unsupported")
    _assert_role(
        store,
        policy,
        role=role,
        kind="variant-supply-objects",
        digest=digest,
    )
    _receipt_validity(
        store,
        kind="variant-supply-objects",
        digest=digest,
        valid_until=value["valid_until"],
        label=f"variant {subject_kind} object",
    )
    store.assert_claims(
        "variant-supply-objects",
        digest,
        {
            "variant_digest": variant.digest,
            "object_kind": subject_kind,
            "image_digest": image["digest"],
            "oci_subject_sha256": image["oci_subject_sha256"],
            "source_revision": source_revision,
            "build_identity_sha256": build_identity_sha256,
            "raw_object_sha256": digest,
        },
    )
    return value


def _validate_supply(
    store: EvidenceStore,
    digest: str,
    variant: ModelVariant,
    manifest: ArtifactManifest,
    policy: Mapping[str, tuple[str, ...]],
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    value = _exact(
        store.receipt(
            "variant-supplies",
            digest,
            MODEL_VARIANT_SUPPLY_SCHEMA,
            variant.exposed_model_id,
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "variant_id",
            "variant_digest",
            "base_model_id",
            "exposed_model_id",
            "source",
            "artifact",
            "license",
            "runtime",
            "build",
            "attestations",
            "valid_until",
        },
        "model variant supply receipt",
    )
    source, runtime = _variant_source(variant)
    if (
        value["status"] != "PASS"
        or value["variant_id"] != variant.variant_id
        or value["variant_digest"] != variant.digest
        or value["base_model_id"] != variant.base_model_id
        or value["exposed_model_id"] != variant.exposed_model_id
    ):
        raise CatalogError("variant supply receipt names another static variant")
    receipt_source = _exact(
        value["source"],
        {"kind", "repository", "revision", "revision_url"},
        "variant supply source",
    )
    if receipt_source != {
        key: source[key] for key in ("kind", "repository", "revision", "revision_url")
    }:
        raise CatalogError("variant supply source differs from static discovery")
    artifact = _exact(
        value["artifact"],
        {
            "manifest_schema",
            "manifest_sha256",
            "content_sha256",
            "file_count",
            "expanded_bytes",
            "file_inventory_sha256",
        },
        "variant supply artifact",
    )
    files = [
        {"path": item.path, "bytes": item.bytes, "sha256": item.sha256}
        for item in manifest.files
    ]
    expected_artifact = {
        "manifest_schema": "fs2-serve.nebius.ai/artifact-manifest/v1",
        "manifest_sha256": manifest.digest,
        "content_sha256": manifest.content_digest,
        "file_count": len(files),
        "expanded_bytes": manifest.expanded_bytes,
        "file_inventory_sha256": hashlib.sha256(canonical_bytes(files)).hexdigest(),
    }
    if artifact != expected_artifact:
        raise CatalogError("variant supply receipt does not bind the full ordered manifest")
    license_value = _exact(
        value["license"],
        {"id", "source_url", "artifact_sha256", "revision"},
        "variant supply license",
    )
    static_license = source["license"]
    if (
        license_value["id"] != static_license["id"]
        or license_value["source_url"] != static_license["source_url"]
        or license_value["revision"] != source["revision"]
        or source["revision"] not in license_value["source_url"]
        or manifest.license_id != license_value["id"]
    ):
        raise CatalogError("variant supply license is not revision-bound to static discovery")
    _digest(license_value["artifact_sha256"], "variant immutable license artifact")
    license_bytes = store.raw_bytes(
        "variant-license-artifacts",
        license_value["artifact_sha256"],
        MODEL_VARIANT_LICENSE_ARTIFACT_SCHEMA,
        variant.exposed_model_id,
    )
    if not license_bytes.strip():
        raise CatalogError("variant immutable license artifact is empty")
    store.assert_claims(
        "variant-license-artifacts",
        license_value["artifact_sha256"],
        {
            "variant_digest": variant.digest,
            "source_revision": source["revision"],
            "license_id": license_value["id"],
            "source_url": license_value["source_url"],
            "raw_license_sha256": license_value["artifact_sha256"],
        },
    )
    _assert_role(
        store,
        policy,
        role="license",
        kind="variant-license-artifacts",
        digest=license_value["artifact_sha256"],
    )
    image = _exact(
        value["runtime"],
        {
            "architecture",
            "repository",
            "digest",
            "reference",
            "oci_subject_sha256",
        },
        "variant runtime image",
    )
    repository = _text(image["repository"], "variant image repository")
    assert repository is not None
    if "@" in repository or repository.startswith(("http://", "https://")):
        raise CatalogError("variant image repository is not a canonical OCI repository")
    image_digest = _digest(image["digest"], "variant image digest", image=True)
    if (
        image["architecture"] != runtime["architecture"]
        or image["reference"] != f"{repository}@{image_digest}"
        or image["oci_subject_sha256"]
        != hashlib.sha256(
            canonical_bytes({"repository": repository, "digest": image_digest})
        ).hexdigest()
    ):
        raise CatalogError("variant runtime image does not bind repository@digest OCI subject")
    build = _exact(
        value["build"],
        {
            "source_repository",
            "source_revision",
            "source_tree_sha256",
            "materials_sha256",
            "builder_identity_sha256",
            "build_type",
        },
        "variant build",
    )
    if (
        build["source_repository"] != source["repository"]
        or build["source_revision"] != source["revision"]
        or not isinstance(build["build_type"], str)
        or not build["build_type"]
    ):
        raise CatalogError("variant build is detached from its immutable source")
    for key in ("source_tree_sha256", "materials_sha256", "builder_identity_sha256"):
        _digest(build[key], f"variant build {key}")
    attestation_refs = _exact(
        value["attestations"],
        {
            "signature_object_digest",
            "provenance_object_digest",
            "sbom_object_digest",
            "scan_object_digest",
        },
        "variant supply attestations",
    )
    if len(set(attestation_refs.values())) != 4:
        raise CatalogError("variant supply subjects must be four distinct immutable receipts")
    build_identity_sha256 = hashlib.sha256(canonical_bytes(build)).hexdigest()
    subjects = {}
    for subject_kind in ("signature", "provenance", "sbom", "scan"):
        subject_digest = _digest(
            attestation_refs[f"{subject_kind}_object_digest"],
            f"variant {subject_kind} object digest",
        )
        subjects[subject_kind] = _validate_supply_object(
            store,
            subject_digest,
            subject_kind=subject_kind,
            variant=variant,
            image=image,
            source_revision=source["revision"],
            build_identity_sha256=build_identity_sha256,
            build=build,
            policy=policy,
        )
    supply_issued_at = store.attestation_issued_at("variant-supplies", digest)
    if supply_issued_at < max(
        (
            *(store.attestation_issued_at("variant-supply-objects", subject_digest)
              for subject_digest in attestation_refs.values()),
            store.attestation_issued_at(
                "variant-license-artifacts", license_value["artifact_sha256"]
            ),
        )
    ):
        raise CatalogError("variant supply receipt was signed before its reopened subjects")
    _receipt_validity(
        store,
        kind="variant-supplies",
        digest=digest,
        valid_until=value["valid_until"],
        label="variant supply receipt",
    )
    store.assert_claims(
        "variant-supplies",
        digest,
        {
            "variant_digest": variant.digest,
            "source_revision": source["revision"],
            "artifact_manifest_digest": manifest.digest,
            "artifact_content_digest": manifest.content_digest,
            "license_artifact_sha256": license_value["artifact_sha256"],
            "image_reference": image["reference"],
            "image_digest": image_digest,
            "oci_subject_sha256": image["oci_subject_sha256"],
            "build_identity_sha256": build_identity_sha256,
            "supply_attestation_set_sha256": hashlib.sha256(
                canonical_bytes(attestation_refs)
            ).hexdigest(),
        },
    )
    _assert_role(store, policy, role="supply", kind="variant-supplies", digest=digest)
    return value, tuple(subjects[kind] for kind in sorted(subjects))


def _validate_runtime_tuple(
    store: EvidenceStore,
    digest: str,
    variant: ModelVariant,
    manifest: ArtifactManifest,
    supply: Mapping[str, Any],
    policy: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    value = _exact(
        store.receipt(
            "variant-runtime-tuples",
            digest,
            MODEL_VARIANT_RUNTIME_TUPLE_SCHEMA,
            variant.exposed_model_id,
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "captured_at",
            "variant_id",
            "variant_digest",
            "supply_receipt_digest",
            "worker",
            "runtime",
            "artifact",
            "kernels",
            "valid_until",
        },
        "model variant runtime tuple",
    )
    if (
        value["status"] != "PASS"
        or value["variant_id"] != variant.variant_id
        or value["variant_digest"] != variant.digest
        or value["supply_receipt_digest"] != supply["receipt_digest"]
    ):
        raise CatalogError("variant runtime tuple names another supply subject")
    captured_at = _utc(value["captured_at"], "variant runtime captured_at")
    if captured_at > store.attestation_issued_at("variant-runtime-tuples", digest):
        raise CatalogError("variant runtime tuple was signed before capture")
    worker = _exact(
        value["worker"],
        {
            "project_sha256",
            "region",
            "cluster_sha256",
            "node_name",
            "node_uid",
            "worker_image_reference",
            "worker_image_digest",
            "driver_version",
            "cuda_version",
            "device_plugin_reference",
            "device_plugin_digest",
            "gpu_class",
            "gpu_architecture",
            "compute_capability",
            "allocated_gpu_count",
            "gpu_uuids",
        },
        "variant worker tuple",
    )
    for label in ("project_sha256", "cluster_sha256"):
        _digest(worker[label], f"variant worker {label}")
    if (
        worker["region"] != "us-north1"
        or K8S_UID.fullmatch(worker["node_uid"]) is None
        or DRIVER_VERSION.fullmatch(worker["driver_version"]) is None
        or worker["gpu_class"] != "NVIDIA B300"
        or worker["gpu_architecture"] != "sm_103"
        or worker["compute_capability"] != "10.3"
        or worker["allocated_gpu_count"] != 1
    ):
        raise CatalogError("variant runtime tuple is not exact single-GPU B300 SM103")
    cuda_match = CUDA_VERSION.fullmatch(worker["cuda_version"])
    if cuda_match is None or int(cuda_match.group("major")) < 13:
        raise CatalogError("variant runtime tuple requires CUDA 13 or newer")
    for reference_key, digest_key in (
        ("worker_image_reference", "worker_image_digest"),
        ("device_plugin_reference", "device_plugin_digest"),
    ):
        image_digest = _digest(worker[digest_key], f"variant worker {digest_key}", image=True)
        if worker[reference_key] is None or not worker[reference_key].endswith("@" + image_digest):
            raise CatalogError("variant worker images must use repository@digest references")
    gpu_uuids = _list(worker["gpu_uuids"], "variant worker GPU UUIDs", nonempty=True)
    if len(gpu_uuids) != 1 or not isinstance(gpu_uuids[0], str) or not gpu_uuids[0].startswith(
        "GPU-"
    ):
        raise CatalogError("variant runtime tuple lacks one exact GPU UUID")
    runtime = _exact(
        value["runtime"],
        {
            "architecture",
            "image_repository",
            "image_reference",
            "image_digest",
            "source_revision",
            "argv_sha256",
            "execution_identity_sha256",
            "network_startup",
        },
        "variant runtime execution",
    )
    supplied_runtime = supply["runtime"]
    if (
        runtime["architecture"] != variant.runtime_architecture
        or runtime["image_repository"] != supplied_runtime["repository"]
        or runtime["image_reference"] != supplied_runtime["reference"]
        or runtime["image_digest"] != supplied_runtime["digest"]
        or runtime["source_revision"] != supply["source"]["revision"]
        or runtime["network_startup"] != "deny-egress-mounted-content-address-only"
    ):
        raise CatalogError("variant runtime execution differs from signed supply")
    _digest(runtime["argv_sha256"], "variant runtime argv")
    _digest(runtime["execution_identity_sha256"], "variant runtime execution identity")
    artifact = _exact(
        value["artifact"],
        {"manifest_sha256", "content_sha256", "mount_read_only", "network_denied_startup"},
        "variant runtime artifact",
    )
    if artifact != {
        "manifest_sha256": manifest.digest,
        "content_sha256": manifest.content_digest,
        "mount_read_only": True,
        "network_denied_startup": True,
    }:
        raise CatalogError("variant runtime did not consume the exact mounted artifact offline")
    kernels = _list(value["kernels"], "variant runtime kernel dispatches", nonempty=True)
    names: list[str] = []
    for raw in kernels:
        kernel = _exact(
            raw,
            {"name", "architecture", "binary_sha256", "dispatch_count"},
            "variant native kernel dispatch",
        )
        if (
            not isinstance(kernel["name"], str)
            or KERNEL_NAME.fullmatch(kernel["name"]) is None
            or kernel["name"].lower() in {"kernel", "native-kernel", "sm103-kernel"}
            or kernel["architecture"] != "sm_103"
        ):
            raise CatalogError("variant native kernel name/architecture is not measured SM103")
        _digest(kernel["binary_sha256"], "variant kernel binary")
        _positive_int(kernel["dispatch_count"], "variant kernel dispatch count")
        names.append(kernel["name"])
    if names != sorted(set(names)):
        raise CatalogError("variant kernel dispatches must be sorted and unique")
    _receipt_validity(
        store,
        kind="variant-runtime-tuples",
        digest=digest,
        valid_until=value["valid_until"],
        label="variant runtime tuple",
    )
    store.assert_claims(
        "variant-runtime-tuples",
        digest,
        {
            "variant_digest": variant.digest,
            "supply_receipt_digest": supply["receipt_digest"],
            "worker_identity_sha256": hashlib.sha256(canonical_bytes(worker)).hexdigest(),
            "runtime_identity_sha256": hashlib.sha256(canonical_bytes(runtime)).hexdigest(),
            "artifact_manifest_digest": manifest.digest,
            "artifact_content_digest": manifest.content_digest,
            "kernel_dispatch_set_sha256": hashlib.sha256(canonical_bytes(kernels)).hexdigest(),
        },
    )
    _assert_role(
        store, policy, role="runtime", kind="variant-runtime-tuples", digest=digest
    )
    return value


def _gateway_subject(binding: ServingBinding, *, backend_service_uid: str) -> dict[str, Any]:
    return {
        "gateway_class": binding.gateway_class,
        "gateway_namespace": binding.gateway_namespace,
        "gateway_service_name": binding.gateway_service_name,
        "gateway_service_uid": binding.gateway_service_uid,
        "gateway_identity_sha256": binding.gateway_identity_sha256,
        "gateway_auth_class": binding.gateway_auth_class,
        "route_id": binding.model_id,
        "backend_namespace": binding.backend_namespace,
        "backend_service_name": binding.backend_service_name,
        "backend_service_uid": backend_service_uid,
        "backend_port": binding.backend_port,
        "transport": "gateway-proxy",
    }


def _validate_semantic_receipt(
    store: EvidenceStore,
    digest: str,
    *,
    variant: ModelVariant,
    runtime_digest: str,
    manifest: ArtifactManifest,
    semantic_contract: SemanticRequestContract,
    semantic_validator: Mapping[str, Any],
    binding: ServingBinding,
    backend_service_uid: str,
    attempt_id: str,
    attempt_completed: datetime,
    policy: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    value = _exact(
        store.receipt(
            "variant-semantics",
            digest,
            MODEL_VARIANT_SEMANTIC_SCHEMA,
            variant.exposed_model_id,
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "variant_id",
            "variant_digest",
            "attempt_id",
            "observed_at",
            "runtime_tuple_digest",
            "artifact_manifest_digest",
            "semantic_contract_digest",
            "gateway",
            "operation",
            "protocol",
            "requests",
            "valid_until",
        },
        "model variant semantic receipt",
    )
    invocation = semantic_contract.invocation
    observed_at = _utc(value["observed_at"], "variant semantic observed_at")
    if (
        value["status"] != "PASS"
        or value["variant_id"] != variant.variant_id
        or value["variant_digest"] != variant.digest
        or value["attempt_id"] != attempt_id
        or value["runtime_tuple_digest"] != runtime_digest
        or value["artifact_manifest_digest"] != manifest.digest
        or value["semantic_contract_digest"] != semantic_contract.digest
        or value["operation"] != invocation["operation"]
        or value["protocol"] != invocation["protocol"]
        or observed_at != attempt_completed
    ):
        raise CatalogError("variant semantic receipt differs from its canonical contract")
    gateway = _exact(
        value["gateway"],
        set(_gateway_subject(binding, backend_service_uid=backend_service_uid)),
        "variant semantic gateway identity",
    )
    expected_gateway = _gateway_subject(binding, backend_service_uid=backend_service_uid)
    if gateway != expected_gateway:
        raise CatalogError("variant semantic receipt used a Pod-local or substituted backend path")
    requests = _list(value["requests"], "variant semantic requests", nonempty=True)
    if len(requests) != 2:
        raise CatalogError("variant semantic qualification requires exactly two responses")
    expected = list(zip(semantic_contract.request_ids, semantic_contract.request_sha256))
    responses: list[str] = []
    normalized: list[dict[str, Any]] = []
    validator = semantic_validator
    for raw, (request_id, request_sha256) in zip(requests, expected, strict=True):
        request = _exact(
            raw,
            {
                "request_id",
                "request_sha256",
                "response_sha256",
                "validator_source_sha256",
                "validator_fixture_sha256",
                "validation_result_sha256",
                "semantic_valid",
            },
            "variant semantic request result",
        )
        if (
            request["request_id"] != request_id
            or request["request_sha256"] != request_sha256
            or request["validator_source_sha256"] != validator["source_sha256"]
            or request["validator_fixture_sha256"] != validator["fixture_sha256"]
            or request["semantic_valid"] is not True
        ):
            raise CatalogError("variant semantic request/validator subject was substituted")
        for key in ("response_sha256", "validation_result_sha256"):
            _digest(request[key], f"variant semantic {key}")
        responses.append(request["response_sha256"])
        normalized.append(request)
    if len(set(semantic_contract.request_sha256)) != 2 or len(set(responses)) != 2:
        raise CatalogError("variant semantic requests and responses must both be distinct")
    _receipt_validity(
        store,
        kind="variant-semantics",
        digest=digest,
        valid_until=value["valid_until"],
        label="variant semantic receipt",
    )
    store.assert_claims(
        "variant-semantics",
        digest,
        {
            "variant_digest": variant.digest,
            "attempt_id": attempt_id,
            "runtime_tuple_digest": runtime_digest,
            "artifact_manifest_digest": manifest.digest,
            "semantic_contract_digest": semantic_contract.digest,
            "gateway_identity_sha256": hashlib.sha256(
                canonical_bytes(gateway)
            ).hexdigest(),
            "request_result_set_sha256": hashlib.sha256(
                canonical_bytes(normalized)
            ).hexdigest(),
        },
    )
    if store.attestation_issued_at("variant-semantics", digest) < observed_at:
        raise CatalogError("variant semantic result was attested before observation")
    _assert_role(
        store, policy, role="semantic", kind="variant-semantics", digest=digest
    )
    return value


def _validate_cohort(
    store: EvidenceStore,
    digest: str,
    *,
    expected_kind: str,
    variant: ModelVariant,
    runtime_digest: str,
    runtime_tuple: Mapping[str, Any],
    manifest: ArtifactManifest,
    semantic_contract: SemanticRequestContract,
    semantic_validator: Mapping[str, Any],
    binding: ServingBinding,
    backend_service_uid: str,
    policy: Mapping[str, tuple[str, ...]],
    preemption: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    value = _exact(
        store.receipt(
            "variant-cohorts",
            digest,
            MODEL_VARIANT_COHORT_SCHEMA,
            variant.exposed_model_id,
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "variant_id",
            "variant_digest",
            "cohort_kind",
            "runtime_tuple_digest",
            "artifact_manifest_digest",
            "attempts_total",
            "successes_total",
            "failures_total",
            "failures_in_denominator",
            "attempts",
            "valid_until",
        },
        "model variant cohort",
    )
    if (
        value["status"] != "PASS"
        or value["variant_id"] != variant.variant_id
        or value["variant_digest"] != variant.digest
        or value["cohort_kind"] != expected_kind
        or value["runtime_tuple_digest"] != runtime_digest
        or value["artifact_manifest_digest"] != manifest.digest
        or value["failures_in_denominator"] is not True
    ):
        raise CatalogError("variant cohort differs from the exact runtime/artifact subject")
    attempts = _list(value["attempts"], "variant cohort attempts", nonempty=True)
    minimum = 10 if expected_kind == "warm" else 3
    if len(attempts) < minimum or value["attempts_total"] != len(attempts):
        raise CatalogError(f"variant {expected_kind} cohort is below its minimum attempt count")
    successes = failures = 0
    attempt_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    semantic_receipts: list[dict[str, Any]] = []
    previous_completed: datetime | None = None
    expected_node_uid = runtime_tuple["worker"]["node_uid"]
    expected_gpu_uuid = runtime_tuple["worker"]["gpu_uuids"][0]
    for raw in attempts:
        attempt = _exact(
            raw,
            {
                "attempt_id",
                "status",
                "t0",
                "completed_at",
                "duration_seconds",
                "failure_reason",
                "semantic_receipt_digest",
                "output_sha256",
                "kernel_dispatch_sha256",
                "pre_t0_work_sha256",
                "pod_uid",
                "node_uid",
                "gpu_uuid",
                "cold_boundary_receipt_digest",
            },
            "variant cohort attempt",
        )
        attempt_id = _text(attempt["attempt_id"], "variant cohort attempt ID")
        assert attempt_id is not None
        if attempt_id in attempt_ids:
            raise CatalogError("variant cohort attempt ID was replayed")
        attempt_ids.add(attempt_id)
        status = _enum(attempt["status"], {"PASS", "FAIL"}, "variant attempt status")
        started = _utc(attempt["t0"], "variant attempt T0")
        completed = _utc(attempt["completed_at"], "variant attempt completion")
        duration = _bounded_number(
            attempt["duration_seconds"],
            "variant attempt duration",
            maximum=MAX_DURATION_SECONDS,
        )
        if (
            completed < started
            or abs(duration - (completed - started).total_seconds())
            > MAX_DURATION_ERROR_SECONDS
        ):
            raise CatalogError("variant cohort attempt duration/timestamps are inconsistent")
        if previous_completed is not None and started < previous_completed:
            raise CatalogError("variant cohort attempts overlap or are not chronologically ordered")
        previous_completed = completed
        _digest(attempt["pre_t0_work_sha256"], "variant attempt pre-T0 work")
        expected_pod_uid: str | None = None
        if expected_kind == "warm" and preemption is not None and attempt_id == preemption["attempt_id"]:
            expected_pod_uid = preemption["replacement_identity"]["pod_uid"]
            expected_node_uid = preemption["replacement_identity"]["node_uid"]
            expected_gpu_uuid = preemption["replacement_identity"]["gpu_uuid"]
        else:
            expected_node_uid = runtime_tuple["worker"]["node_uid"]
            expected_gpu_uuid = runtime_tuple["worker"]["gpu_uuids"][0]
        if (
            K8S_UID.fullmatch(attempt["pod_uid"]) is None
            or K8S_UID.fullmatch(attempt["node_uid"]) is None
            or not attempt["gpu_uuid"].startswith("GPU-")
            or attempt["node_uid"] != expected_node_uid
            or attempt["gpu_uuid"] != expected_gpu_uuid
            or (expected_pod_uid is not None and attempt["pod_uid"] != expected_pod_uid)
        ):
            raise CatalogError("variant attempt lacks exact Pod/Node/GPU identity")
        if expected_kind == "cold":
            cold_digest = _digest(
                attempt["cold_boundary_receipt_digest"],
                "variant cold-boundary receipt digest",
            )
            _validate_cold_boundary(
                store,
                cold_digest,
                attempt=attempt,
                variant=variant,
                runtime_digest=runtime_digest,
                runtime_tuple=runtime_tuple,
                manifest=manifest,
                policy=policy,
            )
        elif attempt["cold_boundary_receipt_digest"] is not None:
            raise CatalogError("warm attempt cannot claim a cold-boundary receipt")
        if status == "PASS":
            successes += 1
            if attempt["failure_reason"] is not None:
                raise CatalogError("successful variant attempt cannot claim a failure")
            for key in ("semantic_receipt_digest", "output_sha256", "kernel_dispatch_sha256"):
                _digest(attempt[key], f"variant successful attempt {key}")
            semantic_receipts.append(
                _validate_semantic_receipt(
                    store,
                    attempt["semantic_receipt_digest"],
                    variant=variant,
                    runtime_digest=runtime_digest,
                    manifest=manifest,
                    semantic_contract=semantic_contract,
                    semantic_validator=semantic_validator,
                    binding=binding,
                    backend_service_uid=backend_service_uid,
                    attempt_id=attempt_id,
                    attempt_completed=completed,
                    policy=policy,
                )
            )
            if preemption is not None and attempt_id == preemption["attempt_id"] and attempt["semantic_receipt_digest"] != preemption["semantic_receipt_digest"]:
                raise CatalogError("preemption attempt/semantic pair is not exact")
        else:
            failures += 1
            if (
                not isinstance(attempt["failure_reason"], str)
                or not attempt["failure_reason"]
                or any(
                    attempt[key] is not None
                    for key in (
                        "semantic_receipt_digest",
                        "output_sha256",
                        "kernel_dispatch_sha256",
                    )
                )
            ):
                raise CatalogError("failed variant attempt is missing its retained outcome")
        normalized.append(attempt)
    if (
        successes == 0
        or value["successes_total"] != successes
        or value["failures_total"] != failures
        or successes + failures != value["attempts_total"]
    ):
        raise CatalogError("variant cohort denominator does not retain every outcome")
    failure_rate = failures / len(attempts)
    if failure_rate > MAX_FAILURE_RATE:
        raise CatalogError("variant cohort exceeds the promotion failure-rate threshold")
    if expected_kind == "warm" and preemption is not None and sum(
        item["attempt_id"] == preemption["attempt_id"] for item in attempts
    ) != 1:
        raise CatalogError("preemption attempt is absent or duplicated in the warm cohort")
    _receipt_validity(
        store,
        kind="variant-cohorts",
        digest=digest,
        valid_until=value["valid_until"],
        label="variant cohort",
    )
    store.assert_claims(
        "variant-cohorts",
        digest,
        {
            "variant_digest": variant.digest,
            "cohort_kind": expected_kind,
            "runtime_tuple_digest": runtime_digest,
            "artifact_manifest_digest": manifest.digest,
            "attempt_set_sha256": hashlib.sha256(canonical_bytes(normalized)).hexdigest(),
            "attempts_total": len(attempts),
            "successes_total": successes,
            "failures_total": failures,
        },
    )
    assert previous_completed is not None
    if store.attestation_issued_at("variant-cohorts", digest) < previous_completed:
        raise CatalogError("variant cohort was attested before its final observation")
    _assert_role(store, policy, role="cohort", kind="variant-cohorts", digest=digest)
    return value, tuple(semantic_receipts)


def _validate_cohort_separation(
    cold: Mapping[str, Any], warm: Mapping[str, Any]
) -> None:
    cold_attempts = cold["attempts"]
    warm_attempts = warm["attempts"]
    cold_ids = {item["attempt_id"] for item in cold_attempts}
    warm_ids = {item["attempt_id"] for item in warm_attempts}
    if cold_ids & warm_ids:
        raise CatalogError("cold and warm attempt IDs must be globally unique")
    cold_pods = [item["pod_uid"] for item in cold_attempts]
    if len(set(cold_pods)) != len(cold_pods):
        raise CatalogError("each cold attempt must prove a distinct new Pod UID")
    cold_end = max(_utc(item["completed_at"], "cold attempt completion") for item in cold_attempts)
    warm_start = min(_utc(item["t0"], "warm attempt T0") for item in warm_attempts)
    if cold_end > warm_start:
        raise CatalogError("cold and warm cohorts overlap on the same GPU")
    combined = sorted(
        (*cold_attempts, *warm_attempts),
        key=lambda item: _utc(item["t0"], "variant global attempt T0"),
    )
    last_by_gpu: dict[tuple[str, str], datetime] = {}
    for attempt in combined:
        identity = (attempt["node_uid"], attempt["gpu_uuid"])
        started = _utc(attempt["t0"], "variant global attempt T0")
        completed = _utc(attempt["completed_at"], "variant global completion")
        if identity in last_by_gpu and started < last_by_gpu[identity]:
            raise CatalogError("variant attempts overlap on one Node/GPU identity")
        last_by_gpu[identity] = completed


def _load_k8s_observation(
    store: EvidenceStore,
    digest: str,
    *,
    expected_kind: str,
    model_id: str,
    cluster_sha256: str,
    policy: Mapping[str, tuple[str, ...]],
    role: str,
) -> dict[str, Any]:
    value = _exact(
        store.raw_object(
            "variant-kubernetes-observations",
            digest,
            MODEL_VARIANT_K8S_OBSERVATION_SCHEMA,
            model_id,
        )[0],
        {
            "schema",
            "object_kind",
            "observed_at",
            "valid_until",
            "observer",
            "object",
        },
        "variant Kubernetes observation",
    )
    if value["object_kind"] != expected_kind:
        raise CatalogError("variant Kubernetes observation has another object kind")
    observer = _exact(
        value["observer"],
        {
            "source",
            "cluster_identity_sha256",
            "api_server_identity_sha256",
            "service_account_uid",
            "complete",
        },
        "variant Kubernetes observer",
    )
    if (
        observer["source"] != "kubernetes-apiserver"
        or observer["cluster_identity_sha256"] != cluster_sha256
        or observer["complete"] is not True
        or K8S_UID.fullmatch(observer["service_account_uid"]) is None
    ):
        raise CatalogError("variant Kubernetes observation is not a complete API-server view")
    _digest(observer["api_server_identity_sha256"], "variant Kubernetes API identity")
    observed = _utc(value["observed_at"], "variant Kubernetes observed_at")
    if observed > store.attestation_issued_at("variant-kubernetes-observations", digest):
        raise CatalogError("variant Kubernetes object was attested before observation")
    _receipt_validity(
        store,
        kind="variant-kubernetes-observations",
        digest=digest,
        valid_until=value["valid_until"],
        label="variant Kubernetes observation",
    )
    _assert_role(
        store,
        policy,
        role=role,
        kind="variant-kubernetes-observations",
        digest=digest,
    )
    store.assert_claims(
        "variant-kubernetes-observations",
        digest,
        {
            "object_kind": expected_kind,
            "cluster_identity_sha256": cluster_sha256,
            "raw_object_sha256": digest,
        },
    )
    if not isinstance(value["object"], dict):
        raise CatalogError("variant Kubernetes observation object must be an object")
    return value


def _validate_cold_boundary(
    store: EvidenceStore,
    digest: str,
    *,
    attempt: Mapping[str, Any],
    variant: ModelVariant,
    runtime_digest: str,
    runtime_tuple: Mapping[str, Any],
    manifest: ArtifactManifest,
    policy: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    value = _exact(
        store.receipt(
            "variant-cold-boundaries",
            digest,
            MODEL_VARIANT_COLD_BOUNDARY_SCHEMA,
            variant.exposed_model_id,
        ),
        {
            "schema", "receipt_digest", "status", "variant_id", "variant_digest",
            "attempt_id", "runtime_tuple_digest", "artifact_manifest_digest",
            "pod_uid", "node_uid", "gpu_uuid", "process_identity_sha256",
            "cache_generation_sha256", "observations", "ready_at", "t0",
            "completed_at", "valid_until",
        },
        "variant cold-boundary receipt",
    )
    worker = runtime_tuple["worker"]
    if (
        value["status"] != "PASS" or value["variant_id"] != variant.variant_id
        or value["variant_digest"] != variant.digest
        or value["attempt_id"] != attempt["attempt_id"]
        or value["runtime_tuple_digest"] != runtime_digest
        or value["artifact_manifest_digest"] != manifest.digest
        or value["pod_uid"] != attempt["pod_uid"]
        or value["node_uid"] != attempt["node_uid"]
        or value["gpu_uuid"] != attempt["gpu_uuid"]
        or value["t0"] != attempt["t0"] or value["completed_at"] != attempt["completed_at"]
    ):
        raise CatalogError("cold-boundary receipt names another attempt/runtime")
    _digest(value["process_identity_sha256"], "cold process identity")
    _digest(value["cache_generation_sha256"], "cold cache generation")
    refs = _exact(
        value["observations"],
        {"pod_absence", "pod", "node", "pod_resources", "process_cache"},
        "cold-boundary observations",
    )
    if len(set(refs.values())) != 5:
        raise CatalogError("cold-boundary observations must be distinct")
    observed = {
        kind: _load_k8s_observation(
            store,
            _digest(raw, f"cold-boundary {kind} observation"),
            expected_kind=kind,
            model_id=variant.exposed_model_id,
            cluster_sha256=worker["cluster_sha256"],
            policy=policy,
            role="cold-boundary",
        )
        for kind, raw in refs.items()
    }
    absence = _exact(
        observed["pod_absence"]["object"],
        {"namespace", "label_selector", "continue", "remainingItemCount", "items", "gpu_processes", "replicas"},
        "cold API Pod absence",
    )
    if (
        absence["namespace"] != "fs2-models"
        or absence["label_selector"] != f"fs2.nebius/model-id={variant.exposed_model_id}"
        or absence["continue"] != "" or absence["remainingItemCount"] != 0
        or absence["items"] != [] or absence["gpu_processes"] != 0 or absence["replicas"] != 0
    ):
        raise CatalogError("cold boundary does not prove complete zero/Pod absence")
    pod = _exact(
        observed["pod"]["object"],
        {"namespace", "name", "uid", "resourceVersion", "nodeName", "imageID", "startedAt"},
        "cold attempt Pod",
    )
    if (
        pod["uid"] != value["pod_uid"] or pod["nodeName"] != worker["node_name"]
        or pod["imageID"] != runtime_tuple["runtime"]["image_reference"]
        or K8S_UID.fullmatch(pod["uid"]) is None or not pod["resourceVersion"]
    ):
        raise CatalogError("cold boundary new Pod differs from the exact runtime")
    node = _exact(observed["node"]["object"], {"name", "uid", "resourceVersion"}, "cold attempt Node")
    if node["name"] != worker["node_name"] or node["uid"] != value["node_uid"] or not node["resourceVersion"]:
        raise CatalogError("cold attempt Node differs from the runtime tuple")
    resources = _exact(
        observed["pod_resources"]["object"],
        {"pod_uid", "container", "resource_name", "device_ids"},
        "cold attempt PodResources",
    )
    if resources != {"pod_uid": value["pod_uid"], "container": "model", "resource_name": "nvidia.com/gpu", "device_ids": [value["gpu_uuid"]]}:
        raise CatalogError("cold attempt does not bind the exact Pod/GPU allocation")
    boundary = _exact(
        observed["process_cache"]["object"],
        {"pod_uid", "process_started_at", "process_identity_sha256", "gpu_clients_before", "cache_state", "cache_generation_sha256", "writer_count"},
        "cold process/cache boundary",
    )
    if (
        boundary["pod_uid"] != value["pod_uid"]
        or boundary["process_identity_sha256"] != value["process_identity_sha256"]
        or boundary["cache_generation_sha256"] != value["cache_generation_sha256"]
        or boundary["gpu_clients_before"] != 0 or boundary["writer_count"] != 0
        or boundary["cache_state"] != "cold-empty-or-version-absent"
    ):
        raise CatalogError("cold attempt process/cache boundary is not exact zero state")
    absence_at = _utc(observed["pod_absence"]["observed_at"], "cold absence observed_at")
    process_at = _utc(boundary["process_started_at"], "cold process start")
    ready_at = _utc(value["ready_at"], "cold ready_at")
    t0 = _utc(value["t0"], "cold T0")
    completed = _utc(value["completed_at"], "cold completion")
    if not absence_at < process_at <= ready_at <= t0 <= completed:
        raise CatalogError("cold boundary chronology is not zero-start-ready-T0-complete")
    if store.attestation_issued_at("variant-cold-boundaries", digest) < completed:
        raise CatalogError("cold boundary was attested before attempt completion")
    _receipt_validity(store, kind="variant-cold-boundaries", digest=digest, valid_until=value["valid_until"], label="variant cold boundary")
    store.assert_claims(
        "variant-cold-boundaries", digest,
        {"variant_digest": variant.digest, "attempt_id": attempt["attempt_id"], "runtime_tuple_digest": runtime_digest, "artifact_manifest_digest": manifest.digest, "observation_set_sha256": hashlib.sha256(canonical_bytes(refs)).hexdigest(), "pod_uid": value["pod_uid"], "node_uid": value["node_uid"], "gpu_uuid": value["gpu_uuid"], "process_identity_sha256": value["process_identity_sha256"], "cache_generation_sha256": value["cache_generation_sha256"]},
    )
    _assert_role(store, policy, role="cold-boundary", kind="variant-cold-boundaries", digest=digest)
    return value


def _validate_preemption(
    store: EvidenceStore,
    digest: str,
    *,
    variant: ModelVariant,
    runtime_digest: str,
    runtime_tuple: Mapping[str, Any],
    manifest: ArtifactManifest,
    readiness_digest: str,
    readiness: Mapping[str, Any],
    policy: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    value = _exact(
        store.receipt("variant-preemptions", digest, MODEL_VARIANT_PREEMPTION_SCHEMA, variant.exposed_model_id),
        {"schema", "receipt_digest", "status", "variant_id", "variant_digest", "attempt_id", "semantic_receipt_digest", "runtime_tuple_digest", "artifact_manifest_digest", "backend_readiness_receipt_digest", "observations", "old_identity", "replacement_identity", "old_fencing_token", "replacement_fencing_token", "observed_at", "valid_until"},
        "variant preemption receipt",
    )
    if (
        value["status"] != "PASS" or value["variant_id"] != variant.variant_id
        or value["variant_digest"] != variant.digest or value["runtime_tuple_digest"] != runtime_digest
        or value["artifact_manifest_digest"] != manifest.digest
        or value["backend_readiness_receipt_digest"] != readiness_digest
    ):
        raise CatalogError("variant preemption names another route/runtime")
    refs = _exact(value["observations"], {"old_pod", "old_node", "old_pod_resources", "event", "old_fence", "replacement_pod", "replacement_node", "replacement_pod_resources"}, "preemption observations")
    if len(set(refs.values())) != 8:
        raise CatalogError("preemption observations must be distinct")
    worker = runtime_tuple["worker"]
    observed = {kind: _load_k8s_observation(store, _digest(raw, f"preemption {kind} observation"), expected_kind=kind, model_id=variant.exposed_model_id, cluster_sha256=worker["cluster_sha256"], policy=policy, role="preemption") for kind, raw in refs.items()}
    old = _exact(value["old_identity"], {"pod_uid", "node_uid", "gpu_uuid"}, "preempted identity")
    replacement = _exact(value["replacement_identity"], {"pod_uid", "node_uid", "gpu_uuid"}, "replacement identity")
    if old != {"pod_uid": readiness["ready_pod_uid"], "node_uid": readiness["ready_node_uid"], "gpu_uuid": readiness["ready_gpu_uuid"]}:
        raise CatalogError("preemption old identity does not join the ready runtime")
    if len(set((old["pod_uid"], replacement["pod_uid"]))) != 2 or old["node_uid"] == replacement["node_uid"] or old["gpu_uuid"] == replacement["gpu_uuid"]:
        raise CatalogError("preemption replacement Pod/Node/GPU must all be distinct")
    old_pod = _exact(observed["old_pod"]["object"], {"uid", "node_uid", "imageID"}, "preempted Pod")
    old_node = _exact(observed["old_node"]["object"], {"uid"}, "preempted Node")
    old_gpu = _exact(observed["old_pod_resources"]["object"], {"pod_uid", "gpu_uuid"}, "preempted PodResources")
    event = _exact(observed["event"]["object"], {"reason", "regarding_uid", "event_time", "attempt_id"}, "preemption Event")
    fence = _exact(observed["old_fence"]["object"], {"pod_uid", "pod_absent", "node_fenced", "gpu_clients", "fencing_token"}, "old preemption fence")
    new_pod = _exact(observed["replacement_pod"]["object"], {"uid", "node_uid", "imageID", "ready"}, "replacement Pod")
    new_node = _exact(observed["replacement_node"]["object"], {"uid", "gpu_class", "compute_capability"}, "replacement Node")
    new_gpu = _exact(observed["replacement_pod_resources"]["object"], {"pod_uid", "gpu_uuid"}, "replacement PodResources")
    if (
        old_pod != {"uid": old["pod_uid"], "node_uid": old["node_uid"], "imageID": runtime_tuple["runtime"]["image_reference"]}
        or old_node != {"uid": old["node_uid"]} or old_gpu != {"pod_uid": old["pod_uid"], "gpu_uuid": old["gpu_uuid"]}
        or event["reason"] != "Preempted" or event["regarding_uid"] != old["pod_uid"] or event["attempt_id"] != value["attempt_id"]
        or fence != {"pod_uid": old["pod_uid"], "pod_absent": True, "node_fenced": True, "gpu_clients": 0, "fencing_token": value["old_fencing_token"]}
        or new_pod != {"uid": replacement["pod_uid"], "node_uid": replacement["node_uid"], "imageID": runtime_tuple["runtime"]["image_reference"], "ready": True}
        or new_node != {"uid": replacement["node_uid"], "gpu_class": "NVIDIA B300", "compute_capability": "10.3"}
        or new_gpu != {"pod_uid": replacement["pod_uid"], "gpu_uuid": replacement["gpu_uuid"]}
        or not (0 <= _bounded_count(value["old_fencing_token"], "old preemption fence") < _bounded_count(value["replacement_fencing_token"], "replacement preemption fence"))
    ):
        raise CatalogError("preemption API transition is not exact, fenced, and replacement-safe")
    latest = max(_utc(item["observed_at"], "preemption API observation") for item in observed.values())
    if _utc(event["event_time"], "preemption event time") > latest or _utc(value["observed_at"], "preemption observed_at") < latest or store.attestation_issued_at("variant-preemptions", digest) < latest:
        raise CatalogError("preemption chronology or attestation issuance is invalid")
    _receipt_validity(store, kind="variant-preemptions", digest=digest, valid_until=value["valid_until"], label="variant preemption")
    store.assert_claims("variant-preemptions", digest, {"variant_digest": variant.digest, "attempt_id": value["attempt_id"], "semantic_receipt_digest": value["semantic_receipt_digest"], "runtime_tuple_digest": runtime_digest, "artifact_manifest_digest": manifest.digest, "backend_readiness_receipt_digest": readiness_digest, "observation_set_sha256": hashlib.sha256(canonical_bytes(refs)).hexdigest(), "old_identity_sha256": hashlib.sha256(canonical_bytes(old)).hexdigest(), "replacement_identity_sha256": hashlib.sha256(canonical_bytes(replacement)).hexdigest(), "old_fencing_token": value["old_fencing_token"], "replacement_fencing_token": value["replacement_fencing_token"]})
    _assert_role(store, policy, role="preemption", kind="variant-preemptions", digest=digest)
    return value


def _validate_backend_readiness(
    store: EvidenceStore,
    digest: str,
    *,
    variant: ModelVariant,
    binding: ServingBinding,
    runtime_digest: str,
    runtime_tuple: Mapping[str, Any],
    manifest: ArtifactManifest,
    backend_service_uid: str,
    policy: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    value = _exact(
        store.receipt(
            "variant-backend-readiness",
            digest,
            MODEL_VARIANT_BACKEND_READINESS_SCHEMA,
            variant.exposed_model_id,
        ),
        {
            "schema", "receipt_digest", "status", "observed_at", "variant_id",
            "variant_digest", "serving_binding_digest", "runtime_tuple_digest",
            "artifact_manifest_digest", "observation_digests", "valid_until",
        },
        "model variant backend readiness receipt",
    )
    if (
        value["status"] != "PASS"
        or value["variant_id"] != variant.variant_id
        or value["variant_digest"] != variant.digest
        or value["serving_binding_digest"] != binding.binding_digest
        or value["runtime_tuple_digest"] != runtime_digest
        or value["artifact_manifest_digest"] != manifest.digest
    ):
        raise CatalogError("variant backend readiness names another route subject")
    worker = runtime_tuple["worker"]
    refs = _exact(
        value["observation_digests"],
        {"service", "endpoint_slice", "pod", "node", "pod_resources", "probe"},
        "variant backend readiness observations",
    )
    if len(set(refs.values())) != len(refs):
        raise CatalogError("variant backend readiness observations must be distinct")
    observations = {
        kind: _load_k8s_observation(
            store,
            _digest(raw, f"variant backend {kind} observation"),
            expected_kind=kind,
            model_id=variant.exposed_model_id,
            cluster_sha256=worker["cluster_sha256"],
            policy=policy,
            role="backend-readiness",
        )
        for kind, raw in refs.items()
    }
    service = _exact(observations["service"]["object"], {"apiVersion", "kind", "metadata", "spec"}, "observed Service")
    smeta = _exact(service["metadata"], {"namespace", "name", "uid", "resourceVersion"}, "observed Service metadata")
    sspec = _exact(service["spec"], {"selector", "ports"}, "observed Service spec")
    selector = _exact(sspec["selector"], {"app.kubernetes.io/name", "fs2.nebius/model-id"}, "observed Service selector")
    ports = _list(sspec["ports"], "observed Service ports", nonempty=True)
    if (
        service["apiVersion"] != "v1" or service["kind"] != "Service"
        or smeta != {"namespace": binding.backend_namespace, "name": binding.backend_service_name, "uid": backend_service_uid, "resourceVersion": smeta["resourceVersion"]}
        or not smeta["resourceVersion"]
        or selector["fs2.nebius/model-id"] != variant.exposed_model_id
        or ports != [{"name": "http", "port": binding.backend_port, "targetPort": "http"}]
    ):
        raise CatalogError("observed Service differs from the serving binding")
    endpoint = _exact(observations["endpoint_slice"]["object"], {"apiVersion", "kind", "metadata", "addressType", "ports", "endpoints"}, "observed EndpointSlice")
    emeta = _exact(endpoint["metadata"], {"namespace", "name", "uid", "resourceVersion", "labels", "ownerReferences"}, "observed EndpointSlice metadata")
    owners = _list(emeta["ownerReferences"], "EndpointSlice owners", nonempty=True)
    endpoints = _list(endpoint["endpoints"], "EndpointSlice endpoints", nonempty=True)
    ep = _exact(endpoints[0], {"addresses", "conditions", "targetRef", "nodeName"}, "ready EndpointSlice endpoint")
    conditions = _exact(ep["conditions"], {"ready", "serving", "terminating"}, "EndpointSlice conditions")
    target = _exact(ep["targetRef"], {"kind", "namespace", "name", "uid"}, "EndpointSlice targetRef")
    if (
        endpoint["apiVersion"] != "discovery.k8s.io/v1" or endpoint["kind"] != "EndpointSlice"
        or emeta["namespace"] != binding.backend_namespace
        or emeta["labels"] != {"kubernetes.io/service-name": binding.backend_service_name}
        or owners != [{"apiVersion": "v1", "kind": "Service", "name": binding.backend_service_name, "uid": backend_service_uid, "controller": True}]
        or endpoint["addressType"] != "IPv4"
        or endpoint["ports"] != [{"name": "http", "port": binding.backend_port, "protocol": "TCP"}]
        or len(endpoints) != 1 or conditions != {"ready": True, "serving": True, "terminating": False}
    ):
        raise CatalogError("EndpointSlice does not belong to the exact ready Service")
    pod = _exact(observations["pod"]["object"], {"apiVersion", "kind", "metadata", "spec", "status"}, "observed Pod")
    pmeta = _exact(pod["metadata"], {"namespace", "name", "uid", "resourceVersion", "labels"}, "observed Pod metadata")
    pspec = _exact(pod["spec"], {"nodeName", "containers"}, "observed Pod spec")
    pstatus = _exact(pod["status"], {"phase", "podIP", "conditions", "containerStatuses"}, "observed Pod status")
    containers = _list(pspec["containers"], "observed Pod containers", nonempty=True)
    container = _exact(containers[0], {"name", "image", "resources", "ports"}, "observed runtime container")
    statuses = _list(pstatus["containerStatuses"], "observed container statuses", nonempty=True)
    cstatus = _exact(statuses[0], {"name", "ready", "image", "imageID"}, "observed runtime status")
    if (
        pmeta["namespace"] != binding.backend_namespace
        or pmeta["labels"] != selector
        or target != {"kind": "Pod", "namespace": pmeta["namespace"], "name": pmeta["name"], "uid": pmeta["uid"]}
        or ep["addresses"] != [pstatus["podIP"]] or ep["nodeName"] != pspec["nodeName"]
        or pspec["nodeName"] != worker["node_name"] or pstatus["phase"] != "Running"
        or pstatus["conditions"] != [{"type": "Ready", "status": "True"}]
        or container["name"] != "model" or container["image"] != runtime_tuple["runtime"]["image_reference"]
        or container["resources"] != {"limits": {"nvidia.com/gpu": 1}, "requests": {"nvidia.com/gpu": 1}}
        or container["ports"] != [{"name": "http", "containerPort": binding.backend_port}]
        or cstatus != {"name": "model", "ready": True, "image": container["image"], "imageID": runtime_tuple["runtime"]["image_reference"]}
    ):
        raise CatalogError("ready EndpointSlice target Pod does not match the runtime tuple")
    node = _exact(observations["node"]["object"], {"apiVersion", "kind", "metadata", "status"}, "observed Node")
    nmeta = _exact(node["metadata"], {"name", "uid", "resourceVersion", "labels"}, "observed Node metadata")
    if nmeta["name"] != worker["node_name"] or nmeta["uid"] != worker["node_uid"] or nmeta["labels"] != {"nvidia.com/gpu.product": "NVIDIA-B300", "nvidia.com/gpu.compute.major": "10", "nvidia.com/gpu.compute.minor": "3"}:
        raise CatalogError("ready Pod Node does not match the exact B300 runtime tuple")
    pod_resources = _exact(observations["pod_resources"]["object"], {"podUid", "namespace", "name", "containers"}, "observed PodResources")
    if pod_resources != {"podUid": pmeta["uid"], "namespace": pmeta["namespace"], "name": pmeta["name"], "containers": [{"name": "model", "devices": [{"resourceName": "nvidia.com/gpu", "deviceIds": [worker["gpu_uuids"][0]]}]}]}:
        raise CatalogError("PodResources does not bind the exact ready Pod/GPU allocation")
    readiness = _exact(observations["probe"]["object"], {"transport", "gateway_service_uid", "backend_service_uid", "pod_uid", "pod_ip", "method", "path", "port", "status"}, "observed readiness probe")
    observed_at = _utc(value["observed_at"], "variant backend observed_at")
    if (
        readiness != {"transport": "gateway-proxy", "gateway_service_uid": binding.gateway_service_uid, "backend_service_uid": backend_service_uid, "pod_uid": pmeta["uid"], "pod_ip": pstatus["podIP"], "method": "GET", "path": "/health/ready", "port": binding.backend_port, "status": 200}
        or max(_utc(item["observed_at"], "backend API observation time") for item in observations.values()) > observed_at
        or store.attestation_issued_at("variant-backend-readiness", digest) < observed_at
    ):
        raise CatalogError("variant backend readiness probe is substituted or pre-attested")
    _receipt_validity(
        store,
        kind="variant-backend-readiness",
        digest=digest,
        valid_until=value["valid_until"],
        label="variant backend readiness",
    )
    store.assert_claims(
        "variant-backend-readiness",
        digest,
        {
            "variant_digest": variant.digest,
            "serving_binding_digest": binding.binding_digest,
            "runtime_tuple_digest": runtime_digest,
            "artifact_manifest_digest": manifest.digest,
            "observation_set_sha256": hashlib.sha256(canonical_bytes(refs)).hexdigest(),
            "service_uid": backend_service_uid,
            "pod_uid": pmeta["uid"],
            "node_uid": nmeta["uid"],
            "gpu_uuid": worker["gpu_uuids"][0],
            "probe_sha256": hashlib.sha256(canonical_bytes(readiness)).hexdigest(),
        },
    )
    _assert_role(
        store,
        policy,
        role="backend-readiness",
        kind="variant-backend-readiness",
        digest=digest,
    )
    return {**value, "ready_pod_uid": pmeta["uid"], "ready_node_uid": nmeta["uid"], "ready_gpu_uuid": worker["gpu_uuids"][0]}


def _validate_lifecycle_pair(
    store: EvidenceStore,
    zero_digest: str,
    return_digest: str,
    *,
    variant: ModelVariant,
    binding: ServingBinding,
    scale_contract_digest: str,
    runtime_digest: str,
    runtime_tuple: Mapping[str, Any],
    manifest: ArtifactManifest,
    readiness_digest: str,
    backend_service_uid: str,
    policy: Mapping[str, tuple[str, ...]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if zero_digest == return_digest:
        raise CatalogError("zero-to-ready and return-to-zero lifecycle subjects must differ")

    def load_one(digest: str, action: str) -> dict[str, Any]:
        value = _exact(
            store.receipt(
                "variant-lifecycles",
                digest,
                MODEL_VARIANT_LIFECYCLE_SCHEMA,
                variant.exposed_model_id,
            ),
            {
                "schema",
                "receipt_digest",
                "status",
                "action",
                "observed_at",
                "variant_id",
                "variant_digest",
                "serving_binding_digest",
                "scale_contract_digest",
                "runtime_tuple_digest",
                "artifact_manifest_digest",
                "backend_readiness_receipt_digest",
                "operation_id",
                "previous_fencing_token",
                "fencing_token",
                "replicas",
                "backend_service_uid",
                "node_uid",
                "gpu_uuid",
                "artifact_retained",
                "valid_until",
            },
            f"variant {action} lifecycle receipt",
        )
        worker = runtime_tuple["worker"]
        if (
            value["status"] != "PASS"
            or value["action"] != action
            or value["variant_id"] != variant.variant_id
            or value["variant_digest"] != variant.digest
            or value["serving_binding_digest"] != binding.binding_digest
            or value["scale_contract_digest"] != scale_contract_digest
            or value["runtime_tuple_digest"] != runtime_digest
            or value["artifact_manifest_digest"] != manifest.digest
            or value["backend_readiness_receipt_digest"] != readiness_digest
            or value["backend_service_uid"] != backend_service_uid
            or value["node_uid"] != worker["node_uid"]
            or value["gpu_uuid"] != worker["gpu_uuids"][0]
            or value["artifact_retained"] is not True
        ):
            raise CatalogError("variant lifecycle receipt names another route/runtime subject")
        replicas = _exact(
            value["replicas"], {"previous", "desired", "observed"}, "variant lifecycle replicas"
        )
        expected_replicas = (
            {"previous": 0, "desired": 1, "observed": 1}
            if action == "activate"
            else {"previous": 1, "desired": 0, "observed": 0}
        )
        if replicas != expected_replicas:
            raise CatalogError("variant lifecycle does not prove zero-ready-zero")
        observed_at = _utc(value["observed_at"], f"variant {action} observed_at")
        if store.attestation_issued_at("variant-lifecycles", digest) < observed_at:
            raise CatalogError("variant lifecycle was attested before observation")
        _receipt_validity(
            store,
            kind="variant-lifecycles",
            digest=digest,
            valid_until=value["valid_until"],
            label=f"variant {action} lifecycle",
        )
        store.assert_claims(
            "variant-lifecycles",
            digest,
            {
                "variant_digest": variant.digest,
                "action": action,
                "serving_binding_digest": binding.binding_digest,
                "scale_contract_digest": scale_contract_digest,
                "runtime_tuple_digest": runtime_digest,
                "artifact_manifest_digest": manifest.digest,
                "backend_readiness_receipt_digest": readiness_digest,
                "operation_id": value["operation_id"],
                "previous_fencing_token": value["previous_fencing_token"],
                "fencing_token": value["fencing_token"],
                "replica_transition_sha256": hashlib.sha256(
                    canonical_bytes(replicas)
                ).hexdigest(),
                "backend_service_uid": backend_service_uid,
                "node_uid": value["node_uid"],
                "gpu_uuid": value["gpu_uuid"],
            },
        )
        _assert_role(
            store,
            policy,
            role="lifecycle",
            kind="variant-lifecycles",
            digest=digest,
        )
        return value

    zero = load_one(zero_digest, "activate")
    returned = load_one(return_digest, "deactivate")
    if (
        zero["operation_id"] == returned["operation_id"]
        or zero["fencing_token"] < 1
        or returned["previous_fencing_token"] != zero["fencing_token"]
        or returned["fencing_token"] != zero["fencing_token"] + 1
        or _utc(returned["observed_at"], "variant deactivation observed_at")
        <= _utc(zero["observed_at"], "variant activation observed_at")
    ):
        raise CatalogError("variant lifecycle operations are replayed or non-monotonic")
    return zero, returned


def _validate_qualification(
    store: EvidenceStore,
    digest: str,
    *,
    variant: ModelVariant,
    supply_digest: str,
    runtime_digest: str,
    runtime_tuple: Mapping[str, Any],
    manifest: ArtifactManifest,
    cold: Mapping[str, Any],
    warm: Mapping[str, Any],
    semantic_contract: SemanticRequestContract,
    binding: ServingBinding,
    backend_service_uid: str,
    backend_readiness_digest: str,
    preemption: Mapping[str, Any],
    zero_lifecycle: Mapping[str, Any],
    return_lifecycle: Mapping[str, Any],
    policy: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    value = _exact(
        store.receipt(
            "variant-qualifications",
            digest,
            MODEL_VARIANT_QUALIFICATION_SCHEMA,
            variant.exposed_model_id,
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "variant_id",
            "variant_digest",
            "supply_receipt_digest",
            "runtime_tuple_digest",
            "artifact_manifest_digest",
            "semantic_contract_digest",
            "cold_cohort_digest",
            "warm_cohort_digest",
            "backend_readiness_receipt_digest",
            "measurement",
            "quality",
            "preemption_receipt_digest",
            "lifecycle",
            "gateway",
            "vendor_baseline",
            "valid_until",
        },
        "model variant qualification receipt",
    )
    if (
        value["status"] != "PASS"
        or value["variant_id"] != variant.variant_id
        or value["variant_digest"] != variant.digest
        or value["supply_receipt_digest"] != supply_digest
        or value["runtime_tuple_digest"] != runtime_digest
        or value["artifact_manifest_digest"] != manifest.digest
        or value["semantic_contract_digest"] != semantic_contract.digest
        or value["cold_cohort_digest"] != cold["receipt_digest"]
        or value["warm_cohort_digest"] != warm["receipt_digest"]
        or value["backend_readiness_receipt_digest"] != backend_readiness_digest
        or value["preemption_receipt_digest"] != preemption["receipt_digest"]
    ):
        raise CatalogError("variant qualification does not join its signed subjects")
    measurement = _exact(
        value["measurement"],
        {
            "compute_capability",
            "gpu_architecture",
            "warm_attempts_total",
            "warm_failures_total",
            "warm_failure_rate",
            "max_warm_failure_rate",
            "cold_attempts_total",
            "cold_failures_total",
            "cold_failure_rate",
            "max_cold_failure_rate",
            "failures_in_denominator",
            "determinism_attempt_ids",
            "kernel_dispatch_attempt_ids",
            "semantic_responses_per_success",
        },
        "variant qualification measurement",
    )
    determinism_ids = _list(
        measurement["determinism_attempt_ids"], "variant determinism attempts", nonempty=True
    )
    kernel_ids = _list(
        measurement["kernel_dispatch_attempt_ids"],
        "variant kernel-dispatch attempts",
        nonempty=True,
    )
    warm_successes = {
        item["attempt_id"] for item in warm["attempts"] if item["status"] == "PASS"
    }
    warm_attempts_total = _bounded_count(
        measurement["warm_attempts_total"], "variant warm attempts", minimum=10
    )
    cold_attempts_total = _bounded_count(
        measurement["cold_attempts_total"], "variant cold attempts", minimum=3
    )
    warm_failures_total = _bounded_count(
        measurement["warm_failures_total"], "variant warm failures"
    )
    cold_failures_total = _bounded_count(
        measurement["cold_failures_total"], "variant cold failures"
    )
    warm_failure_rate = _bounded_number(
        measurement["warm_failure_rate"], "variant warm failure rate"
    )
    cold_failure_rate = _bounded_number(
        measurement["cold_failure_rate"], "variant cold failure rate"
    )
    max_warm_failure_rate = _bounded_number(
        measurement["max_warm_failure_rate"], "variant maximum warm failure rate"
    )
    max_cold_failure_rate = _bounded_number(
        measurement["max_cold_failure_rate"], "variant maximum cold failure rate"
    )
    if (
        measurement["compute_capability"] != "10.3"
        or measurement["gpu_architecture"] != "sm_103"
        or warm_attempts_total != warm["attempts_total"]
        or warm_failures_total != warm["failures_total"]
        or cold_attempts_total != cold["attempts_total"]
        or cold_failures_total != cold["failures_total"]
        or not math.isclose(warm_failure_rate, warm_failures_total / warm_attempts_total)
        or not math.isclose(cold_failure_rate, cold_failures_total / cold_attempts_total)
        or max_warm_failure_rate != MAX_FAILURE_RATE
        or max_cold_failure_rate != MAX_FAILURE_RATE
        or warm_failure_rate > max_warm_failure_rate
        or cold_failure_rate > max_cold_failure_rate
        or measurement["failures_in_denominator"] is not True
        or measurement["semantic_responses_per_success"] != 2
        or len(determinism_ids) < 3
        or len(kernel_ids) < 3
        or determinism_ids != sorted(set(determinism_ids))
        or kernel_ids != sorted(set(kernel_ids))
        or not set(determinism_ids).issubset(warm_successes)
        or not set(kernel_ids).issubset(warm_successes)
    ):
        raise CatalogError("variant qualification measurements are incomplete or relabeled")
    warm_by_id = {item["attempt_id"]: item for item in warm["attempts"]}
    if len({warm_by_id[item]["output_sha256"] for item in determinism_ids}) != 1:
        raise CatalogError("variant determinism attempts did not repeat the same output identity")
    kernel_dispatch_identity = hashlib.sha256(
        canonical_bytes(runtime_tuple["kernels"])
    ).hexdigest()
    if any(
        warm_by_id[item]["kernel_dispatch_sha256"] != kernel_dispatch_identity
        for item in kernel_ids
    ):
        raise CatalogError("variant cohort kernel dispatch does not match the runtime tuple")
    quality = _exact(
        value["quality"],
        {
            "status",
            "comparator_model_id",
            "comparator_execution_identity_sha256",
            "dataset_sha256",
            "metric",
            "candidate_value",
            "baseline_value",
            "allowed_regression",
        },
        "variant quality comparison",
    )
    if quality["status"] != "PASS" or quality["metric"] not in {
        "exact-match-rate",
        "success-rate",
        "task-score",
    }:
        raise CatalogError("variant quality comparator is not an approved typed measurement")
    for key in ("comparator_execution_identity_sha256", "dataset_sha256"):
        _digest(quality[key], f"variant quality {key}")
    for key in ("candidate_value", "baseline_value", "allowed_regression"):
        _bounded_number(quality[key], f"variant quality {key}")
    if quality["candidate_value"] + quality["allowed_regression"] < quality["baseline_value"]:
        raise CatalogError("variant quality does not preserve the named baseline")
    preemption_attempt = warm_by_id.get(preemption["attempt_id"])
    if (
        preemption_attempt is None
        or preemption_attempt["status"] != "PASS"
        or preemption_attempt["semantic_receipt_digest"]
        != preemption["semantic_receipt_digest"]
        or _utc(preemption["observed_at"], "variant preemption observed_at")
        >= _utc(preemption_attempt["t0"], "variant preemption attempt T0")
    ):
        raise CatalogError("variant preemption attempt/semantic pair is not exact")
    lifecycle = _exact(
        value["lifecycle"],
        {
            "status",
            "zero_to_ready_operation_id",
            "return_to_zero_operation_id",
            "zero_to_ready_receipt_sha256",
            "return_to_zero_receipt_sha256",
            "initial_replicas",
            "ready_replicas",
            "final_replicas",
            "activation_fencing_token",
            "deactivation_fencing_token",
            "artifact_retained",
        },
        "variant zero-ready-zero qualification",
    )
    if (
        lifecycle["status"] != "PASS"
        or lifecycle["zero_to_ready_operation_id"] != zero_lifecycle["operation_id"]
        or lifecycle["return_to_zero_operation_id"] != return_lifecycle["operation_id"]
        or lifecycle["zero_to_ready_receipt_sha256"] != zero_lifecycle["receipt_digest"]
        or lifecycle["return_to_zero_receipt_sha256"]
        != return_lifecycle["receipt_digest"]
        or lifecycle["initial_replicas"] != 0
        or lifecycle["ready_replicas"] < 1
        or lifecycle["final_replicas"] != 0
        or lifecycle["activation_fencing_token"] < 1
        or lifecycle["activation_fencing_token"] != zero_lifecycle["fencing_token"]
        or lifecycle["deactivation_fencing_token"]
        != return_lifecycle["fencing_token"]
        or lifecycle["artifact_retained"] is not True
    ):
        raise CatalogError("variant lifecycle is not monotonic zero-ready-zero")
    for key in ("zero_to_ready_receipt_sha256", "return_to_zero_receipt_sha256"):
        _digest(lifecycle[key], f"variant lifecycle {key}")
    gateway = _exact(
        value["gateway"],
        set(_gateway_subject(binding, backend_service_uid=backend_service_uid)),
        "variant qualification gateway identity",
    )
    if gateway != _gateway_subject(binding, backend_service_uid=backend_service_uid):
        raise CatalogError("variant qualification bypasses the canonical gateway/binding")
    baseline = _exact(
        value["vendor_baseline"],
        {
            "mode",
            "schema",
            "model_id",
            "record_sha256",
            "execution_identity_sha256",
            "b300_state",
        },
        "variant vendor baseline",
    )
    static_baseline = variant.to_dict()["relationship"]["vendor_baseline"]
    if baseline != static_baseline:
        raise CatalogError("variant qualification substituted its canonical vendor baseline")
    if (
        quality["comparator_model_id"] != baseline["model_id"]
        or quality["comparator_execution_identity_sha256"]
        != baseline["execution_identity_sha256"]
    ):
        raise CatalogError("variant quality comparator differs from the canonical vendor baseline")
    _receipt_validity(
        store,
        kind="variant-qualifications",
        digest=digest,
        valid_until=value["valid_until"],
        label="variant qualification",
    )
    store.assert_claims(
        "variant-qualifications",
        digest,
        {
            "variant_digest": variant.digest,
            "supply_receipt_digest": supply_digest,
            "runtime_tuple_digest": runtime_digest,
            "artifact_manifest_digest": manifest.digest,
            "cold_cohort_digest": cold["receipt_digest"],
            "warm_cohort_digest": warm["receipt_digest"],
            "measurement_sha256": hashlib.sha256(canonical_bytes(measurement)).hexdigest(),
            "quality_sha256": hashlib.sha256(canonical_bytes(quality)).hexdigest(),
            "preemption_receipt_digest": preemption["receipt_digest"],
            "lifecycle_sha256": hashlib.sha256(canonical_bytes(lifecycle)).hexdigest(),
            "backend_readiness_receipt_digest": backend_readiness_digest,
            "gateway_identity_sha256": hashlib.sha256(canonical_bytes(gateway)).hexdigest(),
            "vendor_baseline_sha256": hashlib.sha256(canonical_bytes(baseline)).hexdigest(),
        },
    )
    latest_observation = max(
        _utc(item["completed_at"], "variant qualification attempt completion")
        for item in (*cold["attempts"], *warm["attempts"])
    )
    latest_observation = max(
        latest_observation,
        _utc(return_lifecycle["observed_at"], "variant return-to-zero observation"),
    )
    if store.attestation_issued_at("variant-qualifications", digest) < latest_observation:
        raise CatalogError("variant qualification was attested before its observations")
    _assert_role(
        store,
        policy,
        role="qualification",
        kind="variant-qualifications",
        digest=digest,
    )
    return value


def _validate_review(
    store: EvidenceStore,
    digest: str,
    *,
    variant: ModelVariant,
    candidate_id: str,
    candidate_digest: str,
    runtime_profile: str,
    canonical_model_digest: str,
    binding_digest: str,
    scale_contract_digest: str,
    evidence: Mapping[str, str],
    supply: Mapping[str, Any],
    cold: Mapping[str, Any],
    warm: Mapping[str, Any],
    policy: Mapping[str, tuple[str, ...]],
    policy_digest: str,
) -> dict[str, Any]:
    value = _exact(
        store.receipt(
            "variant-reviews",
            digest,
            MODEL_VARIANT_REVIEW_SCHEMA,
            variant.exposed_model_id,
        ),
        {
            "schema",
            "receipt_digest",
            "status",
            "decision",
            "variant_id",
            "candidate_id",
            "candidate_digest",
            "runtime_profile",
            "variant_digest",
            "canonical_model_digest",
            "serving_binding_digest",
            "scale_contract_digest",
            "attestor_policy_sha256",
            "artifact_manifest_digest",
            "supply_receipt_digest",
            "runtime_tuple_digest",
            "cold_cohort_digest",
            "warm_cohort_digest",
            "qualification_receipt_digest",
            "backend_readiness_receipt_digest",
            "preemption_receipt_digest",
            "zero_to_ready_receipt_digest",
            "return_to_zero_receipt_digest",
            "reviewer_identity_sha256",
            "review_commit",
            "valid_until",
        },
        "model variant independent review",
    )
    expected = {
        "variant_id": variant.variant_id,
        "candidate_id": candidate_id,
        "candidate_digest": candidate_digest,
        "runtime_profile": runtime_profile,
        "variant_digest": variant.digest,
        "canonical_model_digest": canonical_model_digest,
        "serving_binding_digest": binding_digest,
        "scale_contract_digest": scale_contract_digest,
        "attestor_policy_sha256": policy_digest,
        **evidence,
    }
    if value["status"] != "PASS" or value["decision"] != "approve-route":
        raise CatalogError("model variant lacks an independent positive route review")
    for key, expected_value in expected.items():
        if value[key] != expected_value:
            raise CatalogError("model variant review names another route/evidence subject")
    _digest(value["reviewer_identity_sha256"], "variant reviewer identity")
    review_commit = value["review_commit"]
    if not isinstance(review_commit, str) or re.fullmatch(r"[0-9a-f]{40}", review_commit) is None:
        raise CatalogError("variant review commit is not immutable")
    _receipt_validity(
        store,
        kind="variant-reviews",
        digest=digest,
        valid_until=value["valid_until"],
        label="variant independent review",
    )
    review_key_id = _assert_role(
        store, policy, role="review", kind="variant-reviews", digest=digest
    )
    evidence_signers = {
        store.attestation_key_id("artifacts", evidence["artifact_manifest_digest"]),
        store.attestation_key_id("variant-supplies", evidence["supply_receipt_digest"]),
        store.attestation_key_id(
            "variant-runtime-tuples", evidence["runtime_tuple_digest"]
        ),
        store.attestation_key_id("variant-cohorts", evidence["cold_cohort_digest"]),
        store.attestation_key_id("variant-cohorts", evidence["warm_cohort_digest"]),
        store.attestation_key_id(
            "variant-qualifications", evidence["qualification_receipt_digest"]
        ),
        store.attestation_key_id(
            "variant-backend-readiness", evidence["backend_readiness_receipt_digest"]
        ),
        store.attestation_key_id(
            "variant-preemptions", evidence["preemption_receipt_digest"]
        ),
        store.attestation_key_id(
            "variant-lifecycles", evidence["zero_to_ready_receipt_digest"]
        ),
        store.attestation_key_id(
            "variant-lifecycles", evidence["return_to_zero_receipt_digest"]
        ),
    }
    evidence_signers.update(
        store.attestation_key_id("variant-semantics", item["semantic_receipt_digest"])
        for item in (*cold["attempts"], *warm["attempts"])
        if item["status"] == "PASS"
    )
    evidence_signers.update(
        store.attestation_key_id("variant-supply-objects", subject_digest)
        for subject_digest in supply["attestations"].values()
    )
    if review_key_id in evidence_signers or value[
        "reviewer_identity_sha256"
    ] != hashlib.sha256(review_key_id.encode()).hexdigest():
        raise CatalogError("variant route review is not signed by an independent reviewer")
    qualification_issued = store.attestation_issued_at(
        "variant-qualifications", evidence["qualification_receipt_digest"]
    )
    if store.attestation_issued_at("variant-reviews", digest) < qualification_issued:
        raise CatalogError("variant route review was attested before qualification")
    store.assert_claims(
        "variant-reviews",
        digest,
        {
            **expected,
            "decision": "approve-route",
            "reviewer_identity_sha256": value["reviewer_identity_sha256"],
            "review_commit": review_commit,
        },
    )
    return value


@dataclass(frozen=True)
class VariantPromotion:
    """One signed, expiring variant route authority."""

    variant_id: str
    candidate_id: str
    candidate_digest: str
    runtime_profile: str
    base_model_id: str
    exposed_model_id: str
    variant_digest: str
    canonical_model_digest: str
    serving_binding_digest: str
    scale_contract_digest: str
    runtime_image_reference: str
    runtime_image_digest: str
    artifact_manifest_digest: str
    valid_until: str
    binding: ServingBinding

    def valid_at(self, when: datetime | None = None) -> bool:
        current = when or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise CatalogError("variant promotion validity check must be timezone-aware")
        return current.astimezone(timezone.utc).replace(microsecond=0) < _utc(
            self.valid_until, "variant promotion valid_until"
        )


@dataclass(frozen=True)
class VariantPromotions:
    """Validated signed overlay bound to one catalog and serving overlay."""

    catalog_digest: str
    promotions: Mapping[str, VariantPromotion]

    def get(self, variant_id: str) -> VariantPromotion | None:
        return self.promotions.get(variant_id)

    def routable_variant_ids(self) -> tuple[str, ...]:
        return tuple(self.promotions)


@dataclass(frozen=True)
class VariantGatewayModel:
    """Public-safe route projection; private origins and activation state stay omitted."""

    promotion: VariantPromotion
    display_name: str
    protocols: tuple[str, ...]
    endpoints: Mapping[str, str]
    operations: tuple[str, ...]
    license_id: str
    non_clinical: bool
    commercial_use: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "fs2-serve.nebius.ai/variant-gateway-model/v1",
            "variant_id": self.promotion.variant_id,
            "candidate_id": self.promotion.candidate_id,
            "runtime_profile": self.promotion.runtime_profile,
            "base_model_id": self.promotion.base_model_id,
            "model_id": self.promotion.exposed_model_id,
            "display_name": self.display_name,
            "runtime": {
                "image_reference": self.promotion.runtime_image_reference,
                "image_digest": self.promotion.runtime_image_digest,
                "artifact_manifest_digest": self.promotion.artifact_manifest_digest,
            },
            "interface": {
                "protocols": list(self.protocols),
                "endpoints": dict(self.endpoints),
                "operations": list(self.operations),
            },
            "policy": {
                "license_id": self.license_id,
                "non_clinical": self.non_clinical,
                "commercial_use": self.commercial_use,
            },
            "backend": {
                "class": self.promotion.binding.backend_class,
                "region": self.promotion.binding.backend_region,
                "gpu_class": "NVIDIA B300",
            },
            "valid_until": self.promotion.valid_until,
            "routable": True,
            "mcp": {"enabled": False},
        }


@dataclass(frozen=True)
class VariantGatewayCatalog:
    """Typed variant routing catalog created only after all intersections pass."""

    catalog_digest: str
    models: Mapping[str, VariantGatewayModel]

    def routable_variant_ids(self, when: datetime | None = None) -> tuple[str, ...]:
        return tuple(
            variant_id
            for variant_id, model in self.models.items()
            if model.promotion.valid_at(when)
        )

    def public_models(self, when: datetime | None = None) -> tuple[dict[str, Any], ...]:
        allowed = set(self.routable_variant_ids(when))
        return tuple(
            self.models[variant_id].to_dict()
            for variant_id in sorted(allowed)
        )


def load_model_variant_promotions(
    path: Path | str,
    catalog: Catalog,
    bindings: ServingBindings,
    *,
    evidence_root: Path | str | None = None,
    trusted_attestors: Mapping[str, str] | None = None,
    trusted_attestor_policy: Mapping[str, Any] | None = None,
    validation_time: datetime | None = None,
) -> VariantPromotions:
    """Reopen a signed promotion overlay and its canonical route intersection."""

    value = _exact(
        _load_json(Path(path)),
        {
            "schema",
            "route_authority",
            "catalog_digest",
            "attestor_policy_sha256",
            "promotions",
        },
        "model variant promotions",
    )
    if (
        value["schema"] != MODEL_VARIANT_PROMOTIONS_SCHEMA
        or value["route_authority"] != "signed-live-evidence-only"
        or value["catalog_digest"] != catalog.digest
        or bindings.catalog_digest != catalog.digest
    ):
        raise CatalogError("model variant promotions are detached from the canonical catalog")
    raw_promotions = value["promotions"]
    if not isinstance(raw_promotions, dict) or list(raw_promotions) != sorted(raw_promotions):
        raise CatalogError("model variant promotions must be a canonically sorted object")
    loaded: dict[str, VariantPromotion] = {}
    global_attempt_ids: set[str] = set()
    role_policy: dict[str, tuple[str, ...]] | None = None
    role_policy_digest: str | None = None
    for variant_id, raw in raw_promotions.items():
        item = _exact(
            raw,
            {
                "variant_id",
                "candidate_id",
                "candidate_digest",
                "runtime_profile",
                "variant_digest",
                "base_model_id",
                "exposed_model_id",
                "canonical_model_digest",
                "serving_binding_digest",
                "scale_contract_digest",
                "enabled",
                "valid_until",
                "backend_service_uid",
                "evidence_session_id",
                "artifact_manifest_digest",
                "supply_receipt_digest",
                "runtime_tuple_digest",
                "cold_cohort_digest",
                "warm_cohort_digest",
                "qualification_receipt_digest",
                "backend_readiness_receipt_digest",
                "preemption_receipt_digest",
                "zero_to_ready_receipt_digest",
                "return_to_zero_receipt_digest",
                "independent_review_receipt_digest",
            },
            f"model variant promotion {variant_id}",
        )
        if item["variant_id"] != variant_id:
            raise CatalogError("model variant promotion key and variant_id differ")
        variant = catalog.model_variant(variant_id)
        fallback, runtime_profile = catalog.fallback_for_variant(variant_id)
        if (
            item["candidate_id"] != fallback.candidate_id
            or item["candidate_digest"] != fallback.digest
            or item["runtime_profile"] != runtime_profile
            or item["variant_digest"] != variant.digest
            or item["base_model_id"] != variant.base_model_id
            or item["exposed_model_id"] != variant.exposed_model_id
        ):
            raise CatalogError("model variant promotion names another static candidate/profile")
        if variant.exposed_model_id not in catalog.records:
            raise CatalogError(
                "capability alternative needs its own canonical base record before promotion"
            )
        canonical_model_digest = _model_digest(catalog, variant.exposed_model_id)
        scale_contract_digest = catalog.scale_contract(variant.exposed_model_id).digest
        binding = bindings.get(variant.exposed_model_id)
        if (
            item["canonical_model_digest"] != canonical_model_digest
            or item["scale_contract_digest"] != scale_contract_digest
            or binding is None
            or item["serving_binding_digest"] != binding.binding_digest
        ):
            raise CatalogError("variant promotion bypasses its base record/serving binding")
        enabled = _boolean(item["enabled"], "variant promotion enabled")
        evidence_fields = (
            "evidence_session_id",
            "artifact_manifest_digest",
            "supply_receipt_digest",
            "runtime_tuple_digest",
            "cold_cohort_digest",
            "warm_cohort_digest",
            "qualification_receipt_digest",
            "backend_readiness_receipt_digest",
            "preemption_receipt_digest",
            "zero_to_ready_receipt_digest",
            "return_to_zero_receipt_digest",
            "independent_review_receipt_digest",
        )
        if not enabled:
            if (
                item["valid_until"] is not None
                or item["backend_service_uid"] is not None
                or any(item[field] is not None for field in evidence_fields)
            ):
                raise CatalogError("disabled model variant promotion implies partial evidence")
            continue
        if evidence_root is None or not trusted_attestors or not trusted_attestor_policy:
            raise CatalogError("enabled model variant promotion requires signed evidence trust")
        if role_policy is None:
            role_policy, role_policy_digest = _validate_attestor_policy(
                trusted_attestor_policy, trusted_attestors
            )
        assert role_policy_digest is not None
        if value["attestor_policy_sha256"] != role_policy_digest:
            raise CatalogError("variant promotion attestor policy differs from gateway trust")
        if not binding.enabled or not binding.ready or not binding.valid_at(validation_time):
            raise CatalogError("variant promotion requires an enabled, ready, fresh serving binding")
        if binding.backend_class != "local-kubernetes" or binding.backend_region != "us-north1":
            raise CatalogError("initial model variant promotion is B300 local-Kubernetes only")
        if binding.model_digest != canonical_model_digest:
            raise CatalogError("variant promotion binding belongs to another canonical model")
        if item["backend_service_uid"] is None or K8S_UID.fullmatch(
            item["backend_service_uid"]
        ) is None:
            raise CatalogError("variant promotion lacks the exact backend Service UID")
        for field in evidence_fields:
            _digest(item[field], f"variant promotion {field}")
        if item["cold_cohort_digest"] == item["warm_cohort_digest"]:
            raise CatalogError("cold and warm cohort subjects must be distinct")
        store = EvidenceStore(
            evidence_root,
            session_id=item["evidence_session_id"],
            trusted_attestors=trusted_attestors,
            validation_time=validation_time,
        )
        manifest = _validate_artifact(store, item["artifact_manifest_digest"], variant)
        assert role_policy is not None
        _assert_role(
            store,
            role_policy,
            role="artifact",
            kind="artifacts",
            digest=item["artifact_manifest_digest"],
        )
        supply, supply_subjects = _validate_supply(
            store, item["supply_receipt_digest"], variant, manifest, role_policy
        )
        runtime = _validate_runtime_tuple(
            store,
            item["runtime_tuple_digest"],
            variant,
            manifest,
            supply,
            role_policy,
        )
        if binding.backend_runtime_image_digest != supply["runtime"]["digest"]:
            raise CatalogError("ready serving binding uses another variant runtime image")
        semantic_contract = catalog.semantic_request_contract(variant.exposed_model_id)
        if semantic_contract.state != "qualified":
            raise CatalogError("variant promotion lacks a qualified canonical semantic contract")
        semantic_validator = catalog.model(variant.exposed_model_id).to_dict()[
            "semantic_validator"
        ]
        backend_readiness = _validate_backend_readiness(
            store,
            item["backend_readiness_receipt_digest"],
            variant=variant,
            binding=binding,
            runtime_digest=item["runtime_tuple_digest"],
            runtime_tuple=runtime,
            manifest=manifest,
            backend_service_uid=item["backend_service_uid"],
            policy=role_policy,
        )
        preemption = _validate_preemption(
            store,
            item["preemption_receipt_digest"],
            variant=variant,
            runtime_digest=item["runtime_tuple_digest"],
            runtime_tuple=runtime,
            manifest=manifest,
            readiness_digest=item["backend_readiness_receipt_digest"],
            readiness=backend_readiness,
            policy=role_policy,
        )
        cold, cold_semantics = _validate_cohort(
            store,
            item["cold_cohort_digest"],
            expected_kind="cold",
            variant=variant,
            runtime_digest=item["runtime_tuple_digest"],
            runtime_tuple=runtime,
            manifest=manifest,
            semantic_contract=semantic_contract,
            semantic_validator=semantic_validator,
            binding=binding,
            backend_service_uid=item["backend_service_uid"],
            policy=role_policy,
            preemption=None,
        )
        warm, warm_semantics = _validate_cohort(
            store,
            item["warm_cohort_digest"],
            expected_kind="warm",
            variant=variant,
            runtime_digest=item["runtime_tuple_digest"],
            runtime_tuple=runtime,
            manifest=manifest,
            semantic_contract=semantic_contract,
            semantic_validator=semantic_validator,
            binding=binding,
            backend_service_uid=item["backend_service_uid"],
            policy=role_policy,
            preemption=preemption,
        )
        _validate_cohort_separation(cold, warm)
        attempt_ids = {
            attempt["attempt_id"] for attempt in (*cold["attempts"], *warm["attempts"])
        }
        if global_attempt_ids & attempt_ids:
            raise CatalogError("variant attempt IDs must be globally unique across the overlay")
        global_attempt_ids.update(attempt_ids)
        zero_lifecycle, return_lifecycle = _validate_lifecycle_pair(
            store,
            item["zero_to_ready_receipt_digest"],
            item["return_to_zero_receipt_digest"],
            variant=variant,
            binding=binding,
            scale_contract_digest=scale_contract_digest,
            runtime_digest=item["runtime_tuple_digest"],
            runtime_tuple=runtime,
            manifest=manifest,
            readiness_digest=item["backend_readiness_receipt_digest"],
            backend_service_uid=item["backend_service_uid"],
            policy=role_policy,
        )
        activation_at = _utc(zero_lifecycle["observed_at"], "variant activation")
        readiness_at = _utc(backend_readiness["observed_at"], "variant readiness")
        first_t0 = min(
            _utc(attempt["t0"], "variant first T0")
            for attempt in (*cold["attempts"], *warm["attempts"])
        )
        final_completion = max(
            _utc(attempt["completed_at"], "variant final completion")
            for attempt in (*cold["attempts"], *warm["attempts"])
        )
        deactivation_at = _utc(
            return_lifecycle["observed_at"], "variant deactivation"
        )
        if not activation_at < readiness_at <= first_t0 <= final_completion < deactivation_at:
            raise CatalogError(
                "variant lifecycle/readiness/cohort chronology is not activate-ready-measure-deactivate"
            )
        qualification = _validate_qualification(
            store,
            item["qualification_receipt_digest"],
            variant=variant,
            supply_digest=item["supply_receipt_digest"],
            runtime_digest=item["runtime_tuple_digest"],
            runtime_tuple=runtime,
            manifest=manifest,
            cold=cold,
            warm=warm,
            semantic_contract=semantic_contract,
            binding=binding,
            backend_service_uid=item["backend_service_uid"],
            backend_readiness_digest=item["backend_readiness_receipt_digest"],
            preemption=preemption,
            zero_lifecycle=zero_lifecycle,
            return_lifecycle=return_lifecycle,
            policy=role_policy,
        )
        evidence = {
            "artifact_manifest_digest": item["artifact_manifest_digest"],
            "supply_receipt_digest": item["supply_receipt_digest"],
            "runtime_tuple_digest": item["runtime_tuple_digest"],
            "cold_cohort_digest": item["cold_cohort_digest"],
            "warm_cohort_digest": item["warm_cohort_digest"],
            "qualification_receipt_digest": item["qualification_receipt_digest"],
            "backend_readiness_receipt_digest": item[
                "backend_readiness_receipt_digest"
            ],
            "preemption_receipt_digest": item["preemption_receipt_digest"],
            "zero_to_ready_receipt_digest": item["zero_to_ready_receipt_digest"],
            "return_to_zero_receipt_digest": item["return_to_zero_receipt_digest"],
        }
        review = _validate_review(
            store,
            item["independent_review_receipt_digest"],
            variant=variant,
            candidate_id=fallback.candidate_id,
            candidate_digest=fallback.digest,
            runtime_profile=runtime_profile,
            canonical_model_digest=canonical_model_digest,
            binding_digest=binding.binding_digest,
            scale_contract_digest=scale_contract_digest,
            evidence=evidence,
            supply=supply,
            cold=cold,
            warm=warm,
            policy=role_policy,
            policy_digest=role_policy_digest,
        )
        expected_expiry = store.valid_until()
        for subject in (
            supply,
            *supply_subjects,
            runtime,
            cold,
            *cold_semantics,
            warm,
            *warm_semantics,
            backend_readiness,
            preemption,
            zero_lifecycle,
            return_lifecycle,
            qualification,
            review,
        ):
            if _utc(subject["valid_until"], "variant subject valid_until") < _utc(
                expected_expiry, "variant promotion signed expiry"
            ):
                expected_expiry = subject["valid_until"]
        assert binding.valid_until is not None
        if _utc(binding.valid_until, "variant serving binding valid_until") < _utc(
            expected_expiry, "variant promotion signed expiry"
        ):
            expected_expiry = binding.valid_until
        if item["valid_until"] != expected_expiry:
            raise CatalogError(
                "variant promotion valid_until must equal its earliest signed subject expiry"
            )
        loaded[variant_id] = VariantPromotion(
            variant_id=variant_id,
            candidate_id=fallback.candidate_id,
            candidate_digest=fallback.digest,
            runtime_profile=runtime_profile,
            base_model_id=variant.base_model_id,
            exposed_model_id=variant.exposed_model_id,
            variant_digest=variant.digest,
            canonical_model_digest=canonical_model_digest,
            serving_binding_digest=binding.binding_digest,
            scale_contract_digest=scale_contract_digest,
            runtime_image_reference=supply["runtime"]["reference"],
            runtime_image_digest=supply["runtime"]["digest"],
            artifact_manifest_digest=manifest.digest,
            valid_until=expected_expiry,
            binding=binding,
        )
    return VariantPromotions(
        catalog_digest=catalog.digest,
        promotions=MappingProxyType(dict(sorted(loaded.items()))),
    )


def bind_variant_gateway_catalog(
    catalog: Catalog,
    bindings: ServingBindings,
    promotions: VariantPromotions,
) -> VariantGatewayCatalog:
    """Create the public typed view after the promotion/base/binding intersection."""

    if catalog.digest != bindings.catalog_digest or catalog.digest != promotions.catalog_digest:
        raise CatalogError("variant gateway inputs belong to different canonical catalogs")
    models: dict[str, VariantGatewayModel] = {}
    for variant_id, promotion in promotions.promotions.items():
        record = catalog.model(promotion.exposed_model_id).to_dict()
        binding = bindings.get(promotion.exposed_model_id)
        if (
            binding is None
            or binding.binding_digest != promotion.serving_binding_digest
            or not binding.enabled
            or not binding.ready
            or binding.valid_until is None
        ):
            raise CatalogError("variant gateway route bypassed its exact serving binding")
        source = catalog.model_variant(variant_id).to_dict()["source"]
        models[variant_id] = VariantGatewayModel(
            promotion=promotion,
            display_name=record["model"]["display_name"],
            protocols=binding.protocols,
            endpoints=binding.endpoints,
            operations=binding.operations,
            license_id=source["license"]["id"],
            non_clinical=record["interface"]["policy"]["non_clinical"],
            commercial_use=source["license"]["commercial_use"],
        )
    return VariantGatewayCatalog(
        catalog_digest=catalog.digest,
        models=MappingProxyType(dict(sorted(models.items()))),
    )


def load_variant_gateway_catalog(
    catalog: Catalog,
    bindings: ServingBindings,
    promotions_path: Path | str,
    *,
    evidence_root: Path | str | None = None,
    trusted_attestors: Mapping[str, str] | None = None,
    trusted_attestor_policy: Mapping[str, Any] | None = None,
    validation_time: datetime | None = None,
) -> VariantGatewayCatalog:
    """Stable typed consumer API for a separately promoted runtime variant."""

    return bind_variant_gateway_catalog(
        catalog,
        bindings,
        load_model_variant_promotions(
            promotions_path,
            catalog,
            bindings,
            evidence_root=evidence_root,
            trusted_attestors=trusted_attestors,
            trusted_attestor_policy=trusted_attestor_policy,
            validation_time=validation_time,
        ),
    )


def variant_promotion_contract_fixture() -> dict[str, Any]:
    """Versioned schema/consumer fixture shared with gateway and fallback lanes."""

    return {
        "schema": "fs2-serve.nebius.ai/model-variant-consumer/v4",
        "static_schema": "fs2-serve.nebius.ai/model-variants/v4",
        "promotion_overlay_schema": MODEL_VARIANT_PROMOTIONS_SCHEMA,
        "supply_receipt_schema": MODEL_VARIANT_SUPPLY_SCHEMA,
        "supply_object_schema": MODEL_VARIANT_SUPPLY_OBJECT_SCHEMA,
        "license_artifact_schema": MODEL_VARIANT_LICENSE_ARTIFACT_SCHEMA,
        "attestor_policy_schema": MODEL_VARIANT_ATTESTOR_POLICY_SCHEMA,
        "runtime_tuple_schema": MODEL_VARIANT_RUNTIME_TUPLE_SCHEMA,
        "semantic_receipt_schema": MODEL_VARIANT_SEMANTIC_SCHEMA,
        "cohort_schema": MODEL_VARIANT_COHORT_SCHEMA,
        "qualification_receipt_schema": MODEL_VARIANT_QUALIFICATION_SCHEMA,
        "backend_readiness_receipt_schema": MODEL_VARIANT_BACKEND_READINESS_SCHEMA,
        "kubernetes_observation_schema": MODEL_VARIANT_K8S_OBSERVATION_SCHEMA,
        "cold_boundary_receipt_schema": MODEL_VARIANT_COLD_BOUNDARY_SCHEMA,
        "preemption_receipt_schema": MODEL_VARIANT_PREEMPTION_SCHEMA,
        "lifecycle_receipt_schema": MODEL_VARIANT_LIFECYCLE_SCHEMA,
        "independent_review_schema": MODEL_VARIANT_REVIEW_SCHEMA,
        "loader": "fs2_serve_catalog.variant_promotions.load_variant_gateway_catalog",
        "shape_schemas_authoritative": False,
        "cross_lane_identity": "explicit-candidate-id-plus-runtime-profile-to-full-variant-id",
        "route_intersection": [
            "canonical-base-model-digest",
            "normal-serving-binding-digest",
            "enabled-ready-fresh-serving-binding",
            "api-observed-backend-readiness",
            "raw-key-derived-one-principal-per-role-and-group-policy",
            "raw-cosign-dsse-slsa-spdx-scan-object-verification",
            "single-descriptor-root-dirfd-no-follow-filesystem-custody",
            "service-endpointslice-pod-node-gpu-probe-api-chain",
            "per-attempt-cold-zero-new-process-cache-boundary",
            "api-event-backed-preemption-replacement-fence",
            "immutable-scale-contract-digest",
            "signed-live-variant-promotion",
        ],
        "static_route_authority": False,
        "public_projection_omits": [
            "activation",
            "backend_service_uid",
            "evidence_session_id",
            "service_origin",
        ],
    }

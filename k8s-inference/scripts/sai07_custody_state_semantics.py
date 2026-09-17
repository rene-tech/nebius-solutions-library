"""Canonical, non-secret SAI-07 custody semantics derived from Terraform state.

The raw state remains the authoritative source.  This module projects only the
identity and security-relevant desired fields for the small, closed set of
custody resources; it never returns provider private data or unrelated state
values.  The same projection is used for signed desired manifests and
immediate Kubernetes reads so an address cannot be rebound to another object.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

EXTERNAL_OWNER_LABEL = "security.fs2.nebius.ai/custody-owner"
SECURITY_METADATA_PREFIXES = (
    "security.fs2.nebius.ai/",
    "pod-security.kubernetes.io/",
)

TYPED_IDENTITIES = {
    "kubernetes_service_account_v1": ("v1", "ServiceAccount"),
    "kubernetes_role_v1": ("rbac.authorization.k8s.io/v1", "Role"),
    "kubernetes_role_binding_v1": (
        "rbac.authorization.k8s.io/v1",
        "RoleBinding",
    ),
    "kubernetes_cluster_role_v1": (
        "rbac.authorization.k8s.io/v1",
        "ClusterRole",
    ),
    "kubernetes_cluster_role_binding_v1": (
        "rbac.authorization.k8s.io/v1",
        "ClusterRoleBinding",
    ),
}

BODY_FIELDS = {
    "ConfigMap": ("data", "binaryData", "immutable"),
    "Secret": ("data", "type", "immutable"),
    "ServiceAccount": (
        "automountServiceAccountToken",
        "imagePullSecrets",
        "secrets",
    ),
    "Role": ("rules",),
    "ClusterRole": ("rules",),
    "RoleBinding": ("roleRef", "subjects"),
    "ClusterRoleBinding": ("roleRef", "subjects"),
    "ValidatingAdmissionPolicy": ("spec",),
    "ValidatingAdmissionPolicyBinding": ("spec",),
    "NetworkPolicy": ("spec",),
    "DaemonSet": ("spec",),
}


class StateSemanticsError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StateSemanticsError(f"{label} must be an object")
    return value


def _string(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise StateSemanticsError(f"{label} must be a string")
    return value


def _metadata_block(attributes: dict[str, Any], label: str) -> dict[str, Any]:
    metadata = attributes.get("metadata")
    if not isinstance(metadata, list) or len(metadata) != 1:
        raise StateSemanticsError(f"{label} must contain exactly one metadata block")
    return _mapping(metadata[0], f"{label}[0]")


def _metadata_manifest(metadata: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": _string(metadata.get("name"), "state metadata name"),
    }
    namespace = metadata.get("namespace")
    if namespace not in (None, ""):
        result["namespace"] = _string(namespace, "state metadata namespace")
    for field in ("annotations", "labels"):
        value = metadata.get(field)
        if value not in (None, {}):
            result[field] = _mapping(value, f"state metadata {field}")
    return result


def _rbac_rule(rule: object) -> dict[str, Any]:
    source = _mapping(rule, "Terraform RBAC rule")
    fields = {
        "api_groups": "apiGroups",
        "non_resource_urls": "nonResourceURLs",
        "resource_names": "resourceNames",
        "resources": "resources",
        "verbs": "verbs",
    }
    result: dict[str, Any] = {}
    for state_name, api_name in fields.items():
        value = source.get(state_name)
        if value not in (None, []):
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise StateSemanticsError(
                    f"Terraform RBAC {state_name} must be a string list"
                )
            result[api_name] = value
    for required in ("apiGroups", "verbs"):
        result.setdefault(required, [])
    if "resources" not in result and "nonResourceURLs" not in result:
        raise StateSemanticsError("Terraform RBAC rule has no resource scope")
    return result


def _role_ref(value: object) -> dict[str, Any]:
    if not isinstance(value, list) or len(value) != 1:
        raise StateSemanticsError("Terraform role_ref must contain exactly one block")
    source = _mapping(value[0], "Terraform role_ref")
    return {
        "apiGroup": _string(source.get("api_group"), "role_ref.api_group"),
        "kind": _string(source.get("kind"), "role_ref.kind"),
        "name": _string(source.get("name"), "role_ref.name"),
    }


def _subject(value: object) -> dict[str, Any]:
    source = _mapping(value, "Terraform subject")
    result = {
        "kind": _string(source.get("kind"), "subject.kind"),
        "name": _string(source.get("name"), "subject.name"),
    }
    for state_name, api_name in (
        ("api_group", "apiGroup"),
        ("namespace", "namespace"),
    ):
        item = source.get(state_name)
        if item not in (None, ""):
            result[api_name] = _string(item, f"subject.{state_name}")
    return result


def typed_manifest(resource_type: str, attributes: dict[str, Any]) -> dict[str, Any]:
    try:
        api_version, kind = TYPED_IDENTITIES[resource_type]
    except KeyError as error:
        raise StateSemanticsError(
            f"unsupported typed custody resource {resource_type}"
        ) from error
    metadata = _metadata_block(attributes, f"{resource_type}.metadata")
    manifest: dict[str, Any] = {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": _metadata_manifest(metadata),
    }
    if kind == "ServiceAccount":
        manifest["automountServiceAccountToken"] = attributes.get(
            "automount_service_account_token"
        )
        manifest["imagePullSecrets"] = [
            {
                "name": _string(
                    _mapping(item, "image_pull_secret").get("name"),
                    "image_pull_secret.name",
                )
            }
            for item in attributes.get("image_pull_secret", []) or []
        ]
        manifest["secrets"] = [
            {"name": _string(_mapping(item, "secret").get("name"), "secret.name")}
            for item in attributes.get("secret", []) or []
        ]
    elif kind in {"Role", "ClusterRole"}:
        rules = attributes.get("rule")
        if not isinstance(rules, list):
            raise StateSemanticsError(f"{resource_type}.rule must be a list")
        manifest["rules"] = [_rbac_rule(item) for item in rules]
    elif kind in {"RoleBinding", "ClusterRoleBinding"}:
        manifest["roleRef"] = _role_ref(attributes.get("role_ref"))
        subjects = attributes.get("subject")
        if not isinstance(subjects, list):
            raise StateSemanticsError(f"{resource_type}.subject must be a list")
        manifest["subjects"] = [_subject(item) for item in subjects]
    return manifest


def manifest_identity(manifest: object) -> tuple[str, str, str, str]:
    source = _mapping(manifest, "custody manifest")
    metadata = _mapping(source.get("metadata"), "custody manifest metadata")
    identity = (
        _string(source.get("apiVersion"), "custody apiVersion"),
        _string(source.get("kind"), "custody kind"),
        _string(metadata.get("namespace", ""), "custody namespace", empty=True),
        _string(metadata.get("name"), "custody name"),
    )
    return identity


def security_semantics(
    manifest: object, *, strip_external_owner: bool = False
) -> dict[str, Any]:
    source = _mapping(manifest, "custody semantic source")
    api_version, kind, namespace, name = manifest_identity(source)
    metadata = _mapping(source["metadata"], "custody semantic metadata")
    projected_metadata: dict[str, Any] = {}
    for field in ("annotations", "labels"):
        raw = metadata.get(field, {})
        if raw is None:
            raw = {}
        values = dict(_mapping(raw, f"custody semantic metadata.{field}"))
        if strip_external_owner and field == "labels":
            values.pop(EXTERNAL_OWNER_LABEL, None)
        if values:
            projected_metadata[field] = values
    body = {
        field: source[field]
        for field in BODY_FIELDS.get(kind, ())
        if field in source
    }
    return {
        "api_version": api_version,
        "body": body,
        "kind": kind,
        "metadata": projected_metadata,
        "name": name,
        "namespace": namespace,
    }


def state_instance_projection(
    address: str,
    resource_type: str,
    instance: dict[str, Any],
) -> dict[str, Any]:
    attributes = _mapping(instance.get("attributes"), f"{address} attributes")
    if resource_type == "kubernetes_manifest":
        manifest = _mapping(attributes.get("manifest"), f"{address} manifest")
        observed = _mapping(attributes.get("object"), f"{address} object")
        observed_metadata = _mapping(
            observed.get("metadata"), f"{address} object metadata"
        )
        uid = observed_metadata.get("uid")
        resource_version = observed_metadata.get("resourceVersion")
    elif resource_type == "kubernetes_labels":
        metadata = _metadata_block(attributes, f"{address}.metadata")
        manifest = {
            "apiVersion": _string(
                attributes.get("api_version"), f"{address}.api_version"
            ),
            "kind": _string(attributes.get("kind"), f"{address}.kind"),
            "metadata": {
                **_metadata_manifest(metadata),
                "labels": _mapping(attributes.get("labels"), f"{address}.labels"),
            },
        }
        uid = metadata.get("uid")
        resource_version = metadata.get("resource_version")
    else:
        manifest = typed_manifest(resource_type, attributes)
        metadata = _metadata_block(attributes, f"{address}.metadata")
        uid = metadata.get("uid")
        resource_version = metadata.get("resource_version")
    identity = manifest_identity(manifest)
    if resource_type != "kubernetes_labels":
        if not isinstance(uid, str) or not uid:
            raise StateSemanticsError(f"{address} raw state omits live UID")
        if not isinstance(resource_version, str) or not resource_version:
            raise StateSemanticsError(f"{address} raw state omits live resourceVersion")
    else:
        uid = uid if isinstance(uid, str) and uid else None
        resource_version = (
            resource_version
            if isinstance(resource_version, str) and resource_version
            else None
        )
    desired = security_semantics(manifest)
    return {
        "api_version": identity[0],
        "attributes_sha256": digest(attributes),
        "desired_semantics": desired,
        "desired_semantics_sha256": digest(desired),
        "kind": identity[1],
        "name": identity[3],
        "namespace": identity[2],
        "resource_type": resource_type,
        "resource_version": resource_version,
        "state_address": address,
        "uid": uid,
    }


def assert_manifest_matches_state(
    manifest: object, state_object: dict[str, Any]
) -> None:
    desired = security_semantics(manifest, strip_external_owner=True)
    if digest(desired) != state_object.get("desired_semantics_sha256"):
        raise StateSemanticsError(
            "signed desired manifest differs from raw Terraform desired semantics"
        )
    if desired != state_object.get("desired_semantics"):
        raise StateSemanticsError(
            "signed desired manifest semantic body differs from raw Terraform state"
        )


def assert_live_matches_manifest(manifest: object, live: object) -> None:
    # The v1 compatibility validator requires this documentary label, but the
    # retained-state v3 field manager owns zero fields on platform objects.
    # Therefore the label is stripped from desired semantics and, if it appears
    # live without being present in raw Terraform desired state, the exact
    # security-metadata comparison below rejects it.
    desired = security_semantics(manifest, strip_external_owner=True)
    observed = security_semantics(live)
    if (
        desired["api_version"],
        desired["kind"],
        desired["namespace"],
        desired["name"],
    ) != (
        observed["api_version"],
        observed["kind"],
        observed["namespace"],
        observed["name"],
    ):
        raise StateSemanticsError("live object identity differs from desired manifest")
    # A label-only DaemonSet state address intentionally owns no Pod-template
    # fields.  Its complete live object remains UID/RV/full-hash bound; only its
    # exact desired label patch is compared semantically here.
    if not (desired["kind"] == "DaemonSet" and not desired["body"]):
        if desired["body"] != observed["body"]:
            raise StateSemanticsError(
                "live security-relevant body differs from desired manifest"
            )
    for field in ("annotations", "labels"):
        wanted = desired["metadata"].get(field, {})
        actual = observed["metadata"].get(field, {})
        if any(actual.get(key) != value for key, value in wanted.items()):
            raise StateSemanticsError(
                f"live metadata.{field} omits or changes a desired value"
            )
        wanted_security = {
            key: value
            for key, value in wanted.items()
            if key.startswith(SECURITY_METADATA_PREFIXES)
        }
        actual_security = {
            key: value
            for key, value in actual.items()
            if key.startswith(SECURITY_METADATA_PREFIXES)
        }
        if wanted_security != actual_security:
            raise StateSemanticsError(
                f"live metadata.{field} contains unapproved security metadata"
            )

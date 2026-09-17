#!/usr/bin/env python3
"""Verify the short-lived release-owner projection used by the Loki auth gate.

The Terraform external-data contract passes public keys, signed evidence and
the exact run-owned kubeconfig path/context.  At apply time this process rereads
effective Loki/OTel configuration, the Grafana datasource Secret, workload
objects and the runtime-image permit. Secret/configuration bytes never appear
in its result; only bounded metadata and canonical SHA-256 digests do.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT / "catalog" / "runtime"))

from fs2_serve_catalog.artifacts import canonical_bytes  # noqa: E402
from fs2_serve_catalog.attestations import (  # noqa: E402
    raw_public_key_id,
    verify_signed_attestation,
)
from fs2_serve_catalog.loader import CatalogError  # noqa: E402


PROJECTION_SCHEMA = "fs2-serve.nebius.ai/observability-release-owner-projection/v2"
PROJECTION_KIND = "observability-release-owner-projection"
PROJECTION_MODEL_ID = "observability-access"
MAX_PROJECTION_LIFETIME = timedelta(minutes=5)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
KEY_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_OBJECT = re.compile(r"^[0-9a-f]{40}$")
KUBERNETES_UID = re.compile(r"^[0-9a-fA-F-]{20,}$")
DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
KUBERNETES_NAME = re.compile(
    r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*$"
)
MAX_LIVE_OBJECT_BYTES = 8 * 1024 * 1024


def _fail(message: str) -> None:
    raise CatalogError(message)


def _object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _fail(f"{label} must contain exactly {sorted(keys)}")
    return value


def _string(value: Any, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{label} must be a non-empty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        _fail(f"{label} has an invalid format")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(f"{label} must be an integer >= {minimum}")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{label} must be boolean")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    text = _string(value, label)
    if not text.endswith("Z"):
        _fail(f"{label} must be an RFC3339 UTC whole-second timestamp")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise CatalogError(f"{label} is not a valid timestamp") from exc
    if parsed.tzinfo != timezone.utc or parsed.microsecond:
        _fail(f"{label} must be an RFC3339 UTC whole-second timestamp")
    return parsed


def _json(value: str, label: str, *, maximum_bytes: int) -> Any:
    encoded = value.encode("utf-8")
    if not encoded or len(encoded) > maximum_bytes:
        _fail(f"{label} is empty or exceeds {maximum_bytes} bytes")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                _fail(f"{label} contains duplicate key {key!r}")
            result[key] = item
        return result

    try:
        return json.loads(value, object_pairs_hook=reject_duplicates)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CatalogError(f"{label} is not valid JSON") from exc


def _resource(value: Any, label: str, *, kinds: set[str]) -> dict[str, Any]:
    item = _object(
        value,
        {
            "api_version",
            "kind",
            "namespace",
            "name",
            "uid",
            "resource_version",
            "content_sha256",
        },
        label,
    )
    _string(item["api_version"], f"{label}.api_version")
    if _string(item["kind"], f"{label}.kind") not in kinds:
        _fail(f"{label}.kind is not one of {sorted(kinds)}")
    if item["kind"] in {
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
    }:
        if item["namespace"] is not None:
            _fail(f"{label}.namespace must be null for a cluster-scoped resource")
    else:
        _string(item["namespace"], f"{label}.namespace", DNS_LABEL)
    _string(item["name"], f"{label}.name", KUBERNETES_NAME)
    _string(item["uid"], f"{label}.uid", KUBERNETES_UID)
    _string(item["resource_version"], f"{label}.resource_version")
    _string(item["content_sha256"], f"{label}.content_sha256", SHA256)
    return item


def _content_sha256(data: Any, label: str) -> str:
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in data.items()
    ):
        _fail(f"{label} must be a string-to-string data map")
    try:
        # Match Terraform sha256(jsonencode(map)) exactly: sorted compact JSON
        # without the newline used by repository artifact attestations.
        encoded = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise CatalogError(f"{label} is not canonically encodable") from exc
    if len(encoded) > MAX_LIVE_OBJECT_BYTES:
        _fail(f"{label} exceeds the bounded live-content limit")
    return hashlib.sha256(encoded).hexdigest()


def _kubectl_reader(kubeconfig_path: str, kube_context: str):
    kubeconfig = Path(kubeconfig_path)
    if not kubeconfig.is_absolute() or not kubeconfig.is_file():
        _fail("kubeconfig_path must name the existing run-owned kubeconfig")
    if not kube_context or any(character in kube_context for character in "\r\n\0"):
        _fail("kube_context has an invalid format")

    def read(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        command = [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "--context",
            kube_context,
            "get",
            kind,
            name,
        ]
        if namespace is not None:
            command.extend(["--namespace", namespace])
        command.extend(["--output=json", "--request-timeout=15s"])
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=20,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise CatalogError(
                f"current Kubernetes read failed for {kind}/{name}"
            ) from exc
        if completed.returncode != 0:
            _fail(f"current Kubernetes read failed for {kind}/{name}")
        raw = completed.stdout
        if not raw or len(raw) > MAX_LIVE_OBJECT_BYTES:
            _fail(f"current Kubernetes object {kind}/{name} is empty or oversized")
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CatalogError(
                f"current Kubernetes object {kind}/{name} is not JSON"
            ) from exc
        if not isinstance(value, dict):
            _fail(f"current Kubernetes object {kind}/{name} is not an object")
        return value

    return read


def _current_resource(
    reference: dict[str, Any],
    label: str,
    read_object,
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = read_object(
        reference["kind"], reference["namespace"], reference["name"]
    )
    metadata = current.get("metadata")
    if not isinstance(metadata, dict):
        _fail(f"{label} current metadata is missing")
    actual = {
        "api_version": current.get("apiVersion"),
        "kind": current.get("kind"),
        "namespace": metadata.get("namespace"),
        "name": metadata.get("name"),
        "uid": metadata.get("uid"),
        "resource_version": metadata.get("resourceVersion"),
        "content_sha256": _content_sha256(current.get("data"), f"{label}.data"),
    }
    if current.get("binaryData") not in (None, {}):
        _fail(f"{label} binaryData is not permitted in the authorization digest")
    if actual != reference:
        _fail(f"{label} current identity or content differs from the signed projection")
    return actual, current


def _current_workload(reference: dict[str, Any], label: str, read_object) -> dict[str, Any]:
    current = read_object(
        reference["kind"], reference["namespace"], reference["name"]
    )
    metadata = current.get("metadata")
    if not isinstance(metadata, dict):
        _fail(f"{label} current metadata is missing")
    for field, current_field in (
        ("uid", "uid"),
        ("resource_version", "resourceVersion"),
        ("generation", "generation"),
    ):
        if reference[field] != metadata.get(current_field):
            _fail(f"{label} current identity differs from the signed projection")
    return current


def _assert_mounted_resource(
    workload: dict[str, Any], resource: dict[str, Any], label: str
) -> None:
    try:
        pod_spec = workload["spec"]["template"]["spec"]
        volumes = pod_spec.get("volumes", [])
        containers = pod_spec.get("containers", [])
    except (KeyError, TypeError) as exc:
        raise CatalogError(f"{label} workload Pod template is malformed") from exc
    source_key = "configMap" if resource["kind"] == "ConfigMap" else "secret"
    name_key = "name" if source_key == "configMap" else "secretName"
    matched_volumes = {
        volume.get("name")
        for volume in volumes
        if isinstance(volume, dict)
        and isinstance(volume.get(source_key), dict)
        and volume[source_key].get(name_key) == resource["name"]
    }
    mounted_volumes = {
        mount.get("name")
        for container in containers
        if isinstance(container, dict)
        for mount in container.get("volumeMounts", [])
        if isinstance(mount, dict)
    }
    if not matched_volumes or not matched_volumes.issubset(mounted_volumes):
        _fail(f"{label} signed configuration is not mounted by the current workload")


def _validate_payload_permits(
    inventory: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    data = current.get("data")
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in data.items()
    ):
        _fail("current payload-safety ConfigMap data is malformed")
    if set(data) == {"inventory.json"} or "inventory.json" not in data:
        _fail("current payload-safety ConfigMap has no image permits")
    record = _json(
        data["inventory.json"],
        "current payload-safety inventory.json",
        maximum_bytes=2 * 1024 * 1024,
    )
    if not isinstance(record, dict) or not isinstance(record.get("images"), dict):
        _fail("current payload-safety inventory has no images map")
    permits: dict[str, str] = {}
    for key, evidence in record["images"].items():
        _string(key, "payload inventory image key", SHA256)
        if not isinstance(evidence, dict):
            _fail("payload inventory image evidence is malformed")
        image_reference = _string(
            evidence.get("image_reference"), "payload inventory image reference"
        )
        if hashlib.sha256(image_reference.encode()).hexdigest() != key:
            _fail("payload inventory key does not bind its image reference")
        permits[f"image-{key}"] = image_reference
    expected_data = {"inventory.json": data["inventory.json"], **permits}
    if data != expected_data:
        _fail(
            "current payload-safety data must contain exactly inventory.json "
            "and its derived image-<sha256> permits"
        )
    summary = {
        "inventory_sha256": hashlib.sha256(
            data["inventory.json"].encode()
        ).hexdigest(),
        "permit_sha256": _content_sha256(permits, "payload permits"),
        "data_sha256": _content_sha256(data, "payload-safety ConfigMap data"),
        "image_count": len(permits),
    }
    for field in ("inventory_sha256", "permit_sha256", "data_sha256", "image_count"):
        if summary[field] != inventory[field]:
            _fail(f"current payload-safety {field} differs from the signed projection")
    if summary["data_sha256"] != inventory["resource"]["content_sha256"]:
        _fail("payload-safety resource digest does not bind the complete data map")
    return summary


def _validate_live_state(
    projection: dict[str, Any], validation_time: datetime, read_object
) -> dict[str, Any]:
    kube_system = read_object("Namespace", None, "kube-system")
    if kube_system.get("metadata", {}).get("uid") != projection["target"][
        "kube_system_uid"
    ]:
        _fail("apply-time Kubernetes reader selected the wrong cluster")

    workloads = {
        name: _current_workload(reference, f"workload.{name}", read_object)
        for name, reference in projection["workloads"].items()
    }
    configuration = projection["live_configuration"]
    loki_resource, _ = _current_resource(
        configuration["loki"]["resource"], "live Loki configuration", read_object
    )
    loki_runtime_resource, _ = _current_resource(
        configuration["loki"]["runtime_resource"],
        "live Loki runtime configuration",
        read_object,
    )
    otel_resource, _ = _current_resource(
        configuration["otel_gateway"]["resource"],
        "live OTel gateway configuration",
        read_object,
    )
    grafana_resource, grafana_current = _current_resource(
        configuration["grafana_datasource"]["resource"],
        "live Grafana datasource",
        read_object,
    )
    control_resource, _ = _current_resource(
        configuration["control_plane"]["resource"],
        "live control-plane reader configuration",
        read_object,
    )
    _assert_mounted_resource(workloads["loki"], loki_resource, "Loki")
    _assert_mounted_resource(
        workloads["loki"], loki_runtime_resource, "Loki runtime"
    )
    _assert_mounted_resource(workloads["otel_gateway"], otel_resource, "OTel")
    if grafana_current.get("metadata", {}).get("labels", {}).get(
        "grafana_datasource"
    ) != "1":
        _fail("current Grafana datasource Secret lacks the exact sidecar label")

    inventory_resource, inventory_current = _current_resource(
        projection["payload_safety"]["inventory"]["resource"],
        "live payload-safety parameter",
        read_object,
    )
    payload = _validate_payload_permits(
        projection["payload_safety"]["inventory"], inventory_current
    )
    live_state = {
        "schema": "fs2-serve.nebius.ai/observability-apply-time-live-state/v1",
        "validated_at": validation_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target": projection["target"],
        "configuration": {
            "loki": loki_resource,
            "loki_runtime": loki_runtime_resource,
            "otel_gateway": otel_resource,
            "grafana_datasource": grafana_resource,
            "control_plane": control_resource,
        },
        "payload_safety": {
            "resource": inventory_resource,
            **payload,
        },
    }
    return live_state


def _workload(value: Any, label: str) -> dict[str, Any]:
    item = _object(
        value,
        {
            "api_version",
            "kind",
            "namespace",
            "name",
            "uid",
            "generation",
            "resource_version",
            "pod_template_sha256",
            "container_images_sha256",
        },
        label,
    )
    _string(item["api_version"], f"{label}.api_version")
    if _string(item["kind"], f"{label}.kind") not in {
        "Deployment",
        "StatefulSet",
    }:
        _fail(f"{label}.kind must be Deployment or StatefulSet")
    _string(item["namespace"], f"{label}.namespace", DNS_LABEL)
    _string(item["name"], f"{label}.name", DNS_LABEL)
    _string(item["uid"], f"{label}.uid", KUBERNETES_UID)
    _integer(item["generation"], f"{label}.generation", minimum=1)
    _string(item["resource_version"], f"{label}.resource_version")
    _string(
        item["pod_template_sha256"], f"{label}.pod_template_sha256", SHA256
    )
    _string(
        item["container_images_sha256"],
        f"{label}.container_images_sha256",
        SHA256,
    )
    return item


def _release(value: Any, label: str) -> dict[str, Any]:
    item = _object(
        value,
        {
            "name",
            "namespace",
            "revision",
            "status",
            "chart",
            "app_version",
            "storage_secret",
            "rendered_manifest_sha256",
            "effective_values_sha256",
        },
        label,
    )
    _string(item["name"], f"{label}.name", DNS_LABEL)
    _string(item["namespace"], f"{label}.namespace", DNS_LABEL)
    _integer(item["revision"], f"{label}.revision", minimum=1)
    if _string(item["status"], f"{label}.status") != "deployed":
        _fail(f"{label}.status must be deployed")
    _string(item["chart"], f"{label}.chart")
    _string(item["app_version"], f"{label}.app_version")
    _resource(item["storage_secret"], f"{label}.storage_secret", kinds={"Secret"})
    _string(
        item["rendered_manifest_sha256"],
        f"{label}.rendered_manifest_sha256",
        SHA256,
    )
    _string(
        item["effective_values_sha256"],
        f"{label}.effective_values_sha256",
        SHA256,
    )
    return item


def _coverage(value: Any) -> dict[str, Any]:
    item = _object(
        value,
        {
            "complete",
            "namespaces",
            "namespaces_sha256",
            "live_pods",
            "live_workload_controllers",
            "terraform_runtime_addresses",
            "static_runtime_manifests",
            "catalog_runtime_bindings",
        },
        "projection.payload_safety.coverage",
    )
    if not _boolean(item["complete"], "projection.payload_safety.coverage.complete"):
        _fail("projection.payload_safety.coverage.complete must be true")
    if (
        not isinstance(item["namespaces"], list)
        or not item["namespaces"]
        or item["namespaces"] != sorted(set(item["namespaces"]))
    ):
        _fail(
            "projection.payload_safety.coverage.namespaces must be a non-empty "
            "sorted unique list"
        )
    for namespace in item["namespaces"]:
        _string(
            namespace,
            "projection.payload_safety.coverage.namespaces[]",
            DNS_LABEL,
        )
    expected_namespace_digest = hashlib.sha256(
        json.dumps(item["namespaces"], separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    if item["namespaces_sha256"] != expected_namespace_digest:
        _fail(
            "projection.payload_safety.coverage.namespaces_sha256 does not bind "
            "namespaces"
        )
    for field in (
        "live_pods",
        "live_workload_controllers",
        "terraform_runtime_addresses",
        "static_runtime_manifests",
        "catalog_runtime_bindings",
    ):
        entry = _object(
            item[field],
            {"count", "sha256"},
            f"projection.payload_safety.coverage.{field}",
        )
        _integer(
            entry["count"],
            f"projection.payload_safety.coverage.{field}.count",
            minimum=1,
        )
        _string(
            entry["sha256"],
            f"projection.payload_safety.coverage.{field}.sha256",
            SHA256,
        )
    return item


def _validate_projection(
    value: Any,
    *,
    expected_stage: str,
    expected_target: dict[str, str],
    expected_acknowledgement_sha256: str,
    validation_time: datetime,
) -> dict[str, Any]:
    projection = _object(
        value,
        {
            "schema",
            "stage",
            "target",
            "source",
            "evidence_session_id",
            "observed_at",
            "valid_until",
            "releases",
            "workloads",
            "live_configuration",
            "cached_markers",
            "payload_safety",
            "migration_proof",
            "acknowledgement_sha256",
        },
        "projection",
    )
    if projection["schema"] != PROJECTION_SCHEMA:
        _fail("projection.schema is unsupported")
    if projection["stage"] != expected_stage or expected_stage not in {
        "pretransition",
        "posttransition",
    }:
        _fail("projection.stage does not match the requested migration stage")
    if projection["target"] != expected_target:
        _fail("projection.target does not match the Terraform target")
    if projection["acknowledgement_sha256"] != expected_acknowledgement_sha256:
        _fail("projection does not bind the exact source-accepted acknowledgement")
    source = _object(projection["source"], {"commit", "tree"}, "projection.source")
    _string(source["commit"], "projection.source.commit", GIT_OBJECT)
    _string(source["tree"], "projection.source.tree", GIT_OBJECT)
    _string(
        projection["evidence_session_id"],
        "projection.evidence_session_id",
        SHA256,
    )
    observed_at = _timestamp(projection["observed_at"], "projection.observed_at")
    valid_until = _timestamp(projection["valid_until"], "projection.valid_until")
    if (
        valid_until <= observed_at
        or valid_until - observed_at > MAX_PROJECTION_LIFETIME
    ):
        _fail(
            "projection validity must be greater than zero and no longer than "
            "five minutes"
        )
    if (
        observed_at > validation_time + timedelta(minutes=5)
        or valid_until <= validation_time
    ):
        _fail("projection is not current at Terraform authorization time")

    releases = _object(
        projection["releases"],
        {"loki", "otel_gateway", "grafana", "control_plane"},
        "projection.releases",
    )
    for name, release in releases.items():
        _release(release, f"projection.releases.{name}")

    workloads = _object(
        projection["workloads"],
        {"loki", "otel_gateway", "grafana", "control_plane"},
        "projection.workloads",
    )
    for name, workload in workloads.items():
        _workload(workload, f"projection.workloads.{name}")

    configuration = _object(
        projection["live_configuration"],
        {"loki", "otel_gateway", "grafana_datasource", "control_plane"},
        "projection.live_configuration",
    )
    loki = _object(
        configuration["loki"],
        {
            "resource",
            "runtime_resource",
            "runtime_config_sha256",
            "auth_enabled",
            "multi_tenant_queries_enabled",
            "read_tenants",
        },
        "projection.live_configuration.loki",
    )
    _resource(
        loki["resource"],
        "projection.live_configuration.loki.resource",
        kinds={"ConfigMap", "Secret"},
    )
    _resource(
        loki["runtime_resource"],
        "projection.live_configuration.loki.runtime_resource",
        kinds={"ConfigMap", "Secret"},
    )
    if (
        loki["resource"]["kind"],
        loki["resource"]["namespace"],
        loki["resource"]["name"],
    ) != ("ConfigMap", "fs2-observability", "fs2-loki"):
        _fail("effective Loki config must bind the chart-mounted fs2-loki ConfigMap")
    if (
        loki["runtime_resource"]["kind"],
        loki["runtime_resource"]["namespace"],
        loki["runtime_resource"]["name"],
    ) != ("ConfigMap", "fs2-observability", "fs2-loki-runtime"):
        _fail("Loki runtime config must bind the chart-mounted runtime ConfigMap")
    _string(
        loki["runtime_config_sha256"],
        "projection.live_configuration.loki.runtime_config_sha256",
        SHA256,
    )
    if loki["runtime_config_sha256"] != loki["runtime_resource"]["content_sha256"]:
        _fail("Loki runtime digest must bind the reread runtime resource")
    _boolean(loki["auth_enabled"], "projection.live_configuration.loki.auth_enabled")
    if not _boolean(
        loki["multi_tenant_queries_enabled"],
        "projection.live_configuration.loki.multi_tenant_queries_enabled",
    ):
        _fail("effective Loki config must enable multi-tenant queries")
    if loki["read_tenants"] != ["fake", "fs2-platform"]:
        _fail(
            "effective Loki config must preserve the bounded "
            "fake|fs2-platform dual-read cohort"
        )

    otel = _object(
        configuration["otel_gateway"],
        {"resource", "tenant_header_name", "tenant_header_value_sha256"},
        "projection.live_configuration.otel_gateway",
    )
    _resource(
        otel["resource"],
        "projection.live_configuration.otel_gateway.resource",
        kinds={"ConfigMap", "Secret"},
    )
    if (
        otel["resource"]["kind"],
        otel["resource"]["namespace"],
        otel["resource"]["name"],
    ) != ("ConfigMap", "fs2-observability", "fs2-otel-gateway"):
        _fail("effective OTel config must bind the gateway chart ConfigMap")
    if otel["tenant_header_name"] != "X-Scope-OrgID":
        _fail("the current OTel writer must use X-Scope-OrgID")
    _string(
        otel["tenant_header_value_sha256"],
        "projection.live_configuration.otel_gateway.tenant_header_value_sha256",
        SHA256,
    )
    if otel["tenant_header_value_sha256"] != hashlib.sha256(
        b"fs2-platform"
    ).hexdigest():
        _fail("the current OTel writer tenant digest must bind fs2-platform")

    for reader_name in ("grafana_datasource", "control_plane"):
        reader = _object(
            configuration[reader_name],
            {"resource", "content_sha256", "tenant_header_name", "read_tenants"},
            f"projection.live_configuration.{reader_name}",
        )
        _resource(
            reader["resource"],
            f"projection.live_configuration.{reader_name}.resource",
            kinds={"ConfigMap", "Secret"},
        )
        _string(
            reader["content_sha256"],
            f"projection.live_configuration.{reader_name}.content_sha256",
            SHA256,
        )
        if reader["content_sha256"] != reader["resource"]["content_sha256"]:
            _fail(f"{reader_name} content digest must bind the reread live resource")
        if reader["tenant_header_name"] != "X-Scope-OrgID" or reader[
            "read_tenants"
        ] != ["fake", "fs2-platform"]:
            _fail(f"{reader_name} must prove the exact bounded dual-read header")
    grafana_resource = configuration["grafana_datasource"]["resource"]
    if (
        grafana_resource["kind"],
        grafana_resource["namespace"],
        grafana_resource["name"],
    ) != (
        "Secret",
        "fs2-observability",
        "fs2-serve-postgres-grafana-datasource",
    ):
        _fail("Grafana live content must bind the exact workloads-owned datasource Secret")

    markers = _object(
        projection["cached_markers"],
        {"foundation", "workloads"},
        "projection.cached_markers",
    )
    for name, marker in markers.items():
        _resource(marker, f"projection.cached_markers.{name}", kinds={"ConfigMap"})

    payload_safety = _object(
        projection["payload_safety"],
        {"inventory", "coverage", "admission"},
        "projection.payload_safety",
    )
    inventory = _object(
        payload_safety["inventory"],
        {
            "resource",
            "inventory_sha256",
            "permit_sha256",
            "data_sha256",
            "image_count",
        },
        "projection.payload_safety.inventory",
    )
    _resource(
        inventory["resource"],
        "projection.payload_safety.inventory.resource",
        kinds={"ConfigMap"},
    )
    _string(
        inventory["inventory_sha256"],
        "projection.payload_safety.inventory.inventory_sha256",
        SHA256,
    )
    _string(
        inventory["permit_sha256"],
        "projection.payload_safety.inventory.permit_sha256",
        SHA256,
    )
    _string(
        inventory["data_sha256"],
        "projection.payload_safety.inventory.data_sha256",
        SHA256,
    )
    _integer(
        inventory["image_count"],
        "projection.payload_safety.inventory.image_count",
        minimum=1,
    )
    if inventory["data_sha256"] != inventory["resource"]["content_sha256"]:
        _fail("payload data digest must bind the complete reread ConfigMap data map")
    _coverage(payload_safety["coverage"])
    admission = _object(
        payload_safety["admission"],
        {"policy", "binding", "parameter"},
        "projection.payload_safety.admission",
    )
    _resource(
        admission["policy"],
        "projection.payload_safety.admission.policy",
        kinds={"ValidatingAdmissionPolicy"},
    )
    _resource(
        admission["binding"],
        "projection.payload_safety.admission.binding",
        kinds={"ValidatingAdmissionPolicyBinding"},
    )
    _resource(
        admission["parameter"],
        "projection.payload_safety.admission.parameter",
        kinds={"ConfigMap"},
    )
    if admission["parameter"] != inventory["resource"]:
        _fail("admission parameter must be the exact reread payload inventory ConfigMap")

    proof = _object(
        projection["migration_proof"],
        {
            "sealed_evidence_sha256",
            "marker_sha256",
            "writer_identity",
            "writer_scoped_header_configured",
            "writer_marker_ingested",
            "marker_storage_tenant",
            "grafana_legacy_read",
            "grafana_scoped_read",
            "control_plane_legacy_read",
            "control_plane_scoped_read",
        },
        "projection.migration_proof",
    )
    _string(
        proof["sealed_evidence_sha256"],
        "projection.migration_proof.sealed_evidence_sha256",
        SHA256,
    )
    _string(
        proof["marker_sha256"],
        "projection.migration_proof.marker_sha256",
        SHA256,
    )
    if proof["writer_identity"] != "fs2-otel-gateway":
        _fail("projection.migration_proof.writer_identity is not the intended writer")
    for field in (
        "writer_scoped_header_configured",
        "writer_marker_ingested",
        "grafana_legacy_read",
        "grafana_scoped_read",
        "control_plane_legacy_read",
        "control_plane_scoped_read",
    ):
        _boolean(proof[field], f"projection.migration_proof.{field}")
    expected_auth = expected_stage == "posttransition"
    if loki["auth_enabled"] != expected_auth:
        _fail("effective Loki auth state does not match the migration stage")
    if proof["marker_storage_tenant"] != (
        "fs2-platform" if expected_auth else "fake"
    ):
        _fail("marker storage tenant does not match the migration stage")
    if not proof["writer_scoped_header_configured"] or not proof[
        "writer_marker_ingested"
    ]:
        _fail(
            "the current writer has not proved configured scoped headers and "
            "marker ingestion"
        )
    if not proof["grafana_legacy_read"] or not proof["control_plane_legacy_read"]:
        _fail("both current readers must preserve the legacy tenant")
    if expected_auth:
        if not proof["grafana_scoped_read"] or not proof["control_plane_scoped_read"]:
            _fail("posttransition readers must prove scoped-tenant reads")
    elif proof["grafana_scoped_read"] or proof["control_plane_scoped_read"]:
        _fail("pretransition proof must not claim impossible auth-off scoped reads")
    return projection


def verify(query: dict[str, Any], *, read_object=None) -> dict[str, str]:
    required = {
        "projection_json",
        "attestation_json",
        "trusted_attestors_json",
        "validation_time",
        "kubeconfig_path",
        "kube_context",
        "expected_stage",
        "expected_run_id",
        "expected_cluster_id",
        "expected_kube_system_uid",
        "expected_acknowledgement_sha256",
    }
    if set(query) != required or not all(
        isinstance(query[key], str) for key in required
    ):
        _fail(
            f"external query must contain exactly the string fields "
            f"{sorted(required)}"
        )
    validation_time = _timestamp(query["validation_time"], "validation_time")
    projection = _validate_projection(
        _json(
            query["projection_json"],
            "projection_json",
            maximum_bytes=1024 * 1024,
        ),
        expected_stage=query["expected_stage"],
        expected_target={
            "run_id": query["expected_run_id"],
            "cluster_id": query["expected_cluster_id"],
            "kube_system_uid": query["expected_kube_system_uid"],
        },
        expected_acknowledgement_sha256=_string(
            query["expected_acknowledgement_sha256"],
            "expected_acknowledgement_sha256",
            SHA256,
        ),
        validation_time=validation_time,
    )
    trust = _json(
        query["trusted_attestors_json"],
        "trusted_attestors_json",
        maximum_bytes=64 * 1024,
    )
    if not isinstance(trust, dict) or not 1 <= len(trust) <= 32:
        _fail("trusted_attestors_json must contain between one and 32 public keys")
    for key_id, public_key in trust.items():
        _string(key_id, "trusted attestor key ID", KEY_ID)
        if raw_public_key_id(public_key, "trusted attestor public key") != key_id:
            _fail("trusted attestor key ID does not bind its public key")
    attestation = _json(
        query["attestation_json"],
        "attestation_json",
        maximum_bytes=64 * 1024,
    )
    projection_bytes = canonical_bytes(projection)
    projection_sha256 = hashlib.sha256(projection_bytes).hexdigest()
    target_sha256 = hashlib.sha256(
        canonical_bytes(projection["target"])
    ).hexdigest()
    verified = verify_signed_attestation(
        attestation,
        trusted_attestors=trust,
        expected_session_id=projection["evidence_session_id"],
        expected_kind=PROJECTION_KIND,
        expected_schema=PROJECTION_SCHEMA,
        expected_digest=projection_sha256,
        expected_model_id=PROJECTION_MODEL_ID,
        validation_time=validation_time,
    )
    expected_claims = {
        "authorization": "loki-auth-transition",
        "stage": projection["stage"],
        "target_sha256": target_sha256,
        "acknowledgement_sha256": projection["acknowledgement_sha256"],
        "payload_inventory_sha256": projection["payload_safety"]["inventory"][
            "inventory_sha256"
        ],
    }
    if verified["claims"] != expected_claims:
        _fail(
            "signed attestation claims do not bind the exact target, stage, "
            "acknowledgement and inventory"
        )
    if verified["issued_at"] != projection["observed_at"] or verified[
        "expires_at"
    ] != projection["valid_until"]:
        _fail("signed attestation and owner projection freshness windows differ")
    if read_object is None:
        read_object = _kubectl_reader(
            query["kubeconfig_path"], query["kube_context"]
        )
    live_state = _validate_live_state(projection, validation_time, read_object)
    live_state_bytes = canonical_bytes(live_state)
    return {
        "verified": "true",
        "projection_json": projection_bytes.decode("ascii").rstrip("\n"),
        "projection_sha256": projection_sha256,
        "live_state_json": live_state_bytes.decode("ascii").rstrip("\n"),
        "live_state_sha256": hashlib.sha256(live_state_bytes).hexdigest(),
        "validated_at": live_state["validated_at"],
        "key_id": verified["key_id"],
        "expires_at": verified["expires_at"],
    }


def main() -> int:
    try:
        raw = sys.stdin.read(2 * 1024 * 1024 + 1)
        query = _json(raw, "external query", maximum_bytes=2 * 1024 * 1024)
        if not isinstance(query, dict):
            _fail("external query must be a JSON object")
        result = verify(query)
    except (CatalogError, KeyError, TypeError, ValueError) as exc:
        print(f"observability owner projection rejected: {exc}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

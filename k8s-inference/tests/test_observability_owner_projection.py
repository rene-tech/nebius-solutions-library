from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CATALOG_RUNTIME = REPOSITORY_ROOT / "catalog" / "runtime"
sys.path.insert(0, str(CATALOG_RUNTIME))

from fs2_serve_catalog.artifacts import canonical_bytes  # noqa: E402
from fs2_serve_catalog.attestations import (  # noqa: E402
    create_signed_attestation,
    public_key_id,
    public_key_value,
)
from fs2_serve_catalog.loader import CatalogError  # noqa: E402


VERIFIER_PATH = (
    REPOSITORY_ROOT
    / "stages"
    / "foundation"
    / "scripts"
    / "verify-observability-owner-projection.py"
)
SPEC = importlib.util.spec_from_file_location("observability_owner_projection", VERIFIER_PATH)
assert SPEC is not None and SPEC.loader is not None
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def namespaced_resource(
    kind: str, namespace: str, name: str, seed: str
) -> dict[str, object]:
    return {
        "api_version": "v1",
        "kind": kind,
        "namespace": namespace,
        "name": name,
        "uid": f"00000000-0000-4000-8000-{digest(seed)[:12]}",
        "resource_version": str(int(digest(seed)[:8], 16)),
        "content_sha256": digest(f"content:{seed}"),
    }


def cluster_resource(kind: str, name: str, seed: str) -> dict[str, object]:
    value = namespaced_resource(kind, "unused", name, seed)
    value["namespace"] = None
    return value


def release(name: str, namespace: str, seed: str) -> dict[str, object]:
    return {
        "name": name,
        "namespace": namespace,
        "revision": 7,
        "status": "deployed",
        "chart": f"{name}-1.2.3",
        "app_version": "1.2.3",
        "storage_secret": namespaced_resource(
            "Secret", namespace, f"sh.helm.release.v1.{name}.v7", seed
        ),
        "rendered_manifest_sha256": digest(f"manifest:{seed}"),
        "effective_values_sha256": digest(f"values:{seed}"),
    }


def workload(
    kind: str, namespace: str, name: str, seed: str
) -> dict[str, object]:
    return {
        "api_version": "apps/v1",
        "kind": kind,
        "namespace": namespace,
        "name": name,
        "uid": f"00000000-0000-4000-8000-{digest(seed)[:12]}",
        "generation": 3,
        "resource_version": str(int(digest(seed)[:8], 16)),
        "pod_template_sha256": digest(f"template:{seed}"),
        "container_images_sha256": digest(f"images:{seed}"),
    }


def projection() -> dict[str, object]:
    namespaces = ["fs2-models", "fs2-system"]
    inventory_sha256 = digest("inventory")
    inventory_resource = namespaced_resource(
        "ConfigMap", "fs2-system", "fs2-runtime-log-safety-aaaaaaaaaaaa", "inventory"
    )
    inventory_resource["content_sha256"] = inventory_sha256
    datasource_resource = namespaced_resource(
        "Secret", "fs2-observability", "fs2-serve-postgres-grafana-datasource", "datasource"
    )
    return {
        "schema": (
            "fs2-serve.nebius.ai/observability-release-owner-projection/v1"
        ),
        "stage": "posttransition",
        "target": {
            "run_id": "fs2abc",
            "cluster_id": "mk8scluster-example",
            "kube_system_uid": "00000000-0000-4000-8000-000000000001",
        },
        "source": {"commit": "a" * 40, "tree": "b" * 40},
        "evidence_session_id": "c" * 64,
        "observed_at": "2026-09-17T11:58:00Z",
        "valid_until": "2026-09-17T12:03:00Z",
        "releases": {
            "loki": release("fs2-fs2abc-loki", "fs2-observability", "loki"),
            "otel_gateway": release("fs2-fs2abc-otel-gateway", "fs2-observability", "otel"),
            "grafana": release("fs2-fs2abc-monitoring", "fs2-observability", "grafana"),
            "control_plane": release("fs2-serve-control-plane", "fs2-system", "control"),
        },
        "workloads": {
            "loki": workload("StatefulSet", "fs2-observability", "fs2-loki", "loki"),
            "otel_gateway": workload(
                "Deployment", "fs2-observability", "fs2-otel-gateway", "otel"
            ),
            "grafana": workload(
                "Deployment",
                "fs2-observability",
                "fs2-fs2abc-monitoring-grafana",
                "grafana",
            ),
            "control_plane": workload(
                "Deployment", "fs2-system", "fs2-serve-control-plane", "control"
            ),
        },
        "live_configuration": {
            "loki": {
                "resource": namespaced_resource(
                    "Secret", "fs2-observability", "fs2-loki", "loki-config"
                ),
                "runtime_config_sha256": digest("loki-runtime-config"),
                "auth_enabled": True,
                "multi_tenant_queries_enabled": True,
                "read_tenants": ["fake", "fs2-platform"],
            },
            "otel_gateway": {
                "resource": namespaced_resource(
                    "ConfigMap",
                    "fs2-observability",
                    "fs2-otel-gateway",
                    "otel-config",
                ),
                "tenant_header_name": "X-Scope-OrgID",
                "tenant_header_value_sha256": digest("fs2-platform"),
            },
            "grafana_datasource": {
                "resource": datasource_resource,
                "content_sha256": datasource_resource["content_sha256"],
                "tenant_header_name": "X-Scope-OrgID",
                "read_tenants": ["fake", "fs2-platform"],
            },
            "control_plane": {
                "resource": namespaced_resource(
                    "ConfigMap",
                    "fs2-system",
                    "fs2-serve-admin-configuration",
                    "control-config",
                ),
                "content_sha256": digest("content:control-config"),
                "tenant_header_name": "X-Scope-OrgID",
                "read_tenants": ["fake", "fs2-platform"],
            },
        },
        "cached_markers": {
            "foundation": namespaced_resource(
                "ConfigMap",
                "fs2-observability",
                "fs2-loki-foundation-freshness",
                "foundation-marker",
            ),
            "workloads": namespaced_resource(
                "ConfigMap",
                "fs2-system",
                "fs2-loki-workloads-freshness",
                "workloads-marker",
            ),
        },
        "payload_safety": {
            "inventory": {
                "resource": inventory_resource,
                "inventory_sha256": inventory_sha256,
                "image_count": 2,
            },
            "coverage": {
                "complete": True,
                "namespaces": namespaces,
                "namespaces_sha256": hashlib.sha256(
                    json.dumps(namespaces, separators=(",", ":")).encode()
                ).hexdigest(),
                "live_pods": {"count": 2, "sha256": digest("pods")},
                "live_workload_controllers": {
                    "count": 2,
                    "sha256": digest("controllers"),
                },
                "terraform_runtime_addresses": {
                    "count": 2,
                    "sha256": digest("terraform"),
                },
                "static_runtime_manifests": {
                    "count": 2,
                    "sha256": digest("manifests"),
                },
                "catalog_runtime_bindings": {
                    "count": 2,
                    "sha256": digest("catalog"),
                },
            },
            "admission": {
                "policy": cluster_resource(
                    "ValidatingAdmissionPolicy",
                    "fs2-runtime-log-payload-safety",
                    "policy",
                ),
                "binding": cluster_resource(
                    "ValidatingAdmissionPolicyBinding",
                    "fs2-runtime-log-payload-safety",
                    "binding",
                ),
                "parameter": inventory_resource,
            },
        },
        "migration_proof": {
            "sealed_evidence_sha256": digest("sealed"),
            "marker_sha256": digest("marker"),
            "writer_identity": "fs2-otel-gateway",
            "writer_scoped_header_configured": True,
            "writer_marker_ingested": True,
            "marker_storage_tenant": "fs2-platform",
            "grafana_legacy_read": True,
            "grafana_scoped_read": True,
            "control_plane_legacy_read": True,
            "control_plane_scoped_read": True,
        },
        "acknowledgement_sha256": "d" * 64,
    }


def signed_query(
    value: dict[str, object],
) -> tuple[dict[str, str], Ed25519PrivateKey]:
    private_key = Ed25519PrivateKey.generate()
    projection_sha256 = hashlib.sha256(canonical_bytes(value)).hexdigest()
    target_sha256 = hashlib.sha256(canonical_bytes(value["target"])).hexdigest()
    attestation = create_signed_attestation(
        private_key=private_key,
        session_id=str(value["evidence_session_id"]),
        nonce="e" * 64,
        issued_at=str(value["observed_at"]),
        expires_at=str(value["valid_until"]),
        kind="observability-release-owner-projection",
        subject_schema=(
            "fs2-serve.nebius.ai/observability-release-owner-projection/v1"
        ),
        subject_digest=projection_sha256,
        model_id="observability-access",
        claims={
            "authorization": "loki-auth-transition",
            "stage": value["stage"],
            "target_sha256": target_sha256,
            "acknowledgement_sha256": value["acknowledgement_sha256"],
            "payload_inventory_sha256": value["payload_safety"]["inventory"][  # type: ignore[index]
                "inventory_sha256"
            ],
        },
    )
    public_key = private_key.public_key()
    query = {
        "projection_json": json.dumps(value, sort_keys=True, separators=(",", ":")),
        "attestation_json": json.dumps(attestation, sort_keys=True, separators=(",", ":")),
        "trusted_attestors_json": json.dumps(
            {public_key_id(public_key): public_key_value(public_key)}
        ),
        "validation_time": "2026-09-17T12:00:00Z",
        "expected_stage": "posttransition",
        "expected_run_id": "fs2abc",
        "expected_cluster_id": "mk8scluster-example",
        "expected_kube_system_uid": "00000000-0000-4000-8000-000000000001",
        "expected_acknowledgement_sha256": "d" * 64,
    }
    return query, private_key


def test_valid_owner_projection_binds_current_state() -> None:
    query, _ = signed_query(projection())
    result = VERIFIER.verify(query)
    assert result["verified"] == "true"
    assert result["projection_sha256"] == hashlib.sha256(
        canonical_bytes(json.loads(query["projection_json"]))
    ).hexdigest()


def test_unsigned_datasource_content_change_is_rejected() -> None:
    value = projection()
    query, _ = signed_query(value)
    changed = copy.deepcopy(value)
    changed["live_configuration"]["grafana_datasource"][  # type: ignore[index]
        "content_sha256"
    ] = digest("changed")
    query["projection_json"] = json.dumps(changed, sort_keys=True, separators=(",", ":"))
    with pytest.raises(CatalogError, match="content digest|signature"):
        VERIFIER.verify(query)


def test_caller_selected_key_cannot_replace_owner() -> None:
    query, _ = signed_query(projection())
    attacker = Ed25519PrivateKey.generate().public_key()
    query["trusted_attestors_json"] = json.dumps(
        {public_key_id(attacker): public_key_value(attacker)}
    )
    with pytest.raises(CatalogError, match="untrusted key"):
        VERIFIER.verify(query)


def test_expired_owner_projection_is_rejected() -> None:
    query, _ = signed_query(projection())
    query["validation_time"] = "2026-09-17T12:04:00Z"
    with pytest.raises(CatalogError, match="not current|not fresh"):
        VERIFIER.verify(query)

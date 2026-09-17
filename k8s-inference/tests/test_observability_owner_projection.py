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


def resource_data(seed: str) -> dict[str, str]:
    return {"config.yaml": f"fixture:{seed}"}


def control_plane_config_data() -> dict[str, str]:
    return {
        "config.json": json.dumps(
            {"allowed_hosts": [], "datasource_uids": {}, "installed": {}, "links": {}},
            sort_keys=True,
            separators=(",", ":"),
        )
    }


def data_digest(data: dict[str, str]) -> str:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


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
        "content_sha256": data_digest(resource_data(seed)),
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


def payload_fixture() -> tuple[dict[str, object], dict[str, str]]:
    references = [
        "registry.example/runtime-a@sha256:" + "1" * 64,
        "registry.example/runtime-b@sha256:" + "2" * 64,
    ]
    images = {
        digest(reference): {"image_reference": reference}
        for reference in references
    }
    record = {"images": images}
    inventory_json = json.dumps(record, sort_keys=True, separators=(",", ":"))
    permits = {
        f"image-{key}": evidence["image_reference"]
        for key, evidence in images.items()
    }
    data = {"inventory.json": inventory_json, **permits}
    return (
        {
            "inventory_sha256": digest(inventory_json),
            "permit_sha256": data_digest(permits),
            "data_sha256": data_digest(data),
            "image_count": len(images),
        },
        data,
    )


def projection() -> dict[str, object]:
    namespaces = ["fs2-models", "fs2-system"]
    payload_summary, _payload_data = payload_fixture()
    inventory_resource = namespaced_resource(
        "ConfigMap", "fs2-system", "fs2-runtime-log-safety-aaaaaaaaaaaa", "inventory"
    )
    inventory_resource["content_sha256"] = payload_summary["data_sha256"]
    datasource_resource = namespaced_resource(
        "Secret", "fs2-observability", "fs2-serve-postgres-grafana-datasource", "datasource"
    )
    control_plane_resource = namespaced_resource(
        "ConfigMap",
        "fs2-system",
        "fs2-serve-control-plane-admin-observability",
        "control-config",
    )
    control_plane_resource["content_sha256"] = data_digest(
        control_plane_config_data()
    )
    return {
        "schema": (
            "fs2-serve.nebius.ai/observability-release-owner-projection/v3"
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
                    "ConfigMap", "fs2-observability", "fs2-loki", "loki-config"
                ),
                "runtime_resource": namespaced_resource(
                    "ConfigMap",
                    "fs2-observability",
                    "fs2-loki-runtime",
                    "loki-runtime-config",
                ),
                "runtime_config_sha256": data_digest(
                    resource_data("loki-runtime-config")
                ),
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
                "resource": control_plane_resource,
                "content_sha256": control_plane_resource["content_sha256"],
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
                **payload_summary,
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


def current_resource(
    reference: dict[str, object],
    data: dict[str, str],
    *,
    labels: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "apiVersion": reference["api_version"],
        "kind": reference["kind"],
        "metadata": {
            "namespace": reference["namespace"],
            "name": reference["name"],
            "uid": reference["uid"],
            "resourceVersion": reference["resource_version"],
            "labels": labels or {},
        },
        "data": data,
    }


def current_workload(
    reference: dict[str, object],
    mounted: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    volumes: list[dict[str, object]] = []
    mounts: list[dict[str, str]] = []
    for index, resource in enumerate(mounted or []):
        volume_name = f"config-{index}"
        source = (
            {"configMap": {"name": resource["name"]}}
            if resource["kind"] == "ConfigMap"
            else {"secret": {"secretName": resource["name"]}}
        )
        volumes.append({"name": volume_name, **source})
        mounts.append({"name": volume_name, "mountPath": f"/fixture/{index}"})
    return {
        "apiVersion": reference["api_version"],
        "kind": reference["kind"],
        "metadata": {
            "namespace": reference["namespace"],
            "name": reference["name"],
            "uid": reference["uid"],
            "resourceVersion": reference["resource_version"],
            "generation": reference["generation"],
        },
        "spec": {
            "template": {
                "spec": {
                    "volumes": volumes,
                    "containers": [{"name": "fixture", "volumeMounts": mounts}],
                }
            }
        },
    }


def current_control_plane_workload(
    reference: dict[str, object], resource: dict[str, object]
) -> dict[str, object]:
    value = current_workload(reference)
    value["spec"]["template"]["spec"] = {  # type: ignore[index]
        "volumes": [
            {
                "name": "admin-observability",
                "configMap": {
                    "name": resource["name"],
                    "items": [{"key": "config.json", "path": "config.json"}],
                },
            }
        ],
        "containers": [
            {
                "name": "control-plane",
                "env": [
                    {
                        "name": "FS2_ADMIN_LOKI_URL",
                        "value": "http://fs2-loki.fs2-observability.svc.cluster.local:3100",
                    },
                    {
                        "name": "FS2_ADMIN_LOKI_READ_TENANT_HEADER",
                        "value": "fake|fs2-platform",
                    },
                    {
                        "name": "FS2_ADMIN_OBSERVABILITY_CONFIG_FILE",
                        "value": "/etc/fs2-serve/admin-observability/config.json",
                    },
                ],
                "volumeMounts": [
                    {
                        "name": "admin-observability",
                        "mountPath": "/etc/fs2-serve/admin-observability",
                        "readOnly": True,
                    }
                ],
            }
        ],
    }
    return value


def live_objects(value: dict[str, object]) -> dict[tuple[str, str | None, str], dict[str, object]]:
    configuration = value["live_configuration"]  # type: ignore[index]
    payload = value["payload_safety"]["inventory"]  # type: ignore[index]
    workloads = value["workloads"]  # type: ignore[index]
    _payload_summary, payload_data = payload_fixture()
    resources = {
        "loki": current_resource(
            configuration["loki"]["resource"], resource_data("loki-config")  # type: ignore[index]
        ),
        "loki_runtime": current_resource(
            configuration["loki"]["runtime_resource"],  # type: ignore[index]
            resource_data("loki-runtime-config"),
        ),
        "otel_gateway": current_resource(
            configuration["otel_gateway"]["resource"], resource_data("otel-config")  # type: ignore[index]
        ),
        "grafana_datasource": current_resource(
            configuration["grafana_datasource"]["resource"],  # type: ignore[index]
            resource_data("datasource"),
            labels={"grafana_datasource": "1"},
        ),
        "control_plane": current_resource(
            configuration["control_plane"]["resource"],  # type: ignore[index]
            control_plane_config_data(),
        ),
        "payload": current_resource(payload["resource"], payload_data),  # type: ignore[index]
    }
    objects: dict[tuple[str, str | None, str], dict[str, object]] = {
        ("Namespace", None, "kube-system"): {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": "kube-system", "uid": value["target"]["kube_system_uid"]},  # type: ignore[index]
        }
    }
    for resource in resources.values():
        metadata = resource["metadata"]  # type: ignore[index]
        objects[(resource["kind"], metadata["namespace"], metadata["name"])] = resource  # type: ignore[index]
    workload_objects = {
        "loki": current_workload(
            workloads["loki"],  # type: ignore[index]
            [
                configuration["loki"]["resource"],  # type: ignore[index]
                configuration["loki"]["runtime_resource"],  # type: ignore[index]
            ],
        ),
        "otel_gateway": current_workload(
            workloads["otel_gateway"],  # type: ignore[index]
            [configuration["otel_gateway"]["resource"]],  # type: ignore[index]
        ),
        "grafana": current_workload(workloads["grafana"]),  # type: ignore[index]
        "control_plane": current_control_plane_workload(
            workloads["control_plane"],  # type: ignore[index]
            configuration["control_plane"]["resource"],  # type: ignore[index]
        ),
    }
    for workload_value in workload_objects.values():
        metadata = workload_value["metadata"]  # type: ignore[index]
        objects[
            (
                workload_value["kind"],  # type: ignore[index]
                metadata["namespace"],  # type: ignore[index]
                metadata["name"],  # type: ignore[index]
            )
        ] = workload_value
    return objects


def fixture_reader(objects: dict[tuple[str, str | None, str], dict[str, object]]):
    def read(kind: str, namespace: str | None, name: str) -> dict[str, object]:
        return copy.deepcopy(objects[(kind, namespace, name)])

    return read


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
            "fs2-serve.nebius.ai/observability-release-owner-projection/v3"
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
        "kubeconfig_path": "/run/fs2-test/kubeconfig",
        "kube_context": "fs2-test",
        "expected_stage": "posttransition",
        "expected_run_id": "fs2abc",
        "expected_cluster_id": "mk8scluster-example",
        "expected_kube_system_uid": "00000000-0000-4000-8000-000000000001",
        "expected_acknowledgement_sha256": "d" * 64,
    }
    return query, private_key


def test_valid_owner_projection_binds_current_state() -> None:
    value = projection()
    query, _ = signed_query(value)
    result = VERIFIER.verify(query, read_object=fixture_reader(live_objects(value)))
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
        VERIFIER.verify(query, read_object=fixture_reader(live_objects(value)))


def test_caller_selected_key_cannot_replace_owner() -> None:
    value = projection()
    query, _ = signed_query(value)
    attacker = Ed25519PrivateKey.generate().public_key()
    query["trusted_attestors_json"] = json.dumps(
        {public_key_id(attacker): public_key_value(attacker)}
    )
    with pytest.raises(CatalogError, match="untrusted key"):
        VERIFIER.verify(query, read_object=fixture_reader(live_objects(value)))


def test_expired_owner_projection_is_rejected() -> None:
    value = projection()
    query, _ = signed_query(value)
    query["validation_time"] = "2026-09-17T12:04:00Z"
    with pytest.raises(CatalogError, match="not current|not fresh"):
        VERIFIER.verify(query, read_object=fixture_reader(live_objects(value)))


def test_apply_time_config_only_drift_is_rejected() -> None:
    value = projection()
    query, _ = signed_query(value)
    objects = live_objects(value)
    datasource = value["live_configuration"]["grafana_datasource"]["resource"]  # type: ignore[index]
    current = objects[(datasource["kind"], datasource["namespace"], datasource["name"])]  # type: ignore[index]
    current["data"] = {"datasource.yaml": "changed-after-plan"}
    with pytest.raises(CatalogError, match="current identity or content differs"):
        VERIFIER.verify(query, read_object=fixture_reader(objects))


def test_arbitrary_control_plane_config_reference_is_rejected() -> None:
    value = projection()
    resource = value["live_configuration"]["control_plane"]["resource"]  # type: ignore[index]
    resource["name"] = "fs2-serve-admin-configuration"  # type: ignore[index]
    query, _ = signed_query(value)
    with pytest.raises(CatalogError, match="exact chart-owned"):
        VERIFIER.verify(query, read_object=fixture_reader(live_objects(value)))


def test_control_plane_reader_header_drift_is_rejected() -> None:
    value = projection()
    query, _ = signed_query(value)
    objects = live_objects(value)
    workload = value["workloads"]["control_plane"]  # type: ignore[index]
    current = objects[(workload["kind"], workload["namespace"], workload["name"])]  # type: ignore[index]
    environment = current["spec"]["template"]["spec"]["containers"][0]["env"]  # type: ignore[index]
    next(
        entry
        for entry in environment
        if entry["name"] == "FS2_ADMIN_LOKI_READ_TENANT_HEADER"
    )["value"] = "fs2-platform"
    with pytest.raises(CatalogError, match="exact Loki reader environment"):
        VERIFIER.verify(query, read_object=fixture_reader(objects))


def test_control_plane_reader_mount_drift_is_rejected() -> None:
    value = projection()
    query, _ = signed_query(value)
    objects = live_objects(value)
    workload = value["workloads"]["control_plane"]  # type: ignore[index]
    current = objects[(workload["kind"], workload["namespace"], workload["name"])]  # type: ignore[index]
    current["spec"]["template"]["spec"]["volumes"][0]["configMap"][  # type: ignore[index]
        "name"
    ] = "fs2-serve-admin-configuration"
    with pytest.raises(CatalogError, match="mounted|signed ConfigMap"):
        VERIFIER.verify(query, read_object=fixture_reader(objects))


def test_apply_time_payload_permit_drift_is_rejected() -> None:
    value = projection()
    objects = live_objects(value)
    parameter = value["payload_safety"]["inventory"]["resource"]  # type: ignore[index]
    current = objects[(parameter["kind"], parameter["namespace"], parameter["name"])]  # type: ignore[index]
    current["data"]["image-" + "f" * 64] = (  # type: ignore[index]
        "registry.example/attacker@sha256:" + "f" * 64
    )
    drifted_data_sha256 = data_digest(current["data"])  # type: ignore[arg-type]
    parameter["content_sha256"] = drifted_data_sha256  # type: ignore[index]
    value["payload_safety"]["inventory"]["data_sha256"] = drifted_data_sha256  # type: ignore[index]
    query, _ = signed_query(value)
    with pytest.raises(CatalogError, match="exactly inventory"):
        VERIFIER.verify(query, read_object=fixture_reader(objects))

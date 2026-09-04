from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from types import MappingProxyType

import pytest
import yaml
from conftest import CATALOG_ROOT, CONTROL_ROOT, REPO_ROOT, SOLUTION_ROOT
from fs2_serve_catalog.capabilities import BackendCapability
from fs2_serve_catalog.loader import load_catalog
from fs2_serve_catalog.workloads import _metadata

CHART = SOLUTION_ROOT / "charts/control-plane/fs2-serve-control-plane"
WORKLOAD_VALUES = SOLUTION_ROOT / "charts/control-plane/control-plane.values.yaml"
TEST_DIGEST = "sha256:" + "1" * 64
TEST_REPOSITORY = "registry.nebius.cloud/unit/fs2-serve-control-plane"
TEST_ADMIN_DIGEST = "sha256:" + "2" * 64
TEST_ADMIN_REPOSITORY = "registry.nebius.cloud/unit/fs2-serve-admin-console"
TEST_ADMIN_SOURCE_COMMIT = "a" * 40
TEST_ADMIN_SOURCE_TREE = "b" * 40
TEST_ADMIN_SBOM_SHA256 = "c" * 64
TEST_PUBLIC_IP = "203.0.113.17"
TEST_PUBLIC_URL = f"https://{TEST_PUBLIC_IP}"
TEST_AUTHORIZATION_URL = "https://identity.unit.test"
TEST_TARGET_PROJECT_ID = "project-e00abc123xyz"
TEST_ALLOCATION_ID = "vpcallocation-e00abc123xyz"
TEST_ACME_EMAIL = "edge-owner@unit.test"
TEST_HTTP_NODE_PORT = 31425
TEST_HTTPS_NODE_PORT = 32633
TEST_CATALOG_ROLLOUT_DIGEST = "sha256:" + "3" * 64
HELM = shutil.which("helm")
assert HELM is not None, "helm is required for chart tests"
POSTGRESQL_CONTRACT = json.loads((CONTROL_ROOT / "contracts" / "postgresql-release-contract.json").read_text())
POSTGRESQL_ANNOTATIONS = {
    "fs2.nebius.ai/postgresql-contract-schema": POSTGRESQL_CONTRACT["schema"],
    "fs2.nebius.ai/postgresql-contract-payload-sha256": POSTGRESQL_CONTRACT["contract_payload_sha256"],
    "fs2.nebius.ai/postgresql-migration-set-sha256": POSTGRESQL_CONTRACT["required_release_receipt_inputs"][
        "migration_set_sha256"
    ],
    "fs2.nebius.ai/postgresql-migration-count": str(
        POSTGRESQL_CONTRACT["required_release_receipt_inputs"]["migration_count"]
    ),
    "fs2.nebius.ai/postgresql-first-migration": POSTGRESQL_CONTRACT["required_release_receipt_inputs"][
        "first_migration_version"
    ],
    "fs2.nebius.ai/postgresql-last-migration": POSTGRESQL_CONTRACT["required_release_receipt_inputs"][
        "last_migration_version"
    ],
    "fs2.nebius.ai/postgresql-namespace-role-sha256": POSTGRESQL_CONTRACT["required_release_receipt_inputs"][
        "namespace_role_ownership_sha256"
    ],
}


def helm_values() -> list[str]:
    return [
        "--set",
        f"image.repository={TEST_REPOSITORY}",
        "--set",
        f"image.digest={TEST_DIGEST}",
        "--set",
        f"catalog.rolloutDigest={TEST_CATALOG_ROLLOUT_DIGEST}",
        "--set",
        f"config.publicBaseUrl={TEST_PUBLIC_URL}",
        "--set",
        f"config.authorizationServerUrl={TEST_AUTHORIZATION_URL}",
        "--set",
        "config.publicAuthorityMode=ip",
        "--set",
        "httpRoute.authorityMode=ip",
    ]


def edge_prerequisite_values() -> list[str]:
    return [
        "--set",
        "publicGateway.enabled=true",
        "--set",
        "publicTls.enabled=true",
        "--set",
        f"publicTls.ipAddress={TEST_PUBLIC_IP}",
        "--set",
        "publicTls.issuerRef.name=fs2-serve-ip-acme",
        "--set",
        "publicTls.acmeIssuer.enabled=true",
        "--set",
        f"publicTls.acmeIssuer.email={TEST_ACME_EMAIL}",
    ]


def admin_console_values(*, route: bool = True) -> list[str]:
    values = [
        "--set",
        "adminConsole.enabled=true",
        "--set",
        f"adminConsole.image.repository={TEST_ADMIN_REPOSITORY}",
        "--set",
        f"adminConsole.image.digest={TEST_ADMIN_DIGEST}",
        "--set",
        f"adminConsole.provenance.sourceCommit={TEST_ADMIN_SOURCE_COMMIT}",
        "--set",
        f"adminConsole.provenance.sourceTree={TEST_ADMIN_SOURCE_TREE}",
        "--set",
        f"adminConsole.provenance.sbomSha256={TEST_ADMIN_SBOM_SHA256}",
    ]
    if route:
        values.extend(["--set", "adminConsole.httpRoute.enabled=true"])
    return values


def render_command(*extra: str) -> list[str]:
    return [
        HELM,
        "template",
        "fs2-serve",
        str(CHART),
        "--namespace",
        "fs2-system",
        *helm_values(),
        "--set",
        "serviceMonitor.enabled=true",
        "--set",
        "prometheusRule.enabled=true",
        "--set",
        "publicGateway.enabled=true",
        "--set",
        "httpRoute.enabled=true",
        "--set",
        "publicLoadBalancer.enabled=true",
        "--set",
        f"publicLoadBalancer.targetProjectId={TEST_TARGET_PROJECT_ID}",
        "--set",
        f"publicLoadBalancer.allocationProjectId={TEST_TARGET_PROJECT_ID}",
        "--set",
        f"publicLoadBalancer.allocationId={TEST_ALLOCATION_ID}",
        "--set",
        "publicTls.enabled=true",
        "--set",
        f"publicTls.ipAddress={TEST_PUBLIC_IP}",
        "--set",
        "publicTls.issuerRef.name=fs2-serve-ip-acme",
        "--set",
        "publicTls.acmeIssuer.enabled=true",
        "--set",
        f"publicTls.acmeIssuer.email={TEST_ACME_EMAIL}",
        *extra,
    ]


def render(*extra: str) -> list[dict]:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        render_command(*extra),
        check=True,
        capture_output=True,
        text=True,
    )
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def gateway_deployment(documents: list[dict]) -> dict:
    return next(
        document
        for document in documents
        if document["kind"] == "Deployment" and document["metadata"]["name"] == "fs2-serve-control-plane"
    )


def gateway_network_policy(documents: list[dict]) -> dict:
    return next(
        document
        for document in documents
        if document["kind"] == "NetworkPolicy" and document["metadata"]["name"] == "fs2-serve-control-plane"
    )


def application_route(documents: list[dict]) -> dict:
    return next(
        document
        for document in documents
        if document["kind"] == "HTTPRoute" and document["metadata"]["name"] == "fs2-serve-control-plane"
    )


def redirect_route(documents: list[dict]) -> dict:
    return next(
        document
        for document in documents
        if document["kind"] == "HTTPRoute" and document["metadata"]["name"] == "fs2-serve-control-plane-http-redirect"
    )


def expected_envoy_service(
    *, http_node_port: int = TEST_HTTP_NODE_PORT, https_node_port: int = TEST_HTTPS_NODE_PORT
) -> dict:
    return {
        "type": "LoadBalancer",
        "externalTrafficPolicy": "Cluster",
        "annotations": {"nebius.com/load-balancer-allocation-id": TEST_ALLOCATION_ID},
        "patch": {
            "type": "StrategicMerge",
            "value": {
                "spec": {
                    "ports": [
                        {
                            "name": "http-80",
                            "protocol": "TCP",
                            "port": 80,
                            "targetPort": 10080,
                            "nodePort": http_node_port,
                        },
                        {
                            "name": "https-443",
                            "protocol": "TCP",
                            "port": 443,
                            "targetPort": 10443,
                            "nodePort": https_node_port,
                        },
                    ]
                }
            },
        },
    }


def test_chart_lints_and_renders_all_hardened_components() -> None:
    subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [HELM, "lint", str(CHART), "--namespace", "fs2-system", *helm_values()],
        check=True,
        capture_output=True,
        text=True,
    )
    documents = render()
    kinds = {document["kind"] for document in documents}
    assert {
        "CronJob",
        "BackendTrafficPolicy",
        "ClientTrafficPolicy",
        "Deployment",
        "Job",
        "HorizontalPodAutoscaler",
        "GatewayClass",
        "Gateway",
        "HTTPRoute",
        "NetworkPolicy",
        "EnvoyProxy",
        "Certificate",
        "Issuer",
        "PodDisruptionBudget",
        "PrometheusRule",
        "Service",
        "ServiceAccount",
        "ServiceMonitor",
    } <= kinds
    assert "Secret" not in kinds


def test_admin_console_is_disabled_by_default() -> None:
    documents = render()
    assert not any(document["metadata"]["name"] == "fs2-serve-control-plane-admin-console" for document in documents)


def test_admin_console_renders_digest_bound_workload_route_and_network_boundary() -> None:
    documents = render(*admin_console_values())
    named = [
        document for document in documents if document["metadata"]["name"] == "fs2-serve-control-plane-admin-console"
    ]
    assert {document["kind"] for document in named} == {
        "Deployment",
        "HTTPRoute",
        "NetworkPolicy",
        "PodDisruptionBudget",
        "Service",
    }

    deployment = next(document for document in named if document["kind"] == "Deployment")
    pod = deployment["spec"]["template"]
    labels = pod["metadata"]["labels"]
    assert labels["app.kubernetes.io/component"] == "admin-console"
    assert deployment["spec"]["selector"]["matchLabels"] == labels
    assert pod["metadata"]["annotations"] == {
        "fs2.nebius.ai/admin-image-digest": TEST_ADMIN_DIGEST,
        "fs2.nebius.ai/admin-source-commit": TEST_ADMIN_SOURCE_COMMIT,
        "fs2.nebius.ai/admin-source-tree": TEST_ADMIN_SOURCE_TREE,
        "fs2.nebius.ai/admin-sbom-sha256": TEST_ADMIN_SBOM_SHA256,
        "fs2.nebius.ai/admin-sbom-format": "cyclonedx-json",
    }
    pod_spec = pod["spec"]
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["enableServiceLinks"] is False
    assert pod_spec["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 101,
        "runAsGroup": 101,
        "fsGroup": 101,
        "fsGroupChangePolicy": "OnRootMismatch",
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert "serviceAccountName" not in pod_spec
    container = pod_spec["containers"][0]
    assert container["image"] == f"{TEST_ADMIN_REPOSITORY}@{TEST_ADMIN_DIGEST}"
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert {name: container[name]["httpGet"] for name in ("startupProbe", "livenessProbe", "readinessProbe")} == {
        "startupProbe": {"path": "/healthz", "port": "http"},
        "livenessProbe": {"path": "/healthz", "port": "http"},
        "readinessProbe": {"path": "/healthz", "port": "http"},
    }
    assert container["volumeMounts"] == [
        {"name": "runtime-tmp", "mountPath": "/tmp"}  # noqa: S108 - exact container mount contract.
    ]
    assert pod_spec["volumes"] == [{"name": "runtime-tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"}}]

    service = next(document for document in named if document["kind"] == "Service")
    assert service["spec"]["selector"] == labels
    assert service["spec"]["ports"] == [{"name": "http", "port": 8080, "targetPort": "http", "protocol": "TCP"}]
    policy = next(document for document in named if document["kind"] == "NetworkPolicy")
    assert policy["spec"]["podSelector"]["matchLabels"] == labels
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert policy["spec"]["egress"] == []
    assert policy["spec"]["ingress"][0]["ports"] == [{"port": 8080, "protocol": "TCP"}]

    route = next(document for document in named if document["kind"] == "HTTPRoute")
    assert [rule["matches"][0]["path"] for rule in route["spec"]["rules"]] == [
        {"type": "PathPrefix", "value": "/admin/api"},
        {"type": "PathPrefix", "value": "/admin"},
    ]
    assert [rule["backendRefs"][0] for rule in route["spec"]["rules"]] == [
        {"name": "fs2-serve-control-plane", "port": 8080},
        {"name": "fs2-serve-control-plane-admin-console", "port": 8080},
    ]
    for rule in route["spec"]["rules"]:
        assert rule["timeouts"] == {"request": "30s", "backendRequest": "30s"}
        assert set(rule["filters"][0]["requestHeaderModifier"]["remove"]) == {
            "x-fs2-tenant",
            "x-fs2-principal",
            "x-fs2-token-id",
            "x-fs2-model-scope",
            "x-fs2-accounting-id",
        }

    edge_policy = next(document for document in documents if document["kind"] == "BackendTrafficPolicy")
    assert [target["name"] for target in edge_policy["spec"]["targetRefs"]] == [
        "fs2-serve-control-plane",
        "fs2-serve-control-plane-admin-console",
    ]
    public_edge = next(
        document
        for document in documents
        if document["kind"] == "NetworkPolicy"
        and document["metadata"]["name"] == "fs2-serve-control-plane-public-envoy"
    )
    egress = public_edge["spec"]["egress"]
    admin_egress = next(
        rule
        for rule in egress
        if rule["to"][0].get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/component")
        == "admin-console"
    )
    assert admin_egress["ports"] == [{"port": 8080, "protocol": "TCP"}]
    assert all(document["kind"] != "Secret" for document in documents)


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (["--set", "adminConsole.enabled=true"], "adminConsole.image.repository"),
        (
            [
                *admin_console_values(route=False),
                "--set",
                "adminConsole.image.repository=registry.unit.test/fs2-admin:latest",
            ],
            "must not contain a tag or digest",
        ),
        (
            [*admin_console_values(route=False), "--set", "adminConsole.provenance.sourceCommit=short"],
            "sourceCommit",
        ),
        (
            [
                *admin_console_values(route=False),
                "--set",
                "adminConsole.replicaCount=1",
                "--set",
                "adminConsole.podDisruptionBudget.minAvailable=2",
            ],
            "minAvailable cannot exceed replicaCount",
        ),
        (
            ["--set", "adminConsole.httpRoute.enabled=true"],
            "adminConsole.enabled is required",
        ),
    ],
)
def test_admin_console_rejects_incomplete_or_mutable_release_values(extra: list[str], expected: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and bounded adversarial values.
        [HELM, "template", "fs2-serve", str(CHART), "--namespace", "fs2-system", *helm_values(), *extra],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert expected in result.stderr


def test_admin_configuration_optionally_mounts_a_reviewed_apply_receipt() -> None:
    digest = "d" * 64
    documents = render(
        "--set",
        "adminConfiguration.enabled=true",
        "--set",
        f"adminConfiguration.configMapName=fs2-admin-configuration-{digest[:16]}",
        "--set",
        f"adminConfiguration.sha256={digest}",
        "--set",
        "adminConfiguration.receiptKey=terraform-apply-receipt.json",
    )
    pod = gateway_deployment(documents)["spec"]["template"]["spec"]
    container = pod["containers"][0]
    environment = {item["name"]: item["value"] for item in container["env"] if "value" in item}
    assert environment["FS2_ADMIN_CONFIGURATION_FILE"] == "/etc/fs2-serve/admin/admin-configuration.json"
    assert environment["FS2_ADMIN_CONFIGURATION_RECEIPT_FILE"] == ("/etc/fs2-serve/admin/terraform-apply-receipt.json")
    assert [item for item in container["volumeMounts"] if item["name"] == "admin-configuration"] == [
        {
            "name": "admin-configuration",
            "mountPath": "/etc/fs2-serve/admin/admin-configuration.json",
            "subPath": "admin-configuration.json",
            "readOnly": True,
        },
        {
            "name": "admin-configuration",
            "mountPath": "/etc/fs2-serve/admin/terraform-apply-receipt.json",
            "subPath": "terraform-apply-receipt.json",
            "readOnly": True,
        },
    ]
    assert next(item for item in pod["volumes"] if item["name"] == "admin-configuration") == {
        "name": "admin-configuration",
        "configMap": {
            "name": f"fs2-admin-configuration-{digest[:16]}",
            "defaultMode": 292,
            "items": [
                {"key": "admin-configuration.json", "path": "admin-configuration.json"},
                {"key": "terraform-apply-receipt.json", "path": "terraform-apply-receipt.json"},
            ],
        },
    }


def test_admin_configuration_baseline_mount_does_not_require_a_receipt() -> None:
    digest = "d" * 64
    documents = render(
        "--set",
        "adminConfiguration.enabled=true",
        "--set",
        f"adminConfiguration.configMapName=fs2-admin-configuration-{digest[:16]}-baseline",
        "--set",
        f"adminConfiguration.sha256={digest}",
    )
    pod = gateway_deployment(documents)["spec"]["template"]["spec"]
    container = pod["containers"][0]
    environment = {item["name"]: item["value"] for item in container["env"] if "value" in item}

    assert environment["FS2_ADMIN_CONFIGURATION_FILE"] == "/etc/fs2-serve/admin/admin-configuration.json"
    assert "FS2_ADMIN_CONFIGURATION_RECEIPT_FILE" not in environment
    assert [item for item in container["volumeMounts"] if item["name"] == "admin-configuration"] == [
        {
            "name": "admin-configuration",
            "mountPath": "/etc/fs2-serve/admin/admin-configuration.json",
            "subPath": "admin-configuration.json",
            "readOnly": True,
        }
    ]
    assert next(item for item in pod["volumes"] if item["name"] == "admin-configuration") == {
        "name": "admin-configuration",
        "configMap": {
            "name": f"fs2-admin-configuration-{digest[:16]}-baseline",
            "defaultMode": 292,
            "items": [{"key": "admin-configuration.json", "path": "admin-configuration.json"}],
        },
    }


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (["--set", "adminConfiguration.enabled=true"], "configMapName is required"),
        (
            [
                "--set",
                "adminConfiguration.enabled=true",
                "--set",
                "adminConfiguration.configMapName=fs2-admin-configuration-deadbeef",
                "--set",
                "adminConfiguration.sha256=short",
            ],
            "does not match pattern",
        ),
        (
            [
                "--set",
                "adminConfiguration.enabled=true",
                "--set",
                "adminConfiguration.configMapName=fs2-admin-configuration-deadbeef",
                "--set",
                f"adminConfiguration.sha256={'d' * 64}",
                "--set",
                "adminConfiguration.receiptKey=admin-configuration.json",
            ],
            "receiptKey must differ from key",
        ),
    ],
)
def test_admin_configuration_rejects_unbound_release_values(extra: list[str], expected: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and bounded adversarial values.
        [HELM, "template", "fs2-serve", str(CHART), "--namespace", "fs2-system", *helm_values(), *extra],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert expected in result.stderr


def test_lean_routes_mount_is_explicit_and_read_only() -> None:
    documents = render(
        "--set",
        "catalog.leanRoutes.enabled=true",
        "--set",
        "catalog.delivery=image",
    )
    pod = gateway_deployment(documents)["spec"]["template"]["spec"]
    container = pod["containers"][0]
    catalog_env = next(item for item in container["env"] if item["name"] == "FS2_CATALOG_DIR")
    assert catalog_env["value"] == "/opt/fs2/catalog"
    repo_env = next(item for item in container["env"] if item["name"] == "FS2_REPO_ROOT")
    assert repo_env["value"] == "/opt/fs2/catalog/repository"
    assert "catalog" not in {item["name"] for item in pod["volumes"]}
    assert "catalog" not in {item["name"] for item in container["volumeMounts"]}
    env = next(item for item in container["env"] if item["name"] == "FS2_LEAN_ROUTES_FILE")
    assert env["value"] == "/etc/fs2-serve/lean-routes/lean-routes.json"
    volume = next(item for item in pod["volumes"] if item["name"] == "lean-routes")
    assert volume["configMap"] == {
        "name": "fs2-serve-lean-routes",
        "items": [{"key": "lean-routes.json", "path": "lean-routes.json"}],
    }
    mount = next(item for item in container["volumeMounts"] if item["name"] == "lean-routes")
    assert mount == {"name": "lean-routes", "mountPath": "/etc/fs2-serve/lean-routes", "readOnly": True}
    evidence = next(item for item in pod["volumes"] if item["name"] == "evidence")
    assert evidence == {"name": "evidence", "emptyDir": {}}


def test_offline_entrypoint_supplies_every_required_nonplaceholder_chart_value() -> None:
    source = (CONTROL_ROOT / "scripts" / "test.sh").read_text()
    for value in (
        "image.repository=${test_repository}",
        "image.digest=${test_digest}",
        "catalog.rolloutDigest=${test_catalog_rollout_digest}",
        "config.publicBaseUrl=${test_public_url}",
        "config.authorizationServerUrl=${test_authorization_url}",
        "config.publicAuthorityMode=ip",
        "httpRoute.authorityMode=ip",
    ):
        assert value in source
    assert source.count('"${helm_test_values[@]}"') == 3


def test_release_defaults_to_ip_authority_without_a_hostnames_value() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert values["config"]["publicAuthorityMode"] == "ip"
    assert values["httpRoute"]["authorityMode"] == "ip"
    assert "hostnames" not in values["httpRoute"]


def test_public_direct_ip_route_binds_reserved_public_allocation_without_internal_type() -> None:
    documents = render()
    proxy = next(document for document in documents if document["kind"] == "EnvoyProxy")
    service = proxy["spec"]["provider"]["kubernetes"]["envoyService"]
    assert service == expected_envoy_service()
    assert "nebius.com/load-balancer-type" not in service["annotations"]


def test_public_edge_node_ports_are_configurable_inside_the_validated_range() -> None:
    documents = render(
        "--set",
        "publicLoadBalancer.servicePorts.http.nodePort=32080",
        "--set",
        "publicLoadBalancer.servicePorts.https.nodePort=32443",
    )
    proxy = next(document for document in documents if document["kind"] == "EnvoyProxy")
    service = proxy["spec"]["provider"]["kubernetes"]["envoyService"]
    assert service == expected_envoy_service(http_node_port=32080, https_node_port=32443)


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ("publicLoadBalancer.externalTrafficPolicy=Local", "externalTrafficPolicy"),
        ("publicLoadBalancer.servicePorts.http.nodePort=29999", "nodePort"),
    ],
)
def test_public_edge_rejects_unsupported_traffic_policy_or_node_port(override: str, expected: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and bounded adversarial values.
        render_command("--set", override),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert expected in result.stderr


def test_public_edge_rejects_duplicate_node_ports() -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and bounded adversarial values.
        render_command(
            "--set",
            f"publicLoadBalancer.servicePorts.http.nodePort={TEST_HTTPS_NODE_PORT}",
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "NodePorts must be distinct" in result.stderr


def test_public_route_without_reserved_allocation_binding_fails_closed() -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [
            HELM,
            "template",
            "fs2-serve",
            str(CHART),
            *helm_values(),
            *edge_prerequisite_values(),
            "--set",
            "httpRoute.enabled=true",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "publicLoadBalancer" in result.stderr and "enabled" in result.stderr


def test_workloads_are_nonroot_bounded_and_use_digest_pins_and_secret_references() -> None:
    documents = render()
    deployment = gateway_deployment(documents)
    pod = deployment["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert pod["terminationGracePeriodSeconds"] > 30
    for container in [*pod["initContainers"], *pod["containers"]]:
        assert container["image"].endswith(f"@{TEST_DIGEST}")
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
        assert container["resources"]["requests"] and container["resources"]["limits"]
    database = next(item for item in pod["containers"][0]["env"] if item["name"] == "FS2_DATABASE_URL")
    assert "secretKeyRef" in database["valueFrom"]
    evidence_env = next(item for item in pod["containers"][0]["env"] if item["name"] == "FS2_EVIDENCE_ROOT")
    assert evidence_env["value"] == "/etc/fs2-serve/evidence"
    assert "FS2_MIGRATIONS_DIR" not in {item["name"] for item in pod["containers"][0]["env"]}
    evidence_volume = next(item for item in pod["volumes"] if item["name"] == "evidence")
    assert evidence_volume["persistentVolumeClaim"]["readOnly"] is True
    catalog_volume = next(item for item in pod["volumes"] if item["name"] == "catalog")
    assert catalog_volume["persistentVolumeClaim"] == {
        "claimName": "fs2-serve-catalog",
        "readOnly": True,
    }
    env_names = {item["name"] for item in pod["containers"][0]["env"]}
    assert "FS2_PAT_RETENTION_SECONDS" not in env_names
    assert "FS2_TOKEN_RETENTION_SECONDS" not in env_names
    assert "FS2_ROUTE_ATTESTORS_FILE" in env_names
    route_interval = next(
        item for item in pod["containers"][0]["env"] if item["name"] == "FS2_ROUTE_REVALIDATION_INTERVAL_SECONDS"
    )
    assert route_interval["value"] == "15"
    keyring = next(item for item in pod["volumes"] if item["name"] == "crypto-keyrings")
    projected_paths = {item["path"] for source in keyring["projected"]["sources"] for item in source["secret"]["items"]}
    assert projected_paths == {"payload-keyring.json", "ledger-hmac-keyring.json"}
    assert {source["secret"]["name"] for source in keyring["projected"]["sources"]} == {
        "fs2-serve-payload-keyring",
        "fs2-serve-ledger-hmac-keyring",
    }
    binding_mounts = [item for item in pod["containers"][0]["volumeMounts"] if item["name"] == "bindings"]
    assert binding_mounts == [
        {
            "name": "bindings",
            "mountPath": "/etc/fs2-serve/bindings/serving-bindings.json",
            "subPath": "serving-bindings.json",
            "readOnly": True,
        },
        {
            "name": "bindings",
            "mountPath": "/etc/fs2-serve/bindings/model-variant-promotions.json",
            "subPath": "model-variant-promotions.json",
            "readOnly": True,
        },
    ]
    init = pod["initContainers"][0]
    assert init["args"] == ["wait-schema"]
    assert {item["name"] for item in init["env"]} == {"FS2_DATABASE_URL", "FS2_SCHEMA_WAIT_SECONDS"}
    assert init["volumeMounts"] == [{"name": "database-ca", "mountPath": "/tls", "readOnly": True}]
    init_database = next(item for item in init["env"] if item["name"] == "FS2_DATABASE_URL")
    assert init_database["valueFrom"]["secretKeyRef"] == {"name": "fs2-serve-database", "key": "url"}
    assert database["valueFrom"]["secretKeyRef"] == {"name": "fs2-serve-database", "key": "url"}
    database_ca = next(item for item in pod["volumes"] if item["name"] == "database-ca")
    assert database_ca["secret"] == {
        "secretName": "fs2-serve-database",
        "defaultMode": 256,
        "items": [{"key": "ca.crt", "path": "ca.crt"}],
    }
    attestors = next(item for item in pod["volumes"] if item["name"] == "route-attestors")
    assert attestors["secret"] == {
        "secretName": "fs2-serve-route-attestors",
        "defaultMode": 256,
        "items": [{"key": "attestors.json", "path": "route-attestors.json"}],
    }
    attestor_mount = next(item for item in pod["containers"][0]["volumeMounts"] if item["name"] == "route-attestors")
    assert attestor_mount == {
        "name": "route-attestors",
        "mountPath": "/var/run/secrets/fs2-serve/attestors",
        "readOnly": True,
    }
    rendered = json.dumps(documents)
    assert all(document["kind"] != "Secret" for document in documents)
    assert "private-key" not in rendered.lower() and "private_key" not in rendered.lower()
    assert "Kueue" not in rendered and "kueue" not in rendered


def test_activation_controller_is_owned_by_the_separate_child_and_absent_from_the_gateway_chart() -> None:
    documents = render()
    assert {document["metadata"]["name"] for document in documents if document["kind"] == "Service"} == {
        "fs2-serve-control-plane"
    }
    deployments = {document["metadata"]["name"]: document for document in documents if document["kind"] == "Deployment"}
    assert set(deployments) == {"fs2-serve-control-plane"}
    assert not any(
        document["kind"] in {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding", "PodDisruptionBudget"}
        and "activation" in document["metadata"]["name"]
        for document in documents
    )
    gateway = deployments["fs2-serve-control-plane"]["spec"]["template"]["spec"]
    assert gateway["automountServiceAccountToken"] is False
    assert not any(volume["name"].startswith("kubernetes") for volume in gateway["volumes"])
    rendered = json.dumps(documents)
    for forbidden in (
        "activation-controller",
        "fs2-model-activation-controller",
        "fs2-serve-database-activation",
        "FS2_KUBERNETES_",
        "FS2_ACTIVATION_POD_",
        "FS2_ACTIVATION_SERVICE_ACCOUNT_NAME",
        "serviceAccountToken",
        "/internal/activate",
        "activation-token",
    ):
        assert forbidden not in rendered


def test_dynamic_model_controller_is_explicitly_gated_and_least_privilege() -> None:
    documents = render(
        "--set",
        "modelController.enabled=true",
        "--set",
        "modelController.writesEnabled=true",
        "--set",
        "modelController.admission.enabled=true",
        "--set",
        "modelController.infrastructureEnvelopeConfigMapName=fs2-model-envelope",
        "--set",
        "modelController.rendererBundlesConfigMapName=fs2-model-bundles",
        "--set",
        "adminReadAdapters.capacity.enabled=true",
        "--set",
        "networkPolicy.kubernetesApiCidrs[0]=10.0.0.1/32",
    )
    named = {(document["kind"], document["metadata"]["name"]): document for document in documents}
    deployment = named[("Deployment", "fs2-serve-control-plane-model-controller")]
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "fs2-serve-control-plane-controller"
    assert pod["automountServiceAccountToken"] is False
    container = pod["containers"][0]
    assert container["args"] == ["model-controller"]
    environment = {item["name"]: item for item in container["env"]}
    assert environment["FS2_MODEL_CONTROLLER_ENABLED"]["value"] == "true"
    assert environment["FS2_MODEL_CONTROLLER_WRITES_ENABLED"]["value"] == "true"
    assert environment["FS2_MODEL_CONTROLLER_HOLDER_IDENTITY"]["value"] == "$(POD_NAMESPACE)/$(POD_NAME):$(POD_UID)"
    assert environment["FS2_ADMIN_CAPACITY_ENABLED"]["value"] == "true"
    assert environment["FS2_ADMIN_KUBERNETES_API_URL"]["value"] == "https://kubernetes.default.svc"
    assert environment["FS2_ADMIN_KUBERNETES_TOKEN_FILE"]["value"] == "/var/run/secrets/fs2-model-controller/token"
    assert environment["FS2_ADMIN_KUBERNETES_CA_FILE"]["value"] == "/var/run/secrets/fs2-model-controller/ca.crt"
    assert environment["FS2_ADMIN_KUBERNETES_MODEL_NAMESPACE"]["value"] == "fs2-models"
    assert environment["FS2_ADMIN_KUBERNETES_SYSTEM_NAMESPACE"]["value"] == "fs2-system"
    assert environment["FS2_DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "fs2-serve-database",
        "key": "url",
    }
    assert {item["name"] for item in pod["volumes"]} == {
        "database-ca",
        "kubernetes-api",
        "infrastructure-envelope",
        "renderer-bundles",
    }
    token = next(item for item in pod["volumes"] if item["name"] == "kubernetes-api")
    assert token["projected"]["sources"][0]["serviceAccountToken"] == {
        "expirationSeconds": 600,
        "path": "token",
    }

    model_role = named[("Role", "fs2-serve-control-plane-model-controller-models")]
    assert model_role["metadata"]["namespace"] == "fs2-models"
    assert all("secrets" not in rule["resources"] for rule in model_role["rules"])
    assert {
        "apiGroups": ["autoscaling"],
        "resources": ["horizontalpodautoscalers"],
        "verbs": ["get", "list", "watch"],
    } in model_role["rules"]
    assert {
        "apiGroups": ["apps"],
        "resources": ["daemonsets", "deployments"],
        "verbs": ["get", "list", "watch", "create", "patch", "delete"],
    } in model_role["rules"]
    assert not any(
        document["kind"] in {"ClusterRole", "ClusterRoleBinding"} and "model-controller" in document["metadata"]["name"]
        for document in documents
    )
    leader_role = named[("Role", "fs2-serve-control-plane-model-controller-leader")]
    assert leader_role["rules"][0]["resourceNames"] == ["fs2-model-controller"]

    policy = named[("ValidatingAdmissionPolicy", "fs2-serve-control-plane-model-controller-delete")]
    assert policy["spec"]["failurePolicy"] == "Fail"
    assert policy["spec"]["matchConstraints"]["resourceRules"][0]["operations"] == ["DELETE"]
    assert "observedGeneration == oldObject.metadata.generation" in policy["spec"]["validations"][0]["expression"]
    network = named[("NetworkPolicy", "fs2-serve-control-plane-model-controller")]
    egress = network["spec"]["egress"]
    assert next(rule for rule in egress if rule["ports"] == [{"port": 443, "protocol": "TCP"}])["to"] == [
        {"ipBlock": {"cidr": "10.0.0.1/32"}}
    ]
    assert any(rule["ports"] == [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}] for rule in egress)
    assert any(rule["ports"] == [{"port": 9090, "protocol": "TCP"}] for rule in egress)


def test_scientific_batch_consumer_is_explicitly_gated_and_namespace_scoped() -> None:
    academic_model = {
        "model_id": "alphafold3",
        "workload_namespace": "fs2-academic-poc",
        "access_profile": "academic",
        "stages": [
            {
                "service_account_name": "fs2-academic-runner",
                "mounts": [
                    {
                        "kind": "private",
                        "claim_name": "academic-assets-runtime-rwx",
                        "mount_path": "/models/af3.bin.zst",
                        "sub_path": "alphafold3/af3.bin.zst",
                    }
                ],
            }
        ],
    }
    academic_binding = {
        "alphafold3": {
            "modelId": "alphafold3",
            "artifactId": "alphafold3-parameters",
            "sourceSubPath": "alphafold3/af3.bin.zst",
            "consumerPath": "/models/af3.bin.zst",
            "mechanism": "subpath-file-mount",
            "contentIdentityKind": "file-digest",
            "contentManifestAlgorithm": None,
            "contentDigestSha256": "a" * 64,
            "sizeBytes": 1020545840,
            "sourceArtifact": {"filename": "af3.bin.zst", "sha256": "a" * 64, "size_bytes": 1020545840},
            "readOnly": True,
        }
    }
    documents = render(
        "--set",
        "scientificBatch.enabled=true",
        "--set",
        "scientificBatch.writesEnabled=true",
        "--set",
        "scientificBatch.schedulingContractConfigMapName=scientific-scheduling-a1",
        "--set",
        "scientificBatch.schedulingContractNamespace=fs2-system",
        "--set",
        "scientificBatch.schedulingContractSha256=" + "c" * 64,
        "--set",
        "scientificBatch.executionMapConfigMapName=scientific-execution-b2",
        "--set-json",
        "scientificBatch.executionMap.models="
        + json.dumps(
            [
                {"model_id": "protein-design", "workload_namespace": "fs2-models", "access_profile": "public"},
                academic_model,
            ]
        ),
        "--set",
        "academicAssets.enabled=true",
        "--set",
        "academicAssets.execution.enabled=true",
        "--set",
        "academicAssets.readinessManifestSha256=" + "b" * 64,
        "--set",
        "academicAssets.execution.referenceDataLocalQueue=academic-scientific-cpu",
        "--set",
        "academicAssets.execution.referenceDataClusterQueue=reference-data-cpu",
        "--set-json",
        "academicAssets.runtimeBindings=" + json.dumps(academic_binding),
        "--set",
        "scientificArtifacts.enabled=true",
        "--set-string",
        "networkPolicy.kubernetesApiCidrs[0]=192.0.2.10/32",
        "--set-string",
        "scientificArtifacts.egressCidrs[0]=192.0.2.20/32",
    )
    named = {(document["kind"], document["metadata"]["name"]): document for document in documents}
    namespaced = {
        (document["kind"], document["metadata"]["name"], document["metadata"].get("namespace")): document
        for document in documents
    }
    pod = gateway_deployment(documents)["spec"]["template"]["spec"]
    container = pod["containers"][0]
    environment = {item["name"]: item for item in container["env"]}
    assert pod["automountServiceAccountToken"] is False
    assert environment["FS2_SCIENTIFIC_BATCH_ENABLED"]["value"] == "true"
    assert environment["FS2_SCIENTIFIC_BATCH_WRITES_ENABLED"]["value"] == "true"
    assert environment["FS2_SCIENTIFIC_BATCH_CONTROLLER_ID"]["valueFrom"]["fieldRef"] == {"fieldPath": "metadata.uid"}
    assert environment["FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_FILE"]["value"].endswith("/kueue-scheduling.json")
    assert environment["FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_SCHEMA"]["value"] == (
        "fs2-serve.nebius.ai/kueue-scheduling/v1"
    )
    assert environment["FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_SHA256"]["value"] == "c" * 64
    assert environment["FS2_SCIENTIFIC_BATCH_EXECUTION_MAP_FILE"]["value"].endswith("/execution-map.json")
    assert environment["FS2_SCIENTIFIC_ARTIFACTS_ENABLED"]["value"] == "true"
    assert environment["FS2_ARTIFACT_STORE_CREDENTIALS_FILE"]["value"] == (
        "/var/run/secrets/fs2-serve/artifact-store/credentials.json"
    )
    assert "FS2_ARTIFACT_STORE_ACCESS_KEY" not in environment
    assert "FS2_ARTIFACT_STORE_SECRET_KEY" not in environment
    volumes = {item["name"]: item for item in pod["volumes"]}
    token = volumes["scientific-batch-kubernetes"]["projected"]["sources"]
    assert token[0]["serviceAccountToken"] == {
        "audience": "kubernetes.default.svc",
        "expirationSeconds": 600,
        "path": "token",
    }
    assert volumes["scientific-batch-scheduling"]["configMap"]["name"] == "scientific-scheduling-a1"

    role = namespaced[("Role", "fs2-serve-control-plane-scientific-batch", "fs2-models")]
    binding = namespaced[("RoleBinding", "fs2-serve-control-plane-scientific-batch", "fs2-models")]
    assert role["metadata"]["namespace"] == "fs2-models"
    assert role["rules"] == [
        {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["get", "create", "delete"]},
        {
            "apiGroups": ["jobset.x-k8s.io"],
            "resources": ["jobsets"],
            "verbs": ["get", "create", "delete"],
        },
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
        {"apiGroups": ["kueue.x-k8s.io"], "resources": ["workloads"], "verbs": ["get", "list"]},
    ]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "fs2-serve-control-plane-runtime", "namespace": "fs2-system"}
    ]
    academic_role = namespaced[("Role", "fs2-serve-control-plane-scientific-batch", "fs2-academic-poc")]
    academic_binding = namespaced[("RoleBinding", "fs2-serve-control-plane-scientific-batch", "fs2-academic-poc")]
    assert academic_role["rules"] == role["rules"]
    assert academic_binding["subjects"] == binding["subjects"]
    execution_map = next(
        document
        for document in documents
        if document["kind"] == "ConfigMap"
        and document.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "scientific-execution-map"
    )
    assert execution_map["immutable"] is True
    rendered_map_json = execution_map["data"]["execution-map.json"]
    rendered_map = json.loads(rendered_map_json)
    assert rendered_map["schema"] == "fs2-serve.nebius.ai/scientific-execution-map/v3"
    execution_map_sha256 = hashlib.sha256(rendered_map_json.encode()).hexdigest()
    execution_map_name = f"scientific-execution-b2-{execution_map_sha256[:12]}"
    assert execution_map["metadata"]["name"] == execution_map_name
    assert volumes["scientific-batch-execution"]["configMap"]["name"] == execution_map_name
    assert execution_map["metadata"]["annotations"] == {
        "fs2-serve.nebius.ai/resource-owner": "helm/fs2-serve-control-plane",
        "fs2-serve.nebius.ai/execution-map-schema": "fs2-serve.nebius.ai/scientific-execution-map/v3",
        "fs2-serve.nebius.ai/execution-map-sha256": execution_map_sha256,
    }
    assert {model["workload_namespace"] for model in rendered_map["models"]} == {
        "fs2-models",
        "fs2-academic-poc",
    }
    dependency_map = next(
        document
        for document in documents
        if document["kind"] == "ConfigMap" and document["metadata"]["name"].endswith("-dependency-contract")
    )
    dependency_contract = json.loads(dependency_map["data"]["dependency-contract.json"])
    assert dependency_contract["scientific_batch"]["execution_map"] == {
        "name": execution_map_name,
        "schema": "fs2-serve.nebius.ai/scientific-execution-map/v3",
        "sha256": execution_map_sha256,
    }
    assert dependency_contract["scientific_batch"]["scheduling_contract"] == {
        "config_map_name": "scientific-scheduling-a1",
        "namespace": "fs2-system",
        "key": "kueue-scheduling.json",
        "schema": "fs2-serve.nebius.ai/kueue-scheduling/v1",
        "sha256": "c" * 64,
    }
    for workload_namespace in ("fs2-models", "fs2-academic-poc"):
        workload_network = namespaced[
            ("NetworkPolicy", "fs2-serve-control-plane-scientific-workloads", workload_namespace)
        ]
        assert workload_network["spec"]["podSelector"]["matchExpressions"] == [
            {"key": "fs2.nebius.ai/workload-id", "operator": "Exists"}
        ]
    flavor_role = named[("ClusterRole", "fs2-serve-control-plane-scientific-batch-flavors")]
    assert flavor_role["rules"] == [
        {"apiGroups": ["kueue.x-k8s.io"], "resources": ["resourceflavors"], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["nodes"], "verbs": ["get"]},
    ]
    assert named[("ClusterRoleBinding", "fs2-serve-control-plane-scientific-batch-flavors")]["subjects"] == [
        {"kind": "ServiceAccount", "name": "fs2-serve-control-plane-runtime", "namespace": "fs2-system"}
    ]
    tls_egress = [
        rule
        for rule in named[("NetworkPolicy", "fs2-serve-control-plane-runtime")]["spec"]["egress"]
        if rule["ports"] == [{"port": 443, "protocol": "TCP"}]
    ]
    assert {rule["to"][0]["ipBlock"]["cidr"] for rule in tls_egress} >= {
        "192.0.2.10/32",
        "192.0.2.20/32",
    }


def test_committed_scientific_profile_binds_exact_helm_execution_map_bytes() -> None:
    execution_map = json.loads((CATALOG_ROOT / "contracts/scientific-execution-map.json").read_text())
    profiles = json.loads((CATALOG_ROOT / "contracts/scientific-workload-profiles.json").read_text())["profiles"]

    documents = render(
        "--set",
        "scientificBatch.enabled=true",
        "--set",
        "scientificBatch.writesEnabled=true",
        "--set",
        "scientificBatch.schedulingContractConfigMapName=scientific-scheduling-committed",
        "--set",
        "scientificBatch.schedulingContractNamespace=fs2-system",
        "--set",
        "scientificBatch.schedulingContractSha256=" + "c" * 64,
        "--set",
        "scientificBatch.executionMapConfigMapName=scientific-execution-committed",
        "--set-json",
        "scientificBatch.executionMap=" + json.dumps(execution_map, separators=(",", ":"), sort_keys=True),
        "--set",
        "scientificArtifacts.enabled=true",
        "--set-string",
        "networkPolicy.kubernetesApiCidrs[0]=192.0.2.10/32",
        "--set-string",
        "scientificArtifacts.egressCidrs[0]=192.0.2.20/32",
    )
    rendered = next(
        document
        for document in documents
        if document["kind"] == "ConfigMap"
        and document.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "scientific-execution-map"
    )
    rendered_bytes = rendered["data"]["execution-map.json"].encode()
    rendered_sha256 = hashlib.sha256(rendered_bytes).hexdigest()
    assert rendered["metadata"]["annotations"]["fs2-serve.nebius.ai/execution-map-sha256"] == rendered_sha256
    profiles_by_id = {item["model_id"]: item for item in profiles}
    for map_model in execution_map["models"]:
        profile = profiles_by_id[map_model["model_id"]]
        assert profile["qualification"]["execution_map_sha256"] == rendered_sha256

        identity = profile["execution_identity"]
        identity_payload = {key: value for key, value in identity.items() if key != "execution_identity_sha256"}
        expected_identity = hashlib.sha256(
            json.dumps(identity_payload, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        assert identity["execution_identity_sha256"] == expected_identity
        assert map_model["execution_identity_sha256"] == expected_identity


def test_scientific_batch_can_start_fail_closed_with_only_the_internal_cpu_canary() -> None:
    documents = render(
        "--set",
        "scientificBatch.enabled=true",
        "--set",
        "scientificBatch.writesEnabled=true",
        "--set",
        "scientificBatch.schedulingContractConfigMapName=scientific-scheduling-a1",
        "--set",
        "scientificBatch.schedulingContractNamespace=fs2-system",
        "--set",
        "scientificBatch.schedulingContractSha256=" + "c" * 64,
        "--set",
        "scientificBatch.executionMapConfigMapName=scientific-execution-empty",
        "--set",
        "scientificArtifacts.enabled=true",
        "--set-string",
        "networkPolicy.kubernetesApiCidrs[0]=192.0.2.10/32",
        "--set-string",
        "scientificArtifacts.egressCidrs[0]=192.0.2.20/32",
    )

    deployment = gateway_deployment(documents)
    environment = {item["name"]: item for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert environment["FS2_SCIENTIFIC_BATCH_ENABLED"]["value"] == "true"
    execution_map = next(
        document
        for document in documents
        if document["kind"] == "ConfigMap"
        and document.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "scientific-execution-map"
    )
    assert json.loads(execution_map["data"]["execution-map.json"]) == {
        "models": [],
        "schema": "fs2-serve.nebius.ai/scientific-execution-map/v3",
    }


def test_scientific_execution_map_has_one_helm_owner_and_no_terraform_writer() -> None:
    templates = CHART / "templates"
    helm_owners = [
        path.relative_to(SOLUTION_ROOT).as_posix()
        for path in templates.glob("*.yaml")
        if "kind: ConfigMap" in path.read_text()
        and 'include "fs2-serve.scientificExecutionMapName" .' in path.read_text()
        and "fs2-serve.nebius.ai/resource-owner: helm/fs2-serve-control-plane" in path.read_text()
    ]
    assert helm_owners == ["charts/control-plane/fs2-serve-control-plane/templates/scientific-execution-map.yaml"]

    resource_pattern = re.compile(
        r'^resource\s+"(?:kubernetes_config_map(?:_v1)?|kubernetes_manifest)"\s+"[^"]+"\s*\{.*?^\}',
        re.MULTILINE | re.DOTALL,
    )
    terraform_writers: list[str] = []
    legacy_contracts: list[str] = []
    for path in SOLUTION_ROOT.rglob("*.tf"):
        if ".terraform" in path.parts:
            continue
        source = path.read_text()
        relative = path.relative_to(SOLUTION_ROOT).as_posix()
        if "fs2-serve.nebius.ai/scientific-execution-map/v1" in source:
            legacy_contracts.append(relative)
        for resource in resource_pattern.findall(source):
            if any(
                marker in resource
                for marker in ("scientific_execution_map", "scientific-execution-map", "execution-map.json")
            ):
                terraform_writers.append(relative)
    assert legacy_contracts == []
    assert terraform_writers == []


@pytest.mark.parametrize(
    "extra",
    [
        (),
        ("--set", "academicAssets.enabled=true"),
        (
            "--set",
            "academicAssets.enabled=true",
            "--set",
            "academicAssets.execution.enabled=true",
            "--set",
            "academicAssets.execution.namespace=fs2-models",
        ),
    ],
)
def test_academic_scientific_execution_requires_the_exact_namespace_local_contract(
    extra: tuple[str, ...],
) -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, test-only Helm validation
        [
            *render_command(),
            "--set",
            "scientificBatch.enabled=true",
            "--set",
            "scientificBatch.writesEnabled=true",
            "--set",
            "scientificBatch.schedulingContractConfigMapName=scientific-scheduling-a1",
            "--set",
            "scientificBatch.schedulingContractNamespace=fs2-system",
            "--set",
            "scientificBatch.schedulingContractSha256=" + "c" * 64,
            "--set",
            "scientificBatch.executionMapConfigMapName=scientific-execution-b2",
            "--set-json",
            'scientificBatch.executionMap.models=[{"model_id":"alphafold3","workload_namespace":"fs2-academic-poc","access_profile":"academic"}]',
            "--set",
            "scientificArtifacts.enabled=true",
            "--set-string",
            "networkPolicy.kubernetesApiCidrs[0]=192.0.2.10/32",
            "--set-string",
            "scientificArtifacts.egressCidrs[0]=192.0.2.20/32",
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "academic scientific" in result.stderr


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (
            ("--set", "scientificBatch.schedulingContractNamespace=another-namespace"),
            "scheduling-contract ConfigMap must be in the control-plane release namespace",
        ),
        (
            ("--set", "scientificBatch.schedulingContractSchema=fs2-serve.nebius.ai/kueue-scheduling/v2"),
            "value must be 'fs2-serve.nebius.ai/kueue-scheduling/v1'",
        ),
    ],
)
def test_scientific_batch_rejects_scheduling_ref_identity_drift(extra: tuple[str, ...], expected: str) -> None:
    arguments = [
        "--set",
        "scientificBatch.enabled=true",
        "--set",
        "scientificBatch.writesEnabled=true",
        "--set",
        "scientificBatch.schedulingContractConfigMapName=scientific-scheduling-a1",
        "--set",
        "scientificBatch.schedulingContractNamespace=fs2-system",
        "--set",
        "scientificBatch.schedulingContractSha256=" + "c" * 64,
        "--set",
        "scientificBatch.executionMapConfigMapName=scientific-execution-b2",
        "--set-json",
        'scientificBatch.executionMap.models=[{"model_id":"protein-design","workload_namespace":"fs2-models","access_profile":"public"}]',
        "--set",
        "scientificArtifacts.enabled=true",
        "--set-string",
        "networkPolicy.kubernetesApiCidrs[0]=192.0.2.10/32",
        "--set-string",
        "scientificArtifacts.egressCidrs[0]=192.0.2.20/32",
    ]
    arguments.extend(extra)
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and bounded adversarial values
        render_command(*arguments),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert expected in result.stderr


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (("--set", "scientificBatch.enabled=true"), "independent writesEnabled gate"),
        (("--set", "scientificBatch.writesEnabled=true"), "requires scientificBatch.enabled"),
        (
            (
                "--set",
                "scientificBatch.enabled=true",
                "--set",
                "scientificBatch.writesEnabled=true",
                "--set",
                "scientificArtifacts.enabled=true",
                "--set-string",
                "scientificArtifacts.egressCidrs[0]=192.0.2.20/32",
            ),
            "immutable scheduling-contract and execution-map ConfigMaps",
        ),
        (
            (
                "--set",
                "scientificBatch.enabled=true",
                "--set",
                "scientificBatch.writesEnabled=true",
                "--set",
                "scientificBatch.schedulingContractConfigMapName=scientific-scheduling-a1",
                "--set",
                "scientificBatch.executionMapConfigMapName=scientific-execution-b2",
                "--set-string",
                "networkPolicy.kubernetesApiCidrs[0]=192.0.2.10/32",
            ),
            "requires scientificArtifacts.enabled",
        ),
    ],
)
def test_scientific_batch_consumer_rejects_partial_enablement(extra: tuple[str, ...], expected: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and bounded adversarial values
        render_command(*extra),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert expected in result.stderr


def test_dynamic_model_writer_requires_delete_admission_gate() -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [
            *render_command(
                "--set",
                "modelController.enabled=true",
                "--set",
                "modelController.writesEnabled=true",
                "--set",
                "modelController.infrastructureEnvelopeConfigMapName=fs2-model-envelope",
                "--set",
                "modelController.rendererBundlesConfigMapName=fs2-model-bundles",
                "--set",
                "adminReadAdapters.capacity.enabled=true",
                "--set",
                "networkPolicy.kubernetesApiCidrs[0]=10.0.0.1/32",
            )
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "writesEnabled requires deletion admission safeguards" in result.stderr


def test_gateway_chart_rejects_controller_owned_values_instead_of_silently_rendering_them() -> None:
    for override, expected in (
        ("activationController.replicas=2", "owned by the separate activation child"),
        (
            "secrets.activationDatabase.name=fs2-serve-database-activation",
            "additional properties 'activationDatabase' not allowed",
        ),
    ):
        result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
            [
                HELM,
                "template",
                "fs2-serve",
                str(CHART),
                "--namespace",
                "fs2-system",
                *helm_values(),
                "--set",
                override,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert expected in result.stderr


@pytest.mark.parametrize("interval", ["0", "301", "-1"])
def test_chart_rejects_out_of_bounds_route_revalidation_interval(interval: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [
            HELM,
            "template",
            "fs2-serve",
            str(CHART),
            "--namespace",
            "fs2-system",
            *helm_values(),
            "--set-string",
            f"config.routeRevalidationIntervalSeconds={interval}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "routeRevalidationIntervalSeconds" in result.stderr


def test_service_selects_only_gateway_pods_and_workload_identities_are_split() -> None:
    documents = render()
    deployment = gateway_deployment(documents)
    cron = next(document for document in documents if document["kind"] == "CronJob")
    migration = next(document for document in documents if document["kind"] == "Job")
    service = next(document for document in documents if document["kind"] == "Service")
    runtime_labels = deployment["spec"]["template"]["metadata"]["labels"]
    maintenance_labels = cron["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    migration_labels = migration["spec"]["template"]["metadata"]["labels"]
    assert service["spec"]["selector"] == runtime_labels
    assert service["spec"]["selector"]["app.kubernetes.io/component"] == "gateway"
    assert not all(maintenance_labels.get(key) == value for key, value in service["spec"]["selector"].items())
    assert not all(migration_labels.get(key) == value for key, value in service["spec"]["selector"].items())

    accounts = {document["metadata"]["name"] for document in documents if document["kind"] == "ServiceAccount"}
    assert accounts == {
        "fs2-serve-control-plane-runtime",
        "fs2-serve-control-plane-maintenance",
        "fs2-serve-control-plane-migration",
    }
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"].endswith("-runtime")
    assert cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["serviceAccountName"].endswith("-maintenance")
    assert migration["spec"]["template"]["spec"]["serviceAccountName"].endswith("-migration")


def test_migration_job_is_the_only_ddl_credential_consumer_and_has_no_runtime_secrets() -> None:
    documents = render()
    migration = next(document for document in documents if document["kind"] == "Job")
    assert "helm.sh/hook" not in migration["metadata"].get("annotations", {})
    assert migration["metadata"]["annotations"] == POSTGRESQL_ANNOTATIONS
    deployment = gateway_deployment(documents)
    assert deployment["spec"]["template"]["spec"]["initContainers"][0]["args"] == ["wait-schema"]
    assert "initContainers" not in migration["spec"]["template"]["spec"]
    accounts = {document["metadata"]["name"] for document in documents if document["kind"] == "ServiceAccount"}
    assert migration["spec"]["template"]["spec"]["serviceAccountName"] in accounts
    migration_index = documents.index(migration)
    migration_account_index = next(
        index
        for index, document in enumerate(documents)
        if document["kind"] == "ServiceAccount"
        and document["metadata"]["name"] == migration["spec"]["template"]["spec"]["serviceAccountName"]
    )
    assert migration_account_index < migration_index

    upgrade_documents = render("--is-upgrade")
    upgrade_migration = next(document for document in upgrade_documents if document["kind"] == "Job")
    assert upgrade_migration["metadata"]["annotations"] == POSTGRESQL_ANNOTATIONS | {
        "helm.sh/hook": "pre-upgrade",
        "helm.sh/hook-weight": "-5",
        "helm.sh/hook-delete-policy": "before-hook-creation,hook-succeeded",
    }
    assert gateway_deployment(upgrade_documents)["spec"]["template"]["spec"]["initContainers"][0]["args"] == [
        "wait-schema"
    ]
    migration_pod = migration["spec"]["template"]["spec"]
    container = migration_pod["containers"][0]
    assert container["args"] == ["migrate"]
    assert {item["name"] for item in container["env"]} == {
        "FS2_DATABASE_URL",
        "FS2_REPORTING_DATABASE_ROLE",
        "FS2_RUNTIME_DATABASE_ROLE",
        "FS2_MAINTENANCE_DATABASE_ROLE",
        "FS2_ACTIVATION_DATABASE_ROLE",
    }
    assert container["env"][0]["valueFrom"]["secretKeyRef"] == {
        "name": "fs2-serve-database-migrations",
        "key": "url",
    }
    assert container["volumeMounts"] == [{"name": "database-ca", "mountPath": "/tls", "readOnly": True}]
    assert migration_pod["volumes"] == [
        {
            "name": "database-ca",
            "secret": {
                "secretName": "fs2-serve-database-migrations",
                "defaultMode": 256,
                "items": [{"key": "ca.crt", "path": "ca.crt"}],
            },
        }
    ]

    other_workloads = [
        next(document for document in documents if document["kind"] == "Deployment")["spec"]["template"]["spec"],
        next(document for document in documents if document["kind"] == "CronJob")["spec"]["jobTemplate"]["spec"][
            "template"
        ]["spec"],
    ]
    assert "fs2-serve-database-migrations" not in json.dumps(other_workloads)


def test_exact_helm4_lifecycle_uses_digest_registry_and_typed_release_values() -> None:
    script = (CONTROL_ROOT / "scripts" / "test-helm4-lifecycle.sh").read_text()
    assert "helm version --template '{{.Version}}'" in script
    assert "containerdConfigPatches:" not in script
    assert "/etc/containerd/certs.d/localhost:${registry_port}" in script
    assert '[host."http://${registry_name}:5000"]' in script
    assert 'capabilities = ["pull", "resolve", "push"]' in script
    assert 'docker network connect kind "${registry_name}"' in script
    assert script.index('docker push "${image_repository}:failing-migration"') < script.index(
        'docker network connect kind "${registry_name}"'
    )
    assert "kind load docker-image" not in script
    assert script.count("--from-literal='ca.crt=test-only-placeholder-ca'") == 3
    assert "--set-string config.schemaWaitSeconds=120" in script
    assert "--wait=watcher --wait-for-jobs --rollback-on-failure" in script
    assert "--cleanup-on-fail --timeout 90s" in script
    assert "helm status fs2-serve -n fs2-system -o json" in script
    assert "SELECT count(*) FROM fs2_schema_migrations" in script
    assert '.status == "ready" and .models == 0 and .activation.required == false' in script
    assert "adminConsole.image.repository=${admin_image_repository}" in script
    assert "adminConsole.image.digest=${admin_digest}" in script
    assert "deployment/fs2-serve-control-plane-admin-console" in script
    assert "service/fs2-serve-control-plane-admin-console" in script
    assert '"404 application/problem+json"' in script


def test_maintenance_is_independent_fixed_cadence_and_network_egress_is_allowlisted() -> None:
    documents = render()
    cron = next(document for document in documents if document["kind"] == "CronJob")
    deployment = gateway_deployment(documents)
    assert cron["spec"]["schedule"] == "*/1 * * * *"
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    assert cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"] == ["maintenance"]
    maintenance_pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    maintenance = maintenance_pod["containers"][0]
    assert {item["name"] for item in maintenance["env"]} == {
        "FS2_DATABASE_URL",
        "FS2_OPERATION_RETENTION_SECONDS",
        "FS2_PAT_RETENTION_SECONDS",
        "FS2_AUDIT_RETENTION_SECONDS",
        "FS2_USAGE_RETENTION_SECONDS",
    }
    assert maintenance["env"][0]["valueFrom"]["secretKeyRef"] == {
        "name": "fs2-serve-database-maintenance",
        "key": "url",
    }
    assert maintenance["volumeMounts"] == [{"name": "database-ca", "mountPath": "/tls", "readOnly": True}]
    assert maintenance_pod["volumes"][0]["secret"]["secretName"] == "fs2-serve-database-maintenance"
    assert "fs2-serve-database-maintenance" not in json.dumps(deployment)
    assert 'fs2-serve-database"' not in json.dumps(cron)
    runtime_env_names = {item["name"] for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert (
        not {
            "FS2_OPERATION_RETENTION_SECONDS",
            "FS2_PAT_RETENTION_SECONDS",
            "FS2_AUDIT_RETENTION_SECONDS",
            "FS2_USAGE_RETENTION_SECONDS",
        }
        & runtime_env_names
    )
    policy = next(
        document
        for document in documents
        if document["kind"] == "NetworkPolicy" and document["metadata"]["name"].endswith("-maintenance")
    )
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert policy["spec"]["ingress"] == []
    assert any(5432 in [port["port"] for port in rule.get("ports", [])] for rule in policy["spec"]["egress"])
    assert any(53 in [port["port"] for port in rule.get("ports", [])] for rule in policy["spec"]["egress"])


def test_network_policies_use_exact_architecture_namespaces_labels_and_ports() -> None:
    documents = render()
    policies = {document["metadata"]["name"]: document for document in documents if document["kind"] == "NetworkPolicy"}
    assert set(policies) == {
        "fs2-serve-control-plane-runtime",
        "fs2-serve-control-plane-public-envoy",
        "fs2-serve-control-plane-acme-solver",
        "fs2-serve-control-plane-envoy-controller-xds",
        "fs2-serve-control-plane-maintenance",
        "fs2-serve-control-plane-migration",
    }
    runtime = policies["fs2-serve-control-plane-runtime"]["spec"]
    assert runtime["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "gateway"

    gateway_peer = runtime["ingress"][0]["from"][0]
    assert gateway_peer == {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "envoy-gateway-system"}},
        "podSelector": {
            "matchLabels": {
                "app.kubernetes.io/component": "proxy",
                "app.kubernetes.io/managed-by": "envoy-gateway",
                "app.kubernetes.io/name": "envoy",
                "gateway.envoyproxy.io/owning-gateway-name": "public",
                "gateway.envoyproxy.io/owning-gateway-namespace": "fs2-system",
            }
        },
    }
    metrics_peer = runtime["ingress"][1]["from"][0]
    assert metrics_peer == {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "fs2-observability"}},
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus"}},
    }
    assert all(rule["ports"] == [{"port": 8080, "protocol": "TCP"}] for rule in runtime["ingress"])

    egress_by_port = {tuple(port["port"] for port in rule["ports"]): rule["to"][0] for rule in runtime["egress"]}
    assert egress_by_port[(53, 53)] == {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
    }
    assert egress_by_port[(5432,)] == {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "fs2-data"}},
        "podSelector": {"matchLabels": {"cnpg.io/cluster": "fs2-control-db"}},
    }
    assert egress_by_port[(8000, 8080)] == {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "fs2-models"}},
        "podSelector": {
            "matchLabels": {
                "app.kubernetes.io/part-of": "fs2-serve",
            }
        },
    }
    assert egress_by_port[(4318,)] == {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "fs2-observability"}},
        "podSelector": {
            "matchLabels": {
                "app.kubernetes.io/instance": "otel-gateway",
                "app.kubernetes.io/name": "opentelemetry-collector",
            }
        },
    }
    for component in ("maintenance", "migration"):
        restricted = policies[f"fs2-serve-control-plane-{component}"]["spec"]
        assert restricted["ingress"] == []
        assert {tuple(port["port"] for port in rule["ports"]) for rule in restricted["egress"]} == {
            (53, 53),
            (5432,),
        }

    contract = json.loads((CONTROL_ROOT / "contracts" / "public-edge-artifact-observations.json").read_text())
    assert contract["envoy_gateway"]["source_commit"] == "94850d4323922cba7c67b5b5ad23de9c715f8048"
    assert contract["envoy_gateway"]["source_tree"] == "f63106a5a9d3cdabb6b5c6a0ca601307e8875ec9"
    assert contract["envoy_gateway"]["oci_manifest_sha256"] == (
        "cfb34ff4266c87a394cd6be5c13607a2dd47083aef771368302eaeaa99c4a0a9"
    )
    assert contract["cert_manager"]["source_commit"] == "24e33194fb39488eff2bbf10c6dc640f407cad44"
    assert contract["cert_manager"]["source_tree"] == "2640b89e5f7f17ace93cc5878c11f54c82f63438"
    assert contract["cert_manager"]["oci_manifest_sha256"] == (
        "15c0b46d9006ce8eb9ff14d1bf54d1bbfcc587bb9e24cd9fe186fb8fec56af1f"
    )
    proxy_selector = contract["envoy_gateway"]["proxy_selector"]
    assert proxy_selector == gateway_peer["podSelector"]["matchLabels"]
    public_envoy = policies["fs2-serve-control-plane-public-envoy"]["spec"]
    assert public_envoy["podSelector"]["matchLabels"] == proxy_selector
    assert public_envoy["ingress"] == [
        {
            "from": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
            "ports": [
                {"port": 10080, "protocol": "TCP"},
                {"port": 10443, "protocol": "TCP"},
            ],
        }
    ]
    public_egress = {tuple(port["port"] for port in rule["ports"]): rule["to"][0] for rule in public_envoy["egress"]}
    assert public_egress[(8080,)] == {
        "podSelector": {
            "matchLabels": {
                "app.kubernetes.io/component": "gateway",
                "app.kubernetes.io/instance": "fs2-serve",
                "app.kubernetes.io/name": "fs2-serve-control-plane",
            }
        }
    }
    solver_selector = contract["cert_manager"]["http01_solver_selector"]
    assert public_egress[(8089,)] == {"podSelector": {"matchLabels": solver_selector}}
    assert public_egress[(18000,)] == {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "envoy-gateway-system"}},
        "podSelector": {"matchLabels": contract["envoy_gateway"]["controller_selector"]},
    }
    solver = policies["fs2-serve-control-plane-acme-solver"]["spec"]
    assert solver["podSelector"]["matchLabels"] == solver_selector
    assert solver["ingress"] == [
        {
            "from": [
                {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "envoy-gateway-system"}},
                    "podSelector": {"matchLabels": proxy_selector},
                }
            ],
            "ports": [{"port": 8089, "protocol": "TCP"}],
        }
    ]
    xds = policies["fs2-serve-control-plane-envoy-controller-xds"]
    assert xds["metadata"]["namespace"] == "envoy-gateway-system"
    assert xds["spec"]["podSelector"]["matchLabels"] == contract["envoy_gateway"]["controller_selector"]
    assert xds["spec"]["ingress"] == [
        {
            "from": [
                {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "envoy-gateway-system"}},
                    "podSelector": {"matchLabels": proxy_selector},
                }
            ],
            "ports": [{"port": 18000, "protocol": "TCP"}],
        }
    ]


def test_gateway_alerts_are_bounded_payload_free_and_cover_release_failures() -> None:
    documents = render()
    prometheus_rule = next(document for document in documents if document["kind"] == "PrometheusRule")
    groups = prometheus_rule["spec"]["groups"]
    assert len(groups) == 1 and groups[0]["name"] == "fs2-serve-control-plane.rules"
    rules = {rule["alert"]: rule for rule in groups[0]["rules"]}
    assert set(rules) == {
        "Fs2ServeCatalogMetricsUnavailable",
        "Fs2ServeDeploymentUnavailable",
        "Fs2ServeOldestQueueAgeHigh",
        "Fs2ServeQueueDepthHigh",
        "Fs2ServeSyncWaitSaturation",
        "Fs2ServeAuthenticationFailureSpike",
        "Fs2ServeLifecycleReconciliationFailed",
        "Fs2ServeLifecycleOccupancyUnclassified",
        "Fs2ServePublicCertificateNotReady",
        "Fs2ServePublicCertificateRenewalOverdue",
        "Fs2ServePublicCertificateExpiresSoon",
    }
    rendered = json.dumps(prometheus_rule)
    assert "fs2_serve_model_info" in rendered
    assert "fs2_serve_oldest_queued_operation_age_seconds" in rendered
    assert 'fs2_serve_operations{state=\\"queued\\"}' in rendered
    assert "fs2_serve_sync_wait_saturated_total" in rendered
    assert "fs2_serve_authentication_failures_total" in rendered
    assert "fs2_serve_lifecycle_workloads_total" in rendered
    assert "fs2_serve_lifecycle_unclassified_gpu_seconds_total" in rendered
    assert "kube_deployment_status_replicas_available" in rendered
    assert "certmanager_certificate_ready_status" in rendered
    assert "certmanager_certificate_renewal_timestamp_seconds" in rendered
    assert "certmanager_certificate_expiration_timestamp_seconds" in rendered
    assert rendered.count("absent(certmanager_certificate_") == 3
    for forbidden in ("principal", "tenant", "token", "prompt", "response", "bearer"):
        assert forbidden not in rendered.lower()


def test_grafana_dashboard_is_discoverable_in_the_foundation_watch_namespace() -> None:
    documents = render()
    dashboard = next(
        document
        for document in documents
        if document["kind"] == "ConfigMap" and document["metadata"]["name"].endswith("-dashboard")
    )
    assert dashboard["metadata"]["namespace"] == "fs2-observability"
    assert dashboard["metadata"]["labels"]["grafana_dashboard"] == "1"
    dashboard_json = json.loads(dashboard["data"]["fs2-serve-control-plane.json"])
    postgres_panels = [
        panel for panel in dashboard_json["panels"] if panel.get("datasource", {}).get("type") == "postgres"
    ]
    assert postgres_panels
    assert {panel["datasource"]["uid"] for panel in postgres_panels} == {"fs2-serve-reporting"}
    assert {variable["name"] for variable in dashboard_json["templating"]["list"]} == {
        "prometheus",
        "tenant",
        "model",
    }


def test_value_suppressed_dependency_contract_binds_catalog_database_roles_and_reporting_datasource() -> None:
    documents = render()
    contract_map = next(
        document
        for document in documents
        if document["kind"] == "ConfigMap" and document["metadata"]["name"].endswith("-dependency-contract")
    )
    contract = json.loads(contract_map["data"]["dependency-contract.json"])
    assert contract["schema"] == "fs2-serve.nebius.ai/gateway-dependency-contract/v1"
    assert contract["secret_values_rendered"] is False
    assert contract["catalog"] == {
        "resource_owner": "fs2-serve-models",
        "namespace": "fs2-system",
        "catalog_pvc": "fs2-serve-catalog",
        "bindings_config_map": "fs2-serve-serving-bindings",
        "bindings_key": "serving-bindings.json",
        "variant_promotions_key": "model-variant-promotions.json",
        "evidence_pvc": "fs2-serve-model-evidence",
        "rollout_digest": TEST_CATALOG_ROLLOUT_DIGEST,
    }
    database = contract["database"]
    assert database["resource_owner"] == "postgresql-platform-release"
    assert (database["namespace"], database["cluster_name"], database["read_write_service"]) == (
        "fs2-data",
        "fs2-control-db",
        "fs2-control-db-rw",
    )
    assert database["group_roles"] == {
        "runtime": "fs2_serve_runtime",
        "maintenance": "fs2_serve_maintenance",
        "activation": "fs2_serve_activation",
        "reporting": "fs2_serve_reporting",
    }
    assert database["secret_refs"]["runtime"] == {
        "namespace": "fs2-system",
        "name": "fs2-serve-database",
        "key": "url",
    }
    assert database["secret_refs"]["maintenance"]["name"] == "fs2-serve-database-maintenance"
    assert database["secret_refs"]["migrations"]["name"] == "fs2-serve-database-migrations"
    assert database["secret_refs"]["reporting"] == {
        "namespace": "fs2-observability",
        "name": "fs2-serve-database-reporting",
        "key": "url",
    }
    assert contract["grafana"] == {
        "resource_owner": "fs2-observability",
        "namespace": "fs2-observability",
        "datasource_uid": "fs2-serve-reporting",
        "datasource_secret": "fs2-serve-postgres-grafana-datasource",
        "datasource_secret_key": "datasource.yaml",
        "datasource_label": "grafana_datasource",
        "datasource_label_value": "1",
        "allowed_relations": [
            "fs2_reporting_model_usage",
            "fs2_reporting_principal_usage",
            "fs2_reporting_gpu_phase_usage",
            "fs2_reporting_lifecycle_workloads",
        ],
    }
    assert not any(document["kind"] == "Secret" for document in documents)


def test_gateway_alerts_require_the_scrape_contract() -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [
            HELM,
            "template",
            "fs2-serve",
            str(CHART),
            "--namespace",
            "fs2-system",
            *helm_values(),
            "--set",
            "prometheusRule.enabled=true",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "serviceMonitor.enabled" in result.stderr


def test_runtime_network_peer_matches_the_models_lane_workload_contract() -> None:
    documents = render()
    policy = next(
        document
        for document in documents
        if document["kind"] == "NetworkPolicy" and document["metadata"]["name"].endswith("-runtime")
    )
    runtime_rule = next(
        rule for rule in policy["spec"]["egress"] if [port["port"] for port in rule["ports"]] == [8000, 8080]
    )
    selector = runtime_rule["to"][0]["podSelector"]["matchLabels"]
    catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
    tuple_digest = hashlib.sha256(b"control-plane-network-contract").hexdigest()
    capability = BackendCapability(
        backend_id="unit-network-contract",
        backend_class="local-kubernetes",
        admission_scope="route-qualified",
        gpu_class="NVIDIA-B300-SXM6-288GB",
        node_gpu_count=8,
        workload_gpu_count=1,
        storage_mode=None,
        runtime_tuple_digest=tuple_digest,
        _value=MappingProxyType(
            {
                "backend_identity_digest": tuple_digest,
                "storage": None,
                "scheduling": {"node_selector": {}, "tolerations": []},
                "nim_image": None,
            }
        ),
    )
    model_labels = _metadata(catalog.records["qwen3-8b"], capability)["labels"]
    assert selector.items() <= model_labels.items()


def test_federation_secret_mount_external_secret_and_exact_egress_are_hardened() -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [
            HELM,
            "template",
            "fs2-serve",
            str(CHART),
            "--namespace",
            "fs2-system",
            *helm_values(),
            "--set",
            "federation.externalSecret.enabled=true",
            "--set",
            "federation.externalSecret.secretStoreRef.name=platform-secrets",
            "--set",
            "federation.externalSecret.remoteKey=fs2/serve/federation",
            "--set",
            "networkPolicy.federationCidrs[0]=8.8.8.8/32",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    documents = [document for document in yaml.safe_load_all(result.stdout) if document]
    external = next(document for document in documents if document["kind"] == "ExternalSecret")
    assert external["apiVersion"] == "external-secrets.io/v1"
    assert external["spec"]["target"]["name"] == "fs2-serve-federation"
    assert external["spec"]["dataFrom"] == [{"extract": {"key": "fs2/serve/federation"}}]
    deployment = gateway_deployment(documents)
    pod = deployment["spec"]["template"]["spec"]
    federation = next(volume for volume in pod["volumes"] if volume["name"] == "federation")
    assert federation["secret"] == {
        "secretName": "fs2-serve-federation",
        "optional": True,
        "defaultMode": 256,
    }
    container = pod["containers"][0]
    mount = next(mount for mount in container["volumeMounts"] if mount["name"] == "federation")
    assert mount["readOnly"] is True
    env = {item["name"]: item["value"] for item in container["env"] if "value" in item}
    assert env["FS2_FEDERATION_ROUTES_FILE"] == "/var/run/secrets/fs2-serve/federation/routes.json"
    policy = next(
        document
        for document in documents
        if document["kind"] == "NetworkPolicy" and document["metadata"]["name"].endswith("-runtime")
    )
    exact_egress = next(
        rule for rule in policy["spec"]["egress"] if rule.get("to") == [{"ipBlock": {"cidr": "8.8.8.8/32"}}]
    )
    assert exact_egress["ports"] == [{"port": 443, "protocol": "TCP"}]
    rendered = json.dumps(documents)
    assert "federation-test-value" not in rendered and "upstream.unit.invalid" not in rendered


@pytest.mark.parametrize("cidr", ["0.0.0.0/0", "::/0", "8.8.8.0/24"])
def test_chart_rejects_broad_federation_egress(cidr: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [
            HELM,
            "template",
            "fs2-serve",
            str(CHART),
            *helm_values(),
            "--set-string",
            f"networkPolicy.federationCidrs[0]={cidr}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "federationCidrs" in result.stderr


def test_public_route_exposes_inference_and_session_authenticated_admin_paths() -> None:
    documents = render()
    route = application_route(documents)
    redirect = redirect_route(documents)
    paths = {
        match["path"]["value"]: match["path"]["type"]
        for rule in route["spec"]["rules"]
        for match in rule.get("matches", [])
    }
    assert paths == {
        "/v1": "PathPrefix",
        "/mcp": "Exact",
        "/admin/api/v1": "PathPrefix",
        "/.well-known/oauth-protected-resource": "Exact",
        "/.well-known/oauth-protected-resource/mcp": "Exact",
        "/readyz": "Exact",
    }
    assert "/" not in paths
    removed = route["spec"]["rules"][0]["filters"][0]["requestHeaderModifier"]["remove"]
    assert set(removed) == {
        "x-fs2-tenant",
        "x-fs2-principal",
        "x-fs2-token-id",
        "x-fs2-model-scope",
        "x-fs2-accounting-id",
    }
    assert route["spec"]["parentRefs"][0]["sectionName"] == "public-https"
    assert route["spec"]["rules"][0]["timeouts"] == {
        "request": "40s",
        "backendRequest": "40s",
    }
    assert redirect["spec"] == {
        "parentRefs": [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "Gateway",
                "name": "public",
                "sectionName": "acme-http",
            }
        ],
        "rules": [
            {
                "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                "filters": [
                    {
                        "type": "RequestRedirect",
                        "requestRedirect": {"scheme": "https", "port": 443, "statusCode": 308},
                    }
                ],
            }
        ],
    }
    assert all("backendRefs" not in rule for rule in redirect["spec"]["rules"])
    assert all("backendRefs" in rule for rule in route["spec"]["rules"])
    rate_limit = next(document for document in documents if document["kind"] == "BackendTrafficPolicy")
    assert rate_limit["apiVersion"] == "gateway.envoyproxy.io/v1alpha1"
    assert rate_limit["spec"]["targetRefs"] == [
        {
            "group": "gateway.networking.k8s.io",
            "kind": "HTTPRoute",
            "name": "fs2-serve-control-plane",
        }
    ]
    rule = rate_limit["spec"]["rateLimit"]["local"]["rules"][0]
    assert rule == {"limit": {"requests": 200, "unit": "Second"}}
    assert rate_limit["spec"]["mergeType"] == "StrategicMerge"


def test_enabled_public_route_rejects_an_incomplete_edge() -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [
            HELM,
            "template",
            "fs2-serve",
            str(CHART),
            *helm_values(),
            "--set",
            "httpRoute.enabled=true",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "publicGateway" in result.stderr and "enabled" in result.stderr


def test_internal_only_chart_lints_and_renders_configured_loopback_origin() -> None:
    operator_origin = "http://localhost:28082"
    command = [
        HELM,
        "template",
        "fs2-serve",
        str(CHART),
        "--namespace",
        "fs2-system",
        *helm_values(),
        *admin_console_values(route=False),
        "--set",
        f"config.publicBaseUrl={operator_origin}",
        "--set",
        f"config.authorizationServerUrl={operator_origin}",
        "--set",
        "config.publicAuthorityMode=dns",
        "--set",
        "config.allowNonClusterUrls=true",
        "--set",
        "httpRoute.authorityMode=dns",
    ]
    lint_command = [HELM, "lint", str(CHART), *command[4:]]
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    lint = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        lint_command,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "0 chart(s) failed" in lint.stdout
    documents = [document for document in yaml.safe_load_all(result.stdout) if document]
    kinds = {document["kind"] for document in documents}
    names = {document["metadata"]["name"] for document in documents}
    assert "Gateway" not in kinds
    assert "HTTPRoute" not in kinds
    assert "Issuer" not in kinds
    assert "fs2-serve-control-plane-admin-console" in names
    deployment = gateway_deployment(documents)
    environment = {
        item["name"]: item.get("value") for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert environment["FS2_PUBLIC_BASE_URL"] == operator_origin
    assert environment["FS2_AUTHORIZATION_SERVER_URL"] == operator_origin
    assert environment["FS2_ALLOW_NON_CLUSTER_URLS"] == "true"


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            ("--set", "config.allowNonClusterUrls=true"),
            "value must be false",
        ),
        (
            (
                "--set",
                "config.allowNonClusterUrls=true",
                "--set",
                "publicGateway.enabled=false",
                "--set",
                "publicLoadBalancer.enabled=false",
                "--set",
                "publicTls.enabled=false",
                "--set",
                "publicTls.acmeIssuer.enabled=false",
                "--set",
                "httpRoute.enabled=false",
                "--set",
                "config.publicAuthorityMode=dns",
                "--set",
                "httpRoute.authorityMode=dns",
                "--set",
                "config.publicBaseUrl=https://cluster.internal.invalid",
                "--set",
                "config.authorizationServerUrl=https://cluster.internal.invalid",
            ),
            "http://localhost",
        ),
        (
            (
                "--set",
                "config.allowNonClusterUrls=true",
                "--set",
                "publicGateway.enabled=false",
                "--set",
                "publicLoadBalancer.enabled=false",
                "--set",
                "publicTls.enabled=false",
                "--set",
                "publicTls.acmeIssuer.enabled=false",
                "--set",
                "httpRoute.enabled=false",
                "--set",
                "config.publicAuthorityMode=dns",
                "--set",
                "httpRoute.authorityMode=dns",
                "--set",
                "config.publicBaseUrl=http://localhost:28082",
                "--set",
                "config.authorizationServerUrl=http://localhost:28083",
            ),
            "same operator-proxy port",
        ),
        (
            (
                "--set",
                "config.allowNonClusterUrls=true",
                "--set",
                "publicGateway.enabled=false",
                "--set",
                "publicLoadBalancer.enabled=false",
                "--set",
                "publicTls.enabled=false",
                "--set",
                "publicTls.acmeIssuer.enabled=false",
                "--set",
                "httpRoute.enabled=false",
                "--set",
                "config.publicAuthorityMode=dns",
                "--set",
                "httpRoute.authorityMode=dns",
                "--set",
                "config.publicBaseUrl=http://localhost:65536",
                "--set",
                "config.authorizationServerUrl=http://localhost:65536",
            ),
            "1024 through 65535",
        ),
    ],
)
def test_internal_url_escape_hatch_rejects_public_gateway_or_synthetic_origin(
    arguments: tuple[str, ...],
    expected: str,
) -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and bounded adversarial values
        render_command(*arguments),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert expected in result.stderr


def test_public_edge_rejects_disabled_release_owned_network_policies() -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        render_command("--set", "networkPolicy.enabled=false"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "release-owned NetworkPolicies" in result.stderr or "networkPolicy/enabled" in result.stderr


def test_inert_edge_does_not_open_envoy_or_solver_flows() -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [HELM, "template", "fs2-serve", str(CHART), "--namespace", "fs2-system", *helm_values()],
        check=True,
        capture_output=True,
        text=True,
    )
    documents = [document for document in yaml.safe_load_all(result.stdout) if document]
    policy_names = {document["metadata"]["name"] for document in documents if document["kind"] == "NetworkPolicy"}
    assert policy_names == {
        "fs2-serve-control-plane-runtime",
        "fs2-serve-control-plane-maintenance",
        "fs2-serve-control-plane-migration",
    }


def test_direct_ip_edge_is_complete_tls_only_and_acme_reachable() -> None:
    documents = render()
    gateway_class = next(document for document in documents if document["kind"] == "GatewayClass")
    gateway = next(document for document in documents if document["kind"] == "Gateway")
    route = application_route(documents)
    redirect = redirect_route(documents)
    issuer = next(document for document in documents if document["kind"] == "Issuer")
    certificate = next(document for document in documents if document["kind"] == "Certificate")
    client_policy = next(document for document in documents if document["kind"] == "ClientTrafficPolicy")
    assert gateway_class["spec"] == {
        "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
        "parametersRef": {
            "group": "gateway.envoyproxy.io",
            "kind": "EnvoyProxy",
            "name": "fs2-serve-control-plane-public",
            "namespace": "fs2-system",
        },
    }
    assert "annotations" not in gateway_class["metadata"]
    assert gateway["spec"]["gatewayClassName"] == "fs2-serve-public"
    listeners = {listener["name"]: listener for listener in gateway["spec"]["listeners"]}
    assert set(listeners) == {"acme-http", "public-https"}
    assert listeners["acme-http"] == {
        "name": "acme-http",
        "protocol": "HTTP",
        "port": 80,
        "allowedRoutes": {
            "namespaces": {"from": "Same"},
            "kinds": [{"group": "gateway.networking.k8s.io", "kind": "HTTPRoute"}],
        },
    }
    assert listeners["public-https"] == {
        "name": "public-https",
        "protocol": "HTTPS",
        "port": 443,
        "tls": {
            "mode": "Terminate",
            "certificateRefs": [{"group": "", "kind": "Secret", "name": "fs2-serve-public-tls"}],
        },
        "allowedRoutes": {
            "namespaces": {"from": "Same"},
            "kinds": [{"group": "gateway.networking.k8s.io", "kind": "HTTPRoute"}],
        },
    }
    assert "hostname" not in listeners["acme-http"] and "hostname" not in listeners["public-https"]
    assert "addresses" not in gateway["spec"]
    assert client_policy["spec"] == {
        "targetRefs": [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "Gateway",
                "name": "public",
                "sectionName": "public-https",
            }
        ],
        "tls": {"minVersion": "1.2", "maxVersion": "1.3"},
    }
    assert route["spec"]["parentRefs"] == [
        {
            "group": "gateway.networking.k8s.io",
            "kind": "Gateway",
            "name": "public",
            "sectionName": "public-https",
        }
    ]
    assert "hostnames" not in route["spec"]
    assert "hostnames" not in redirect["spec"]
    assert issuer["spec"]["acme"] == {
        "server": "https://acme-staging-v02.api.letsencrypt.org/directory",
        "profile": "shortlived",
        "email": TEST_ACME_EMAIL,
        "privateKeySecretRef": {"name": "fs2-serve-ip-acme-account"},
        "solvers": [
            {
                "http01": {
                    "gatewayHTTPRoute": {
                        "serviceType": "ClusterIP",
                        "parentRefs": [
                            {
                                "group": "gateway.networking.k8s.io",
                                "kind": "Gateway",
                                "name": "public",
                                "namespace": "fs2-system",
                                "sectionName": "acme-http",
                            }
                        ],
                    }
                }
            }
        ],
    }
    proxy = next(document for document in documents if document["kind"] == "EnvoyProxy")
    service = proxy["spec"]["provider"]["kubernetes"]["envoyService"]
    assert service == expected_envoy_service()
    assert "nebius.com/load-balancer-type" not in service["annotations"]
    assert certificate["spec"]["ipAddresses"] == [TEST_PUBLIC_IP]
    assert certificate["spec"]["duration"] == "160h"
    assert certificate["spec"]["renewBeforePercentage"] == 40
    assert certificate["spec"]["issuerRef"] == {
        "name": "fs2-serve-ip-acme",
        "kind": "Issuer",
        "group": "cert-manager.io",
    }
    assert certificate["spec"]["privateKey"]["rotationPolicy"] == "Always"
    for document in (gateway_class, gateway, route, redirect):
        assert TEST_PUBLIC_IP not in json.dumps(document)
    assert not {"Secret", "ClusterIssuer"} & {document["kind"] for document in documents}


def test_foundation_default_deny_and_release_edge_flows_coexist_without_plaintext_app_access() -> None:
    documents = render()
    contract = json.loads((CONTROL_ROOT / "contracts" / "public-edge-artifact-observations.json").read_text())
    assert contract["release_authority"] is False
    assert contract["foundation_dependency"] == {
        "independent_receipt_required": True,
        "combined_render_required": True,
        "default_deny_namespaces": ["envoy-gateway-system", "fs2-system"],
    }
    assert contract["envoy_gateway"]["external_traffic_policy"] == "Cluster"
    assert contract["envoy_gateway"]["listener_service_ports"] == [
        {
            "name": "http-80",
            "port": 80,
            "target_port": 10080,
            "node_port": TEST_HTTP_NODE_PORT,
        },
        {
            "name": "https-443",
            "port": 443,
            "target_port": 10443,
            "node_port": TEST_HTTPS_NODE_PORT,
        },
    ]
    policies = {
        (document["metadata"].get("namespace", "fs2-system"), document["metadata"]["name"]): document
        for document in documents
        if document["kind"] == "NetworkPolicy"
    }
    assert ("fs2-system", "fs2-serve-control-plane-public-envoy") in policies
    assert ("fs2-system", "fs2-serve-control-plane-acme-solver") in policies
    assert ("envoy-gateway-system", "fs2-serve-control-plane-envoy-controller-xds") in policies

    redirect = redirect_route(documents)
    application = application_route(documents)
    assert redirect["spec"]["parentRefs"][0]["sectionName"] == "acme-http"
    assert application["spec"]["parentRefs"][0]["sectionName"] == "public-https"
    assert "backendRefs" not in redirect["spec"]["rules"][0]
    assert application["spec"]["rules"][0]["backendRefs"] == [{"name": "fs2-serve-control-plane", "port": 8080}]

    solver_match = contract["cert_manager"]["http01_route_match"]
    assert solver_match["path_type"] == "Exact"
    assert solver_match["path_prefix"] == "/.well-known/acme-challenge/"
    assert redirect["spec"]["rules"][0]["matches"][0]["path"]["type"] == "PathPrefix"
    # Gateway API precedence makes the cert-manager Exact challenge match win
    # over this chart's catch-all PathPrefix redirect.
    assert solver_match["path_type"] != redirect["spec"]["rules"][0]["matches"][0]["path"]["type"]


def test_production_acme_issuer_uses_exact_directory_without_manual_receipt_fields() -> None:
    documents = render(
        "--set",
        "publicTls.acmeIssuer.environment=production",
    )
    issuer = next(document for document in documents if document["kind"] == "Issuer")
    assert issuer["spec"]["acme"]["server"] == "https://acme-v02.api.letsencrypt.org/directory"
    assert issuer["spec"]["acme"]["profile"] == "shortlived"
    assert "privateKey" not in issuer["spec"]["acme"]["privateKeySecretRef"]


@pytest.mark.parametrize(
    "hostname_arguments",
    [
        ["--set", "httpRoute.hostnames[0]=203.0.113.17"],
        ["--set-json", "httpRoute.hostnames=[]"],
    ],
)
def test_ip_authority_forbids_hostnames_values_even_when_empty(hostname_arguments: list[str]) -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [
            HELM,
            "template",
            "fs2-serve",
            str(CHART),
            *helm_values(),
            "--set",
            "config.publicBaseUrl=https://203.0.113.17",
            "--set",
            "config.publicAuthorityMode=ip",
            "--set",
            "httpRoute.authorityMode=ip",
            *hostname_arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "httpRoute" in result.stderr and "hostnames" in result.stderr


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ("publicGateway.gatewayName=attacker", "gatewayName"),
        ("publicGateway.gatewayClassName=attacker", "gatewayClassName"),
        ("publicGateway.httpsListenerName=acme-http", "httpsListenerName"),
        ("publicGateway.httpListenerName=public-https", "httpListenerName"),
    ],
)
def test_public_edge_rejects_parent_or_listener_authority_substitution(override: str, expected: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and bounded adversarial values.
        render_command("--set-string", override),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert expected in result.stderr


def test_public_allocation_project_mismatch_fails_closed() -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [
            HELM,
            "template",
            "fs2-serve",
            str(CHART),
            *helm_values(),
            *edge_prerequisite_values(),
            "--set",
            "publicLoadBalancer.enabled=true",
            "--set",
            f"publicLoadBalancer.targetProjectId={TEST_TARGET_PROJECT_ID}",
            "--set",
            "publicLoadBalancer.allocationProjectId=project-wrong",
            "--set",
            f"publicLoadBalancer.allocationId={TEST_ALLOCATION_ID}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "allocation project must match" in result.stderr


def test_public_allocation_type_mismatch_fails_closed() -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [
            HELM,
            "template",
            "fs2-serve",
            str(CHART),
            *helm_values(),
            *edge_prerequisite_values(),
            "--set",
            "publicLoadBalancer.enabled=true",
            "--set",
            f"publicLoadBalancer.targetProjectId={TEST_TARGET_PROJECT_ID}",
            "--set",
            f"publicLoadBalancer.allocationProjectId={TEST_TARGET_PROJECT_ID}",
            "--set",
            "publicLoadBalancer.allocationType=private-ipv4",
            "--set",
            f"publicLoadBalancer.allocationId={TEST_ALLOCATION_ID}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "public-ipv4" in result.stderr


@pytest.mark.parametrize(
    "allocation_id",
    [
        "vpcallocation-placeholder",
        "vpcallocation-replaceme",
        "vpcallocation-unitfixture",
    ],
)
def test_public_allocation_placeholder_fails_closed(allocation_id: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [
            HELM,
            "template",
            "fs2-serve",
            str(CHART),
            *helm_values(),
            *edge_prerequisite_values(),
            "--set",
            "publicLoadBalancer.enabled=true",
            "--set",
            f"publicLoadBalancer.targetProjectId={TEST_TARGET_PROJECT_ID}",
            "--set",
            f"publicLoadBalancer.allocationProjectId={TEST_TARGET_PROJECT_ID}",
            "--set",
            f"publicLoadBalancer.allocationId={allocation_id}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "exact provider identities, not placeholders" in result.stderr


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ("image.repository=registry.nebius.cloud/replace-me/fs2-serve-control-plane", "repository"),
        ("image.digest=sha256:" + "0" * 64, "digest"),
        ("config.publicBaseUrl=https://inference.example.invalid", "publicBaseUrl"),
        ("config.authorizationServerUrl=https://identity.example.invalid", "authorizationServerUrl"),
    ],
)
def test_chart_rejects_placeholder_release_values(override: str, expected: str) -> None:
    arguments = helm_values()
    key = override.split("=", 1)[0]
    for index, value in enumerate(arguments):
        if value.startswith(f"{key}="):
            arguments[index] = override
            break
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [HELM, "template", "fs2-serve", str(CHART), "--namespace", "fs2-system", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert expected in result.stderr


def test_chart_rejects_reusing_the_runtime_database_credential_for_migrations() -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [
            HELM,
            "template",
            "fs2-serve",
            str(CHART),
            "--namespace",
            "fs2-system",
            *helm_values(),
            "--set",
            "secrets.migrationsDatabase.name=fs2-serve-database",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "distinct DML and DDL credentials" in result.stderr


def test_chart_does_not_accept_an_activation_database_secret() -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [
            HELM,
            "template",
            "fs2-serve",
            str(CHART),
            "--namespace",
            "fs2-system",
            *helm_values(),
            "--set",
            "secrets.activationDatabase.name=fs2-serve-database-activation",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "additional properties 'activationDatabase' not allowed" in result.stderr


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("activationDatabaseRole", "fs2_serve_runtime"),
        ("activationDatabaseRole", "fs2_serve_reporting"),
        ("activationDatabaseRole", "fs2_serve_maintenance"),
        ("runtimeDatabaseRole", "fs2_serve_reporting"),
        ("maintenanceDatabaseRole", "fs2_serve_runtime"),
    ],
)
def test_chart_rejects_database_role_reuse(field: str, value: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [
            HELM,
            "template",
            "fs2-serve",
            str(CHART),
            "--namespace",
            "fs2-system",
            *helm_values(),
            "--set-string",
            f"migration.{field}={value}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "database roles must differ" in result.stderr


@pytest.mark.parametrize(
    ("namespace", "override", "expected"),
    [
        ("fs2-data", None, "credential Secrets in fs2-system"),
        (
            "fs2-system",
            r"networkPolicy.database.namespaceLabels.kubernetes\.io/metadata\.name=fs2-system",
            "database Cluster in fs2-data",
        ),
        (
            "fs2-system",
            r"networkPolicy.database.podLabels.cnpg\.io/cluster=other-db",
            "Cluster fs2-data/fs2-control-db",
        ),
        ("fs2-system", "secrets.database.name=other-runtime", "database Secret names and keys"),
        ("fs2-system", "secrets.maintenanceDatabase.name=fs2-serve-database", "database Secret names and keys"),
        ("fs2-system", "migration.runtimeDatabaseRole=other_runtime", "database group roles"),
        (
            "fs2-system",
            "migration.releaseContract.migrationSetSha256=" + "0" * 64,
            "migrationSetSha256",
        ),
    ],
)
def test_chart_rejects_postgresql_namespace_secret_role_or_receipt_drift(
    namespace: str,
    override: str | None,
    expected: str,
) -> None:
    command = [HELM, "template", "fs2-serve", str(CHART), "--namespace", namespace, *helm_values()]
    if override is not None:
        command.extend(["--set-string", override])
    result = subprocess.run(  # noqa: S603 - fixed local Helm and bounded adversarial values.
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert expected in result.stderr


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (
            "serviceAccounts.maintenance.name=shared",
            "serviceAccounts.runtime.name=shared",
            "distinct service accounts",
        ),
        (
            "secrets.ledgerHmacKeyring.name=shared-secret",
            "secrets.payloadKeyring.name=shared-secret",
            "distinct Secret objects",
        ),
        (
            "secrets.maintenanceDatabase.name=shared-db-secret",
            "secrets.database.name=shared-db-secret",
            "database Secret names and keys",
        ),
    ],
)
def test_chart_rejects_identity_or_secret_boundary_collapse(first: str, second: str, expected: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        [
            HELM,
            "template",
            "fs2-serve",
            str(CHART),
            "--namespace",
            "fs2-system",
            *helm_values(),
            "--set",
            first,
            "--set",
            second,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert expected in result.stderr


def test_deployment_wires_bounded_sync_wait_and_backoff_settings() -> None:
    documents = render()
    deployment = gateway_deployment(documents)
    env = {
        item["name"]: item["value"]
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in item
    }
    assert env["FS2_SYNC_WAIT_SECONDS"] == "2"
    assert env["FS2_MAX_SYNC_WAIT_SECONDS"] == "30"
    assert env["FS2_MAX_SYNC_WAITERS"] == "32"
    assert env["FS2_WAIT_POLL_INITIAL_SECONDS"] == "0.05"
    assert env["FS2_WAIT_POLL_MAX_SECONDS"] == "0.5"
    assert "FS2_ACTIVATION_TIMEOUT_SECONDS" not in env


def test_elastic_release_can_extend_activation_timeout_to_two_hours() -> None:
    documents = render("--set-string", "config.activationTimeoutSeconds=7200")
    deployment = gateway_deployment(documents)
    env = {
        item["name"]: item["value"]
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in item
    }
    assert env["FS2_ACTIVATION_TIMEOUT_SECONDS"] == "7200"


@pytest.mark.parametrize("timeout", ["0", "7201"])
def test_chart_rejects_activation_timeout_outside_application_bound(timeout: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and bounded adversarial value.
        [
            HELM,
            "template",
            "fs2-serve",
            str(CHART),
            "--namespace",
            "fs2-system",
            *helm_values(),
            "--set-string",
            f"config.activationTimeoutSeconds={timeout}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "activationTimeoutSeconds" in result.stderr


def test_chart_rejects_incoherent_sync_wait_limits() -> None:
    cases = (
        ("config.maxSyncWaiters=3", "config.workerConcurrency=4", "maxSyncWaiters"),
        ("config.syncWaitSeconds=31", "config.maxSyncWaitSeconds=30", "syncWaitSeconds"),
        ("config.waitPollInitialSeconds=1", "config.waitPollMaxSeconds=0.5", "waitPollInitialSeconds"),
    )
    for first, second, expected in cases:
        result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
            [
                HELM,
                "template",
                "fs2-serve",
                str(CHART),
                "--namespace",
                "fs2-system",
                *helm_values(),
                "--set-string",
                first,
                "--set-string",
                second,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert expected in result.stderr


def test_grafana_dashboard_is_valid_and_separates_estimate_dcgm_and_principal_ledger() -> None:
    documents = render()
    dashboard_map = next(
        document
        for document in documents
        if document["kind"] == "ConfigMap" and "fs2-serve-control-plane.json" in document.get("data", {})
    )
    dashboard = json.loads(dashboard_map["data"]["fs2-serve-control-plane.json"])
    rendered = json.dumps(dashboard)
    assert "fs2_serve_estimated_gpu_seconds_total" in rendered
    assert "not measured utilization" in rendered
    assert "DCGM_FI_DEV_GPU_UTIL" in rendered
    assert "principal_id" in rendered and "fs2_reporting_principal_usage" in rendered
    assert "fs2_reporting_model_usage" in rendered and "FROM fs2_operations" not in rendered
    assert "fs2_serve_lifecycle_gpu_seconds_total" in rendered
    assert "fs2_serve_lifecycle_clock_gpu_seconds_total" in rendered
    assert "fs2_serve_lifecycle_reconciliation_delta_seconds_total" in rendered
    assert "fs2_reporting_lifecycle_workloads" in rendered
    assert "occupied idle" in rendered.lower()
    assert '"uid": "fs2-serve-reporting"' in rendered
    assert "$postgres" not in rendered
    assert "request_ciphertext" not in rendered and "response_ciphertext" not in rendered


def test_grafana_reporting_role_is_aggregate_only_and_provisioned_by_migration_job() -> None:
    documents = render()
    migration = next(document for document in documents if document["kind"] == "Job")
    environment = {item["name"]: item for item in migration["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert environment["FS2_REPORTING_DATABASE_ROLE"]["value"] == "fs2_serve_reporting"
    assert environment["FS2_RUNTIME_DATABASE_ROLE"]["value"] == "fs2_serve_runtime"
    assert environment["FS2_MAINTENANCE_DATABASE_ROLE"]["value"] == "fs2_serve_maintenance"

    store_source = (CONTROL_ROOT / "src" / "fs2_serve" / "postgres.py").read_text()
    assert "CREATE ROLE" in store_source and "NOLOGIN" in store_source
    assert "fs2_operation_events,fs2_audit_events,fs2_usage_facts" in store_source
    assert "GRANT SELECT ON fs2_reporting_model_usage,fs2_reporting_principal_usage" in store_source
    assert "GRANT SELECT ON fs2_operations" not in store_source
    assert "GRANT SELECT,INSERT ON fs2_operation_events,fs2_audit_events" in store_source
    assert "DELETE ON fs2_audit_events TO {quoted_maintenance}" in store_source
    assert "DELETE ON fs2_usage_facts TO {quoted_maintenance}" in store_source
    assert "GRANT SELECT (id,model_id,model_revision,status,attempt,lease_expires_at,deadline_at) " in store_source
    assert "GRANT SELECT (id,model_id,model_revision,status,attempt,max_attempts,worker_id," not in store_source


def test_admin_read_adapters_are_default_off_without_credentials_or_rbac() -> None:
    documents = render()
    deployment = gateway_deployment(documents)
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env_names = {item["name"] for item in container["env"]}
    volume_names = {item["name"] for item in pod["volumes"]}
    names = {document["metadata"]["name"] for document in documents}

    assert pod["automountServiceAccountToken"] is False
    assert "FS2_ADMIN_CAPACITY_ENABLED" not in env_names
    assert "FS2_ADMIN_KUBERNETES_TOKEN_FILE" not in env_names
    assert "FS2_ADMIN_PROMETHEUS_URL" not in env_names
    assert "admin-kubernetes" not in volume_names
    assert "admin-observability" not in volume_names
    assert not any("admin-capacity-reader" in name for name in names)


def test_capacity_adapter_has_short_lived_token_and_exact_list_only_rbac() -> None:
    documents = render(
        "--set",
        "adminReadAdapters.capacity.enabled=true",
        "--set-string",
        "networkPolicy.kubernetesApiCidrs[0]=192.0.2.10/32",
        "--set-string",
        "adminReadAdapters.context.project=project-e00unit",
        "--set-string",
        "adminReadAdapters.context.cluster=k8s-inference-unit",
        "--set-string",
        "adminReadAdapters.context.region=eu-north1",
        "--set-string",
        "adminReadAdapters.context.label=Unit cluster",
    )
    pod = gateway_deployment(documents)["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"] if "value" in item}
    volume = next(item for item in pod["volumes"] if item["name"] == "admin-kubernetes")
    sources = volume["projected"]["sources"]

    assert pod["automountServiceAccountToken"] is False
    assert env["FS2_ADMIN_CAPACITY_ENABLED"] == "true"
    assert env["FS2_ADMIN_KUBERNETES_API_URL"] == "https://kubernetes.default.svc"
    assert env["FS2_ADMIN_KUBERNETES_TOKEN_FILE"].endswith("/admin-kubernetes/token")
    assert env["FS2_ADMIN_KUBERNETES_CACHE_TTL_SECONDS"] == "15"
    assert env["FS2_ADMIN_CONTEXT_PROJECT"] == "project-e00unit"
    assert env["FS2_ADMIN_CONTEXT_CLUSTER"] == "k8s-inference-unit"
    assert env["FS2_ADMIN_CONTEXT_REGION"] == "eu-north1"
    assert env["FS2_ADMIN_CONTEXT_LABEL"] == "Unit cluster"
    assert sources[0]["serviceAccountToken"] == {
        "expirationSeconds": 600,
        "path": "token",
    }
    assert sources[1]["configMap"] == {
        "name": "kube-root-ca.crt",
        "items": [{"key": "ca.crt", "path": "ca.crt"}],
    }
    assert next(item for item in container["volumeMounts"] if item["name"] == "admin-kubernetes")["readOnly"] is True

    cluster_role = next(
        item
        for item in documents
        if item["kind"] == "ClusterRole" and item["metadata"]["name"].endswith("admin-capacity-reader")
    )
    assert cluster_role["rules"] == [
        {"apiGroups": [""], "resources": ["nodes"], "verbs": ["list"]},
        {
            "apiGroups": ["kueue.x-k8s.io"],
            "resources": ["clusterqueues", "resourceflavors", "cohorts"],
            "verbs": ["list"],
        },
    ]
    roles = {
        (item["metadata"]["namespace"], item["metadata"]["name"]): item
        for item in documents
        if item["kind"] == "Role" and "admin-capacity-reader" in item["metadata"]["name"]
    }
    model_role = next(item for (namespace, _), item in roles.items() if namespace == "fs2-models")
    system_role = next(
        item for (namespace, name), item in roles.items() if namespace == "fs2-system" and name.endswith("-system")
    )
    autoscaler_status_role = next(
        item
        for (namespace, name), item in roles.items()
        if namespace == "kube-system" and name.endswith("-cluster-autoscaler-status")
    )
    assert model_role["rules"] == [
        {"apiGroups": [""], "resources": ["pods", "services"], "verbs": ["list"]},
        {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["list"]},
        {
            "apiGroups": ["autoscaling"],
            "resources": ["horizontalpodautoscalers"],
            "verbs": ["list"],
        },
        {
            "apiGroups": ["kueue.x-k8s.io"],
            "resources": ["localqueues", "workloads"],
            "verbs": ["list"],
        },
        {"apiGroups": ["keda.sh"], "resources": ["scaledobjects"], "verbs": ["list"]},
    ]
    assert system_role["rules"] == [
        {
            "apiGroups": ["autoscaling"],
            "resources": ["horizontalpodautoscalers"],
            "verbs": ["list"],
        }
    ]
    assert autoscaler_status_role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["configmaps"],
            "resourceNames": ["cluster-autoscaler-status"],
            "verbs": ["get"],
        }
    ]
    rbac_json = json.dumps([cluster_role, *roles.values()])
    for forbidden in ("watch", "create", "update", "patch", "delete", "secrets", "pods/log", "pods/exec"):
        assert forbidden not in rbac_json

    runtime_policy = next(
        item for item in documents if item["kind"] == "NetworkPolicy" and item["metadata"]["name"].endswith("-runtime")
    )
    api_egress = next(
        rule for rule in runtime_policy["spec"]["egress"] if rule["ports"] == [{"port": 443, "protocol": "TCP"}]
    )
    assert api_egress["to"] == [{"ipBlock": {"cidr": "192.0.2.10/32"}}]


def test_managed_node_scaler_provider_is_bound_to_the_mounted_pool_contract() -> None:
    digest = "d" * 64
    documents = render(
        "--set",
        "adminReadAdapters.capacity.enabled=true",
        "--set-string",
        "networkPolicy.kubernetesApiCidrs[0]=192.0.2.10/32",
        "--set",
        "adminReadAdapters.capacity.nodeScalerProvider=nebius-managed-node-group-autoscaler",
        "--set",
        "adminConfiguration.enabled=true",
        "--set",
        f"adminConfiguration.configMapName=fs2-admin-configuration-{digest[:16]}",
        "--set",
        f"adminConfiguration.sha256={digest}",
    )
    container = gateway_deployment(documents)["spec"]["template"]["spec"]["containers"][0]
    environment = {item["name"]: item["value"] for item in container["env"] if "value" in item}
    assert environment["FS2_ADMIN_NODE_SCALER_PROVIDER"] == "nebius-managed-node-group-autoscaler"
    assert environment["FS2_ADMIN_CONFIGURATION_FILE"] == "/etc/fs2-serve/admin/admin-configuration.json"


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (
            (
                "--set",
                "adminReadAdapters.capacity.nodeScalerProvider=nebius-managed-node-group-autoscaler",
            ),
            "requires the capacity adapter",
        ),
        (
            (
                "--set",
                "adminReadAdapters.capacity.enabled=true",
                "--set-string",
                "networkPolicy.kubernetesApiCidrs[0]=192.0.2.10/32",
                "--set",
                "adminReadAdapters.capacity.nodeScalerProvider=nebius-managed-node-group-autoscaler",
            ),
            "requires the exact adminConfiguration pool contract",
        ),
    ],
)
def test_managed_node_scaler_rejects_incomplete_chart_wiring(extra: tuple[str, ...], expected: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and bounded adversarial values
        render_command(*extra),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert expected in result.stderr


@pytest.mark.parametrize(
    "extra",
    [
        ("--set", "adminReadAdapters.capacity.enabled=true"),
        (
            "--set",
            "adminReadAdapters.capacity.enabled=true",
            "--set-string",
            "networkPolicy.kubernetesApiCidrs[0]=0.0.0.0/0",
        ),
        (
            "--set",
            "adminReadAdapters.observability.enabled=true",
            "--set",
            "networkPolicy.enabled=false",
        ),
    ],
)
def test_admin_read_adapters_reject_missing_or_unbounded_network_access(extra: tuple[str, ...]) -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and bounded adversarial values
        render_command(*extra),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "networkPolicy" in result.stderr or "kubernetesApiCidrs" in result.stderr


def test_observability_adapter_has_explicit_prometheus_peer_and_optional_config() -> None:
    documents = render(
        "--set",
        "adminReadAdapters.observability.enabled=true",
        "--set-string",
        "adminReadAdapters.observability.links.allowedHosts[0]=observe.example.invalid",
        "--set-string",
        "adminReadAdapters.observability.links.grafana.url=https://observe.example.invalid/observability/grafana/",
        "--set",
        "adminReadAdapters.observability.links.grafana.verifiedExternalRoute=true",
    )
    pod = gateway_deployment(documents)["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"] if "value" in item}
    volume = next(item for item in pod["volumes"] if item["name"] == "admin-observability")

    assert env["FS2_ADMIN_PROMETHEUS_URL"].endswith(".fs2-observability.svc:9090")
    assert env["FS2_ADMIN_OBSERVABILITY_CONFIG_FILE"].endswith("/config.json")
    assert volume == {
        "name": "admin-observability",
        "configMap": {
            "name": "fs2-serve-control-plane-admin-observability",
            "items": [{"key": "config.json", "path": "config.json"}],
        },
    }
    config_map = next(
        item
        for item in documents
        if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "fs2-serve-control-plane-admin-observability"
    )
    config = json.loads(config_map["data"]["config.json"])
    assert config == {
        "allowed_hosts": ["observe.example.invalid"],
        "datasource_uids": {},
        "installed": {"alertmanager": False, "tempo": False},
        "links": {"grafana": "https://observe.example.invalid/observability/grafana/"},
    }
    policy = next(
        item for item in documents if item["kind"] == "NetworkPolicy" and item["metadata"]["name"].endswith("-runtime")
    )
    prometheus_egress = next(
        rule for rule in policy["spec"]["egress"] if rule["ports"] == [{"port": 9090, "protocol": "TCP"}]
    )
    assert prometheus_egress["to"] == [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "fs2-observability"}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus"}},
        }
    ]


def test_observability_adapter_accepts_installed_tempo_from_workloads_values_merge() -> None:
    documents = render(
        "-f",
        str(WORKLOAD_VALUES),
        "--set",
        "adminReadAdapters.observability.enabled=true",
        "--set",
        "adminReadAdapters.observability.installed.tempo=true",
        "--set-string",
        "adminReadAdapters.observability.datasourceUids.tempo=fs2-r0123456789-tempo",
    )
    config_map = next(
        item
        for item in documents
        if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "fs2-serve-control-plane-admin-observability"
    )
    config = json.loads(config_map["data"]["config.json"])
    assert config["installed"] == {"alertmanager": False, "tempo": True}
    assert config["datasource_uids"] == {"tempo": "fs2-r0123456789-tempo"}


def test_observability_adapter_accepts_installed_alertmanager_with_verified_grafana_route() -> None:
    documents = render(
        "-f",
        str(WORKLOAD_VALUES),
        "--set",
        "adminReadAdapters.observability.enabled=true",
        "--set",
        "adminReadAdapters.observability.installed.alertmanager=true",
        "--set-string",
        "adminReadAdapters.observability.datasourceUids.alertmanager=fs2-r0123456789-alertmanager",
        "--set-string",
        "adminReadAdapters.observability.links.allowedHosts[0]=observe.example.invalid",
        "--set-string",
        "adminReadAdapters.observability.links.alertmanager.url=https://observe.example.invalid/admin/observability/grafana/alerting/silences",
        "--set",
        "adminReadAdapters.observability.links.alertmanager.verifiedExternalRoute=true",
    )
    config_map = next(
        item
        for item in documents
        if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "fs2-serve-control-plane-admin-observability"
    )
    config = json.loads(config_map["data"]["config.json"])
    assert config["installed"]["alertmanager"] is True
    assert config["datasource_uids"]["alertmanager"] == "fs2-r0123456789-alertmanager"
    assert config["links"]["alertmanager"].endswith("/grafana/alerting/silences")


def test_observability_link_requires_verified_allowlisted_https_route() -> None:
    cases = (
        (
            "--set-string",
            "adminReadAdapters.observability.links.grafana.url=https://observe.example.invalid/observability/grafana/",
            "verifiedExternalRoute",
        ),
        (
            "--set",
            "adminReadAdapters.observability.links.grafana.verifiedExternalRoute=true",
            "requires a URL",
        ),
    )
    for flag, value, expected in cases:
        result = subprocess.run(  # noqa: S603 - fixed Helm binary and bounded adversarial values
            render_command(
                "--set",
                "adminReadAdapters.observability.enabled=true",
                flag,
                value,
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert expected in result.stderr


def _deployment(documents: list[dict]) -> dict:
    return next(item for item in documents if item["kind"] == "Deployment")


def test_scientific_artifact_routes_are_absent_until_object_storage_is_configured() -> None:
    deployment = _deployment(render())
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    names = {item["name"] for item in container["env"]}
    assert not [name for name in names if name.startswith("FS2_ARTIFACT_")]
    assert "FS2_SCIENTIFIC_ARTIFACTS_ENABLED" not in names
    mounts = {item["name"] for item in container["volumeMounts"]}
    assert "artifact-store" not in mounts
    volumes = {item["name"] for item in deployment["spec"]["template"]["spec"]["volumes"]}
    assert "artifact-store" not in volumes


def test_enabled_scientific_artifacts_render_settings_the_runtime_accepts() -> None:
    documents = render(
        "--set",
        "scientificArtifacts.enabled=true",
        "--set",
        "scientificArtifacts.egressCidrs[0]=203.0.113.0/24",
    )
    deployment = _deployment(documents)
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    environment = {item["name"]: item.get("value") for item in container["env"]}

    # Every artifact value must survive Helm's float64 number handling.
    assert environment["FS2_ARTIFACT_MAX_BYTES"] == "1099511627776"
    assert environment["FS2_ARTIFACT_RETENTION_SECONDS"] == "7776000"
    assert environment["FS2_ARTIFACT_HANDLE_TTL_SECONDS"] == "600"
    assert "e+" not in "".join(value or "" for value in environment.values())

    # Credentials arrive as a read-only projected file, never as an env value.
    assert "FS2_ARTIFACT_STORE_ACCESS_KEY" not in environment
    assert "FS2_ARTIFACT_STORE_SECRET_KEY" not in environment
    assert environment["FS2_ARTIFACT_STORE_CREDENTIALS_FILE"] == (
        "/var/run/secrets/fs2-serve/artifact-store/credentials.json"
    )
    volume = next(item for item in pod["volumes"] if item["name"] == "artifact-store")
    assert volume["secret"]["defaultMode"] == 0o400
    mount = next(item for item in container["volumeMounts"] if item["name"] == "artifact-store")
    assert mount["readOnly"] is True
    assert mount["mountPath"] == "/var/run/secrets/fs2-serve/artifact-store"

    # The rendered environment must construct the real Settings object.
    from fs2_serve.settings import Settings

    settings = Settings(
        **{
            key.removeprefix("FS2_").lower(): value
            for key, value in environment.items()
            if key.startswith("FS2_ARTIFACT") or key == "FS2_SCIENTIFIC_ARTIFACTS_ENABLED"
        }
    )
    assert settings.scientific_artifacts_enabled is True
    assert settings.artifact_max_bytes == 1099511627776
    assert "chemical/x-pdb" in settings.artifact_media_types_set()


def test_object_storage_egress_is_opt_in_and_scoped_to_tls() -> None:
    without = render("--set", "scientificArtifacts.enabled=true")
    policies = [item for item in without if item["kind"] == "NetworkPolicy"]
    assert policies, "the chart must still render its default-deny policies"
    assert "203.0.113.0/24" not in json.dumps(without)

    with_egress = render(
        "--set",
        "scientificArtifacts.enabled=true",
        "--set",
        "scientificArtifacts.egressCidrs[0]=203.0.113.0/24",
    )
    rules = [
        rule
        for item in with_egress
        if item["kind"] == "NetworkPolicy"
        for rule in item["spec"].get("egress", [])
        if any(peer.get("ipBlock", {}).get("cidr") == "203.0.113.0/24" for peer in rule.get("to", []))
    ]
    assert len(rules) == 1
    assert rules[0]["ports"] == [{"port": 443, "protocol": "TCP"}]


def test_extra_kueue_namespaces_grant_least_privilege_capacity_reads() -> None:
    """Scientific lanes need queue and pod allocation visibility only."""

    documents = render(
        "--set",
        "adminReadAdapters.capacity.enabled=true",
        "--set",
        "networkPolicy.kubernetesApiCidrs[0]=192.0.2.10/32",
        "--set",
        "adminReadAdapters.capacity.kueueExtraNamespaces={fs2-academic-poc,fs2-reference-data}",
    )
    roles = {
        document["metadata"]["namespace"]: document
        for document in documents
        if document["kind"] == "Role" and document["metadata"]["name"].endswith("-admin-capacity-reader-queues")
    }
    assert set(roles) == {"fs2-academic-poc", "fs2-reference-data"}
    for role in roles.values():
        assert role["rules"] == [
            {
                "apiGroups": [""],
                "resources": ["pods"],
                "verbs": ["list"],
            },
            {
                "apiGroups": ["kueue.x-k8s.io"],
                "resources": ["localqueues", "workloads"],
                "verbs": ["list"],
            },
        ]

    bindings = {
        document["metadata"]["namespace"]
        for document in documents
        if document["kind"] == "RoleBinding" and document["metadata"]["name"].endswith("-admin-capacity-reader-queues")
    }
    assert bindings == {"fs2-academic-poc", "fs2-reference-data"}

    deployment = gateway_deployment(documents)
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    environment = {item["name"]: item.get("value") for item in container["env"]}
    assert environment["FS2_ADMIN_KUEUE_EXTRA_NAMESPACES"] == '["fs2-academic-poc","fs2-reference-data"]'


def test_extra_kueue_namespace_must_not_repeat_the_model_namespace() -> None:
    result = subprocess.run(  # noqa: S603 - fixed Helm binary and test-owned arguments
        render_command(
            "--set",
            "adminReadAdapters.capacity.enabled=true",
            "--set",
            "networkPolicy.kubernetesApiCidrs[0]=192.0.2.10/32",
            "--set",
            "adminReadAdapters.capacity.kueueExtraNamespaces={fs2-models}",
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "must not repeat modelNamespace" in result.stderr

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_application_namespaces_enforce_baseline_with_restricted_reporting() -> None:
    foundation = _source("stages/foundation/locals.tf")
    for namespace in ("fs2-data", "fs2-models", "fs2-observability", "fs2-system"):
        assert re.search(rf'"{re.escape(namespace)}"\s*=\s*"baseline"', foundation)
    assert '"pod-security.kubernetes.io/audit"   = "restricted"' in foundation
    assert '"pod-security.kubernetes.io/warn"    = "restricted"' in foundation

    namespaces = _source("stages/foundation/namespaces.tf")
    assert "lookup(local.pod_security_labels, each.value, {})" in namespaces
    assert "lookup(local.pod_security_annotations, each.value, {})" in namespaces

    academic = _source("modules/academic-assets/main.tf")
    modelexpress = _source("stages/workloads/modelexpress.tf")
    for source in (academic, modelexpress):
        assert '"pod-security.kubernetes.io/enforce" = "baseline"' in source
        assert '"pod-security.kubernetes.io/audit"   = "restricted"' in source
        assert '"pod-security.kubernetes.io/warn"    = "restricted"' in source


def test_host_integrated_agents_are_isolated_in_an_annotated_exception_namespace() -> None:
    foundation = _source("stages/foundation/locals.tf")
    assert '"fs2-node-observability" = "privileged"' in foundation
    assert (
        '"security.fs2.nebius.ai/pod-security-exception" = '
        '"node-observability-host-integration"'
    ) in foundation

    releases = _source("stages/foundation/releases.tf")
    workloads = _source("stages/workloads/observability.tf")
    control_plane = _source("stages/workloads/control_plane.tf")
    assert 'namespaceOverride = kubernetes_namespace_v1.platform["fs2-node-observability"]' in releases
    assert 'namespace        = kubernetes_namespace_v1.platform["fs2-node-observability"]' in releases
    assert 'node_observability_namespace = "fs2-node-observability"' in workloads
    assert "daemonSetNamespace = local.node_observability_namespace" in control_plane


def test_reference_data_exception_is_explicit_and_dedicated() -> None:
    reference_data = _source("reference-data/terraform/main.tf")
    assert '"pod-security.kubernetes.io/enforce" = "privileged"' in reference_data
    assert '"pod-security.kubernetes.io/audit"   = "restricted"' in reference_data
    assert '"pod-security.kubernetes.io/warn"    = "restricted"' in reference_data
    assert (
        '"security.fs2.nebius.ai/pod-security-exception" = '
        '"reference-data-host-path"'
    ) in reference_data


def test_dynamic_controller_cannot_discover_or_own_node_agents_or_identities() -> None:
    renderer = _source("components/control-plane/src/fs2_serve/model_deployment.py")
    controller = _source("components/control-plane/src/fs2_serve/model_deployment_controller.py")
    workloads = _source("stages/workloads/model_controller.tf")
    control_plane = _source("stages/workloads/control_plane.tf")

    allowed_gvks = renderer.split("_ALLOWED_TEMPLATE_GVKS = frozenset(", 1)[1].split(")\n", 1)[0]
    endpoints = controller.split("RESOURCE_ENDPOINTS = {", 1)[1].split("}\n", 1)[0]
    supported_gvks = workloads.split("model_controller_supported_template_gvks = toset([", 1)[1].split("])", 1)[0]
    for source in (allowed_gvks, endpoints, supported_gvks):
        assert "ServiceAccount" not in source
        assert "DaemonSet" not in source
    assert 'if mechanism != "hostMemoryResidency"' in workloads
    assert "model_controller_network_policy_resource_names" in workloads
    assert 'toset(["v1/ServiceAccount"])' in workloads
    models = _source("stages/workloads/models.tf")
    assert 'resource "kubernetes_service_account_v1" "model_runtime"' in models
    assert 'name      = "fs2-model-runtime"' in models
    assert "automount_service_account_token = false" in models
    assert (
        "networkPolicyResourceNames          = "
        "local.model_controller_network_policy_resource_names"
    ) in control_plane

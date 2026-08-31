from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest
from conftest import CATALOG_ROOT, CONTROL_ROOT, REPO_ROOT, _canonical_fixture_builder, promote_qwen, qwen_binding
from fs2_serve_catalog.consumer import SERVING_BINDINGS_SCHEMA, contract_fixture
from fs2_serve_catalog.variant_promotions import variant_promotion_contract_fixture

from fs2_serve.registry import Registry, RegistryError


def load_registry(
    catalog: Path,
    bindings: Path,
    evidence_root: Path | None = None,
    variant_promotions: Path | None = None,
) -> Registry:
    return Registry.load(
        catalog,
        bindings,
        repo_root=REPO_ROOT,
        evidence_root=evidence_root,
        variant_promotions_file=variant_promotions,
        max_attempts=3,
        max_gpu_seconds_per_attempt=10,
        retry_base_seconds=1,
    )


def test_exact_published_consumer_fixture_and_single_catalog_authority(registry: Registry) -> None:
    expected = json.loads((CATALOG_ROOT / "contracts" / "gateway-consumer.fixture.json").read_text(encoding="utf-8"))
    assert expected == contract_fixture()
    assert expected["loader"] == "fs2_serve_catalog.consumer.load_gateway_catalog"
    variant_expected = json.loads(
        (CATALOG_ROOT / "contracts" / "model-variant-consumer.fixture.json").read_text(encoding="utf-8")
    )
    assert variant_expected == variant_promotion_contract_fixture()
    assert variant_expected["loader"] == "fs2_serve_catalog.variant_promotions.load_variant_gateway_catalog"
    assert variant_expected["static_route_authority"] is False
    assert not (CONTROL_ROOT / "contracts" / "model.schema.json").exists()
    assert registry.catalog.catalog_digest
    assert registry.catalog.routable_model_ids() == ("qwen3-8b",)


def test_registry_consumes_empty_signed_variant_overlay_without_static_route_authority(tmp_path: Path) -> None:
    catalog_root = tmp_path / "catalog"
    shutil.copytree(CATALOG_ROOT, catalog_root)
    builder = _canonical_fixture_builder()
    catalog, evidence_root, qualification, _ = builder.live_fixture(catalog_root)
    builder.secure_evidence_tree(evidence_root)
    binding = builder.binding_value(catalog, qualification)
    binding_path = tmp_path / "serving-bindings.json"
    binding_path.write_text(json.dumps(binding) + "\n", encoding="utf-8")
    promotions_path = tmp_path / "model-variant-promotions.json"
    promotions_path.write_text(
        json.dumps(
            {
                "schema": variant_promotion_contract_fixture()["promotion_overlay_schema"],
                "route_authority": "signed-live-evidence-only",
                "catalog_digest": catalog.digest,
                "attestor_policy_sha256": None,
                "promotions": {},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    loaded = Registry.load(
        catalog_root,
        binding_path,
        variant_promotions_file=promotions_path,
        repo_root=REPO_ROOT,
        evidence_root=evidence_root,
        trusted_attestors=builder.trusted_attestors,
        validation_time=builder.validation_time,
        max_attempts=2,
        max_gpu_seconds_per_attempt=5,
        retry_base_seconds=0.01,
    )
    assert loaded.catalog.routable_model_ids() == ("qwen3-8b",)
    assert all(model.variant_id is None for model in loaded.list())

    value = json.loads(promotions_path.read_text(encoding="utf-8"))
    value["route_authority"] = "static-candidate-inventory"
    promotions_path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="canonical gateway catalog validation failed"):
        Registry.load(
            catalog_root,
            binding_path,
            variant_promotions_file=promotions_path,
            repo_root=REPO_ROOT,
            evidence_root=evidence_root,
            trusted_attestors=builder.trusted_attestors,
            validation_time=builder.validation_time,
            max_attempts=2,
            max_gpu_seconds_per_attempt=5,
            retry_base_seconds=0.01,
        )


def test_registry_propagates_canonical_runtime_policy_and_protocol(registry: Registry) -> None:
    model = registry.get("qwen3-8b")
    assert model.model_revision == "b968826d9c46dd6066d109eabc6255188de91218"
    assert model.gateway.execution_mode == "http"
    assert model.gateway.gpu_class == "NVIDIA-B300-SXM6-288GB"
    assert model.gateway.gpu_allocation_count == 1
    assert model.binding.endpoints == {"openai-chat": "/v1/chat/completions"}
    assert model.binding.backend_class == "local-kubernetes"
    assert model.binding.backend_region == "us-north1"
    assert model.binding.backend_gpu_class == model.gateway.gpu_class
    assert model.binding.backend_runtime_image_digest == model.gateway.runtime_image_digest
    assert model.binding.activation.controller_leader_role_namespace == "fs2-system"
    assert model.binding.activation.controller_leader_role_name == "fs2-serve-control-plane-activation-leader"
    assert model.binding.activation.controller_target_role_namespace == "fs2-models"
    assert model.binding.activation.controller_target_role_name == "fs2-serve-control-plane-activation-targets"
    assert model.readiness_probe.path == "/health"
    assert model.activation_mechanism == "replica-scale"
    assert model.gateway.endpoints == model.binding.endpoints
    assert registry.operation_for_protocol(model, "openai-chat") == "chat"
    assert model.gateway.mcp_invocable and model.binding.mcp_enabled

    glm = registry.get("glm-5-2-fp8", require_enabled=False)
    assert glm.gateway.protocols == ("openai-chat",)
    assert glm.gateway.policy_operations == ("chat",)

    cxr = registry.get("nv-reason-cxr-3b", require_enabled=False)
    assert cxr.gateway.protocols == ("openai-chat",)
    assert cxr.gateway.policy_operations == ("analyze-image",)
    assert cxr.gateway.non_clinical is True
    assert cxr.gateway.commercial_use == "prohibited"
    assert cxr.required_scopes == frozenset({"use.nonclinical", "use.noncommercial"})

    public = json.dumps(registry.render_runtime_config())
    assert "service_origin" not in public and "activation_url" not in public
    assert model.binding.service_origin not in public
    assert not hasattr(model.binding.activation, "endpoint")
    assert model.binding.activation.intent_interface_sha256 not in public
    assert model.binding.activation.controller_leader_role_namespace == "fs2-system"
    assert model.binding.activation.controller_leader_role_name == "fs2-serve-control-plane-activation-leader"
    assert model.binding.activation.controller_target_role_namespace == "fs2-models"
    assert model.binding.activation.controller_target_role_name == "fs2-serve-control-plane-activation-targets"
    assert not hasattr(model.binding.activation, "target_resource_version")
    assert not hasattr(model.binding.activation, "target_observed_generation")
    assert model.binding.activation.controller_leader_role_name not in public
    assert model.binding.activation.controller_target_role_name not in public


def test_protocol_operation_resolution_requires_one_exact_bound_policy_operation(registry: Registry) -> None:
    model = registry.get("qwen3-8b")

    no_operation_binding = replace(model.binding, operations=())
    no_operation = replace(
        model,
        gateway=replace(model.gateway, policy_operations=(), binding=no_operation_binding),
    )
    with pytest.raises(RegistryError, match="no canonical policy operation"):
        registry.operation_for_protocol(no_operation, "openai-chat")

    ambiguous_operations = ("analyze-image", "chat")
    ambiguous_binding = replace(model.binding, operations=ambiguous_operations)
    ambiguous = replace(
        model,
        gateway=replace(model.gateway, policy_operations=ambiguous_operations, binding=ambiguous_binding),
    )
    with pytest.raises(RegistryError, match="ambiguous"):
        registry.operation_for_protocol(ambiguous, "openai-chat")

    mismatched_binding = replace(model.binding, operations=("analyze-image",))
    mismatched = replace(model, gateway=replace(model.gateway, binding=mismatched_binding))
    with pytest.raises(RegistryError, match="differs from canonical"):
        registry.operation_for_protocol(mismatched, "openai-chat")

    missing_endpoint_binding = replace(model.binding, endpoints=MappingProxyType({}))
    missing_endpoint = replace(model, gateway=replace(model.gateway, binding=missing_endpoint_binding))
    with pytest.raises(RegistryError, match="differs from canonical"):
        registry.operation_for_protocol(missing_endpoint, "openai-chat")

    with pytest.raises(RegistryError, match="no bound route"):
        registry.operation_for_protocol(model, "openai-images")


def test_binding_claim_with_zero_routable_models_fails_startup(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    shutil.copytree(CATALOG_ROOT, catalog)
    promote_qwen(catalog)
    binding = qwen_binding(catalog, enabled=False)
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps(binding) + "\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="intersection is empty"):
        load_registry(catalog, path)


def test_empty_overlay_can_start_as_explicit_nonserving_bootstrap(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    shutil.copytree(CATALOG_ROOT, catalog)
    from fs2_serve_catalog.loader import load_catalog

    base = load_catalog(catalog, repo_root=REPO_ROOT)
    path = tmp_path / "bindings.json"
    path.write_text(
        json.dumps(
            {
                "schema": SERVING_BINDINGS_SCHEMA,
                "catalog_digest": base.digest,
                "bindings": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert load_registry(catalog, path).catalog.routable_model_ids() == ()

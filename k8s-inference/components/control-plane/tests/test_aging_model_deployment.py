"""Actual aging declarations and manifests through the managed native seam."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest
import yaml
from conftest import CATALOG_ROOT, REPO_ROOT, SOLUTION_ROOT
from fs2_serve_catalog.consumer import ServingBindings, bind_gateway_catalog
from fs2_serve_catalog.loader import load_catalog
from test_cpu_model_deployment import cpu_pool
from test_model_deployment import digest, envelope, model_spec
from test_model_deployment_mutation import FakeWriter
from test_model_deployment_publication import revision, status_view

from fs2_serve.crypto import KeyedHasher, PayloadCipher
from fs2_serve.deployment_runtimes import SET_SCHEMA, bind_deployment_runtimes
from fs2_serve.dynamic_routes import bind_dynamic_publication
from fs2_serve.memory_store import MemoryStore
from fs2_serve.model_deployment import (
    InfrastructureEnvelope,
    LegacyManifestRenderer,
    LegacyTemplateBundle,
    ModelDeploymentSpec,
    ModelQualification,
    RenderContext,
    ValidationDisposition,
    canonical_digest,
    spec_digest,
    validate_model_deployment,
)
from fs2_serve.model_deployment_admin import StoreModelDeploymentRepository
from fs2_serve.model_deployment_mutation import ModelDeploymentMutationService
from fs2_serve.model_deployment_publication import assess_model_publication
from fs2_serve.native_catalog import augment_native_catalog
from fs2_serve.scientific_batch.podset_envelope import effective_pod_requests


@pytest.mark.parametrize("model_id,variant", [("phenoage", "cpu"), ("altumage", "cuda")])
def test_actual_aging_native_runtime_can_render_and_publish_without_inventing_elasticity(tmp_path, model_id, variant):
    archive = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
    catalog = augment_native_catalog(archive, CATALOG_ROOT, repo_root=REPO_ROOT)
    bindings = ServingBindings(archive.digest, MappingProxyType({}))
    gateway = bind_gateway_catalog(catalog, bindings)
    entry = json.loads((CATALOG_ROOT / "deployment-runtimes" / f"{model_id}-{variant}.json").read_text())
    selected_path = tmp_path / "selected.json"
    selected_path.write_text(json.dumps({"schema": SET_SCHEMA, "models": {model_id: entry}}))
    selected = bind_deployment_runtimes(gateway, catalog, bindings, selected_path, catalog_dir=CATALOG_ROOT)
    effective = selected.model(model_id)
    assert not effective.routable and not effective.mcp_invocable
    assert not effective.qualification["states"]["elasticity_qualified"]
    assert not effective.qualification["states"]["http_mcp_qualified"]
    assert catalog.digest == archive.digest and len(archive.records) == 16

    resources = list(yaml.safe_load_all((SOLUTION_ROOT / "models/aging/k8s" / f"{model_id}.yaml").read_text()))
    template_digest = canonical_digest(resources)
    record = entry["record"]
    gpu_count = record["resources"]["gpu"]["count"]
    if gpu_count == 0:
        pool = cpu_pool()
        placement_resources = {
            "cpuResources": {
                "cpuMillis": record["resources"]["cpu_millis"],
                "memoryBytes": record["resources"]["memory_bytes"],
            }
        }
        local_queue, cache_tier = "general-cpu", "Disabled"
    else:
        pool = envelope().pools["pool-a"].model_copy(update={"accelerator_class": record["resources"]["gpu"]["class"]})
        placement_resources = {}
        local_queue, cache_tier = "interactive", "NodeLocal"
    value = model_spec().model_dump(mode="json", by_alias=True)
    value.update(modelRef=model_id)
    value["runtime"] = {
        "profile": record["runtime"]["kind"],
        "image": record["runtime"]["image"]["reference"],
        "templateRef": {"name": f"{model_id}.legacy-v1", "digest": template_digest},
    }
    value["artifact"] = {
        "revision": record["model"]["source"]["revision"],
        "manifestDigest": "sha256:" + record["cache"]["artifact"]["manifest_digest"],
    }
    value["placement"] = {
        "poolRefs": [pool.pool_id],
        "acceleratorsPerReplica": gpu_count,
        "topologyPolicy": "SingleNode",
        **placement_resources,
    }
    value["cache"].update(tier=cache_tier)
    value["queue"]["localQueue"] = local_queue
    value["exposure"].update(openAI=False, openAIAliases=[], mcpToolName=model_id)
    spec = ModelDeploymentSpec.model_validate(value)
    qualification = ModelQualification.model_validate(
        {
            "modelRef": model_id,
            "runtimeProfile": spec.runtime.profile,
            "artifactRevisions": {spec.artifact.revision: spec.artifact.manifest_digest},
            "artifactManifestDigests": [spec.artifact.manifest_digest],
            "runtimeImages": [spec.runtime.image],
            "acceleratorClasses": [pool.accelerator_class],
            "maxAcceleratorsPerReplica": gpu_count,
            "templateDigests": [template_digest],
            "templateRefs": {f"{model_id}.legacy-v1": template_digest},
            "templateCacheTiers": {template_digest: cache_tier},
            "openAIQualified": False,
            "mcpToolName": model_id,
            "scaleToZeroQualified": False,
            **({**placement_resources, "localQueue": local_queue} if gpu_count == 0 else {}),
        }
    )
    installed = InfrastructureEnvelope(
        revision=digest("d"),
        pools={pool.pool_id: pool},
        qualifications={model_id: qualification},
        local_queues=[local_queue],
        priority_classes=["standard"],
        tenant_ids=["tenant-a"],
    )
    bundle = LegacyTemplateBundle(
        model_ref=model_id,
        runtime_profile=spec.runtime.profile,
        template_digest=template_digest,
        primary_workload_name=model_id,
        runtime_container_name="runtime",
        primary_service_name=model_id,
        primary_service_port=8000,
        resources=resources,
    )
    renderer = LegacyManifestRenderer({(model_id, template_digest): bundle})
    service = ModelDeploymentMutationService(
        repository=StoreModelDeploymentRepository(
            MemoryStore(
                PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
                KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
            )
        ),
        writer=FakeWriter(),
        envelope=installed,
        renderer=renderer,
        prometheus_server_address="http://prometheus:9090",
    )
    option = service.configuration_options()[0]
    assert option.default_spec.availability.min_replicas == 1
    assert not option.scale_to_zero_qualified and option.scale_to_zero_warning
    assert option.gpu_snapshot_choices == []
    decision = validate_model_deployment(spec, installed)
    assert decision.disposition is ValidationDisposition.ACCEPTED
    assert [(item.code, item.severity.value) for item in decision.issues] == [("scale_to_zero_unqualified", "warning")]
    context = RenderContext(
        name=f"{model_id}-acceptance",
        namespace="fs2-models",
        uid="model-uid",
        generation=1,
        pool=pool,
        eligible_pools=[pool],
        prometheus_server_address="http://prometheus:9090",
    )
    plan = renderer.render(spec, context)
    deployment = next(item.manifest for item in plan.resources if item.kind == "Deployment")
    actual = effective_pod_requests(deployment["spec"]["template"]["spec"])
    assert actual.cpu_millis == record["resources"]["cpu_millis"]
    assert actual.memory_bytes == record["resources"]["memory_bytes"]
    assert sum(count for _, count in actual.accelerators) == gpu_count
    assert any(item.kind == "ScaledObject" for item in plan.resources)
    item = revision(name=context.name).model_copy(update={"spec": spec, "etag": spec_digest(spec)})
    publication = assess_model_publication(item, status_view(item)).publication
    assert publication is not None
    routed = bind_dynamic_publication(effective, publication, valid_until=datetime.now(UTC) + timedelta(minutes=5))
    assert routed.routable and routed.mcp_invocable
    assert routed.gpu_allocation_count == gpu_count
    assert routed.endpoints["native"] == "/v1/predict"
    # These are synthetic controller observations for integration, not live
    # route/HTTP/MCP or elasticity qualification of either native record.
    assert not effective.qualification["states"]["elasticity_qualified"]

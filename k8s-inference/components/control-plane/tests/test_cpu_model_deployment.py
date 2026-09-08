"""CPU Apps share the managed serving lifecycle without synthetic GPUs."""

from __future__ import annotations

import copy

import pytest
import yaml
from pydantic import ValidationError
from test_model_deployment import CRD, digest, envelope, model_spec
from test_model_deployment_controller import FakeApi, ZeroActiveOperations, fence, model_object
from test_model_deployment_mutation import FakeWriter
from test_model_deployment_publication import revision, status_view

from fs2_serve.crypto import KeyedHasher, PayloadCipher
from fs2_serve.fast_start_mechanisms import FastStartMechanism
from fs2_serve.memory_store import MemoryStore
from fs2_serve.model_deployment import (
    CpuResources,
    InfrastructureEnvelope,
    LegacyManifestRenderer,
    LegacyTemplateBundle,
    ModelDeploymentSpec,
    ModelQualification,
    PlacementSpec,
    PoolEnvelope,
    RenderContext,
    ValidationDisposition,
    ValidationSeverity,
    canonical_digest,
    pool_replica_capacity,
    pool_replicas_per_node,
    spec_digest,
    validate_model_deployment,
)
from fs2_serve.model_deployment_admin import StoreModelDeploymentRepository
from fs2_serve.model_deployment_controller import ModelDeploymentController, ModelKey
from fs2_serve.model_deployment_mutation import ModelDeploymentMutationService
from fs2_serve.model_deployment_publication import PublicationDisposition, assess_model_publication
from fs2_serve.model_deployment_records import ModelDeploymentRuntimePhase
from fs2_serve.scientific_batch.podset_envelope import effective_pod_requests

MIB = 1024**2
PROMETHEUS = "http://prometheus.fs2-observability.svc:9090"


def cpu_spec() -> ModelDeploymentSpec:
    value = model_spec().model_dump(mode="json", by_alias=True)
    value.update(modelRef="clinical-phenoage")
    value["runtime"].update(profile="native", templateRef={"name": "clinical-phenoage.v1", "digest": digest("c")})
    value["placement"] = {
        "poolRefs": ["batch-cpu"],
        "acceleratorsPerReplica": 0,
        "topologyPolicy": "SingleNode",
        "cpuResources": {"cpuMillis": 1000, "memoryBytes": 512 * MIB},
    }
    value["cache"].update(tier="Disabled")
    value["queue"]["localQueue"] = "general-cpu"
    value["exposure"].update(openAI=False, openAIAliases=[], mcpToolName="clinical_phenoage")
    return ModelDeploymentSpec.model_validate(value)


def cpu_pool() -> PoolEnvelope:
    return PoolEnvelope.model_validate(
        {
            "poolId": "batch-cpu",
            "acceleratorClass": "CPU",
            "acceleratorsPerNode": 0,
            "resourceName": "cpu",
            "capacityType": "regular",
            "allocatableCpuMillis": 7000,
            "allocatableMemoryBytes": 28672 * MIB,
            "minNodes": 1,
            "maxNodes": 2,
            "nodeSelector": {"capacity.fs2.nebius/pool-id": "batch-cpu"},
            "tolerations": [
                {"key": "workload.fs2.nebius/general-cpu", "operator": "Equal", "value": "true", "effect": "NoSchedule"}
            ],
        }
    )


def cpu_envelope() -> InfrastructureEnvelope:
    spec = cpu_spec()
    qualification = envelope().qualifications["qwen.3-8b"].model_dump(mode="json", by_alias=True)
    qualification.update(
        modelRef=spec.model_ref,
        runtimeProfile="native",
        acceleratorClasses=["CPU"],
        maxAcceleratorsPerReplica=0,
        cpuResources=spec.placement.cpu_resources.model_dump(by_alias=True),
        localQueue="general-cpu",
        templateRefs={"clinical-phenoage.v1": digest("c")},
        templateCacheTiers={digest("c"): "Disabled"},
        openAIQualified=False,
        mcpToolName="clinical_phenoage",
    )
    return InfrastructureEnvelope(
        revision=digest("d"),
        pools={"batch-cpu": cpu_pool()},
        qualifications={spec.model_ref: ModelQualification.model_validate(qualification)},
        local_queues=["general-cpu", "interactive"],
        priority_classes=["standard"],
        tenant_ids=["tenant-a"],
        max_accelerators_per_model=0,
    )


def cpu_renderer(pod_spec=None) -> LegacyManifestRenderer:
    pod = pod_spec or {
        "containers": [
            {
                "name": "runtime",
                "image": f"old.example/runtime@{digest('e')}",
                "resources": {"requests": {"cpu": "1", "memory": "512Mi"}, "limits": {"cpu": "2", "memory": "1Gi"}},
            }
        ]
    }
    bundle = LegacyTemplateBundle(
        model_ref="clinical-phenoage",
        runtime_profile="native",
        template_digest=digest("c"),
        primary_workload_name="clinical-phenoage",
        runtime_container_name="runtime",
        primary_service_name="clinical-phenoage",
        primary_service_port=8000,
        resources=[
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "clinical-phenoage", "namespace": "fs2-models"},
                "spec": {
                    "selector": {"matchLabels": {"app": "clinical-phenoage"}},
                    "template": {"metadata": {"labels": {"app": "clinical-phenoage"}}, "spec": copy.deepcopy(pod)},
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "clinical-phenoage", "namespace": "fs2-models"},
                "spec": {"selector": {"app": "clinical-phenoage"}, "ports": [{"port": 8000}]},
            },
        ],
    )
    return LegacyManifestRenderer({(bundle.model_ref, bundle.template_digest): bundle})


def cpu_context() -> RenderContext:
    return RenderContext(
        name="clinical-phenoage-live",
        namespace="fs2-models",
        uid="cpu-model-uid",
        generation=1,
        pool=cpu_pool(),
        eligible_pools=[cpu_pool()],
        prometheus_server_address=PROMETHEUS,
    )


def test_new_cpu_optionals_preserve_exact_legacy_gpu_digests():
    assert spec_digest(model_spec()) == "sha256:092bab27467b2a92ccfba642ba13cbd2896bdbde3e85080ebf687d105987f000"
    assert canonical_digest(envelope().model_dump(mode="json", by_alias=True)) == (
        "sha256:27e7554358ce022697f1b35df8c39a8e46586a9232f06bcfb2374067bb9e407d"
    )
    assert "cpuResources" not in model_spec().model_dump(by_alias=True)["placement"]
    old_qualification = envelope().qualifications["qwen.3-8b"].model_dump(by_alias=True)
    assert "cpuResources" not in old_qualification and "localQueue" not in old_qualification


@pytest.mark.parametrize("field,value", [("cpuMillis", 0), ("memoryBytes", 0)])
def test_cpu_resources_must_be_positive(field, value):
    resources = {"cpuMillis": 1000, "memoryBytes": 512 * MIB, field: value}
    with pytest.raises(ValidationError):
        CpuResources.model_validate(resources)


def test_zero_accelerators_and_cpu_resources_are_an_explicit_pair():
    for placement in (
        {"poolRefs": ["batch-cpu"], "acceleratorsPerReplica": 0},
        {"poolRefs": ["pool-a"], "acceleratorsPerReplica": 1, "cpuResources": {"cpuMillis": 1000, "memoryBytes": MIB}},
    ):
        with pytest.raises(ValidationError, match="zero-accelerator"):
            PlacementSpec.model_validate({**placement, "topologyPolicy": "SingleNode"})


def test_cpu_capacity_is_minimum_full_pod_cpu_and_memory_fit_times_nodes():
    pool = cpu_pool()
    assert pool_replicas_per_node(pool, 0, CpuResources(cpu_millis=2000, memory_bytes=MIB)) == 3
    assert pool_replica_capacity(pool, 0, CpuResources(cpu_millis=2000, memory_bytes=MIB)) == 6
    assert pool_replicas_per_node(pool, 0, CpuResources(cpu_millis=1000, memory_bytes=16384 * MIB)) == 1
    assert pool_replica_capacity(pool, 0, CpuResources(cpu_millis=8000, memory_bytes=MIB)) == 0
    assert pool_replica_capacity(pool, 1) == 0
    assert pool_replica_capacity(envelope().pools["pool-a"], 0, cpu_spec().placement.cpu_resources) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"resourceName": "nvidia.com/gpu"},
        {"acceleratorsPerNode": 1},
        {"allocatableCpuMillis": None},
        {"allocatableMemoryBytes": None},
        {"nodeSelector": {"accelerator.fs2.nebius/pool-id": "batch-cpu"}},
    ],
)
def test_cpu_pool_requires_real_resource_envelope_and_exact_identity(change):
    with pytest.raises(ValidationError):
        PoolEnvelope.model_validate({**cpu_pool().model_dump(by_alias=True), **change})


def test_cpu_qualification_must_choose_an_installed_cpu_queue():
    qualified = cpu_envelope().qualifications["clinical-phenoage"].model_dump(by_alias=True)
    qualified.pop("localQueue")
    with pytest.raises(ValidationError, match="localQueue"):
        ModelQualification.model_validate(qualified)
    installed = cpu_envelope().model_dump(by_alias=True)
    installed["localQueues"] = ["interactive"]
    with pytest.raises(ValidationError, match="localQueue"):
        InfrastructureEnvelope.model_validate(installed)


def test_cpu_envelope_requires_exact_queue_and_resource_qualification():
    spec, installed = cpu_spec(), cpu_envelope()
    assert validate_model_deployment(spec, installed).disposition is ValidationDisposition.ACCEPTED
    assert validate_model_deployment(spec, installed).fast_start_mechanism.mechanism is FastStartMechanism.CONVENTIONAL
    for section, field, value, expected in (
        ("queue", "localQueue", "interactive", "local_queue_unqualified"),
        ("placement", "cpuResources", {"cpuMillis": 2000, "memoryBytes": 512 * MIB}, "cpu_resources_unqualified"),
    ):
        payload = spec.model_dump(by_alias=True)
        payload[section][field] = value
        outcome = validate_model_deployment(ModelDeploymentSpec.model_validate(payload), installed)
        assert expected in {issue.code for issue in outcome.issues}
    oversized = spec.model_copy(update={"availability": spec.availability.model_copy(update={"max_replicas": 15})})
    outcome = validate_model_deployment(oversized, installed)
    assert "pool_capacity_infrastructure_required" in {issue.code for issue in outcome.issues}
    assert "cpu_pools.batch-cpu.max_nodes" in outcome.terraform_inputs


def test_cpu_does_not_offer_gpu_snapshot_or_residency_settings():
    payload = cpu_spec().model_dump(by_alias=True)
    for mechanism in ("gpu-resident", "host-memory-residency", "regional-cache"):
        payload["cache"]["mechanism"] = mechanism
        with pytest.raises(ValidationError, match="CPU|not selectable"):
            ModelDeploymentSpec.model_validate(payload)
    qualification = cpu_envelope().qualifications["clinical-phenoage"].model_dump(by_alias=True)
    qualification["snapshotDigests"] = [digest("a")]
    with pytest.raises(ValidationError, match="CPU"):
        ModelQualification.model_validate(qualification)


def test_cpu_renderer_preserves_real_resources_and_existing_keda_lifecycle():
    plan = cpu_renderer().render(cpu_spec(), cpu_context())
    deployment = next(item.manifest for item in plan.resources if item.kind == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["nodeSelector"]["capacity.fs2.nebius/pool-id"] == "batch-cpu"
    assert pod["tolerations"] == cpu_pool().tolerations
    assert pod["containers"][0]["resources"] == {
        "requests": {"cpu": "1", "memory": "512Mi"},
        "limits": {"cpu": "2", "memory": "1Gi"},
    }
    assert effective_pod_requests(pod).accelerators == ()
    scaled = next(item.manifest for item in plan.resources if item.kind == "ScaledObject")
    assert scaled["spec"]["minReplicaCount"] == 0
    assert scaled["spec"]["maxReplicaCount"] == 4
    assert any("fs2_serve" in trigger["metadata"]["query"] for trigger in scaled["spec"]["triggers"])
    hot_spec = cpu_spec().model_copy(
        update={"availability": cpu_spec().availability.model_copy(update={"min_replicas": 4})}
    )
    hot_plan = cpu_renderer().render(hot_spec, cpu_context())
    assert not any(item.kind == "ScaledObject" for item in hot_plan.resources)
    assert next(item.manifest for item in hot_plan.resources if item.kind == "Deployment")["spec"]["replicas"] == 4


@pytest.mark.parametrize(
    "requests,reason",
    [
        ({"cpu": "2", "memory": "512Mi"}, "full Pod requests"),
        ({"cpu": "1", "memory": "1Gi"}, "full Pod requests"),
        ({"cpu": "1", "memory": "512Mi", "nvidia.com/gpu": "1"}, "accelerator resources"),
    ],
)
def test_cpu_renderer_rejects_template_request_mismatch_before_cleanup(requests, reason):
    pod = {"containers": [{"name": "runtime", "image": f"image@{digest('b')}", "resources": {"requests": requests}}]}
    with pytest.raises(ValueError, match=reason):
        cpu_renderer(pod).render(cpu_spec(), cpu_context())


def test_cpu_full_pod_math_includes_native_sidecars_init_limits_and_overhead():
    pod = {
        "containers": [{"name": "runtime", "image": "image", "resources": {"limits": {"cpu": "1", "memory": "512Mi"}}}],
        "initContainers": [
            {
                "name": "sidecar",
                "restartPolicy": "Always",
                "resources": {"requests": {"cpu": "200m", "memory": "64Mi"}},
            },
            {"name": "init", "resources": {"requests": {"cpu": "2", "memory": "1Gi"}}},
        ],
        "overhead": {"cpu": "50m", "memory": "32Mi"},
    }
    actual = effective_pod_requests(pod)
    assert (actual.cpu_millis, actual.memory_bytes, actual.accelerators) == (2250, 1120 * MIB, ())
    spec = cpu_spec()
    spec = spec.model_copy(
        update={
            "placement": spec.placement.model_copy(
                update={"cpu_resources": CpuResources(cpu_millis=actual.cpu_millis, memory_bytes=actual.memory_bytes)}
            )
        }
    )
    assert cpu_renderer(pod).render(spec, cpu_context()).resources


def test_cpu_configuration_options_use_cpu_headroom_queue_and_native_exposure():
    service = ModelDeploymentMutationService(
        repository=StoreModelDeploymentRepository(
            MemoryStore(
                PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
                KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
            )
        ),
        writer=FakeWriter(),
        envelope=cpu_envelope(),
        renderer=cpu_renderer(),
        prometheus_server_address=PROMETHEUS,
    )
    options = service.configuration_options()
    assert len(options) == 1
    option = options[0]
    assert option.local_queue_choices == ["general-cpu"]
    assert option.default_spec.queue.local_queue == "general-cpu"
    assert option.default_spec.placement.accelerators_per_replica == 0
    assert option.default_spec.placement.cpu_resources == cpu_spec().placement.cpu_resources
    assert option.pool_choices[0].maximum_replicas == 14
    assert option.gpu_snapshot_choices == []
    assert [item.mechanism for item in option.fast_start_mechanism_choices] == [FastStartMechanism.CONVENTIONAL]


def test_unmeasured_cpu_still_defaults_hot_but_operator_can_test_zero_without_faking_qualification():
    installed = cpu_envelope()
    qualification = installed.qualifications["clinical-phenoage"].model_copy(update={"scale_to_zero_qualified": False})
    installed = installed.model_copy(update={"qualifications": {qualification.model_ref: qualification}})
    service = ModelDeploymentMutationService(
        repository=StoreModelDeploymentRepository(
            MemoryStore(
                PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
                KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
            )
        ),
        writer=FakeWriter(),
        envelope=installed,
        renderer=cpu_renderer(),
        prometheus_server_address=PROMETHEUS,
    )
    option = service.configuration_options()[0]
    assert not option.scale_to_zero_qualified
    assert option.default_spec.availability.min_replicas == 1
    assert "not yet been benchmark-qualified" in option.scale_to_zero_warning
    assert option.gpu_snapshot_choices == []
    assert [item.mechanism for item in option.fast_start_mechanism_choices] == [FastStartMechanism.CONVENTIONAL]

    explicit_zero = cpu_spec()
    decision = validate_model_deployment(explicit_zero, installed)
    assert decision.disposition is ValidationDisposition.ACCEPTED
    assert [(item.code, item.severity) for item in decision.issues] == [
        ("scale_to_zero_unqualified", ValidationSeverity.WARNING)
    ]
    assert not installed.qualifications["clinical-phenoage"].scale_to_zero_qualified
    assert not explicit_zero.cache.snapshot_ref
    assert validate_model_deployment(option.default_spec, installed).issues == []

    wrong_artifact = explicit_zero.model_copy(
        update={"artifact": explicit_zero.artifact.model_copy(update={"manifest_digest": digest("f")})}
    )
    assert validate_model_deployment(wrong_artifact, installed).disposition is ValidationDisposition.REJECTED
    unknown_pool = explicit_zero.model_copy(
        update={"placement": explicit_zero.placement.model_copy(update={"pool_refs": ["absent-cpu"]})}
    )
    assert (
        validate_model_deployment(unknown_pool, installed).disposition is ValidationDisposition.INFRASTRUCTURE_REQUIRED
    )


@pytest.mark.parametrize("phase", [ModelDeploymentRuntimePhase.READY, ModelDeploymentRuntimePhase.COLD])
def test_cpu_route_publication_preserves_zero_allocation_and_existing_status_gate(phase):
    spec = cpu_spec()
    item = revision().model_copy(update={"spec": spec, "etag": spec_digest(spec)})
    result = assess_model_publication(item, status_view(item, phase=phase))
    assert result.disposition is PublicationDisposition.PUBLISH
    assert result.publication.accelerators_per_replica == 0
    assert result.publication.runtime_ready == (phase is ModelDeploymentRuntimePhase.READY)
    assert assess_model_publication(item, None).publication is None


@pytest.mark.asyncio
async def test_existing_controller_bootstraps_cpu_at_zero_and_hands_off_to_keda():
    model = model_object()
    model["spec"] = cpu_spec().model_dump(mode="json", by_alias=True)
    api = FakeApi(model)
    controller = ModelDeploymentController(
        api=api,
        envelope=cpu_envelope(),
        renderer=cpu_renderer(),
        namespace="fs2-models",
        holder_identity="fs2-system/controller:pod-uid",
        prometheus_server_address=PROMETHEUS,
        writes_enabled=True,
        active_operations=ZeroActiveOperations(),
        queue_capacity=2,
        worker_count=1,
        poll_seconds=0.01,
    )
    key = ModelKey(namespace="fs2-models", name="qwen-live")
    assert (await controller.reconcile(key, fence())).action == "finalizer-added"
    assert (await controller.reconcile(key, fence())).action == "autoscaler-bootstrap"
    deployment = next(item for item in api.resources.values() if item.observed.kind == "Deployment")
    assert deployment.raw["spec"]["replicas"] == 0
    assert not effective_pod_requests(deployment.raw["spec"]["template"]["spec"]).accelerators
    assert (await controller.reconcile(key, fence())).action == "autoscaler-handoff"
    assert api.status_writes[-1]["phase"] == "Ready"
    assert api.status_writes[-1]["admittedPoolRef"] == "batch-cpu"
    assert api.status_writes[-1]["endpoint"]["serviceName"] == "clinical-phenoage"


def test_crd_explicit_cpu_contract_does_not_inject_gpu_defaults():
    crd = yaml.safe_load(CRD.read_text())
    placement = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["placement"]
    assert placement["properties"]["acceleratorsPerReplica"]["minimum"] == 0
    assert "default" not in placement["properties"]["cpuResources"]
    assert placement["properties"]["cpuResources"]["required"] == ["cpuMillis", "memoryBytes"]
    assert "has(self.cpuResources)" in placement["x-kubernetes-validations"][0]["rule"]

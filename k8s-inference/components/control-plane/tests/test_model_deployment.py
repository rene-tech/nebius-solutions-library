from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from fs2_serve.access_models import OperatorPrincipal, OperatorRole, PrincipalKind
from fs2_serve.api import create_app
from fs2_serve.model_deployment import (
    FIELD_MANAGER,
    AdoptionMode,
    AdoptionResourceEvidence,
    AdoptionSpec,
    AdoptionVerification,
    ArtifactSpec,
    AvailabilitySpec,
    CacheSpec,
    CacheTier,
    DesiredState,
    DrainObservation,
    ExposureSpec,
    InfrastructureEnvelope,
    LegacyManifestRenderer,
    LegacyTemplateBundle,
    LifecycleSpec,
    ModelDeploymentSpec,
    ModelQualification,
    NamedDigest,
    ObservedResource,
    PlacementSpec,
    PoolEnvelope,
    QueueSpec,
    ReconcileAction,
    RenderContext,
    RolloutSpec,
    RolloutStrategy,
    RuntimeSpec,
    SnapshotPreference,
    TenantPolicySpec,
    TopologyPolicy,
    ValidationDisposition,
    Visibility,
    plan_reconciliation,
    spec_digest,
    validate_model_deployment,
)
from fs2_serve.model_deployment_preview import (
    InMemoryModelDeploymentPreviewAudit,
    InMemoryModelDeploymentPreviewState,
    ModelDeploymentCurrent,
    ModelDeploymentPreviewProblemError,
    ModelDeploymentPreviewProposal,
    ModelDeploymentPreviewService,
)

CONTROL_ROOT = Path(__file__).resolve().parents[1]
SOLUTION_ROOT = CONTROL_ROOT.parents[1]
CRD = (
    SOLUTION_ROOT
    / "charts/control-plane/fs2-serve-control-plane/crds/modeldeployments.inference.fs2.nebius.ai.yaml"
)
WORKLOADS = SOLUTION_ROOT / "stages/workloads"


def digest(character: str) -> str:
    return f"sha256:{character * 64}"


def model_spec(
    *,
    desired_state: DesiredState = DesiredState.ENABLED,
    adoption: AdoptionSpec | None = None,
) -> ModelDeploymentSpec:
    return ModelDeploymentSpec(
        model_ref="qwen.3-8b",
        tenant_id="tenant-a",
        lifecycle=LifecycleSpec(desired_state=desired_state),
        artifact=ArtifactSpec(revision="revision-1", manifest_digest=digest("a")),
        runtime=RuntimeSpec(
            profile="vllm",
            image=f"registry.example/fs2/vllm@{digest('b')}",
            template_ref=NamedDigest(name="qwen-template.v1", digest=digest("c")),
        ),
        placement=PlacementSpec(
            pool_refs=["pool-b", "pool-a"],
            accelerators_per_replica=1,
            topology_policy=TopologyPolicy.SINGLE_NODE,
        ),
        availability=AvailabilitySpec(
            min_replicas=0,
            max_replicas=4,
            idle_seconds=900,
            target_queue_depth=1,
            polling_interval_seconds=5,
            cooldown_seconds=300,
        ),
        cache=CacheSpec(tier=CacheTier.NODE_LOCAL, snapshot_preference=SnapshotPreference.NEVER),
        queue=QueueSpec(local_queue="interactive", priority_class="standard", max_queue_seconds=7200),
        rollout=RolloutSpec(
            strategy=RolloutStrategy.ROLLING,
            max_unavailable=0,
            max_surge=1,
            progress_deadline_seconds=7200,
        ),
        exposure=ExposureSpec(
            open_ai=True,
            open_ai_aliases=["qwen-alias-b", "qwen-alias-a"],
            mcp=True,
            mcp_tool_name="qwen_3_8b",
        ),
        policy=TenantPolicySpec(
            visibility=Visibility.TENANT,
            policy_ref="tenant-default.v1",
            allowed_principal_ids=["principal-b", "principal-a"],
        ),
        adoption=adoption or AdoptionSpec(),
    )


def envelope() -> InfrastructureEnvelope:
    pools = {
        "pool-a": PoolEnvelope(
            pool_id="pool-a",
            accelerator_class="nvidia-h100-sxm",
            resource_name="nvidia.com/gpu",
            capacity_type="preemptible",
            accelerators_per_node=8,
            min_nodes=0,
            max_nodes=1,
            node_selector={"accelerator.fs2.nebius/pool-id": "pool-a"},
        ),
        "pool-b": PoolEnvelope(
            pool_id="pool-b",
            accelerator_class="vendor-future-gpu",
            resource_name="vendor.example/gpu",
            capacity_type="preemptible",
            accelerators_per_node=4,
            min_nodes=0,
            max_nodes=4,
            node_selector={"accelerator.fs2.nebius/pool-id": "pool-b"},
            tolerations=[{"key": "dedicated", "operator": "Equal", "value": "inference"}],
        ),
    }
    qualification = ModelQualification(
        model_ref="qwen.3-8b",
        runtime_profile="vllm",
        artifact_manifest_digests=[digest("a")],
        runtime_images=[f"registry.example/fs2/vllm@{digest('b')}"],
        accelerator_classes=["nvidia-h100-sxm", "vendor-future-gpu"],
        max_accelerators_per_replica=8,
        template_digests=[digest("c")],
    )
    return InfrastructureEnvelope(
        revision=digest("d"),
        pools=pools,
        qualifications={qualification.model_ref: qualification},
        local_queues=["interactive"],
        priority_classes=["standard"],
        tenant_ids=["tenant-a"],
        max_accelerators_per_model=128,
    )


def renderer() -> LegacyManifestRenderer:
    bundle = LegacyTemplateBundle(
        model_ref="qwen.3-8b",
        runtime_profile="vllm",
        template_digest=digest("c"),
        primary_workload_name="qwen-runtime",
        runtime_container_name="runtime",
        resources=[
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "qwen-runtime", "namespace": "fs2-models"},
                "spec": {
                    "selector": {"matchLabels": {"app": "qwen"}},
                    "template": {
                        "metadata": {"labels": {"app": "qwen"}},
                        "spec": {
                            "containers": [
                                {
                                    "name": "runtime",
                                    "image": f"old.example/runtime@{digest('e')}",
                                    "resources": {
                                        "requests": {"nvidia.com/gpu": "1"},
                                        "limits": {"nvidia.com/gpu": "1"},
                                    },
                                }
                            ]
                        },
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "qwen-runtime", "namespace": "fs2-models"},
                "spec": {"selector": {"app": "qwen"}, "ports": [{"port": 8000}]},
            },
        ],
    )
    return LegacyManifestRenderer({(bundle.model_ref, bundle.template_digest): bundle})


def render_context(*, preview: bool = False, generation: int = 1) -> RenderContext:
    return RenderContext(
        name="qwen-live",
        namespace="fs2-models",
        uid=None if preview else "cr-uid-1",
        generation=generation,
        pool=envelope().pools["pool-b"],
        prometheus_server_address="http://prometheus.fs2-observability.svc:9090",
        preview=preview,
    )


def observed_from_render(spec: ModelDeploymentSpec) -> list[ObservedResource]:
    plan = renderer().render(spec, render_context())
    return [
        ObservedResource(
            api_version=item.api_version,
            kind=item.kind,
            namespace=item.namespace,
            name=item.name,
            uid=f"uid-{index}",
            digest=item.digest,
            controller_owner_uid="cr-uid-1",
            field_managers=[FIELD_MANAGER],
        )
        for index, item in enumerate(plan.resources)
    ]


def operator(role: OperatorRole = OperatorRole.ADMIN) -> OperatorPrincipal:
    now = datetime.now(UTC)
    return OperatorPrincipal(
        id=uuid4(),
        subject="operator@example.test",
        display_name="Operator",
        kind=PrincipalKind.HUMAN,
        role=role,
        tenant_id=None,
        enabled=True,
        created_at=now,
        created_by="test",
        updated_at=now,
    )


def test_crd_is_structural_versioned_and_has_explicit_terraform_upgrade_owner() -> None:
    document = yaml.safe_load(CRD.read_text())
    assert document["metadata"]["name"] == "modeldeployments.inference.fs2.nebius.ai"
    version = document["spec"]["versions"][0]
    assert version["name"] == "v1alpha1" and version["served"] and version["storage"]
    schema = version["schema"]["openAPIV3Schema"]

    def objects(value: object) -> list[dict]:
        found: list[dict] = []
        if isinstance(value, dict):
            found.append(value)
            for child in value.values():
                found.extend(objects(child))
        elif isinstance(value, list):
            for child in value:
                found.extend(objects(child))
        return found

    assert not any(item.get("additionalProperties") is False for item in objects(schema))
    condition = schema["properties"]["status"]["properties"]["conditions"]["items"]
    assert "observedGeneration" in condition["required"]
    rules = {item["message"] for item in schema["properties"]["spec"]["x-kubernetes-validations"]}
    assert "Claim adoption cannot be downgraded" in rules
    assert "an enabled model must permit at least one replica" in rules

    terraform = (WORKLOADS / "model_deployment_api.tf").read_text()
    control_plane = (WORKLOADS / "control_plane.tf").read_text()
    assert 'resource "kubernetes_manifest" "model_deployment_crd"' in terraform
    assert 'type   = "Established"' in terraform
    assert "kubernetes_manifest.model_deployment_crd" in control_plane


def test_spec_digest_normalizes_kubernetes_set_and_map_list_semantics() -> None:
    first = model_spec()
    wire = first.model_dump(mode="json", by_alias=True)
    assert set(wire["exposure"]) >= {"openAI", "openAIAliases"}
    assert "openAi" not in wire["exposure"] and "openAiAliases" not in wire["exposure"]
    assert ModelDeploymentSpec.model_validate(wire) == first
    second = first.model_copy(
        update={
            "placement": first.placement.model_copy(update={"pool_refs": list(reversed(first.placement.pool_refs))}),
            "exposure": first.exposure.model_copy(
                update={"open_ai_aliases": list(reversed(first.exposure.open_ai_aliases))}
            ),
            "policy": first.policy.model_copy(
                update={"allowed_principal_ids": list(reversed(first.policy.allowed_principal_ids))}
            ),
        }
    )
    assert spec_digest(first) == spec_digest(second)


def test_validation_is_gpu_neutral_deterministic_and_fails_before_render() -> None:
    accepted = validate_model_deployment(model_spec(), envelope())
    assert accepted.disposition is ValidationDisposition.ACCEPTED
    assert accepted.admitted_pool_ref == "pool-b"

    unknown = model_spec().model_copy(
        update={"placement": model_spec().placement.model_copy(update={"pool_refs": ["new-pool"]})}
    )
    infrastructure = validate_model_deployment(unknown, envelope())
    assert infrastructure.disposition is ValidationDisposition.INFRASTRUCTURE_REQUIRED
    assert infrastructure.terraform_inputs == ["accelerator_pools.new-pool"]

    unqualified = model_spec().model_copy(
        update={"artifact": model_spec().artifact.model_copy(update={"manifest_digest": digest("f")})}
    )
    rejected = validate_model_deployment(unqualified, envelope())
    assert rejected.disposition is ValidationDisposition.REJECTED
    assert "artifact_digest_unqualified" in {issue.code for issue in rejected.issues}


def test_renderer_uses_selected_pool_resource_and_safe_derived_metadata() -> None:
    spec = model_spec()
    first = renderer().render(spec, render_context())
    assert first == renderer().render(spec, render_context())
    deployment = next(item.manifest for item in first.resources if item.kind == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["requests"] == {"vendor.example/gpu": "1"}
    assert container["resources"]["limits"] == {"vendor.example/gpu": "1"}
    assert deployment["metadata"]["ownerReferences"][0]["uid"] == "cr-uid-1"
    scaler = next(item.manifest for item in first.resources if item.kind == "ScaledObject")
    assert scaler["spec"]["triggers"][0]["metadata"]["metricName"] == "fs2_operation_demand_qwen_3_8b"
    assert any(item.manifest["metadata"]["name"].startswith("fs2-model-publication-") for item in first.resources)

    disabled = spec.model_copy(
        update={
            "lifecycle": LifecycleSpec(desired_state=DesiredState.DISABLED),
            "availability": spec.availability.model_copy(update={"min_replicas": 0, "warm_windows": []}),
        }
    )
    disabled_render = renderer().render(disabled, render_context())
    assert not {item.kind for item in disabled_render.resources} & {"ScaledObject"}
    assert not any("publication" in item.name for item in disabled_render.resources)
    disabled_deployment = next(item.manifest for item in disabled_render.resources if item.kind == "Deployment")
    assert disabled_deployment["spec"]["replicas"] == 0


def test_invalid_lifecycle_and_resource_names_fail_in_schema_model() -> None:
    spec = model_spec()
    with pytest.raises(ValidationError):
        ModelDeploymentSpec.model_validate(
            spec.model_dump(mode="json", by_alias=True)
            | {
                "lifecycle": {"desiredState": "Disabled"},
                "availability": spec.availability.model_dump(mode="json", by_alias=True) | {"minReplicas": 1},
            }
        )
    with pytest.raises(ValidationError):
        NamedDigest(name="invalid..name", digest=digest("a"))
    with pytest.raises(ValidationError):
        PlacementSpec(
            pool_refs=["INVALID_POOL"], accelerators_per_replica=1, topology_policy=TopologyPolicy.ANY
        )
    with pytest.raises(ValidationError):
        ExposureSpec(open_ai=True, open_ai_aliases=["invalid alias"], mcp=False)
    with pytest.raises(ValidationError):
        TenantPolicySpec(
            visibility=Visibility.TENANT,
            policy_ref="default",
            allowed_principal_ids=["invalid principal"],
        )
    with pytest.raises(ValidationError):
        RenderContext(
            name="preview",
            namespace="fs2-models",
            uid=None,
            generation=1,
            pool=envelope().pools["pool-b"],
            prometheus_server_address="http://prometheus:9090",
        )


def test_reconcile_rejects_foreign_collision_and_cleans_only_proven_owned_stale_resources() -> None:
    spec = model_spec()
    observed = observed_from_render(spec)
    collision = observed[0].model_copy(update={"controller_owner_uid": None})
    rejected = plan_reconciliation(
        generation=1,
        deleting=False,
        spec=spec,
        envelope=envelope(),
        renderer=renderer(),
        render_context=render_context(),
        observed=[collision, *observed[1:]],
        discovery_complete=True,
    )
    assert rejected.action is ReconcileAction.REJECT
    assert "foreign_resource_collision" in {issue.code for issue in rejected.validation.issues}

    stale_owned = ObservedResource(
        api_version="v1",
        kind="ConfigMap",
        namespace="fs2-models",
        name="stale-owned",
        uid="stale-owned-uid",
        digest=digest("9"),
        controller_owner_uid="cr-uid-1",
    )
    stale_foreign = stale_owned.model_copy(
        update={"name": "stale-foreign", "uid": "stale-foreign-uid", "controller_owner_uid": "other-cr"}
    )
    repair = plan_reconciliation(
        generation=1,
        deleting=False,
        spec=spec,
        envelope=envelope(),
        renderer=renderer(),
        render_context=render_context(),
        observed=[*observed, stale_owned, stale_foreign],
        discovery_complete=True,
    )
    assert repair.action is ReconcileAction.APPLY
    assert repair.delete_resource_identities == [stale_owned.identity]
    assert repair.target_generation == 1


def test_delete_is_a_drain_backstop_and_finalizer_requires_complete_empty_discovery() -> None:
    spec = model_spec()
    observed = observed_from_render(spec)
    draining = plan_reconciliation(
        generation=1,
        deleting=True,
        spec=spec,
        envelope=envelope(),
        renderer=renderer(),
        render_context=render_context(),
        observed=observed,
        discovery_complete=True,
        drain_observation=DrainObservation(
            publication_withdrawn=False,
            active_operations=1,
            observed_replicas=1,
            ready_replicas=1,
        ),
    )
    assert draining.action is ReconcileAction.DRAIN
    assert any(
        "publication" in identity or "ScaledObject" in identity
        for identity in draining.delete_resource_identities
    )
    assert not draining.remove_finalizer

    safe = DrainObservation(
        publication_withdrawn=True,
        active_operations=0,
        observed_replicas=0,
        ready_replicas=0,
    )
    deleting = plan_reconciliation(
        generation=1,
        deleting=True,
        spec=spec,
        envelope=envelope(),
        renderer=renderer(),
        render_context=render_context(),
        observed=observed,
        discovery_complete=True,
        drain_observation=safe,
    )
    assert deleting.action is ReconcileAction.DELETE and not deleting.remove_finalizer
    complete = plan_reconciliation(
        generation=1,
        deleting=True,
        spec=spec,
        envelope=envelope(),
        renderer=renderer(),
        render_context=render_context(),
        observed=[],
        discovery_complete=True,
        drain_observation=safe,
    )
    assert complete.action is ReconcileAction.NOOP and complete.remove_finalizer
    retry = plan_reconciliation(
        generation=1,
        deleting=True,
        spec=spec,
        envelope=envelope(),
        renderer=renderer(),
        render_context=render_context(),
        observed=[],
        discovery_complete=False,
        drain_observation=safe,
    )
    assert retry.action is ReconcileAction.RETRY and not retry.remove_finalizer


def test_claim_requires_exact_receipt_and_recovers_after_controller_restart() -> None:
    adoption = AdoptionSpec(
        mode=AdoptionMode.CLAIM,
        receipt_ref=NamedDigest(name="qwen-adoption.v1", digest=digest("7")),
    )
    spec = model_spec(adoption=adoption)
    preview = renderer().render(spec, render_context(preview=True))
    preclaim = [
        ObservedResource(
            api_version=item.api_version,
            kind=item.kind,
            namespace=item.namespace,
            name=item.name,
            uid=f"claim-uid-{index}",
            digest=item.digest,
            field_managers=["terraform-models"],
        )
        for index, item in enumerate(preview.resources)
    ]
    blocked = plan_reconciliation(
        generation=1,
        deleting=False,
        spec=spec,
        envelope=envelope(),
        renderer=renderer(),
        render_context=render_context(),
        observed=preclaim,
        discovery_complete=True,
    )
    assert blocked.action is ReconcileAction.REJECT

    evidence = [
        AdoptionResourceEvidence(
            identity=item.identity,
            uid=item.uid,
            digest=item.digest,
            field_managers=item.field_managers,
        )
        for item in preclaim
    ]
    verification = AdoptionVerification(
        receipt_digest=digest("7"),
        terraform_state_released=True,
        inventory_complete=True,
        pre_diff_equal=True,
        conflicts_resolved=True,
        resources=evidence,
    )
    claim = plan_reconciliation(
        generation=1,
        deleting=False,
        spec=spec,
        envelope=envelope(),
        renderer=renderer(),
        render_context=render_context(),
        observed=preclaim,
        discovery_complete=True,
        adoption_verification=verification,
    )
    assert claim.action is ReconcileAction.APPLY

    owned_render = renderer().render(spec, render_context())
    owned = [
        preclaim[index].model_copy(
            update={
                "digest": item.digest,
                "controller_owner_uid": "cr-uid-1",
                "field_managers": ["terraform-models", FIELD_MANAGER],
            }
        )
        for index, item in enumerate(owned_render.resources)
    ]
    recovered = plan_reconciliation(
        generation=1,
        deleting=False,
        spec=spec,
        envelope=envelope(),
        renderer=renderer(),
        render_context=render_context(),
        observed=owned,
        discovery_complete=True,
        adoption_verification=verification.model_copy(update={"status_owned": True}),
    )
    assert recovered.action is ReconcileAction.NOOP


@pytest.mark.asyncio
async def test_preview_is_optimistic_audited_and_has_no_writer_surface() -> None:
    audit = InMemoryModelDeploymentPreviewAudit()
    service = ModelDeploymentPreviewService(
        envelope=envelope(),
        renderer=renderer(),
        state=InMemoryModelDeploymentPreviewState(),
        prometheus_server_address="http://prometheus:9090",
        audit=audit,
    )
    proposal = ModelDeploymentPreviewProposal(name="qwen-live", spec=model_spec())
    result = await service.plan(proposal, operator(OperatorRole.OPERATOR))
    assert result.render is not None and result.mutation_supported is False
    assert result.blocked_actions == ["apply", "adopt", "delete"]
    assert not hasattr(service, "apply") and not hasattr(service, "kubernetes")
    assert audit.events[0]["detail"]["mutation_supported"] is False

    stale_state = InMemoryModelDeploymentPreviewState(
        [
            ModelDeploymentCurrent(
                name="qwen-live",
                namespace="fs2-models",
                revision=1,
                etag=digest("8"),
                spec=model_spec(),
            )
        ]
    )
    stale_service = ModelDeploymentPreviewService(
        envelope=envelope(),
        renderer=renderer(),
        state=stale_state,
        prometheus_server_address="http://prometheus:9090",
    )
    with pytest.raises(ModelDeploymentPreviewProblemError) as conflict:
        await stale_service.plan(proposal, operator(OperatorRole.OPERATOR))
    assert conflict.value.status_code == 409


def test_preview_http_routes_are_feature_gated_and_mutations_return_501(
    registry: object,
    cipher: object,
    hasher: object,
) -> None:
    from test_admin_access_api import BOOTSTRAP_AUTH, _runtime

    default_runtime = _runtime(registry, cipher, hasher)
    with TestClient(create_app(default_runtime), base_url="https://inference.test.invalid") as client:
        assert client.post("/admin/api/v1/model-deployments:plan-preview", json={}).status_code == 404

    runtime = _runtime(registry, cipher, hasher)
    runtime.model_deployment_preview = ModelDeploymentPreviewService(
        envelope=envelope(),
        renderer=renderer(),
        state=InMemoryModelDeploymentPreviewState(),
        prometheus_server_address="http://prometheus:9090",
    )
    payload = ModelDeploymentPreviewProposal(name="qwen-live", spec=model_spec()).model_dump(
        mode="json", by_alias=True
    )
    with TestClient(create_app(runtime), base_url="https://inference.test.invalid") as client:
        assert client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH).status_code == 200
        preview = client.post("/admin/api/v1/model-deployments:plan-preview", json=payload)
        assert preview.status_code == 200 and preview.json()["data"]["mutation_supported"] is False
        blocked = client.post(
            "/admin/api/v1/model-deployments:apply",
            json={
                "preview_id": preview.json()["data"]["preview_id"],
                "base_etag": None,
                "idempotency_key": "blocked-apply-1",
            },
        )
        blocked_adopt = client.post(
            "/admin/api/v1/model-deployments/qwen-live:adopt",
            json={
                "preview_id": preview.json()["data"]["preview_id"],
                "base_etag": None,
                "idempotency_key": "blocked-adopt-1",
            },
        )
        blocked_delete = client.delete("/admin/api/v1/model-deployments/qwen-live")
    assert blocked.status_code == 501
    assert blocked.json()["code"] == "model_deployment_writer_disabled"
    assert blocked_adopt.status_code == 501
    assert blocked_adopt.json()["code"] == "model_deployment_writer_disabled"
    assert blocked_delete.status_code == 501
    assert blocked_delete.json()["code"] == "model_deployment_writer_disabled"

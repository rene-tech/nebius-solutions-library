from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_model_deployment import digest, model_spec

from fs2_serve.model_deployment import DesiredState, LifecycleSpec, TenantPolicySpec, Visibility, spec_digest
from fs2_serve.model_deployment_publication import (
    DynamicModelPublication,
    IntegrationAvailability,
    IntegrationSource,
    ModelIntegrationObservation,
    ModelIntegrationQuery,
    ModelIntegrationSnapshot,
    ModelPublicationAssessment,
    PublicationDisposition,
    PublicationPrincipal,
    PublicationReason,
    assess_model_publication,
    collect_model_integrations,
    project_dynamic_model_metrics,
    project_dynamic_publications,
    publications_for_principal,
)
from fs2_serve.model_deployment_records import (
    KubernetesConditionStatus,
    ModelDeploymentCondition,
    ModelDeploymentConditionType,
    ModelDeploymentEndpointStatus,
    ModelDeploymentObservedStatus,
    ModelDeploymentRevision,
    ModelDeploymentRevisionAction,
    ModelDeploymentRuntimePhase,
    ModelDeploymentStatusAvailability,
    ModelDeploymentStatusObservation,
    ModelDeploymentStatusView,
)


def revision(
    *,
    name: str = "qwen-live",
    tenant_id: str = "tenant-a",
    desired_state: DesiredState = DesiredState.ENABLED,
    visibility: Visibility = Visibility.TENANT,
    allowed_principals: list[str] | None = None,
) -> ModelDeploymentRevision:
    now = datetime.now(UTC)
    spec = model_spec(desired_state=desired_state)
    spec = spec.model_copy(
        update={
            "tenant_id": tenant_id,
            "policy": TenantPolicySpec(
                visibility=visibility,
                policy_ref="tenant-default.v1",
                allowed_principal_ids=allowed_principals or [],
            ),
        }
    )
    return ModelDeploymentRevision(
        namespace="fs2-models",
        name=name,
        tenant_id=tenant_id,
        revision=1,
        etag=spec_digest(spec),
        spec=spec,
        action=ModelDeploymentRevisionAction.CREATE,
        created_at=now,
        created_by="operator@example.test",
    )


def status_view(
    item: ModelDeploymentRevision,
    *,
    phase: ModelDeploymentRuntimePhase = ModelDeploymentRuntimePhase.READY,
    ready_condition: bool = True,
) -> ModelDeploymentStatusView:
    now = datetime.now(UTC)
    conditions = []
    condition_type = {
        ModelDeploymentRuntimePhase.READY: ModelDeploymentConditionType.READY,
        ModelDeploymentRuntimePhase.COLD: ModelDeploymentConditionType.COLD,
        ModelDeploymentRuntimePhase.RUNTIME_STARTING: ModelDeploymentConditionType.LOADING,
        ModelDeploymentRuntimePhase.WARMING: ModelDeploymentConditionType.LOADING,
    }.get(phase, ModelDeploymentConditionType.PROGRESSING)
    if ready_condition:
        conditions.append(
            ModelDeploymentCondition(
                type=condition_type,
                status=KubernetesConditionStatus.TRUE,
                observed_generation=7,
                reason="RuntimeReady",
                message="runtime and route are observed ready",
                last_transition_time=now,
            )
        )
    observation = ModelDeploymentStatusObservation(
        observation_id=uuid4(),
        source_uid="modeldeployment-uid-1",
        source_resource_version=str(item.revision),
        namespace=item.namespace,
        name=item.name,
        tenant_id=item.tenant_id,
        revision=item.revision,
        status=ModelDeploymentObservedStatus(
            observed_generation=7,
            phase=phase,
            spec_digest=item.etag,
            admitted_pool_ref="h100-preemptible",
            endpoint=ModelDeploymentEndpointStatus(
                namespace=item.namespace,
                service_name=item.name,
                service_port=8000,
                uid="service-uid-1",
                digest=digest("e"),
            ),
            last_reconcile_time=now,
            conditions=conditions,
        ),
        observed_at=now,
    )
    return ModelDeploymentStatusView(
        namespace=item.namespace,
        name=item.name,
        revision=item.revision,
        etag=item.etag,
        state=ModelDeploymentStatusAvailability.OBSERVED,
        observation=observation,
    )


def test_only_observed_ready_current_models_are_publication_candidates() -> None:
    item = revision(allowed_principals=["principal-a"])
    status = status_view(item)
    assessment = assess_model_publication(item, status)

    assert assessment.disposition is PublicationDisposition.PUBLISH
    assert assessment.publication is not None
    assert assessment.publication.admitted_pool_ref == "h100-preemptible"
    assert assessment.publication.open_ai_aliases == sorted(item.spec.exposure.open_ai_aliases)

    assert assess_model_publication(item, None).reason is PublicationReason.STATUS_UNAVAILABLE
    warming = status_view(item, phase=ModelDeploymentRuntimePhase.WARMING)
    warming_assessment = assess_model_publication(item, warming)
    assert warming_assessment.reason is PublicationReason.ACTIVATABLE
    assert warming_assessment.publication is not None
    assert not warming_assessment.publication.runtime_ready
    no_condition = status_view(item, ready_condition=False)
    assert assess_model_publication(item, no_condition).reason is PublicationReason.READY_CONDITION_MISSING

    disabled_spec = item.spec.model_copy(
        update={
            "lifecycle": LifecycleSpec(desired_state=DesiredState.DISABLED),
            "availability": item.spec.availability.model_copy(update={"min_replicas": 0}),
        }
    )
    disabled = item.model_copy(update={"spec": disabled_spec, "etag": spec_digest(disabled_spec)})
    assert assess_model_publication(disabled, status_view(disabled)).reason is PublicationReason.DESIRED_STATE_DISABLED


def test_snapshot_withdraws_all_colliding_catalog_identities_deterministically() -> None:
    first = revision(name="qwen-a", tenant_id="tenant-a")
    second = revision(name="qwen-b", tenant_id="tenant-b")
    statuses = {
        (first.namespace, first.name): status_view(first),
        (second.namespace, second.name): status_view(second),
    }

    snapshot = project_dynamic_publications([second, first], statuses)
    replay = project_dynamic_publications([first, second], statuses)

    assert snapshot == replay
    assert snapshot.publications == []
    assert {item.reason for item in snapshot.assessments} == {PublicationReason.CATALOG_IDENTITY_CONFLICT}


def test_principal_filter_is_tenant_isolated_and_honors_private_allowlist() -> None:
    tenant_publication = revision(name="tenant-model", allowed_principals=[])
    private_publication = revision(
        name="private-model",
        visibility=Visibility.PRIVATE,
        allowed_principals=["principal-a"],
    )
    snapshot = project_dynamic_publications(
        [tenant_publication, private_publication],
        {
            (tenant_publication.namespace, tenant_publication.name): status_view(tenant_publication),
            (private_publication.namespace, private_publication.name): status_view(private_publication),
        },
    )
    # Both revisions intentionally reference the same catalog model, so prove
    # policy evaluation on independent non-colliding snapshots.
    tenant_snapshot = project_dynamic_publications(
        [tenant_publication],
        {(tenant_publication.namespace, tenant_publication.name): status_view(tenant_publication)},
    )
    private_snapshot = project_dynamic_publications(
        [private_publication],
        {(private_publication.namespace, private_publication.name): status_view(private_publication)},
    )
    assert snapshot.publications == []
    assert len(
        publications_for_principal(
            tenant_snapshot,
            PublicationPrincipal(tenant_id="tenant-a", principal_id="any-user"),
        )
    ) == 1
    assert len(
        publications_for_principal(
            private_snapshot,
            PublicationPrincipal(tenant_id="tenant-a", principal_id="principal-a"),
        )
    ) == 1
    assert publications_for_principal(
        private_snapshot,
        PublicationPrincipal(tenant_id="tenant-a", principal_id="principal-b"),
    ) == []
    assert publications_for_principal(
        tenant_snapshot,
        PublicationPrincipal(tenant_id="tenant-b", principal_id="any-user"),
    ) == []


class FakeAdapter:
    def __init__(
        self,
        source: IntegrationSource,
        observation: ModelIntegrationObservation | Exception,
    ) -> None:
        self.source = source
        self.observation = observation

    async def observe(self, query: ModelIntegrationQuery) -> ModelIntegrationObservation:
        del query
        if isinstance(self.observation, Exception):
            raise self.observation
        return self.observation


@pytest.mark.asyncio
async def test_integration_collection_is_independent_and_never_invents_unavailable_values() -> None:
    now = datetime.now(UTC)
    query = ModelIntegrationQuery(
        namespace="fs2-models",
        name="qwen-live",
        tenant_id="tenant-a",
        model_ref="qwen.3-8b",
        revision=1,
        etag=digest("a"),
    )
    autoscaler = ModelIntegrationObservation(
        source=IntegrationSource.AUTOSCALER,
        availability=IntegrationAvailability.AVAILABLE,
        namespace=query.namespace,
        name=query.name,
        revision=query.revision,
        etag=query.etag,
        observed_at=now,
        values={"desired_replicas": 3, "ready_replicas": 2},
    )
    stale_queue = autoscaler.model_copy(
        update={
            "source": IntegrationSource.QUEUE,
            "revision": 2,
            "values": {"queue_depth": 99},
        }
    )
    snapshot = await collect_model_integrations(
        query,
        {
            IntegrationSource.AUTOSCALER: FakeAdapter(IntegrationSource.AUTOSCALER, autoscaler),
            IntegrationSource.QUEUE: FakeAdapter(IntegrationSource.QUEUE, stale_queue),
            IntegrationSource.CACHE: FakeAdapter(IntegrationSource.CACHE, RuntimeError("private backend detail")),
        },
    )

    by_source = {item.source: item for item in snapshot.observations}
    assert by_source[IntegrationSource.AUTOSCALER].values == {"desired_replicas": 3, "ready_replicas": 2}
    assert by_source[IntegrationSource.QUEUE].availability is IntegrationAvailability.STALE
    assert by_source[IntegrationSource.QUEUE].values == {}
    assert by_source[IntegrationSource.CACHE].reason == "adapter-error"
    assert by_source[IntegrationSource.SNAPSHOT].reason == "adapter-not-configured"
    assert "private backend detail" not in snapshot.model_dump_json()


def test_metric_projection_is_bounded_and_omits_unavailable_resource_values() -> None:
    item = revision()
    assessment = assess_model_publication(item, status_view(item))
    query = ModelIntegrationQuery(
        namespace=item.namespace,
        name=item.name,
        tenant_id=item.tenant_id,
        model_ref=item.spec.model_ref,
        revision=item.revision,
        etag=item.etag,
    )
    now = datetime.now(UTC)
    snapshot = ModelIntegrationSnapshot(
        query=query,
        observations=[
            ModelIntegrationObservation(
                source=IntegrationSource.AUTOSCALER,
                availability=IntegrationAvailability.AVAILABLE,
                namespace=query.namespace,
                name=query.name,
                revision=query.revision,
                etag=query.etag,
                observed_at=now,
                values={"desired_replicas": 2, "ready_replicas": 1, "ignored": 999},
            ),
            ModelIntegrationObservation(
                source=IntegrationSource.QUEUE,
                availability=IntegrationAvailability.UNAVAILABLE,
                namespace=query.namespace,
                name=query.name,
                revision=query.revision,
                etag=query.etag,
                reason="adapter-not-configured",
            ),
        ],
        digest=digest("f"),
    )
    samples = project_dynamic_model_metrics(assessment, snapshot)
    by_name = {sample.name: sample.value for sample in samples}

    assert by_name["fs2_model_publication_eligible"] == 1
    assert by_name["fs2_model_desired_replicas"] == 2
    assert by_name["fs2_model_ready_replicas"] == 1
    assert "fs2_model_queue_depth" not in by_name
    assert all("tenant" not in sample.labels and "principal" not in sample.labels for sample in samples)


def test_publication_models_reject_inconsistent_payloads() -> None:
    with pytest.raises(ValueError):
        DynamicModelPublication(
            namespace="fs2-models",
            name="qwen-live",
            tenant_id="tenant-a",
            model_ref="qwen.3-8b",
            revision=1,
            etag=digest("a"),
            observed_generation=1,
            admitted_pool_ref="h100",
            runtime_profile="vllm",
            runtime_image=f"registry.example/vllm@{digest('b')}",
            artifact_revision="revision-1",
            artifact_manifest_digest=digest("c"),
            accelerators_per_replica=1,
            max_queue_seconds=7200,
            endpoint=ModelDeploymentEndpointStatus(
                namespace="fs2-models",
                service_name="qwen-live",
                service_port=8000,
                uid="service-uid-1",
                digest=digest("d"),
            ),
            runtime_ready=True,
            open_ai=False,
            open_ai_aliases=["qwen"],
            mcp=False,
            visibility=Visibility.TENANT,
            policy_ref="default",
        )

    with pytest.raises(ValueError):
        ModelPublicationAssessment(
            namespace="fs2-models",
            name="qwen-live",
            tenant_id="tenant-a",
            model_ref="qwen.3-8b",
            revision=1,
            disposition=PublicationDisposition.PUBLISH,
            reason=PublicationReason.READY,
        )

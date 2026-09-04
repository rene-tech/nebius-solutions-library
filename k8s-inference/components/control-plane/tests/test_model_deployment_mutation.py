from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from test_model_deployment import (
    envelope,
    model_spec,
    render_gpu_resident,
    render_host_memory,
    render_regional_cache,
    renderer,
    reserved_and_preemptible_envelope,
)

from fs2_serve.access_models import OperatorPrincipal, OperatorRole, PrincipalKind
from fs2_serve.crypto import KeyedHasher, PayloadCipher
from fs2_serve.fast_start import FastStartLevel, FastStartSpec
from fs2_serve.fast_start_mechanisms import FastStartMechanism
from fs2_serve.memory_store import MemoryStore
from fs2_serve.model_deployment import (
    ArtifactStorageRef,
    CacheTier,
    DesiredState,
    ValidationDisposition,
    spec_digest,
    validate_model_deployment,
)
from fs2_serve.model_deployment_admin import StoreModelDeploymentRepository
from fs2_serve.model_deployment_mutation import (
    DesiredWriteError,
    DesiredWriteReceipt,
    HttpKubernetesDesiredWriter,
    ModelDeploymentActionRequest,
    ModelDeploymentApplyRequest,
    ModelDeploymentConfigurationOption,
    ModelDeploymentMutationProblemError,
    ModelDeploymentMutationService,
    ModelDeploymentReconcileRequest,
    ModelDeploymentRollbackRequest,
)
from fs2_serve.model_deployment_preview import (
    InMemoryModelDeploymentPreviewState,
    ModelDeploymentPreviewProblemError,
    ModelDeploymentPreviewProposal,
    ModelDeploymentPreviewService,
)
from fs2_serve.model_deployment_records import (
    ModelDeploymentAdoptionState,
    ModelDeploymentAdoptionStatus,
    ModelDeploymentObservedStatus,
    ModelDeploymentReplicaStatus,
    ModelDeploymentRuntimePhase,
    ModelDeploymentStatusObservation,
)


def _actor() -> OperatorPrincipal:
    now = datetime.now(UTC)
    return OperatorPrincipal(
        id=UUID("22222222-2222-2222-2222-222222222222"),
        subject="operator@example.test",
        display_name="Operator",
        kind=PrincipalKind.HUMAN,
        role=OperatorRole.ADMIN,
        enabled=True,
        created_at=now,
        created_by="bootstrap",
        updated_at=now,
    )


class FakeWriter:
    def __init__(self) -> None:
        self.writes: list[object] = []
        self.fail = False

    async def apply(self, revision: object) -> DesiredWriteReceipt:
        self.writes.append(revision)
        if self.fail:
            raise DesiredWriteError("private failure")
        value = revision
        return DesiredWriteReceipt(
            namespace=value.namespace,
            name=value.name,
            uid="model-deployment-uid",
            resource_version=str(value.revision),
            generation=value.revision,
            spec_digest=value.etag,
        )


def _apply_request(*, key: str, proposal: ModelDeploymentPreviewProposal) -> ModelDeploymentApplyRequest:
    return ModelDeploymentApplyRequest(
        preview_id=uuid4(),
        proposed_etag=spec_digest(proposal.spec),
        proposal=proposal,
        idempotency_key=key,
    )


def test_configuration_options_are_exact_valid_installed_defaults() -> None:
    installed = envelope()
    service = ModelDeploymentMutationService(
        repository=StoreModelDeploymentRepository(
            MemoryStore(
                PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
                KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
            )
        ),
        writer=FakeWriter(),
        envelope=installed,
    )

    options = service.configuration_options()

    assert len(options) == 1
    option = options[0]
    qualification = installed.qualifications[option.model_ref]
    default = option.default_spec
    assert option.suggested_name == "qwen.3-8b-live"
    assert option.namespace == "fs2-models"
    assert option.scale_to_zero_qualified
    assert default.artifact.manifest_digest == qualification.artifact_revisions[default.artifact.revision]
    assert default.runtime.image in qualification.runtime_images
    assert qualification.template_refs[default.runtime.template_ref.name] == default.runtime.template_ref.digest
    assert default.cache.tier is qualification.template_cache_tiers[default.runtime.template_ref.digest]
    assert default.fast_start == FastStartSpec()
    assert option.fast_start_qualified_level is FastStartLevel.OFF
    assert [choice.mechanism for choice in option.fast_start_mechanism_choices] == [FastStartMechanism.CONVENTIONAL]
    assert default.placement.accelerators_per_replica == qualification.max_accelerators_per_replica
    assert default.exposure.open_ai_aliases == []
    assert default.exposure.mcp
    assert default.exposure.mcp_tool_name == qualification.mcp_tool_name
    assert [choice.pool_ref for choice in option.pool_choices] == ["pool-a"]
    assert option.local_queue_choices == ["interactive"]
    assert option.priority_class_choices == ["standard"]
    assert option.tenant_choices == ["tenant-a"]
    assert validate_model_deployment(default, installed).disposition is ValidationDisposition.ACCEPTED

    hot_only_qualification = qualification.model_copy(update={"scale_to_zero_qualified": False})
    hot_only = installed.model_copy(
        update={"qualifications": {hot_only_qualification.model_ref: hot_only_qualification}}
    )
    hot_only_service = ModelDeploymentMutationService(
        repository=service.repository,
        writer=FakeWriter(),
        envelope=hot_only,
    )
    hot_only_option = hot_only_service.configuration_options()[0]
    assert not hot_only_option.scale_to_zero_qualified
    assert hot_only_option.default_spec.availability.min_replicas == 1

    no_mcp_qualification = qualification.model_copy(update={"mcp_tool_name": None})
    no_mcp = installed.model_copy(update={"qualifications": {no_mcp_qualification.model_ref: no_mcp_qualification}})
    no_mcp_option = ModelDeploymentMutationService(
        repository=service.repository,
        writer=FakeWriter(),
        envelope=no_mcp,
    ).configuration_options()[0]
    assert not no_mcp_option.default_spec.exposure.mcp
    assert no_mcp_option.default_spec.exposure.mcp_tool_name is None


def test_configuration_options_publish_only_declared_mechanisms_and_their_dependencies() -> None:
    installed = envelope()
    base_qualification = installed.qualifications["qwen.3-8b"]
    qualification = base_qualification.model_copy(
        update={
            "regional_cache": render_regional_cache(pool_refs=("pool-a",)),
            "host_memory_residency": render_host_memory(pool_refs=("pool-a",)),
            "gpu_resident": render_gpu_resident(pool_refs=("pool-a",)),
            "template_cache_tiers": {
                digest: CacheTier.SHARED_FILESYSTEM for digest in base_qualification.template_digests
            },
        }
    )
    installed = installed.model_copy(
        update={
            "qualifications": {qualification.model_ref: qualification},
            "residency_holder_image": qualification.runtime_images[0],
        }
    )
    service = ModelDeploymentMutationService(
        repository=StoreModelDeploymentRepository(
            MemoryStore(
                PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
                KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
            )
        ),
        writer=FakeWriter(),
        envelope=installed,
        renderer=renderer(),
        prometheus_server_address="http://prometheus.fs2-observability.svc:9090",
    )

    option = service.configuration_options()[0]
    choices = {choice.mechanism: choice for choice in option.fast_start_mechanism_choices}
    assert list(choices) == [
        FastStartMechanism.CONVENTIONAL,
        FastStartMechanism.REGIONAL_CACHE,
        FastStartMechanism.HOST_MEMORY_RESIDENCY,
    ]
    assert choices[FastStartMechanism.REGIONAL_CACHE].required_cache_tier.value == "SharedFilesystem"
    assert choices[FastStartMechanism.HOST_MEMORY_RESIDENCY].required_cache_tier.value == "SharedFilesystem"
    assert all(choice.pool_refs == ["pool-a"] for choice in choices.values())

    unproven = ModelDeploymentMutationService(
        repository=service.repository,
        writer=FakeWriter(),
        envelope=installed,
    ).configuration_options()[0]
    assert FastStartMechanism.HOST_MEMORY_RESIDENCY not in {
        choice.mechanism for choice in unproven.fast_start_mechanism_choices
    }

    for mechanism, choice in choices.items():
        selected = option.default_spec.model_copy(
            update={
                "placement": option.default_spec.placement.model_copy(update={"pool_refs": choice.pool_refs}),
                "cache": option.default_spec.cache.model_copy(
                    update={
                        "mechanism": mechanism,
                        "tier": choice.required_cache_tier or option.default_spec.cache.tier,
                    }
                ),
                "availability": option.default_spec.availability.model_copy(
                    update={
                        "min_replicas": max(
                            option.default_spec.availability.min_replicas,
                            choice.minimum_hot_replicas,
                        ),
                        "max_replicas": max(
                            option.default_spec.availability.max_replicas,
                            choice.minimum_max_replicas,
                        ),
                    }
                ),
            }
        )
        assert validate_model_deployment(selected, installed).disposition is ValidationDisposition.ACCEPTED


def test_configuration_options_prove_host_memory_fit_with_the_qualified_runtime_template() -> None:
    gib = 1024**3
    installed = envelope()
    base_qualification = installed.qualifications["qwen.3-8b"]
    host_memory = render_host_memory(
        pool_refs=("pool-a",),
        reserved_bytes=18 * gib,
        node_allocatable_bytes=24 * gib,
    )
    qualification = base_qualification.model_copy(
        update={
            "max_accelerators_per_replica": 1,
            "host_memory_residency": host_memory,
            "template_cache_tiers": {
                digest: CacheTier.SHARED_FILESYSTEM for digest in base_qualification.template_digests
            },
        }
    )
    installed = installed.model_copy(
        update={
            "pools": {
                **installed.pools,
                "pool-a": installed.pools["pool-a"].model_copy(update={"allocatable_memory_bytes": 24 * gib}),
            },
            "qualifications": {qualification.model_ref: qualification},
            "residency_holder_image": qualification.runtime_images[0],
        }
    )
    repository = StoreModelDeploymentRepository(
        MemoryStore(
            PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
            KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
        )
    )

    cannot_fill_node = ModelDeploymentMutationService(
        repository=repository,
        writer=FakeWriter(),
        envelope=installed,
        renderer=renderer(runtime_memory_request="1Gi"),
        prometheus_server_address="http://prometheus.fs2-observability.svc:9090",
    ).configuration_options()[0]
    assert FastStartMechanism.HOST_MEMORY_RESIDENCY not in {
        choice.mechanism for choice in cannot_fill_node.fast_start_mechanism_choices
    }

    fills_node = ModelDeploymentMutationService(
        repository=repository,
        writer=FakeWriter(),
        envelope=installed,
        renderer=renderer(runtime_memory_request="512Mi"),
        prometheus_server_address="http://prometheus.fs2-observability.svc:9090",
    ).configuration_options()[0]
    host_choice = next(
        choice
        for choice in fills_node.fast_start_mechanism_choices
        if choice.mechanism is FastStartMechanism.HOST_MEMORY_RESIDENCY
    )
    assert host_choice.pool_refs == ["pool-a"]


def test_configuration_options_hide_sleep_offload_without_a_runtime_actor() -> None:
    installed = envelope()
    qualification = installed.qualifications["qwen.3-8b"].model_copy(
        update={
            "host_memory_residency": render_host_memory(
                pool_refs=("pool-a",),
                mode="runtime-sleep-offload",
            )
        }
    )
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
    )

    mechanisms = [choice.mechanism for choice in service.configuration_options()[0].fast_start_mechanism_choices]
    assert FastStartMechanism.HOST_MEMORY_RESIDENCY not in mechanisms


def test_configuration_default_selects_every_compatible_pool_and_invariant_allows_subsets() -> None:
    installed = reserved_and_preemptible_envelope()
    qualification = installed.qualifications["qwen.3-8b"].model_copy(update={"max_accelerators_per_replica": 1})
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
    )

    option = service.configuration_options()[0]
    assert [choice.pool_ref for choice in option.pool_choices] == ["reserved-h100", "preemptible-h100"]
    assert option.default_spec.placement.pool_refs == ["reserved-h100", "preemptible-h100"]
    assert option.default_spec.availability.max_replicas == 4
    assert validate_model_deployment(option.default_spec, installed).disposition is ValidationDisposition.ACCEPTED

    one_pool_default = option.default_spec.model_copy(
        update={"placement": option.default_spec.placement.model_copy(update={"pool_refs": ["preemptible-h100"]})}
    )
    subset = ModelDeploymentConfigurationOption(
        **option.model_dump(exclude={"default_spec"}),
        default_spec=one_pool_default,
    )
    assert subset.default_spec.placement.pool_refs == ["preemptible-h100"]
    unknown_pool_default = option.default_spec.model_copy(
        update={"placement": option.default_spec.placement.model_copy(update={"pool_refs": ["unknown-pool"]})}
    )
    with pytest.raises(ValueError, match="non-empty subset"):
        ModelDeploymentConfigurationOption(
            **option.model_dump(exclude={"default_spec"}),
            default_spec=unknown_pool_default,
        )


@pytest.mark.asyncio
async def test_apply_returns_exact_fail_closed_validation_and_never_writes_invalid_intent() -> None:
    store = MemoryStore(
        PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
        KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
    )
    writer = FakeWriter()
    service = ModelDeploymentMutationService(
        repository=StoreModelDeploymentRepository(store),
        writer=writer,
        envelope=envelope(),
    )
    spec = model_spec()
    unsupported_storage = spec.model_copy(
        update={
            "artifact": spec.artifact.model_copy(
                update={
                    "storage_ref": ArtifactStorageRef(
                        kind="PersistentVolumeClaim",
                        name="unbound-cache",
                    )
                }
            )
        }
    )
    with pytest.raises(ModelDeploymentMutationProblemError) as rejected:
        await service.apply(
            _apply_request(
                key="reject-unrendered-storage-0001",
                proposal=ModelDeploymentPreviewProposal(name="qwen-live", spec=unsupported_storage),
            ),
            _actor(),
        )
    assert rejected.value.status_code == 422
    assert rejected.value.code == "model_deployment_rejected"
    assert "artifact_storage_ref_unsupported at $.spec.artifact.storageRef" in rejected.value.detail
    assert writer.writes == []

    missing_pool = spec.model_copy(
        update={"placement": spec.placement.model_copy(update={"pool_refs": ["future-pool"]})}
    )
    with pytest.raises(ModelDeploymentMutationProblemError) as infrastructure:
        await service.apply(
            _apply_request(
                key="reject-missing-infrastructure-0002",
                proposal=ModelDeploymentPreviewProposal(name="qwen-live", spec=missing_pool),
            ),
            _actor(),
        )
    assert infrastructure.value.status_code == 409
    assert infrastructure.value.code == "model_deployment_infrastructure_required"
    assert infrastructure.value.detail.endswith("accelerator_pools.future-pool")
    assert writer.writes == []


@pytest.mark.asyncio
async def test_plan_preview_returns_the_exact_rejected_field() -> None:
    spec = model_spec()
    unsupported_storage = spec.model_copy(
        update={
            "artifact": spec.artifact.model_copy(
                update={
                    "storage_ref": ArtifactStorageRef(
                        kind="ObjectStore",
                        name="unbound-artifact-store",
                    )
                }
            )
        }
    )
    service = ModelDeploymentPreviewService(
        envelope=envelope(),
        renderer=renderer(),
        state=InMemoryModelDeploymentPreviewState(),
        prometheus_server_address="http://prometheus:9090",
        mutation_supported=True,
    )

    with pytest.raises(ModelDeploymentPreviewProblemError) as rejected:
        await service.plan(
            ModelDeploymentPreviewProposal(name="qwen-live", spec=unsupported_storage),
            _actor(),
        )

    assert rejected.value.status_code == 422
    assert rejected.value.code == "model_deployment_rejected"
    assert "artifact_storage_ref_unsupported at $.spec.artifact.storageRef" in rejected.value.detail


def test_configuration_options_fail_closed_on_incomplete_envelope_choices() -> None:
    installed = envelope().model_copy(
        update={
            "local_queues": ["not a DNS queue"],
            "priority_classes": ["INVALID PRIORITY"],
            "tenant_ids": ["invalid tenant"],
        }
    )
    service = ModelDeploymentMutationService(
        repository=StoreModelDeploymentRepository(
            MemoryStore(
                PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
                KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
            )
        ),
        writer=FakeWriter(),
        envelope=installed,
    )

    assert service.configuration_options() == []

    unpinned = envelope()
    qualification = unpinned.qualifications["qwen.3-8b"].model_copy(
        update={"runtime_images": ["registry.example/fs2/vllm:latest"]}
    )
    unpinned = unpinned.model_copy(update={"qualifications": {qualification.model_ref: qualification}})
    unpinned_service = ModelDeploymentMutationService(
        repository=service.repository,
        writer=FakeWriter(),
        envelope=unpinned,
    )
    assert unpinned_service.configuration_options() == []


def test_capabilities_route_publishes_server_authoritative_configuration_options(
    registry: object,
    cipher: object,
    hasher: object,
) -> None:
    from test_admin_access_api import BOOTSTRAP_AUTH, _client, _runtime

    runtime = _runtime(registry, cipher, hasher)
    runtime.model_deployment_mutation = ModelDeploymentMutationService(
        repository=StoreModelDeploymentRepository(runtime.store),
        writer=FakeWriter(),
        envelope=envelope(),
    )
    with _client(runtime) as client:
        assert client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH).status_code == 200
        response = client.get("/admin/api/v1/model-deployments:capabilities")

    assert response.status_code == 200
    capabilities = response.json()["data"]
    assert capabilities["configuration_revision"] == envelope().revision
    assert [option["model_ref"] for option in capabilities["configuration_options"]] == ["qwen.3-8b"]
    assert capabilities["configuration_options"][0]["fast_start_mechanism_choices"] == [
        {
            "mechanism": "conventional",
            "pool_refs": ["pool-a"],
            "required_cache_tier": None,
            "minimum_hot_replicas": 0,
            "minimum_max_replicas": 1,
        }
    ]
    default = capabilities["configuration_options"][0]["default_spec"]
    assert default["runtime"]["image"] == envelope().qualifications["qwen.3-8b"].runtime_images[0]
    assert default["placement"]["acceleratorsPerReplica"] == 8
    assert default["exposure"]["mcp"] is True
    assert default["exposure"]["mcpToolName"] == "qwen_3_8b"


@pytest.mark.asyncio
async def test_apply_drain_rollback_reconcile_and_pending_projection() -> None:
    store = MemoryStore(
        PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
        KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
    )
    writer = FakeWriter()
    service = ModelDeploymentMutationService(
        repository=StoreModelDeploymentRepository(store),
        writer=writer,
        envelope=envelope(),
    )
    proposal = ModelDeploymentPreviewProposal(name="qwen-live", spec=model_spec())

    created = await service.apply(_apply_request(key="create-qwen-0001", proposal=proposal), _actor())
    assert created.revision.revision == 1
    assert created.projection == "applied"
    assert created.receipt is not None
    replay = await service.apply(_apply_request(key="create-qwen-0001", proposal=proposal), _actor())
    assert replay.idempotent_replay
    assert replay.revision == created.revision

    drained = await service.drain(
        name="qwen-live",
        request=ModelDeploymentActionRequest(
            base_etag=created.revision.etag,
            idempotency_key="drain-qwen-0002",
        ),
        actor=_actor(),
    )
    assert drained.revision.spec.lifecycle.desired_state is DesiredState.DRAINING
    assert drained.revision.spec.availability.min_replicas == 0

    rolled_back = await service.rollback(
        name="qwen-live",
        request=ModelDeploymentRollbackRequest(
            target_revision=1,
            base_etag=drained.revision.etag,
            idempotency_key="rollback-qwen-0003",
        ),
        actor=_actor(),
    )
    assert rolled_back.revision.action.value == "rollback"
    assert rolled_back.revision.spec == created.revision.spec

    reconciled = await service.reconcile(
        name="qwen-live",
        request=ModelDeploymentReconcileRequest(expected_etag=rolled_back.revision.etag),
        actor=_actor(),
    )
    assert reconciled.idempotent_replay
    assert reconciled.revision == rolled_back.revision

    changed_spec = rolled_back.revision.spec.model_copy(
        update={"availability": rolled_back.revision.spec.availability.model_copy(update={"max_replicas": 3})}
    )
    changed = ModelDeploymentPreviewProposal(
        name="qwen-live",
        base_etag=rolled_back.revision.etag,
        spec=changed_spec,
    )
    writer.fail = True
    pending = await service.apply(_apply_request(key="scale-qwen-0004", proposal=changed), _actor())
    assert pending.projection == "pending"
    assert pending.receipt is None
    assert "private failure" not in (pending.reason or "")


@pytest.mark.asyncio
async def test_runtime_material_change_requires_observed_cold_drained_revision() -> None:
    store = MemoryStore(
        PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
        KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
    )
    service = ModelDeploymentMutationService(
        repository=StoreModelDeploymentRepository(store),
        writer=FakeWriter(),
        envelope=envelope(),
    )
    created = await service.apply(
        _apply_request(
            key="cold-cutover-create-0001",
            proposal=ModelDeploymentPreviewProposal(name="qwen-live", spec=model_spec()),
        ),
        _actor(),
    )
    changed_spec = created.revision.spec.model_copy(
        update={"placement": created.revision.spec.placement.model_copy(update={"accelerators_per_replica": 2})}
    )
    with pytest.raises(ModelDeploymentMutationProblemError, match="drain the current revision"):
        await service.apply(
            _apply_request(
                key="cold-cutover-rejected-0002",
                proposal=ModelDeploymentPreviewProposal(
                    name="qwen-live",
                    base_etag=created.revision.etag,
                    spec=changed_spec,
                ),
            ),
            _actor(),
        )

    drained = await service.drain(
        name="qwen-live",
        request=ModelDeploymentActionRequest(
            base_etag=created.revision.etag,
            idempotency_key="cold-cutover-drain-0003",
        ),
        actor=_actor(),
    )
    now = datetime.now(UTC)
    await store.model_deployment_append_status(
        ModelDeploymentStatusObservation(
            observation_id=uuid4(),
            source_uid="modeldeployment-uid-1",
            source_resource_version="3",
            namespace=drained.revision.namespace,
            name=drained.revision.name,
            tenant_id=drained.revision.tenant_id,
            revision=drained.revision.revision,
            status=ModelDeploymentObservedStatus(
                observed_generation=2,
                phase=ModelDeploymentRuntimePhase.COLD,
                spec_digest=drained.revision.etag,
                replicas=ModelDeploymentReplicaStatus(desired=0, ready=0, available=0),
                adoption=ModelDeploymentAdoptionStatus(state=ModelDeploymentAdoptionState.NONE),
                last_reconcile_time=now,
            ),
            observed_at=now,
        )
    )
    enabled_changed = changed_spec.model_copy(update={"lifecycle": created.revision.spec.lifecycle})
    accepted = await service.apply(
        _apply_request(
            key="cold-cutover-accepted-0004",
            proposal=ModelDeploymentPreviewProposal(
                name="qwen-live",
                base_etag=drained.revision.etag,
                spec=enabled_changed,
            ),
        ),
        _actor(),
    )
    assert accepted.revision.spec.placement.accelerators_per_replica == 2


@pytest.mark.asyncio
async def test_kubernetes_writer_updates_post_owned_fields_with_merge_patch(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("t" * 32, encoding="utf-8")
    observed: list[dict[str, object]] = []
    live: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        entry: dict[str, object] = {
            "method": request.method,
            "url": str(request.url),
            "content_type": request.headers.get("content-type"),
        }
        observed.append(entry)
        if request.method == "GET":
            return httpx.Response(200, json=live) if live else httpx.Response(404, json={})
        body = __import__("json").loads(request.content)
        entry["body"] = body
        if request.method == "POST":
            assert not live
            live.update(
                {
                    **body,
                    "metadata": {
                        **body["metadata"],
                        "labels": {
                            **body["metadata"]["labels"],
                            "example.test/preserved": "label",
                        },
                        "annotations": {
                            **body["metadata"]["annotations"],
                            "example.test/preserved": "annotation",
                        },
                        "uid": "uid-1",
                        "resourceVersion": "7",
                        "generation": 1,
                        "managedFields": [
                            {
                                "apiVersion": "inference.fs2.nebius.ai/v1alpha1",
                                "fieldsType": "FieldsV1",
                                "manager": "fs2-admin-model-desired",
                                "operation": "Update",
                            }
                        ],
                    },
                }
            )
            return httpx.Response(201, json=live)
        assert request.method == "PATCH"
        if request.headers.get("content-type") == "application/apply-patch+yaml":
            return httpx.Response(409, json={"message": "apply conflicts with fields created by Update"})
        assert request.headers.get("content-type") == "application/merge-patch+json"
        assert set(body) == {"metadata", "spec"}
        assert set(body["metadata"]) == {"annotations", "labels", "resourceVersion"}
        assert body["metadata"]["resourceVersion"] == live["metadata"]["resourceVersion"]
        live["spec"] = body["spec"]
        live["metadata"]["labels"].update(body["metadata"]["labels"])
        live["metadata"]["annotations"].update(body["metadata"]["annotations"])
        live["metadata"]["resourceVersion"] = "8"
        live["metadata"]["generation"] = 2
        return httpx.Response(200, json=live)

    client = httpx.AsyncClient(base_url="https://kubernetes.test", transport=httpx.MockTransport(handler))
    writer = HttpKubernetesDesiredWriter(
        base_url="https://kubernetes.test",
        token_file=token_file,
        ca_file=tmp_path / "unused-ca",
        namespace="fs2-models",
        client=client,
    )
    store = MemoryStore(
        PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
        KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
    )
    service = ModelDeploymentMutationService(
        repository=StoreModelDeploymentRepository(store),
        writer=writer,
        envelope=envelope(),
    )
    proposal = ModelDeploymentPreviewProposal(name="qwen-live", spec=model_spec())
    result = await service.apply(_apply_request(key="create-qwen-0001", proposal=proposal), _actor())
    changed_spec = proposal.spec.model_copy(
        update={"availability": proposal.spec.availability.model_copy(update={"max_replicas": 3})}
    )
    changed = await service.apply(
        _apply_request(
            key="update-qwen-0002",
            proposal=ModelDeploymentPreviewProposal(
                name="qwen-live",
                base_etag=result.revision.etag,
                spec=changed_spec,
            ),
        ),
        _actor(),
    )
    with pytest.raises(DesiredWriteError, match="newer desired revision"):
        await writer.apply(result.revision)
    await client.aclose()

    assert result.projection == "applied"
    assert changed.projection == "applied"
    assert [item["method"] for item in observed] == ["GET", "POST", "GET", "GET", "PATCH", "GET", "GET"]
    create = observed[1]
    assert "fieldManager=fs2-admin-model-desired" in str(create["url"])
    assert "force=false" not in str(create["url"])
    assert create["content_type"] == "application/json"
    update = observed[4]
    assert "fieldManager=fs2-admin-model-desired" in str(update["url"])
    assert "fieldValidation=Strict" in str(update["url"])
    assert "force=" not in str(update["url"])
    assert update["content_type"] == "application/merge-patch+json"
    assert update["body"]["metadata"]["resourceVersion"] == "7"
    assert live["metadata"]["annotations"]["inference.fs2.nebius.ai/desired-revision"] == "2"
    assert live["metadata"]["labels"]["example.test/preserved"] == "label"
    assert live["metadata"]["annotations"]["example.test/preserved"] == "annotation"
    assert live["metadata"]["managedFields"][0]["operation"] == "Update"

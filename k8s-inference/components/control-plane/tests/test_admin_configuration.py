from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fs2_serve_catalog.loader import load_catalog

from fs2_serve.access_models import OperatorPrincipal, OperatorRole, PrincipalKind
from fs2_serve.cli import _synchronize_admin_configuration
from fs2_serve.configuration import (
    ConfigurationProblemError,
    ConfigurationService,
    DeclarativeConfigurationRenderer,
    InMemoryConfigurationAuditSink,
    InMemoryConfigurationRepository,
    StaticCatalogConfigurationAdapter,
    StoreConfigurationAuditSink,
    StoreConfigurationRepository,
    catalog_configuration_contracts,
    configuration_etag,
    load_platform_configuration,
)
from fs2_serve.configuration_models import (
    AcceleratorPoolConfiguration,
    ArtifactIdentity,
    AutoscalingConfiguration,
    ConfigurationOwner,
    ConfigurationPlanState,
    ConfigurationProposal,
    McpConfiguration,
    ModelConfiguration,
    PlacementConfiguration,
    PlatformConfiguration,
    QueueConfiguration,
    RateConfiguration,
    ReconciliationPhase,
    RollbackRequest,
    SnapshotConfiguration,
    TerraformApplyReceipt,
    ValidationSeverity,
)
from fs2_serve.memory_store import MemoryStore
from fs2_serve.store import ConflictError

CONTROL_ROOT = Path(__file__).resolve().parents[1]
SOLUTION_ROOT = CONTROL_ROOT.parents[1]
CATALOG_ROOT = SOLUTION_ROOT / "catalog/runtime"
REPO_ROOT = CATALOG_ROOT / "packaged-repository"


def principal(role: OperatorRole) -> OperatorPrincipal:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    return OperatorPrincipal(
        id=uuid4(),
        subject=f"{role.value}@example.test",
        display_name=role.value.title(),
        kind=PrincipalKind.HUMAN,
        role=role,
        enabled=True,
        created_at=now,
        created_by="bootstrap",
        updated_at=now,
    )


def qualified_configuration() -> tuple[PlatformConfiguration, StaticCatalogConfigurationAdapter]:
    catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
    contracts = catalog_configuration_contracts(catalog)
    model_id = "qwen3-8b"
    contract = contracts[model_id]
    record = catalog.model(model_id).to_dict()
    gpu_count = int(record["resources"]["gpu"]["count"])
    pool_id = "elastic-qualified"
    configuration = PlatformConfiguration(
        pools={
            pool_id: AcceleratorPoolConfiguration(
                resource_name="nvidia.com/gpu",
                accelerator_class=sorted(contract.supported_accelerator_classes)[0],
                capacity_type="preemptible",
                accelerators_per_node=max(gpu_count, 8),
                min_nodes=0,
                max_nodes=4,
                node_selector={"accelerator.fs2.example/class": "qualified"},
            )
        },
        models={
            model_id: ModelConfiguration(
                model_id=model_id,
                placement=PlacementConfiguration(
                    pool_ids=[pool_id],
                    accelerators=gpu_count,
                    topology_policy="single-node",
                ),
                autoscaling=AutoscalingConfiguration(
                    min_replicas=0,
                    max_replicas=1,
                    target_queue_depth=1,
                    polling_interval_seconds=5,
                    cooldown_seconds=300,
                ),
                queue=QueueConfiguration(
                    local_queue="inference",
                    priority_class="interactive",
                    max_queue_seconds=7200,
                ),
                snapshot=SnapshotConfiguration(),
                mcp=McpConfiguration(exposed=True, tool_name="qwen3_8b"),
                rate=RateConfiguration(concurrent_requests=4, requests_per_minute=120),
                artifact=ArtifactIdentity(
                    image_repository=record["runtime"]["image"]["reference"].split("@", 1)[0],
                    image_digest=contract.runtime_image_digest,
                    model_revision=contract.model_revision,
                    artifact_manifest_sha256=contract.artifact_manifest_sha256,
                    acquisition_contract_sha256=contract.acquisition_contract_sha256,
                    provenance_sha256=contract.provenance_sha256,
                    semantic_health_contract_sha256=contract.semantic_health_contract_sha256,
                ),
            )
        },
    )
    return configuration, StaticCatalogConfigurationAdapter(contracts)


def with_cooldown(configuration: PlatformConfiguration, seconds: int) -> PlatformConfiguration:
    model_id = next(iter(configuration.models))
    model = configuration.models[model_id]
    models = dict(configuration.models)
    models[model_id] = model.model_copy(
        update={
            "autoscaling": model.autoscaling.model_copy(update={"cooldown_seconds": seconds}),
        }
    )
    return configuration.model_copy(update={"models": models})


@pytest.mark.asyncio
async def test_supported_autoscaling_change_stops_at_a_reviewed_terraform_handoff() -> None:
    initial, catalog = qualified_configuration()
    desired = with_cooldown(initial, 301)
    repository = InMemoryConfigurationRepository(initial)
    audit = InMemoryConfigurationAuditSink()
    service = ConfigurationService(repository=repository, catalog=catalog, audit=audit)
    proposal = ConfigurationProposal(base_etag=configuration_etag(initial), desired=desired)

    diff = await service.diff(proposal)
    assert diff.runtime_change_count == 0
    assert diff.terraform_change_count == 1
    assert {item.owner for item in diff.changes} == {ConfigurationOwner.TERRAFORM}

    with pytest.raises(ConfigurationProblemError) as forbidden:
        await service.plan(proposal, principal(OperatorRole.VIEWER))
    assert forbidden.value.status_code == 403

    plan = await service.plan(proposal, principal(OperatorRole.OPERATOR))
    assert plan.state is ConfigurationPlanState.VALID, plan.validation.issues
    assert plan.validation.issues == []
    assert plan.terraform.required and plan.terraform.state == "review-required"
    assert plan.terraform.variables["model_scaling_mode"] == "keda"
    assert "admin_configuration_bootstrap_baseline_accepted" not in plan.terraform.variables
    assert plan.terraform.variables["model_scaling_overrides"]["qwen3-8b"]["cooldown_seconds"] == 301
    assert plan.terraform.variables_sha256
    assert json.loads(plan.terraform.tfvars_json) == plan.terraform.variables
    assert hashlib.sha256(plan.terraform.tfvars_json.encode()).hexdigest() == plan.terraform.tfvars_sha256
    assert plan.terraform.tfvars_filename == f"admin-configuration-{plan.plan_id}.tfvars.json"
    assert all("secret" not in key.lower() for key in plan.terraform.variables)

    status = await service.reconcile(
        plan_id=plan.plan_id,
        base_etag=configuration_etag(initial),
        actor=principal(OperatorRole.OPERATOR),
    )
    assert status.phase is ReconciliationPhase.AWAITING_TERRAFORM
    assert (await service.read()).revision == 1
    assert [receipt.action for receipt in audit.receipts] == ["plan", "reconcile"]


@pytest.mark.asyncio
async def test_noop_and_unqualified_models_are_rejected_before_handoff() -> None:
    initial, catalog = qualified_configuration()
    service = ConfigurationService(repository=InMemoryConfigurationRepository(initial), catalog=catalog)
    operator = principal(OperatorRole.OPERATOR)

    no_op = await service.plan(
        ConfigurationProposal(base_etag=configuration_etag(initial), desired=initial),
        operator,
    )
    assert no_op.state is ConfigurationPlanState.REJECTED
    assert not no_op.terraform.required and no_op.terraform.variables == {}
    assert {issue.code for issue in no_op.validation.issues} == {"configuration_no_changes"}

    model_id = next(iter(initial.models))
    bad_model = initial.models[model_id].model_copy(
        update={"artifact": initial.models[model_id].artifact.model_copy(update={"provenance_sha256": "0" * 64})}
    )
    rejected_configuration = initial.model_copy(update={"models": {model_id: bad_model}})
    rejected = await service.plan(
        ConfigurationProposal(base_etag=configuration_etag(initial), desired=rejected_configuration),
        operator,
    )
    assert rejected.state is ConfigurationPlanState.REJECTED
    assert "catalog_identity_mismatch" in {issue.code for issue in rejected.validation.issues}
    assert rejected.artifacts == [] and rejected.terraform.variables == {}


@pytest.mark.asyncio
async def test_heterogeneous_capacity_is_representable_and_unsupported_placement_fails_closed() -> None:
    initial, catalog = qualified_configuration()
    model_id = next(iter(initial.models))
    original_pool = next(iter(initial.pools.values()))
    second_pool = original_pool.model_copy(
        update={
            "resource_name": "gpu.nvidia.com",
            "capacity_type": "regular",
            "min_nodes": 1,
            "node_selector": {"accelerator.fs2.example/fabric": "gb300"},
        }
    )
    model = initial.models[model_id]
    heterogeneous = initial.model_copy(
        update={
            "pools": {"elastic-qualified": original_pool, "reserved-fabric": second_pool},
            "models": {
                model_id: model.model_copy(
                    update={
                        "placement": model.placement.model_copy(
                            update={"pool_ids": ["elastic-qualified", "reserved-fabric"]}
                        )
                    }
                )
            },
        }
    )
    service = ConfigurationService(repository=InMemoryConfigurationRepository(initial), catalog=catalog)
    accepted = await service.validate(
        ConfigurationProposal(base_etag=configuration_etag(initial), desired=heterogeneous),
        principal(OperatorRole.VIEWER),
    )
    assert accepted.valid
    assert {pool.capacity_type for pool in heterogeneous.pools.values()} == {"preemptible", "regular"}
    assert {pool.resource_name for pool in heterogeneous.pools.values()} == {"nvidia.com/gpu", "gpu.nvidia.com"}

    unsupported_pool = original_pool.model_copy(update={"accelerator_class": "nvidia-h100-sxm-80gb"})
    unsupported = initial.model_copy(update={"pools": {"elastic-qualified": unsupported_pool}})
    rejected = await service.validate(
        ConfigurationProposal(base_etag=configuration_etag(initial), desired=unsupported),
        principal(OperatorRole.VIEWER),
    )
    assert not rejected.valid
    assert "unsupported_accelerator_placement" in {issue.code for issue in rejected.issues}

    oversized = initial.model_copy(
        update={
            "models": {
                model_id: model.model_copy(
                    update={
                        "placement": model.placement.model_copy(
                            update={"accelerators": original_pool.accelerators_per_node + 1}
                        )
                    }
                )
            }
        }
    )
    rejected = await service.validate(
        ConfigurationProposal(base_etag=configuration_etag(initial), desired=oversized),
        principal(OperatorRole.VIEWER),
    )
    assert "placement_exceeds_node" in {issue.code for issue in rejected.issues}


@pytest.mark.asyncio
async def test_unsupported_accelerator_is_bootstrap_warning_but_proposals_stay_fail_closed() -> None:
    initial, catalog = qualified_configuration()
    pool_id = next(iter(initial.pools))
    unsupported_pool = initial.pools[pool_id].model_copy(update={"accelerator_class": "nvidia-h100-sxm5-80gb"})
    observed_baseline = initial.model_copy(update={"pools": {pool_id: unsupported_pool}})
    service = ConfigurationService(repository=InMemoryConfigurationRepository(initial), catalog=catalog)

    bootstrap = await service.validate_bootstrap(observed_baseline)
    placement_issue = next(issue for issue in bootstrap.issues if issue.code == "unsupported_accelerator_placement")
    assert bootstrap.valid
    assert placement_issue.severity is ValidationSeverity.WARNING
    assert "without catalog qualification" in placement_issue.message

    proposal = ConfigurationProposal(base_etag=configuration_etag(initial), desired=observed_baseline)
    validation = await service.validate(proposal, principal(OperatorRole.VIEWER))
    placement_issue = next(issue for issue in validation.issues if issue.code == "unsupported_accelerator_placement")
    assert not validation.valid
    assert placement_issue.severity is ValidationSeverity.ERROR

    plan = await service.plan(proposal, principal(OperatorRole.OPERATOR))
    placement_issue = next(
        issue for issue in plan.validation.issues if issue.code == "unsupported_accelerator_placement"
    )
    assert plan.state is ConfigurationPlanState.REJECTED
    assert placement_issue.severity is ValidationSeverity.ERROR
    assert plan.artifacts == [] and plan.terraform.variables == {}


@pytest.mark.asyncio
async def test_existing_unqualified_placement_can_change_scaling_without_changing_hardware() -> None:
    initial, catalog = qualified_configuration()
    pool_id = next(iter(initial.pools))
    model_id = next(iter(initial.models))
    unsupported_pool = initial.pools[pool_id].model_copy(update={"accelerator_class": "nvidia-h100-sxm5-80gb"})
    observed_baseline = initial.model_copy(update={"pools": {pool_id: unsupported_pool}})
    repository = InMemoryConfigurationRepository(observed_baseline)
    service = ConfigurationService(repository=repository, catalog=catalog)
    assert observed_baseline.models[model_id].enabled is True
    assert observed_baseline.models[model_id].autoscaling.min_replicas == 0
    scaled_model = observed_baseline.models[model_id].model_copy(
        update={
            "autoscaling": observed_baseline.models[model_id].autoscaling.model_copy(
                update={"min_replicas": 1 if observed_baseline.models[model_id].autoscaling.min_replicas == 0 else 0}
            )
        }
    )
    scaled = observed_baseline.model_copy(update={"models": {model_id: scaled_model}})
    proposal = ConfigurationProposal(base_etag=configuration_etag(observed_baseline), desired=scaled)

    validation = await service.validate(proposal, principal(OperatorRole.VIEWER))
    placement_issue = next(issue for issue in validation.issues if issue.code == "unsupported_accelerator_placement")
    assert validation.valid
    assert placement_issue.severity is ValidationSeverity.WARNING
    assert "does not change" in placement_issue.message

    plan = await service.plan(proposal, principal(OperatorRole.OPERATOR))
    assert plan.state is ConfigurationPlanState.VALID, plan.validation.issues
    assert plan.terraform.required is True

    changed_selector_pool = unsupported_pool.model_copy(
        update={"node_selector": {"accelerator.fs2.nebius/pool-id": "different-h100-pool"}}
    )
    changed_selector = scaled.model_copy(update={"pools": {pool_id: changed_selector_pool}})
    selector_rejected = await service.validate(
        ConfigurationProposal(base_etag=configuration_etag(observed_baseline), desired=changed_selector),
        principal(OperatorRole.VIEWER),
    )
    assert not selector_rejected.valid
    assert (
        next(issue for issue in selector_rejected.issues if issue.code == "unsupported_accelerator_placement").severity
        is ValidationSeverity.ERROR
    )

    moved_pool = unsupported_pool.model_copy(update={"accelerator_class": "nvidia-b300-sxm"})
    moved = scaled.model_copy(update={"pools": {pool_id: moved_pool}})
    rejected = await service.validate(
        ConfigurationProposal(base_etag=configuration_etag(observed_baseline), desired=moved),
        principal(OperatorRole.VIEWER),
    )
    assert not rejected.valid
    assert (
        next(issue for issue in rejected.issues if issue.code == "unsupported_accelerator_placement").severity
        is ValidationSeverity.ERROR
    )


@pytest.mark.asyncio
async def test_disabled_unqualified_model_cannot_be_activated_by_grandfathering() -> None:
    initial, catalog = qualified_configuration()
    pool_id = next(iter(initial.pools))
    model_id = next(iter(initial.models))
    unsupported_pool = initial.pools[pool_id].model_copy(update={"accelerator_class": "nvidia-h100-sxm5-80gb"})
    disabled_model = initial.models[model_id].model_copy(update={"enabled": False})
    observed_baseline = initial.model_copy(
        update={"pools": {pool_id: unsupported_pool}, "models": {model_id: disabled_model}}
    )
    enabled = observed_baseline.model_copy(
        update={"models": {model_id: disabled_model.model_copy(update={"enabled": True})}}
    )
    service = ConfigurationService(
        repository=InMemoryConfigurationRepository(observed_baseline),
        catalog=catalog,
    )

    validation = await service.validate(
        ConfigurationProposal(base_etag=configuration_etag(observed_baseline), desired=enabled),
        principal(OperatorRole.VIEWER),
    )

    placement_issue = next(issue for issue in validation.issues if issue.code == "unsupported_accelerator_placement")
    assert not validation.valid
    assert placement_issue.severity is ValidationSeverity.ERROR


@pytest.mark.asyncio
async def test_bootstrap_warning_does_not_downgrade_immutable_identity_mismatch() -> None:
    initial, catalog = qualified_configuration()
    pool_id = next(iter(initial.pools))
    model_id = next(iter(initial.models))
    unsupported_pool = initial.pools[pool_id].model_copy(update={"accelerator_class": "nvidia-h100-sxm5-80gb"})
    mismatched_model = initial.models[model_id].model_copy(
        update={
            "artifact": initial.models[model_id].artifact.model_copy(
                update={"model_revision": "unqualified-runtime-revision"}
            )
        }
    )
    baseline = initial.model_copy(update={"pools": {pool_id: unsupported_pool}, "models": {model_id: mismatched_model}})

    validation = await ConfigurationService(
        repository=InMemoryConfigurationRepository(initial),
        catalog=catalog,
    ).validate_bootstrap(baseline)

    assert not validation.valid
    severities = {issue.code: issue.severity for issue in validation.issues}
    assert severities["unsupported_accelerator_placement"] is ValidationSeverity.WARNING
    assert severities["catalog_revision_mismatch"] is ValidationSeverity.ERROR


@pytest.mark.asyncio
async def test_unconsumed_fields_and_add_model_cannot_reach_awaiting_or_effective_state() -> None:
    initial, catalog = qualified_configuration()
    model_id = next(iter(initial.models))
    pool_id = next(iter(initial.pools))
    model = initial.models[model_id]
    pool = initial.pools[pool_id]
    added_id = "not-qualified-new-model"
    added_model = model.model_copy(update={"model_id": added_id})
    candidates = {
        f"$.pools.{pool_id}.max_nodes": initial.model_copy(
            update={"pools": {pool_id: pool.model_copy(update={"max_nodes": pool.max_nodes + 1})}}
        ),
        f"$.models.{model_id}.enabled": initial.model_copy(
            update={"models": {model_id: model.model_copy(update={"enabled": False})}}
        ),
        f"$.models.{model_id}.placement.topology_policy": initial.model_copy(
            update={
                "models": {
                    model_id: model.model_copy(
                        update={"placement": model.placement.model_copy(update={"topology_policy": "any"})}
                    )
                }
            }
        ),
        f"$.models.{model_id}.queue.max_queue_seconds": initial.model_copy(
            update={
                "models": {
                    model_id: model.model_copy(
                        update={
                            "queue": model.queue.model_copy(
                                update={"max_queue_seconds": model.queue.max_queue_seconds + 1}
                            )
                        }
                    )
                }
            }
        ),
        f"$.models.{model_id}.snapshot.restore_timeout_seconds": initial.model_copy(
            update={
                "models": {
                    model_id: model.model_copy(
                        update={
                            "snapshot": model.snapshot.model_copy(
                                update={"restore_timeout_seconds": model.snapshot.restore_timeout_seconds + 1}
                            )
                        }
                    )
                }
            }
        ),
        f"$.models.{model_id}.mcp.tool_name": initial.model_copy(
            update={
                "models": {
                    model_id: model.model_copy(
                        update={"mcp": model.mcp.model_copy(update={"tool_name": "qwen3_8b_v2"})}
                    )
                }
            }
        ),
        f"$.models.{model_id}.rate.concurrent_requests": initial.model_copy(
            update={
                "models": {
                    model_id: model.model_copy(
                        update={
                            "rate": model.rate.model_copy(
                                update={"concurrent_requests": model.rate.concurrent_requests + 1}
                            )
                        }
                    )
                }
            }
        ),
        f"$.models.{added_id}": initial.model_copy(update={"models": {model_id: model, added_id: added_model}}),
    }

    for expected_path, desired in candidates.items():
        repository = InMemoryConfigurationRepository(initial)
        service = ConfigurationService(repository=repository, catalog=catalog)
        plan = await service.plan(
            ConfigurationProposal(base_etag=configuration_etag(initial), desired=desired),
            principal(OperatorRole.OPERATOR),
        )
        unsupported = [
            issue.path for issue in plan.validation.issues if issue.code == "configuration_change_not_applicable"
        ]
        assert any(path == expected_path or path.startswith(f"{expected_path}.") for path in unsupported)
        assert plan.state is ConfigurationPlanState.REJECTED
        assert not plan.terraform.required
        assert plan.terraform.variables == {}
        assert plan.artifacts == []
        with pytest.raises(ConfigurationProblemError) as rejected:
            await service.reconcile(
                plan_id=plan.plan_id,
                base_etag=configuration_etag(initial),
                actor=principal(OperatorRole.OPERATOR),
            )
        assert rejected.value.code == "configuration_plan_rejected"
        assert (await service.read()).desired == (await service.read()).effective == initial
        with pytest.raises(ConfigurationProblemError) as absent:
            await service.status(plan.plan_id)
        assert absent.value.code == "configuration_reconciliation_not_found"


@pytest.mark.asyncio
async def test_rollback_is_admin_only_and_remains_a_terraform_plan(cipher, hasher) -> None:
    initial, catalog = qualified_configuration()
    second = with_cooldown(initial, 301)
    store = MemoryStore(cipher, hasher)
    repository = StoreConfigurationRepository(store)
    first = await repository.ensure_initial(initial, actor="terraform-bootstrap")
    service = ConfigurationService(repository=repository, catalog=catalog)
    applied_plan = await service.plan(
        ConfigurationProposal(base_etag=first.etag, desired=second),
        principal(OperatorRole.OPERATOR),
    )
    awaiting = await service.reconcile(
        plan_id=applied_plan.plan_id,
        base_etag=first.etag,
        actor=principal(OperatorRole.OPERATOR),
    )
    await repository.accept_terraform_applied(
        second,
        TerraformApplyReceipt(
            plan_id=applied_plan.plan_id,
            reconciliation_id=awaiting.reconciliation_id,
            base_revision=first.revision,
            base_etag=first.etag,
            proposed_etag=applied_plan.proposed_etag,
            configuration_sha256=configuration_etag(second),
        ),
        actor="terraform-applied",
    )

    request = RollbackRequest(target_revision=1, base_etag=configuration_etag(second))
    with pytest.raises(ConfigurationProblemError) as forbidden:
        await service.rollback(request, principal(OperatorRole.OPERATOR))
    assert forbidden.value.status_code == 403
    rollback = await service.rollback(request, principal(OperatorRole.ADMIN))
    assert rollback.target_revision == 1
    assert rollback.plan.terraform.required
    assert rollback.plan.diff.runtime_change_count == 0


@pytest.mark.asyncio
async def test_terraform_apply_receipt_closes_status_atomically_and_replays_exactly(cipher, hasher) -> None:
    initial, catalog = qualified_configuration()
    second = with_cooldown(initial, 301)
    store = MemoryStore(cipher, hasher)
    repository = StoreConfigurationRepository(store)

    with pytest.raises(ValueError, match="Terraform-rendered baseline actor"):
        await repository.ensure_initial(initial, actor="operator-request")
    assert await store.configuration_current() is None

    first = await repository.ensure_initial(initial, actor="terraform-bootstrap")
    same = await repository.ensure_initial(initial, actor="terraform-bootstrap")
    with pytest.raises(ConflictError, match="correlated Terraform apply receipt"):
        await repository.ensure_initial(second, actor="terraform-bootstrap")

    service = ConfigurationService(
        repository=repository,
        catalog=catalog,
        audit=StoreConfigurationAuditSink(store),
    )
    plan = await service.plan(
        ConfigurationProposal(base_etag=first.etag, desired=second),
        principal(OperatorRole.OPERATOR),
    )
    awaiting = await service.reconcile(
        plan_id=plan.plan_id,
        base_etag=first.etag,
        actor=principal(OperatorRole.OPERATOR),
    )
    receipt = TerraformApplyReceipt(
        plan_id=plan.plan_id,
        reconciliation_id=awaiting.reconciliation_id,
        base_revision=plan.base_revision,
        base_etag=plan.base_etag,
        proposed_etag=plan.proposed_etag,
        configuration_sha256=configuration_etag(second),
    )

    applied, replay = await asyncio.gather(
        repository.accept_terraform_applied(second, receipt, actor="terraform-applied"),
        repository.accept_terraform_applied(second, receipt, actor="terraform-applied"),
    )

    assert first.revision == same.revision == 1
    assert applied.revision == 2 and applied.previous_revision == 1
    assert replay == applied
    assert applied.desired == applied.effective == second
    assert (await repository.current()).etag == configuration_etag(second)
    succeeded = await service.status(awaiting.reconciliation_id)
    assert succeeded.phase is ReconciliationPhase.SUCCEEDED
    assert succeeded.applied_revision == 2
    assert [item.action for item in store.audit] == [
        "configuration.bootstrap",
        "configuration.plan",
        "configuration.reconcile",
        "configuration.terraform-applied",
    ]


@pytest.mark.asyncio
async def test_terraform_baseline_durably_adopts_changed_configuration_without_receipt(cipher, hasher) -> None:
    initial, catalog = qualified_configuration()
    second = with_cooldown(initial, 301)
    store = MemoryStore(cipher, hasher)
    repository = StoreConfigurationRepository(store)

    first = await repository.adopt_terraform_baseline(initial)
    sync_error = await _synchronize_admin_configuration(
        repository,
        first,
        second,
        None,
    )
    adopted = await repository.current()
    replay_error = await _synchronize_admin_configuration(repository, adopted, second, None)
    replay = await repository.current()

    assert first.revision == 1
    assert first.created_by == "terraform-baseline"
    assert sync_error is replay_error is None
    assert adopted == replay
    assert adopted.revision == 2
    assert adopted.previous_revision == first.revision
    assert adopted.reconciliation_id is None
    assert adopted.created_by == "terraform-baseline"
    assert adopted.desired == adopted.effective == second
    assert await repository.current() == adopted
    assert await repository.get_revision(1) == first

    # The baseline path does not replace the optional reviewed planning flow.
    service = ConfigurationService(repository=repository, catalog=catalog)
    rollback = await service.rollback(
        RollbackRequest(target_revision=1, base_etag=adopted.etag),
        principal(OperatorRole.ADMIN),
    )
    assert rollback.plan.state is ConfigurationPlanState.VALID
    assert rollback.plan.terraform.required


@pytest.mark.asyncio
async def test_invalid_optional_receipt_falls_back_to_authoritative_terraform_baseline(
    tmp_path: Path,
    cipher,
    hasher,
) -> None:
    initial, _ = qualified_configuration()
    desired = with_cooldown(initial, 301)
    repository = StoreConfigurationRepository(MemoryStore(cipher, hasher))
    current = await repository.adopt_terraform_baseline(initial)
    receipt_file = tmp_path / "terraform-apply-receipt.json"
    receipt_file.write_text("{}\n", encoding="utf-8")

    await _synchronize_admin_configuration(repository, current, desired, receipt_file)

    adopted = await repository.current()
    assert adopted.revision == 2
    assert adopted.desired == adopted.effective == desired
    assert adopted.created_by == "terraform-baseline"
    assert adopted.reconciliation_id is None


@pytest.mark.asyncio
async def test_terraform_apply_receipt_mismatch_is_fail_closed_without_mutation(cipher, hasher) -> None:
    initial, catalog = qualified_configuration()
    desired = with_cooldown(initial, 301)
    store = MemoryStore(cipher, hasher)
    repository = StoreConfigurationRepository(store)
    first = await repository.ensure_initial(initial, actor="terraform-bootstrap")
    service = ConfigurationService(repository=repository, catalog=catalog)
    plan = await service.plan(
        ConfigurationProposal(base_etag=first.etag, desired=desired),
        principal(OperatorRole.OPERATOR),
    )
    awaiting = await service.reconcile(
        plan_id=plan.plan_id,
        base_etag=first.etag,
        actor=principal(OperatorRole.OPERATOR),
    )
    invented = uuid4()
    receipt = TerraformApplyReceipt(
        plan_id=invented,
        reconciliation_id=invented,
        base_revision=plan.base_revision,
        base_etag=plan.base_etag,
        proposed_etag=plan.proposed_etag,
        configuration_sha256=plan.proposed_etag,
    )
    audit_count = len(store.audit)

    with pytest.raises(ConflictError, match="no durable plan"):
        await repository.accept_terraform_applied(desired, receipt, actor="terraform-applied")

    assert (await repository.current()) == first
    assert (await service.status(awaiting.reconciliation_id)).phase is ReconciliationPhase.AWAITING_TERRAFORM
    assert len(store.audit) == audit_count


@pytest.mark.asyncio
async def test_renderer_is_deterministic_and_rejects_nested_secret_keys() -> None:
    initial, _ = qualified_configuration()
    plan_id = uuid4()
    renderer = DeclarativeConfigurationRenderer()
    first = await renderer.render(
        initial,
        plan_id=plan_id,
        base_revision=1,
        base_etag=configuration_etag(initial),
    )
    second = await renderer.render(
        initial,
        plan_id=plan_id,
        base_revision=1,
        base_etag=configuration_etag(initial),
    )
    assert first == second

    pool_id = next(iter(initial.pools))
    unsafe_pool = initial.pools[pool_id].model_copy(
        update={"node_selector": {"example.test/secret-token": "forbidden"}}
    )
    unsafe = initial.model_copy(update={"pools": {pool_id: unsafe_pool}})
    with pytest.raises(RuntimeError, match="secret-bearing key"):
        await renderer.render(
            unsafe,
            plan_id=plan_id,
            base_revision=1,
            base_etag=configuration_etag(initial),
        )


def test_configuration_loader_rejects_duplicates_nonfinite_and_symlinks(tmp_path: Path) -> None:
    configuration, _ = qualified_configuration()
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps(configuration.model_dump(mode="json")), encoding="utf-8")
    assert load_platform_configuration(valid) == configuration

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"a","schema_version":"b"}', encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_platform_configuration(duplicate)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"value":NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_platform_configuration(nonfinite)

    linked = tmp_path / "linked.json"
    linked.symlink_to(valid)
    with pytest.raises(ValueError, match="symlink"):
        load_platform_configuration(linked)

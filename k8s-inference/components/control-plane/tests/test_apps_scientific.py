from __future__ import annotations

import json
import runpy
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from conftest import SOLUTION_ROOT
from test_scientific_batch_execution_handoff import runtime_execution_map, runtime_plan, runtime_profile
from test_scientific_batch_execution_handoff import scheduling as runtime_scheduling
from test_scientific_batch_production import principal, scientific_runtime

from fs2_serve.apps_models import AppRecord
from fs2_serve.apps_repository import MemoryAppsRepository
from fs2_serve.apps_scientific import (
    AppScientificCluster,
    AppScientificExecution,
    AppScientificModels,
    AppScientificProfile,
    AppScientificProfiles,
    AppScientificScheduling,
    ScientificAppsInventory,
)
from fs2_serve.scientific_batch.codec import state_from_value, state_to_value
from fs2_serve.scientific_batch.models import (
    ArtifactAccessContext,
    ResolvedArtifactMaterialization,
    ServiceClass,
    WorkloadKind,
    WorkloadResource,
)


async def _inventory(model_ref="protein-design"):
    repository = MemoryAppsRepository()
    records = []
    now = datetime.now(UTC)
    for index in range(2):
        app_id = uuid4()
        record = AppRecord(
            app_id=app_id,
            model_ref=model_ref,
            public_model_id=f"app-{app_id.hex}",
            display_name=f"Scientific app {index}",
            execution_mode="scientific",
            namespace="fs2-models",
            created_at=now,
            updated_at=now,
        )
        await repository.seed(record)
        records.append(record)
    inventory = ScientificAppsInventory(repository)
    await inventory.refresh()
    return inventory, records


@pytest.mark.asyncio
async def test_admin_replica_refreshes_clone_inventory_before_policy_startup_projection(tmp_path):
    from test_scientific_admin import _readiness

    from fs2_serve.scientific_admin import ScientificModelSnapshot
    from fs2_serve.scientific_admin_models import ScientificModelReadinessList

    creator, records = await _inventory("protenix-v2")
    # A different API replica has no PAT request and did not create the app.
    # The durable policy row is already visible to its policy repository.
    replica = ScientificAppsInventory(creator.repository)
    source = runtime_execution_map(tmp_path)
    execution = AppScientificExecution(source, replica)
    assert not replica.records

    class SourceModels:
        async def list_models(self, *, tenant_id=None):
            return ScientificModelSnapshot(
                data=ScientificModelReadinessList(items=[_readiness("protenix-v2")]),
                observed_at=datetime.now(UTC),
            )

    models = AppScientificModels(SourceModels(), replica)
    snapshot = await models.list_models()
    known = {item.model_id for item in snapshot.data.items}
    assert known == {"protenix-v2", *(record.public_model_id for record in records)}
    for record in records:
        assert execution.startup_policy_options(record.public_model_id) == source.startup_policy_options("protenix-v2")
    again = await models.list_models()
    assert len(again.data.items) == len(known)  # Refresh cannot duplicate aliases.


@pytest.mark.asyncio
async def test_two_scientific_apps_keep_qualified_source_and_independent_frozen_operations(registry, cipher, hasher):
    runtime, controller, repository, cluster, pointer = scientific_runtime(registry, cipher, hasher)
    service = runtime.scientific_batches
    inventory, records = await _inventory()
    canonical = service.profiles.get("protein-design")
    service.profiles = AppScientificProfiles(service.profiles, inventory)
    service.execution_binding = AppScientificExecution(service.execution_binding, inventory)
    service.plan_factory = AppScientificExecution(service.plan_factory, inventory)
    service.scheduling = AppScientificScheduling(service.scheduling, inventory)
    identity = (await principal(runtime.store)).model_copy(update={"models": frozenset({"*"})})
    discovery = service.discover(identity, surface="mcp")["data"]
    assert {row["model_id"] for row in discovery} == {"protein-design", *(item.public_model_id for item in records)}
    for record in records:
        profile = service.profiles.get(record.public_model_id)
        assert profile.value is canonical.value
        assert profile.model_revision == canonical.model_revision
        assert profile.execution_identity_sha256 == canonical.execution_identity_sha256
        assert profile.mcp_tool_name == f"app_{record.app_id.hex}"
    request = {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": "design",
        "service_class": "customer-batch",
        "input_manifest": pointer,
        "parameters": {},
    }
    operations = []
    for record in records:
        first = await service.submit(
            principal=identity,
            model_id=record.public_model_id,
            request=request,
            idempotency_key=f"apps-test-{record.app_id}",
        )
        replay = await service.submit(
            principal=identity,
            model_id=record.public_model_id,
            request=request,
            idempotency_key=f"apps-test-{record.app_id}",
        )
        operation_id = UUID(first["operation"]["id"])
        operations.append(operation_id)
        assert replay["operation"]["id"] == str(operation_id)
        assert replay["operation"]["reused"] is True
        state = repository.records[operation_id]
        assert state.model_id == record.public_model_id
        assert state.execution_plan.model_id == record.public_model_id
        assert state.scheduling.model_lane == record.public_model_id
        assert state.execution_plan.source_revision == canonical.model_revision
        assert state.variant_id == "protein-design-h100"
        assert state.scheduling.stages[0].resolved_pool_preference == ("h100-preemptible",)
        assert state_from_value(state_to_value(state)) == state
        assert (await runtime.store.get_operation(operation_id)).model_id == record.public_model_id
    assert operations[0] != operations[1]
    assert len(runtime.store.operations) == 2  # Replays do not add logical operations.
    assert await runtime.store.claim_operation("generic-worker", lease_seconds=30) is None
    await controller.reconcile_once()
    assert cluster.apply_history[0].model_id in {record.public_model_id for record in records}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage_id", ["prepare", "inference"])
async def test_real_scientific_renderer_preserves_source_bindings_but_signs_app_capability(tmp_path, stage_id):
    inventory, records = await _inventory("protenix-v2")
    app = records[0]
    source = runtime_execution_map(tmp_path)
    renderer = AppScientificExecution(source, inventory)
    profile = AppScientificProfile(value=runtime_profile().value, app=app)
    canonical_plan = runtime_plan()
    app_plan = replace(canonical_plan, model_id=app.public_model_id)
    access = ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="tenant-a")
    localized = renderer.verify_runtime_artifacts(profile, app_plan, access)
    bound = renderer.bind_runtime_artifacts(profile, app_plan, access, localized)
    canonical_bound = source.bind_runtime_artifacts(runtime_profile(), canonical_plan, access, localized)
    assert replace(bound, model_id=canonical_bound.model_id) == canonical_bound
    snapshot = runtime_scheduling(bound.controller_plan)
    invocation = bound.invocation(stage_id, "main")
    resource = WorkloadResource(
        operation_id=uuid4(),
        batch_id=uuid4(),
        workload_id=uuid4(),
        attempt_id=uuid4(),
        stage_id=stage_id,
        shard_id="main",
        attempt_number=1,
        tenant_id="tenant-a",
        model_id=app.public_model_id,
        variant_id=bound.variant_id,
        input_artifact_id=uuid4(),
        service_class=ServiceClass.CUSTOMER_BATCH,
        scheduling_snapshot_digest=snapshot.digest,
        namespace="fs2-models",
        name="app-prepare",
        kind=WorkloadKind.JOB,
        scheduling=replace(snapshot.stage(stage_id), workload_namespace="fs2-models", route_namespace="fs2-models"),
        invocation=invocation,
        materializations=tuple(
            ResolvedArtifactMaterialization.resolve(
                item, artifact_id=uuid4(), digest="sha256:" + "e" * 64,
                size_bytes=128, media_type="application/x-tar", compression=None,
            )
            for item in invocation.materializations
        ),
        access_context=access,
        runtime_artifacts=localized,
        execution_map_sha256=bound.execution_map_sha256,
        execution_binding=bound.execution_binding(stage_id),
    )
    manifest = renderer.render(resource)
    pod = manifest["spec"]["template"]["spec"]
    env = {entry["name"]: entry["value"] for entry in pod["containers"][0]["env"]}
    marker = json.loads(env["FS2_RUNTIME_ARTIFACTS_JSON"])
    assert marker["model_id"] == app.model_ref
    assert marker["operation_id"] == str(resource.operation_id)
    assert marker["attempt_id"] == str(resource.attempt_id)
    assert marker["tenant_id"] == resource.tenant_id
    # Preparation, verification and model processes see the same canonical
    # marker. The signed access capability below remains app-scoped.
    marker_values = [
        json.loads(entry["value"])
        for container in [*pod["initContainers"], *pod["containers"]]
        for entry in container.get("env", [])
        if entry["name"] == "FS2_RUNTIME_ARTIFACTS_JSON"
    ]
    assert len(marker_values) == 3
    assert all(value == marker for value in marker_values)
    # Run the actual unchanged image contract, not just an invented equality
    # assertion: this rejected the first live clone before the correction.
    image_contract = runpy.run_path(str(
        SOLUTION_ROOT / "models/cancer-immunotherapy/images/structure-secondary/runtime_localization.py"
    ))
    marker_path = tmp_path / "actual-image-contract-marker.json"
    marker_path.write_text(json.dumps(marker))
    expectations = tuple(
        image_contract["RuntimeArtifactExpectation"](
            artifact_id=item["artifact_id"],
            mount_path=item["mount_path"],
            content_sha256=item["content_digest"].removeprefix("sha256:"),
            expected_manifest_sha256=item["artifact_manifest_sha256"],
            sub_path=item["sub_path"],
        )
        for item in marker["artifacts"]
    )
    assert image_contract["validate_runtime_localization"](
        marker_path, model_id=app.model_ref, variant_id=resource.variant_id,
        stage_id=stage_id, artifacts=expectations,
    ) == marker
    marker_path.write_text(json.dumps({**marker, "model_id": app.public_model_id}))
    with pytest.raises(SystemExit, match="model_id differs from the image contract"):
        image_contract["validate_runtime_localization"](
            marker_path, model_id=app.model_ref, variant_id=resource.variant_id,
            stage_id=stage_id, artifacts=expectations,
        )
    capability = next(
        entry["value"]
        for container in pod["containers"]
        for entry in container.get("env", [])
        if entry["name"] == "FS2_SCIENTIFIC_WORKLOAD_CAPABILITY"
    )
    claims = source.capability_authority.verify(capability)
    assert claims.model_id == app.public_model_id
    assert claims.operation_id == resource.operation_id
    assert bound.stage_bindings == canonical_bound.stage_bindings
    # True-gang templates use the same marker boundary; do not accidentally
    # patch only the top-level fanout Job layout.
    gang = renderer.render(replace(
        resource, kind=WorkloadKind.JOB_SET, shard_id=None, gang_size=2,
        invocation=replace(invocation, shard_id="gang"),
    ))
    gang_pod = gang["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
    assert all(
        json.loads(entry["value"])["model_id"] == app.model_ref
        for container in [*gang_pod["initContainers"], *gang_pod["containers"]]
        for entry in container.get("env", [])
        if entry["name"] == "FS2_RUNTIME_ARTIFACTS_JSON"
    )


@pytest.mark.asyncio
async def test_independent_worker_refreshes_new_app_mapping_before_rendering():
    inventory, records = await _inventory()
    worker_inventory = ScientificAppsInventory(inventory.repository)

    class Cluster:
        calls = 0

        async def apply(self, resource, *, controller_fence):
            assert worker_inventory.source(resource.model_id) == "protein-design"
            assert controller_fence == 42
            self.calls += 1
            return "one-created-workload"

    from types import SimpleNamespace

    source = Cluster()
    result = await AppScientificCluster(source, worker_inventory).apply(
        SimpleNamespace(model_id=records[0].public_model_id),
        controller_fence=42,
    )
    assert result == "one-created-workload"
    assert source.calls == 1

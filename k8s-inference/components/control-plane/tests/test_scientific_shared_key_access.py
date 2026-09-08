"""Customer model grants over shared platform-authorized scientific runtimes.

The runtime/receipt/input values here are synthetic unit fixtures, not new
qualification evidence. Authorization uses the production renderer method and
service; scheduling, GPU work and storage remain local test doubles.
"""

from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import pytest
from test_apps_scientific import _inventory
from test_scientific_batch_execution_handoff import _academic_af3_renderer
from test_scientific_batch_production import (
    FakeArtifactAccess,
    FakeExecutionBinding,
    profile_value,
    scheduling_with_academic_route,
    scientific_runtime,
)
from test_scientific_mcp_aliases import scientific_client

from fs2_serve.api import create_app
from fs2_serve.apps_scientific import AppScientificExecution, AppScientificProfiles, AppScientificScheduling
from fs2_serve.live_acceptance import _mcp_result
from fs2_serve.models import Scope, TokenCreate
from fs2_serve.scientific_artifacts import ArtifactNotFoundError
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer
from fs2_serve.scientific_batch.models import ArtifactAccessContext, ScientificBatchPlan, ScientificStagePlan
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileError, ScientificWorkloadProfile
from fs2_serve.scientific_batch.scheduling import SchedulingContractResolver
from fs2_serve.store import NotFoundError

MODEL = "protein-design"
PLATFORM_RECEIPT = "a" * 64
SCOPES = {
    Scope.CATALOG_READ,
    Scope.INFERENCE_INVOKE,
    Scope.MCP_INVOKE,
    Scope.OPERATIONS_READ,
    Scope.OPERATIONS_RESULT,
}


class PlatformBinding(FakeExecutionBinding):
    # Exercise the real platform authorization boundary, not a fake permit.
    access_context = FileScientificManifestRenderer.access_context
    access_profiles = {MODEL: "academic"}
    academic_tenant_id = "operator-assets"
    academic_authorization_receipt_sha256 = PLATFORM_RECEIPT


class CustomerInputs:
    def __init__(self):
        self.inputs = {}

    def add(self, tenant):
        pointer = {
            "artifact_id": str(uuid4()),
            "sha256": "1" * 64,
            "size_bytes": 100,
            "media_type": "application/vnd.fs2.scientific-manifest+json",
        }
        self.inputs[tenant] = FakeArtifactAccess(pointer)
        return pointer

    async def validate_input(self, pointer, *, tenant_id):
        owned = self.inputs.get(tenant_id)
        if owned is None or owned.pointer != pointer:
            raise ArtifactNotFoundError("input artifact does not exist")
        return replace(
            owned.admission,
            access_context=ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id=tenant_id),
        )


def shared_runtime(registry, cipher, hasher):
    document = profile_value()
    document["access"] = {
        "profile": "academic",
        "state": "verified",
        "credentials_embedded": False,
        "receipt_digest": "sha256:" + PLATFORM_RECEIPT,
    }
    runtime, controller, repository, cluster, _ = scientific_runtime(
        registry, cipher, hasher, profile_document=document
    )
    runtime.scientific_batches.execution_binding = PlatformBinding()
    runtime.scientific_batches.artifacts = CustomerInputs()
    return runtime, controller, repository, cluster


def request(pointer):
    return {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": "design",
        "service_class": "customer-batch",
        "input_manifest": pointer,
        "parameters": {},
    }


async def issue(runtime, tenant, *, models=None, scopes=None):
    issued = await runtime.tokens.issue(
        TokenCreate(
            principal_id="same-user-name",
            tenant_id=tenant,
            scopes=SCOPES if scopes is None else scopes,
            models={MODEL} if models is None else models,
            max_concurrency=4,
        ),
        created_by="unit-test",
    )
    return issued, await runtime.tokens.verify(issued.token)


@pytest.mark.parametrize("tenant", ["operator-assets", "kopra", "another-customer"])
@pytest.mark.parametrize("asset_owner", [None, "operator-assets"])
def test_actual_academic_renderer_retains_platform_receipt_and_customer_tenant(tmp_path, tenant, asset_owner):
    renderer, profile, _ = _academic_af3_renderer(tmp_path)
    renderer.academic_tenant_id = asset_owner
    renderer.academic_authorization_receipt_sha256 = PLATFORM_RECEIPT
    profile = ScientificWorkloadProfile({
        **profile.value,
        "access": {**profile.value["access"], "credentials_embedded": False},
    })
    assert renderer.access_context(profile, tenant_id=tenant) == ArtifactAccessContext(
        profile="academic", receipt_digest="sha256:" + PLATFORM_RECEIPT, tenant_id=tenant
    )


@pytest.mark.parametrize("model_id", ["alphafold3", "bindcraft"])
@pytest.mark.parametrize("tenant", ["kopra", "another-customer"])
def test_shared_scientific_queue_keeps_existing_platform_asset_namespace(model_id, tenant):
    contract = scheduling_with_academic_route()
    # The operator-owned queue route is shared. Model grants are checked by the
    # service, not encoded as a second customer allow-list in infrastructure.
    contract["local_queue_routes"]["academic-scientific"]["tenant_ids"] = []
    model_profile = {**profile_value(), "model_id": model_id}
    snapshot = SchedulingContractResolver(contract).freeze(
        service_class="customer-batch", model_id=model_id, tenant_id=tenant,
        profile=model_profile,
        plan=ScientificBatchPlan((ScientificStagePlan(stage_id="design"),)),
        workload_namespace="fs2-academic-poc",
    )
    assert snapshot.workload_namespace == snapshot.route_namespace == "fs2-academic-poc"
    assert snapshot.stage("design").resolved_local_queue == "academic-scientific"
    assert snapshot.stage("design").resolved_cluster_queue == "inference-accelerators"


@pytest.mark.asyncio
async def test_customers_share_execution_but_keep_distinct_operations_inputs_and_usage_owners(registry, cipher, hasher):
    runtime, _, repository, _ = shared_runtime(registry, cipher, hasher)
    service = runtime.scientific_batches
    accepted = []
    for tenant in ("kopra", "another-customer"):
        _, identity = await issue(runtime, tenant)
        pointer = service.artifacts.add(tenant)
        for surface in ("http", "mcp"):
            assert [row["model_id"] for row in service.discover(identity, surface=surface)["data"]] == [MODEL]
        result = await service.submit(
            principal=identity, model_id=MODEL, request=request(pointer), idempotency_key="same-customer-request"
        )
        operation_id = UUID(result["operation"]["id"])
        operation = await runtime.store.get_operation(operation_id)
        state = repository.records[operation_id]
        assert (operation.tenant_id, operation.principal_id, operation.token_id) == (
            tenant, identity.principal_id, identity.token_id
        )
        assert state.tenant_id == state.access_context.tenant_id == tenant
        assert state.input_artifact_id == UUID(pointer["artifact_id"])
        assert state.access_context.receipt_digest == "sha256:" + PLATFORM_RECEIPT
        assert state.model_id == MODEL
        assert state.scheduling.workload_namespace == "fs2-models"
        assert state.execution_plan.stage_bindings[0].image == "registry.example/protein@sha256:" + "a" * 64
        accepted.append((identity, operation_id, pointer))
    assert accepted[0][1] != accepted[1][1]
    assert len(runtime.store.operations) == 2
    with pytest.raises(NotFoundError):
        await service.status(accepted[0][1], principal=accepted[1][0])
    with pytest.raises(ArtifactNotFoundError):
        await service.submit(
            principal=accepted[1][0], model_id=MODEL, request=request(accepted[0][2]),
            idempotency_key="foreign-input-rejected",
        )
    assert len(runtime.store.operations) == 2
    # Both durable plans target the same platform model/queue, without changing
    # either customer owner into the platform's historical asset owner.
    assert {state.scheduling.tenant_queue for state in repository.records.values()} == {"scientific"}


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant", ["operator-assets", "kopra"])
@pytest.mark.parametrize("restriction", ["model", "invoke-scope"])
async def test_matching_asset_owner_is_not_a_model_grant(registry, cipher, hasher, tenant, restriction):
    runtime, _, repository, _ = shared_runtime(registry, cipher, hasher)
    service = runtime.scientific_batches
    _, identity = await issue(
        runtime, tenant,
        models={"other-model"} if restriction == "model" else {MODEL},
        scopes=SCOPES - {Scope.INFERENCE_INVOKE} if restriction == "invoke-scope" else SCOPES,
    )
    for surface in ("http", "mcp"):
        assert service.discover(identity, surface=surface)["data"] == []
    with pytest.raises(PermissionError):
        await service.submit(principal=identity, model_id=MODEL, request={}, idempotency_key="denied-model-0001")
    assert not repository.records


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "restriction", ["missing-receipt", "wrong-platform-profile", "revoked", "embedded-credentials"]
)
async def test_key_grant_does_not_override_missing_or_revoked_platform_assets(registry, cipher, hasher, restriction):
    runtime, _, repository, _ = shared_runtime(registry, cipher, hasher)
    service = runtime.scientific_batches
    _, identity = await issue(runtime, "kopra")
    pointer = service.artifacts.add("kopra")
    if restriction == "missing-receipt":
        service.execution_binding.academic_authorization_receipt_sha256 = None
    elif restriction == "wrong-platform-profile":
        service.execution_binding.access_profiles = {MODEL: "public"}
    else:
        profile = service.profiles.get(MODEL)
        profile.value["access"]["state" if restriction == "revoked" else "credentials_embedded"] = (
            "revoked" if restriction == "revoked" else True
        )
    for surface in ("http", "mcp"):
        assert service.discover(identity, surface=surface)["data"] == []
    with pytest.raises(ScientificProfileError):
        await service.submit(
            principal=identity, model_id=MODEL, request=request(pointer), idempotency_key="platform-unavailable-0001"
        )
    assert not repository.records


@pytest.mark.asyncio
async def test_clone_grant_is_exact_and_does_not_grant_source_or_sibling(registry, cipher, hasher):
    runtime, _, repository, _ = shared_runtime(registry, cipher, hasher)
    service = runtime.scientific_batches
    inventory, apps = await _inventory(MODEL)
    service.profiles = AppScientificProfiles(service.profiles, inventory)
    service.execution_binding = AppScientificExecution(service.execution_binding, inventory)
    service.plan_factory = AppScientificExecution(service.plan_factory, inventory)
    service.scheduling = AppScientificScheduling(service.scheduling, inventory)
    app_id = apps[0].public_model_id
    _, identity = await issue(runtime, "kopra", models={app_id})
    pointer = service.artifacts.add("kopra")
    for surface in ("http", "mcp"):
        assert [row["model_id"] for row in service.discover(identity, surface=surface)["data"]] == [app_id]
    accepted = await service.submit(
        principal=identity, model_id=app_id, request=request(pointer), idempotency_key="granted-clone-0001"
    )
    assert repository.records[UUID(accepted["operation"]["id"])].tenant_id == "kopra"
    for denied_id in (MODEL, apps[1].public_model_id):
        with pytest.raises(PermissionError):
            await service.submit(
                principal=identity, model_id=denied_id, request=request(pointer), idempotency_key="denied-clone-0001"
            )
    _, source_only = await issue(runtime, "kopra", models={MODEL})
    with pytest.raises(PermissionError):
        await service.submit(
            principal=source_only, model_id=app_id, request=request(pointer), idempotency_key="source-not-clone-0001"
        )
    assert len(repository.records) == 1


@pytest.mark.asyncio
async def test_shared_academic_http_and_mcp_use_ordinary_key_grants(registry, cipher, hasher):
    runtime, _, repository, _ = shared_runtime(registry, cipher, hasher)
    service = runtime.scientific_batches
    pointer = service.artifacts.add("kopra")
    issued, _ = await issue(runtime, "kopra")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime)),
        base_url=runtime.settings.public_origin(),
        headers={"authorization": f"Bearer {issued.token}"},
    ) as client:
        discovered = await client.get("/v1/scientific-models")
        assert discovered.status_code == 200
        assert [row["model_id"] for row in discovered.json()["data"]] == [MODEL]
        result = await client.post(
            f"/v1/models/{MODEL}:submit", json=request(pointer),
            headers={"idempotency-key": "shared-http-0001"},
        )
        assert result.status_code == 202
    async with scientific_client(runtime, tenant="kopra") as client:
        discovered = _mcp_result(await client.call_tool("list_scientific_models", {}))
        assert [row["model_id"] for row in discovered["data"]] == [MODEL]
        arguments = {"request": request(pointer), "idempotency_key": "shared-mcp-0001"}
        result = _mcp_result(await client.call_tool("submit_protein_design", arguments))
        replay = _mcp_result(await client.call_tool("submit_scientific_run", {**arguments, "model_id": MODEL}))
        assert result["operation"]["id"] == replay["operation"]["id"]
        assert replay["operation"]["reused"] is True
    assert len(repository.records) == 2
    assert {record.tenant_id for record in repository.records.values()} == {"kopra"}

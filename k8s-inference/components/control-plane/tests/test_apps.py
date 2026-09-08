from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from test_dynamic_routes import _revision
from test_model_deployment import envelope, model_spec, render_context, renderer
from test_model_deployment_mutation import FakeWriter, _actor
from test_model_deployment_publication import status_view

from fs2_serve.admin import AdminProblemError, AdminReadService
from fs2_serve.admin_models import AdminContext
from fs2_serve.apps import AppsService, default_app_id
from fs2_serve.apps_models import AppCreate, AppRecord, AppSettingsUpdate
from fs2_serve.apps_repository import AppConflictError, MemoryAppsRepository
from fs2_serve.apps_templates import instantiate_app_template
from fs2_serve.memory_store import MemoryStore
from fs2_serve.model_deployment import AppDeploymentIdentity, DesiredState, canonical_digest, spec_digest
from fs2_serve.model_deployment_admin import StoreModelDeploymentRepository
from fs2_serve.model_deployment_mutation import ModelDeploymentMutationService
from fs2_serve.model_deployment_publication import project_dynamic_publications
from fs2_serve.model_deployment_records import ModelDeploymentAppendRequest, ModelDeploymentRevisionAction


def _context():
    now = datetime.now(UTC)
    return AdminContext(from_at=now - timedelta(days=1), to_at=now + timedelta(seconds=1), timezone="UTC")


def _record(*, public_id="qwen.3-8b", name="qwen-live"):
    now = datetime.now(UTC)
    return AppRecord(
        app_id=default_app_id(public_id),
        model_ref="qwen.3-8b",
        public_model_id=public_id,
        display_name="Qwen",
        execution_mode="serving",
        namespace="fs2-models",
        deployment_name=name,
        created_at=now,
        updated_at=now,
    )


def _app_spec(source):
    app_id = uuid4()
    return source.model_copy(
        update={
            "app": AppDeploymentIdentity(app_id=app_id, public_model_id=f"app-{app_id.hex}"),
            "exposure": source.exposure.model_copy(
                update={
                    "open_ai_aliases": [],
                    "mcp_tool_name": f"app_{app_id.hex}",
                }
            ),
        }
    )


def test_legacy_spec_digest_unchanged_when_app_identity_absent():
    spec = model_spec()
    payload = spec.model_dump(mode="json", by_alias=True)
    del payload["app"]
    del payload["fastStart"]
    del payload["cache"]["mechanism"]
    del payload["availability"]["startupTimeoutSeconds"]
    payload["placement"]["poolRefs"] = sorted(payload["placement"]["poolRefs"])
    payload["exposure"]["openAIAliases"] = sorted(payload["exposure"]["openAIAliases"])
    payload["policy"]["allowedPrincipalIds"] = sorted(payload["policy"]["allowedPrincipalIds"])
    assert spec_digest(spec) == canonical_digest(payload)


def test_two_apps_render_independent_names_selectors_metrics_and_shared_artifact():
    source = model_spec()
    specs = [_app_spec(source), _app_spec(source)]
    plans = [
        renderer().render(spec, render_context().model_copy(update={"name": spec.public_model_id})) for spec in specs
    ]
    names = [{(item.kind, item.name) for item in plan.resources} for plan in plans]
    assert not names[0] & names[1]
    for spec, plan in zip(specs, plans, strict=True):
        deployments = [item.manifest for item in plan.resources if item.kind == "Deployment"]
        services = [item.manifest for item in plan.resources if item.kind == "Service"]
        assert deployments and services
        for workload in deployments:
            labels = workload["spec"]["template"]["metadata"]["labels"]
            assert labels["fs2-serve.nebius.ai/app-id"] == str(spec.app.app_id)
            assert labels["fs2-serve.nebius.ai/model-id"] == spec.public_model_id
            assert "app" not in labels  # Old Service selectors must not capture new app Pods.
            assert labels.items() >= workload["spec"]["selector"]["matchLabels"].items()
            volumes = workload["spec"]["template"]["spec"]["volumes"]
            assert any(value.get("persistentVolumeClaim", {}).get("claimName") == "qwen-cache-rwx" for value in volumes)
        assert services[0]["spec"]["selector"] == {"fs2-serve.nebius.ai/model-deployment": spec.public_model_id}
        queries = [
            trigger["metadata"]["query"]
            for item in plan.resources
            if item.kind == "ScaledObject"
            for trigger in item.manifest["spec"]["triggers"]
        ]
        assert any(f'model="{spec.public_model_id}"' in query for query in queries)
        assert all('model="qwen.3-8b"' not in query for query in queries)


def test_template_rewrites_owned_references_not_runtime_model_arguments():
    bundle = next(iter(renderer()._bundles.values())).model_copy(deep=True)
    runtime = bundle.resources[0]["spec"]["template"]["spec"]["containers"][0]
    runtime["args"] = ["--served-model-name", "qwen-runtime"]
    runtime["env"] = [
        {"name": "UPSTREAM", "value": "http://qwen-runtime.fs2-models.svc:8000/v1"},
        {"name": "CONFIG", "valueFrom": {"configMapKeyRef": {"name": "qwen-config", "key": "config"}}},
    ]
    bundle.resources.append(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "qwen-config", "namespace": "fs2-models"},
            "data": {"model": "qwen-runtime", "upstream": "http://qwen-runtime:8000"},
        }
    )
    spec = _app_spec(model_spec())
    clone = instantiate_app_template(bundle, spec.public_model_id, identity=spec.app)
    cloned_runtime = clone.resources[0]["spec"]["template"]["spec"]["containers"][0]
    config = next(item for item in clone.resources if item["kind"] == "ConfigMap")
    assert cloned_runtime["args"] == runtime["args"]
    assert config["data"]["model"] == "qwen-runtime"
    assert cloned_runtime["env"][0]["value"] == f"http://{clone.primary_service_name}.fs2-models.svc:8000/v1"
    assert config["data"]["upstream"] == f"http://{clone.primary_service_name}:8000"
    assert cloned_runtime["env"][1]["valueFrom"]["configMapKeyRef"]["name"] == config["metadata"]["name"]
    assert bundle.resources[0]["metadata"]["name"] == "qwen-runtime"


def test_two_apps_publish_independent_routes_and_restore_exact_dispatch(registry):
    original = _revision(registry)
    revisions = []
    for _index in range(2):
        spec = _app_spec(original.spec)
        revisions.append(
            original.model_copy(
                update={
                    "name": spec.public_model_id,
                    "spec": spec,
                    "etag": spec_digest(spec),
                }
            )
        )
    snapshot = project_dynamic_publications(
        revisions, {(revision.namespace, revision.name): status_view(revision) for revision in revisions}
    )
    registry.set_dynamic_publications(snapshot, valid_until=datetime.now(UTC) + timedelta(minutes=10))
    assert registry.get("qwen3-8b").id == "qwen3-8b"  # Existing route survives new app publication.
    for revision in revisions:
        model = registry.get(revision.spec.public_model_id)
        assert model.id == revision.spec.public_model_id
        assert model.dynamic_policy.publication.source_model_ref == "qwen3-8b"
        payload = registry.dispatch_snapshot(model)
        restored = registry.restore_dispatch_snapshot(payload, model_id=model.id, revision=model.model_revision)
        assert restored.id == model.id
        assert restored.binding.service_origin == model.binding.service_origin
    disabled = revisions[0].spec.model_copy(
        update={
            "lifecycle": revisions[0].spec.lifecycle.model_copy(update={"desired_state": DesiredState.DISABLED}),
            "availability": revisions[0].spec.availability.model_copy(update={"min_replicas": 0}),
        }
    )
    revisions[0] = revisions[0].model_copy(update={"spec": disabled, "etag": spec_digest(disabled)})
    registry.set_dynamic_publications(
        project_dynamic_publications(
            revisions, {(revision.namespace, revision.name): status_view(revision) for revision in revisions}
        ),
        valid_until=datetime.now(UTC) + timedelta(minutes=10),
    )
    assert registry.get(revisions[1].spec.public_model_id).enabled
    assert registry.get("qwen3-8b").enabled
    assert revisions[0].spec.public_model_id not in {item.id for item in registry.list(enabled_only=True)}


@pytest.mark.asyncio
async def test_app_admission_preserves_public_identity_but_sends_canonical_openai_model(registry, cipher, hasher):
    from test_admission_workers import service
    from test_dynamic_routes import _principal

    from fs2_serve.models import AdmissionRequest, Scope, TokenCreate
    from fs2_serve.runtime import RuntimeClient

    original = _revision(registry)
    spec = _app_spec(original.spec)
    revision = original.model_copy(update={"name": spec.public_model_id, "spec": spec, "etag": spec_digest(spec)})
    registry.set_dynamic_publications(
        project_dynamic_publications([revision], {(revision.namespace, revision.name): status_view(revision)}),
        valid_until=datetime.now(UTC) + timedelta(minutes=10),
    )
    store = MemoryStore(cipher, hasher)
    principal = _principal()
    await store.model_deployment_append_revision(
        ModelDeploymentAppendRequest(
            namespace=revision.namespace,
            name=revision.name,
            expected_etag=None,
            spec=spec,
            action=ModelDeploymentRevisionAction.CREATE,
            actor_id=uuid4(),
            actor="operator",
            idempotency_key="app-http-canonical-create",
        )
    )
    await store.issue_token(
        token_id=principal.token_id,
        prefix=principal.token_prefix,
        pepper_key_id="test",
        digest="test",
        request=TokenCreate(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            scopes={Scope.INFERENCE_INVOKE},
            models={spec.public_model_id},
            max_concurrency=8,
        ),
        created_by="test",
    )
    requests = []

    async def handler(request):
        requests.append(request)
        assert json.loads(request.content)["model"] == "qwen3-8b"
        assert spec.public_model_id in str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        runtime = RuntimeClient(
            activation_timeout_seconds=2, runtime_timeout_seconds=2, max_response_bytes=4096, client=client
        )
        admitted = await service(registry, store, runtime).admit(
            principal,
            AdmissionRequest(
                model_id=spec.public_model_id,
                operation="chat",
                protocol="openai-chat",
                idempotency_key="app-http-canonical-submit",
                request_body=json.dumps(
                    {
                        "model": spec.public_model_id,
                        "messages": [{"role": "user", "content": "test"}],
                    }
                ).encode(),
            ),
        )
        assert admitted.model_id == spec.public_model_id
        claimed = await store.claim_operation("app-worker", lease_seconds=30)
        assert claimed is not None and claimed.model_id == spec.public_model_id
        payload = await store.read_request_payload(
            claimed.id, worker_id="app-worker", fencing_token=claimed.fencing_token
        )
        result = await runtime.invoke(registry.get(spec.public_model_id), claimed, payload)
        assert result.status_code == 200 and len(requests) == 1
        assert requests[0].headers["x-fs2-operation-id"] == str(admitted.id)


@pytest.mark.asyncio
async def test_app_drain_and_automatic_cache_history_use_its_own_public_route():
    from test_fast_start import with_fast_start
    from test_model_deployment_controller import FakeApi, controller, fence, model_object

    from fs2_serve.model_deployment_controller import Discovery, ModelKey

    calls = []

    class OwnTraffic:
        async def active_operations(self, *, tenant_id, model_ref):
            calls.append(("drain", model_ref))
            return 0

        async def fast_start_history(self, *, model_ref, idle_seconds, now):
            calls.append(("history", model_ref))
            return None

    spec = with_fast_start(_app_spec(model_spec()), mode="Automatic")
    raw = model_object()
    raw["spec"] = spec.model_dump(mode="json", by_alias=True)
    subject = controller(FakeApi(raw), active_operations=OwnTraffic())
    await subject._drain_observation(spec, Discovery(complete=True, resources=[], pods=[]))
    await subject.reconcile(ModelKey(namespace="fs2-models", name="qwen-live"), fence())
    assert ("drain", spec.public_model_id) in calls
    assert ("history", spec.public_model_id) in calls
    assert all(model_id == spec.public_model_id for _, model_id in calls)


@pytest.mark.asyncio
async def test_create_only_metadata_and_optimistic_independent_edits():
    repository = MemoryAppsRepository()
    original = _record()
    await repository.seed(original)
    edited = await repository.update(original.model_copy(update={"display_name": "Edited"}), expected_revision=1)
    assert (await repository.seed(original)).display_name == "Edited"
    assert (await repository.get(original.app_id)).revision == 2
    with pytest.raises(AppConflictError):
        await repository.update(original, expected_revision=1)
    assert edited.model_ref == original.model_ref


@pytest.mark.asyncio
async def test_seed_defaults_preserves_existing_clone_identity_and_edited_metadata(registry, cipher, hasher):
    from test_scientific_admin import _readiness

    from fs2_serve.scientific_admin import ScientificModelSnapshot
    from fs2_serve.scientific_admin_models import ScientificModelReadinessList

    repository = MemoryAppsRepository()
    app_id = uuid4()
    clone = _record(public_id=f"app-{app_id.hex}", name=None).model_copy(
        update={
            "app_id": app_id,
            "model_ref": "rfdiffusion",
            "execution_mode": "scientific",
            "display_name": "Customer-specific RF",
            "academic_required": True,
        }
    )
    await repository.seed(clone)
    await repository.update(clone.model_copy(update={"display_name": "Operator edited"}), expected_revision=1)

    class Models:
        async def list_models(self, *, tenant_id=None):
            return ScientificModelSnapshot(
                data=ScientificModelReadinessList(
                    items=[
                        _readiness("rfdiffusion"),
                        _readiness(clone.public_model_id),
                    ]
                ),
                observed_at=datetime.now(UTC),
            )

    store = MemoryStore(cipher, hasher)
    service = AppsService(
        repository=repository,
        registry=registry,
        admin=AdminReadService(store=store, registry=registry),
        scientific=SimpleNamespace(models=Models()),
    )
    await service.seed_defaults()
    await service.seed_defaults()  # Another API process/restart sees the same rows.
    records = await repository.list_records()
    matching = [item for item in records if item.public_model_id == clone.public_model_id]
    assert len(matching) == 1
    assert matching[0].app_id == app_id
    assert matching[0].model_ref == "rfdiffusion"
    assert matching[0].display_name == "Operator edited"
    assert matching[0].academic_required is True
    assert matching[0].revision == 2
    assert {item.id for item in registry.list()} <= {item.public_model_id for item in records}


@pytest.mark.asyncio
async def test_last_used_is_lifetime_while_operation_count_is_windowed(registry, cipher, hasher):
    old = datetime.now(UTC) - timedelta(days=5)

    class Repository(MemoryAppsRepository):
        async def usage(self, model_id, context, tenant_id):
            return {"logical_runs": 0, "succeeded_runs": 0, "failed_runs": 0, "active_runs": 0}

        async def last_used(self, model_id, tenant_id):
            return old

    repository = Repository()
    record = _record(public_id="qwen3-8b", name=None)
    await repository.seed(record)
    store = MemoryStore(cipher, hasher)
    service = AppsService(
        repository=repository, registry=registry, admin=AdminReadService(store=store, registry=registry)
    )
    summary = await service.summary(record, _context(), None)
    assert summary.logical_run_count == 0
    assert summary.last_used_at == old


@pytest.mark.asyncio
async def test_real_mutation_service_creates_independent_app_and_updates_only_its_spec(registry, cipher, hasher):
    store = MemoryStore(cipher, hasher, payload_ttl_seconds=3600)
    actor = _actor()
    writer = FakeWriter()
    deployments = ModelDeploymentMutationService(
        repository=StoreModelDeploymentRepository(store),
        writer=writer,
        envelope=envelope(),
        renderer=renderer(),
        prometheus_server_address="http://prometheus.fs2-observability.svc:9090",
    )
    await deployments.repository.append_revision(
        ModelDeploymentAppendRequest(
            namespace="fs2-models",
            name="qwen-live",
            expected_etag=None,
            spec=model_spec(),
            action=ModelDeploymentRevisionAction.CREATE,
            actor_id=actor.id,
            actor=actor.subject,
            idempotency_key="apps-source-create",
        )
    )
    repository = MemoryAppsRepository()
    await repository.seed(_record())
    service = AppsService(
        repository=repository,
        registry=registry,
        admin=AdminReadService(store=store, registry=registry),
        deployments=deployments,
    )
    created = await service.create(AppCreate(model_ref="qwen.3-8b", display_name="Second app"), actor, _context())
    assert created.app_id != _record().app_id
    assert created.public_model_id != created.model_ref
    assert created.capabilities.duplicate
    assert len(writer.writes) == 1
    settings = await service.settings(created.app_id, _context())
    assert settings.serving.spec.availability.min_replicas == 0
    assert settings.serving.spec.artifact == model_spec().artifact
    new_spec = settings.serving.spec.model_copy(
        update={
            "availability": settings.serving.spec.availability.model_copy(update={"max_replicas": 2}),
        }
    )
    saved = await service.update_settings(
        created.app_id,
        AppSettingsUpdate(
            expected_app_revision=1,
            display_name="Renamed second app",
            serving_spec=new_spec,
            serving_base_etag=settings.serving.etag,
        ),
        actor,
        _context(),
    )
    assert saved.serving.spec.availability.max_replicas == 2
    assert saved.app_revision == 2
    source = await service.settings(_record().app_id, _context())
    assert source.serving.spec == model_spec()
    assert source.app_revision == 1
    choices = await service.choices()
    assert {item.public_model_id for item in choices} == {created.public_model_id, "qwen.3-8b"}
    target = await service.resolve_observability(str(created.app_id))
    assert target.model_id == created.public_model_id
    assert target.pod_labels["fs2-serve.nebius.ai/model-deployment"] == created.deployment_name
    with pytest.raises(AdminProblemError, match="reload"):
        await service.update_settings(created.app_id, AppSettingsUpdate(expected_app_revision=1), actor, _context())

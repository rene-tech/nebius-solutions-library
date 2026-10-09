"""Signed metadata-only access never confers batch admission or upload authority."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import CATALOG_ROOT
from fs2_serve_catalog.artifacts import canonical_bytes
from fs2_serve_catalog.loader import CatalogError
from test_medical_batch_demo import BASE, MODEL, NOW, principal
from test_medical_batch_demo import medical as medical_fixture

from fs2_serve.api import _model_view
from fs2_serve.native_serverless import (
    ENTRY_SCHEMA,
    DeploymentSet,
    NativeServerlessError,
    _digest,
    bind_native_serverless,
    signed_subject,
)
from fs2_serve.scientific_input_uploads import ScientificInputUploadRequest, ScientificInputUploadService

DISCOVERY = {"tenant_id": "academic-test", "principal_id": "website-reader"}


@pytest.fixture(name="medical")
def reused_medical(tmp_path):
    return medical_fixture.__wrapped__(tmp_path)


def academic(**changes):
    return principal(**{
        **DISCOVERY, "models": frozenset({"*"}),
        "scopes": frozenset({"catalog.read", "mcp.invoke", "inference.invoke"}), **changes,
    })


def discovery_registry(s):
    doc = copy.deepcopy(s.document)
    doc["models"][MODEL]["discovery_access"] = [dict(DISCOVERY)]
    s.route.write_text(json.dumps(s.sign(doc)))
    assert s.registry.revalidate()
    return s.registry


@pytest.mark.parametrize("explicit_empty", [False, True])
def test_old_subject_bytes_and_original_signature_survive_empty_discovery(medical, explicit_empty):
    doc = copy.deepcopy(medical.document)
    # Independently reconstruct the pre-extension subject from the original
    # closed raw fixture, not from the new signed_subject implementation.
    legacy_subject = {
        "schema": ENTRY_SCHEMA, "model_id": MODEL,
        "gateway_service": copy.deepcopy(doc["gateway_service"]),
        "deployment": {key: value for key, value in doc["models"][MODEL].items() if key != "attestation"},
    }
    original_signed = json.loads(medical.route.read_bytes())
    original_attestation = copy.deepcopy(original_signed["models"][MODEL]["attestation"])
    assert original_attestation["subject"]["digest"] == _digest(legacy_subject)
    if explicit_empty:
        original_signed["models"][MODEL]["discovery_access"] = []
    typed = DeploymentSet.model_validate(original_signed)
    assert canonical_bytes(signed_subject(MODEL, typed, typed.models[MODEL])) == canonical_bytes(legacy_subject)
    # Do not re-sign after adding an explicit empty field.
    medical.route.write_text(json.dumps(original_signed))
    gateway, policies = bind_native_serverless(
        medical.gateway, medical.catalog, medical.route, catalog_dir=CATALOG_ROOT,
        trusted_attestors=medical.trust, validation_time=NOW,
    )
    assert gateway.model(MODEL).routable
    assert policies[MODEL].discovery_identities == frozenset()
    assert original_signed["models"][MODEL]["attestation"] == original_attestation


def test_signed_nonempty_discovery_is_catalog_only_even_with_wildcard_and_invoke_scopes(medical):
    registry = discovery_registry(medical)
    caller, model = academic(), registry.get(MODEL)
    assert model in registry.allowed_for_principal(caller, surface="catalog")
    registry.authorize_principal(model, caller, requested_model_id=MODEL, surface="catalog")
    assert model.qualification_policy.discovery_identities == frozenset({tuple(DISCOVERY.values())})
    assert not model.qualification_policy.permits(caller.tenant_id, caller.principal_id)
    with pytest.raises(PermissionError):
        registry.authorize_qualification_dispatch(model, caller.tenant_id, caller.principal_id)
    public = json.dumps(_model_view(model))
    assert DISCOVERY["tenant_id"] not in public and DISCOVERY["principal_id"] not in public


@pytest.mark.parametrize("surface", ["native", "mcp", "openai", "unknown"])
def test_metadata_reader_cannot_discover_or_authorize_any_invocation_surface(medical, surface):
    registry = discovery_registry(medical)
    caller = academic()
    assert MODEL not in {model.id for model in registry.allowed_for_principal(caller, surface=surface)}
    with pytest.raises(PermissionError):
        registry.authorize_principal(registry.get(MODEL), caller, requested_model_id=MODEL, surface=surface)


@pytest.mark.parametrize("changes", [
    {"tenant_id": "other"}, {"principal_id": "other"},
    {"tenant_id": "demo-a"}, {"principal_id": "presenter-a"},
    {"models": frozenset()}, {"models": frozenset({BASE})},
])
def test_metadata_identity_pair_and_normal_model_grant_both_required(medical, changes):
    registry = discovery_registry(medical)
    caller = academic(**changes)
    assert MODEL not in {model.id for model in registry.allowed_for_principal(caller, surface="catalog")}
    with pytest.raises(PermissionError):
        registry.authorize_principal(registry.get(MODEL), caller, requested_model_id=MODEL, surface="catalog")


@pytest.mark.parametrize("change", ["insert_unsigned", "modify_signed", "remove_signed", "duplicate_signed"])
def test_nonempty_metadata_access_is_signed_and_duplicates_rejected(medical, change):
    doc = copy.deepcopy(medical.document)
    if change != "insert_unsigned":
        doc["models"][MODEL]["discovery_access"] = [dict(DISCOVERY)]
    if change == "duplicate_signed":
        doc["models"][MODEL]["discovery_access"] *= 2
    doc = medical.sign(doc)
    if change == "insert_unsigned":
        doc["models"][MODEL]["discovery_access"] = [dict(DISCOVERY)]
    elif change == "modify_signed":
        doc["models"][MODEL]["discovery_access"][0]["tenant_id"] = "other"
    elif change == "remove_signed":
        del doc["models"][MODEL]["discovery_access"]
    medical.route.write_text(json.dumps(doc))
    with pytest.raises((NativeServerlessError, CatalogError)):
        bind_native_serverless(
            medical.gateway, medical.catalog, medical.route, catalog_dir=CATALOG_ROOT,
            trusted_attestors=medical.trust, validation_time=NOW,
        )


@pytest.mark.parametrize("tenant,name", [("demo-a", "presenter-a"), ("demo-b", "presenter-b")])
def test_presenter_invocation_and_current_dispatch_unchanged(medical, tenant, name):
    registry = discovery_registry(medical)
    caller, model = principal(tenant_id=tenant, principal_id=name), registry.get(MODEL)
    for surface in ("catalog", "native", "mcp"):
        assert model in registry.allowed_for_principal(caller, surface=surface)
        registry.authorize_principal(model, caller, requested_model_id=MODEL, surface=surface)
    registry.authorize_qualification_dispatch(model, tenant, name)
    assert model.max_attempts == 1


def upload_request(model=MODEL):
    return ScientificInputUploadRequest(model_id=model, sha256="7" * 64, size_bytes=44, media_type="audio/wav")


@pytest.mark.asyncio
async def test_discovery_upload_denied_before_operation_append_or_any_artifact_call(medical):
    registry = discovery_registry(medical)
    store, artifacts = AsyncMock(), Mock()
    profiles = SimpleNamespace(get=Mock(side_effect=KeyError("not-scientific")))
    service = ScientificInputUploadService(store=store, artifacts=artifacts, profiles=profiles, registry=registry)
    with pytest.raises(PermissionError):
        await service.begin(principal=academic(), request=upload_request(), idempotency_key="metadata-cannot-upload")
    assert store.mock_calls == [] and artifacts.mock_calls == []
    profiles.get.assert_called_once_with(MODEL, runnable=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["presenter", "cold-serving", "scientific"])
async def test_original_upload_paths_still_reach_store_without_new_qualification_requirement(medical, case):
    class ReachedStoreError(Exception):
        pass

    registry = discovery_registry(medical)
    caller = principal(models=frozenset({"*"}))
    model = BASE if case == "cold-serving" else MODEL
    profiles = SimpleNamespace(get=Mock(return_value=object()))
    if case != "scientific":
        profiles.get.side_effect = KeyError("not-scientific")
    else:
        registry = Mock()
        registry.get.side_effect = AssertionError("scientific profile must not use the fallback serving path")
    store, artifacts = AsyncMock(), Mock()
    store.append_operation.side_effect = ReachedStoreError
    service = ScientificInputUploadService(store=store, artifacts=artifacts, profiles=profiles, registry=registry)
    with pytest.raises(ReachedStoreError):
        await service.begin(principal=caller, request=upload_request(model), idempotency_key="existing-upload-path")
    store.append_operation.assert_awaited_once()
    assert artifacts.mock_calls == []


@pytest.mark.asyncio
async def test_direct_native_admission_denies_catalog_reader_without_storage_or_worker_calls(medical):
    from test_admission_workers import service

    from fs2_serve.models import AdmissionRequest

    registry = discovery_registry(medical)
    store, runtime = AsyncMock(), AsyncMock()
    admission = service(registry, store, runtime)
    with pytest.raises(PermissionError):
        await admission.admit(academic(), AdmissionRequest(
            model_id=MODEL, operation="transcribe", protocol="native", idempotency_key="metadata-cannot-infer",
            request_body=b'{}',
        ))
    assert not store.mock_calls and not runtime.mock_calls

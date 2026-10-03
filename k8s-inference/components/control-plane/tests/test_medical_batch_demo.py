"""Offline medical-only batch adapter contracts; synthetic signatures, no live proof."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from conftest import CATALOG_ROOT, REPO_ROOT
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fs2_serve_catalog.attestations import create_signed_attestation, public_key_id, public_key_value
from fs2_serve_catalog.consumer import SERVING_BINDINGS_SCHEMA, ServingBindings, bind_gateway_catalog
from fs2_serve_catalog.loader import CatalogError, load_catalog

from fs2_serve.api import _model_view
from fs2_serve.apps import AppsService, default_app_id
from fs2_serve.apps_models import AppRecord
from fs2_serve.apps_repository import MemoryAppsRepository
from fs2_serve.model_input_contracts import contract_for
from fs2_serve.models import AdmissionRequest, Principal
from fs2_serve.native_catalog import augment_native_catalog
from fs2_serve.native_serverless import (
    ENTRY_SCHEMA,
    SCHEMA,
    DeploymentSet,
    NativeServerlessError,
    _digest,
    bind_native_serverless,
    signed_subject,
    validate_worker_checkpoint,
)
from fs2_serve.registry import Registry
from fs2_serve.speech_stream import speech_stream_router

MODEL = "nemotron-speech-en-medical-0-6b"
BASE = "nemotron-speech-en-0-6b"
SORT = "diar-streaming-sortformer-4spk-v2-1"
CHECKPOINT = "2a2b1cae8e96d62e83a82351f7d483df01fc28d1d64793ce45a5de514a6c3b5f"
NOW = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def medical(tmp_path):
    """One non-deployable signed medical route, using the real catalog contract."""
    archive = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
    catalog = augment_native_catalog(archive, CATALOG_ROOT, repo_root=REPO_ROOT)
    gateway = bind_gateway_catalog(catalog, ServingBindings(archive.digest, MappingProxyType({})))
    record = catalog.model(MODEL)
    value = record.to_dict()
    key = Ed25519PrivateKey.generate()
    trust = {public_key_id(key.public_key()): public_key_value(key.public_key())}
    document = {
        "schema": SCHEMA,
        "session_id": digest("synthetic-medical-batch-session"),
        "gateway_service": {
            "namespace": "medical-demo-test", "service_name": "demo-gateway",
            "service_uid": str(uuid4()), "port": 8080,
        },
        "models": {MODEL: {
            "model_digest": record.digest,
            "variant_id": catalog.variants_for(MODEL)[0].variant_id,
            "artifact_manifest_sha256": value["cache"]["artifact"]["manifest_digest"],
            "discovery_service": {
                "namespace": "fs2-models", "service_name": MODEL,
                "service_uid": str(uuid4()), "port": 443,
            },
            "endpoint": {
                "provider": "nebius-serverless", "project_id": "project-synthetic",
                "endpoint_id": "aiendpoint-synthetic", "origin": "https://worker.example.invalid",
                "runtime_image_digest": value["runtime"]["image"]["digest"],
                "checkpoint_sha256": CHECKPOINT, "cloud_observation_sha256": digest("synthetic-cloud"),
            },
            "credential": {
                "requirement_id": "fs2-models/medical-test-worker", "secret_uid": str(uuid4()),
                "gateway_secret_uid": str(uuid4()), "resource_version": "1",
                "observation_sha256": digest("synthetic-secret-metadata"),
            },
            "trust_bundle_sha256": digest("synthetic-ca"), "region": "eu-north2",
            "native_fixture_receipt_sha256": digest("synthetic-fixture"),
            "file_live_parity_receipt_sha256": digest("synthetic-parity-not-live-evidence"),
            "qualification_access": [
                {"tenant_id": "demo-a", "principal_id": "presenter-a"},
                {"tenant_id": "demo-b", "principal_id": "presenter-b"},
            ],
            "mcp_tool_name": "infer_medical_demo_test_native",
            "mcp_description": "Synthetic named-demo batch route; not clinical validation.",
            "attestation": {},
        }},
    }

    def sign(doc):
        typed = DeploymentSet.model_validate(doc)
        subject_digest = _digest(signed_subject(MODEL, typed, typed.models[MODEL]))
        doc["models"][MODEL]["attestation"] = create_signed_attestation(
            private_key=key, session_id=doc["session_id"], nonce=digest("synthetic-one-route-nonce"),
            issued_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            expires_at=(NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            kind="native-serverless-deployment", subject_schema=ENTRY_SCHEMA,
            subject_digest=subject_digest, model_id=MODEL,
            claims={"deployment_sha256": subject_digest, "qualification_only": True,
                    "model_digest": record.digest,
                    "native_request_contract_sha256": catalog.semantic_request_contract(MODEL).digest},
        )
        return doc

    route = tmp_path / "synthetic-medical-route.json"
    route.write_text(json.dumps(sign(copy.deepcopy(document))))
    bindings = tmp_path / "empty-archive-bindings.json"
    bindings.write_text(json.dumps({
        "schema": SERVING_BINDINGS_SCHEMA, "catalog_digest": archive.digest, "bindings": {},
    }))
    registry = Registry.load(
        CATALOG_ROOT, bindings, repo_root=REPO_ROOT, evidence_root=None,
        native_serverless_deployments_file=route, trusted_attestors=trust,
        validation_time=NOW, max_attempts=8, max_gpu_seconds_per_attempt=60, retry_base_seconds=0.01,
    )
    return SimpleNamespace(archive=archive, catalog=catalog, gateway=gateway, registry=registry,
                           trust=trust, document=document, sign=sign, route=route)


def principal(**changes):
    return Principal(
        token_id=uuid4(), token_prefix="test", tenant_id="demo-a", principal_id="presenter-a",
        scopes=frozenset({"inference.invoke", "catalog.read", "mcp.invoke"}), models=frozenset({MODEL}),
    ).model_copy(update=changes)


def test_exact_medical_identity_is_additive_batch_candidate(medical):
    model = medical.registry.get(MODEL)
    value = medical.catalog.model(MODEL).to_dict()
    assert value["model"]["source"]["revision"] == "sha256:" + CHECKPOINT
    assert model.gateway.qualification["native_serverless"]["checkpoint_sha256"] == CHECKPOINT
    assert model.max_attempts == 1  # Native demo dispatch must not inherit the caller's eight attempts.
    assert model.enabled and not model.binding.ready
    contract = contract_for(model, "native")
    assert contract.model_ref == MODEL
    assert contract.input_schema["properties"]["options"]["properties"]["model"]["const"] == (
        "nemotron-speech-en-0.6b"
    )
    assert medical.catalog.digest == medical.archive.digest
    for name in medical.archive.records:
        assert medical.catalog.model(name).to_dict() == medical.archive.model(name).to_dict()
    # Base and Sort are existing native additions, not records in the archival digest.
    for name in (BASE, SORT):
        original = medical.gateway.model(name)
        current = medical.registry.get(name, require_enabled=False)
        assert current.gateway.to_dict() == original.to_dict()
        assert current.qualification_policy is None


@pytest.mark.parametrize("tenant,name", [("demo-a", "presenter-a"), ("demo-b", "presenter-b")])
def test_only_named_tenant_principal_pairs_with_normal_model_grant_can_use_batch(medical, tenant, name):
    caller = principal(tenant_id=tenant, principal_id=name)
    model = medical.registry.get(MODEL)
    assert [item.id for item in medical.registry.allowed_for_principal(caller, surface="native")] == [MODEL]
    medical.registry.authorize_principal(model, caller, requested_model_id=MODEL, surface="native")
    medical.registry.authorize_qualification_dispatch(model, tenant, name)


@pytest.mark.parametrize("changes", [
    {"tenant_id": "other"}, {"principal_id": "other"},
    {"tenant_id": "demo-b"}, {"principal_id": "presenter-b"},
    {"models": frozenset({BASE})}, {"models": frozenset()},
    {"tenant_id": "other", "models": frozenset({"*"})},
])
def test_cross_pair_and_missing_grant_are_not_discoverable_or_admissible(medical, changes):
    caller = principal(**changes)
    assert MODEL not in {m.id for m in medical.registry.allowed_for_principal(caller, surface="native")}
    with pytest.raises(PermissionError):
        medical.registry.authorize_principal(
            medical.registry.get(MODEL), caller, requested_model_id=MODEL, surface="native",
        )


def test_current_route_revocation_blocks_previously_selected_dispatch(medical):
    old = medical.registry.get(MODEL)
    medical.route.write_text("{}")
    assert medical.registry.revalidate()  # Optional route revocation is not a platform outage.
    assert medical.registry.validation_health()["healthy"] is True
    with pytest.raises(RuntimeError):
        medical.registry.authorize_qualification_dispatch(old, "demo-a", "presenter-a")


@pytest.mark.parametrize("changed", ["checkpoint", "image", "tampered"])
def test_signed_identity_fail_closed(medical, changed):
    doc = copy.deepcopy(medical.document)
    item = doc["models"][MODEL]
    if changed == "checkpoint":
        item["endpoint"]["checkpoint_sha256"] = digest("wrong-checkpoint")
    if changed == "image":
        item["endpoint"]["runtime_image_digest"] = "sha256:" + digest("wrong-image")
    doc = medical.sign(doc)
    if changed == "tampered":
        item["mcp_description"] += " unsigned change"
    medical.route.write_text(json.dumps(doc))
    with pytest.raises((NativeServerlessError, CatalogError)):
        bind_native_serverless(
            medical.gateway, medical.catalog, medical.route, catalog_dir=CATALOG_ROOT,
            trusted_attestors=medical.trust,
            validation_time=NOW,
        )


def test_durable_registration_does_not_expire_with_signing_evidence(medical):
    for when in (NOW + timedelta(hours=1), NOW + timedelta(days=3650)):
        assert medical.registry.revalidate(validation_time=when)
        model = medical.registry.get(MODEL)
        assert model.valid_at(when)
        assert model.binding.valid_until is None
        assert medical.registry.validation_health()["healthy"] is True


@pytest.mark.parametrize("failure", ["tampered", "missing", "malformed", "revoked-attestor"])
def test_optional_route_failure_preserves_unrelated_routes_and_recovers(medical, tmp_path, failure):
    from test_lean_routes import _route

    lean = tmp_path / "lean.json"
    lean.write_text(json.dumps(_route()))
    kwargs = dict(
        catalog_dir=CATALOG_ROOT, bindings_file=tmp_path / "empty-archive-bindings.json",
        repo_root=REPO_ROOT, evidence_root=None, lean_routes_file=lean,
        native_serverless_deployments_file=medical.route, trusted_attestors_loader=lambda: medical.trust,
        validation_time=NOW, max_attempts=2, max_gpu_seconds_per_attempt=60, retry_base_seconds=0.01,
    )
    registry = Registry.load(**kwargs)
    original = medical.route.read_text()
    trust = dict(medical.trust)
    if failure == "tampered":
        doc = json.loads(original)
        doc["models"][MODEL]["mcp_description"] += " unsigned change"
        medical.route.write_text(json.dumps(doc))
    elif failure == "missing":
        medical.route.unlink()
    elif failure == "malformed":
        medical.route.write_text("{}")
    else:
        medical.trust.clear()
    assert registry.revalidate()
    assert registry.get("qwen3-8b").enabled
    assert not registry.get(MODEL, require_enabled=False).enabled
    assert registry.validation_health()["healthy"] is True
    # Cold process startup must have the same isolation as periodic reload.
    assert Registry.load(**kwargs).get("qwen3-8b").enabled
    medical.route.write_text(original)
    medical.trust.update(trust)
    assert registry.revalidate()
    assert registry.get(MODEL).enabled


def test_invalid_second_registration_does_not_hide_valid_first_registration(medical):
    doc = json.loads(medical.route.read_text())
    doc["models"][BASE] = {"malformed": True}
    medical.route.write_text(json.dumps(doc))
    assert medical.registry.revalidate()
    assert medical.registry.get(MODEL).enabled
    assert medical.registry.validation_health()["healthy"] is True


def test_worker_response_checkpoint_is_checked_not_inferred_from_catalog(medical):
    model = medical.registry.get(MODEL)
    good = {"runtime_identity": {"checkpoint_sha256": CHECKPOINT}, "model_revision": "sha256:" + CHECKPOINT}
    validate_worker_checkpoint(model, good, file_result=True)
    for bad in ({}, {**good, "model_revision": "base"},
                {**good, "runtime_identity": {"checkpoint_sha256": digest("wrong")}}):
        with pytest.raises(NativeServerlessError):
            validate_worker_checkpoint(model, bad, file_result=True)


def test_public_view_never_promotes_demo_or_exposes_upstream_and_access_list(medical):
    view = _model_view(medical.registry.get(MODEL))
    q = view["qualification"]
    assert q["qualification_only"] is True and q["clinical_qualified"] is False
    assert q["measured_capacity"] is None and q["observed_at"] is None
    assert q["states"]["runtime_ready"] is False and q["states"]["http_mcp_qualified"] is False
    encoded = json.dumps(view)
    for private in ("worker.example.invalid", "aiendpoint-synthetic", "presenter-a", "demo-a"):
        assert private not in encoded


@pytest.mark.asyncio
async def test_ordinary_apps_seeding_adds_medical_without_replacing_existing_apps(medical):
    repository = MemoryAppsRepository()
    originals = []
    for name in (BASE, SORT):
        row = AppRecord(
            app_id=default_app_id(name), public_model_id=name, model_ref=name,
            display_name="Operator edited " + name, execution_mode="serving", namespace="fs2-models",
            deployment_name="existing-" + name, academic_required=True, created_at=NOW, updated_at=NOW,
        )
        originals.append(await repository.seed(row))
    service = AppsService(repository=repository, registry=medical.registry, admin=SimpleNamespace(), clock=lambda: NOW)
    await service.seed_defaults()
    await service.seed_defaults()
    rows = await repository.list_records()
    matching = [row for row in rows if row.public_model_id == MODEL]
    assert len(matching) == 1
    assert matching[0].app_id == default_app_id(MODEL) and matching[0].model_ref == MODEL
    assert matching[0].app_id not in {row.app_id for row in originals}
    for row in originals:
        assert await repository.get(row.app_id) == row


@pytest.mark.asyncio
async def test_medical_websocket_is_refused_before_admission_or_worker_access(medical):
    socket = SimpleNamespace(
        headers={"authorization": "Bearer synthetic-test-only"},
        accept=AsyncMock(), close=AsyncMock(), send_json=AsyncMock(),
        receive_text=AsyncMock(return_value=json.dumps({
            "type": "session.start", "options": {"model": MODEL},
        })),
    )
    admission = SimpleNamespace(admit=AsyncMock())
    store = SimpleNamespace(cancel_operation=AsyncMock())
    router = speech_stream_router(
        verifier=AsyncMock(return_value=principal()), registry=medical.registry, admission=admission, store=store,
    )
    endpoint = next(route.endpoint for route in router.routes if route.path == "/v1/audio/stream")
    await endpoint(socket)
    socket.send_json.assert_awaited_once_with({
        "type": "session.error", "code": "speech_session_rejected", "retryable": False,
    })
    admission.admit.assert_not_awaited()
    store.cancel_operation.assert_not_awaited()
    socket.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_streaming_admission_cannot_bypass_medical_live_refusal(medical):
    from test_admission_workers import service

    store, runtime = AsyncMock(), AsyncMock()
    admission = service(medical.registry, store, runtime)
    request = AdmissionRequest(
        model_id=MODEL, operation="transcribe", protocol="native", idempotency_key="medical-live-denied",
        request_body=b'{"type":"session.start","options":{"model":"nemotron-speech-en-0.6b"}}',
    )
    with pytest.raises(ValueError, match="model does not implement live speech"):
        await admission.admit(principal(), request, streaming=True)
    assert not store.mock_calls and not runtime.mock_calls

"""SAI-23 regressions: Cosmos cannot admit caller-selected network fetches."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from test_dynamic_routes import _cosmos_revision, _principal
from test_model_deployment_publication import status_view

from fs2_serve.admission import AdmissionService
from fs2_serve.cosmos_media_security import CosmosMediaReferenceError
from fs2_serve.crypto import KeyedHasher, PayloadCipher
from fs2_serve.memory_store import MemoryStore
from fs2_serve.model_deployment_publication import project_dynamic_publications
from fs2_serve.model_deployment_records import (
    ModelDeploymentAppendRequest,
    ModelDeploymentRevisionAction,
)
from fs2_serve.model_input_contracts import ModelInputContract
from fs2_serve.models import AdmissionRequest, Principal, Scope, TokenCreate
from fs2_serve.registry import Registry
from fs2_serve.runtime import StubRuntimeClient
from fs2_serve.telemetry import Metrics


def _artifact(*, media_type: str = "video/mp4", size_bytes: int = 4096) -> dict[str, object]:
    return {
        "artifact_id": "00000000-0000-4000-8000-000000000023",
        "sha256": "a" * 64,
        "size_bytes": size_bytes,
        "media_type": media_type,
        "compression": "none",
    }


def test_cosmos_contract_drops_every_url_capable_media_branch() -> None:
    vulnerable_reference = {
        "anyOf": [
            {"type": "string", "pattern": "^https://", "maxLength": 4096},
            {"type": "object"},
        ]
    }
    contract = ModelInputContract(
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "input_reference": vulnerable_reference,
                "vision_path": {"type": "string", "pattern": "^https://", "maxLength": 4096},
                "controls": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"reference": vulnerable_reference},
                    },
                },
            },
        },
        examples=(),
        source_refs=(),
        model_ref="cosmos3-nano",
        protocol="native",
    )
    validator = Draft202012Validator(contract.input_schema)
    private_target = "https://10.5.0.1/"

    assert not validator.is_valid({"input_reference": private_target})
    assert not validator.is_valid({"vision_path": private_target})
    assert not validator.is_valid({"controls": [{"reference": private_target}]})
    validator.validate({"input_reference": _artifact()})
    validator.validate({"vision_path": _artifact()})
    validator.validate({"controls": [{"reference": _artifact()}]})


async def _published_cosmos(registry: Registry) -> tuple[AdmissionService, MemoryStore, Principal]:
    revision = _cosmos_revision(registry)
    snapshot = project_dynamic_publications(
        [revision],
        {(revision.namespace, revision.name): status_view(revision)},
    )
    assert registry.set_dynamic_publications(snapshot, valid_until=datetime.now(UTC) + timedelta(minutes=1))
    store = MemoryStore(
        PayloadCipher(active_key_id="payload", keys={"payload": b"p" * 32}),
        KeyedHasher(active_key_id="ledger", keys={"ledger": b"h" * 32}),
    )
    service = AdmissionService(
        registry=registry,
        store=store,
        runtime=StubRuntimeClient(),
        metrics=Metrics(registry.list()),
        worker_concurrency=1,
        poll_seconds=0.01,
        lease_seconds=30,
        maintenance_interval_seconds=1,
        shutdown_grace_seconds=1,
    )
    principal = _principal()
    await store.model_deployment_append_revision(
        ModelDeploymentAppendRequest(
            namespace=revision.namespace,
            name=revision.name,
            expected_etag=None,
            spec=revision.spec,
            action=ModelDeploymentRevisionAction.CREATE,
            actor_id=uuid4(),
            actor="operator@example.test",
            idempotency_key="cosmos-security-model-create-0001",
        )
    )
    await store.issue_token(
        token_id=principal.token_id,
        prefix=principal.token_prefix,
        pepper_key_id="pepper-v1",
        digest="cosmos-security-test-digest",
        request=TokenCreate(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            scopes={
                Scope.CATALOG_READ,
                Scope.INFERENCE_INVOKE,
                Scope.MCP_INVOKE,
                Scope.USE_NONCLINICAL,
            },
            models={"*"},
            max_concurrency=principal.max_concurrency,
        ),
        created_by="test",
    )
    return service, store, principal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "video-to-video", "prompt": "fixture", "input_reference": "https://10.5.0.1/"},
        {"mode": "video-to-video", "prompt": "fixture", "vision_path": "https://10.5.0.1/"},
        {
            "mode": "transfer-video",
            "prompt": "fixture",
            "controls": [{"control_type": "depth", "reference": "https://10.5.0.1/"}],
        },
    ],
)
async def test_private_https_media_reference_is_rejected_before_durable_admission(
    registry: Registry,
    payload: dict[str, object],
) -> None:
    service, store, principal = await _published_cosmos(registry)

    with pytest.raises(CosmosMediaReferenceError, match="platform artifact reference"):
        await service.admit(
            principal,
            AdmissionRequest(
                model_id="cosmos3-nano",
                operation="generate-media",
                protocol="native",
                idempotency_key="cosmos-private-url-denied-0001",
                request_body=json.dumps(payload).encode(),
            ),
        )

    assert store.operations == {}


@pytest.mark.asyncio
async def test_escaped_private_url_field_name_cannot_bypass_admission(registry: Registry) -> None:
    service, store, principal = await _published_cosmos(registry)

    with pytest.raises(CosmosMediaReferenceError, match="platform artifact reference"):
        await service.admit(
            principal,
            AdmissionRequest(
                model_id="cosmos3-nano",
                operation="generate-media",
                protocol="native",
                idempotency_key="cosmos-escaped-private-url-denied-0001",
                request_body=(
                    b'{"mode":"video-to-video","prompt":"fixture",'
                    b'"\\u0069nput_reference":"https://10.5.0.1/"}'
                ),
            ),
        )

    assert store.operations == {}


@pytest.mark.asyncio
async def test_existing_cosmos_text_workflow_and_artifact_reference_remain_admissible(
    registry: Registry,
) -> None:
    service, store, principal = await _published_cosmos(registry)
    text_operation = await service.admit(
        principal,
        AdmissionRequest(
            model_id="cosmos3-nano",
            operation="generate-media",
            protocol="native",
            idempotency_key="cosmos-text-media-allowed-0001",
            request_body=b'{"mode":"text-to-video","prompt":"A synthetic red cube"}',
        ),
    )
    artifact_operation = await service.admit(
        principal,
        AdmissionRequest(
            model_id="cosmos3-nano",
            operation="generate-media",
            protocol="native",
            idempotency_key="cosmos-artifact-media-allowed-0001",
            request_body=json.dumps(
                {"mode": "video-to-video", "prompt": "fixture", "input_reference": _artifact()}
            ).encode(),
        ),
    )

    assert text_operation.id in store.operations
    assert artifact_operation.id in store.operations

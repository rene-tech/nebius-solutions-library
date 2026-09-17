"""SAI-23 regressions: Cosmos cannot admit caller-selected network fetches."""

# ruff: noqa: F811,I001 -- additive successor preserves the sealed v1 imports.

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import httpx
from jsonschema import Draft202012Validator
from test_dynamic_routes import _cosmos_revision, _principal
from test_model_input_contracts import selected
from test_model_deployment_publication import status_view

from fs2_serve.admission import AdmissionService
from fs2_serve.artifact_inputs import ArtifactInputMaterializer
from fs2_serve.cosmos_media_security import CosmosMediaReferenceError
from fs2_serve.cosmos_media_security import enforce_cosmos_runtime_payload_policy
from fs2_serve.crypto import KeyedHasher, PayloadCipher
from fs2_serve.memory_store import MemoryStore
from fs2_serve.model_deployment_publication import project_dynamic_publications
from fs2_serve.model_deployment_records import (
    ModelDeploymentAppendRequest,
    ModelDeploymentRevisionAction,
)
from fs2_serve.model_input_contracts import ModelInputContract
from fs2_serve.model_input_contracts import contract_for
from fs2_serve.models import AdmissionRequest, Principal, Scope, TokenCreate
from fs2_serve.models import AdmissionRequest, OperationStatus, Principal, Scope, TokenCreate
from fs2_serve.registry import Registry
from fs2_serve.runtime import StubRuntimeClient
from fs2_serve.runtime import RuntimeClient
from fs2_serve.scientific_artifacts import ArtifactNotFoundError
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


def test_actual_cosmos_contract_covers_five_modes_with_artifact_only_media(registry: Registry) -> None:
    contract = contract_for(selected(registry, "cosmos3-nano"), "native")
    validator = Draft202012Validator(contract.input_schema)
    image = _artifact(media_type="image/png")
    video = _artifact(media_type="video/mp4")
    payloads = (
        {"mode": "text-to-image", "prompt": "A synthetic red cube"},
        {"mode": "text-to-video", "prompt": "A synthetic red cube rotates"},
        {
            "mode": "image-to-video",
            "prompt": "Animate the cube",
            "input_reference": image,
        },
        {
            "mode": "video-to-video",
            "prompt": "Restyle the cube",
            "vision_path": video,
        },
        {
            "mode": "transfer-video",
            "prompt": "Follow the supplied depth control",
            "controls": [{"control_type": "depth", "reference": image}],
        },
    )

    for payload in payloads:
        validator.validate(payload)

    validator.validate(
        {
            "mode": "text-to-image",
            "prompt": "Explicit published delivery",
            "output_delivery": "inline-base64",
        }
    )

    validator.validate(
        {
            "mode": "transfer-video",
            "prompt": "Derive edge control from the source video",
            "input_reference": video,
            "controls": [{"control_type": "edge"}],
        }
    )
    assert not validator.is_valid(
        {
            "mode": "transfer-video",
            "prompt": "Missing source for a derived edge control",
            "controls": [{"control_type": "edge"}],
        }
    )
    assert not validator.is_valid(
        {
            "mode": "transfer-video",
            "prompt": "Transfer primary inputs are videos",
            "input_reference": image,
            "controls": [{"control_type": "edge"}],
        }
    )

    private_target = "https://10.5.0.1/"
    for payload in (
        {"mode": "image-to-video", "prompt": "fixture", "input_reference": private_target},
        {"mode": "video-to-video", "prompt": "fixture", "vision_path": private_target},
        {
            "mode": "transfer-video",
            "prompt": "fixture",
            "controls": [{"control_type": "depth", "reference": private_target}],
        },
    ):
        assert not validator.is_valid(payload)


def test_generic_cosmos_hardening_follows_escaped_refs_and_conditionals() -> None:
    vulnerable = {"type": "string", "pattern": "^https://", "maxLength": 4096}
    contract = ModelInputContract(
        input_schema={
            "type": "object",
            "$defs": {
                "control/reference": {
                    "type": "object",
                    "properties": {"reference": vulnerable},
                }
            },
            "properties": {
                "mode": {"type": "string"},
                "controls": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/control~1reference"},
                },
            },
            "allOf": [
                {
                    "if": {"properties": {"mode": {"const": "conditional"}}},
                    "then": {"properties": {"vision_path": vulnerable}},
                }
            ],
        },
        examples=(),
        source_refs=(),
        model_ref="cosmos3-nano",
        protocol="future-native",
    )
    validator = Draft202012Validator(contract.input_schema)

    assert not validator.is_valid(
        {"mode": "conditional", "vision_path": "https://10.5.0.1/"}
    )
    assert not validator.is_valid(
        {"controls": [{"reference": "https://10.5.0.1/"}]}
    )
    validator.validate({"mode": "conditional", "vision_path": _artifact()})
    validator.validate({"controls": [{"reference": _artifact()}]})


class _InvocationRecordingRuntime(StubRuntimeClient):
    def __init__(self) -> None:
        super().__init__()
        self.invocations = 0

    async def invoke(self, model, operation, request_body):
        self.invocations += 1
        return await super().invoke(model, operation, request_body)


class _MissingArtifactController:
    async def open_content(self, artifact_id, *, tenant_id):
        raise ArtifactNotFoundError("tenant-scoped artifact is unavailable")


@pytest.mark.asyncio
async def test_retained_https_row_fails_terminally_before_runtime_without_deletion(
    registry: Registry,
) -> None:
    service, store, principal = await _published_cosmos(registry)
    runtime = _InvocationRecordingRuntime()
    service.runtime = runtime
    operation = await service.admit(
        principal,
        AdmissionRequest(
            model_id="cosmos3-nano",
            operation="generate-media",
            protocol="native",
            idempotency_key="cosmos-retained-row-denied-0001",
            request_body=b'{"mode":"text-to-video","prompt":"safe at admission"}',
        ),
    )
    unsafe = (
        b'{"mode":"video-to-video","prompt":"retained",'
        b'"input_reference":"https://10.5.0.1/"}'
    )
    row = store.operations[operation.id]
    row.request = store.cipher.encrypt(
        unsafe,
        aad=store.cipher.aad(operation.id, principal.tenant_id, operation.model_id, "request"),
    )

    claimed = await store.claim_operation("cosmos-retained-row-worker", lease_seconds=30)
    assert claimed is not None
    await service._execute_claim(claimed)

    final = await store.get_operation(operation.id, tenant_id=principal.tenant_id)
    assert final.status is OperationStatus.FAILED
    assert final.error_code == "artifact_input_invalid"
    assert runtime.invocations == 0
    assert operation.id in store.operations


@pytest.mark.asyncio
async def test_missing_tenant_artifact_fails_terminally_without_retry_or_row_deletion(
    registry: Registry,
) -> None:
    service, store, principal = await _published_cosmos(registry)
    runtime = _InvocationRecordingRuntime()
    service.runtime = runtime
    service.artifact_inputs = ArtifactInputMaterializer(_MissingArtifactController())
    operation = await service.admit(
        principal,
        AdmissionRequest(
            model_id="cosmos3-nano",
            operation="generate-media",
            protocol="native",
            idempotency_key="cosmos-missing-artifact-terminal-0001",
            request_body=json.dumps(
                {
                    "mode": "video-to-video",
                    "prompt": "retained artifact reference",
                    "input_reference": _artifact(),
                }
            ).encode(),
        ),
    )

    claimed = await store.claim_operation("cosmos-missing-artifact-worker", lease_seconds=30)
    assert claimed is not None
    await service._execute_claim(claimed)

    final = await store.get_operation(operation.id, tenant_id=principal.tenant_id)
    assert final.status is OperationStatus.FAILED
    assert final.error_code == "artifact_input_invalid"
    assert final.attempt == 1
    assert runtime.invocations == 0
    assert operation.id in store.operations


def test_runtime_budget_accepts_exact_limit_padded_base64_control(registry: Registry) -> None:
    model = selected(registry, "cosmos3-nano")
    exact_limit = b"x" * (4 * 1024 * 1024)
    exact_limit = b"\x89PNG\r\n\x1a\n" + exact_limit[8:]
    payload = {
        "mode": "transfer-video",
        "prompt": "exact padded control",
        "controls": [
            {
                "control_type": "depth",
                "reference": (
                    "data:image/png;base64,"
                    + base64.b64encode(exact_limit).decode("ascii")
                ),
            }
        ],
    }

    enforce_cosmos_runtime_payload_policy(
        model,
        "native",
        json.dumps(payload, separators=(",", ":")).encode(),
    )


@pytest.mark.asyncio
async def test_aggregate_artifact_budget_is_rejected_at_admission(registry: Registry) -> None:
    service, store, principal = await _published_cosmos(registry)
    payload = {
        "mode": "transfer-video",
        "prompt": "fixture",
        "input_reference": _artifact(size_bytes=24 * 1024 * 1024),
        "controls": [
            {
                "control_type": kind,
                "reference": _artifact(media_type="image/png", size_bytes=4 * 1024 * 1024),
            }
            for kind in ("depth", "seg", "wsm")
        ],
    }

    with pytest.raises(CosmosMediaReferenceError, match="per-request byte budget"):
        await service.admit(
            principal,
            AdmissionRequest(
                model_id="cosmos3-nano",
                operation="generate-media",
                protocol="native",
                idempotency_key="cosmos-aggregate-budget-denied-0001",
                request_body=json.dumps(payload).encode(),
            ),
        )

    assert store.operations == {}


@pytest.mark.asyncio
async def test_control_plane_accepts_only_identity_bound_cosmos_mp4_output(
    registry: Registry,
) -> None:
    service, store, principal = await _published_cosmos(registry)
    operation = await service.admit(
        principal,
        AdmissionRequest(
            model_id="cosmos3-nano",
            operation="generate-media",
            protocol="native",
            idempotency_key="cosmos-binary-output-identity-0001",
            request_body=(
                b'{"mode":"text-to-video","prompt":"synthetic",'
                b'"output_delivery":"artifact"}'
            ),
        ),
    )
    claimed = await store.claim_operation("cosmos-binary-output-worker", lease_seconds=30)
    assert claimed is not None
    model = await service._current_model(claimed)
    media = b"\x00\x00\x00\x18ftypmp42synthetic"
    operation = SimpleNamespace(
        request_body=(
            b'{"mode":"text-to-video","prompt":"synthetic",'
            b'"output_delivery":"artifact"}'
        )
    )

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "video/mp4",
                "x-fs2-output-sha256": hashlib.sha256(media).hexdigest(),
                "x-fs2-output-bytes": str(len(media)),
            },
            content=media,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    runtime = RuntimeClient(
        activation_timeout_seconds=2,
        runtime_timeout_seconds=2,
        max_response_bytes=1024,
        client=client,
    )
    try:
        result = await runtime.invoke(model, claimed, operation.request_body)
    finally:
        await client.aclose()

    assert result.body == media
    assert result.content_type == "video/mp4"
    assert result.semantic_outcome == "protocol_valid"

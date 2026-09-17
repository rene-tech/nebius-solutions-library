"""Bulk model inputs stay out of MCP/LLM context and materialize tenant-safely."""

import hashlib
import json
from dataclasses import dataclass, replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from conftest import CATALOG_ROOT
from test_api_mcp import bound_model_registry

from fs2_serve.artifact_inputs import ArtifactInputError, ArtifactInputMaterializer
from fs2_serve.registry import Registry
from fs2_serve.scientific_artifacts import ArtifactContentStream
from fs2_serve.scientific_run_result import ArtifactRef


def _portable_model(registry: Registry, model_id: str):
    native = bound_model_registry(registry, model_id)
    model = native.get(model_id)
    declaration = json.loads((CATALOG_ROOT / "deployment-runtimes" / f"{model_id}-portable-h100.json").read_text())
    record = declaration["record"]
    image_digest = record["runtime"]["image"]["digest"]
    gpu_class = record["resources"]["gpu"]["class"]
    rebound = Registry(
        native.catalog,
        {
            model_id: replace(
                model,
                variant_id=declaration["variant_id"],
                gateway=replace(
                    model.gateway,
                    runtime_kind=record["runtime"]["kind"],
                    runtime_image_digest=image_digest,
                    gpu_class=gpu_class,
                    qualification=None,
                    binding=replace(
                        model.binding, backend_runtime_image_digest=image_digest, backend_gpu_class=gpu_class
                    ),
                ),
            )
        },
    )
    return rebound.get(model_id)


@dataclass
class _Record:
    reference: ArtifactRef
    operation_id: UUID

    def to_public_ref(self) -> ArtifactRef:
        return self.reference


class _Artifacts:
    def __init__(
        self,
        content: bytes,
        reference: ArtifactRef,
        *,
        tenant_id: str = "tenant-a",
        operation_id: UUID | None = None,
    ) -> None:
        self.content = content
        self.reference = reference
        self.tenant_id = tenant_id
        self.operation_id = operation_id or uuid4()
        self.calls: list[tuple[UUID, str]] = []
        self.chunks_read = 0

    async def open_content(self, artifact_id: UUID, *, tenant_id: str) -> ArtifactContentStream:
        self.calls.append((artifact_id, tenant_id))
        if tenant_id != self.tenant_id or str(artifact_id) != self.reference.artifact_id:
            raise ArtifactInputError("artifact not found")

        async def chunks():
            self.chunks_read += 1
            yield self.content[:17]
            yield self.content[17:]

        return ArtifactContentStream(
            artifact=_Record(self.reference, self.operation_id),  # type: ignore[arg-type]
            chunks=chunks(),
        )


@dataclass(frozen=True)
class _Owner:
    tenant_id: str = "tenant-a"
    principal_id: str = "scientist-a"
    token_id: UUID = UUID("00000000-0000-4000-8000-000000000001")


class _OwnerStore:
    def __init__(self, artifacts: _Artifacts, *, source: _Owner, caller: _Owner) -> None:
        self.artifacts = artifacts
        self.source = source
        self.caller = caller

    async def get_operation(self, operation_id: UUID, *, tenant_id: str):
        if operation_id != self.artifacts.operation_id or tenant_id != self.source.tenant_id:
            raise AssertionError("unexpected artifact operation lookup")
        return SimpleNamespace(
            tenant_id=self.source.tenant_id,
            principal_id=self.source.principal_id,
            token_id=self.source.token_id,
        )

    async def get_token(self, token_id: UUID):
        if token_id != self.caller.token_id:
            raise AssertionError("unexpected caller token lookup")
        return SimpleNamespace(
            prefix="fst_test",
            tenant_id=self.caller.tenant_id,
            principal_id=self.caller.principal_id,
            scopes=[],
            models=["diffdock"],
            expires_at=None,
            revoked_at=None,
            rotated_at=None,
            request_budget=None,
            gpu_seconds_budget=None,
            max_concurrency=1,
        )


def _materializer(
    artifacts: _Artifacts,
    *,
    source: _Owner | None = None,
    caller: _Owner | None = None,
) -> tuple[ArtifactInputMaterializer, _Owner]:
    source = source or _Owner()
    caller = caller or source
    return ArtifactInputMaterializer(
        artifacts,  # type: ignore[arg-type]
        _OwnerStore(artifacts, source=source, caller=caller),  # type: ignore[arg-type]
    ), caller


def _reference(content: bytes, *, media_type: str = "chemical/x-pdb") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=str(uuid4()),
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        media_type=media_type,
        compression="none",
    )


@pytest.mark.asyncio
async def test_materializes_caller_owned_pdb_reference_at_runtime_boundary(registry):
    content = b"HEADER    SYNTHETIC\nATOM      1  CA  ALA A   1       0.0     0.0     0.0\nEND\n"
    reference = _reference(content)
    artifacts = _Artifacts(content, reference)
    materializer, owner = _materializer(artifacts)
    request = {
        "protein": reference.model_dump(mode="json"),
        "ligand": "CC(=O)Oc1ccccc1C(=O)O",
    }

    body = await materializer.materialize(
        _portable_model(registry, "diffdock"),
        "native",
        owner=owner,  # type: ignore[arg-type]
        request_body=json.dumps(request).encode(),
    )

    assert json.loads(body)["protein"] == content.decode()
    assert artifacts.calls == [(UUID(reference.artifact_id), "tenant-a")]


@pytest.mark.asyncio
async def test_materializes_pinned_fixture_without_artifact_or_llm_bytes(registry):
    reference = _reference(b"unused")
    artifacts = _Artifacts(b"unused", reference)
    materializer, owner = _materializer(artifacts)

    body = await materializer.materialize(
        _portable_model(registry, "diffdock"),
        "native",
        owner=owner,  # type: ignore[arg-type]
        request_body=json.dumps(
            {"protein": {"fixture_id": "pdb/1ubq"}, "ligand": "CC(=O)Oc1ccccc1C(=O)O"}
        ).encode(),
    )

    protein = json.loads(body)["protein"]
    assert protein.startswith("HEADER") and "1UBQ" in protein[:256] and len(protein) == 78570
    assert not artifacts.calls


@pytest.mark.asyncio
async def test_rejects_reference_metadata_that_does_not_match_stored_artifact(registry):
    content = b"HEADER\nATOM\n"
    actual = _reference(content)
    supplied = actual.model_copy(update={"sha256": "0" * 64})
    artifacts = _Artifacts(content, actual)
    materializer, owner = _materializer(artifacts)

    with pytest.raises(ArtifactInputError, match="does not match"):
        await materializer.materialize(
            _portable_model(registry, "diffdock"),
            "native",
            owner=owner,  # type: ignore[arg-type]
            request_body=json.dumps(
                {"protein": supplied.model_dump(mode="json"), "ligand": "CC"}
            ).encode(),
        )


@pytest.mark.asyncio
async def test_same_tenant_peer_artifact_is_rejected_before_its_bytes_are_consumed(registry):
    content = b"HEADER\nATOM\n"
    reference = _reference(content)
    artifacts = _Artifacts(content, reference)
    source = _Owner()
    peer = _Owner(
        principal_id="scientist-b",
        token_id=UUID("00000000-0000-4000-8000-000000000002"),
    )
    materializer, owner = _materializer(artifacts, source=source, caller=peer)

    with pytest.raises(ArtifactInputError, match="unavailable"):
        await materializer.materialize(
            _portable_model(registry, "diffdock"),
            "native",
            owner=owner,  # type: ignore[arg-type]
            request_body=json.dumps(
                {"protein": reference.model_dump(mode="json"), "ligand": "CC"}
            ).encode(),
        )

    assert artifacts.chunks_read == 0

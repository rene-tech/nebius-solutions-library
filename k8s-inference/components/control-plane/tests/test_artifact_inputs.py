"""Bulk model inputs stay out of MCP/LLM context and materialize tenant-safely."""

import hashlib
import json
from dataclasses import dataclass, replace
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

    def to_public_ref(self) -> ArtifactRef:
        return self.reference


class _Artifacts:
    def __init__(self, content: bytes, reference: ArtifactRef, *, tenant_id: str = "tenant-a") -> None:
        self.content = content
        self.reference = reference
        self.tenant_id = tenant_id
        self.calls: list[tuple[UUID, str]] = []

    async def open_content(self, artifact_id: UUID, *, tenant_id: str) -> ArtifactContentStream:
        self.calls.append((artifact_id, tenant_id))
        if tenant_id != self.tenant_id or str(artifact_id) != self.reference.artifact_id:
            raise ArtifactInputError("artifact not found")

        async def chunks():
            yield self.content[:17]
            yield self.content[17:]

        return ArtifactContentStream(artifact=_Record(self.reference), chunks=chunks())  # type: ignore[arg-type]


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
    materializer = ArtifactInputMaterializer(artifacts)  # type: ignore[arg-type]
    request = {
        "protein": reference.model_dump(mode="json"),
        "ligand": "CC(=O)Oc1ccccc1C(=O)O",
    }

    body = await materializer.materialize(
        _portable_model(registry, "diffdock"),
        "native",
        tenant_id="tenant-a",
        request_body=json.dumps(request).encode(),
    )

    assert json.loads(body)["protein"] == content.decode()
    assert artifacts.calls == [(UUID(reference.artifact_id), "tenant-a")]


@pytest.mark.asyncio
async def test_materializes_pinned_fixture_without_artifact_or_llm_bytes(registry):
    reference = _reference(b"unused")
    artifacts = _Artifacts(b"unused", reference)
    materializer = ArtifactInputMaterializer(artifacts)  # type: ignore[arg-type]

    body = await materializer.materialize(
        _portable_model(registry, "diffdock"),
        "native",
        tenant_id="tenant-a",
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
    materializer = ArtifactInputMaterializer(_Artifacts(content, actual))  # type: ignore[arg-type]

    with pytest.raises(ArtifactInputError, match="does not match"):
        await materializer.materialize(
            _portable_model(registry, "diffdock"),
            "native",
            tenant_id="tenant-a",
            request_body=json.dumps(
                {"protein": supplied.model_dump(mode="json"), "ligand": "CC"}
            ).encode(),
        )

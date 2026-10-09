"""Scientific target selection and pre-GPU input failures, with no model calls."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import zstandard
from starlette.requests import Request
from test_api_mcp import build_runtime
from test_scientific_batch_production import principal, profile_catalog_for, scientific_runtime
from test_scientific_primary_adapters import fixture

from fs2_serve.api import create_app
from fs2_serve.scientific_artifacts import ArtifactNotFoundError
from fs2_serve.scientific_batch.adapters import proteina_complexa
from fs2_serve.scientific_batch.adapters.primitives import ScientificParameterError
from fs2_serve.scientific_batch.adapters.proteina_targets import (
    public_target_catalog,
    target_configuration,
    validate_target_bundle,
)
from fs2_serve.scientific_batch.artifact_bridge import ArtifactServiceBridge
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer
from fs2_serve.scientific_batch.models import ScientificInputArtifact
from fs2_serve.scientific_batch.profile_catalog import ScientificRequestError


def _tar(path: str, *, compression="gzip", content=b"ATOM example fixture\n") -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz" if compression == "gzip" else "w") as archive:
        info = tarfile.TarInfo(path)
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    raw = stream.getvalue()
    return zstandard.ZstdCompressor().compress(raw) if compression == "zstd" else raw


def test_catalog_pins_all_variant_sources_and_does_not_claim_custom_or_live_support() -> None:
    catalog = public_target_catalog()
    assert catalog["source_revision"] == proteina_complexa.SOURCE_REVISION
    assert catalog["hosted_custom_target_configuration"] is False
    assert {key: len(value["targets"]) for key, value in catalog["variants"].items()} == {
        "protein-target": 44,
        "ligand-target": 4,
        "ame": 44,
    }
    assert "does not establish runtime" in catalog["selection_help"]
    for variant in catalog["variants"].values():
        assert len(variant["source_sha256"]) == 64
        assert catalog["source_revision"] in variant["source_url"]
        assert all(value["bundle_path"] and value["description"] for value in variant["targets"].values())
    # Public consumers cannot mutate compiler state.
    catalog["variants"]["protein-target"]["targets"].clear()
    assert target_configuration("protein-target", "02_PDL1")


def test_pd_l1_filename_does_not_choose_between_different_scientific_configurations() -> None:
    ordinary = target_configuration("protein-target", "02_PDL1")
    aav = target_configuration("protein-target", "03_PDL1_AAV")
    assert ordinary["bundle_path"] == aav["bundle_path"] == "assets/target_data/bindcraft_targets/PD-L1.pdb"
    assert ordinary["binder_length"] == [64, 155]
    assert aav["binder_length"] == [75, 115]
    assert ordinary["hotspot_residues"] != aav["hotspot_residues"]
    parameters = fixture("proteina-complexa", "positive-protein.json")["parameters"]
    parameters["target_id"] = "PD-L1"
    with pytest.raises(ScientificParameterError) as caught:
        proteina_complexa.ProteinaParameters.parse(parameters)
    assert "02_PDL1" in caught.value.public_detail
    assert "03_PDL1_AAV" in caught.value.public_detail
    assert "not interchangeable" in caught.value.public_detail
    assert parameters["target_id"] == "PD-L1"


@pytest.mark.parametrize(
    "variant,target",
    [
        ("protein-target", "custom-patient-target"),
        ("ligand-target", "02_PDL1"),
        ("ame", "39_7V11_LIGAND"),
    ],
)
def test_unsupported_custom_and_cross_variant_targets_are_actionable(variant, target) -> None:
    with pytest.raises(ScientificParameterError) as caught:
        target_configuration(variant, target)
    assert "get_model_schema" in caught.value.public_detail
    assert "Custom target configurations are not implemented" in caught.value.public_detail


@pytest.mark.parametrize(
    "variant,target",
    [
        ("protein-target", "01_PD1"),
        ("protein-target", "02_PDL1"),
        ("protein-target", "03_PDL1_AAV"),
        ("ligand-target", "39_7V11_LIGAND"),
        ("ame", "M0024_1nzy_og"),
        ("ame", "M0024_1nzy"),
    ],
)
@pytest.mark.parametrize("compression", ["gzip", "zstd"])
def test_variant_required_members_validate_without_extracting_or_inferring_config(variant, target, compression) -> None:
    path = target_configuration(variant, target)["bundle_path"]
    if target == "M0024_1nzy":
        assert path == "target_data/ame_targets/M0024_1nzy_v2.pdb"
    validate_target_bundle(
        _tar(path, compression=compression),
        variant=variant,
        target_id=target,
        compression=compression,
        maximum_bytes=proteina_complexa.MAX_INPUT_BYTES,
    )


@pytest.mark.parametrize("payload", [b"not a tar", _tar("PD-L1.pdb"), _tar("assets/wrong.pdb")])
def test_wrong_bundle_layout_is_a_public_scientific_parameter_error(payload) -> None:
    with pytest.raises(ScientificParameterError) as caught:
        validate_target_bundle(
            payload,
            variant="protein-target",
            target_id="02_PDL1",
            compression="gzip",
            maximum_bytes=proteina_complexa.MAX_INPUT_BYTES,
        )
    assert "assets/target_data/bindcraft_targets/PD-L1.pdb" in caught.value.public_detail


def _bridge(admission, payload, *, reader=None):
    bridge = ArtifactServiceBridge.__new__(ArtifactServiceBridge)
    bridge.content_reader = reader or SimpleNamespace(read=AsyncMock(return_value=payload))
    entry = ScientificInputArtifact(
        logical_artifact_id="target-bundle",
        semantic_type="proteina-complexa-target-bundle/v1",
        artifact_id=uuid4(),
        digest="sha256:" + hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        media_type="application/x-tar",
        compression="gzip",
    )
    return bridge, replace(admission, manifest=replace(admission.manifest, entries=(entry,)))


@pytest.mark.asyncio
async def test_missing_target_member_prevents_durable_admission_and_gpu_work(registry, cipher, hasher) -> None:
    runtime, _, repository, cluster, pointer = scientific_runtime(registry, cipher, hasher)
    service = runtime.scientific_batches
    service.profiles = profile_catalog_for("proteina-complexa")
    actor = (await principal(runtime.store)).model_copy(update={"models": frozenset({"proteina-complexa"})})
    access = service.artifacts.admission.access_context
    service.execution_binding = SimpleNamespace(
        variant_id=lambda _: "test",
        workload_namespace=lambda _: "fs2-models",
        access_context=lambda *a, **k: access,
    )
    bridge, admission = _bridge(service.artifacts.admission, _tar("wrong.pdb"))
    service.artifacts = SimpleNamespace(
        validate_input=AsyncMock(return_value=admission),
        validate_model_input=bridge.validate_model_input,
    )
    # The canonical parameters are already validated before this model-content
    # check. Use the production preflight position without replacing admission.
    request = {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": "design",
        "service_class": "customer-batch",
        "input_manifest": pointer,
        "parameters": {},
    }
    original_validate = service.profiles.validate_request
    service.profiles.validate_request = lambda p, r: {
        **original_validate(p, r),
        "parameters": {"variant": "protein-target", "target_id": "02_PDL1"},
    }
    with pytest.raises(ScientificRequestError) as caught:
        await service.submit(
            principal=actor,
            model_id="proteina-complexa",
            request=request,
            idempotency_key="invalid-target-bundle-before-admission",
        )
    assert "required layout" in caught.value.public_detail
    assert repository.records == {}
    assert runtime.store.operations == {}
    bridge.content_reader.read.assert_awaited_once_with(
        admission.manifest.entries[0].artifact_id,
        tenant_id="tenant-a",
        maximum_bytes=proteina_complexa.MAX_INPUT_BYTES,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "authorization", "hash"])
async def test_content_failures_keep_distinct_classes(registry, cipher, hasher, failure) -> None:
    runtime, *_ = scientific_runtime(registry, cipher, hasher)
    payload = _tar("assets/target_data/bindcraft_targets/PD-L1.pdb")
    bridge, admission = _bridge(runtime.scientific_batches.artifacts.admission, payload)
    if failure == "transport":
        bridge.content_reader.read.side_effect = httpx.ConnectError("temporary read failure")
        expected = httpx.ConnectError
    elif failure == "authorization":
        bridge.content_reader.read.side_effect = ArtifactNotFoundError("not authorized")
        expected = ArtifactNotFoundError
    else:
        bridge.content_reader.read.return_value = payload + b"altered"
        expected = ArtifactNotFoundError
    with pytest.raises(expected):
        await bridge.validate_model_input(
            "proteina-complexa",
            {"variant": "protein-target", "target_id": "02_PDL1"},
            admission,
            tenant_id="tenant-a",
        )


@pytest.mark.asyncio
async def test_other_models_do_not_read_input_content() -> None:
    bridge = ArtifactServiceBridge.__new__(ArtifactServiceBridge)
    bridge.content_reader = SimpleNamespace(read=AsyncMock())
    await bridge.validate_model_input("boltzgen", {}, None, tenant_id="tenant-a")
    bridge.content_reader.read.assert_not_called()


@pytest.mark.parametrize("public_detail", [None, "Select an exact target configuration from get_model_schema."])
@pytest.mark.asyncio
async def test_public_details_opt_in_across_dispatch_and_http(
    registry,
    cipher,
    hasher,
    monkeypatch,
    public_detail,
) -> None:
    def factory(*args, **kwargs):
        raise ScientificParameterError("PRIVATE_INTERNAL_EXCEPTION", public_detail=public_detail)

    monkeypatch.setattr(
        "fs2_serve.scientific_batch.execution.importlib.import_module",
        lambda _: SimpleNamespace(compile=factory),
    )
    renderer = SimpleNamespace(plan_adapters={"proteina-complexa": ("test", "compile")}, variant_id=lambda _: "test")
    with pytest.raises(ScientificRequestError) as caught:
        FileScientificManifestRenderer.plan(
            renderer,
            SimpleNamespace(model_id="proteina-complexa", value={}),
            {},
            access_context=SimpleNamespace(),
            input_artifacts=(),
        )
    assert caught.value.public_detail == public_detail
    app = create_app(build_runtime(registry, cipher, hasher))
    response = await app.exception_handlers[ScientificRequestError](Request({"type": "http"}), caught.value)
    assert response.status_code == 422
    body = response.body.decode()
    assert "PRIVATE_INTERNAL_EXCEPTION" not in body
    assert (public_detail or "request violates the canonical scientific contract") in body
    assert json.loads(body)["error"]["type"] == "scientific_request_invalid"

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from fs2_serve.scientific_batch.input_contracts import public_input_contract, validate_input_roles
from fs2_serve.scientific_batch.models import ScientificInputArtifact
from fs2_serve.scientific_batch.profile_catalog import ScientificRequestError

FIXED_MODELS = (
    "esmfold2",
    "esmfold2-fast",
    "openfold3-openbind",
    "protenix-v2",
    "alphafold3",
    "mosaic",
    "bindcraft",
    "boltzgen",
    "proteina-complexa",
)


def artifact(entry):
    return ScientificInputArtifact(
        logical_artifact_id=entry["name"],
        semantic_type=entry["semantic_type"],
        artifact_id=uuid4(),
        digest="sha256:" + "a" * 64,
        size_bytes=596,
        media_type=entry["media_type"],
        compression=entry["compression"],
    )


@pytest.mark.parametrize("model", FIXED_MODELS)
def test_discovered_fixed_input_is_accepted_without_mutation(model):
    contract = public_input_contract(model)
    assert contract["exactly_one_entry"]
    item = artifact(contract["entry"])
    request = {"input_manifest": {"artifact_id": str(uuid4())}}
    for compression in contract["entry"]["allowed_compressions"]:
        validate_input_roles(model, request, (replace(item, compression=compression),))
    contract["entry"]["name"] = "caller-mutated"
    assert public_input_contract(model)["entry"]["name"] != "caller-mutated"


def test_original_protenix_customer_failure_is_actionable_not_unavailable():
    item = ScientificInputArtifact(
        logical_artifact_id="protenix-v2-1acb-heteromer-s1",
        semantic_type="fs2.protenix-v2-input/v1",
        artifact_id=uuid4(),
        digest="sha256:" + "a" * 64,
        size_bytes=596,
        media_type="application/vnd.fs2.scientific-manifest+json",
        compression="none",
    )
    with pytest.raises(ScientificRequestError) as error:
        validate_input_roles("protenix-v2", {"input_manifest": {"artifact_id": str(uuid4())}}, (item,))
    detail = error.value.public_detail
    assert "name, semantic_type, media_type" in detail
    assert "'protenix-input'" in detail
    assert "'protenix-input-json/v1'" in detail
    assert "'application/json'" in detail
    assert "No operation was admitted" in detail
    assert str(item.artifact_id) not in detail


@pytest.mark.parametrize(
    "field,value",
    [
        ("logical_artifact_id", "experiment"),
        ("semantic_type", "incorrect/v1"),
        ("media_type", "application/octet-stream"),
        ("compression", "gzip"),
        ("size_bytes", 0),
        ("size_bytes", 256 * 1024 * 1024 + 1),
    ],
)
def test_each_mismatched_payload_descriptor_is_rejected(field, value):
    item = artifact(public_input_contract("protenix-v2")["entry"])
    with pytest.raises(ScientificRequestError):
        validate_input_roles(
            "protenix-v2", {"input_manifest": {"artifact_id": str(uuid4())}}, (replace(item, **{field: value}),)
        )


@pytest.mark.parametrize("count", [0, 2])
def test_exact_entry_count_is_enforced(count):
    item = artifact(public_input_contract("protenix-v2")["entry"])
    with pytest.raises(ScientificRequestError):
        validate_input_roles("protenix-v2", {"input_manifest": {"artifact_id": str(uuid4())}}, (item,) * count)


def test_payload_cannot_be_its_own_manifest_and_none_means_uncompressed():
    item = replace(artifact(public_input_contract("protenix-v2")["entry"]), compression=None)
    with pytest.raises(ScientificRequestError):
        validate_input_roles("protenix-v2", {"input_manifest": {"artifact_id": str(item.artifact_id)}}, (item,))
    validate_input_roles("protenix-v2", {"input_manifest": {"artifact_id": str(uuid4())}}, (item,))


@pytest.mark.parametrize("operation", ["design-backbone", "scaffold-motif"])
def test_rfdiffusion_roles_are_operation_dependent(operation):
    entry = public_input_contract("rfdiffusion")["operations"][operation]
    request = {"operation": operation, "input_manifest": {"artifact_id": str(uuid4())}}
    validate_input_roles("rfdiffusion", request, (artifact(entry),))


def test_rfdiffusion_discovery_distinguishes_provenance_note_from_target_coordinates():
    from pathlib import Path

    operations = public_input_contract("rfdiffusion")["operations"]
    design = operations["design-backbone"]
    fixture = (
        Path(__file__).resolve().parents[3]
        / "models/cancer-immunotherapy/runtime-images/rfdiffusion/activation/unconditional-target.txt"
    )
    assert design["example_content"] == fixture.read_text()
    assert design["name"] == "design_constraint"
    assert design["media_type"] == "text/plain"
    assert "does not configure inference" in design["description"]
    assert "parameters" in design["description"]
    scaffold = operations["scaffold-motif"]
    assert scaffold["media_type"] == "chemical/x-pdb"
    assert "coordinates are used in inference" in scaffold["description"]
    assert "example_content" not in scaffold
    design["description"] = "caller mutation"
    assert public_input_contract("rfdiffusion")["operations"]["design-backbone"]["description"] != "caller mutation"


@pytest.mark.parametrize("kind", ["uploaded-bundle", "huggingface", "object-store"])
def test_lerobot_existing_source_kinds_remain_compatible(kind):
    entry = public_input_contract("cosmos3-lerobot-augmentation")["source_kinds"][kind]
    request = {"parameters": {"source": {"kind": kind}}, "input_manifest": {"artifact_id": str(uuid4())}}
    validate_input_roles("cosmos3-lerobot-augmentation", request, (artifact(entry),))


def test_unknown_app_does_not_gain_a_guessed_contract():
    assert public_input_contract("future-model") is None
    validate_input_roles("future-model", {}, ())

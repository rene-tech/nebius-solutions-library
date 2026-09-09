from __future__ import annotations

import ast
import copy
import json
import re
from dataclasses import replace
from types import SimpleNamespace
from typing import Annotated, Literal

import pydantic
import pytest
from conftest import CATALOG_ROOT, SOLUTION_ROOT
from jsonschema import Draft202012Validator

from fs2_serve.model_input_contracts import InputContractUnavailable, _resource, contract_for, scientific_contract_for
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog


def selected(registry, model_id, protocol="native"):
    base = registry.get("qwen3-8b", require_enabled=False)
    adapter = _resource("runtime-adapters.json").get(model_id)
    return replace(
        base,
        gateway=replace(
            base.gateway,
            model_id=model_id,
            protocols=(protocol,),
            runtime_kind=adapter["runtime_kind"] if adapter else ("custom" if protocol == "native" else "vllm"),
            qualification={"variant_id": adapter["variant_id"]} if adapter else None,
        ),
    )


def source_models(path):
    """Execute only trusted DTO declarations, never imports or model startup."""
    nodes = []
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.ClassDef) and any(
            isinstance(base, ast.Name) and base.id in {"BaseModel", "StrictModel"} for base in node.bases
        ):
            node.body = [child for child in node.body if isinstance(child, ast.Assign | ast.AnnAssign)]
            nodes.append(node)
        elif (
            path.name == "contracts.py"
            and isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"NonNegative", "Positive", "Percentage", "BetaValue"}
        ):
            nodes.append(node)
    namespace = {name: getattr(pydantic, name) for name in ("BaseModel", "ConfigDict", "Field", "FiniteFloat")}
    namespace.update(Annotated=Annotated, Literal=Literal)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec", dont_inherit=True), namespace)  # noqa: S102
    return namespace


@pytest.mark.parametrize(
    ("model_id", "dto"),
    [
        ("boltz2", "PredictRequest"),
        ("genmol", "GenerateRequest"),
        ("molmim", "GenerateRequest"),
        ("msa-search-pdb70", "SearchRequest"),
        ("altumage", "AltumAgeRequest"),
        ("phenoage", "ClinicalRequest"),
    ],
)
def test_pydantic_snapshots_match_actual_runtime_source(model_id, dto):
    record = _resource("runtime-pydantic.json")[model_id]
    path = SOLUTION_ROOT.parent / record["source"]
    assert source_models(path)[dto].model_json_schema() == record["schema"]


NATIVE_MODELS = (
    "evo2-40b",
    "rfdiffusion",
    "cosmos3-nano",
    "boltz2",
    "openfold2",
    "openfold3",
    "diffdock",
    "proteinmpnn",
    "genmol",
    "molmim",
    "msa-search-pdb70",
    "sdxl",
    "nv-segment-ct",
    "phenoage",
    "altumage",
)


@pytest.mark.parametrize("model_id", NATIVE_MODELS)
def test_native_fields_descriptions_and_examples(registry, model_id):
    contract = contract_for(selected(registry, model_id), "native")
    Draft202012Validator.check_schema(contract.input_schema)
    assert contract.model_ref == model_id and contract.source_refs
    assert contract.input_schema["type"] == "object"
    assert contract.input_schema["required"]
    assert contract.input_schema["additionalProperties"] is False
    assert not {"payload", "request"} & contract.input_schema["properties"].keys()
    assert all(field["description"] for field in contract.input_schema["properties"].values())
    for example in contract.examples:
        Draft202012Validator(contract.input_schema).validate(example)


def test_clone_source_identity_and_unknown_runtime(registry):
    source = selected(registry, "openfold2")
    model = replace(source, gateway=replace(source.gateway, model_id="app-independent-openfold"))
    policy = SimpleNamespace(
        publication=SimpleNamespace(
            model_ref="app-independent-openfold",
            source_model_ref="openfold2",
        )
    )
    clone = replace(model, dynamic_policy=policy)
    assert contract_for(clone, "native").model_ref == "openfold2"
    assert clone.id == "app-independent-openfold"
    with pytest.raises(InputContractUnavailable):
        contract_for(selected(registry, "not-reviewed"), "native")
    with pytest.raises(InputContractUnavailable):
        contract_for(selected(registry, "openfold2"), "openai-chat")


def test_returned_schema_cannot_mutate_cache(registry):
    contract_for(selected(registry, "boltz2"), "native").input_schema["properties"].clear()
    assert "polymers" in contract_for(selected(registry, "boltz2"), "native").input_schema["properties"]


def test_known_nim_or_other_variant_never_gets_portable_schema(registry):
    archival = registry.get("boltz2", require_enabled=False)
    assert archival.gateway.runtime_kind == "nim"
    with pytest.raises(InputContractUnavailable, match="not the reviewed"):
        contract_for(archival, "native")
    current = selected(registry, "boltz2")
    changed = replace(
        current,
        gateway=replace(
            current.gateway,
            qualification={
                "runtime_origin": {"variant_id": "boltz2-full-nim"},
            },
        ),
    )
    with pytest.raises(InputContractUnavailable, match="variant"):
        contract_for(changed, "native")
    # Rebuilding the same reviewed adapter must not create a new schema gate.
    rebuilt = replace(current, gateway=replace(current.gateway, runtime_image_digest="sha256:" + "9" * 64))
    assert contract_for(rebuilt, "native").input_schema == contract_for(current, "native").input_schema


def test_adapter_identities_match_current_selected_catalog():
    for model_id, adapter in _resource("runtime-adapters.json").items():
        declaration = json.loads((SOLUTION_ROOT.parent / adapter["source"]).read_text())
        assert declaration["model_id"] == model_id
        assert declaration["variant_id"] == adapter["variant_id"]
        assert declaration["record"]["runtime"]["kind"] == adapter["runtime_kind"]


def test_openfold_four_fields_match_actual_parser(registry):
    path = SOLUTION_ROOT / "models/structure/openfold2-upstream/server.py"
    tree = ast.parse(path.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "parse_request")
    namespace = {
        "IDENTIFIER": re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"),
        "SEQUENCE": re.compile(r"[ACDEFGHIKLMNPQRSTVWY]+"),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)  # noqa: S102
    contract = contract_for(selected(registry, "openfold2"), "native")
    good = contract.examples[0]
    assert namespace["parse_request"](good) == (good["input_id"], good["sequence"])
    for field, value in [
        ("selected_models", [2]),
        ("relax_prediction", True),
        ("alignments", {}),
        ("templates", []),
        ("sequence", "ABC"),
    ]:
        invalid = good | {field: value}
        with pytest.raises(ValueError):
            namespace["parse_request"](invalid)
        assert not Draft202012Validator(contract.input_schema).is_valid(invalid)


def test_boltz_rejects_nim_only_fields_and_missing_msa(registry):
    contract = contract_for(selected(registry, "boltz2"), "native")
    validator = Draft202012Validator(contract.input_schema)
    good = contract.examples[0]
    for name in ("ligands", "affinity", "templates", "random_seed", "step_scale"):
        assert not validator.is_valid(good | {name: 1})
    missing_msa = copy.deepcopy(good)
    del missing_msa["polymers"][0]["msa"]
    assert not validator.is_valid(missing_msa)
    assert not validator.is_valid(good | {"sampling_steps": 501})
    assert "NOT consumed" in contract.input_schema["$defs"]["A3M"]["properties"]["rank"]["description"]


def test_aging_units_and_full_panel_not_replaced_by_toy(registry):
    pheno = contract_for(selected(registry, "phenoage"), "native")
    value = copy.deepcopy(pheno.examples[0])
    value["samples"][0]["c_reactive_protein_mg_dl"] = 0
    assert not Draft202012Validator(pheno.input_schema).is_valid(value)
    altum = contract_for(selected(registry, "altumage"), "native")
    assert altum.examples == ()  # The full example requires pinned canonical CpG assets.
    assert "fixtures.py" in altum.input_schema["description"]
    assert not Draft202012Validator(altum.input_schema).is_valid(
        {
            "cpg_sites": ["cg00000001"],
            "samples": [{"sample_id": "toy", "beta_values": [0.5]}],
        }
    )


def test_every_scientific_profile_uses_canonical_schema_and_examples():
    catalog = ScientificProfileCatalog.load(CATALOG_ROOT)
    assert len(catalog._profiles) == 10
    before = json.dumps({name: validator.schema for name, validator in catalog._validators.items()}, sort_keys=True)
    for profile in catalog._profiles.values():
        contract = scientific_contract_for(profile, catalog=catalog)
        schema = contract.input_schema
        Draft202012Validator.check_schema(schema)
        assert schema["properties"]["parameters"]["properties"]
        assert all(field.get("description") for field in schema["properties"]["parameters"]["properties"].values())
        assert schema["properties"]["operation"]["enum"] == list(profile.operations)
        assert schema["properties"]["service_class"]["enum"] == list(profile.service_classes)
        assert len(contract.examples) == 1
        for example in contract.examples:
            Draft202012Validator(schema).validate(example)
            catalog.validate_request(profile, example)
            invalid = copy.deepcopy(example)
            invalid["parameters"]["imaginary_parameter"] = True
            assert not Draft202012Validator(schema).is_valid(invalid)
    after = json.dumps({name: validator.schema for name, validator in catalog._validators.items()}, sort_keys=True)
    assert before == after


def test_scientific_clone_inherits_source():
    from fs2_serve.apps_scientific import AppScientificProfile

    catalog = ScientificProfileCatalog.load(CATALOG_ROOT)
    profile = catalog.get("rfdiffusion")
    app = SimpleNamespace(public_model_id="app-independent-rf", display_name="Independent RF")
    clone = AppScientificProfile(profile.value, app)
    contract = scientific_contract_for(clone, catalog=catalog)
    source = scientific_contract_for(profile, catalog=catalog)
    assert contract.model_ref == "rfdiffusion"
    assert contract.input_schema["properties"] == source.input_schema["properties"]
    assert clone.model_id == "app-independent-rf"


@pytest.mark.parametrize("model_id", ["qwen3-8b", "nv-reason-cxr-3b", "glm-5-2-fp8"])
def test_chat_actual_image_schema_and_modality(registry, model_id):
    contract = contract_for(selected(registry, model_id, "openai-chat"), "openai-chat")
    schema = contract.input_schema
    assert len(schema["properties"]) == 65
    assert "model" not in schema["properties"]
    assert schema["properties"]["stream"]["const"] is False
    assert all(field.get("description") for field in schema["properties"].values())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(contract.examples[0])
    image = {
        "messages": [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,c3ludGhldGlj"}}],
            }
        ]
    }
    assert validator.is_valid(image) is (model_id == "nv-reason-cxr-3b")
    assert not validator.is_valid(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "video_url", "video_url": {"url": "https://example.invalid/video.mp4"}}],
                }
            ]
        }
    )
    assert not validator.is_valid(contract.examples[0] | {"stream": True})
    source = json.loads((CATALOG_ROOT / "models" / f"{model_id}.json").read_text())
    assert source["runtime"]["image"]["digest"] == _resource("vllm-chat.json")["source"]["image"]


def test_all_catalog_source_endpoints_are_covered(registry):
    ids = set()
    for path in (CATALOG_ROOT / "models").glob("*.json"):
        record = json.loads(path.read_text())
        model_id = record["model"]["id"]
        ids.add(model_id)
        for protocol in record["interface"]["protocols"]:
            result = contract_for(selected(registry, model_id, protocol), protocol)
            assert len(result.input_schema["description"]) > 80
    assert ids == set(NATIVE_MODELS) - {"altumage", "phenoage"} | {
        "qwen3-8b",
        "nv-reason-cxr-3b",
        "glm-5-2-fp8",
    }


def test_evo2_schema_matches_actual_image_parser(registry):
    import math
    from dataclasses import dataclass
    from typing import Any

    namespace = {"Any": Any, "math": math, "DNA": re.compile(r"^[ACGTN]+$", re.IGNORECASE)}
    source = _resource("evo2-source.json")["request_classes"]
    exec(compile(source, "evo2-live-request-source", "exec", dont_inherit=True), namespace)  # noqa: S102
    parser = dataclass(namespace["GenerationRequest"])
    contract = contract_for(selected(registry, "evo2-40b"), "native")
    good = contract.examples[0]
    assert parser.from_mapping(good).sequence == good["sequence"]
    validator = Draft202012Validator(contract.input_schema)
    for field, value in [
        ("num_tokens", 513),
        ("temperature", 0),
        ("top_k", 0),
        ("enable_logits", True),
        ("enable_elapsed_ms_per_token", False),
        ("sequence", "AUCG"),
    ]:
        bad = good | {field: value}
        with pytest.raises(namespace["RuntimeFailure"]):
            parser.from_mapping(bad)
        assert not validator.is_valid(bad)


def test_sdxl_binary_or_json_response_and_cosmos_mode_fields(registry):
    sdxl = contract_for(selected(registry, "sdxl"), "native")
    validator = Draft202012Validator(sdxl.input_schema)
    for output in ("image/png", "b64_json"):
        validator.validate({"prompt": "A cube", "response_format": output})
    assert not validator.is_valid({"prompt": "A cube", "response_format": "jpeg"})
    cosmos = contract_for(selected(registry, "cosmos3-nano"), "native")
    validator = Draft202012Validator(cosmos.input_schema)
    validator.validate({"mode": "text-to-image", "prompt": "A cube", "output_format": "png"})
    assert not validator.is_valid({"mode": "text-to-image", "prompt": "A cube", "num_frames": 25})
    assert not validator.is_valid({"mode": "text-to-video", "prompt": "A cube", "output_format": "png"})

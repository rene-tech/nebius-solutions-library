from __future__ import annotations

import ast
import copy
import gzip
import ipaddress
import json
import math
import re
from dataclasses import replace
from types import SimpleNamespace
from typing import Annotated, Literal, Union
from urllib.parse import urlsplit

import pydantic
import pytest
import yaml
from conftest import CATALOG_ROOT, SOLUTION_ROOT
from jsonschema import Draft202012Validator

from fs2_serve.model_input_contracts import (
    InputContractUnavailable,
    _resource,
    contract_for,
    cosmos_specialized_contracts,
    packaged_input_fixture,
    scientific_contract_for,
)
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
    "cellpose-cpsam-v2",
    "scvi-scanvi",
    "sam2-1-hiera-large",
    "wan2-2-t2v-nim",
    "wan2-2-i2v-nim",
    "ace-step-1-5",
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
    assert len(altum.examples) == 1
    assert altum.examples[0]["cpg_sites"]["sha256"] == (
        "0037a70f092cc2e157d07253a1849ff5d5035c6b60d6b7fb518f20ebc9e8f15e"
    )
    assert "fixtures.py" in altum.input_schema["description"]
    assert not Draft202012Validator(altum.input_schema).is_valid(
        {
            "cpg_sites": ["cg00000001"],
            "samples": [{"sample_id": "toy", "beta_values": [0.5]}],
        }
    )


def test_segment_contract_publishes_a_real_deterministic_nifti_fixture(registry):
    contract = contract_for(selected(registry, "nv-segment-ct"), "native")
    example = contract.examples[0]
    fixture_id = example["input_nifti_base64"]["fixture_id"]
    first, media_type = packaged_input_fixture(fixture_id)
    second, _ = packaged_input_fixture(fixture_id)
    assert first == second
    assert media_type == "application/gzip"
    assert gzip.decompress(first)[344:348] == b"n+1\x00"


def test_visual_science_contracts_keep_file_bytes_out_of_model_context(registry):
    cellpose = contract_for(selected(registry, "cellpose-cpsam-v2"), "native")
    scvi = contract_for(selected(registry, "scvi-scanvi"), "native")
    for contract, field, media_type in (
        (cellpose, "image_base64", "image/png"),
        (scvi, "anndata_base64", "application/x-hdf5"),
    ):
        schema = contract.input_schema["properties"][field]
        assert schema["x-fs2-artifact-materialization"] == "base64"
        assert media_type in schema["x-fs2-artifact-media-types"]
        assert contract.examples[0][field]["artifact_id"].startswith("00000000-")
        Draft202012Validator(contract.input_schema).validate(contract.examples[0])
    assert cellpose.input_schema["properties"]["research_only"]["const"] is True
    assert scvi.input_schema["properties"]["max_epochs"]["maximum"] == 20
    assert scvi.input_schema["properties"]["research_only"]["const"] is True


def test_every_scientific_profile_uses_canonical_schema_and_examples():
    catalog = ScientificProfileCatalog.load(CATALOG_ROOT)
    declared = json.loads((CATALOG_ROOT / "contracts/scientific-workload-profiles.json").read_text())["profiles"]
    declared_models = {profile["model_id"] for profile in declared}
    assert declared_models == {
        "boltzgen", "proteina-complexa", "bindcraft", "mosaic", "rfdiffusion",
        "esmfold2", "esmfold2-fast", "openfold3-openbind", "protenix-v2", "alphafold3",
        "cosmos3-lerobot-augmentation",
    }
    assert len(declared) == len(declared_models)
    assert set(catalog._profiles) == declared_models
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
    assert ids == set(NATIVE_MODELS) - {
        "altumage",
        "phenoage",
        "cellpose-cpsam-v2",
        "scvi-scanvi",
        "sam2-1-hiera-large",
        "wan2-2-t2v-nim",
        "wan2-2-i2v-nim",
        "ace-step-1-5",
    } | {
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


def test_cosmos_media_contracts_are_mode_specific_and_unqualified_actions_are_rejected(registry):
    model = selected(registry, "cosmos3-nano")
    generic = contract_for(model, "native")
    generic_validator = Draft202012Validator(generic.input_schema)
    artifact = {
        "artifact_id": "00000000-0000-4000-8000-000000000021",
        "sha256": "a" * 64,
        "size_bytes": 4096,
        "media_type": "video/mp4",
        "compression": "none",
    }
    generic_validator.validate(
        {
            "mode": "video-to-video",
            "prompt": "Preserve the robot and change the lighting.",
            "vision_path": "https://media.example.test/input.mp4",
            "condition_frame_indexes_vision": [0, 1],
            "output_format": "mp4",
            "output_delivery": "artifact",
        }
    )
    invalid = (
        {"mode": "video-to-video", "prompt": "missing reference"},
        {
            "mode": "video-to-video",
            "prompt": "local path",
            "vision_path": "/tmp/customer.mp4",  # noqa: S108 - deliberate rejected customer input
        },
        {
            "mode": "video-to-video",
            "prompt": "two references",
            "vision_path": "https://media.example.test/input.mp4",
            "input_reference": artifact,
        },
        {
            "mode": "video-to-video",
            "prompt": "wrong-mode sound",
            "input_reference": artifact,
            "generate_sound": True,
        },
        {
            "mode": "text-to-video",
            "prompt": "sound duration without sound",
            "generate_sound": False,
            "sound_duration": 2,
        },
        {
            "mode": "transfer-video",
            "prompt": "depth requires a reference",
            "controls": [{"control_type": "depth"}],
        },
        {
            "mode": "transfer-video",
            "prompt": "transfer size must use the existing WIDTHxHEIGHT syntax",
            "controls": [{"control_type": "edge"}],
            "input_reference": artifact,
            "size": "640by480",
        },
        {
            "mode": "forward-dynamics",
            "prompt": "unqualified action runtime",
            "input_reference": artifact,
            "domain_name": "av",
            "raw_action_dim": 9,
            "action_chunk_size": 16,
        },
        {
            "mode": "inverse-dynamics",
            "prompt": "unqualified action runtime",
            "input_reference": artifact,
            "domain_name": "av",
            "raw_action_dim": 9,
            "action_chunk_size": 16,
            "num_frames": 17,
        },
    )
    assert all(not generic_validator.is_valid(value) for value in invalid)

    contracts = cosmos_specialized_contracts(model)
    assert {item[0] for item in contracts} == {
        "cosmos3_nano_text_to_image",
        "cosmos3_nano_text_to_video",
        "cosmos3_nano_image_to_video",
        "cosmos3_nano_video_to_video",
        "cosmos3_nano_transfer_video",
    }
    for _, contract, defaults, _, _ in contracts:
        Draft202012Validator.check_schema(contract.input_schema)
        assert "mode" not in contract.input_schema["properties"]
        assert "output_delivery" not in contract.input_schema["properties"]
        for example in contract.examples:
            Draft202012Validator(contract.input_schema).validate(example)
        generic_validator.validate(contract.examples[0] | defaults)


def cosmos_adapter_request():
    """Load actual YAML DTOs and validators without importing/starting the GPU adapter."""
    path = SOLUTION_ROOT / "models/general-media/k8s/cosmos3-nano.yaml"
    documents = yaml.safe_load_all(path.read_text())
    source = next(doc["data"]["adapter.py"] for doc in documents if "adapter.py" in doc.get("data", {}))
    nodes = []
    constants = {"MODEL_REPOSITORY", "MODEL_REVISION", "RESOLVED_MODEL", "ACTION_DOMAINS", "GenerateRequest"}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in constants:
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "validate_reference_syntax":
            nodes.append(node)
        elif isinstance(node, ast.ClassDef) and (node.name.endswith("Request") or node.name == "TransferControl"):
            nodes.append(node)
    namespace = {name: getattr(pydantic, name) for name in ("BaseModel", "ConfigDict", "Field", "model_validator")}
    namespace.update(
        Annotated=Annotated, Literal=Literal, Union=Union, ipaddress=ipaddress, math=math, urlsplit=urlsplit
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec", dont_inherit=True), namespace)  # noqa: S102
    return pydantic.TypeAdapter(namespace["GenerateRequest"])


@pytest.mark.parametrize("size", ["256x256", "512x288", "640x480", "1280x720"])
def test_cosmos_transfer_exposes_existing_exact_size_in_both_tool_contracts(registry, size):
    model = selected(registry, "cosmos3-nano")
    contract, defaults = next(
        (contract, defaults)
        for name, contract, defaults, _, _ in cosmos_specialized_contracts(model)
        if name == "cosmos3_nano_transfer_video"
    )
    payload = {"prompt": "Recorded robot scene", "input_reference": "https://media.example.test/robot.mp4",
               "controls": [{"control_type": "edge"}], "size": size}
    Draft202012Validator(contract.input_schema).validate(payload)
    Draft202012Validator(contract_for(model, "native").input_schema).validate(payload | defaults)
    assert cosmos_adapter_request().validate_python(payload | defaults).size == size
    description = contract.input_schema["properties"]["resolution"]["description"]
    assert "explicit size" in description and "does not preserve source aspect ratio" in description


def test_cosmos_transfer_default_and_runtime_dimension_bounds_are_unchanged(registry):
    model = selected(registry, "cosmos3-nano")
    contract, defaults = next(
        (contract, defaults)
        for name, contract, defaults, _, _ in cosmos_specialized_contracts(model)
        if name == "cosmos3_nano_transfer_video"
    )
    payload = {"prompt": "Recorded robot scene", "input_reference": "https://media.example.test/robot.mp4",
               "controls": [{"control_type": "edge"}]}
    Draft202012Validator(contract.input_schema).validate(payload)
    adapter = cosmos_adapter_request()
    assert adapter.validate_python(payload | defaults).size == "448x256"
    assert contract.input_schema["properties"]["resolution"]["enum"] == [256, 480, 704, 720]
    assert contract.input_schema["properties"]["resolution"]["default"] == 480
    for invalid in ("255x256", "641x480", "1280x736", "1296x720"):
        with pytest.raises(pydantic.ValidationError):
            adapter.validate_python(payload | defaults | {"size": invalid})


def test_cosmos_continuation_describes_motion_scope_without_claiming_action_alignment(registry):
    contracts = {name: (contract, description) for name, contract, _, _, description
                 in cosmos_specialized_contracts(selected(registry, "cosmos3-nano"))}
    v2v, description = contracts["cosmos3_nano_video_to_video"]
    assert "not preserved from the full source clip" in description
    assert "one source pixel frame" in v2v.input_schema["properties"]["condition_frame_indexes_vision"]["description"]
    assert "does not guarantee" in contracts["cosmos3_nano_transfer_video"][1]


@pytest.mark.parametrize(
    "mode", ["text-to-image", "text-to-video", "image-to-video", "video-to-video", "transfer-video"]
)
def test_cosmos_specialized_and_generic_contracts_match_actual_adapter(registry, mode):
    contract, defaults = next(
        (contract, defaults)
        for _, contract, defaults, _, _ in cosmos_specialized_contracts(selected(registry, "cosmos3-nano"))
        if defaults["mode"] == mode
    )
    payload = copy.deepcopy(contract.examples[0]) | defaults
    # The gateway resolves tenant-owned references before sending to the adapter.
    if "input_reference" in payload:
        payload["input_reference"] = "https://media.example.test/fixture.mp4"
    for control in payload.get("controls", []):
        control["reference"] = "https://media.example.test/control.mp4"
    generic = contract_for(selected(registry, "cosmos3-nano"), "native")
    Draft202012Validator(generic.input_schema).validate(payload)
    parsed = cosmos_adapter_request().validate_python(payload)
    assert parsed.mode == mode
    if mode == "text-to-image":
        assert defaults == {"mode": mode, "output_format": "png"}
        assert parsed.output_format == "png"
        assert "output_delivery" not in parsed.model_dump()
    else:
        assert parsed.output_delivery == "artifact" and parsed.output_format == "mp4"


@pytest.mark.parametrize("delivery", ["inline-base64", "artifact"])
def test_cosmos_generic_t2i_rejects_delivery_but_t2v_keeps_legacy_compatibility(registry, delivery):
    generic = Draft202012Validator(contract_for(selected(registry, "cosmos3-nano"), "native").input_schema)
    adapter = cosmos_adapter_request()
    t2i = {"mode": "text-to-image", "prompt": "Synthetic image", "output_format": "png"}
    generic.validate(t2i)
    adapter.validate_python(t2i)
    assert not generic.is_valid(t2i | {"output_delivery": delivery})
    with pytest.raises(pydantic.ValidationError) as failure:
        adapter.validate_python(t2i | {"output_delivery": delivery})
    assert failure.value.errors()[0]["loc"] == ("text-to-image", "output_delivery")
    t2v = {"mode": "text-to-video", "prompt": "Synthetic video", "output_delivery": delivery}
    generic.validate(t2v)
    assert adapter.validate_python(t2v).output_delivery == delivery

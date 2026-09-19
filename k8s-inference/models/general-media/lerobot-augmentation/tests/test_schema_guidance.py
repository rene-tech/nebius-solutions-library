"""Non-LibreChat callers must see the actual scientific mode semantics too."""
import json
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
INFERENCE = ROOT.parents[2]


def test_public_and_runtime_schema_publish_identical_scientific_guidance():
    local = json.loads((ROOT / "schema/request.schema.json").read_text())
    public = json.loads((INFERENCE / "catalog/runtime/schema/cosmos3-lerobot-augmentation-request.schema.json").read_text())
    assert local == public
    Draft202012Validator.check_schema(public)
    mode = public["properties"]["augmentation"]["properties"]["mode"]
    assert "full-sequence" in mode["description"] and "latent" in mode["description"]
    assert "physical action alignment" in mode["description"]
    assert mode["enum"] == ["video-to-video", "transfer"]
    prompt = public["properties"]["augmentation"]["properties"]["prompt_template"]
    assert "{variation}" in prompt["description"] and "without" in prompt["description"]
    assert "not a numeric" in public["$defs"]["dimension"]["properties"]["strength"]["description"]
    assert "not arbitrary decoded-video" in public["$defs"]["conditioning"]["properties"]["frame_indexes"]["description"]


def test_descriptions_do_not_change_existing_fixture_admission():
    schema = json.loads((ROOT / "schema/request.schema.json").read_text())
    request = json.loads((ROOT / "fixtures/fixture-request.json").read_text())
    Draft202012Validator(schema).validate(request)

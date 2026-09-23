import copy

import pytest
from jsonschema import ValidationError

from fs2_amber import ENGINE_ID, PARAMETER_SCHEMA
from fs2_amber.contracts import canonical, normalize, request_schema


def request():
    return {"schema": PARAMETER_SCHEMA, "jobs": [{"id": "dhfr", "steps": [{"id": "production", "input": "md.in", "topology": "system.prmtop", "coordinates": "equilibrated.rst7", "expected_nsteps": 10000}]}]}


def test_native_identity_and_canonical_defaults():
    value = request()
    before = copy.deepcopy(value)
    result = normalize(value)
    assert value == before
    assert normalize(result) == result
    assert canonical(result) == canonical(normalize(value))
    assert result["jobs"][0]["steps"][0]["backend"] == "cuda-spfp"
    assert len(ENGINE_ID.rsplit(":", 1)[1]) == 64
    assert request_schema()["properties"]["schema"]["const"] == PARAMETER_SCHEMA


@pytest.mark.parametrize("field,value", [("input", "../md.in"), ("coordinates", "/tmp/a"), ("argv", ["sh", "-c", "true"]), ("backend", "auto")])
def test_reject_paths_and_untyped_execution(field, value):
    recipe = request()
    recipe["jobs"][0]["steps"][0][field] = value
    with pytest.raises((ValueError, ValidationError)):
        normalize(recipe)


def test_dynamics_needs_asserted_complete_length():
    recipe = request()
    del recipe["jobs"][0]["steps"][0]["expected_nsteps"]
    with pytest.raises(ValueError, match="expected_nsteps"):
        normalize(recipe)
    recipe["jobs"][0]["steps"][0]["task"] = "minimization"
    assert normalize(recipe)["jobs"][0]["steps"][0]["task"] == "minimization"


@pytest.mark.parametrize("kind", ["tleap", "cpptraj", "parmed"])
def test_typed_tools_require_outputs(kind):
    recipe = request()
    recipe["jobs"][0]["steps"] = [{"id": "tool", "kind": kind, "input": "native.in"}]
    with pytest.raises(ValueError, match="expected_outputs"):
        normalize(recipe)
    recipe["jobs"][0]["steps"][0]["expected_outputs"] = ["prepared.out"]
    assert normalize(recipe)["jobs"][0]["steps"][0]["kind"] == kind


def test_output_cannot_clobber_input_or_another_stage():
    recipe = request()
    step = recipe["jobs"][0]["steps"][0]
    step["coordinates"] = "production.rst7"
    with pytest.raises(ValueError, match="overwrite"):
        normalize(recipe)
    step["coordinates"] = "equilibrated.rst7"
    recipe["jobs"][0]["steps"].append({**step, "id": "second", "output_prefix": "production"})
    with pytest.raises(ValueError, match="unique"):
        normalize(recipe)

"""The repaired MolMIM DTO and discovery retain honest, unchanged budgets."""

from __future__ import annotations

import ast
from typing import Literal

import pydantic
import pytest
from conftest import SOLUTION_ROOT
from jsonschema import Draft202012Validator, ValidationError
from test_model_input_contracts import selected

from fs2_serve.model_input_contracts import _resource, contract_for


def actual_request_model():
    """Load the trusted DTO including its real cross-field validator, without torch."""
    path = SOLUTION_ROOT / "models/bionemo/molmim/server.py"
    node = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef)
                and n.name == "GenerateRequest")
    namespace = {name: getattr(pydantic, name) for name in ("BaseModel", "ConfigDict", "Field", "model_validator")}
    namespace.update(Literal=Literal)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)  # noqa: S102 - trusted repository DTO
    model = namespace["GenerateRequest"]
    model.model_rebuild(_types_namespace=namespace)
    return model


def test_actual_molmim_dto_matches_snapshot_and_preserves_limits():
    model = actual_request_model()
    schema = _resource("runtime-pydantic.json")["molmim"]["schema"]
    assert model.model_json_schema() == schema
    for field, limits in {"particles": (2, 32, 2), "iterations": (1, 16, 1), "num_molecules": (1, 16, 1)}.items():
        assert tuple(schema["properties"][field][key] for key in ("minimum", "maximum", "default")) == limits
    with pytest.raises(pydantic.ValidationError, match="particles"):
        model(smi="CCO", particles=1)
    with pytest.raises(pydantic.ValidationError, match="decode budget"):
        model(smi="CCO", num_molecules=8, particles=2, iterations=1)
    request = model(smi="CCO", num_molecules=8, particles=8, iterations=4)
    assert request.particles == 8 and request.iterations == 4


def test_discovery_explains_hard_filter_fixed_budget_and_exhaustion(registry):
    contract = contract_for(selected(registry, "molmim"), "native")
    schema = contract.input_schema
    assert "GENERATION_EXHAUSTED" in schema["description"]
    assert "particles * iterations" in schema["description"]
    assert "not proof of chemical infeasibility" in schema["description"]
    assert "Hard final-output" in schema["properties"]["min_similarity"]["description"]
    assert "soft" in schema["properties"]["min_similarity"]["description"]
    assert "sigma 0.75" in schema["properties"]["radius"]["description"]
    assert "not guaranteed" in schema["properties"]["minimize"]["description"]
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate({"smi": "CCO", "particles": 1})
    # No fields are silently introduced or enlarged by discovery.
    assert schema["properties"]["iterations"]["default"] == 1
    assert "seed" not in schema["properties"]


def test_molmim_copy_does_not_change_genmol_or_other_runtime_contracts(registry):
    genmol = contract_for(selected(registry, "genmol"), "native").input_schema
    assert "GENERATION_EXHAUSTED" not in genmol["description"]
    assert "particles" not in genmol["properties"]

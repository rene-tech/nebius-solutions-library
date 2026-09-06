"""Executable request constraints remain client errors across adapter dispatch."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from fs2_serve.scientific_batch.adapters.boltzgen import BoltzGenParameters
from fs2_serve.scientific_batch.adapters.primitives import ScientificParameterError
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer, ScientificExecutionMapError
from fs2_serve.scientific_batch.profile_catalog import ScientificRequestError


def _parameters(count: int = 20) -> dict:
    return {
        "protocol": "protein-anything",
        "batches": [{"shard_id": "one", "num_designs": count, "budget": 1, "reuse_completed": False}],
    }


def test_boltzgen_total_candidate_bound_is_a_parameter_error() -> None:
    parameters = _parameters()
    parameters["batches"].append({**parameters["batches"][0], "shard_id": "two"})
    with pytest.raises(ScientificParameterError, match="total design bound"):
        BoltzGenParameters.parse(parameters)


def test_boltzgen_published_schema_matches_executable_single_shard_limit() -> None:
    root = Path(__file__).resolve().parents[3]
    schema = json.loads((root / "catalog/runtime/schema/boltzgen-parameters.schema.json").read_bytes())
    validator = Draft202012Validator(schema)
    assert validator.is_valid(_parameters(20))
    assert not validator.is_valid(_parameters(21))
    assert schema["properties"]["batches"]["maxItems"] == 2
    assert schema["properties"]["batches"]["items"]["properties"]["budget"]["maximum"] == 3


@pytest.mark.parametrize("parameter_error", [True, False])
def test_dispatch_preserves_client_error_classification(monkeypatch, parameter_error: bool) -> None:
    def factory(*args, **kwargs):
        if parameter_error:
            raise ScientificParameterError("request has too many candidates")
        raise ValueError("operator-owned artifact identity mismatch")

    monkeypatch.setattr(
        "fs2_serve.scientific_batch.execution.importlib.import_module",
        lambda _: SimpleNamespace(compile=factory),
    )
    renderer = SimpleNamespace(plan_adapters={"boltzgen": ("test_adapter", "compile")}, variant_id=lambda _: "test")
    expected = ScientificRequestError if parameter_error else ScientificExecutionMapError
    with pytest.raises(expected):
        FileScientificManifestRenderer.plan(
            renderer,
            SimpleNamespace(model_id="boltzgen", value={}),
            {},
            access_context=SimpleNamespace(),
            input_artifacts=(),
        )

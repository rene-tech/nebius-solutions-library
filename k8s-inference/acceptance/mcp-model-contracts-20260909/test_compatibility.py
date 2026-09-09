"""Exercise actual portable parsers without importing CUDA or starting a server."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from compatibility_fixtures import ROOT, incompatible_cases, portable_payloads


def _source_definitions(path: Path, names: set[str], namespace: dict):
    """Compile only named pure parsers/constants from our owned runtime source.

    Imports, environment reads, module initialization and inference code never
    execute. This avoids a second handwritten validator drifting from runtime.
    """
    tree = ast.parse(path.read_text())
    selected = []
    found = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names:
            selected.append(node)
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            targets = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if targets & names:
                selected.append(node)
                found |= targets & names
    assert found == names, "actual runtime parser contract changed"
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


@pytest.fixture(scope="module")
def parsers():
    boltz = _source_definitions(
        ROOT / "models/bionemo/boltz2/server.py",
        {"AMINO_ACIDS", "A3M", "MSASearch", "MSA", "Polymer", "PredictRequest"},
        {"BaseModel": BaseModel, "ConfigDict": ConfigDict, "Field": Field,
         "field_validator": field_validator, "Literal": Literal, "__name__": __name__},
    )
    request = boltz["PredictRequest"]
    request.model_rebuild(_types_namespace=boltz)
    openfold = _source_definitions(
        ROOT / "models/structure/openfold2-upstream/server.py",
        {"IDENTIFIER", "SEQUENCE", "parse_request"}, {"re": re},
    )
    return {"boltz2": request.model_validate, "openfold2": openfold["parse_request"]}


@pytest.mark.parametrize("model_id", ["boltz2", "openfold2"])
def test_original_two_synthetic_payloads_are_accepted_without_changes(parsers, model_id):
    first, second = portable_payloads(model_id)
    assert first != second
    assert parsers[model_id](first)
    assert parsers[model_id](second)


@pytest.mark.parametrize("model_id,case_id,payload,location", incompatible_cases(), ids=lambda value: value if isinstance(value, str) else None)
def test_nim_shape_mismatch_is_explicit_not_silently_dropped(parsers, model_id, case_id, payload, location):
    with pytest.raises((ValidationError, ValueError)) as failure:
        parsers[model_id](payload)
    assert location in str(failure.value), case_id


def test_boltz_rank_is_accepted_but_not_a_runtime_selection_control():
    tree = ast.parse((ROOT / "models/bionemo/boltz2/server.py").read_text())
    predict = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_predict")
    assert not any(isinstance(node, ast.Attribute) and node.attr == "rank" for node in ast.walk(predict))


def test_examples_do_not_call_http_or_model_runtime():
    first, second = portable_payloads("boltz2")
    for payload in (first, second):
        polymer = payload["polymers"][0]
        assert len(polymer["sequence"]) == 20
        assert polymer["msa"]["msa_search"]["a3m"]["alignment"] == ">query\n" + polymer["sequence"]
    assert portable_payloads("openfold2")[0]["selected_models"] == [1]

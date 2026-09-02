from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "catalog" / "profiles"
CONTROL_CONTRACTS = ROOT / "components" / "control-plane" / "contracts"
RECEIPT = PROFILES / "evidence/h100-qwen-cosmos-elasticity-qualification-20260902.json"
RECEIPT_SHA256 = "1cd246c27c5a4c4cc639a189c5b5fc33a8fcd7080f6b621f4bd1bc2c9d5401a6"


def _document(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_h100_elasticity_receipt_is_schema_valid_and_content_addressed() -> None:
    schema = _document(PROFILES / "model-elasticity-qualification-receipt.schema.json")
    receipt_bytes = RECEIPT.read_bytes()
    receipt = json.loads(receipt_bytes)

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    ).validate(receipt)
    assert hashlib.sha256(receipt_bytes).hexdigest() == RECEIPT_SHA256


def test_h100_elasticity_receipt_binds_both_exact_live_tuples() -> None:
    receipt = _document(RECEIPT)
    models = {item["model_id"]: item for item in receipt["models"]}

    assert list(models) == ["cosmos3-nano", "qwen3-8b"]
    assert models["cosmos3-nano"]["source"] == {
        "public_receipt_sha256": "838c58054636e565ff3c680e04387138ebd280bfe08addbb03dfec3b0a717c55",
        "private_receipt_sha256": "c103f5b0744f714f18e573a0ed37db22eb9f474138042fdf3e929b53160ef140",
    }
    assert models["qwen3-8b"]["source"] == {
        "public_receipt_sha256": "01331cf3f27570da5a49043f1cf1a81208936c7c6120e569d868cb30f0ff29b0",
        "private_receipt_sha256": "69e02c3d56e59403ac4725799186e5c2a1e34f9fcc80001781350756fcb505fc",
    }
    assert {item["placement"]["pool_id"] for item in models.values()} == {
        "h100-reserved-8x"
    }
    assert {item["placement"]["accelerator_class"] for item in models.values()} == {
        "nvidia-h100-sxm5-80gb"
    }
    assert all(item["result"] == "PASS" for item in models.values())
    assert all(item["cache"]["outcome"] == "cache-hit" for item in models.values())


def test_live_release_projects_only_the_receipted_models_as_elastic() -> None:
    receipt = _document(RECEIPT)
    inventory = _document(CONTROL_CONTRACTS / "all-models-live-services.json")
    projection = _document(CONTROL_CONTRACTS / "model-qualification-projection.json")

    expected = [item["model_id"] for item in receipt["models"]]
    qualification = inventory["qualification"]
    assert qualification["elasticity_qualified_models"] == expected
    assert qualification["evidence"]["elasticity_acceptance_sha256"] == RECEIPT_SHA256
    elastic_rows = [
        row["model_id"]
        for row in projection["rows"]
        if row["states"]["elasticity_qualified"]
    ]
    assert elastic_rows == expected
    assert all(
        row["evidence"]["elasticity_acceptance_sha256"] == RECEIPT_SHA256
        for row in projection["rows"]
    )

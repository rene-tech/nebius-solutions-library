from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "catalog" / "profiles"
H100_CLASS = "nvidia-h100-sxm5-80gb"
RECEIPT = PROFILES / "evidence/h100-qwen-cosmos-runtime-qualification-20260902.json"
RECEIPT_SHA256 = "0d66f4fab33908b15a9a89bc9977752e21c9f819307198ac72d3e770ee8b208f"


def _document(name: str) -> dict[str, Any]:
    return json.loads((PROFILES / name).read_text(encoding="utf-8"))


def test_h100_candidates_use_sxm5_and_only_promote_the_exact_receipted_tuple() -> None:
    pools = _document("accelerator-pools.json")
    compatibility = _document("model-accelerator-compatibility.json")

    accelerator_classes = pools["accelerator_classes"]
    pool_templates = pools["pool_templates"]
    assert H100_CLASS in accelerator_classes
    assert "nvidia-h100-sxm-80gb" not in accelerator_classes
    assert pool_templates["unbound-h100"]["accelerator_class"] == H100_CLASS

    models = compatibility["models"]
    qwen = models["qwen3-8b"]["runtimes"]["catalog-canonical"]
    qwen_h100 = next(
        binding
        for binding in qwen["bindings"]
        if binding["accelerator_class"] == H100_CLASS
    )
    assert qwen_h100["enabled"] is True
    assert qwen_h100["state"] == "hardware-validated"
    assert qwen_h100["evidence"] == (
        "catalog/profiles/evidence/"
        f"h100-qwen-cosmos-runtime-qualification-20260902.json@sha256:{RECEIPT_SHA256}"
    )
    assert not any(
        candidate["accelerator_class"] == H100_CLASS
        for candidate in qwen["qualification_candidates"]
    )

    cosmos = models["cosmos3-nano"]["runtimes"]["catalog-canonical"]
    cosmos_h100 = next(
        binding
        for binding in cosmos["bindings"]
        if binding["accelerator_class"] == H100_CLASS
    )
    assert cosmos_h100["enabled"] is True
    assert cosmos_h100["state"] == "hardware-validated"
    assert cosmos_h100["evidence"] == qwen_h100["evidence"]


def test_h100_receipt_is_schema_valid_content_addressed_and_model_scoped() -> None:
    schema = _document("h100-runtime-qualification-receipt.schema.json")
    receipt_bytes = RECEIPT.read_bytes()
    receipt = json.loads(receipt_bytes)

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    ).validate(receipt)
    assert hashlib.sha256(receipt_bytes).hexdigest() == RECEIPT_SHA256
    assert [item["model_id"] for item in receipt["models"]] == [
        "cosmos3-nano",
        "qwen3-8b",
    ]
    assert receipt["source_evidence"]["prior_http_mcp_acceptance_sha256"] == (
        "9719765e1986bbeed853de9b30a4740758ade82a4f4510c63e1f5fd7eb58c550"
    )
    assert all(item["runtime_ready"] for item in receipt["models"])
    assert all(item["semantic_qualified"] for item in receipt["models"])
    assert all(item["http_mcp_qualified"] for item in receipt["models"])

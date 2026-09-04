from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR_PATH = SOLUTION_ROOT / "acceptance/scientific-fleet/validate_secondary_inputs.py"
SPEC = importlib.util.spec_from_file_location("fs2_scientific_secondary_acceptance_compile", VALIDATOR_PATH)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VALIDATOR
SPEC.loader.exec_module(VALIDATOR)


def test_all_secondary_public_inputs_compile_through_production_adapters() -> None:
    summary = VALIDATOR.validate_all(compile_adapters=True)

    assert set(summary["models"]) == VALIDATOR.EXPECTED_MODELS
    assert summary["models"]["alphafold3"]["stages"] == [
        "data-pipeline",
        "inference",
    ]
    assert summary["models"]["esmfold2"]["stages"] == [
        "prepare-input",
        "fold",
    ]
    assert summary["models"]["protenix-v2"]["stages"] == [
        "prepare-data",
        "sample-structure",
    ]

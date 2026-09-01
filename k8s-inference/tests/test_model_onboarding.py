"""Expose the model-onboarding contract suite to the solution's CI discovery."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parents[1] / "model-onboarding/tests/test_compile_model.py"
)
SPEC = importlib.util.spec_from_file_location(
    "fs2_model_onboarding_contract_tests", SOURCE
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

# unittest discovery loads this alias; the test implementation remains in one file.
ModelOnboardingCompilerTests = MODULE.ModelOnboardingCompilerTests

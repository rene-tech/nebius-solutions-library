"""The campaign's explicit100-step choice is not a public runtime default."""
import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from fs2_serve.scientific_batch.adapters.proteina_complexa import ProteinaParameters
from fs2_serve.scientific_batch.adapters.common import ScientificAdapterError

MODEL_ROOT = Path(__file__).resolve().parents[1]
SOLUTION = MODEL_ROOT.parents[3]
SCHEMA = json.loads((SOLUTION / "catalog/runtime/schema/proteina-complexa-parameters.schema.json").read_text())
TARGETS = [("protein-target", "02_PDL1"), ("ligand-target", "41_7BKC_LIGAND"), ("ame", "M0584_1ldm")]


@pytest.mark.parametrize("variant,target", TARGETS)
def test_hosted_steps_are_required_and_explicit_values_are_preserved(variant, target):
    request = {"variant": variant, "target_id": target, "run_name": "sampling-contract", "seed": 7, "num_samples": 1}
    assert list(Draft202012Validator(SCHEMA).iter_errors(request))
    with pytest.raises(ScientificAdapterError):
        ProteinaParameters.parse(request)
    for steps in (1, 100, 400, 2000):
        value = dict(request, diffusion_steps=steps)
        assert not list(Draft202012Validator(SCHEMA).iter_errors(value))
        assert ProteinaParameters.parse(value).diffusion_steps == steps


@pytest.mark.parametrize("variant", ["protein", "ligand", "ame"])
def test_standalone_upstream_400_default_is_unchanged(variant):
    spec = importlib.util.spec_from_file_location("sampling_contract_entrypoint", MODEL_ROOT / "runtime_entrypoint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.normalise_request({"variant": variant})["nsteps"] == 400
    assert module.normalise_request({"variant": variant, "nsteps": 100})["nsteps"] == 100

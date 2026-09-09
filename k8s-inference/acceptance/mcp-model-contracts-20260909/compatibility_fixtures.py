"""Original synthetic portable-model fixtures; no HTTP or model execution.

The positive payloads come from the repository's existing two-probe validators.
The negative cases change only the API shape to illustrate toolkit/runtime gaps.
These are contract checks, not additional model or biological qualification.
"""

from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
VALIDATORS = ROOT / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2"
TOOLKIT_REVISION = "0e67a612e4045f007e38fa77adc8f3ebfc5616b6"


def _validator(name: str, relative_path: str) -> ModuleType:
    key = f"fs2_mcp_compatibility_{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, VALIDATORS / relative_path)
        if spec is None or spec.loader is None:
            raise ValueError(f"cannot load original {name} fixture validator")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


def portable_payloads(model_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return untouched existing two-probe payloads, with stable test labels."""
    paths = {
        "boltz2": "boltz2-native/validate_boltz2.py",
        "openfold2": "validate_openfold2.py",
    }
    validator = _validator(model_id, paths[model_id])
    first, second = validator.build_probes(("mcp-contract-a", "mcp-contract-b"))
    return deepcopy(first.payload), deepcopy(second.payload)


def incompatible_cases() -> list[tuple[str, str, dict[str, Any], str]]:
    """API-shape negatives from the pinned toolkit; never submitted live."""
    boltz, _ = portable_payloads("boltz2")
    openfold, _ = portable_payloads("openfold2")
    no_msa = deepcopy(boltz)
    del no_msa["polymers"][0]["msa"]
    flattened_msa = deepcopy(boltz)
    flattened_msa["polymers"][0]["msa"] = {"alignment": ">query\nACDEFGHIKLMNPQRSTVWY", "format": "a3m"}
    rna = deepcopy(boltz)
    rna["polymers"][0]["molecule_type"] = "rna"
    no_relax = deepcopy(openfold)
    del no_relax["relax_prediction"]
    return [
        ("boltz2", "toolkit_optional_msa", no_msa, "polymers.0.msa"),
        ("boltz2", "flat_msa_is_not_nested_record", flattened_msa, "polymers.0.msa"),
        ("boltz2", "non_protein_polymer", rna, "polymers.0.molecule_type"),
        ("boltz2", "empty_ligands_still_unsupported", {**boltz, "ligands": []}, "ligands"),
        ("boltz2", "empty_constraints_still_unsupported", {**boltz, "constraints": []}, "constraints"),
        ("boltz2", "workflow_step_scale", {**boltz, "step_scale": 1.638}, "step_scale"),
        ("boltz2", "workflow_full_pae", {**boltz, "write_full_pae": True}, "write_full_pae"),
        ("boltz2", "nim_sampling_range", {**boltz, "sampling_steps": 1000}, "sampling_steps"),
        ("boltz2", "nim_sample_count", {**boltz, "diffusion_samples": 25}, "diffusion_samples"),
        ("openfold2", "toolkit_local_omits_relax", no_relax, "expected input_id"),
        ("openfold2", "external_alignment", {**openfold, "alignments": {}}, "expected input_id"),
        ("openfold2", "explicit_templates", {**openfold, "explicit_templates": []}, "expected input_id"),
        ("openfold2", "template_switch", {**openfold, "use_templates": False}, "expected input_id"),
        ("openfold2", "multiple_parameter_sets", {**openfold, "selected_models": [1, 2]}, "parameter set 1"),
        ("openfold2", "relaxation", {**openfold, "relax_prediction": True}, "relax_prediction=false"),
    ]

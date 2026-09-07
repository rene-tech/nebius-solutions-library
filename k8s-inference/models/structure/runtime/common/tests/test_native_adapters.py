from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType


RUNTIME = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RUNTIME))
for dependency in ("numpy", "torch", "yaml"):
    sys.modules.setdefault(dependency, ModuleType(dependency))

from adapters.diffdock import Adapter as DiffDockAdapter  # noqa: E402
from adapters.proteinmpnn import Adapter as ProteinMPNNAdapter  # noqa: E402


def test_proteinmpnn_native_response_is_real_sample_data_not_fs2_envelope() -> None:
    sequence = "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"
    probabilities = [[1.0 / 21.0] * 21 for _ in sequence]
    output = {
        "seed": 2370,
        "designed_chains": ["A"],
        "native_sequence": sequence,
        "sequences": [
            {
                "sample": 1,
                "sequence": "A" * len(sequence),
                "score": 0.5,
                "global_score": 0.6,
                "seq_recovery": 0.1,
                "probabilities": probabilities,
            }
        ],
    }
    response = ProteinMPNNAdapter().render_native_response(
        "/biology/ipd/proteinmpnn/predict", {}, output
    )
    assert set(response) == {"mfasta", "scores", "probs"}
    assert "seed=2370" in response["mfasta"]
    assert "designed_chains=['A']" in response["mfasta"]
    assert response["scores"] == [0.5]
    assert response["probs"] == [probabilities]


def test_diffdock_native_response_preserves_actual_pose_and_confidence() -> None:
    request = {"ligand": "CCO", "protein": "ATOM payload"}
    output = {
        "poses": [{"sdf": "actual-pose\n", "confidence": -1.25}],
    }
    response = DiffDockAdapter().render_native_response(
        "/molecular-docking/diffdock/generate", request, output
    )
    assert response == {
        "details": "success: generated 1 pose(s)",
        "ligand": "CCO",
        "ligand_positions": ["actual-pose\n"],
        "position_confidence": [-1.25],
        "protein": "ATOM payload",
        "status": "success",
        "trajectory": [""],
    }


def test_frozen_proteinmpnn_validator_accepts_the_native_projection() -> None:
    validator_path = (
        RUNTIME.parents[2]
        / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2"
        / "proteinmpnn-native/validate_proteinmpnn.py"
    )
    spec = importlib.util.spec_from_file_location("proteinmpnn_validator", validator_path)
    assert spec is not None and spec.loader is not None
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)

    sequence = validator.EXPECTED_INPUT_SEQUENCE
    probabilities = [[1.0 / 21.0] * 21 for _ in sequence]
    output = {
        "seed": 2370,
        "designed_chains": ["A"],
        "native_sequence": sequence,
        "sequences": [
            {
                "sample": 1,
                "sequence": "A" * len(sequence),
                "score": 0.5,
                "global_score": 0.6,
                "seq_recovery": 0.1,
                "probabilities": probabilities,
            }
        ],
    }
    response = ProteinMPNNAdapter().render_native_response(
        "/biology/ipd/proteinmpnn/predict", {}, output
    )
    invariant = validator._validate_response(response, 2370)
    assert invariant["probability_shape"] == [1, 76, 21]

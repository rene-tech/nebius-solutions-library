"""Qualification rejects matching byte inventories with invalid scientific output."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[3] / "models/visual-science/scvi-scanvi/qualify_outputs.py"
SPEC = importlib.util.spec_from_file_location("scvi_output_qualification", PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def fixture(tmp_path, *, latent=None, labels=None, probabilities=None, cells=2, method="scanvi"):
    data = tmp_path / "data"
    data.mkdir()
    values = {
        "latent_embeddings.csv": latent or "cell_id,0,1\na,1,2\nb,3,4\n",
        "predicted_labels.csv": labels or "cell_id,scanvi_prediction\na,T\nb,B\n",
        "label_probabilities.csv": probabilities or "cell_id,T,B\na,0.8,0.2\nb,0.1,0.9\n",
    }
    files = []
    for name, value in values.items():
        raw = value.encode()
        (data / name).write_bytes(raw)
        files.append({"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)})
    result = tmp_path / "result.json"
    result.write_text(
        json.dumps(
            {
                "status": "succeeded",
                "files": files,
                "cells": cells,
                "latent_dimensions": 2,
                "operation_id": "synthetic",
                "parameters": {"method": method},
            }
        )
    )
    return result, data


def test_output_rows_and_hashes_are_verified(tmp_path):
    result = module.validate(*fixture(tmp_path))
    assert result["cells"] == 2
    assert result["annotation_validated"]
    assert result["biological_accuracy_or_convergence_claimed"] is False


@pytest.mark.parametrize(
    "values",
    [
        {"latent": "cell_id,0,1\na,nan,2\nb,3,4\n"},
        {"latent": "cell_id,0,1\na,1,2\na,3,4\n"},
        {"probabilities": "cell_id,T,B\na,0.8,0.8\nb,0.1,0.9\n"},
        {"labels": "cell_id,scanvi_prediction\na,B\nb,T\n"},
        {"labels": "cell_id,scanvi_prediction\nx,T\nb,B\n"},
        {"cells": 3},
    ],
)
def test_valid_inventory_does_not_mask_invalid_outputs(tmp_path, values):
    with pytest.raises(ValueError):
        module.validate(*fixture(tmp_path, **values))


def test_corrupted_download_is_rejected(tmp_path):
    result, data = fixture(tmp_path)
    (data / "latent_embeddings.csv").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="inventory"):
        module.validate(result, data)


def test_scvi_does_not_claim_annotation(tmp_path):
    result, data = fixture(tmp_path, method="scvi")
    assert module.validate(result, data)["annotation_validated"] is False

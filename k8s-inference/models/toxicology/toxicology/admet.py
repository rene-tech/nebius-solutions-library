"""ADMET-AI adapter: retain the exact upstream model and preprocessing."""

from __future__ import annotations

import csv
import importlib.metadata
import math
import os
from pathlib import Path


class ADMETRuntime:
    model_id = "admet-ai"

    def __init__(self):
        import admet_ai
        import torch
        from admet_ai import ADMETModel

        torch.set_num_threads(int(os.getenv("TOX_CPU_THREADS", "2")))
        # DrugBank percentiles are neither calibrated uncertainty nor necessary
        # for inference. Do not expose/distribute the reference database.
        self.model = ADMETModel(drugbank_path=None, num_workers=0)
        self.root = Path(admet_ai.__file__).parent
        with (self.root / "resources/data/admet.csv").open() as handle:
            self.endpoints = {
                row["id"]: {key: row[key] for key in ("name", "category", "task_type", "units", "species")}
                for row in csv.DictReader(handle)
            }

    def metadata(self):
        return {
            "model_id": self.model_id,
            "model_version": importlib.metadata.version("admet-ai"),
            "upstream_revision": "c65bf0418e19c65d7228f9e40da5d0152aade756",
            "device": self.model.device,
            "endpoints": self.endpoints,
            "uncertainty": "not_calibrated; classification scores are not confidence in human safety",
            "preprocessing": "Pinned upstream RDKit/Chemprop; no extra salt stripping, neutralization or tautomer replacement.",
            "drugbank_percentiles": False,
            "limitations": ["Research screening only, not proof of human safety.", "Species and assay definitions differ across endpoints.", "No validated applicability-domain or calibrated uncertainty estimate is supplied."],
        }

    def predict(self, smiles, *, endpoints=None, method=None, seed=0):
        predictions = self.model.predict(smiles)
        if len(predictions) != len(smiles):
            raise RuntimeError("upstream unexpectedly dropped validated molecules")
        selected = endpoints or list(predictions.columns)
        output = []
        for _, row in predictions.iterrows():
            values = {}
            for name in selected:
                value = float(row[name])
                if not math.isfinite(value):
                    raise ValueError(f"non-finite prediction for endpoint {name}")
                values[name] = value
            output.append({"predictions": values})
        return output

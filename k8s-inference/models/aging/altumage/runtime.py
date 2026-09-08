"""Exact official AltumAge network and scaler, with explicit CPU/CUDA selection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import torch
from pyaging.models import AltumAgeNeuralNetwork

from aging.altumage.materialize import SOURCE_REVISION
from aging.contracts import AltumAgeRequest


class AltumAgeRuntime:
    model_id = "altumage"
    model_version = SOURCE_REVISION
    input_modality = "normalized-dna-methylation-beta-values"

    def __init__(self, root: Path, device: str = "cpu", threads: int = 1):
        if device not in {"cpu", "cuda"}:
            raise ValueError("AGING_DEVICE must be cpu or cuda")
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError(
                "CUDA was requested but is unavailable; no silent CPU fallback"
            )
        if threads < 1:
            raise ValueError("AGING_CPU_THREADS must be positive")
        torch.set_num_threads(threads)
        self.device = device
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("source_revision") != SOURCE_REVISION:
            raise ValueError("AltumAge artifact source revision mismatch")
        for name in ("cpgs.json", "preprocessing.npz", "weights.pt"):
            actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
            if actual != manifest["artifacts"][name]["sha256"]:
                raise ValueError(f"AltumAge derived artifact checksum mismatch: {name}")
        self.cpgs = json.loads((root / "cpgs.json").read_text(encoding="utf-8"))
        self.cpg_set = set(self.cpgs)
        with np.load(root / "preprocessing.npz", allow_pickle=False) as scaling:
            # Preserve upstream double-precision scaling before the model's
            # float32 input conversion, as in its original Keras example.
            self.center = scaling["center"].copy()
            self.scale = scaling["scale"].copy()
        self.model = AltumAgeNeuralNetwork()
        self.model.load_state_dict(
            torch.load(root / "weights.pt", map_location="cpu", weights_only=True)
        )
        self.model.to(device).eval()
        with torch.inference_mode():
            self.model(torch.zeros((1, 20318), device=device))
        if device == "cuda":
            torch.cuda.synchronize()
        self.weights_sha256 = manifest["artifacts"]["weights.pt"]["sha256"]

    def predict(self, request: AltumAgeRequest) -> list[dict]:
        if set(request.cpg_sites) != self.cpg_set:
            raise ValueError(
                "cpg_sites must contain exactly the official 20,318 AltumAge CpGs"
            )
        positions = {name: index for index, name in enumerate(request.cpg_sites)}
        order = [positions[name] for name in self.cpgs]
        values = np.asarray(
            [sample.beta_values for sample in request.samples], dtype=np.float64
        )[:, order]
        missing = np.isnan(values)
        if missing.any():
            if request.missing_values != "reference_median":
                raise ValueError(
                    "missing values require explicit reference-median imputation"
                )
            values = np.where(missing, self.center, values)
        scaled = (values - self.center) / self.scale
        tensor = torch.as_tensor(scaled, dtype=torch.float32, device=self.device)
        with torch.inference_mode():
            # Copying back waits for CUDA completion, so reported inference time
            # includes input transfer, compute and output transfer.
            ages = self.model(tensor).flatten().cpu().numpy()
        if not np.isfinite(ages).all():
            raise RuntimeError("AltumAge produced a non-finite prediction")
        return [
            {
                "sample_id": sample.sample_id,
                "predicted_chronological_age_years": float(age),
                "imputed_cpg_count": int(missing[index].sum()),
            }
            for index, (sample, age) in enumerate(
                zip(request.samples, ages, strict=True)
            )
        ]

    def metadata(self) -> dict:
        return {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "device": self.device,
            "input_modality": self.input_modality,
            "weights_sha256": self.weights_sha256,
            "feature_count": len(self.cpgs),
            "preprocessing": "official-robust-scaler",
            "gpu_snapshot": "not-qualified",
        }

"""Generate explicit synthetic (non-patient) requests for demos and benchmarks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def clinical_payload(count: int = 2) -> dict:
    return {
        "samples": [
            {
                "sample_id": f"synthetic-clinical-{index}",
                "age_years": 50 + index % 20,
                "albumin_g_l": 45,
                "creatinine_umol_l": 80,
                "glucose_mmol_l": 5,
                "c_reactive_protein_mg_dl": 0.1,
                "lymphocyte_percent": 30,
                "mean_cell_volume_fl": 90,
                "red_cell_distribution_width_percent": 13,
                "alkaline_phosphatase_u_l": 70,
                "white_blood_cell_count_10e3_per_ul": 6,
            }
            for index in range(count)
        ]
    }


def methylation_payload(root: Path, count: int = 2) -> dict:
    import numpy as np

    cpgs = json.loads((root / "cpgs.json").read_text(encoding="utf-8"))
    with np.load(root / "preprocessing.npz", allow_pickle=False) as scaling:
        center = scaling["center"]
    return {
        "cpg_sites": cpgs,
        "missing_values": "error",
        "samples": [
            {
                "sample_id": f"synthetic-dnam-{index}",
                "beta_values": np.clip(center + 0.005 * (index % 5), 0, 1).tolist(),
            }
            for index in range(count)
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=("phenoage", "altumage"))
    parser.add_argument("--artifact-root", type=Path, default=Path("/opt/altumage"))
    parser.add_argument("--samples", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.samples <= (128 if args.model == "altumage" else 512):
        parser.error("sample count is outside the model's batch contract")
    payload = (
        clinical_payload(args.samples)
        if args.model == "phenoage"
        else methylation_payload(args.artifact_root, args.samples)
    )
    print(json.dumps(payload, separators=(",", ":")))


if __name__ == "__main__":
    main()

"""Clinical PhenoAge from Levine et al. (2018), Supplement 1.

The rounded published coefficients and gamma are intentional. This is neither
DNAm PhenoAge nor the different conversion in pyaging 0.5.2. Algebra is evaluated
in log space so extreme inputs cannot saturate the intermediate mortality CDF.
"""

from __future__ import annotations

import math

from aging.contracts import ClinicalRequest, ClinicalSample

MODEL_VERSION = "levine-2018-supplement-rounded-v1"
GAMMA = 0.0076927
INTERCEPT = -19.9067
LOG_HAZARD_FACTOR = math.log(math.expm1(120 * GAMMA) / GAMMA)
COEFFICIENTS = {
    "albumin_g_l": -0.0336,
    "creatinine_umol_l": 0.0095,
    "glucose_mmol_l": 0.1953,
    "c_reactive_protein_mg_dl": 0.0954,
    "lymphocyte_percent": -0.0120,
    "mean_cell_volume_fl": 0.0268,
    "red_cell_distribution_width_percent": 0.3306,
    "alkaline_phosphatase_u_l": 0.0019,
    "white_blood_cell_count_10e3_per_ul": 0.0554,
    "age_years": 0.0804,
}


def clinical_age(sample: ClinicalSample) -> float:
    values = sample.model_dump(exclude={"sample_id"})
    values["c_reactive_protein_mg_dl"] = math.log(sample.c_reactive_protein_mg_dl)
    xb = INTERCEPT + math.fsum(
        COEFFICIENTS[name] * value for name, value in values.items()
    )
    # -log(1-M) is exactly exp(xb) * expm1(120*gamma) / gamma.
    age = 141.50225 + (math.log(0.00553) + xb + LOG_HAZARD_FACTOR) / 0.090165
    if not math.isfinite(age):
        raise ValueError("inputs produce a non-finite phenotypic age")
    return age


class ClinicalPhenoAgeRuntime:
    model_id = "phenoage"
    device = "cpu"
    model_version = MODEL_VERSION
    input_modality = "age-and-nine-blood-biomarkers"

    def predict(self, request: ClinicalRequest) -> list[dict]:
        return [
            {
                "sample_id": sample.sample_id,
                "phenotypic_age_years": clinical_age(sample),
            }
            for sample in request.samples
        ]

    def metadata(self) -> dict:
        return {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "device": self.device,
            "input_modality": self.input_modality,
            "weight_loading": "not-applicable-closed-form-formula",
            "gpu_snapshot": "not-applicable-cpu",
        }

"""Explicit, batch-friendly input contracts; the two assays are not interchangeable."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

NonNegative = Annotated[FiniteFloat, Field(ge=0)]
Positive = Annotated[FiniteFloat, Field(gt=0)]
Percentage = Annotated[FiniteFloat, Field(ge=0, le=100)]
BetaValue = Annotated[FiniteFloat, Field(ge=0, le=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClinicalSample(StrictModel):
    sample_id: str = Field(min_length=1, max_length=128)
    age_years: NonNegative
    albumin_g_l: Positive
    creatinine_umol_l: Positive
    glucose_mmol_l: Positive
    c_reactive_protein_mg_dl: Positive = Field(
        description="Raw CRP, not log(CRP); mg/dL, not mg/L."
    )
    lymphocyte_percent: Percentage
    mean_cell_volume_fl: Positive
    red_cell_distribution_width_percent: Percentage
    alkaline_phosphatase_u_l: NonNegative
    white_blood_cell_count_10e3_per_ul: Positive


class ClinicalRequest(StrictModel):
    samples: list[ClinicalSample] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def unique_samples(self) -> ClinicalRequest:
        if len({sample.sample_id for sample in self.samples}) != len(self.samples):
            raise ValueError("sample_id must be unique within a request")
        return self


class MethylationSample(StrictModel):
    sample_id: str = Field(min_length=1, max_length=128)
    beta_values: list[BetaValue | None] = Field(min_length=20318, max_length=20318)


class AltumAgeRequest(StrictModel):
    cpg_sites: list[str] = Field(min_length=20318, max_length=20318)
    samples: list[MethylationSample] = Field(min_length=1, max_length=128)
    missing_values: Literal["error", "reference_median"] = "error"

    @model_validator(mode="after")
    def validate_matrix(self) -> AltumAgeRequest:
        if len(set(self.cpg_sites)) != len(self.cpg_sites):
            raise ValueError("cpg_sites must contain unique names")
        if len({sample.sample_id for sample in self.samples}) != len(self.samples):
            raise ValueError("sample_id must be unique within a request")
        if self.missing_values == "error" and any(
            value is None for sample in self.samples for value in sample.beta_values
        ):
            raise ValueError(
                "null beta values require explicit missing_values=reference_median"
            )
        return self

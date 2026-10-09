"""Model-local contracts; authentication and durable operations belong to FS2."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_MOLECULES = 1000
MAX_FILE_CHARS = 16 * 1024 * 1024


class Molecule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=256, description="Caller-owned molecule identifier; preserved in results.")
    smiles: str = Field(min_length=1, max_length=16384, description="Molecular SMILES, including any intended stereochemistry.")


class ScreeningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    smiles: str | None = Field(default=None, min_length=1, max_length=16384, description="One molecule; alternatively supply molecules, csv or sdf.")
    molecules: list[Molecule] | None = Field(default=None, min_length=1, max_length=MAX_MOLECULES, description="Ordered molecule records. Duplicate structures are preserved, but IDs must be unique.")
    csv: str | None = Field(default=None, min_length=1, max_length=MAX_FILE_CHARS, description="CSV content with a SMILES column. The platform also accepts an immutable tenant artifact reference here.")
    sdf: str | None = Field(default=None, min_length=1, max_length=MAX_FILE_CHARS, description="SDF content. Each record is converted to isomeric SMILES; coordinates are not model inputs.")
    smiles_column: str = Field(default="smiles", min_length=1, max_length=128)
    id_column: str | None = Field(default=None, min_length=1, max_length=128)
    endpoints: list[str] | None = Field(default=None, min_length=1, max_length=64, description="Optional endpoint IDs from this App's metadata. Omit for the full panel; unknown IDs are rejected.")

    @model_validator(mode="after")
    def one_input(self):
        if sum(getattr(self, name) is not None for name in ("smiles", "molecules", "csv", "sdf")) != 1:
            raise ValueError("supply exactly one of smiles, molecules, csv, or sdf")
        if self.endpoints is not None and len(set(self.endpoints)) != len(self.endpoints):
            raise ValueError("endpoint IDs must be unique")
        return self


class ADMETRequest(ScreeningRequest):
    """ADMET-AI 2.0.1: learned endpoints and deterministic descriptors."""


class CToxRequest(ScreeningRequest):
    method: Literal["rf-ssl", "dl-sl"] = Field(default="rf-ssl", description="Upstream semi-supervised random forest or supervised neural networks with MC dropout.")
    seed: int = Field(default=0, ge=0, le=2147483647, description="Recorded seed for the neural-network MC-dropout draws; RF predictions are deterministic.")


REQUEST_TYPES = {"admet-ai": ADMETRequest, "ctoxpred2": CToxRequest}

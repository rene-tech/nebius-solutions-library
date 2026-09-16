from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Message(StrictModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=500_000)


class CompletionRequest(StrictModel):
    run_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    profile_id: str = Field(pattern=r"^profile-[0-9]{3}$")
    role: Literal["patient", "clinician"]
    model: str = Field(min_length=1, max_length=200)
    messages: list[Message] = Field(min_length=1, max_length=100)
    max_completion_tokens: int = Field(default=4096, ge=64, le=16384)
    temperature: float = Field(default=0.7, ge=0, le=2)


class JudgmentRequest(StrictModel):
    run_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    profile_id: str = Field(pattern=r"^profile-[0-9]{3}$")
    clinician_model: str = Field(min_length=1, max_length=200)
    interaction: list[Message] = Field(min_length=2, max_length=100)
    max_completion_tokens: int = Field(default=8192, ge=512, le=16384)

    @model_validator(mode="after")
    def no_system(self):
        if any(message.role == "system" for message in self.interaction):
            raise ValueError("judgment interaction cannot contain system messages")
        return self


class RunRegistration(StrictModel):
    profile_ids: list[str] = Field(min_length=1, max_length=20)
    patient_model: str = Field(min_length=1, max_length=200)
    clinician_models: list[str] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.profile_ids)) != len(self.profile_ids):
            raise ValueError("profile IDs must be unique")
        if len(set(self.clinician_models)) != len(self.clinician_models):
            raise ValueError("clinician models must be unique")
        return self


class GatewayError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 502, telemetry: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.telemetry = code, message, status, telemetry or {}


class Identity(StrictModel):
    tenant_id: str
    principal_id: str
    token_id: str
    scopes: set[str]
    models: set[str]
    max_concurrency: int = Field(ge=1, le=100)

    def require(self, model: str | None = None):
        if "inference.invoke" not in self.scopes:
            raise GatewayError("scope_denied", "inference.invoke is required", status=403)
        if model and not self.models.intersection({"*", "mindeval", model}):
            raise GatewayError("model_denied", "model is outside tenant token policy", status=403)

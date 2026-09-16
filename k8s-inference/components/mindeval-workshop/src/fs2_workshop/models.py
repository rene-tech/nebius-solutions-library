"""Public workshop contracts, independent of the underlying model family."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WORKSHOP_", extra="ignore")
    database_url: str
    credential_key_file: str = "/var/run/secrets/workshop/key"
    auth_url: str = "http://fs2-serve-control-plane.fs2-system.svc:8080/internal/ext-authz"
    gateway_url: str = "http://fs2-mindeval-gateway.fs2-system.svc:8080"
    speech_url: str = "ws://fs2-serve-control-plane.fs2-system.svc:8080/v1/audio/stream"
    platform_url: str = "http://fs2-serve-control-plane.fs2-system.svc:8080"
    public_origin: str = "https://inference.example.invalid"
    workers: int = Field(default=10, ge=1, le=50)
    lease_seconds: int = Field(default=90, ge=30, le=600)
    max_team_workers: int = Field(default=5, ge=1, le=5)
    request_timeout_seconds: int = Field(default=180, ge=10, le=600)
    mindguard_model: Literal["mindguard-4b", "mindguard-8b"] | None = "mindguard-4b"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateRuns(Contract):
    profile_ids: list[str] = Field(min_length=1, max_length=20)
    patient_model: str = Field(min_length=1, max_length=200)
    clinician_models: list[str] = Field(min_length=1, max_length=8)
    mode: Literal["canonical", "spoken"] = "canonical"
    max_turns: int = Field(default=10, ge=2, le=30)
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_completion_tokens: int = Field(default=4096, ge=64, le=8192)
    language: str = Field(default="en", pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")
    patient_voice: str = Field(default="Sofia", min_length=1, max_length=100)
    clinician_voice: str = Field(default="Jason", min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_inputs(self):
        if len(set(self.profile_ids)) != len(self.profile_ids):
            raise ValueError("profile_ids must be unique")
        if len(set(self.clinician_models)) != len(self.clinician_models):
            raise ValueError("clinician_models must be unique")
        return self


class Intervention(Contract):
    action: Literal["pause", "nudge", "takeover", "say", "resume", "abort"]
    role: Literal["patient", "clinician"] = "clinician"
    text: str | None = Field(default=None, min_length=1, max_length=16000)
    source: Literal["typed", "microphone"] = "typed"

    @model_validator(mode="after")
    def content_required(self):
        if self.action in {"say", "nudge"} and not self.text:
            raise ValueError("say and nudge require text")
        return self


TERMINAL = frozenset({"completed", "failed", "aborted"})


def messages_for(state: dict, role: str) -> list[dict]:
    """Map role-relative dialogue to MindEval's original system prompts."""
    prompt = state["profile"][f"{role}_system_prompt"]
    messages = [{"role": "system", "content": prompt}]
    for turn in state["transcript"]:
        messages.append({"role": "assistant" if turn["role"] == role else "user", "content": turn["content"]})
    for nudge in state.get("pending_nudges", []):
        if nudge["role"] == role:
            messages.append({"role": "system", "content": f"Workshop operator intervention: {nudge['text']}"})
    return messages


def judge_interaction(state: dict) -> list[dict]:
    return [
        {"role": "assistant" if t["role"] == "clinician" else "user", "content": t["content"]}
        for t in state["transcript"]
    ]

"""Bounded observational input shared by REST, MCP and durable execution."""

from typing import Literal

from pydantic import Field, model_validator

from .mindguard import MindGuardMessage, MindGuardModel, MindGuardModelId


class MindGuardAssessRequest(MindGuardModel):
    model: MindGuardModelId
    messages: list[MindGuardMessage] = Field(min_length=1, max_length=128)
    language: Literal["en"] = "en"

    @model_validator(mode="after")
    def bounded_transcript(self) -> "MindGuardAssessRequest":
        if not any(message.role == "user" for message in self.messages):
            raise ValueError("a transcript must contain a user turn")
        if sum(len(message.content) for message in self.messages) > 200_000:
            raise ValueError("transcript exceeds the observation request size limit")
        return self

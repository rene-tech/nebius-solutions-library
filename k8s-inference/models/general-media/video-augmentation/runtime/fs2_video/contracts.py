"""Closed, versioned recipe shared by single-video previews and batches."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import (
    COSMOS_REVISION,
    MAX_ITEMS,
    PAIDF_REVISION,
    REQUEST_SCHEMA,
    SERVING_REVISION,
)


def canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Recipe(ClosedModel):
    backend: Literal["cosmos3-nano-transfer"] = "cosmos3-nano-transfer"
    weather: Literal["overcast", "clear", "rain"]
    instruction: str = Field(
        default="Change only the weather; preserve the scene and recorded motion.",
        min_length=1,
        max_length=2000,
    )
    seed: int = Field(default=42, ge=0, le=2147483647)
    num_inference_steps: int = Field(default=35, ge=1, le=50)
    guidance_scale: float = Field(default=7.0, ge=0, le=20)
    control_weight: float = Field(default=1.0, gt=0, le=2)
    control_guidance: float = Field(default=1.5, ge=0, le=20)
    motion_threshold: float = Field(default=0.682, ge=0, le=1)
    max_retries: int = Field(default=1, ge=0, le=2)
    audio: Literal["preserve", "drop"] = "preserve"


class VideoItem(ClosedModel):
    id: str = Field(pattern=r"^video-[0-9]{4}$")
    source_name: str = Field(min_length=1, max_length=1000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def relative_name(self):
        if (
            self.source_name.startswith("/")
            or "\\" in self.source_name
            or any(part in {"", ".", ".."} for part in self.source_name.split("/"))
            or any(ord(c) < 32 for c in self.source_name)
        ):
            raise ValueError(
                "source_name must be a relative object name without traversal"
            )
        return self


class AugmentationRequest(ClosedModel):
    schema_version: Literal[REQUEST_SCHEMA] = Field(alias="schema")
    recipe: Recipe
    items: list[VideoItem] = Field(min_length=1, max_length=MAX_ITEMS)
    approved_recipe_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def unique_items(self):
        if len({x.id for x in self.items}) != len(self.items) or len(
            {x.source_name for x in self.items}
        ) != len(self.items):
            raise ValueError("video IDs and source names must be unique")
        return self


def recipe_identity(
    recipe: Recipe, *, vlm_model: str, llm_model: str, provider_url: str = ""
) -> dict:
    value = {
        "schema": "fs2-serve.nebius.ai/video-augmentation-recipe/v1",
        "parameters": recipe.model_dump(),
        "paidf_revision": PAIDF_REVISION,
        "model_revision": COSMOS_REVISION,
        "serving_revision": SERVING_REVISION,
        "adapter_source_sha256": hashlib.sha256(
            b"".join(
                path.name.encode() + b"\0" + path.read_bytes()
                for path in sorted(Path(__file__).parent.glob("*.py"))
            )
        ).hexdigest(),
        "vlm_model": vlm_model,
        "llm_model": llm_model,
        "provider_url": provider_url,
        "evaluation_frames": 5,
        "prompt_template_version": "weather-v1",
    }
    return {**value, "sha256": hashlib.sha256(canonical(value)).hexdigest()}

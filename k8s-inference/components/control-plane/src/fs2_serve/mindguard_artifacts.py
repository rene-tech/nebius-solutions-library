"""Private MindGuard v2 handoff validation; no public-base or production fallback."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from fs2_serve.mindguard import MindGuardModel


class MindGuardArtifactFile(MindGuardModel):
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(gt=0)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value or str(path) != value or value == ".":
            raise ValueError("artifact paths must be normalized relative paths")
        return value


class MindGuardV2ServingProfile(MindGuardModel):
    engine: Literal["vllm", "sglang"]
    image: str = Field(pattern=r"^[^\s@]+@sha256:[a-f0-9]{64}$")
    dtype: Literal["bfloat16"] = "bfloat16"
    context_length: Literal[32768] = 32768
    tensor_parallel_size: int = Field(ge=1, le=8)
    known_good_configuration: MindGuardArtifactFile


class MindGuardV2Bundle(MindGuardModel):
    schema_version: Literal[1] = 1
    artifact_id: str = Field(min_length=1, max_length=200)
    model_role: Literal["clinician"] = "clinician"
    publisher: Literal["swordhealth"] = "swordhealth"
    source_uri: str
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}(?:[a-f0-9]{24})?$")
    approval_record: MindGuardArtifactFile
    config: MindGuardArtifactFile
    tokenizer: MindGuardArtifactFile
    tokenizer_config: MindGuardArtifactFile
    chat_template: MindGuardArtifactFile
    weight_index: MindGuardArtifactFile | None = None
    weights: list[MindGuardArtifactFile] = Field(min_length=1)
    serving: MindGuardV2ServingProfile

    @field_validator("source_uri")
    @classmethod
    def private_source(cls, value: str) -> str:
        source = urlsplit(value)
        if source.scheme not in {"https", "s3"} or not source.hostname:
            raise ValueError("source_uri must identify the supplied HTTPS or S3 artifact")
        if source.username or source.password or source.query or source.fragment:
            raise ValueError("source_uri must not embed credentials, signed query parameters or fragments")
        if source.hostname.lower() == "huggingface.co" and source.path.lower().startswith("/qwen/"):
            raise ValueError("a public Qwen base checkpoint is not the private MindGuard v2 artifact")
        return value

    @model_validator(mode="after")
    def distinct_private_bundle(self) -> MindGuardV2Bundle:
        if self.artifact_id.lower() in {"mindguard-4b", "mindguard-8b"} or self.artifact_id.lower().startswith("qwen/"):
            raise ValueError("public checkpoints must not be relabeled as the private v2 clinician")
        paths = [item.path for item in self.files()]
        if len(paths) != len(set(paths)):
            raise ValueError("each artifact file must have a distinct path")
        if any(not item.path.endswith(".safetensors") for item in self.weights):
            raise ValueError("weights must be delivered as safetensors")
        if len(self.weights) > 1 and self.weight_index is None:
            raise ValueError("sharded weights require the supplied weight index")
        return self

    def files(self) -> list[MindGuardArtifactFile]:
        return [
            self.approval_record,
            self.config,
            self.tokenizer,
            self.tokenizer_config,
            self.chat_template,
            self.serving.known_good_configuration,
            *([self.weight_index] if self.weight_index else []),
            *self.weights,
        ]


class MindGuardV2EventBinding(MindGuardModel):
    """A separate event service identity, consumed through ordinary workshop admission."""

    event_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$")
    namespace: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$")
    model_id: str
    endpoint: str
    artifact_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    min_hot_replicas: int = Field(ge=1)
    role: Literal["clinician"] = "clinician"
    event_only: Literal[True] = True
    production_fallback: Literal[False] = False
    precision: Literal["bfloat16"] = "bfloat16"
    context_length: Literal[32768] = 32768

    @model_validator(mode="after")
    def isolated_endpoint(self) -> MindGuardV2EventBinding:
        expected_model = f"mindguard-v2-event-{self.event_id}"
        endpoint = urlsplit(self.endpoint)
        expected_host = f"{expected_model}.{self.namespace}.svc.cluster.local"
        if self.model_id != expected_model or endpoint.hostname != expected_host:
            raise ValueError("private v2 requires its exact separate event model and service identity")
        if (
            endpoint.scheme != "http"
            or endpoint.port != 8000
            or endpoint.path != "/v1"
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("event endpoint must be its in-cluster HTTP service on port 8000 at /v1")
        return self


def validate_private_bundle(bundle: MindGuardV2Bundle, artifact_root: Path) -> dict[str, object]:
    """Stream hashes of every delivered file; return provenance only, never private contents.

    This proves correspondence to the supplied approval manifest, not clinical fitness.
    Deterministic prompts, 32k context, MindEval and GPU qualification remain separate.
    """
    root = artifact_root.resolve(strict=True)
    for item in bundle.files():
        file_path = root / item.path
        resolved = file_path.resolve(strict=True)
        if not resolved.is_relative_to(root) or file_path.is_symlink() or not resolved.is_file():
            raise ValueError(f"artifact file is not a regular file inside the bundle: {item.path}")
        if resolved.stat().st_size != item.size_bytes:
            raise ValueError(f"artifact size mismatch: {item.path}")
        with resolved.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != item.sha256:
            raise ValueError(f"artifact checksum mismatch: {item.path}")
    config = json.loads((root / bundle.config.path).read_text())
    max_positions = config.get("max_position_embeddings", config.get("text_config", {}).get("max_position_embeddings"))
    if not isinstance(max_positions, int) or max_positions < 32768:
        raise ValueError("delivered config does not support the required 32768-token context")
    if bundle.weight_index:
        index = json.loads((root / bundle.weight_index.path).read_text())
        index_parent = PurePosixPath(bundle.weight_index.path).parent
        indexed = {str(index_parent / filename) for filename in index.get("weight_map", {}).values()}
        if indexed != {weight.path for weight in bundle.weights}:
            raise ValueError("weight index and supplied shard manifest disagree")
    manifest_digest = hashlib.sha256(
        json.dumps(bundle.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "status": "artifact_integrity_verified",
        "artifact_id": bundle.artifact_id,
        "source_revision": bundle.source_revision,
        "manifest_sha256": manifest_digest,
        "file_count": len(bundle.files()),
        "runtime_image": bundle.serving.image,
        "gpu_qualified": False,
        "mindeval_accepted": False,
    }

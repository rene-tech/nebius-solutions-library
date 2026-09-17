"""Strict, provider-neutral request contract for LeRobot augmentation."""

from __future__ import annotations

import json
import re
import string
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal, cast

REQUEST_SCHEMA = "fs2-serve.nebius.ai/cosmos3-lerobot-augmentation-request/v1"
ALLOWED_TEMPLATE_FIELDS = frozenset({"task", "episode_index", "camera", "variation", "instruction"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_BINDING = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CAMERA = re.compile(r"^observation\.images\.[A-Za-z0-9._-]+$")
class ContractError(ValueError):
    """The caller-authored augmentation request is invalid."""


def _object(value: object, *, required: set[str], optional: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ContractError(f"{label} must be an object")
    item = cast(Mapping[str, Any], value)
    missing = required - set(item)
    extra = set(item) - required - optional
    if missing:
        raise ContractError(f"{label} is missing {', '.join(sorted(missing))}")
    if extra:
        raise ContractError(f"{label} contains unsupported fields: {', '.join(sorted(extra))}")
    return item


def _text(value: object, *, label: str, maximum: int, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ContractError(f"{label} must be a non-empty string of at most {maximum} characters")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ContractError(f"{label} has an invalid format")
    return value


def _integer(value: object, *, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ContractError(f"{label} must be an integer in [{minimum}, {maximum}]")
    return value


def _number(value: object, *, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not minimum <= float(value) <= maximum:
        raise ContractError(f"{label} must be a number in [{minimum}, {maximum}]")
    return float(value)


@dataclass(frozen=True, slots=True)
class HuggingFaceSource:
    kind: Literal["huggingface"]
    repo_id: str
    revision: str
    credential_binding: str | None


@dataclass(frozen=True, slots=True)
class ObjectStoreSource:
    kind: Literal["object-store"]
    storage_binding: str
    object_prefix: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class UploadedBundleSource:
    kind: Literal["uploaded-bundle"]
    artifact_id: str
    sha256: str
    size_bytes: int
    media_type: Literal["application/x-tar"]
    compression: Literal["zstd"]


DatasetSource = HuggingFaceSource | ObjectStoreSource | UploadedBundleSource


def parse_source(value: object) -> DatasetSource:
    if not isinstance(value, Mapping):
        raise ContractError("source must be an object")
    kind = value.get("kind")
    if kind in {"local", "path", "file"} or any(field in value for field in ("path", "local_path", "uri", "url")):
        raise ContractError(
            "client-local paths and raw URLs are not accessible; upload a zstd tar bundle or use an authorized "
            "Hugging Face/object-store reference"
        )
    if kind == "huggingface":
        item = _object(
            value,
            required={"kind", "repo_id", "revision"},
            optional={"credential_binding"},
            label="Hugging Face source",
        )
        binding = item.get("credential_binding")
        return HuggingFaceSource(
            kind="huggingface",
            repo_id=_text(item["repo_id"], label="repo_id", maximum=193, pattern=_REPO_ID),
            revision=_text(item["revision"], label="revision", maximum=40, pattern=_REVISION),
            credential_binding=(
                _text(binding, label="credential_binding", maximum=63, pattern=_BINDING)
                if binding is not None
                else None
            ),
        )
    if kind == "object-store":
        item = _object(
            value,
            required={"kind", "storage_binding", "object_prefix", "manifest_sha256"},
            optional=set(),
            label="object-store source",
        )
        prefix = _text(item["object_prefix"], label="object_prefix", maximum=512)
        if prefix.startswith("/") or "//" in prefix or any(part in {"", ".", ".."} for part in prefix.split("/")):
            raise ContractError("object_prefix must be a normalized relative object prefix")
        return ObjectStoreSource(
            kind="object-store",
            storage_binding=_text(item["storage_binding"], label="storage_binding", maximum=63, pattern=_BINDING),
            object_prefix=prefix,
            manifest_sha256=_text(item["manifest_sha256"], label="manifest_sha256", maximum=64, pattern=_SHA256),
        )
    if kind == "uploaded-bundle":
        item = _object(
            value,
            required={"kind", "artifact_id", "sha256", "size_bytes", "media_type", "compression"},
            optional=set(),
            label="uploaded bundle source",
        )
        if item["media_type"] != "application/x-tar" or item["compression"] != "zstd":
            raise ContractError("uploaded bundle must be application/x-tar compressed with zstd")
        return UploadedBundleSource(
            kind="uploaded-bundle",
            artifact_id=_text(item["artifact_id"], label="artifact_id", maximum=128, pattern=_ARTIFACT_ID),
            sha256=_text(item["sha256"], label="sha256", maximum=64, pattern=_SHA256),
            size_bytes=_integer(item["size_bytes"], label="size_bytes", minimum=1, maximum=128 * 1024**3),
            media_type="application/x-tar",
            compression="zstd",
        )
    raise ContractError("source.kind must be huggingface, object-store, or uploaded-bundle")


@dataclass(frozen=True, slots=True)
class Selection:
    episodes: Literal["all"] | tuple[int, ...]
    cameras: Literal["all"] | tuple[str, ...]

    def resolve_episodes(self, total: int) -> tuple[int, ...]:
        selected = tuple(range(total)) if self.episodes == "all" else self.episodes
        if any(index >= total for index in selected):
            raise ContractError(f"episode selection exceeds dataset range 0..{max(total - 1, 0)}")
        return selected

    def resolve_cameras(self, available: tuple[str, ...]) -> tuple[str, ...]:
        selected = available if self.cameras == "all" else self.cameras
        missing = set(selected) - set(available)
        if missing:
            raise ContractError(f"camera selection is not present in the dataset: {', '.join(sorted(missing))}")
        return selected


def _parse_selection(value: object) -> Selection:
    item = _object(value, required={"episodes", "cameras"}, optional=set(), label="selection")
    raw_episodes = item["episodes"]
    if raw_episodes == "all":
        episodes: Literal["all"] | tuple[int, ...] = "all"
    elif isinstance(raw_episodes, list) and 1 <= len(raw_episodes) <= 256:
        episodes = tuple(_integer(index, label="episode index", minimum=0, maximum=2**31 - 1) for index in raw_episodes)
        if len(set(episodes)) != len(episodes):
            raise ContractError("episode indexes must be unique")
    else:
        raise ContractError("selection.episodes must be 'all' or a list of 1..256 unique indexes")
    raw_cameras = item["cameras"]
    if raw_cameras == "all":
        cameras: Literal["all"] | tuple[str, ...] = "all"
    elif isinstance(raw_cameras, list) and 1 <= len(raw_cameras) <= 8:
        cameras = tuple(_text(camera, label="camera", maximum=256, pattern=_CAMERA) for camera in raw_cameras)
        if len(set(cameras)) != len(cameras):
            raise ContractError("camera names must be unique")
    else:
        raise ContractError("selection.cameras must be 'all' or a list of 1..8 unique video features")
    return Selection(episodes=episodes, cameras=cameras)


@dataclass(frozen=True, slots=True)
class Dimension:
    name: Literal["lighting", "weather", "background", "viewpoint", "object-appearance", "custom"]
    instruction: str
    strength: float


@dataclass(frozen=True, slots=True)
class Conditioning:
    keep: Literal["first", "last"]
    frame_indexes: tuple[int, ...]
    controls: tuple[Literal["edge", "blur"], ...]


@dataclass(frozen=True, slots=True)
class Augmentation:
    mode: Literal["video-to-video", "transfer"]
    prompt_template: str
    negative_prompt: str
    dimensions: tuple[Dimension, ...]
    conditioning: Conditioning
    num_inference_steps: int
    guidance_scale: float

    def prompt(self, *, task: str, episode_index: int, camera: str) -> str:
        descriptions = "; ".join(
            f"{item.name} (strength={item.strength:.2f}): {item.instruction}" for item in self.dimensions
        )
        return self.prompt_template.format(
            task=task,
            episode_index=episode_index,
            camera=camera,
            variation=descriptions,
            instruction=descriptions,
        )


def _parse_template(value: object, *, label: str, maximum: int) -> str:
    template = _text(value, label=label, maximum=maximum)
    try:
        fields = {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}
    except ValueError as error:
        raise ContractError(f"{label} has invalid format syntax") from error
    invalid = fields - ALLOWED_TEMPLATE_FIELDS
    if invalid:
        raise ContractError(f"{label} has unsupported placeholders: {', '.join(sorted(invalid))}")
    if any("." in field or "[" in field for field in fields):
        raise ContractError(f"{label} placeholders cannot traverse attributes or indexes")
    return template


def _parse_augmentation(value: object) -> Augmentation:
    item = _object(
        value,
        required={"mode", "prompt_template", "dimensions", "conditioning"},
        optional={"negative_prompt", "num_inference_steps", "guidance_scale"},
        label="augmentation",
    )
    mode = item["mode"]
    if mode not in {"video-to-video", "transfer"}:
        raise ContractError(
            "augmentation.mode must be video-to-video or transfer; the pinned "
            "Cosmos forward-dynamics runtime is not qualified"
        )
    raw_dimensions = item["dimensions"]
    if not isinstance(raw_dimensions, list) or not 1 <= len(raw_dimensions) <= 8:
        raise ContractError("augmentation.dimensions must contain 1..8 dimensions")
    dimensions: list[Dimension] = []
    for raw in raw_dimensions:
        dimension = _object(raw, required={"name", "instruction"}, optional={"strength"}, label="dimension")
        name = dimension["name"]
        if name not in {"lighting", "weather", "background", "viewpoint", "object-appearance", "custom"}:
            raise ContractError("augmentation dimension name is unsupported")
        dimensions.append(
            Dimension(
                name=name,
                instruction=_text(dimension["instruction"], label="dimension instruction", maximum=1024),
                strength=_number(dimension.get("strength", 1.0), label="dimension strength", minimum=0, maximum=1),
            )
        )
    if len({dimension.name for dimension in dimensions}) != len(dimensions):
        raise ContractError("augmentation dimension names must be unique")
    raw_conditioning = _object(
        item["conditioning"], required={"keep", "frame_indexes", "controls"}, optional=set(), label="conditioning"
    )
    if raw_conditioning["keep"] not in {"first", "last"}:
        raise ContractError("conditioning.keep must be first or last")
    raw_indexes = raw_conditioning["frame_indexes"]
    if not isinstance(raw_indexes, list) or not 1 <= len(raw_indexes) <= 16:
        raise ContractError("conditioning.frame_indexes must contain 1..16 indexes")
    indexes = tuple(_integer(index, label="condition frame index", minimum=0, maximum=100) for index in raw_indexes)
    if len(set(indexes)) != len(indexes):
        raise ContractError("conditioning frame indexes must be unique")
    raw_controls = raw_conditioning["controls"]
    if not isinstance(raw_controls, list) or len(raw_controls) > 2:
        raise ContractError("conditioning.controls must contain at most two controls")
    if any(control not in {"edge", "blur"} for control in raw_controls):
        raise ContractError("conditioning control is unsupported")
    controls = tuple(cast(Literal["edge", "blur"], control) for control in raw_controls)
    if len(set(controls)) != len(controls):
        raise ContractError("conditioning controls must be unique")
    if mode == "transfer" and not controls:
        raise ContractError("transfer augmentation requires at least one conditioning control")
    if mode != "transfer" and controls:
        raise ContractError("conditioning controls are accepted only for transfer augmentation")
    return Augmentation(
        mode=cast(Literal["video-to-video", "transfer"], mode),
        prompt_template=_parse_template(item["prompt_template"], label="prompt_template", maximum=4096),
        negative_prompt=(
            _text(item["negative_prompt"], label="negative_prompt", maximum=4096) if item.get("negative_prompt") else ""
        ),
        dimensions=tuple(dimensions),
        conditioning=Conditioning(
            keep=cast(Literal["first", "last"], raw_conditioning["keep"]),
            frame_indexes=indexes,
            controls=controls,
        ),
        num_inference_steps=_integer(
            item.get("num_inference_steps", 35), label="num_inference_steps", minimum=1, maximum=50
        ),
        guidance_scale=_number(item.get("guidance_scale", 6), label="guidance_scale", minimum=0, maximum=20),
    )


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    mode: Literal["preserve"]


def _parse_actions(value: object) -> ActionPolicy:
    item = _object(value, required={"mode"}, optional=set(), label="actions")
    if item["mode"] != "preserve":
        raise ContractError(
            "actions.mode must be preserve; Cosmos inverse-dynamics is not qualified "
            "and generated video cannot be published with unverified replacement actions"
        )
    return ActionPolicy(mode="preserve")


@dataclass(frozen=True, slots=True)
class FailurePolicy:
    mode: Literal["fail-fast", "continue"]
    max_attempts: int


@dataclass(frozen=True, slots=True)
class AugmentationRequest:
    schema: Literal["fs2-serve.nebius.ai/cosmos3-lerobot-augmentation-request/v1"]
    source: DatasetSource
    selection: Selection
    variant_seeds: tuple[int, ...]
    augmentation: Augmentation
    actions: ActionPolicy
    failure_policy: FailurePolicy

    @classmethod
    def parse(cls, value: object) -> AugmentationRequest:
        item = _object(
            value,
            required={"schema", "source", "selection", "variants", "augmentation", "actions", "failure_policy"},
            optional=set(),
            label="augmentation request",
        )
        if item["schema"] != REQUEST_SCHEMA:
            raise ContractError("augmentation request schema is unsupported")
        variants = _object(item["variants"], required={"count", "seeds"}, optional=set(), label="variants")
        count = _integer(variants["count"], label="variant count", minimum=1, maximum=8)
        raw_seeds = variants["seeds"]
        if not isinstance(raw_seeds, list):
            raise ContractError("variant seeds must be an array")
        seeds = tuple(_integer(seed, label="variant seed", minimum=0, maximum=2**32 - 1) for seed in raw_seeds)
        if len(seeds) != count:
            raise ContractError("variant seeds must contain exactly variant count values")
        if len(set(seeds)) != len(seeds):
            raise ContractError("variant seeds must be unique")
        failure = _object(
            item["failure_policy"], required={"mode", "max_attempts"}, optional=set(), label="failure_policy"
        )
        if failure["mode"] not in {"fail-fast", "continue"}:
            raise ContractError("failure_policy.mode must be fail-fast or continue")
        augmentation = _parse_augmentation(item["augmentation"])
        actions = _parse_actions(item["actions"])
        return cls(
            schema="fs2-serve.nebius.ai/cosmos3-lerobot-augmentation-request/v1",
            source=parse_source(item["source"]),
            selection=_parse_selection(item["selection"]),
            variant_seeds=seeds,
            augmentation=augmentation,
            actions=actions,
            failure_policy=FailurePolicy(
                mode=cast(Literal["fail-fast", "continue"], failure["mode"]),
                max_attempts=_integer(failure["max_attempts"], label="max_attempts", minimum=1, maximum=3),
            ),
        )

    def canonical_json(self) -> str:
        """Return the stable, non-secret request identity used in provenance."""

        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False)

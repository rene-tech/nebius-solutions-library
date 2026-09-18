"""LeRobot v3 localization, integrity validation, rewrite, and packaging."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import tarfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from .contracts import MAX_BUNDLE_BYTES, Selection

LEROBOT_VERSION = "0.6.1"
BUNDLE_MANIFEST_SCHEMA = "fs2-serve.nebius.ai/lerobot-bundle-manifest/v1"
PROVENANCE_SCHEMA = "fs2-serve.nebius.ai/lerobot-augmentation-provenance/v1"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_FILES = 100_000
MAX_EXPANDED_BYTES = 8 * 1024**3
MIN_COSMOS_FRAMES = 16
MAX_COSMOS_FRAMES = 400
MAX_PIXELS = 1280 * 720
AUTO_FEATURES = frozenset({"timestamp", "frame_index", "episode_index", "index", "task_index"})


class DatasetError(RuntimeError):
    """The localized dataset is corrupt, unsupported, or misaligned."""


@dataclass(frozen=True, slots=True)
class FileIdentity:
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Episode:
    index: int
    start: int
    stop: int
    task: str

    @property
    def frames(self) -> int:
        return self.stop - self.start


@dataclass(frozen=True, slots=True)
class DatasetInspection:
    root: Path
    repo_id: str
    fps: int
    frames: int
    episodes: tuple[Episode, ...]
    cameras: tuple[str, ...]
    selected_episodes: tuple[int, ...]
    selected_cameras: tuple[str, ...]
    action_shape: tuple[int, ...]
    state_shapes: Mapping[str, tuple[int, ...]]
    decoded_video_frames: int
    tree_sha256: str
    dataset: Any


def _read_json(path: Path, maximum: int = MAX_MANIFEST_BYTES) -> Mapping[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > maximum:
            raise DatasetError(f"{path.name} exceeds its size bound")
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise DatasetError(f"{path.name} is unavailable or invalid JSON") from error
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise DatasetError(f"{path.name} must contain a JSON object")
    return cast(Mapping[str, Any], value)


def _safe_relative(value: object, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise DatasetError(f"{label} has an invalid path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DatasetError(f"{label} must be a normalized relative path")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def write_bundle_manifest(root: Path, *, output: Path | None = None) -> Mapping[str, Any]:
    """Write a complete non-circular inventory (the manifest excludes itself)."""

    destination = output or root / "fs2-bundle-manifest.json"
    identities: list[FileIdentity] = []
    for path in sorted(root.rglob("*")):
        if path == destination:
            continue
        if path.is_symlink():
            raise DatasetError("dataset bundles cannot contain symbolic links")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            identities.append(FileIdentity(relative, path.stat().st_size, sha256_file(path)))
    if not identities or len(identities) > MAX_FILES:
        raise DatasetError("dataset bundle file count is outside the supported bound")
    payload: Mapping[str, Any] = {
        "schema": BUNDLE_MANIFEST_SCHEMA,
        "files": [
            identity.__dict__
            if hasattr(identity, "__dict__")
            else {
                "path": identity.path,
                "size_bytes": identity.size_bytes,
                "sha256": identity.sha256,
            }
            for identity in identities
        ],
    }
    destination.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return payload


def verify_bundle_manifest(root: Path, *, expected_manifest_sha256: str | None = None) -> tuple[FileIdentity, ...]:
    manifest_path = root / "fs2-bundle-manifest.json"
    if expected_manifest_sha256 is not None and sha256_file(manifest_path) != expected_manifest_sha256:
        raise DatasetError("dataset bundle manifest digest does not match the admitted source")
    manifest = _read_json(manifest_path)
    if set(manifest) != {"schema", "files"} or manifest["schema"] != BUNDLE_MANIFEST_SCHEMA:
        raise DatasetError("dataset bundle manifest schema is unsupported")
    raw_files = manifest["files"]
    if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= MAX_FILES:
        raise DatasetError("dataset bundle manifest has an invalid file list")
    identities: list[FileIdentity] = []
    seen: set[str] = set()
    total = 0
    for raw in raw_files:
        if not isinstance(raw, Mapping) or set(raw) != {"path", "size_bytes", "sha256"}:
            raise DatasetError("dataset bundle manifest contains an invalid file entry")
        relative = _safe_relative(raw["path"], label="bundle file").as_posix()
        if relative == "fs2-bundle-manifest.json" or relative in seen:
            raise DatasetError("dataset bundle manifest contains a duplicate or self entry")
        size = raw["size_bytes"]
        digest = raw["sha256"]
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_EXPANDED_BYTES:
            raise DatasetError("dataset bundle manifest contains an invalid size")
        if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise DatasetError("dataset bundle manifest contains an invalid sha256")
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise DatasetError(f"dataset bundle file is missing or unsafe: {relative}")
        if path.stat().st_size != size or sha256_file(path) != digest:
            raise DatasetError(f"dataset bundle file identity mismatch: {relative}")
        seen.add(relative)
        total += size
        if total > MAX_EXPANDED_BYTES:
            raise DatasetError("dataset bundle exceeds the expanded size bound")
        identities.append(FileIdentity(relative, size, digest))
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() and path != manifest_path}
    if actual != seen:
        raise DatasetError("dataset bundle contains files outside the admitted inventory")
    return tuple(identities)


def tree_sha256(identities: Iterable[FileIdentity]) -> str:
    canonical = [
        {"path": item.path, "size_bytes": item.size_bytes, "sha256": item.sha256}
        for item in sorted(identities, key=lambda item: item.path)
    ]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _check_container_magic(path: Path, suffix: str) -> None:
    try:
        with path.open("rb") as source:
            head = source.read(32)
            if suffix == ".parquet":
                source.seek(-4, os.SEEK_END)
                tail = source.read(4)
            else:
                tail = b""
    except (OSError, ValueError) as error:
        raise DatasetError(f"cannot inspect {path.name}") from error
    if suffix == ".parquet" and (head[:4] != b"PAR1" or tail != b"PAR1"):
        raise DatasetError(f"invalid Parquet container: {path.name}")
    if suffix == ".mp4" and (len(head) < 12 or b"ftyp" not in head[:32]):
        raise DatasetError(f"invalid MP4 container: {path.name}")


def preflight_tree(root: Path, *, expected_manifest_sha256: str | None = None) -> tuple[Mapping[str, Any], str]:
    """Reject unsafe/corrupt structure before importing LeRobot or using a GPU."""

    if not root.is_dir() or root.is_symlink():
        raise DatasetError("localized dataset root is unavailable or unsafe")
    identities = verify_bundle_manifest(root, expected_manifest_sha256=expected_manifest_sha256)
    info = _read_json(root / "meta" / "info.json", maximum=4 * 1024 * 1024)
    version = info.get("codebase_version")
    if not isinstance(version, str) or not version.startswith("v3."):
        raise DatasetError("only LeRobotDataset v3 is supported; convert v2.1 before submission")
    fps = info.get("fps")
    total_frames = info.get("total_frames")
    total_episodes = info.get("total_episodes")
    total_tasks = info.get("total_tasks")
    features = info.get("features")
    if (
        isinstance(fps, bool)
        or not isinstance(fps, int)
        or not 1 <= fps <= 240
        or isinstance(total_frames, bool)
        or not isinstance(total_frames, int)
        or total_frames < 1
        or isinstance(total_episodes, bool)
        or not isinstance(total_episodes, int)
        or total_episodes < 1
        or isinstance(total_tasks, bool)
        or not isinstance(total_tasks, int)
        or total_tasks < 1
        or not isinstance(features, Mapping)
    ):
        raise DatasetError("LeRobot info.json has invalid fps, totals, or features")
    required = {"action", "timestamp", "frame_index", "episode_index", "index", "task_index"}
    if not required.issubset(features):
        raise DatasetError("LeRobot dataset is missing action or canonical index/timestamp features")
    video_keys = [
        key for key, value in features.items() if isinstance(value, Mapping) and value.get("dtype") == "video"
    ]
    if not video_keys or any(not str(key).startswith("observation.images.") for key in video_keys):
        raise DatasetError("LeRobot dataset must contain at least one RGB observation.images.* video feature")
    parquet_paths = (
        sorted(root.glob("data/**/*.parquet"))
        + sorted(root.glob("meta/episodes/**/*.parquet"))
        + sorted(root.glob("meta/*.parquet"))
    )
    video_paths = sorted(root.glob("videos/**/*.mp4"))
    if not parquet_paths or not video_paths:
        raise DatasetError("LeRobot dataset has no Parquet data/episode shards or MP4 video shards")
    for path in (*parquet_paths, *video_paths):
        if path.is_symlink():
            raise DatasetError("LeRobot dataset cannot contain symbolic links")
        _check_container_magic(path, path.suffix)
    return info, tree_sha256(identities)


def _scalar(value: object, label: str) -> int | float:
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DatasetError(f"{label} is not numeric")
    return value


def _shape(value: object) -> tuple[int, ...]:
    shape = getattr(value, "shape", None)
    if shape is None:
        if isinstance(value, list):
            return (len(value),)
        return ()
    return tuple(int(part) for part in shape)


def _canonical_timestamp(value: object, *, offset: int, fps: int) -> bool:
    import numpy as np

    raw = np.asarray(value)
    if raw.dtype not in (np.dtype("float32"), np.dtype("float64")):
        return False
    return bool(np.array_equal(raw, np.asarray(offset / fps, dtype=raw.dtype).reshape(raw.shape)))


def source_row(dataset: Any, index: int, *, rows: Any = None) -> Mapping[str, Any]:
    """Read stored numeric values without HF's default float64→float32 formatter."""
    import numpy as np

    row = dict((rows if rows is not None else dataset.hf_dataset.with_format(None))[index])
    for key, feature in dataset.features.items():
        if feature["dtype"] not in {"video", "image", "string"}:
            row[key] = np.asarray(row[key], dtype=feature["dtype"])
    return row


def open_and_validate(
    root: Path,
    *,
    repo_id: str,
    selection: Selection,
    expected_manifest_sha256: str | None = None,
    generation_bounds: bool = True,
    checkpoint: Callable[[], None] | None = None,
) -> DatasetInspection:
    """Fully load rows/videos and prove frame, timestamp, action, and episode alignment."""

    info, digest = preflight_tree(root, expected_manifest_sha256=expected_manifest_sha256)
    try:
        installed = importlib.metadata.version("lerobot")
    except importlib.metadata.PackageNotFoundError as error:
        raise DatasetError(f"lerobot=={LEROBOT_VERSION} is required for semantic dataset validation") from error
    if installed != LEROBOT_VERSION:
        raise DatasetError(f"lerobot=={LEROBOT_VERSION} is required, found {installed}")
    try:
        import numpy as np
        from lerobot.datasets.lerobot_dataset import LeRobotDataset

        dataset = LeRobotDataset(repo_id, root=root, video_backend="pyav", return_uint8=True)
    except Exception as error:
        raise DatasetError("LeRobotDataset failed to open the localized v3 dataset") from error
    fps = cast(int, info["fps"])
    total_frames = cast(int, info["total_frames"])
    total_episodes = cast(int, info["total_episodes"])
    total_tasks = cast(int, info["total_tasks"])
    if len(dataset) != total_frames or dataset.num_episodes != total_episodes:
        raise DatasetError("LeRobot reader totals differ from info.json")
    if fps > 30:
        raise DatasetError("selected LeRobot dataset FPS exceeds the qualified Cosmos bound of 30")
    features = cast(Mapping[str, Mapping[str, Any]], info["features"])
    cameras = tuple(sorted(key for key, feature in features.items() if feature.get("dtype") == "video"))
    selected_episodes = selection.resolve_episodes(total_episodes)
    selected_cameras = selection.resolve_cameras(cameras)
    if generation_bounds and (len(selected_episodes) > 256 or len(selected_cameras) > 8):
        raise DatasetError("generation selection supports at most 256 episodes and 8 cameras, including 'all'")
    for camera in cameras:
        shape = features[camera].get("shape")
        if (
            not isinstance(shape, list)
            or len(shape) != 3
            or any(isinstance(part, bool) or not isinstance(part, int) or part < 1 for part in shape)
        ):
            raise DatasetError(f"camera {camera} has an invalid RGB video shape")
        channels, height, width = shape
        if channels != 3:
            raise DatasetError(f"camera {camera} is not RGB")
        if (
            generation_bounds
            and camera in selected_cameras
            and (
                not 256 <= width <= 1280
                or not 256 <= height <= 720
                or width * height > MAX_PIXELS
                or width % 16
                or height % 16
            )
        ):
            raise DatasetError(f"camera {camera} shape is outside the qualified Cosmos RGB/multiple-of-16/720p bound")
    action_shape = tuple(int(part) for part in cast(list[int], features["action"].get("shape", [])))
    if not action_shape or math.prod(action_shape) > 256:
        raise DatasetError("action feature shape is empty or exceeds the supported bound")
    states = {
        key: tuple(int(part) for part in cast(list[int], feature.get("shape", [])))
        for key, feature in features.items()
        if key.startswith("observation.state")
    }
    episode_rows = dataset.meta.episodes
    if episode_rows is None or len(episode_rows) != total_episodes:
        raise DatasetError("episode metadata is missing or miscounted")
    tasks = dataset.meta.tasks
    if tasks is None or len(tasks) != total_tasks:
        raise DatasetError("task metadata is missing or miscounted")
    episodes: list[Episode] = []
    expected_start = 0
    task_order: dict[str, int] = {}
    raw_rows = dataset.hf_dataset.with_format(None)
    decoded = 0
    for index in range(total_episodes):
        metadata = episode_rows[index]
        start = int(metadata["dataset_from_index"])
        stop = int(metadata["dataset_to_index"])
        length = int(metadata.get("length", stop - start))
        if start != expected_start or stop <= start or stop - start != length or stop > total_frames:
            raise DatasetError(f"episode {index} boundaries are not contiguous and aligned")
        first = source_row(dataset, start, rows=raw_rows)
        first_task_value = _scalar(first["task_index"], "task_index")
        first_task_index = int(first_task_value)
        if first_task_value != first_task_index or not 0 <= first_task_index < total_tasks:
            raise DatasetError(f"episode {index} has an invalid task index")
        first_decoded = dataset[start]
        task = first_decoded.get("task", "")
        if not isinstance(task, str) or not task:
            raise DatasetError(f"episode {index} has no valid task")
        expected_task_index = task_order.setdefault(task, len(task_order))
        if first_task_index != expected_task_index:
            raise DatasetError(
                f"episode {index} has unsupported noncanonical task_index ordering; "
                "tasks must use consecutive first-occurrence indexes so the writer cannot renumber them"
            )
        episodes.append(Episode(index=index, start=start, stop=stop, task=task))
        selected = index in selected_episodes
        if generation_bounds and selected and not MIN_COSMOS_FRAMES <= length <= MAX_COSMOS_FRAMES:
            raise DatasetError(
                f"episode {index} has {length} frames; the qualified Cosmos bound is "
                f"{MIN_COSMOS_FRAMES}..{MAX_COSMOS_FRAMES}"
            )
        for offset, row_index in enumerate(range(start, stop)):
            if checkpoint is not None:
                checkpoint()
            raw = source_row(dataset, row_index, rows=raw_rows)
            if int(_scalar(raw["episode_index"], "episode_index")) != index:
                raise DatasetError(f"episode {index} data rows contain another episode index")
            frame_index = int(_scalar(raw["frame_index"], "frame_index"))
            global_index = int(_scalar(raw["index"], "index"))
            task_value = _scalar(raw["task_index"], "task_index")
            task_index = int(task_value)
            timestamp = float(_scalar(raw["timestamp"], "timestamp"))
            if not _canonical_timestamp(raw["timestamp"], offset=offset, fps=fps):
                raise DatasetError(
                    f"episode {index} has an unsupported noncanonical timestamp; "
                    "fixed-rate datasets require exact source-dtype frame_index/fps values (float32 or float64)"
                )
            if (
                frame_index != offset
                or global_index != row_index
                or task_value != task_index
                or task_index != first_task_index
                or not math.isfinite(timestamp)
                or abs(timestamp - offset / fps) > 1e-4
            ):
                raise DatasetError(f"episode {index} frame/timestamp alignment is invalid")
            if _shape(raw["action"]) != action_shape:
                raise DatasetError(f"episode {index} action shape differs from info.json")
            for key, expected_shape in states.items():
                if _shape(raw[key]) != expected_shape:
                    raise DatasetError(f"episode {index} state shape for {key} differs from info.json")
            for key in ("action", *states):
                if not np.isfinite(np.asarray(raw[key])).all():
                    raise DatasetError(f"episode {index} {key} contains non-finite values")
            decoded_row = dataset[row_index]
            for camera in cameras:
                expected_shape = tuple(int(part) for part in cast(list[int], features[camera]["shape"]))
                value_shape = _shape(decoded_row[camera])
                if value_shape != expected_shape:
                    raise DatasetError(f"episode {index} camera {camera} frame shape differs from info.json")
                decoded += 1
        expected_start = stop
    if expected_start != total_frames:
        raise DatasetError("episode boundaries do not cover every dataset frame")
    return DatasetInspection(
        root=root,
        repo_id=repo_id,
        fps=fps,
        frames=total_frames,
        episodes=tuple(episodes),
        cameras=cameras,
        selected_episodes=selected_episodes,
        selected_cameras=selected_cameras,
        action_shape=action_shape,
        state_shapes=states,
        decoded_video_frames=decoded,
        tree_sha256=digest,
        dataset=dataset,
    )


def _rgb_numpy(value: object) -> Any:
    try:
        import numpy as np
    except ImportError as error:
        raise DatasetError("numpy is required for LeRobot video conversion") from error
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    array = np.asarray(value)
    if array.ndim != 3:
        raise DatasetError("video frame is not three-dimensional")
    if array.shape[0] in {1, 3, 4} and array.shape[-1] not in {1, 3, 4}:
        array = array.transpose(1, 2, 0)
    if array.shape[-1] != 3:
        raise DatasetError("Cosmos augmentation requires RGB video frames")
    if array.dtype.kind == "f":
        if not np.isfinite(array).all() or array.min() < 0 or array.max() > 1:
            raise DatasetError("floating-point video frame values must be finite and in [0, 1]")
        array = (array * 255).round().astype(np.uint8)
    elif array.dtype != np.uint8:
        if array.min() < 0 or array.max() > 255:
            raise DatasetError("integer video frame values must be in [0, 255]")
        array = array.astype(np.uint8)
    return array


def encode_episode_reference(
    inspection: DatasetInspection,
    episode: Episode,
    camera: str,
    output: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    """Encode exactly one source episode so Cosmos never receives adjacent shard episodes."""

    try:
        import av
    except ImportError as error:
        raise DatasetError("PyAV is required to encode episode references") from error
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".partial.mp4")
    try:
        with av.open(str(temporary), mode="w") as container:
            stream = container.add_stream("libx264", rate=inspection.fps)
            feature = inspection.dataset.features[camera]
            channels, height, width = (int(part) for part in feature["shape"])
            if (
                channels != 3
                or width > 1280
                or height > 720
                or width * height > MAX_PIXELS
                or width % 16
                or height % 16
            ):
                raise DatasetError(
                    f"camera {camera} shape is outside the qualified Cosmos RGB/multiple-of-16/720p bound"
                )
            stream.width = width
            stream.height = height
            stream.pix_fmt = "yuv420p"
            stream.options = {"crf": "18", "preset": "fast", "g": str(inspection.fps)}
            for row_index in range(episode.start, episode.stop):
                if checkpoint is not None:
                    checkpoint()
                frame = av.VideoFrame.from_ndarray(_rgb_numpy(inspection.dataset[row_index][camera]), format="rgb24")
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        _check_container_magic(temporary, ".mp4")
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def decode_generated_video(path: Path, *, episode: Episode, fps: int) -> Any:
    """Decode one output frame at every canonical episode timestamp."""

    try:
        from lerobot.datasets.video_utils import decode_video_frames
    except ImportError as error:
        raise DatasetError("lerobot video support is required to validate generated video") from error
    timestamps = [index / fps for index in range(episode.frames)]
    try:
        frames = decode_video_frames(path, timestamps, tolerance_s=1e-4, backend="pyav", return_uint8=True)
    except Exception as error:
        raise DatasetError("generated Cosmos MP4 cannot be decoded at every source timestamp") from error
    if _shape(frames)[:1] != (episode.frames,):
        raise DatasetError("generated Cosmos MP4 frame count differs from the source episode")
    return frames


def rewrite_variant(
    inspection: DatasetInspection,
    *,
    output_root: Path,
    output_repo_id: str,
    video_replacements: Mapping[tuple[int, str], Path],
    action_replacements: Mapping[int, object],
    provenance: Mapping[str, Any],
    checkpoint: Callable[[], None] | None = None,
) -> DatasetInspection:
    """Create, finalize, reopen, and fully validate one complete dataset variant."""

    try:
        import numpy as np
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
    except ImportError as error:
        raise DatasetError("the pinned LeRobot runtime is required to rewrite a dataset") from error
    remove_existing_output(output_root)
    output_root.parent.mkdir(parents=True, exist_ok=True)
    source = inspection.dataset
    user_features = {key: value for key, value in source.features.items() if key not in AUTO_FEATURES}
    target = LeRobotDataset.create(
        repo_id=output_repo_id,
        fps=inspection.fps,
        features=user_features,
        root=output_root,
        robot_type=source.meta.robot_type,
        use_videos=True,
        video_backend="pyav",
        batch_encoding_size=1,
    )
    # The pinned writer derives auto values but defaults their schema to float32/
    # int64. Retain admitted source dtypes, including canonical float64 timestamps.
    target.meta.info.features.update({key: dict(source.features[key]) for key in AUTO_FEATURES})
    raw_rows = source.hf_dataset.with_format(None)
    try:
        for episode in inspection.episodes:
            if checkpoint is not None:
                checkpoint()
            decoded_replacements: dict[str, Any] = {}
            for camera in inspection.cameras:
                path = video_replacements.get((episode.index, camera))
                if path is None:
                    continue
                decoded_replacements[camera] = decode_generated_video(path, episode=episode, fps=inspection.fps)
                expected = tuple(int(part) for part in source.features[camera]["shape"])
                if _shape(decoded_replacements[camera])[1:] != expected:
                    raise DatasetError(f"generated Cosmos MP4 for episode {episode.index}/{camera} has the wrong shape")
            replacement_actions = action_replacements.get(episode.index)
            if replacement_actions is not None:
                action_array = np.asarray(replacement_actions, dtype=source.features["action"]["dtype"])
                if _shape(action_array) != (episode.frames, *inspection.action_shape):
                    raise DatasetError(f"regenerated action trajectory for episode {episode.index} has the wrong shape")
            else:
                action_array = None
            for offset, row_index in enumerate(range(episode.start, episode.stop)):
                if checkpoint is not None:
                    checkpoint()
                decoded = source[row_index]
                frame = {key: value for key, value in decoded.items() if key not in AUTO_FEATURES}
                original = source_row(source, row_index, rows=raw_rows)
                frame.update(
                    {
                        key: value
                        for key, value in original.items()
                        if key not in AUTO_FEATURES and key not in inspection.cameras
                    }
                )
                frame["task"] = episode.task
                for camera in inspection.cameras:
                    replacement = decoded_replacements.get(camera)
                    if replacement is not None:
                        frame[camera] = replacement[offset]
                if action_array is not None:
                    frame["action"] = action_array[offset]
                target.add_frame(frame)
            target.save_episode()
            # Do not retain earlier episodes' decoded video tensors through frame views.
            del frame, decoded, replacement, decoded_replacements
        target.finalize()
    except Exception:
        with __import__("contextlib").suppress(Exception):
            target.finalize()
        raise
    provenance_path = output_root / "meta" / "fs2-augmentation-provenance.json"
    provenance_path.write_text(
        json.dumps({"schema": PROVENANCE_SCHEMA, **dict(provenance)}, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    write_bundle_manifest(output_root)
    return open_and_validate(
        output_root,
        repo_id=output_repo_id,
        selection=Selection(episodes="all", cameras="all"),
        generation_bounds=False,
        checkpoint=checkpoint,
    )


def extract_uploaded_bundle(archive: Path, destination: Path, *, expected_sha256: str) -> None:
    """Extract an admitted `.tar.zst` without links, traversal, or special files."""

    if archive.stat().st_size > MAX_BUNDLE_BYTES:
        raise DatasetError("uploaded dataset bundle exceeds the 5 GiB compressed bound")
    if sha256_file(archive) != expected_sha256:
        raise DatasetError("uploaded dataset artifact digest mismatch")
    try:
        import zstandard
    except ImportError as error:
        raise DatasetError("zstandard is required to extract an uploaded dataset bundle") from error
    destination.mkdir(parents=True, exist_ok=False)
    count = 0
    total = 0
    try:
        with archive.open("rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as decoded:
            with tarfile.open(fileobj=decoded, mode="r|") as bundle:
                for member in bundle:
                    count += 1
                    total += max(member.size, 0)
                    relative = _safe_relative(member.name, label="archive member")
                    if count > MAX_FILES or total > MAX_EXPANDED_BYTES:
                        raise DatasetError("uploaded dataset bundle exceeds extraction bounds")
                    if member.issym() or member.islnk() or member.isdev():
                        raise DatasetError("uploaded dataset bundle contains a link or special file")
                    target = destination.joinpath(*relative.parts)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        source = bundle.extractfile(member)
                        if source is None:
                            raise DatasetError("uploaded dataset bundle contains an unreadable file")
                        with target.open("xb") as sink:
                            shutil.copyfileobj(source, sink, length=1024 * 1024)
                    else:
                        raise DatasetError("uploaded dataset bundle contains an unsupported member")
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def package_dataset(root: Path, output: Path) -> FileIdentity:
    """Create the immutable zstd tar artifact consumed by the existing store."""

    try:
        import zstandard
    except ImportError as error:
        raise DatasetError("zstandard is required to package a dataset bundle") from error
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".partial")
    with temporary.open("xb") as raw, zstandard.ZstdCompressor(level=6, threads=2).stream_writer(raw) as encoded:
        with tarfile.open(fileobj=encoded, mode="w|") as bundle:
            for path in sorted(root.rglob("*")):
                bundle.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
                if raw.tell() > MAX_BUNDLE_BYTES:
                    raise DatasetError("output dataset exceeds the 5 GiB compressed bound")
    if temporary.stat().st_size > MAX_BUNDLE_BYTES:
        raise DatasetError("output dataset exceeds the 5 GiB compressed bound")
    temporary.replace(output)
    return FileIdentity(path=output.name, size_bytes=output.stat().st_size, sha256=sha256_file(output))


def remove_existing_output(path: Path) -> None:
    """Remove only a validated run-local output directory before an idempotent rebuild."""

    if path.exists():
        if not path.is_dir() or path.is_symlink() or not path.name.startswith("variant-"):
            raise DatasetError("refusing to replace an unsafe output path")
        shutil.rmtree(path)

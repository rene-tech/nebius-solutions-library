"""Cancellable, idempotent LeRobot augmentation batch worker."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from .contracts import AugmentationRequest
from .cosmos import MODEL_REVISION, SERVING_REVISION, CosmosClient, CosmosError
from .dataset import (
    DatasetError,
    DatasetInspection,
    encode_episode_reference,
    open_and_validate,
    package_dataset,
    rewrite_variant,
    sha256_file,
    verify_bundle_manifest,
)
from .localize import localize_huggingface, localize_object_store, localize_uploaded, verify_source_reference

RESULT_SCHEMA = "fs2-serve.nebius.ai/cosmos3-lerobot-augmentation-result/v1"
PROGRESS_SCHEMA = "fs2-serve.nebius.ai/cosmos3-lerobot-progress/v1"


class CancelledError(RuntimeError):
    pass


@dataclass(slots=True)
class Cancellation:
    requested: bool = False

    def install(self) -> None:
        def request(_signum: int, _frame: object) -> None:
            self.requested = True

        signal.signal(signal.SIGTERM, request)
        signal.signal(signal.SIGINT, request)


class Progress:
    def __init__(self, path: Path, *, operation_id: str, total_units: int) -> None:
        self.path = path
        self.operation_id = operation_id
        self.total_units = total_units
        self.completed = 0
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: object) -> None:
        payload = {
            "schema": PROGRESS_SCHEMA,
            "operation_id": self.operation_id,
            "event": event,
            "completed_units": self.completed,
            "total_units": self.total_units,
            "recorded_at_unix": time.time(),
            **fields,
        }
        line = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self.path.open("a", encoding="utf-8") as sink:
            sink.write(line + "\n")
            sink.flush()
            os.fsync(sink.fileno())
        print(line, flush=True)


def _source_repo_id(request: AugmentationRequest) -> str:
    if request.source.kind == "huggingface":
        return request.source.repo_id
    return "fs2/localized-lerobot-input"


def _localize(
    request: AugmentationRequest,
    workspace: Path,
    source_artifact: Path | None,
    request_digest: str,
) -> Path:
    destination = workspace / "localized-input"
    receipt = workspace / "localization-receipt.json"
    if destination.exists() or receipt.exists():
        try:
            record = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise DatasetError("an incomplete localization exists and cannot be reused safely") from error
        if record != {"request_sha256": request_digest}:
            raise DatasetError("localized dataset workspace is bound to another request")
        expected = request.source.manifest_sha256 if request.source.kind == "object-store" else None
        verify_bundle_manifest(destination, expected_manifest_sha256=expected)
        return destination
    if request.source.kind == "huggingface":
        if source_artifact is None:
            raise DatasetError("Hugging Face source requires its admitted source-reference artifact")
        verify_source_reference(source_artifact, request.source)
        localized = localize_huggingface(request.source, destination)
    elif request.source.kind == "object-store":
        if source_artifact is None:
            raise DatasetError("object-store source requires its admitted source-reference artifact")
        verify_source_reference(source_artifact, request.source)
        localized = localize_object_store(request.source, destination)
    else:
        if source_artifact is None:
            raise DatasetError("uploaded-bundle source requires the materialized scientific input artifact")
        localized = localize_uploaded(request.source, destination, source_artifact)
    _write_json(receipt, {"request_sha256": request_digest})
    return localized


def _dimensions(inspection: DatasetInspection, camera: str) -> tuple[int, int]:
    shape = inspection.dataset.features[camera]["shape"]
    channels, height, width = (int(part) for part in shape)
    if channels != 3:
        raise DatasetError(f"camera {camera} is not RGB")
    return width, height


def _request_digest(request: AugmentationRequest) -> str:
    return hashlib.sha256(request.canonical_json().encode()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
    temporary.replace(path)


def run(
    request: AugmentationRequest,
    *,
    operation_id: str,
    workspace: Path,
    platform_base_url: str,
    source_artifact: Path | None = None,
    workload_capability: str | None = None,
) -> Mapping[str, Any]:
    """Execute one controller-owned batch attempt and return its public result."""

    UUID(operation_id)
    workspace.mkdir(parents=True, exist_ok=True)
    request_digest = _request_digest(request)
    result_path = workspace / "result.json"
    receipt_path = workspace / "completion-receipt.json"
    if result_path.is_file() and receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text())
        if receipt.get("request_sha256") != request_digest:
            raise DatasetError("operation workspace is already bound to another augmentation request")
        loaded = json.loads(result_path.read_text())
        if not isinstance(loaded, Mapping):
            raise DatasetError("idempotent result receipt points to an invalid result")
        return loaded
    cancellation = Cancellation()
    cancellation.install()
    localized = _localize(request, workspace, source_artifact, request_digest)
    expected_manifest = request.source.manifest_sha256 if request.source.kind == "object-store" else None
    inspection = open_and_validate(
        localized,
        repo_id=_source_repo_id(request),
        selection=request.selection,
        expected_manifest_sha256=expected_manifest,
    )
    for episode_index in inspection.selected_episodes:
        if any(
            index >= inspection.episodes[episode_index].frames
            for index in request.augmentation.conditioning.frame_indexes
        ):
            raise DatasetError(f"episode {episode_index} does not contain every requested conditioning frame")
    total_units = len(request.variant_seeds) * len(inspection.selected_episodes) * len(inspection.selected_cameras)
    progress = Progress(workspace / "progress.jsonl", operation_id=operation_id, total_units=total_units)
    progress.emit("source-validated", source_tree_sha256=inspection.tree_sha256)
    try:
        client = CosmosClient(
            platform_base_url,
            workload_capability=workload_capability,
            parent_operation_id=operation_id,
        )
    except ValueError as error:
        raise DatasetError(str(error)) from error
    references = workspace / "references"
    generated_root = workspace / "generated"
    failures: list[dict[str, Any]] = []
    variant_results: list[dict[str, Any]] = []
    for variant_index, seed in enumerate(request.variant_seeds):
        if cancellation.requested:
            raise CancelledError
        video_replacements: dict[tuple[int, str], Path] = {}
        operations: list[dict[str, Any]] = []
        for episode_index in inspection.selected_episodes:
            episode = inspection.episodes[episode_index]
            for camera in inspection.selected_cameras:
                if cancellation.requested:
                    raise CancelledError
                reference = references / f"episode-{episode_index:06d}" / f"{camera}.mp4"
                if not reference.is_file():
                    encode_episode_reference(inspection, episode, camera, reference)
                output = (
                    generated_root / f"variant-{variant_index:02d}" / f"episode-{episode_index:06d}" / f"{camera}.mp4"
                )
                width, height = _dimensions(inspection, camera)
                prompt = request.augmentation.prompt(task=episode.task, episode_index=episode_index, camera=camera)
                progress.emit(
                    "generation-started",
                    variant_index=variant_index,
                    episode_index=episode_index,
                    camera=camera,
                    seed=seed,
                )
                last_error: CosmosError | None = None
                for attempt in range(1, request.failure_policy.max_attempts + 1):
                    try:
                        generation = client.generate(
                            reference=reference,
                            output=output,
                            prompt=prompt,
                            width=width,
                            height=height,
                            frames=episode.frames,
                            fps=inspection.fps,
                            seed=seed,
                            augmentation=request.augmentation,
                            unit_key=f"v{variant_index}/e{episode_index}/{camera}/s{seed}",
                            cancelled=lambda: cancellation.requested,
                        )
                        video_replacements[(episode_index, camera)] = generation.video_path
                        operations.append(
                            {
                                "variant_index": variant_index,
                                "episode_index": episode_index,
                                "camera": camera,
                                "seed": seed,
                                "operation": generation.operation,
                                "child_operation_id": generation.operation_id,
                                "input_video_sha256": sha256_file(reference),
                                "output_video_sha256": sha256_file(generation.video_path),
                                "action_policy": "preserve",
                            }
                        )
                        progress.completed += 1
                        progress.emit(
                            "generation-succeeded",
                            variant_index=variant_index,
                            episode_index=episode_index,
                            camera=camera,
                            attempt=attempt,
                        )
                        last_error = None
                        break
                    except CosmosError as error:
                        last_error = error
                        progress.emit(
                            "generation-attempt-failed",
                            variant_index=variant_index,
                            episode_index=episode_index,
                            camera=camera,
                            attempt=attempt,
                            code=error.code,
                            retryable=error.retryable,
                        )
                        if not error.retryable or attempt == request.failure_policy.max_attempts:
                            break
                        time.sleep(min(2 ** (attempt - 1), 4))
                if last_error is not None:
                    failure = {
                        "variant_index": variant_index,
                        "episode_index": episode_index,
                        "camera": camera,
                        "code": last_error.code,
                        "message": str(last_error),
                        "retryable": last_error.retryable,
                    }
                    failures.append(failure)
                    progress.emit("generation-failed", **failure)
                    if request.failure_policy.mode == "fail-fast":
                        raise last_error
        output_root = workspace / "datasets" / f"variant-{variant_index:02d}"
        provenance = {
            "operation_id": operation_id,
            "request_sha256": request_digest,
            "source": {
                "repo_id": inspection.repo_id,
                "tree_sha256": inspection.tree_sha256,
                "episodes": len(inspection.episodes),
                "frames": inspection.frames,
            },
            "variant_index": variant_index,
            "seed": seed,
            "configuration": json.loads(request.canonical_json()),
            "model": {"repository": "nvidia/Cosmos3-Nano", "revision": MODEL_REVISION},
            "runtime": {"vllm_omni_revision": SERVING_REVISION},
            "operations": operations,
            "failures": [item for item in failures if item["variant_index"] == variant_index],
        }
        rewritten = rewrite_variant(
            inspection,
            output_root=output_root,
            output_repo_id=f"fs2/cosmos3-augmentation-{operation_id}-v{variant_index}",
            video_replacements=video_replacements,
            action_replacements={},
            provenance=provenance,
        )
        artifact = package_dataset(output_root, workspace / "artifacts" / f"variant-{variant_index:02d}.tar.zst")
        provenance_digest = sha256_file(output_root / "meta" / "fs2-augmentation-provenance.json")
        variant_results.append(
            {
                "variant_index": variant_index,
                "seed": seed,
                "artifact": {
                    "artifact_id": f"{operation_id}.variant-{variant_index:02d}",
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "media_type": "application/x-tar",
                    "compression": "zstd",
                },
                "validation": {
                    "reader": "lerobot==0.6.1",
                    "episodes": len(rewritten.episodes),
                    "frames": rewritten.frames,
                    "decoded_video_frames": rewritten.decoded_video_frames,
                    "status": "passed",
                },
                "provenance_sha256": provenance_digest,
            }
        )
        progress.emit("variant-published", variant_index=variant_index, artifact_sha256=artifact.sha256)
    status = "succeeded" if not failures else ("partially-succeeded" if variant_results else "failed")
    result: Mapping[str, Any] = {
        "schema": RESULT_SCHEMA,
        "operation_id": operation_id,
        "status": status,
        "progress": {"completed_units": progress.completed, "total_units": total_units},
        "variants": variant_results,
        "failures": failures,
    }
    _write_json(result_path, result)
    _write_json(
        workspace / "artifact-index.json",
        {
            "schema": "fs2-serve.nebius.ai/cosmos3-lerobot-artifact-index/v1",
            "artifacts": [
                {**item["artifact"], "path": f"artifacts/variant-{item['variant_index']:02d}.tar.zst"}
                for item in variant_results
            ],
        },
    )
    _write_json(receipt_path, {"request_sha256": request_digest, "result_sha256": sha256_file(result_path)})
    progress.emit("run-completed", status=status)
    return result

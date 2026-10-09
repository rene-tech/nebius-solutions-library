"""Pinned Cosmos3-Nano client through the attempt-scoped fs2 control-plane delegation."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from .contracts import Augmentation

MODEL_ID = "cosmos3-nano"
MODEL_REPOSITORY = "nvidia/Cosmos3-Nano"
MODEL_REVISION = "7a312c868bcce8e40b3eb40861300a9d0ba3fde1"
SERVING_REVISION = "eb11446b7f2e30ca582f8aff3afe12e9a2e66f6c"
ARTIFACT_RESULT_SCHEMA = "fs2-serve.nebius.ai/operation-artifact-result/v1"
MAX_REFERENCE_BYTES = 512 * 1024 * 1024
MAX_OUTPUT_BYTES = 1024**3
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "expired"})


class CosmosError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class CosmosGeneration:
    video_path: Path
    operation_id: str
    operation: str
    model_revision: str = MODEL_REVISION
    serving_revision: str = SERVING_REVISION


def conditioning_scope(augmentation: Augmentation) -> dict[str, Any]:
    """Scientific scope, independent of successful transport/format validation."""
    result: dict[str, Any] = {
        "mode": augmentation.mode,
        "action_values": "preserved",
        "physical_action_alignment_verified": False,
        "policy_training_validity_verified": False,
    }
    if augmentation.mode == "transfer":
        result.update(
            reference_usage="full_sequence_spatial_controls",
            controls=list(augmentation.conditioning.controls),
            limitation=(
                "Controls guide all source frames but do not guarantee robot contacts, "
                "object identity or action alignment."
            ),
        )
    else:
        result.update(
            reference_usage="selected_prefix_or_suffix_latent_frames",
            latent_frame_indexes=list(augmentation.conditioning.frame_indexes),
            reference_end=augmentation.conditioning.keep,
            # Pinned vLLM-Omni utils.condition_pixel_frame_count, temporal compression4.
            reference_pixel_frame_budget=max(augmentation.conditioning.frame_indexes) * 4 + 1,
            limitation=(
                "Unconditioned future motion is generated, not preserved from the recorded actions; "
                "use transfer for full-sequence controls and validate alignment separately."
            ),
        )
    return result


def _validate_mp4(path: Path, *, maximum: int = MAX_OUTPUT_BYTES) -> None:
    try:
        size = path.stat().st_size
        with path.open("rb") as source:
            head = source.read(32)
    except OSError as error:
        raise CosmosError("COSMOS_MEDIA_UNREADABLE", "Cosmos MP4 is unavailable", retryable=False) from error
    if size < 16 or size > maximum or b"ftyp" not in head:
        raise CosmosError("COSMOS_MEDIA_INVALID", "Cosmos MP4 is invalid or oversized", retryable=False)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_transfer_video_alignment(path: Path, *, width: int, height: int, frames: int, fps: int) -> Path:
    """Reject a mismatched generated clip without rescaling or retiming labels."""

    try:
        import av

        with av.open(str(path), mode="r") as source:
            stream = source.streams.video[0]
            if (stream.width, stream.height) != (width, height) or stream.average_rate != Fraction(fps, 1):
                raise CosmosError(
                    "COSMOS_MEDIA_ALIGNMENT_INVALID",
                    "Cosmos transfer dimensions or FPS differ from the source episode; no resizing or retiming applied",
                    retryable=False,
                )
            count = 0
            for frame in source.decode(video=0):
                if (frame.width, frame.height) != (width, height):
                    raise CosmosError(
                        "COSMOS_MEDIA_ALIGNMENT_INVALID", "Cosmos transfer frame dimensions changed", retryable=False
                    )
                if frame.pts is None or frame.time_base is None or abs(
                    Fraction(frame.pts) * frame.time_base - Fraction(count, fps)
                ) > frame.time_base:
                    raise CosmosError(
                        "COSMOS_MEDIA_ALIGNMENT_INVALID",
                        "Cosmos transfer frame timestamps differ from the source episode; no retiming applied",
                        retryable=False,
                    )
                count += 1
            if count != frames:
                raise CosmosError(
                    "COSMOS_MEDIA_ALIGNMENT_INVALID",
                    "Cosmos transfer output frame count differs from the source episode",
                    retryable=False,
                )
        return path
    except CosmosError:
        raise
    except Exception as error:
        raise CosmosError(
            "COSMOS_MEDIA_INVALID",
            "Cosmos transfer output could not be decoded for source-alignment validation",
            retryable=False,
        ) from error


def _object(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise CosmosError("PLATFORM_RESPONSE_INVALID", f"{label} is not an object", retryable=False)
    return cast(Mapping[str, Any], value)


def _operation_id(value: Mapping[str, Any]) -> str:
    nested = value.get("operation")
    # Bare OperationView has an operation *name*, not an envelope object.
    candidate = nested if isinstance(nested, Mapping) else value
    operation = _object(candidate, label="operation response")
    raw = operation.get("id", operation.get("operation_id"))
    try:
        return str(UUID(str(raw)))
    except (TypeError, ValueError) as error:
        raise CosmosError("PLATFORM_RESPONSE_INVALID", "operation response has no UUID", retryable=False) from error


def _idempotency(*parts: object) -> str:
    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode()).hexdigest()
    return f"lr-{digest}"


class CosmosClient:
    """One-in-flight client using platform uploads, admission, polling and artifacts."""

    def __init__(
        self,
        base_url: str,
        *,
        workload_capability: str | None,
        parent_operation_id: str,
        timeout_seconds: float = 1800,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Cosmos platform base URL must be HTTP(S)")
        if not workload_capability:
            raise ValueError("an attempt-scoped workload capability is required for Cosmos calls")
        UUID(parent_operation_id)
        self.base_url = base_url.rstrip("/")
        self.workload_capability = workload_capability
        self.parent_operation_id = parent_operation_id
        self.timeout_seconds = timeout_seconds

    def _client(self) -> Any:
        try:
            import httpx
        except ImportError as error:
            raise CosmosError(
                "RUNTIME_DEPENDENCY_MISSING", "httpx is required for Cosmos requests", retryable=False
            ) from error
        return httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.workload_capability}", "Accept": "application/json"},
            timeout=httpx.Timeout(min(self.timeout_seconds, 300), connect=10, pool=10),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            follow_redirects=False,
            trust_env=False,
        )

    @staticmethod
    def _check(response: Any, *, action: str, expected: set[int]) -> None:
        if response.status_code in expected:
            return
        retryable = response.status_code == 429 or response.status_code >= 500
        raise CosmosError(
            "PLATFORM_UPSTREAM_ERROR",
            f"fs2 {action} returned HTTP {response.status_code}",
            retryable=retryable,
        )

    def _upload_reference(self, client: Any, reference: Path) -> Mapping[str, Any]:
        _validate_mp4(reference, maximum=MAX_REFERENCE_BYTES)
        size = reference.stat().st_size
        digest = _sha256_file(reference)
        begun_response = client.post(
            "/internal/scientific-workloads/cosmos/v1/scientific-artifacts/uploads",
            headers={"Idempotency-Key": _idempotency(self.parent_operation_id, "upload", digest)},
            json={
                "model_id": MODEL_ID,
                "sha256": digest,
                "size_bytes": size,
                "media_type": "video/mp4",
                "compression": "none",
            },
        )
        self._check(begun_response, action="artifact upload admission", expected={201})
        begun = _object(begun_response.json(), label="artifact upload admission")
        upload_id = str(UUID(str(begun.get("upload_id"))))
        upload_operation_id = _operation_id(begun)
        content_path = begun.get("content_path")
        maximum = begun.get("max_content_bytes")
        if not isinstance(content_path, str) or not content_path.startswith(
            "/internal/scientific-workloads/cosmos/v1/scientific-artifacts/uploads/"
        ):
            raise CosmosError("PLATFORM_RESPONSE_INVALID", "artifact upload has no safe content path", retryable=False)
        if isinstance(maximum, bool) or not isinstance(maximum, int) or size > maximum:
            raise CosmosError("REFERENCE_TOO_LARGE", "episode MP4 exceeds the platform upload bound", retryable=False)
        # Begin replays the same upload identity across variants/retries, but
        # finalized uploads are write-once. Resolve durable state rather than
        # depending on an in-process cache or swallowing an arbitrary PUT 409.
        status_response = client.get(f"/internal/scientific-workloads/cosmos/v1/operations/{upload_operation_id}")
        self._check(status_response, action="artifact upload status", expected={200})
        status = _object(status_response.json(), label="artifact upload status")
        if _operation_id(status) != upload_operation_id:
            raise CosmosError("PLATFORM_RESPONSE_INVALID", "artifact upload status identity differs", retryable=False)
        upload_state = status.get("status")
        if not isinstance(upload_state, str):
            raise CosmosError("PLATFORM_RESPONSE_INVALID", "artifact upload status is invalid", retryable=False)
        if upload_state in {"failed", "cancelled", "expired"}:
            raise CosmosError("PLATFORM_UPSTREAM_ERROR", "artifact upload is no longer usable", retryable=False)
        if upload_state not in {"queued", "succeeded"}:
            raise CosmosError("PLATFORM_RESPONSE_INVALID", "artifact upload status is invalid", retryable=False)
        if upload_state == "queued":
            with reference.open("rb") as source:
                stored = client.put(
                    content_path,
                    headers={"Content-Type": "video/mp4", "Content-Length": str(size)},
                    content=source,
                )
            self._check(stored, action="artifact content upload", expected={200})
            receipt = _object(stored.json(), label="artifact upload receipt")
            if receipt.get("sha256") != digest or receipt.get("size_bytes") != size:
                raise CosmosError(
                    "PLATFORM_RESPONSE_INVALID", "artifact upload identity differs from source", retryable=False
                )
        # Finalization is idempotent: a succeeded upload returns its existing
        # immutable ArtifactRef, which is checked against the local bytes below.
        finalized_response = client.post(
            f"/internal/scientific-workloads/cosmos/v1/scientific-artifacts/uploads/{upload_id}:finalize",
            json={"operation_id": upload_operation_id},
        )
        self._check(finalized_response, action="artifact finalization", expected={200})
        artifact = _object(finalized_response.json(), label="finalized artifact")
        if (
            artifact.get("sha256") != digest
            or artifact.get("size_bytes") != size
            or artifact.get("media_type") != "video/mp4"
        ):
            raise CosmosError(
                "PLATFORM_RESPONSE_INVALID", "finalized artifact identity differs from source", retryable=False
            )
        return artifact

    def _invoke(self, client: Any, payload: Mapping[str, Any], *, unit_key: str) -> str:
        response = client.post(
            f"/internal/scientific-workloads/cosmos/v1/models/{MODEL_ID}:invoke",
            headers={"Idempotency-Key": _idempotency(self.parent_operation_id, "invoke", unit_key)},
            json={"operation": "generate-media", "payload": payload},
        )
        self._check(response, action="Cosmos admission", expected={200, 202})
        try:
            admitted = response.json()
        except ValueError as error:
            raise CosmosError(
                "PLATFORM_RESPONSE_INVALID", "Cosmos admission is not valid JSON", retryable=False
            ) from error
        return _operation_id(_object(admitted, label="Cosmos admission"))

    def _cancel(self, client: Any, operation_id: str) -> None:
        try:
            response = client.post(f"/internal/scientific-workloads/cosmos/v1/operations/{operation_id}:cancel")
            self._check(response, action="Cosmos cancellation", expected={200})
        except CosmosError:
            # Preserve the caller's cancellation outcome. The still-running
            # child remains attributable and visible for operator cleanup.
            return

    def _wait(self, client: Any, operation_id: str, *, cancelled: Callable[[], bool]) -> Mapping[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if cancelled():
                self._cancel(client, operation_id)
                raise CosmosError("CANCELLED", "augmentation cancelled during Cosmos execution", retryable=False)
            response = client.get(f"/internal/scientific-workloads/cosmos/v1/operations/{operation_id}")
            self._check(response, action="Cosmos status", expected={200})
            status = _object(response.json(), label="Cosmos status")
            value = status.get("status")
            if value == "succeeded":
                return status
            if value in TERMINAL_STATUSES:
                code = status.get("error_code")
                detail = status.get("error_detail")
                raise CosmosError(
                    str(code) if isinstance(code, str) else "COSMOS_OPERATION_FAILED",
                    str(detail) if isinstance(detail, str) else f"Cosmos operation ended as {value}",
                    retryable=False,
                )
            time.sleep(2)
        self._cancel(client, operation_id)
        raise CosmosError("COSMOS_OPERATION_TIMEOUT", "Cosmos operation timed out", retryable=True)

    def _result(self, client: Any, operation_id: str) -> Mapping[str, Any]:
        response = client.get(f"/internal/scientific-workloads/cosmos/v1/operations/{operation_id}/result")
        self._check(response, action="Cosmos result", expected={200})
        body = _object(response.json(), label="Cosmos result")
        nested = body.get("result")
        return _object(nested, label="Cosmos result payload") if nested is not None else body

    def _download_video(self, client: Any, result: Mapping[str, Any], output: Path) -> Path:
        if result.get("schema") != ARTIFACT_RESULT_SCHEMA or result.get("content_type") != "video/mp4":
            raise CosmosError("PLATFORM_RESPONSE_INVALID", "Cosmos result is not an MP4 artifact", retryable=False)
        artifact = _object(result.get("artifact"), label="Cosmos output artifact")
        try:
            artifact_id = str(UUID(str(artifact.get("artifact_id"))))
        except (TypeError, ValueError) as error:
            raise CosmosError("PLATFORM_RESPONSE_INVALID", "Cosmos artifact has no UUID", retryable=False) from error
        digest = artifact.get("sha256")
        size = artifact.get("size_bytes")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 16 <= size <= MAX_OUTPUT_BYTES
        ):
            raise CosmosError("PLATFORM_RESPONSE_INVALID", "Cosmos artifact identity is invalid", retryable=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".partial.mp4")
        try:
            with client.stream(
                "GET",
                f"/internal/scientific-workloads/cosmos/v1/artifacts/{artifact_id}/content",
                headers={"Accept": "video/mp4"},
            ) as response:
                self._check(response, action="Cosmos artifact download", expected={200})
                total = 0
                measured = hashlib.sha256()
                with temporary.open("xb") as sink:
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_OUTPUT_BYTES:
                            raise CosmosError(
                                "COSMOS_OUTPUT_TOO_LARGE", "Cosmos output exceeded its bound", retryable=False
                            )
                        measured.update(chunk)
                        sink.write(chunk)
            if total != size or measured.hexdigest() != digest:
                raise CosmosError(
                    "COSMOS_OUTPUT_IDENTITY_MISMATCH",
                    "downloaded Cosmos artifact identity differs",
                    retryable=False,
                )
            _validate_mp4(temporary)
            temporary.replace(output)
            return output
        except CosmosError:
            temporary.unlink(missing_ok=True)
            raise
        except Exception as error:
            temporary.unlink(missing_ok=True)
            raise CosmosError("COSMOS_TRANSPORT_ERROR", "Cosmos artifact download failed", retryable=True) from error

    @staticmethod
    def _video_payload(
        *,
        reference: Mapping[str, Any],
        prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        frames: int,
        fps: int,
        seed: int,
        augmentation: Augmentation,
    ) -> dict[str, Any]:
        mode = "transfer-video" if augmentation.mode == "transfer" else augmentation.mode
        payload: dict[str, Any] = {
            "mode": mode,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "size": f"{width}x{height}",
            "num_frames": frames,
            "fps": fps,
            "seed": seed,
            "num_inference_steps": augmentation.num_inference_steps,
            "guidance_scale": augmentation.guidance_scale,
            "input_reference": dict(reference),
            "output_format": "mp4",
            "output_delivery": "artifact",
        }
        if augmentation.mode == "video-to-video":
            payload.update(
                {
                    "condition_frame_indexes_vision": list(augmentation.conditioning.frame_indexes),
                    "condition_video_keep": augmentation.conditioning.keep,
                }
            )
        elif augmentation.mode == "transfer":
            # The native runtime honors this exact source size before generation.
            # Never substitute a bucket and resize the result after generation.
            payload["resolution"] = min((256, 480, 704, 720), key=lambda value: abs(value - height))
            payload["controls"] = [
                {"control_type": control, "control_weight": 1.0} for control in augmentation.conditioning.controls
            ]
            payload.update(
                {
                    "num_video_frames_per_chunk": min(frames, 400),
                    "num_conditional_frames": 1,
                    "num_first_chunk_conditional_frames": 0,
                    "share_vision_temporal_positions": True,
                    "emphasize_control_in_prompt": True,
                }
            )
        return payload

    def generate(
        self,
        *,
        reference: Path,
        output: Path,
        prompt: str,
        width: int,
        height: int,
        frames: int,
        fps: int,
        seed: int,
        augmentation: Augmentation,
        unit_key: str,
        cancelled: Callable[[], bool],
    ) -> CosmosGeneration:
        """Generate through platform admission; never bypass attribution or artifactization."""

        if cancelled():
            raise CosmosError("CANCELLED", "augmentation cancelled before Cosmos admission", retryable=False)
        try:
            with self._client() as client:
                reference_artifact = self._upload_reference(client, reference)
                payload = self._video_payload(
                    reference=reference_artifact,
                    prompt=prompt,
                    negative_prompt=augmentation.negative_prompt,
                    width=width,
                    height=height,
                    frames=frames,
                    fps=fps,
                    seed=seed,
                    augmentation=augmentation,
                )
                operation_id = self._invoke(client, payload, unit_key=f"{unit_key}/video")
                self._wait(client, operation_id, cancelled=cancelled)
                result = self._result(client, operation_id)
                video_path = self._download_video(client, result, output)
                if augmentation.mode == "transfer":
                    video_path = _validate_transfer_video_alignment(
                        video_path,
                        width=width,
                        height=height,
                        frames=frames,
                        fps=fps,
                    )
                return CosmosGeneration(
                    video_path=video_path,
                    operation_id=operation_id,
                    operation=augmentation.mode,
                )
        except CosmosError:
            raise
        except Exception as error:
            raise CosmosError("COSMOS_TRANSPORT_ERROR", "fs2 Cosmos request failed", retryable=True) from error

"""NVIDIA adapters with platform-owned artifact transport and bounded frame VLM I/O.

No model credentials or remote URLs are accepted in user recipes. The Cosmos
transport is the platform's existing attempt-scoped, attributed child client.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import signal
from pathlib import Path

from .contracts import canonical
from .media import inspect_video, validate_alignment


def transfer_request(info: dict, artifact: dict, prompt: str, params: dict) -> dict:
    """Full-sequence edge controls, never first-frame-only video continuation."""
    return {
        "mode": "transfer-video",
        "prompt": prompt,
        "input_reference": artifact,
        "output_format": "mp4",
        "output_delivery": "artifact",
        "size": f"{info['width']}x{info['height']}",
        "num_frames": info["frames"],
        "fps": info["fps"],
        "seed": params["seed"],
        "resolution": info["height"],
        "num_inference_steps": params["num_inference_steps"],
        "guidance_scale": params["guidance_scale"],
        "control_guidance": params["control_guidance"],
        "controls": [
            {"control_type": "edge", "control_weight": params["control_weight"]}
        ],
        "num_video_frames_per_chunk": info["frames"],
        "num_conditional_frames": 1,
        "num_first_chunk_conditional_frames": 0,
        "share_vision_temporal_positions": True,
        "emphasize_control_in_prompt": True,
    }


def install() -> None:
    import av
    import generation.factory
    from fs2_lerobot_augmentation.cosmos import CosmosClient
    from generation.adapters.base import BaseAdapter, Result, primary_media
    from generation.adapters.openai_chat import OpenAIChatAdapter

    cancelled = False

    def stop(_signum, _frame):
        nonlocal cancelled
        cancelled = True

    signal.signal(signal.SIGTERM, stop)
    original_chat = OpenAIChatAdapter.chat
    original_for_chat = OpenAIChatAdapter.for_chat.__func__

    def bounded_chat(cls, *args, **kwargs):
        kwargs["timeout"] = min(kwargs.get("timeout") or 120, 120)
        return original_for_chat(cls, *args, **kwargs)

    OpenAIChatAdapter.for_chat = classmethod(bounded_chat)

    def frame_chat(self, *, model=None, **kwargs):
        """Explicit 5-frame caption sampling; generation still uses every frame."""
        kwargs = copy.deepcopy(kwargs)
        if (model or self.model) == "MiniMaxAI/MiniMax-M3":
            kwargs.setdefault("extra_body", {}).setdefault("chat_template_kwargs", {})[
                "thinking_mode"
            ] = "disabled"
        for message in kwargs.get("messages", []):
            if not isinstance(message.get("content"), list):
                continue
            content = []
            for item in message["content"]:
                if item.get("type") != "video_url":
                    content.append(item)
                    continue
                value = item["video_url"]["url"]
                if not value.startswith("data:video/mp4;base64,"):
                    raise ValueError("captioning accepts only materialized MP4 bytes")
                data = base64.b64decode(value.split(",", 1)[1], validate=True)
                with av.open(io.BytesIO(data)) as video:
                    count = sum(1 for _ in video.decode(video=0))
                indexes = {round(i * (count - 1) / 4) for i in range(5)}
                with av.open(io.BytesIO(data)) as video:
                    for index, frame in enumerate(video.decode(video=0)):
                        if index in indexes:
                            image = frame.to_image()
                            image.thumbnail((768, 768))
                            buffer = io.BytesIO()
                            image.save(buffer, format="JPEG", quality=85)
                            content.append(
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:image/jpeg;base64,"
                                        + base64.b64encode(buffer.getvalue()).decode()
                                    },
                                }
                            )
            message["content"] = content
        response = original_chat(self, model=model, **kwargs)
        usage = getattr(response, "usage", None)
        if usage is not None and os.environ.get("FS2_VIDEO_CACHE"):
            record = {
                "model": model or self.model,
                "request_id": str(getattr(response, "id", ""))[:128],
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
            }
            usage_path = (
                Path(os.environ["FS2_VIDEO_CACHE"]).parent / "provider-usage.jsonl"
            )
            with usage_path.open("ab") as stream:
                stream.write(canonical(record))
                stream.flush()
                os.fsync(stream.fileno())
        if not kwargs.get("stream"):
            if (
                not response.choices
                or response.choices[0].finish_reason == "length"
                or not response.choices[0].message.content
            ):
                raise ValueError(
                    "caption/verification provider did not produce a complete final answer"
                )
        return response

    OpenAIChatAdapter.chat = frame_chat

    class PlatformTransfer(BaseAdapter):
        def invoke(self, payload):
            if cancelled:
                raise RuntimeError("cancelled")
            media = primary_media(payload)
            reference = Path(media["path"])
            info = inspect_video(reference)
            params = payload["params"]
            client = CosmosClient(
                os.environ["FS2_SCIENTIFIC_INTERNAL_API_URL"],
                workload_capability=os.environ["FS2_SCIENTIFIC_WORKLOAD_CAPABILITY"],
                parent_operation_id=os.environ["FS2_VIDEO_OPERATION_ID"],
            )
            identity = hashlib.sha256(
                canonical(
                    {
                        "source": info["sha256"],
                        "prompt": payload["prompt"],
                        "params": params,
                    }
                )
            ).hexdigest()
            cache = Path(os.environ["FS2_VIDEO_CACHE"])
            cache.mkdir(parents=True, exist_ok=True)
            output = cache / f"{identity}.mp4"
            receipt = cache / f"{identity}.json"
            if receipt.is_file() and output.is_file():
                saved = json.loads(receipt.read_text())
                actual = inspect_video(output)
                validate_alignment(info, actual)
                if actual["sha256"] != saved["sha256"]:
                    raise ValueError("cached generation digest mismatch")
                return Result(
                    media_bytes=output.read_bytes(), request_id=saved["operation_id"]
                )
            with client._client() as http:
                artifact = client._upload_reference(http, reference)
                body = transfer_request(info, artifact, payload["prompt"], params)
                operation = client._invoke(http, body, unit_key=identity)
                client._wait(http, operation, cancelled=lambda: cancelled)
                client._download_video(http, client._result(http, operation), output)
            actual = inspect_video(output)
            validate_alignment(info, actual)
            receipt.write_bytes(
                canonical({"operation_id": operation, "sha256": actual["sha256"]})
            )
            self.record_request(
                {
                    "source_sha256": info["sha256"],
                    "prompt": payload["prompt"],
                    "params": params,
                }
            )
            return Result(media_bytes=output.read_bytes(), request_id=operation)

    generation.factory.ADAPTERS["openai.video.sync"] = PlatformTransfer


def main():
    import sys

    sys.path.insert(0, os.environ.get("PAIDF_MODULES", "/opt/paidf/modules"))
    install()
    from cli import main as upstream_main

    upstream_main()


if __name__ == "__main__":
    main()

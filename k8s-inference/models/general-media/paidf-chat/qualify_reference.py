"""Run NVIDIA's pinned caption/prompt/verifier unchanged against private probes.

This is runtime qualification, not public App or end-to-end workbench evidence.
The supplied output directory must be private. No source video is rewritten.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbench", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.workbench))
    import scientific_video_reference as reference
    from scientific_receipts import save

    os.environ.update(
        PAIDF_ROOT=str(args.reference),
        PAIDF_TRANSPORT="direct",
        PAIDF_VLM_URL="http://127.0.0.1:18250/v1",
        PAIDF_LLM_URL="http://127.0.0.1:18252/v1",
        PAIDF_VLM_MODEL=reference.REFERENCE_MODELS["vlm"],
        PAIDF_LLM_MODEL=reference.REFERENCE_MODELS["llm"],
        SCIENTIFIC_MODELS_API_BASE_URL="https://not-invoked.invalid/v1",
    )
    reference.reference_root()
    from generation.adapters import openai_chat
    import httpx

    original = openai_chat.OpenAI
    calls = []

    def request_hook(request):
        body = json.loads(request.content)
        for message in body["messages"]:
            if isinstance(message["content"], list):
                for part in message["content"]:
                    if part["type"] in {"video_url", "image_url"}:
                        media = part[part["type"]]
                        header, encoded = media["url"].split(",", 1)
                        data = base64.b64decode(encoded, validate=True)
                        media["url"] = {
                            "media_type": header[5:].removesuffix(";base64"),
                            "size_bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                        }
        calls.append(
            {
                "body": body,
                "at": time.time(),
                "raw_request_sha256": hashlib.sha256(request.content).hexdigest(),
            }
        )
        save(args.output / "requests.json", {"calls": calls})

    def client(**kwargs):
        kwargs["http_client"] = httpx.Client(
            timeout=7200, trust_env=False, event_hooks={"request": [request_hook]}
        )
        return original(**kwargs)

    openai_chat.OpenAI = client
    source = args.reference / "data/sample_input.mp4"
    config = reference.bind_config(source, args.output, {"weather_condition": "cloudy"})
    save(args.output / "config.json", config)
    info = reference.inspect_video(source)
    save(args.output / "input.json", info)
    started = time.time()
    caption = reference.caption(config, source)
    save(args.output / "caption.json", caption)
    prompt = reference.prompt(config, source, caption["caption"])
    save(args.output / "prompt.json", prompt)
    # Checking the source exercises the actual generated-question/image request
    # contract. It is not a claim that the source already has target weather.
    attributes = reference.attributes(config, source)
    save(args.output / "source-attribute-check.json", attributes)
    result = {
        "kind": "private-reference-chat-qualification",
        "nvidia_revision": reference.REVISION,
        "models": reference.REFERENCE_MODELS,
        "model_revisions": reference.REFERENCE_REVISIONS,
        "source_sha256": info["sha256"],
        "source_unchanged": reference.digest(source) == info["sha256"],
        "source_frames": info["frames"],
        "source_resolution": [info["width"], info["height"]],
        "requests": len(calls),
        "elapsed_seconds": time.time() - started,
        "public_app_qualified": False,
        "workbench_qualified": False,
    }
    save(args.output / "result.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()

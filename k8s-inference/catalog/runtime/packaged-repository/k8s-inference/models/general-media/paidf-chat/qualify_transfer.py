"""One private Transfer call using an already generated NVIDIA reference prompt.

This does not orchestrate the pipeline or retry generation. The caller supplies
the output of the explicit reference prompt stage and retains this new receipt.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbench", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.workbench))
    import scientific_video_reference as reference
    from scientific_receipts import save
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    config = json.loads(args.config.read_text())
    prompt = json.loads(args.prompt.read_text())["prompt"]
    source = args.source.read_bytes()
    payload = {**config["augmentation"]["parameters"], "prompt": prompt,
               "video": base64.b64encode(source).decode(), "output_delivery": "artifact"}
    payload["resolution"] = str(payload["resolution"])
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    info = reference.inspect_video(args.source)
    receipt = {"request_sha256": hashlib.sha256(body).hexdigest(), "input": info,
               "parameters": {key: value for key, value in payload.items() if key != "video"},
               "adapter_image": "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/general-media/cosmos-transfer25-adapter@sha256:4297473ad12501a70ef4d1e0b706bd829a660e4e1e946b82f2d5da44fdab2dea",
               "started_at": time.time(), "public_app_qualified": False}
    save(args.output / "request.json", receipt)
    with httpx.Client(base_url="http://127.0.0.1:18264", timeout=1900, trust_env=False) as client:
        client.get("/v1/health/ready", timeout=15).raise_for_status()
        with client.stream("POST", "/v1/transfer", content=body, headers={"content-type": "application/json"}) as response:
            receipt["http_status"] = response.status_code
            receipt["content_type"] = response.headers.get("content-type")
            receipt["inference_attempts"] = 1
            save(args.output / "request.json", receipt)
            response.raise_for_status()
            if receipt["content_type"] != "video/mp4":
                raise ValueError("Transfer returned no MP4")
            destination = args.output / "output.mp4"
            with destination.open("xb") as output:
                for chunk in response.iter_bytes():
                    output.write(chunk)
    receipt["output"] = reference.inspect_video(destination)
    receipt["media_observation"] = reference.alignment(info, receipt["output"])
    receipt["source_unchanged"] = reference.digest(args.source) == info["sha256"]
    receipt["elapsed_seconds"] = time.time() - receipt["started_at"]
    save(args.output / "result.json", receipt)
    print(json.dumps({"native_transport_passed": True, "source_unchanged": receipt["source_unchanged"],
                      "output_sha256": receipt["output"]["sha256"],
                      "source_frames": info["frames"], "output_frames": receipt["output"]["frames"],
                      "elapsed_seconds": receipt["elapsed_seconds"], "quality_evaluated": False}))


if __name__ == "__main__":
    main()

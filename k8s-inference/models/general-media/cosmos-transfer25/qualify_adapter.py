"""One bounded real-container transfer test; never retries paid inference."""

import argparse
import base64
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fs2_video.media import inspect_video, validate_alignment

PROFILE_ID = "e74ebba119c8a196dca12cac66aa1b5323291048a855fe02ceb6b664f334c672"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--adapter-image", required=True)
    parser.add_argument("--seed", type=int, default=43)
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if (
        url.scheme != "http"
        or url.hostname != "127.0.0.1"
        or url.path not in {"", "/"}
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        parser.error("qualification requires a loopback-only adapter")
    if not re.fullmatch(r"cr\.eu-north1\.nebius\.cloud/[^\s@]+@sha256:[a-f0-9]{64}", args.adapter_image):
        parser.error("qualification requires an immutable adapter image")
    if not 0 <= args.seed <= 2147483647:
        parser.error("unsupported seed")
    source = inspect_video(args.source)
    prompt = args.prompt_file.read_text().strip()
    if not 1 <= len(prompt) <= 4096:
        parser.error("prompt must contain 1–4096 characters")
    args.output_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    parameters = {
        "seed": args.seed,
        "num_steps": 35,
        "guidance": 7,
        "control_weight": 1.0,
        "output_delivery": "artifact",
    }
    body = {
        **parameters,
        "prompt": prompt,
        "video": base64.b64encode(args.source.read_bytes()).decode("ascii"),
    }
    raw_body = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    receipt = {
        "schema": "fs2-serve.nebius.ai/cosmos-transfer25-adapter-probe/v1",
        "started_at": datetime.now(UTC).isoformat(),
        "adapter_image": args.adapter_image,
        "profile_id": PROFILE_ID,
        "source": source,
        "request_parameters": parameters,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "request_sha256": hashlib.sha256(raw_body).hexdigest(),
        "alignment_passed": False,
        "inference_request_attempted": False,
        "public_platform_path_tested": False,
        "customer_ready": False,
    }
    started = time.monotonic()
    stage = "readiness"
    try:
        with httpx.Client(
            base_url=args.base_url,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(1830, connect=10),
        ) as client:
            ready = client.get("/v1/health/ready", timeout=15)
            receipt["readiness_status"] = ready.status_code
            if ready.status_code != 200 or ready.json().get("profile_id") != PROFILE_ID:
                raise ValueError("pinned runtime is not ready")
            stage = "inference"
            receipt["inference_request_attempted"] = True
            with client.stream(
                "POST",
                "/v1/transfer",
                content=raw_body,
                headers={"content-type": "application/json"},
            ) as response:
                receipt["http_status"] = response.status_code
                receipt["content_type"] = response.headers.get("content-type")
                if response.status_code != 200 or receipt["content_type"] != "video/mp4":
                    raise ValueError("adapter did not return a successful MP4")
                output = args.output_directory / "output.mp4"
                size = 0
                with output.open("xb") as stream:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 128 * 1024**2:
                            raise ValueError("output exceeds contract")
                        stream.write(chunk)
            stage = "alignment"
            receipt["output"] = inspect_video(output)
            validate_alignment(source, receipt["output"])
            receipt.update(alignment_passed=True, status="adapter-generation-structure-passed")
    except Exception as error:
        receipt.update(status="failed", error_type=type(error).__name__, failure_stage=stage)
    finally:
        receipt.update(
            elapsed_seconds=time.monotonic() - started,
            finished_at=datetime.now(UTC).isoformat(),
        )
        (args.output_directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt), flush=True)
    return 0 if receipt["alignment_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

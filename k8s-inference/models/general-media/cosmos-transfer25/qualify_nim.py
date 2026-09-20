"""One explicit, bounded direct-NIM video probe; not public App qualification.

Run using the pinned PAIDF environment with the existing fs2_video package on
PYTHONPATH. Retain private video/prompt data outside Git.
"""

import argparse
import base64
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fs2_video.media import inspect_video, validate_alignment

IMAGE = (
    "nvcr.io/nim/nvidia/cosmos-transfer2.5-2b@sha256:1891a2421b57cd5f2249f0b44a2720bbca24804e8e579af297a90c876d62659f"
)
MAX_RESULT_BYTES = 180 * 1024**2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--seed", type=int, default=42)
    arguments = parser.parse_args()
    parsed = urlsplit(arguments.base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1"}
        or parsed.path not in {"", "/"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        parser.error("the direct probe requires a loopback-only Kubernetes port-forward")
    if not 0 <= arguments.seed <= 2147483647:
        parser.error("seed is outside the supported range")
    source_info = inspect_video(arguments.source)
    prompt = arguments.prompt_file.read_text().strip()
    if not 1 <= len(prompt) <= 4096:
        parser.error("prompt must contain 1–4096 characters")
    # Refuse an existing destination before any paid generation.
    arguments.output_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    receipt = {
        "schema": "fs2-serve.nebius.ai/cosmos-transfer25-direct-probe/v1",
        "started_at": datetime.now(UTC).isoformat(),
        "image": IMAGE,
        "requested_profile_id": arguments.profile_id,
        "source": source_info,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "request_parameters": {
            "seed": arguments.seed,
            "guidance": 7.0,
            "num_steps": 35,
            "sigma_max": 90,
            "resolution": source_info["height"],
            "edge": {"control_weight": 1.0},
        },
        "alignment_passed": False,
        "weather_verified": False,
        "motion_verified": False,
        "public_platform_path_tested": False,
        "customer_ready": False,
        "inference_request_attempted": False,
    }
    started = time.monotonic()
    stage = "readiness"
    try:
        with httpx.Client(
            base_url=arguments.base_url,
            timeout=httpx.Timeout(1800, connect=10),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            ready = client.get("/v1/health/ready", timeout=15)
            receipt["readiness_status"] = ready.status_code
            if ready.status_code != 200:
                raise ValueError("NIM is not ready")
            # Runtime identity is retained separately from the operator's selection.
            for name in ("manifest", "metadata"):
                stage = name
                observed = client.get(f"/v1/{name}", timeout=15)
                receipt[name + "_http_status"] = observed.status_code
                if observed.status_code != 200 or len(observed.content) > 8 * 1024**2:
                    raise ValueError(f"NIM {name} is unavailable or oversized")
                (arguments.output_directory / f"{name}.json").write_bytes(observed.content)
                receipt[name + "_sha256"] = hashlib.sha256(observed.content).hexdigest()
            body = {
                **receipt["request_parameters"],
                "prompt": prompt,
                "negative_prompt": (
                    "game playing with bad crappy graphics, cartoonish frames, old outdated games, "
                    "fake lighting, raw basic textures, primitive geometry, pixelated poor CG quality, "
                    "subtitles, unrealistic."
                ),
                "video": base64.b64encode(arguments.source.read_bytes()).decode("ascii"),
            }
            receipt["request_sha256"] = hashlib.sha256(
                json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest()
            stage = "inference"
            payload = bytearray()
            receipt["inference_request_attempted"] = True
            with client.stream("POST", "/v1/infer", json=body) as response:
                receipt["http_status"] = response.status_code
                if response.status_code != 200:
                    raise ValueError("NIM inference returned a non-200 status")
                for chunk in response.iter_bytes():
                    if len(payload) + len(chunk) > MAX_RESULT_BYTES:
                        raise ValueError("NIM response exceeds the bound")
                    payload.extend(chunk)
            stage = "output-decoding"
            envelope = json.loads(payload)
            if not isinstance(envelope.get("b64_video"), str):
                raise ValueError("NIM did not return b64_video")
            output = arguments.output_directory / "output.mp4"
            output.write_bytes(base64.b64decode(envelope["b64_video"], validate=True))
            stage = "output-alignment"
            receipt["output"] = inspect_video(output)
            validate_alignment(source_info, receipt["output"])
            receipt["alignment_passed"] = True
            receipt["status"] = "direct-generation-structure-passed"
    except Exception as error:
        receipt["status"] = "failed"
        receipt["error_type"] = type(error).__name__
        receipt["failure_stage"] = stage
        # No response bodies, URLs, prompt data or raw exception text in receipts.
    finally:
        receipt["elapsed_seconds"] = time.monotonic() - started
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        (arguments.output_directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt), flush=True)
    return 0 if receipt["alignment_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

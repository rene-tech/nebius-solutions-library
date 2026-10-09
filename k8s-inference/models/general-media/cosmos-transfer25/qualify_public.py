"""Ordinary-key public HTTP qualification. One invocation; no generation retry.

Retains each admission before polling, and validates exact artifact bytes and
source alignment. This is not a chat/workbench or approved-batch qualification.
"""

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fs2_video.media import inspect_video, validate_alignment

MODEL = "cosmos-transfer2-5-2b"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    parsed = urlsplit(args.origin)
    if parsed.scheme != "https" or parsed.hostname != "89.169.99.188" or parsed.path or parsed.query or parsed.fragment:
        parser.error("this retained canary requires the exact TLS-verified Stockholm gateway origin")
    if args.key_file.stat().st_mode & 0o077:
        parser.error("key disclosure must be owner-readable only")
    key = json.loads(args.key_file.read_text())["secret"]
    source = inspect_video(args.source)
    if not 93 <= source["frames"] <= 400:
        parser.error("Transfer requires 93–400 frames")
    prompt = args.prompt_file.read_text().strip()
    if not 1 <= len(prompt) <= 4096 or not 0 <= args.seed <= 2147483647:
        parser.error("prompt or seed violates the public contract")
    args.output_directory.mkdir(mode=0o700, exist_ok=False)
    receipt = {
        "schema": "fs2-serve.nebius.ai/cosmos-transfer25-public-probe/v1",
        "started_at": datetime.now(UTC).isoformat(),
        "model": MODEL,
        "source": source,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "transport": "public-http",
        "customer_shaped_key": True,
        "public_platform_path_tested": True,
        "customer_ready": False,
        "alignment_passed": False,
        "inference_request_attempted": False,
    }

    def save():
        (args.output_directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")

    def identity(value):
        if isinstance(value.get("operation"), dict):
            value = value["operation"]
        return str(UUID(value["operation_id"] if "operation_id" in value else value["id"]))

    started = time.monotonic()
    stage = "discovery"
    save()
    try:
        with httpx.Client(
            base_url=args.origin,
            headers={"Authorization": "Bearer " + key},
            timeout=60,
            verify=True,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            discovery = client.get("/v1/models")
            discovery.raise_for_status()
            (args.output_directory / "discovery.json").write_text(json.dumps(discovery.json(), indent=2) + "\n")
            stage = "upload"
            upload = client.post(
                "/v1/scientific-artifacts/uploads",
                headers={
                    "Idempotency-Key": "transfer25-upload-" + source["sha256"],
                },
                json={
                    "model_id": MODEL,
                    "sha256": source["sha256"],
                    "size_bytes": source["size_bytes"],
                    "media_type": "video/mp4",
                    "compression": "none",
                },
            )
            upload.raise_for_status()
            admitted = upload.json()
            receipt["upload_operation_id"] = identity(admitted)
            save()
            upload_id = str(UUID(admitted["upload_id"]))
            content_path = admitted["content_path"]
            if not content_path.startswith("/v1/scientific-artifacts/uploads/" + upload_id + "/content"):
                raise ValueError("unexpected upload destination")
            status = client.get("/v1/operations/" + receipt["upload_operation_id"])
            status.raise_for_status()
            if status.json()["status"] == "queued":
                stored = client.put(
                    content_path, content=args.source.read_bytes(), headers={"Content-Type": "video/mp4"}
                )
                stored.raise_for_status()
            elif status.json()["status"] != "succeeded":
                raise ValueError("upload is no longer usable")
            finalized = client.post(
                "/v1/scientific-artifacts/uploads/" + upload_id + ":finalize",
                json={"operation_id": receipt["upload_operation_id"]},
            )
            finalized.raise_for_status()
            artifact = finalized.json()
            if artifact["sha256"] != source["sha256"] or artifact["size_bytes"] != source["size_bytes"]:
                raise ValueError("uploaded artifact differs from source")
            body = {
                "operation": "transfer-video",
                "payload": {
                    "video": artifact,
                    "prompt": prompt,
                    "seed": args.seed,
                    "num_steps": 35,
                    "guidance": 7,
                    "control_weight": 1.0,
                    "output_delivery": "artifact",
                },
            }
            payload_sha = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            receipt["request_sha256"] = payload_sha
            request_headers = {"Idempotency-Key": "transfer25-public-" + payload_sha, "x-fs2-wait-seconds": "0"}
            receipt["inference_request_attempted"] = True
            stage = "admission"
            save()
            response = client.post("/v1/models/" + MODEL + ":invoke", json=body, headers=request_headers)
            response.raise_for_status()
            receipt["operation_id"] = identity(response.json())
            receipt["admission_status"] = response.status_code
            save()
            print(json.dumps({"operation_id": receipt["operation_id"], "phase": "admitted"}), flush=True)
            stage = "poll"
            deadline = time.monotonic() + 2100
            previous = None
            while time.monotonic() < deadline:
                response = client.get("/v1/operations/" + receipt["operation_id"])
                response.raise_for_status()
                status = response.json()
                if identity(status) != receipt["operation_id"]:
                    raise ValueError("operation identity changed")
                receipt["operation_status"] = status["status"]
                save()
                if status["status"] != previous:
                    print(json.dumps({"operation_id": receipt["operation_id"], "status": status["status"]}), flush=True)
                    previous = status["status"]
                if status["status"] == "succeeded":
                    (args.output_directory / "operation.json").write_text(json.dumps(status, indent=2) + "\n")
                    break
                if status["status"] in {"failed", "cancelled", "expired"}:
                    receipt["error_code"] = status.get("error_code")
                    raise ValueError("generation did not succeed")
                time.sleep(5)
            else:
                raise TimeoutError("poll deadline; operation retained, do not resubmit")
            stage = "result"
            response = client.get("/v1/operations/" + receipt["operation_id"] + "/result")
            response.raise_for_status()
            value = response.json()
            result = value.get("result", value)
            if result.get("content_type") != "video/mp4":
                raise ValueError("operation did not return an MP4 artifact")
            reference = result["artifact"]
            if not 16 <= reference["size_bytes"] <= 128 * 1024**2:
                raise ValueError("output exceeds contract")
            artifact_id = str(UUID(reference["artifact_id"]))
            (args.output_directory / "result.json").write_text(json.dumps(value, indent=2) + "\n")
            stage = "download"
            output = args.output_directory / "output.mp4"
            size = 0
            with client.stream("GET", "/v1/artifacts/" + artifact_id + "/content") as response:
                response.raise_for_status()
                with output.open("xb") as sink:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > reference["size_bytes"]:
                            raise ValueError("download exceeded declared size")
                        sink.write(chunk)
            generated = inspect_video(output)
            if generated["sha256"] != reference["sha256"] or size != reference["size_bytes"]:
                raise ValueError("download hash or size differs from artifact")
            validate_alignment(source, generated)
            receipt.update(output=generated, alignment_passed=True, artifact_id=artifact_id)
            stage = "idempotent-replay"
            replay = client.post("/v1/models/" + MODEL + ":invoke", json=body, headers=request_headers)
            replay.raise_for_status()
            if identity(replay.json()) != receipt["operation_id"]:
                raise ValueError("replay created another operation")
            receipt.update(idempotent_replay_passed=True, status="public-http-artifact-structure-passed")
    except Exception as error:
        receipt.update(status="failed", failure_stage=stage, error_type=type(error).__name__)
        if isinstance(error, httpx.HTTPStatusError):
            receipt["http_status"] = error.response.status_code
    finally:
        receipt.update(elapsed_seconds=time.monotonic() - started, finished_at=datetime.now(UTC).isoformat())
        save()
    print(json.dumps(receipt), flush=True)
    return 0 if receipt.get("status") == "public-http-artifact-structure-passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

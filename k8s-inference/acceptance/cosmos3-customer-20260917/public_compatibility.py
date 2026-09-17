#!/usr/bin/env python3
"""Four bounded supplemental Cosmos cases; no canary issuance or implicit retries."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import public_media as media

CASES = (("mcp", "text-to-image"), ("http", "text-to-video"), ("mcp", "text-to-video"), ("mcp", "image-to-video"))


def payload(mode: str, reference: object = None) -> dict:
    body = {
        "prompt": "A first-person robot gripper moves around a wooden office desk. Warm afternoon lighting.",
        "size": "256x256",
        "seed": 20260917,
        "num_inference_steps": 35,
        "guidance_scale": 6.0,
    }
    if mode != "text-to-image":
        media.check(mode in {"text-to-video", "image-to-video"}, "unsupported_compatibility_mode")
        body.update(num_frames=33, fps=20, generate_sound=False)
    if mode == "image-to-video":
        media.check(reference is not None, "generated_png_reference_required")
        body["input_reference"] = reference
    return body


def http_invocation(body: dict) -> dict:
    # Deliberately exercise the pre-existing JSON/base64 T2V contract.
    return {"operation": "generate-media", "payload": body | {"mode": "text-to-video", "output_format": "mp4"}}


def decode_result(envelope: dict, content_type: str, downloaded: bytes | None = None) -> tuple[bytes, dict]:
    result = envelope.get("result", envelope)
    artifact = result.get("artifact")
    delivery = "inline-json-base64"
    if artifact:
        media.check(downloaded is not None, "artifact_bytes_missing")
        media.check(
            hashlib.sha256(downloaded).hexdigest() == artifact["sha256"] and len(downloaded) == artifact["size_bytes"],
            "artifact_identity_mismatch",
        )
        if result.get("content_type") == content_type:
            return downloaded, {"delivery": "binary-artifact", "artifact": artifact}
        media.check(result.get("content_type") == "application/json", "unexpected_artifact_content_type")
        result = json.loads(downloaded)
        delivery = "externalized-json-base64"
    media.check(result.get("mime_type") == content_type, "legacy_media_type_mismatch")
    raw = base64.b64decode(result["data_base64"], validate=True)
    media.check(
        hashlib.sha256(raw).hexdigest() == result["sha256"] and len(raw) == result["bytes"],
        "legacy_media_identity_mismatch",
    )
    return raw, {
        "delivery": delivery,
        "artifact": artifact,
        "legacy_metadata": {key: value for key, value in result.items() if key != "data_base64"},
    }


def png_probe(path: Path) -> dict:
    raw = path.read_bytes()
    media.check(raw.startswith(b"\x89PNG\r\n\x1a\n"), "png_magic_invalid")
    ffmpeg = shutil.which("ffmpeg")
    media.check(ffmpeg is not None, "ffmpeg_required")
    decoded = subprocess.run(  # noqa: S603 - fixed executable/arguments, task-owned media
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    width, height = int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    media.check((width, height) == (256, 256) and len(decoded) == width * height * 3, "png_decode_geometry_mismatch")
    return media.old.file_identity(path) | {
        "width": width,
        "height": height,
        "decoded_rgb_sha256": hashlib.sha256(decoded).hexdigest(),
    }


def validate_contract(tools: dict, schema: dict, reference: dict) -> None:
    generic = next(
        row["input_schema"] for row in schema["contracts"] if row["tool_name"] == "cosmos3_nano_generate_media_native"
    )
    for transport, mode in CASES:
        body = payload(mode, reference)
        if transport == "http":
            media.Draft202012Validator(generic).validate(http_invocation(body)["payload"])
        else:
            tool = "cosmos3_nano_" + mode.replace("-", "_")
            media.Draft202012Validator(tools[tool]["inputSchema"]).validate(
                body | {"idempotency_key": media.PREFIX + "offline-contract", "wait_seconds": 0}
            )


async def run_case(args, public, before, token, transport, mode, reference=None) -> dict:
    name = transport + "-" + mode
    path = args.output / (name + ".json")
    body = payload(mode, reference)
    row = {
        "case": name,
        "payload": body,
        "payload_sha256": media.digest(body),
        "idempotency_key": media.PREFIX + str(uuid4()),
        "started_at": media.now(),
        "state": "prepared",
    }
    media.check(not path.exists(), "existing_compatibility_case_refused")
    current = await asyncio.to_thread(media.collect, args)
    media.check(media.release(current) == media.release(before), "release_changed_before_submission")
    row["serving_before"] = current["serving"]
    row["pods_before"] = current["serving_pods"]
    row["observed_start_state"] = "cold" if sum(d["replicas"] for d in current["serving"]) == 0 else "hot"
    media.checkpoint(path, row, token)

    async def submit():
        if transport == "mcp":
            return media.operation_id(
                await public.call(
                    "cosmos3_nano_" + mode.replace("-", "_"),
                    body | {"idempotency_key": row["idempotency_key"], "wait_seconds": 0},
                )
            )
        response = await public.http.post(
            f"/v1/models/{media.MODEL}:invoke",
            json=http_invocation(body),
            headers={"Idempotency-Key": row["idempotency_key"], "X-FS2-Wait-Seconds": "0"},
        )
        media.check(response.status_code in {200, 202}, "legacy_http_admission_failed")
        return str(response.headers["x-fs2-operation-id"])

    try:
        row.update(operation_id=await submit(), state="admitted")
        media.checkpoint(path, row, token)
        row["inflight_replay_operation_id"] = await submit()
        media.check(row["inflight_replay_operation_id"] == row["operation_id"], "inflight_replay_changed_operation")
        row["terminal"] = (
            await public.wait(row["operation_id"], args.timeout_seconds)
            if transport == "mcp"
            else await media.wait_http(public.http, row["operation_id"], args.timeout_seconds)
        )
        if transport == "mcp":
            envelope = await public.call("get_operation_result", {"operation_id": row["operation_id"]})
        else:
            response = await public.http.get(f"/v1/operations/{row['operation_id']}/result")
            media.check(response.status_code == 200, "legacy_http_result_failed")
            envelope = response.json()
        media.write_private(args.output / (name + "-result.json"), envelope, (token,))
        artifact = envelope.get("result", envelope).get("artifact")
        downloaded = None
        if artifact:
            download_path = args.output / (name + "-artifact.bin")
            if transport == "mcp":
                await public.download("download_model_artifact", artifact, download_path)
            else:
                response = await public.http.get(f"/v1/artifacts/{artifact['artifact_id']}/content")
                media.check(response.status_code == 200, "legacy_http_artifact_download_failed")
                with download_path.open("xb") as stream:
                    stream.write(response.content)
            downloaded = download_path.read_bytes()
        content_type = "image/png" if mode == "text-to-image" else "video/mp4"
        raw, metadata = decode_result(envelope, content_type, downloaded)
        destination = args.output / (name + (".png" if mode == "text-to-image" else ".mp4"))
        with destination.open("xb") as stream:
            stream.write(raw)
        decoded = png_probe(destination) if mode == "text-to-image" else media.old.mp4_probe(destination)
        if mode != "text-to-image":
            media.check(
                (decoded["width"], decoded["height"], decoded["frames"], decoded["fps"]) == (256, 256, 33, 20),
                "video_geometry_timeline_mismatch",
            )
        row.update(
            decoded=decoded,
            output=metadata,
            terminal_replay_operation_id=await submit(),
            completed_at=media.now(),
            state="passed",
        )
        media.check(row["terminal_replay_operation_id"] == row["operation_id"], "terminal_replay_changed_operation")
    except Exception as error:
        row.update(state="failed", failure_type=type(error).__name__)
        if row.get("operation_id"):
            response = await public.http.get(f"/v1/operations/{row['operation_id']}")
            if response.status_code == 200:
                row["last_operation"] = response.json()
        media.checkpoint(path, row, token)
        raise
    media.checkpoint(path, row, token)
    print(json.dumps({"case": name, "operation_id": row["operation_id"], "state": row["state"]}), flush=True)
    return row


async def execute(args) -> None:
    before = await asyncio.to_thread(media.collect, args)
    media.check(before["adapter_sha256"] == media.ADAPTER_SHA256, "adapter_identity_mismatch")
    media.check(
        before["serving"]
        and all(
            all(image.endswith("@" + media.RUNTIME_DIGEST) for image in row["runtime_images"])
            for row in before["serving"]
        ),
        "runtime_identity_mismatch",
    )
    key = media.private_json(args.key_file)
    token = media.validate_key(key, before)
    args.output.mkdir(mode=0o700, parents=True)
    origin = before["public_endpoint"].rstrip("/")
    media.check(origin.startswith("https://"), "verified_https_required")
    record = {
        "scope": "supplemental-four-case-media-compatibility",
        "customer_ready": False,
        "started_at": media.now(),
        "release": media.release(before),
        "calls": [],
    }
    async with media.httpx2.AsyncClient(
        base_url=origin,
        timeout=60,
        trust_env=False,
        follow_redirects=False,
        headers={"Authorization": "Bearer " + token, "Origin": origin},
    ) as http:
        async with media.Client(
            media.streamable_http_client(origin + "/mcp", http_client=http), mode=media.old.MCP_PROTOCOL_VERSION
        ) as mcp:
            public = media.old.PublicClient(mcp, http)
            tools, catalog = await media.old.inventory(mcp)
            schema = await public.call("get_model_schema", {"model_id": media.MODEL, "protocol": "native"})
            # A syntactically valid placeholder checks I2V before any generation.
            placeholder = {
                "artifact_id": str(uuid4()),
                "sha256": "0" * 64,
                "size_bytes": 1,
                "media_type": "image/png",
                "compression": "none",
            }
            validate_contract(tools, schema, placeholder)
            record["public_contract_sha256"] = media.digest(schema)
            record["tool_catalog_sha256"] = catalog
            uploaded = None
            for transport, mode in CASES:
                if mode == "image-to-video":
                    uploaded = await public.upload(
                        media.MODEL,
                        args.output / "mcp-text-to-image.png",
                        "image/png",
                        "none",
                        media.upload_key(args.output, key["token_id"]),
                    )
                    media.write_private(args.output / "uploaded-png.json", uploaded, (token,))
                    validate_contract(tools, schema, uploaded)
                record["calls"].append(await run_case(args, public, before, token, transport, mode, uploaded))
                media.checkpoint(args.output / "partial-receipt.json", record, token)
            after = await asyncio.to_thread(media.collect, args)
            media.check(media.release(before) == media.release(after), "release_changed_during_compatibility")
            _, after_catalog = await media.old.inventory(mcp)
            media.check(catalog == after_catalog, "catalog_changed_during_compatibility")
            record.update(
                completed_at=media.now(), outcome="partial_scope_passed", serving_pods_after=after["serving_pods"]
            )
            media.checkpoint(args.output / "partial-receipt.json", record, token)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("plan", "run"), default="plan")
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--context")
    parser.add_argument("--expected-cp-image")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    args = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    if args.action == "plan":
        print(json.dumps({"cases": CASES, "customer_ready": False, "admission": "explicit run only"}))
        return
    media.check(
        args.kubeconfig
        and args.context
        and args.expected_cp_image
        and args.key_file
        and args.output
        and 0 < args.timeout_seconds <= 7200,
        "bounded_live_arguments_required",
    )
    asyncio.run(execute(args))


if __name__ == "__main__":
    main()

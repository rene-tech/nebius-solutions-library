#!/usr/bin/env python3
"""Run fail-closed public acceptance for Cosmos MP4 and LeRobot workflows.

The runner is deliberately unable to turn raw MCP-SDK evidence into a
LibreChat qualification.  Supply a matching LibreChat client receipt from the
same release and operations; otherwise emitted evidence remains ``partial`` at
the generic customer-capability gate.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

HERE = Path(__file__).resolve().parent
INFERENCE_ROOT = HERE.parents[1]
CONTROL_PLANE = INFERENCE_ROOT / "components" / "control-plane" / "src"
LEROBOT_ROOT = INFERENCE_ROOT / "models" / "general-media" / "lerobot-augmentation"
sys.path.insert(0, str(CONTROL_PLANE))
sys.path.insert(0, str(LEROBOT_ROOT / "runtime" / "src"))

from fs2_serve.live_acceptance import MCP_PROTOCOL_VERSION  # noqa: E402

GATE_SPEC = importlib.util.spec_from_file_location(
    "customer_capability_gate",
    INFERENCE_ROOT / "acceptance/customer-readiness/capability_gate.py",
)
MANIFEST_SPEC = importlib.util.spec_from_file_location(
    "cosmos_manifest", HERE / "build_manifest.py"
)
assert GATE_SPEC and GATE_SPEC.loader and MANIFEST_SPEC and MANIFEST_SPEC.loader
gate = importlib.util.module_from_spec(GATE_SPEC)
GATE_SPEC.loader.exec_module(gate)
manifest_builder = importlib.util.module_from_spec(MANIFEST_SPEC)
MANIFEST_SPEC.loader.exec_module(manifest_builder)

TERMINAL = {"succeeded", "failed", "cancelled", "preempted", "expired"}
SCENARIOS = ("v2v-url", "v2v-upload", "lerobot-lighting")
SHA256 = re.compile(r"(?:sha256:)?([0-9a-f]{64})$")


class AcceptanceError(RuntimeError):
    """A stable, payload-free failure code suitable for a sanitized receipt."""


def check(condition: object, code: str) -> None:
    if not condition:
        raise AcceptanceError(code)


def now() -> str:
    return datetime.now(UTC).isoformat()


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def file_identity(path: Path) -> dict[str, Any]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
            size += len(chunk)
    return {"sha256": hasher.hexdigest(), "size_bytes": size}


def private_json(path: Path, schema: str | None = None) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        check(stat.S_ISREG(info.st_mode), "private_input_not_regular")
        check(stat.S_IMODE(info.st_mode) == 0o600, "private_input_requires_mode_0600")
        value = json.load(stream)
    check(isinstance(value, dict), "private_input_not_object")
    if schema is not None:
        check(value.get("schema") == schema, "private_input_schema_mismatch")
    return cast(dict[str, Any], value)


def write_private(path: Path, value: object, secrets: tuple[str, ...]) -> None:
    encoded = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    check(
        all(secret not in encoded for secret in secrets if secret),
        "receipt_contains_secret",
    )
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(encoded)


def write_private_text(path: Path, value: str, secrets: tuple[str, ...]) -> None:
    check(
        all(secret not in value for secret in secrets if secret),
        "receipt_contains_secret",
    )
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)


def _sha(value: object, code: str) -> str:
    match = SHA256.fullmatch(str(value))
    check(match is not None, code)
    assert match is not None
    return match.group(1)


def mp4_probe(path: Path) -> dict[str, Any]:
    """Decode enough of the file to reject header-only or unchanged outputs."""

    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    check(ffprobe is not None and ffmpeg is not None, "ffmpeg_tools_unavailable")
    assert ffprobe is not None and ffmpeg is not None
    completed = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=codec_name,width,height,avg_frame_rate,nb_read_frames,duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    raw = json.loads(completed.stdout)
    streams = raw.get("streams", [])
    check(isinstance(streams, list) and len(streams) == 1, "mp4_video_stream_missing")
    stream = streams[0]
    frames = int(stream.get("nb_read_frames", 0))
    rate = Fraction(stream.get("avg_frame_rate", "0/1"))
    check(stream.get("codec_name") in {"h264", "hevc", "av1"}, "mp4_codec_unsupported")
    check(
        int(stream.get("width", 0)) > 0 and int(stream.get("height", 0)) > 0,
        "mp4_dimensions_invalid",
    )
    check(frames > 0 and rate > 0, "mp4_timeline_invalid")
    decoded = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    check(bool(decoded), "mp4_decoded_pixels_empty")
    identity = file_identity(path)
    return {
        **identity,
        "codec": stream["codec_name"],
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "frames": frames,
        "fps": float(rate),
        "duration_seconds": float(stream.get("duration", frames / float(rate))),
        "decoded_rgb_sha256": hashlib.sha256(decoded).hexdigest(),
    }


def validate_mp4(
    input_path: Path, output_path: Path, declared: dict[str, Any]
) -> dict[str, Any]:
    source, output = mp4_probe(input_path), mp4_probe(output_path)
    check(
        output["sha256"] == _sha(declared.get("sha256"), "output_digest_invalid"),
        "download_digest_mismatch",
    )
    check(output["size_bytes"] == declared.get("size_bytes"), "download_size_mismatch")
    check(
        source["width"] == output["width"] and source["height"] == output["height"],
        "output_dimensions_changed",
    )
    check(source["frames"] == output["frames"], "output_frame_count_changed")
    check(
        source["decoded_rgb_sha256"] != output["decoded_rgb_sha256"],
        "output_decoded_pixels_unchanged",
    )
    return {"source": source, "output": output, "decoded_pixels_distinct": True}


def _payload(result: Any) -> dict[str, Any]:
    if getattr(result, "is_error", False):
        raise AcceptanceError("mcp_tool_result_failed")
    value = getattr(result, "structured_content", None)
    if isinstance(value, dict):
        return value
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise AcceptanceError("mcp_structured_result_missing")


def _tool_failure(result: Any) -> tuple[int, str | None]:
    """Extract a typed error only when a tool-error result actually carries one."""

    check(getattr(result, "is_error", False) is True, "invalid_local_path_was_accepted")
    candidates: list[dict[str, Any]] = []
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        candidates.append(structured)
    for item in getattr(result, "content", []):
        value = getattr(item, "text", None)
        if not isinstance(value, str):
            continue
        try:
            parsed = json.loads(value)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            candidates.append(parsed)
    for candidate in candidates:
        error = candidate.get("error", candidate)
        if isinstance(error, dict) and isinstance(error.get("code"), int):
            detail = error.get("data")
            semantic = detail.get("type") if isinstance(detail, dict) else None
            return int(error["code"]), semantic if isinstance(semantic, str) else None
    raise AcceptanceError("invalid_request_missing_jsonrpc_code")


def operation_id(value: dict[str, Any]) -> str:
    candidate = value.get("operation", value)
    check(isinstance(candidate, dict), "operation_envelope_invalid")
    raw = candidate.get("id", candidate.get("operation_id"))
    try:
        return str(UUID(str(raw)))
    except (TypeError, ValueError) as error:
        raise AcceptanceError("operation_id_invalid") from error


class PublicClient:
    def __init__(self, mcp: Client, http: httpx2.AsyncClient):
        self.mcp = mcp
        self.http = http

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return _payload(await self.mcp.call_tool(name, arguments))

    async def upload(
        self,
        model_id: str,
        path: Path,
        media_type: str,
        compression: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        identity = file_identity(path)
        response = await self.http.post(
            "/v1/scientific-artifacts/uploads",
            headers={"Idempotency-Key": idempotency_key},
            json={
                "model_id": model_id,
                **identity,
                "media_type": media_type,
                "compression": compression,
            },
        )
        check(response.status_code == 201, "artifact_upload_begin_failed")
        begun = response.json()
        op_id, upload_id = operation_id(begun), str(UUID(str(begun.get("upload_id"))))
        content_path = begun.get("content_path")
        check(
            isinstance(content_path, str)
            and content_path.startswith("/v1/scientific-artifacts/uploads/"),
            "artifact_content_path_invalid",
        )
        check(
            begun.get("max_content_bytes", 0) >= identity["size_bytes"],
            "artifact_exceeds_gateway_limit",
        )
        content = path.read_bytes()
        stored = await self.http.put(
            content_path,
            headers={"Content-Type": media_type, "Content-Length": str(len(content))},
            content=content,
        )
        check(stored.status_code == 200, "artifact_content_put_failed")
        receipt = stored.json()
        check(
            receipt.get("sha256") == identity["sha256"]
            and receipt.get("size_bytes") == len(content),
            "artifact_put_identity_mismatch",
        )
        finalized = await self.http.post(
            f"/v1/scientific-artifacts/uploads/{upload_id}:finalize",
            json={"operation_id": op_id},
        )
        check(finalized.status_code == 200, "artifact_finalize_failed")
        artifact = finalized.json()
        check(
            artifact.get("sha256") == identity["sha256"]
            and artifact.get("size_bytes") == identity["size_bytes"]
            and artifact.get("media_type") == media_type,
            "finalized_artifact_identity_mismatch",
        )
        return cast(dict[str, Any], artifact)

    async def wait(
        self, op_id: str, timeout_seconds: float, *, expected_status: str = "succeeded"
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        observed: list[dict[str, str]] = []
        while time.monotonic() < deadline:
            value = await self.call("get_operation", {"operation_id": op_id})
            status = value.get("status") or (value.get("operation") or {}).get("status")
            if not observed or observed[-1]["status"] != status:
                observed.append({"at": now(), "status": str(status)})
            if status in TERMINAL:
                check(status == expected_status, "operation_terminal_failure")
                return {"operation": value, "observed_statuses": observed}
            await asyncio.sleep(3)
        raise AcceptanceError("operation_timeout")

    async def download(
        self, tool: str, artifact: dict[str, Any], destination: Path
    ) -> None:
        artifact_id = str(artifact.get("artifact_id"))
        download = await self.call(tool, {"artifact_id": artifact_id})
        check(
            (download.get("artifact") or {}).get("artifact_id") == artifact_id,
            "download_artifact_identity_mismatch",
        )
        handle = download.get("handle")
        check(
            isinstance(handle, dict) and handle.get("method") == "GET",
            "download_handle_invalid",
        )
        assert isinstance(handle, dict)
        url = handle.get("url")
        check(isinstance(url, str), "download_url_invalid")
        assert isinstance(url, str)
        async with httpx2.AsyncClient(
            timeout=300, follow_redirects=False, trust_env=False
        ) as downloader:
            response = await downloader.get(
                urljoin(str(self.http.base_url), url), headers=handle.get("headers", {})
            )
        check(response.status_code == 200, "artifact_download_failed")
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(response.content)
        measured = file_identity(destination)
        check(
            measured["sha256"]
            == _sha(artifact.get("sha256"), "artifact_digest_invalid"),
            "artifact_download_digest_mismatch",
        )
        check(
            measured["size_bytes"] == artifact.get("size_bytes"),
            "artifact_download_size_mismatch",
        )


async def inventory(client: Client) -> tuple[dict[str, dict[str, Any]], str]:
    tools: dict[str, dict[str, Any]] = {}
    cursor = None
    for _ in range(16):
        page = (
            await client.list_tools(cursor=cursor)
            if cursor
            else await client.list_tools()
        )
        for item in page.tools:
            value = item.model_dump(mode="json", by_alias=True, exclude_none=True)
            check(value["name"] not in tools, "duplicate_tool_name")
            tools[value["name"]] = value
        cursor = getattr(page, "next_cursor", None)
        if not cursor:
            break
    check(not cursor, "tool_inventory_truncated")
    return tools, digest([tools[name] for name in sorted(tools)])


async def submit_v2v(
    public: PublicClient, input_reference: object, cohort: int, case: str
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    arguments = {
        "prompt": "Preserve motion and geometry; change only the lighting to warm late-afternoon side light.",
        "input_reference": input_reference,
        "negative_prompt": "changed motion, missing objects, temporal jitter",
        "seed": 20260915 + cohort,
        "num_inference_steps": 8,
        "guidance_scale": 5,
        "size": "256x256",
        "num_frames": 16,
        "fps": 8,
        "condition_video_keep": "first",
        "idempotency_key": f"cosmos-customer-{case}-c{cohort}-20260915",
        "wait_seconds": 0,
    }
    accepted = await public.call("cosmos3_nano_video_to_video", arguments)
    return operation_id(accepted), accepted, arguments


async def validate_v2v(
    public: PublicClient,
    op_id: str,
    input_path: Path,
    output: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    status = await public.wait(op_id, timeout_seconds)
    result = await public.call("get_operation_result", {"operation_id": op_id})
    envelope = result.get("result", result)
    check(
        envelope.get("schema") == "fs2-serve.nebius.ai/operation-artifact-result/v1",
        "v2v_result_schema_invalid",
    )
    check(envelope.get("content_type") == "video/mp4", "v2v_result_media_type_invalid")
    artifact = envelope.get("artifact")
    check(
        isinstance(artifact, dict) and artifact.get("media_type") == "video/mp4",
        "v2v_result_artifact_invalid",
    )
    await public.download("download_model_artifact", artifact, output)
    return {
        "operation_id": op_id,
        "terminal": status,
        "artifact": artifact,
        "validation": validate_mp4(input_path, output, artifact),
    }


def validate_lerobot(
    source_root: Path, archive: Path, result: dict[str, Any], destination: Path
) -> dict[str, Any]:
    from fs2_lerobot_augmentation.contracts import Selection  # type: ignore[import-not-found]
    from fs2_lerobot_augmentation.dataset import (  # type: ignore[import-not-found]
        extract_uploaded_bundle,
        open_and_validate,
        sha256_file,
    )

    variants = result.get("variants")
    check(
        result.get("status") == "succeeded"
        and isinstance(variants, list)
        and len(variants) == 1,
        "lerobot_result_invalid",
    )
    assert isinstance(variants, list)
    variant = variants[0]
    check(isinstance(variant, dict), "lerobot_variant_invalid")
    assert isinstance(variant, dict)
    artifact = variant.get("artifact")
    check(isinstance(artifact, dict), "lerobot_artifact_missing")
    assert isinstance(artifact, dict)
    extract_uploaded_bundle(
        archive,
        destination,
        expected_sha256=_sha(artifact.get("sha256"), "lerobot_digest_invalid"),
    )
    source = open_and_validate(
        source_root,
        repo_id="fs2/synthetic-cosmos3-lerobot",
        selection=Selection(episodes="all", cameras="all"),
    )
    produced = open_and_validate(
        destination,
        repo_id="fs2/synthetic-cosmos3-output",
        selection=Selection(episodes="all", cameras="all"),
    )
    check(source.frames == produced.frames == 16, "lerobot_frame_alignment_mismatch")
    check(source.action_shape == produced.action_shape, "lerobot_action_shape_mismatch")
    provenance = destination / "meta/fs2-augmentation-provenance.json"
    check(provenance.is_file(), "lerobot_provenance_missing")
    check(
        sha256_file(provenance) == variant.get("provenance_sha256"),
        "lerobot_provenance_digest_mismatch",
    )
    for index in range(source.frames):
        left, right = (
            source.dataset.get_raw_item(index),
            produced.dataset.get_raw_item(index),
        )
        check(
            float(left["timestamp"]) == float(right["timestamp"]),
            "lerobot_timestamp_changed",
        )
        check(
            (left["action"] == right["action"]).all(),
            "lerobot_preserved_action_changed",
        )
    return {
        "source_tree_sha256": source.tree_sha256,
        "output_tree_sha256": produced.tree_sha256,
        "frames": produced.frames,
        "decoded_video_frames": produced.decoded_video_frames,
        "action_shape": list(produced.action_shape),
        "provenance_sha256": variant["provenance_sha256"],
        "reader": "lerobot==0.6.1",
    }


async def submit_lerobot(
    public: PublicClient,
    bundle: Path,
    source_root: Path,
    request_template: dict[str, Any],
    cohort: int,
    output_dir: Path,
    timeout_seconds: float,
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    bundle_artifact = await public.upload(
        "cosmos3-lerobot-augmentation",
        bundle,
        "application/x-tar",
        "zstd",
        f"cosmos-lerobot-upload-c{cohort}-20260915",
    )
    input_manifest = {
        "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
        "manifest_id": f"cosmos-lerobot-input-c{cohort}-20260915",
        "entries": [
            {
                "name": "lerobot-dataset",
                "semantic_type": "lerobot-v3-bundle/v1",
                "artifact": bundle_artifact,
            }
        ],
    }
    input_manifest_path = output_dir / f"lerobot-input-manifest-c{cohort}.json"
    write_private(input_manifest_path, input_manifest, secrets)
    manifest_artifact = await public.upload(
        "cosmos3-lerobot-augmentation",
        input_manifest_path,
        "application/vnd.fs2.scientific-manifest+json",
        "none",
        f"cosmos-lerobot-manifest-c{cohort}-20260915",
    )
    request = json.loads(json.dumps(request_template))
    check(
        request.get("schema") == "fs2-serve.nebius.ai/scientific-run-request/v1"
        and request.get("operation") == "augment-lerobot-dataset"
        and isinstance(request.get("parameters"), dict),
        "lerobot_request_template_invalid",
    )
    parameters = request["parameters"]
    request["input_manifest"] = manifest_artifact
    parameters["source"] = {"kind": "uploaded-bundle", **bundle_artifact}
    parameters["variants"] = {"count": 1, "seeds": [20260915 + cohort]}
    parameters["augmentation"]["dimensions"] = [
        {
            "name": "lighting",
            "instruction": "warm late-afternoon side lighting while preserving motion and object identity",
            "strength": 0.7,
        }
    ]
    accepted = await public.call(
        "submit_cosmos3_lerobot_augmentation",
        {**request, "idempotency_key": f"cosmos-lerobot-run-c{cohort}-20260915"},
    )
    op_id = operation_id(accepted)
    terminal = await public.wait(op_id, timeout_seconds)
    published = await public.call("get_scientific_result", {"operation_id": op_id})
    check(
        published.get("schema") == "fs2-serve.nebius.ai/scientific-run-result/v1"
        and published.get("terminal_status") == "succeeded",
        "lerobot_scientific_result_invalid",
    )
    output_manifest = published.get("output_manifest")
    check(
        isinstance(output_manifest, dict)
        and output_manifest.get("media_type")
        == "application/vnd.fs2.scientific-manifest+json",
        "lerobot_output_manifest_missing",
    )
    inspected = await public.call(
        "inspect_scientific_artifact_manifest",
        {"artifact_id": output_manifest.get("artifact_id"), "limit": 16},
    )
    check(
        inspected.get("artifact") == output_manifest
        and inspected.get("entry_count") == 2
        and inspected.get("truncated") is False,
        "lerobot_output_manifest_invalid",
    )
    entries = inspected.get("entries")
    check(isinstance(entries, list), "lerobot_output_manifest_invalid")
    assert isinstance(entries, list)
    by_name = {entry.get("name"): entry for entry in entries if isinstance(entry, dict)}
    check(set(by_name) == {"result", "variant-00"}, "lerobot_output_entries_invalid")
    result_entry, variant_entry = by_name["result"], by_name["variant-00"]
    check(
        result_entry.get("semantic_type") == "lerobot-augmentation-result/v1"
        and variant_entry.get("semantic_type") == "lerobot-v3-augmented-bundle/v1",
        "lerobot_output_semantics_invalid",
    )
    result_path = output_dir / f"lerobot-result-c{cohort}.json"
    await public.download(
        "download_scientific_artifact", result_entry["artifact"], result_path
    )
    result = private_json(
        result_path, "fs2-serve.nebius.ai/cosmos3-lerobot-augmentation-result/v1"
    )
    variants = result.get("variants")
    check(isinstance(variants, list) and len(variants) == 1, "lerobot_variant_missing")
    assert isinstance(variants, list)
    check(
        isinstance(variants[0], dict)
        and variants[0].get("artifact") == variant_entry.get("artifact"),
        "lerobot_variant_manifest_identity_mismatch",
    )
    output = output_dir / f"lerobot-c{cohort}.tar.zst"
    await public.download(
        "download_scientific_artifact", variant_entry["artifact"], output
    )
    validation = validate_lerobot(
        source_root, output, result, output_dir / f"lerobot-c{cohort}"
    )
    return {
        "operation_id": op_id,
        "terminal": terminal,
        "scientific_result": published,
        "output_manifest": inspected,
        "result": result,
        "validation": validation,
    }


def _data(response: httpx2.Response, expected: int = 200) -> Any:
    check(response.status_code == expected, f"admin_http_{response.status_code}")
    value = response.json()
    check(isinstance(value, dict) and "data" in value, "admin_envelope_invalid")
    meta = value.get("meta")
    check(isinstance(meta, dict), "admin_meta_missing")
    assert isinstance(meta, dict)
    check(meta.get("warnings") == [], "admin_reported_warning")
    sources = meta.get("sources")
    check(isinstance(sources, list), "admin_sources_missing")
    assert isinstance(sources, list)
    check(
        all(
            isinstance(source, dict) and source.get("state") == "available"
            for source in sources
        ),
        "admin_source_not_available",
    )
    return value["data"]


def workload_state(containers: dict[str, Any]) -> str:
    """Classify only an observed ready worker or an observed zero-Pod App."""

    check(containers.get("state") == "available", "container_inventory_unavailable")
    items = containers.get("items")
    check(isinstance(items, list), "container_inventory_invalid")
    assert isinstance(items, list)
    if int(containers.get("total", 0)) == 0:
        return "scale-from-zero"
    if any(isinstance(item, dict) and item.get("ready") is True for item in items):
        return "hot"
    raise AcceptanceError("workload_neither_hot_nor_scale_from_zero")


def restart_counts(containers: dict[str, Any]) -> dict[str, int]:
    items = containers.get("items")
    check(isinstance(items, list), "container_inventory_invalid")
    counts: dict[str, int] = {}
    for item in items:
        check(isinstance(item, dict), "container_inventory_invalid")
        assert isinstance(item, dict)
        identifier, restarts = item.get("id"), item.get("restarts")
        check(
            isinstance(identifier, str)
            and isinstance(restarts, int)
            and not isinstance(restarts, bool)
            and restarts >= 0,
            "container_restart_count_invalid",
        )
        counts[identifier] = restarts
    return counts


def _runtime_digest(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.rsplit("@", 1)[-1]
    return (
        candidate if candidate.startswith("sha256:") and len(candidate) == 71 else None
    )


async def verify_live_release(
    admin: httpx2.AsyncClient, release_identity: dict[str, Any]
) -> dict[str, Any]:
    """Join the signed/attested deployment receipt to live admin projections."""

    required_images = {
        "control-plane",
        "cosmos3-nano",
        "cosmos3-lerobot-augmentation",
    }
    check(
        required_images <= set(release_identity["runtime_images"]),
        "release_runtime_image_identity_incomplete",
    )
    configuration = _data(await admin.get("/admin/api/v1/configuration"))
    etag = configuration.get("etag")
    check(
        isinstance(etag, str)
        and "sha256:" + etag.removeprefix("sha256:")
        == release_identity["configuration_sha256"],
        "live_configuration_identity_mismatch",
    )
    serving = _data(await admin.get("/admin/api/v1/models", params={"limit": 256}))
    serving_rows = [
        row.get("identity")
        for row in serving.get("items", [])
        if isinstance(row, dict)
        and isinstance(row.get("identity"), dict)
        and row["identity"].get("id") == "cosmos3-nano"
    ]
    check(len(serving_rows) == 1, "live_cosmos_identity_missing_or_ambiguous")
    cosmos = serving_rows[0]
    check(
        cosmos.get("model_revision") == release_identity["model_revision"]
        and _runtime_digest(cosmos.get("runtime_image_digest"))
        == release_identity["runtime_images"]["cosmos3-nano"],
        "live_cosmos_release_identity_mismatch",
    )
    scientific = _data(await admin.get("/admin/api/v1/scientific-models"))
    scientific_rows = [
        row
        for row in scientific.get("items", [])
        if isinstance(row, dict)
        and row.get("model_id") == "cosmos3-lerobot-augmentation"
    ]
    check(len(scientific_rows) == 1, "live_lerobot_identity_missing_or_ambiguous")
    lerobot = scientific_rows[0]
    backend = lerobot.get("backend")
    check(isinstance(backend, dict), "live_lerobot_backend_identity_missing")
    assert isinstance(backend, dict)
    check(
        backend.get("source_revision") == release_identity["model_revision"]
        and backend.get("model_revision") == release_identity["model_revision"]
        and _runtime_digest(backend.get("runtime_image_digest"))
        == release_identity["runtime_images"]["cosmos3-lerobot-augmentation"],
        "live_lerobot_release_identity_mismatch",
    )
    check(
        lerobot.get("readiness") == "qualified"
        and lerobot.get("qualification", {}).get("state") == "qualified",
        "live_lerobot_not_execution_qualified",
    )
    return {
        "configuration_sha256": release_identity["configuration_sha256"],
        "cosmos3_nano": {
            "model_revision": cosmos["model_revision"],
            "runtime_image_digest": _runtime_digest(cosmos["runtime_image_digest"]),
        },
        "cosmos3_lerobot_augmentation": {
            "model_revision": backend["model_revision"],
            "runtime_image_digest": _runtime_digest(backend["runtime_image_digest"]),
            "qualification": lerobot["qualification"]["state"],
        },
    }


async def _admin_observability_once(
    admin: httpx2.AsyncClient,
    apps: dict[str, str],
    expected_operations: dict[str, dict[str, str]],
    started_at: str,
    *,
    principal_id: str,
    tenant_id: str,
    baseline_restarts: dict[str, dict[str, int]],
) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    end = now()
    for model_id, app_id in apps.items():
        base = f"/admin/api/v1/apps/{app_id}"
        captured[model_id] = {}
        for surface in ("containers", "logs", "metrics", "usage", "runs"):
            params: dict[str, str | int] = (
                {}
                if surface == "containers"
                else {
                    "from": started_at,
                    "to": end,
                    "limit": 200,
                    **({"principal_id": principal_id} if surface == "runs" else {}),
                }
            )
            value = _data(await admin.get(f"{base}/{surface}", params=params))
            captured[model_id][surface] = value
        containers = captured[model_id]["containers"]
        check(containers.get("state") == "available", "admin_containers_unavailable")
        for identifier, count in restart_counts(containers).items():
            check(
                count <= baseline_restarts.get(model_id, {}).get(identifier, 0),
                "container_restarted_during_acceptance",
            )
        logs = captured[model_id]["logs"]
        log_items = logs.get("items")
        check(
            logs.get("state") == "available" and isinstance(log_items, list),
            "admin_logs_unavailable",
        )
        assert isinstance(log_items, list)
        relevant_logs = [
            item
            for item in log_items
            if isinstance(item, dict)
            and str(item.get("run_id")) in expected_operations[model_id]
        ]
        check(relevant_logs, "admin_correlated_logs_missing")
        check(
            not any(
                str(item.get("level", "")).lower()
                in {"warning", "warn", "error", "critical"}
                for item in log_items
                if isinstance(item, dict)
            ),
            "unexpected_app_warning_or_error_during_acceptance",
        )
        charts = captured[model_id]["metrics"].get("charts")
        check(
            isinstance(charts, list)
            and charts
            and all(
                isinstance(chart, dict) and chart.get("state") == "available"
                for chart in charts
            ),
            "admin_metrics_incomplete",
        )
        usage = captured[model_id]["usage"]
        observed = usage.get("observed_transport")
        check(
            isinstance(observed, dict)
            and int(observed.get("request_count", 0)) > 0
            and int(usage.get("logical_runs", 0)) >= len(expected_operations[model_id]),
            "admin_usage_incomplete",
        )
        listed = captured[model_id]["runs"].get("items")
        check(isinstance(listed, list), "admin_runs_invalid")
        listed_ids = {
            str(item.get("operation", {}).get("id"))
            for item in listed
            if isinstance(item, dict) and isinstance(item.get("operation"), dict)
        }
        check(
            listed_ids == set(expected_operations[model_id]),
            "admin_principal_operation_set_mismatch",
        )
    runs = {}
    for op_id, expected_status in {
        op_id: status
        for values in expected_operations.values()
        for op_id, status in values.items()
    }.items():
        matches = []
        for model_id, app_id in apps.items():
            response = await admin.get(f"/admin/api/v1/apps/{app_id}/runs/{op_id}")
            if response.status_code == 200:
                matches.append((model_id, _data(response)))
        check(len(matches) == 1, "admin_run_correlation_missing_or_ambiguous")
        detail = matches[0][1]
        operation = detail.get("operation")
        check(
            isinstance(operation, dict)
            and str(operation.get("id")) == op_id
            and operation.get("tenant_id") == tenant_id
            and operation.get("principal_id") == principal_id
            and operation.get("status") == expected_status
            and operation.get("semantic_outcome") == expected_status
            and operation.get("completed_at") is not None,
            "admin_run_identity_or_outcome_mismatch",
        )
        total = operation.get("timings", {}).get("total_seconds", {})
        check(total.get("value") is not None, "admin_run_total_time_missing")
        transport = detail.get("observed_transport")
        check(
            isinstance(transport, list)
            and transport
            and any(
                row.get("semantic_outcome") is not None
                for row in transport
                if isinstance(row, dict)
            ),
            "admin_run_transport_outcome_missing",
        )
        if matches[0][0] == "cosmos3-lerobot-augmentation":
            scientific = detail.get("scientific")
            check(
                isinstance(scientific, dict)
                and scientific.get("semantic_validation", {}).get("status") == "passed"
                and scientific.get("run", {})
                .get("gpu_accounting", {})
                .get("allocated", {})
                .get("evidence")
                != "unavailable"
                and scientific.get("run", {})
                .get("gpu_accounting", {})
                .get("active", {})
                .get("evidence")
                != "unavailable",
                "lerobot_admin_semantics_or_gpu_accounting_missing",
            )
        runs[op_id] = detail
    captured["operation_runs"] = runs
    return captured


async def admin_observability(
    admin: httpx2.AsyncClient,
    apps: dict[str, str],
    expected_operations: dict[str, dict[str, str]],
    started_at: str,
    *,
    principal_id: str,
    tenant_id: str,
    baseline_restarts: dict[str, dict[str, int]],
    timeout_seconds: float = 180,
) -> dict[str, Any]:
    """Wait for logs/metrics/history to converge, but never waive a missing surface."""

    deadline = time.monotonic() + timeout_seconds
    last_code = "admin_observability_incomplete"
    while time.monotonic() < deadline:
        try:
            return await _admin_observability_once(
                admin,
                apps,
                expected_operations,
                started_at,
                principal_id=principal_id,
                tenant_id=tenant_id,
                baseline_restarts=baseline_restarts,
            )
        except (AcceptanceError, httpx2.HTTPError, ValueError) as error:
            last_code = (
                str(error)
                if isinstance(error, AcceptanceError)
                else type(error).__name__
            )
            await asyncio.sleep(5)
    raise AcceptanceError(f"admin_observability_timeout:{last_code}")


def _debug_body_text(exchange: dict[str, Any], name: str) -> str:
    body = exchange.get(name)
    check(
        isinstance(body, dict) and body.get("encoding") == "utf-8",
        "debug_body_not_utf8",
    )
    assert isinstance(body, dict)
    value = body.get("data")
    check(
        isinstance(value, str) and body.get("complete") is True, "debug_body_incomplete"
    )
    assert isinstance(value, str)
    return value


async def invalid_request_observability(
    admin: httpx2.AsyncClient,
    app_id: str,
    *,
    started_at: str,
    marker: str,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """Correlate the exact negative call without copying its body into evidence."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        end = now()
        page = _data(
            await admin.get(
                f"/admin/api/v1/apps/{app_id}/requests",
                params={"from": started_at, "to": end, "limit": 200},
            )
        )
        candidates = [
            item
            for item in page.get("items", [])
            if item.get("mcp_tool") == "cosmos3_nano_video_to_video"
            and item.get("semantic_outcome") == "failed"
            and item.get("jsonrpc_error_code") == -32602
            and item.get("admission_stage") == "pre_admission"
        ]
        matches: list[dict[str, Any]] = []
        for item in candidates:
            detail = _data(
                await admin.get(f"/admin/api/v1/apps/{app_id}/requests/{item['id']}")
            )
            if marker in _debug_body_text(detail, "request_body"):
                matches.append(detail)
        if matches:
            check(len(matches) == 1, "invalid_request_debug_correlation_ambiguous")
            match = matches[0]
            response = json.loads(_debug_body_text(match, "response_body"))
            error = response.get("error")
            check(
                isinstance(error, dict) and error.get("code") == -32602,
                "invalid_debug_response_wrong_code",
            )
            check(match.get("operation_id") is None, "invalid_request_was_admitted")
            return {
                key: match.get(key)
                for key in (
                    "id",
                    "request_id",
                    "operation_id",
                    "model_id",
                    "mcp_tool",
                    "http_status",
                    "semantic_outcome",
                    "jsonrpc_error_code",
                    "semantic_error_type",
                    "admission_stage",
                )
            }
        await asyncio.sleep(1)
    raise AcceptanceError("invalid_request_debug_capture_missing")


def client_path(
    release_identity: dict[str, Any],
    receipt: dict[str, Any] | None,
    expected_operations: dict[str, str],
) -> str:
    if receipt is None:
        return "mcp-sdk"
    check(
        set(receipt)
        == {"schema", "client_build_sha256", "public_endpoint", "operations"},
        "librechat_receipt_fields",
    )
    check(
        receipt.get("schema") == "fs2-serve.nebius.ai/librechat-mcp-acceptance/v1",
        "librechat_receipt_schema",
    )
    check(
        receipt.get("client_build_sha256") == release_identity["client_build_sha256"],
        "librechat_build_mismatch",
    )
    check(
        receipt.get("public_endpoint", "").rstrip("/")
        == release_identity["public_endpoint"].rstrip("/"),
        "librechat_endpoint_mismatch",
    )
    checks = receipt.get("operations")
    check(isinstance(checks, list), "librechat_operations_missing")
    assert isinstance(checks, list)
    observed: dict[str, str] = {}
    for item in checks:
        check(
            isinstance(item, dict)
            and set(item) == {"scenario_id", "operation_id", "outcome"}
            and item.get("outcome") == "passed",
            "librechat_operation_receipt_invalid",
        )
        assert isinstance(item, dict)
        operation = str(item.get("operation_id"))
        try:
            UUID(operation)
        except (TypeError, ValueError, AttributeError):
            raise AcceptanceError("librechat_operation_identity_invalid") from None
        check(operation not in observed, "librechat_operation_receipt_duplicate")
        scenario = item.get("scenario_id")
        check(
            isinstance(scenario, str) and scenario in {*SCENARIOS, "cancellation"},
            "librechat_scenario_identity_invalid",
        )
        assert isinstance(scenario, str)
        observed[operation] = scenario
    check(
        observed == expected_operations,
        "librechat_did_not_exercise_exact_operations",
    )
    return "librechat+mcp"


def evidence(
    manifest: dict[str, Any],
    receipt: dict[str, Any],
    selected_client_path: str,
) -> list[dict[str, Any]]:
    mapping = {
        "v2v-url": (
            "cosmos3-nano",
            "video-to-video",
            "https-mp4-url",
            "mp4",
            receipt["fixtures"]["url_mp4"],
        ),
        "v2v-upload": (
            "cosmos3-nano",
            "video-to-video",
            "client-local-mp4-upload",
            "mp4",
            receipt["fixtures"]["local_mp4"],
        ),
        "lerobot-lighting": (
            "cosmos3-lerobot-augmentation",
            "lerobot-augmentation",
            "lerobot-v3-upload",
            "lerobot-v3",
            receipt["fixtures"]["lerobot"],
        ),
    }
    results = []
    for scenario_id, (
        app_id,
        capability,
        input_form,
        output_form,
        fixture,
    ) in mapping.items():
        if manifest["app_id"] != app_id:
            continue
        rows = [
            row
            for cohort in receipt["cohorts"]
            for row in cohort["cases"]
            if row["scenario_id"] == scenario_id
        ]
        passed = len(rows) == 2 and all(row.get("outcome") == "passed" for row in rows)
        results.append(
            {
                "schema": gate.EVIDENCE_SCHEMA,
                "evidence_id": f"cosmos-20260915-{scenario_id}",
                "app_id": app_id,
                "capability_id": capability,
                "scenario_id": scenario_id,
                "operation": (
                    "augment-lerobot-dataset"
                    if scenario_id == "lerobot-lighting"
                    else "video-to-video"
                ),
                "input_form": input_form,
                "output_form": output_form,
                "client_path": selected_client_path,
                "observed_at": receipt["completed_at"],
                "release_identity": manifest["release_identity"],
                "fixture_sha256": "sha256:" + fixture["sha256"],
                "outcome": "passed" if passed else "failed",
                "clean_cohorts": 2 if passed else 0,
                "public_endpoint_exercised": True,
                "direct_runtime_only": False,
                "terminal_outcome_validated": passed,
                "artifact_validated": passed,
                "tenant_policy_exercised": receipt["tenant_policy_verified"],
                "workload_states": sorted(
                    {
                        state
                        for row in rows
                        for state in row.get("workload_states", [])
                        if isinstance(state, str)
                    }
                ),
                "integrations": ["robotics-tenant"],
                "observed_surfaces": sorted(receipt["observed_surfaces"]),
                "notes": (
                    []
                    if selected_client_path == "librechat+mcp"
                    else ["raw MCP SDK only; LibreChat client receipt absent"]
                ),
            }
        )
    return results


def human_verdict(
    verdicts: list[dict[str, Any]], *, requested_scope_qualified: bool
) -> str:
    def cell(value: object) -> str:
        return str(value).replace("|", "\\|")

    lines = [
        "# Cosmos customer capability verdict",
        "",
        "Exact requested workflow acceptance: "
        + ("QUALIFIED" if requested_scope_qualified else "NOT QUALIFIED"),
        "",
        "A broad App claim remains controlled independently by every advertised capability.",
        "",
        "| App | Capability | Claim | State | Scenario evidence |",
        "| --- | --- | --- | --- | --- |",
    ]
    for verdict in verdicts:
        for capability in verdict["capabilities"]:
            scenarios = cell(
                ", ".join(
                    f"{scenario['scenario_id']}={scenario['state']}"
                    + (
                        f" ({'; '.join(scenario['reasons'])})"
                        if scenario["reasons"]
                        else ""
                    )
                    for scenario in capability["scenarios"]
                )
            )
            claim = (
                "advertised"
                if capability["advertised"]
                else "requested"
                if capability["requested"]
                else "withdrawn / unsupported"
            )
            lines.append(
                f"| {cell(verdict['app_id'])} | {cell(capability['capability_id'])} | "
                f"{claim} | {capability['state']} | {scenarios} |"
            )
    lines.extend(
        [
            "",
            "Generated from the machine-evaluated verdicts; HTTP success alone is not evidence.",
            "",
        ]
    )
    return "\n".join(lines)


async def execute(args: argparse.Namespace) -> int:
    release_identity = gate.validate_identity(
        json.loads(args.release_identity.read_text(encoding="utf-8"))
    )
    manifests = manifest_builder.manifests(release_identity)
    deployment = json.loads(args.deployment_receipt.read_text(encoding="utf-8"))
    check(
        deployment.get("schema")
        == "fs2-serve.nebius.ai/customer-deployment-identity/v1",
        "deployment_receipt_schema",
    )
    check(
        deployment.get("release_identity") == release_identity,
        "deployed_release_identity_mismatch",
    )
    origin = release_identity["public_endpoint"].removesuffix("/mcp").rstrip("/")
    check(urlsplit(origin).scheme == "https", "public_origin_not_https")
    key = private_json(args.key_file, "fs2-customer-key/v1")
    access = private_json(args.admin_access)
    token = key.get("secret")
    admin_token = access.get("admin_bootstrap_token") or access.get(
        "credentials", {}
    ).get("admin_bootstrap_token")
    check(isinstance(token, str) and isinstance(admin_token, str), "credential_missing")
    assert isinstance(token, str) and isinstance(admin_token, str)
    secrets: tuple[str, ...] = (token, admin_token)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    started = now()
    receipt: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/cosmos3-customer-acceptance/v1",
        "outcome": "failed",
        "started_at": started,
        "release_identity": release_identity,
        "fixtures": {
            "local_mp4": file_identity(args.mp4_file),
            "lerobot": file_identity(args.lerobot_bundle),
        },
        "cohorts": [],
        "workload_states": [],
        "observed_surfaces": [],
        "tenant_policy_verified": False,
    }
    try:
        async with AsyncExitStack() as stack:
            public_http = await stack.enter_async_context(
                httpx2.AsyncClient(
                    base_url=origin,
                    headers={"authorization": "Bearer " + token, "origin": origin},
                    timeout=120,
                    follow_redirects=False,
                    trust_env=False,
                )
            )
            admin = await stack.enter_async_context(
                httpx2.AsyncClient(
                    base_url=origin,
                    headers={"origin": origin},
                    timeout=120,
                    trust_env=False,
                )
            )
            login = await admin.post(
                "/admin/api/v1/session",
                headers={"authorization": "Bearer " + admin_token},
            )
            check(login.status_code == 200, "admin_authentication_failed")
            stack.push_async_callback(admin.delete, "/admin/api/v1/session")
            stream = streamable_http_client(origin + "/mcp", http_client=public_http)
            mcp = await stack.enter_async_context(
                Client(stream, mode=MCP_PROTOCOL_VERSION)
            )
            public = PublicClient(mcp, public_http)
            tools, catalog_digest = await inventory(mcp)
            check(
                catalog_digest == release_identity["public_tool_catalog_sha256"],
                "public_tool_catalog_drift",
            )
            required_tools = {
                "cosmos3_nano_video_to_video",
                "submit_cosmos3_lerobot_augmentation",
                "get_operation",
                "get_operation_result",
                "get_scientific_result",
                "inspect_scientific_artifact_manifest",
                "download_model_artifact",
                "download_scientific_artifact",
            }
            check(required_tools <= set(tools), "required_public_tools_missing")
            receipt["live_release_identity"] = await verify_live_release(
                admin, release_identity
            )
            apps_page = _data(
                await admin.get("/admin/api/v1/apps", params={"limit": 1000})
            )
            app_rows = apps_page.get("items", [])
            apps = {
                model: row["app_id"]
                for model in ("cosmos3-nano", "cosmos3-lerobot-augmentation")
                for row in app_rows
                if row.get("public_model_id") == model
            }
            check(
                set(apps) == {"cosmos3-nano", "cosmos3-lerobot-augmentation"},
                "customer_apps_missing",
            )
            user_id = key.get("user_id")
            check(isinstance(user_id, str), "canary_user_id_missing")
            keys = _data(await admin.get(f"/admin/api/v1/users/{user_id}/keys")).get(
                "items", []
            )
            key_row = next(
                (row for row in keys if row.get("id") == key.get("key_id")), None
            )
            check(isinstance(key_row, dict), "canary_key_not_found")
            assert isinstance(key_row, dict)
            policy = {
                field: key_row.get(field)
                for field in (
                    "tenant_id",
                    "principal_id",
                    "models",
                    "scopes",
                    "state",
                    "max_concurrency",
                    "request_budget",
                    "gpu_seconds_budget",
                    "rate_limit_requests",
                    "rate_window_seconds",
                    "expires_at",
                )
            }
            check(policy["tenant_id"] == "robotics", "canary_not_robotics_tenant")
            check(
                set(policy["models"] or [])
                == {"cosmos3-nano", "cosmos3-lerobot-augmentation"},
                "canary_not_cosmos_only",
            )
            check(
                set(policy["scopes"] or [])
                == {
                    "artifacts.write",
                    "catalog.read",
                    "inference.invoke",
                    "mcp.invoke",
                    "operations.cancel",
                    "operations.read",
                    "operations.result",
                },
                "canary_scope_mismatch",
            )
            check(
                isinstance(policy["principal_id"], str)
                and policy["state"] == "active"
                and int(policy["max_concurrency"] or 0) >= 2
                and policy["expires_at"] is not None,
                "canary_not_disposable_or_concurrent",
            )
            check(
                digest(policy) == release_identity["tenant_policy_sha256"],
                "tenant_policy_identity_mismatch",
            )
            receipt["tenant_policy_verified"] = True
            baseline_restarts = {}
            receipt["initial_workload_states"] = {}
            for model_id, app_id in apps.items():
                initial = _data(
                    await admin.get(f"/admin/api/v1/apps/{app_id}/containers")
                )
                check(isinstance(initial, dict), "container_inventory_invalid")
                assert isinstance(initial, dict)
                receipt["initial_workload_states"][model_id] = workload_state(initial)
                baseline_restarts[model_id] = restart_counts(initial)

            url_input = args.output / "url-input.mp4"
            async with httpx2.AsyncClient(
                timeout=120, follow_redirects=True, trust_env=False
            ) as fixture_http:
                async with fixture_http.stream("GET", args.mp4_url) as url_response:
                    check(url_response.status_code == 200, "url_fixture_unavailable")
                    observed_bytes = 0
                    descriptor = os.open(
                        url_input, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                    )
                    with os.fdopen(descriptor, "wb") as stream_out:
                        async for chunk in url_response.aiter_bytes():
                            observed_bytes += len(chunk)
                            check(
                                observed_bytes <= 64 * 1024 * 1024,
                                "url_fixture_too_large",
                            )
                            stream_out.write(chunk)
                    check(observed_bytes > 0, "url_fixture_unavailable")
            mp4_probe(url_input)
            receipt["fixtures"]["url_mp4"] = file_identity(url_input)
            template = json.loads(args.lerobot_request.read_text(encoding="utf-8"))

            for cohort in (1, 2):
                cohort_row: dict[str, Any] = {
                    "cohort": cohort,
                    "started_at": now(),
                    "cases": [],
                }
                starting_states = {}
                for model_id, app_id in apps.items():
                    current = _data(
                        await admin.get(f"/admin/api/v1/apps/{app_id}/containers")
                    )
                    check(isinstance(current, dict), "container_inventory_invalid")
                    assert isinstance(current, dict)
                    starting_states[model_id] = workload_state(current)
                cohort_row["starting_workload_states"] = starting_states
                local_artifact = await public.upload(
                    "cosmos3-nano",
                    args.mp4_file,
                    "video/mp4",
                    "none",
                    f"cosmos-v2v-local-upload-c{cohort}-20260915",
                )
                url_submission, upload_submission = await asyncio.gather(
                    submit_v2v(public, args.mp4_url, cohort, "url"),
                    submit_v2v(public, local_artifact, cohort, "upload"),
                )
                op_url, accepted_url, args_url = url_submission
                op_upload, _accepted_upload, _args_upload = upload_submission
                check(op_url != op_upload, "concurrent_requests_collapsed")
                during_burst = await public.call("list_models", {})
                check(
                    "cosmos3-nano"
                    in {item.get("id") for item in during_burst.get("data", [])},
                    "platform_unavailable_during_burst",
                )
                replay = await public.call("cosmos3_nano_video_to_video", args_url)
                check(
                    operation_id(replay) == op_url,
                    "idempotent_retry_created_second_operation",
                )
                results = await asyncio.gather(
                    validate_v2v(
                        public,
                        op_url,
                        url_input,
                        args.output / f"v2v-url-c{cohort}.mp4",
                        args.timeout_seconds,
                    ),
                    validate_v2v(
                        public,
                        op_upload,
                        args.mp4_file,
                        args.output / f"v2v-upload-c{cohort}.mp4",
                        args.timeout_seconds,
                    ),
                )
                cohort_row["cases"].extend(
                    [
                        {
                            "scenario_id": "v2v-url",
                            "outcome": "passed",
                            "workload_states": [starting_states["cosmos3-nano"]],
                            **results[0],
                        },
                        {
                            "scenario_id": "v2v-upload",
                            "outcome": "passed",
                            "workload_states": [starting_states["cosmos3-nano"]],
                            **results[1],
                        },
                    ]
                )
                lerobot = await submit_lerobot(
                    public,
                    args.lerobot_bundle,
                    args.lerobot_source,
                    template,
                    cohort,
                    args.output,
                    args.timeout_seconds,
                    secrets,
                )
                cohort_row["cases"].append(
                    {
                        "scenario_id": "lerobot-lighting",
                        "outcome": "passed",
                        "workload_states": [
                            starting_states["cosmos3-lerobot-augmentation"]
                        ],
                        **lerobot,
                    }
                )
                cohort_row["concurrent_operation_ids"] = [op_url, op_upload]
                cohort_row["idempotent_replay_operation_id"] = operation_id(replay)
                cohort_row["platform_available_during_burst"] = True
                cohort_row["completed_at"] = now()
                receipt["cohorts"].append(cohort_row)
                live_containers = _data(
                    await admin.get(
                        f"/admin/api/v1/apps/{apps['cosmos3-nano']}/containers"
                    )
                )
                check(isinstance(live_containers, dict), "container_inventory_invalid")
                assert isinstance(live_containers, dict)
                if workload_state(live_containers) == "hot":
                    receipt["workload_states"].append("hot")
                check(accepted_url is not None, "accepted_response_missing")

            cancel_id, _cancel_accepted, _cancel_arguments = await submit_v2v(
                public, args.mp4_url, 99, "cancel"
            )
            cancel_result = await public.call(
                "cancel_operation", {"operation_id": cancel_id}
            )
            check(
                operation_id(cancel_result) == cancel_id,
                "cancel_operation_identity_mismatch",
            )
            receipt["cancellation"] = await public.wait(
                cancel_id, min(args.timeout_seconds, 600), expected_status="cancelled"
            )

            invalid_before = now()
            invalid_marker = "cosmos-invalid-local-path-20260915"
            invalid_code: int
            invalid_type: str | None
            try:
                invalid_result = await mcp.call_tool(
                    "cosmos3_nano_video_to_video",
                    {
                        "prompt": "invalid local path must be rejected before admission",
                        "input_reference": "customer.mp4",
                        "idempotency_key": invalid_marker,
                        "wait_seconds": 0,
                    },
                )
            except MCPError as error:
                invalid_code = error.code
                invalid_type = (
                    (error.data or {}).get("type")
                    if isinstance(error.data, dict)
                    else None
                )
            else:
                invalid_code, invalid_type = _tool_failure(invalid_result)
            check(invalid_code == -32602, "invalid_request_wrong_jsonrpc_code")
            receipt["invalid_request"] = {
                "at": invalid_before,
                "jsonrpc_error_code": invalid_code,
                "semantic_error_type": invalid_type,
                "admission_stage": "pre_admission",
            }
            receipt["invalid_request"][
                "admin_debug"
            ] = await invalid_request_observability(
                admin,
                apps["cosmos3-nano"],
                started_at=invalid_before,
                marker=invalid_marker,
            )

            expected_operations = {
                "cosmos3-nano": {
                    case["operation_id"]: "succeeded"
                    for cohort in receipt["cohorts"]
                    for case in cohort["cases"]
                    if case["scenario_id"] in {"v2v-url", "v2v-upload"}
                }
                | {cancel_id: "cancelled"},
                "cosmos3-lerobot-augmentation": {
                    case["operation_id"]: "succeeded"
                    for cohort in receipt["cohorts"]
                    for case in cohort["cases"]
                    if case["scenario_id"] == "lerobot-lighting"
                },
            }
            client_operations = {
                str(case["operation_id"]): str(case["scenario_id"])
                for cohort in receipt["cohorts"]
                for case in cohort["cases"]
            }
            client_operations[cancel_id] = "cancellation"
            receipt["admin"] = await admin_observability(
                admin,
                apps,
                expected_operations,
                started,
                principal_id=str(policy["principal_id"]),
                tenant_id="robotics",
                baseline_restarts=baseline_restarts,
            )
            receipt["observed_surfaces"] = [
                "containers",
                "logs",
                "metrics",
                "runs",
                "usage",
            ]
            receipt["workload_states"] = sorted(
                {
                    state
                    for cohort in receipt["cohorts"]
                    for case in cohort["cases"]
                    for state in case["workload_states"]
                }
                | set(receipt["workload_states"])
            )
            check(
                set(receipt["workload_states"]) == {"hot", "scale-from-zero"},
                "hot_and_scale_from_zero_not_both_observed",
            )
            observed_states = {
                observation["status"]
                for cohort in receipt["cohorts"]
                for case in cohort["cases"]
                for observation in case["terminal"]["observed_statuses"]
            }
            check("queued" in observed_states, "queueing_not_observed")
            client_receipt = (
                private_json(args.librechat_receipt) if args.librechat_receipt else None
            )
            selected_path = client_path(
                release_identity, client_receipt, client_operations
            )
            receipt["client_path"] = selected_path
            receipt["completed_at"] = now()
            generated = {
                app_id: evidence(manifest, receipt, selected_path)
                for app_id, manifest in manifests.items()
            }
            verdicts = [
                gate.evaluate(manifests[app_id], generated[app_id])
                for app_id in sorted(manifests)
            ]
            receipt["capability_verdicts"] = verdicts
            # The requested cross-App workflow may qualify while the broader
            # Cosmos App stays not-ready due to other advertised, untested modes.
            requested = {
                row["capability_id"]: row["state"]
                for verdict in verdicts
                for row in verdict["capabilities"]
                if row["capability_id"] in {"video-to-video", "lerobot-augmentation"}
            }
            check(
                requested
                == {"video-to-video": "qualified", "lerobot-augmentation": "qualified"},
                "requested_capabilities_not_qualified",
            )
            receipt["requested_scope_qualified"] = True
            receipt["outcome"] = "passed"
            for app_id, manifest in manifests.items():
                write_private(
                    args.output / f"capability-manifest-{app_id}.json",
                    manifest,
                    secrets,
                )
                for item in generated[app_id]:
                    write_private(
                        args.output / f"evidence-{item['scenario_id']}.json",
                        item,
                        secrets,
                    )
            verdict_index = {
                "schema": "fs2-serve.nebius.ai/customer-capability-verdict-index/v1",
                "verdicts": verdicts,
            }
            write_private(args.output / "verdict-index.json", verdict_index, secrets)
            write_private_text(
                args.output / "VERDICT.md",
                human_verdict(verdicts, requested_scope_qualified=True),
                secrets,
            )
    except Exception as error:
        receipt["error_type"] = type(error).__name__
        receipt["error_code"] = (
            str(error) if isinstance(error, AcceptanceError) else None
        )
    receipt["completed_at"] = receipt.get("completed_at", now())
    write_private(args.output / "receipt.json", receipt, secrets)
    print(
        json.dumps(
            {
                key: receipt.get(key)
                for key in (
                    "outcome",
                    "error_type",
                    "error_code",
                    "started_at",
                    "completed_at",
                )
            }
        ),
        flush=True,
    )
    return 0 if receipt["outcome"] == "passed" else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-identity", required=True, type=Path)
    parser.add_argument("--deployment-receipt", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--admin-access", required=True, type=Path)
    parser.add_argument("--mp4-file", required=True, type=Path)
    parser.add_argument("--mp4-url", required=True)
    parser.add_argument("--lerobot-source", required=True, type=Path)
    parser.add_argument("--lerobot-bundle", required=True, type=Path)
    parser.add_argument("--lerobot-request", required=True, type=Path)
    parser.add_argument("--librechat-receipt", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    check(args.execute, "live_execution_requires_execute")
    check(300 <= args.timeout_seconds <= 7200, "timeout_outside_acceptance_bound")
    check(urlsplit(args.mp4_url).scheme == "https", "mp4_url_must_be_https")
    check(not args.output.exists(), "output_must_not_exist")
    check(
        not args.output.resolve().is_relative_to(INFERENCE_ROOT.parent),
        "output_must_be_private_outside_repository",
    )
    os.umask(0o077)
    return asyncio.run(execute(args))


if __name__ == "__main__":
    raise SystemExit(main())

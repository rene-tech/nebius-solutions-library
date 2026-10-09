"""Run public file-to-result and typed-MCP acceptance for the visual-science Apps."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import stat
import struct
import time
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import httpx
import httpx2
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client


MCP_VERSION = "2026-07-28"
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "expired", "preempted"})
RESULT_SCHEMA = "fs2-serve.nebius.ai/operation-artifact-result/v1"


class CheckError(RuntimeError):
    pass


def check(condition: object, code: str) -> None:
    if not condition:
        raise CheckError(code)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def json_digest(value: object) -> str:
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def read_token(path: Path) -> str:
    check(path.is_absolute(), "token_path_not_absolute")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        check(stat.S_ISREG(metadata.st_mode), "token_not_regular")
        check(stat.S_IMODE(metadata.st_mode) == 0o600, "token_mode_invalid")
        check(metadata.st_uid == os.geteuid(), "token_owner_invalid")
        check(32 <= metadata.st_size <= 4096, "token_size_invalid")
        raw = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    check(len(raw) <= 4096 and b"\n" not in raw, "token_content_invalid")
    return raw.decode("ascii")


def validate_origin(value: str) -> str:
    parsed = urlsplit(value)
    check(
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.path in {"", "/"}
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.username is None
        and parsed.password is None,
        "origin_invalid",
    )
    return f"https://{parsed.netloc}"


def data(result: Any) -> dict[str, Any]:
    check(getattr(result, "is_error", True) is False, "mcp_tool_error")
    content = getattr(result, "structured_content", None)
    check(isinstance(content, dict), "mcp_result_invalid")
    return content


def png_dimensions(content: bytes) -> tuple[int, int]:
    check(
        content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 24, "png_invalid"
    )
    return struct.unpack(">II", content[16:24])


async def list_tools(client: Client) -> dict[str, Any]:
    tools: dict[str, Any] = {}
    cursor: str | None = None
    for _ in range(32):
        page = (
            await client.list_tools(cursor=cursor)
            if cursor
            else await client.list_tools()
        )
        for tool in page.tools:
            check(tool.name not in tools, "duplicate_tool")
            tools[tool.name] = tool
        cursor = getattr(page, "next_cursor", None)
        if not cursor:
            break
    check(not cursor, "tool_inventory_truncated")
    return tools


async def upload(
    http: httpx.AsyncClient, model_id: str, path: Path, media_type: str, key: str
) -> dict[str, Any]:
    content = path.read_bytes()
    reserved = await http.post(
        "/v1/scientific-artifacts/uploads",
        json={
            "model_id": model_id,
            "sha256": digest(content),
            "size_bytes": len(content),
            "media_type": media_type,
            "compression": "none",
        },
        headers={"idempotency-key": key},
    )
    check(reserved.status_code == 201, "artifact_reservation_failed")
    reservation = reserved.json()
    check(
        reservation["max_content_bytes"] >= len(content),
        "artifact_inline_ceiling_too_small",
    )
    written = await http.put(
        reservation["content_path"],
        content=content,
        headers={"content-type": media_type, "content-length": str(len(content))},
    )
    check(written.status_code == 200, "artifact_write_failed")
    finalized = await http.post(
        f"/v1/scientific-artifacts/uploads/{reservation['upload_id']}:finalize",
        json={"operation_id": reservation["operation_id"]},
    )
    check(finalized.status_code == 200, "artifact_finalize_failed")
    reference = finalized.json()
    check(
        reference["sha256"] == digest(content)
        and reference["size_bytes"] == len(content)
        and reference["media_type"] == media_type,
        "artifact_reference_mismatch",
    )
    return reference


async def poll(
    client: Client, operation_id: str, timeout: float
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    deadline = time.monotonic() + timeout
    states: list[dict[str, str]] = []
    while time.monotonic() < deadline:
        operation = data(
            await client.call_tool("get_operation", {"operation_id": operation_id})
        )
        if not states or states[-1]["status"] != operation["status"]:
            states.append({"at": now(), "status": operation["status"]})
        if operation["status"] in TERMINAL:
            return operation, states
        await asyncio.sleep(1)
    raise CheckError("operation_timeout")


async def result_bytes(
    client: Client, http: httpx.AsyncClient, operation_id: str
) -> tuple[dict[str, Any], bytes]:
    returned = data(
        await client.call_tool("get_operation_result", {"operation_id": operation_id})
    )
    check(returned["operation"]["id"] == operation_id, "result_operation_mismatch")
    result = returned["result"]
    check(result.get("schema") == RESULT_SCHEMA, "result_not_externalized")
    artifact = result["artifact"]
    response = await http.get(f"/v1/artifacts/{artifact['artifact_id']}/content")
    check(response.status_code == 200, "result_artifact_download_failed")
    check(
        digest(response.content) == artifact["sha256"]
        and len(response.content) == artifact["size_bytes"],
        "result_artifact_digest_mismatch",
    )
    return result, response.content


def validate_cellpose(
    content: bytes, expected_input: str, output: Path, stem: str
) -> dict[str, Any]:
    document = json.loads(content)
    check(document["input_sha256"] == expected_input, "cellpose_input_digest_mismatch")
    check(document["object_count"] > 0, "cellpose_empty_result")
    check(
        document["research_only"] is True and document["commercial_use"] is False,
        "cellpose_policy_mismatch",
    )
    mask = base64.b64decode(document["mask_base64"], validate=True)
    overlay = base64.b64decode(document["overlay_base64"], validate=True)
    mask_dimensions = png_dimensions(mask)
    overlay_dimensions = png_dimensions(overlay)
    check(
        mask_dimensions
        == overlay_dimensions
        == tuple(reversed(document["image_shape"][:2])),
        "cellpose_shape_mismatch",
    )
    (output / f"{stem}-mask.png").write_bytes(mask)
    (output / f"{stem}-overlay.png").write_bytes(overlay)
    return {
        "input_sha256": document["input_sha256"],
        "model_sha256": document["model_sha256"],
        "object_count": document["object_count"],
        "image_shape": document["image_shape"],
        "mask_sha256": digest(mask),
        "overlay_sha256": digest(overlay),
        "overlay_bytes": len(overlay),
    }


def validate_scvi(
    content: bytes, expected_input: str, expected_method: str, output: Path, stem: str
) -> dict[str, Any]:
    with zipfile.ZipFile(BytesIO(content)) as archive:
        names = archive.namelist()
        required = {
            "manifest.json",
            "integrated.h5ad",
            "latent_embeddings.csv",
            "umap_embeddings.csv",
            "preview.png",
        }
        check(
            len(names) == len(set(names)) and required <= set(names),
            "scvi_bundle_incomplete",
        )
        check(archive.testzip() is None, "scvi_bundle_corrupt")
        check(
            any(name.startswith("model/") and not name.endswith("/") for name in names),
            "scvi_model_missing",
        )
        manifest = json.loads(archive.read("manifest.json"))
        preview = archive.read("preview.png")
    check(manifest["input_sha256"] == expected_input, "scvi_input_digest_mismatch")
    check(manifest["method"] == expected_method, "scvi_method_mismatch")
    check(
        manifest["cells"] > 0
        and manifest["genes"] > 0
        and manifest["latent_dimensions"] == 4,
        "scvi_shape_invalid",
    )
    check(
        manifest["research_only"] is True and manifest["clinical_use"] is False,
        "scvi_policy_mismatch",
    )
    png_dimensions(preview)
    zip_path = output / f"{stem}-{expected_method}.zip"
    preview_path = output / f"{stem}-{expected_method}-preview.png"
    zip_path.write_bytes(content)
    preview_path.write_bytes(preview)
    return {
        "input_sha256": manifest["input_sha256"],
        "method": manifest["method"],
        "cells": manifest["cells"],
        "genes": manifest["genes"],
        "latent_dimensions": manifest["latent_dimensions"],
        "gpu": manifest["gpu"],
        "bundle_sha256": digest(content),
        "bundle_bytes": len(content),
        "preview_sha256": digest(preview),
    }


async def invoke_case(
    client: Client,
    http: httpx.AsyncClient,
    *,
    cohort: int,
    model: str,
    tool: str,
    fixture: Path,
    media_type: str,
    output: Path,
    timeout: float,
) -> dict[str, Any]:
    content = fixture.read_bytes()
    fixture_sha = digest(content)
    artifact = await upload(
        http,
        model,
        fixture,
        media_type,
        f"visual-upload-c{cohort}-{model}-{fixture_sha[:24]}",
    )
    key = f"visual-public-c{cohort}-{model}-{fixture_sha[:24]}"
    if model == "cellpose-cpsam-v2":
        arguments = {
            "image_base64": artifact,
            "media_type": "image/png",
            "diameter": None,
            "research_only": True,
            "wait_seconds": 0,
            "idempotency_key": key,
        }
    else:
        method = "scvi" if cohort == 1 else "scanvi"
        arguments = {
            "anndata_base64": artifact,
            "filename": fixture.name,
            "method": method,
            "batch_key": "batch",
            "labels_key": None if method == "scvi" else "cell_type",
            "unlabeled_category": "Unknown",
            "max_epochs": 2,
            "n_latent": 4,
            "seed": 16 + cohort,
            "research_only": True,
            "wait_seconds": 0,
            "idempotency_key": key,
        }
    started_at = now()
    started = time.monotonic()
    accepted = data(await client.call_tool(tool, arguments))
    replay = data(await client.call_tool(tool, arguments))
    check(replay["id"] == accepted["id"], "idempotency_replay_changed_operation")
    operation, states = await poll(client, accepted["id"], timeout)
    check(operation["status"] == "succeeded", "operation_failed")
    envelope, content = await result_bytes(client, http, accepted["id"])
    if model == "cellpose-cpsam-v2":
        semantic = validate_cellpose(content, fixture_sha, output, fixture.stem)
    else:
        semantic = validate_scvi(
            content, fixture_sha, arguments["method"], output, fixture.stem
        )
    terminal_replay = data(await client.call_tool(tool, arguments))
    check(terminal_replay["id"] == accepted["id"], "terminal_replay_changed_operation")
    return {
        "cohort": cohort,
        "model_id": model,
        "tool_name": tool,
        "fixture": fixture.name,
        "fixture_sha256": fixture_sha,
        "input_artifact": artifact,
        "arguments_sha256": json_digest(arguments),
        "operation_id": accepted["id"],
        "idempotency_replay_verified": True,
        "started_at": started_at,
        "completed_at": now(),
        "elapsed_seconds": time.monotonic() - started,
        "states": states,
        "runtime": operation.get("runtime"),
        "attempt": operation.get("attempt"),
        "result_artifact": envelope["artifact"],
        "result_content_type": envelope["content_type"],
        "semantic": semantic,
        "outcome": "passed",
    }


async def negative(
    client: Client, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    try:
        result = await client.call_tool(tool, arguments)
    except MCPError:
        return {
            "tool_name": tool,
            "rejected": True,
            "transport": "mcp-error",
            "operation_id": None,
        }
    check(getattr(result, "is_error", False) is True, "invalid_input_was_accepted")
    structured = getattr(result, "structured_content", None)
    operation_id = structured.get("id") if isinstance(structured, dict) else None
    check(operation_id is None, "invalid_input_created_operation")
    return {
        "tool_name": tool,
        "rejected": True,
        "transport": "tool-error",
        "operation_id": None,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    origin = validate_origin(args.origin)
    token = read_token(args.token_file)
    fixtures = args.fixtures.resolve()
    output = args.output.resolve()
    check(fixtures.is_dir(), "fixture_directory_missing")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    evidence: dict[str, Any] = {
        "schema": "fs2-visual-science-public-qualification/v1",
        "started_at": now(),
        "origin": origin,
        "control_plane_image": args.control_plane_image,
        "source_commit": args.source_commit,
        "cohorts": [],
        "negative_inputs": [],
        "outcome": "failed",
    }
    headers = {"authorization": "Bearer " + token, "origin": origin}
    async with (
        httpx2.AsyncClient(
            headers=headers, verify=args.verify_tls, trust_env=False, timeout=120
        ) as transport,
        httpx.AsyncClient(
            base_url=origin,
            headers={"authorization": "Bearer " + token},
            verify=args.verify_tls,
            trust_env=False,
            timeout=120,
        ) as http,
        Client(
            streamable_http_client(origin + "/mcp", http_client=transport),
            mode=MCP_VERSION,
        ) as client,
    ):
        tools = await list_tools(client)
        contracts: dict[str, dict[str, Any]] = {}
        for model, expected_tool in (
            ("cellpose-cpsam-v2", "segment_cells_native"),
            ("scvi-scanvi", "integrate_single_cell_native"),
        ):
            view = data(
                await client.call_tool(
                    "get_model_schema", {"model_id": model, "protocol": "native"}
                )
            )
            contract = next(
                item for item in view["contracts"] if item["protocol"] == "native"
            )
            check(
                contract["tool_name"] == expected_tool and expected_tool in tools,
                "typed_tool_missing",
            )
            check(
                tools[expected_tool].input_schema == contract["input_schema"],
                "typed_schema_drift",
            )
            contracts[model] = contract
        evidence["discovery"] = {
            "protocol_version": client.protocol_version,
            "tool_count": len(tools),
            "tools": {
                model: {
                    "tool_name": contract["tool_name"],
                    "schema_sha256": json_digest(contract["input_schema"]),
                }
                for model, contract in contracts.items()
            },
        }
        for cohort in (1, 2):
            for model, tool, filename, media_type in (
                (
                    "cellpose-cpsam-v2",
                    "segment_cells_native",
                    f"microscopy-{cohort - 1}.png",
                    "image/png",
                ),
                (
                    "scvi-scanvi",
                    "integrate_single_cell_native",
                    f"cells-{cohort - 1}.h5ad",
                    "application/x-hdf5",
                ),
            ):
                row = await invoke_case(
                    client,
                    http,
                    cohort=cohort,
                    model=model,
                    tool=tool,
                    fixture=fixtures / filename,
                    media_type=media_type,
                    output=output,
                    timeout=args.timeout_seconds,
                )
                evidence["cohorts"].append(row)
                print(
                    json.dumps(
                        {
                            "model": model,
                            "cohort": cohort,
                            "operation_id": row["operation_id"],
                            "outcome": "passed",
                        }
                    ),
                    flush=True,
                )
        evidence["negative_inputs"].append(
            await negative(
                client,
                "segment_cells_native",
                {
                    "image_base64": "!!!",
                    "media_type": "image/png",
                    "research_only": True,
                },
            )
        )
        evidence["negative_inputs"].append(
            await negative(
                client,
                "integrate_single_cell_native",
                {
                    "anndata_base64": "dGVzdA==",
                    "filename": "invalid.h5ad",
                    "method": "scvi",
                    "max_epochs": 2,
                    "n_latent": 4,
                    "seed": 0,
                },
            )
        )
    evidence.update(completed_at=now(), outcome="passed", mcp_session_closed=True)
    report = output / "qualification.json"
    report.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report.chmod(0o600)
    checksum = output / "SHA256SUMS"
    files = sorted(
        path for path in output.iterdir() if path.is_file() and path != checksum
    )
    checksum.write_text(
        "".join(f"{digest(path.read_bytes())}  {path.name}\n" for path in files),
        encoding="utf-8",
    )
    checksum.chmod(0o600)
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--fixtures", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--control-plane-image", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--verify-tls", action="store_true")
    args = parser.parse_args()
    evidence = asyncio.run(run(args))
    print(
        json.dumps(
            {
                "outcome": evidence["outcome"],
                "cohorts": len(evidence["cohorts"]),
                "negatives": len(evidence["negative_inputs"]),
            }
        )
    )


if __name__ == "__main__":
    main()

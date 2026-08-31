#!/usr/bin/env python3
"""Validate one direct PNG and one gateway-compatible SDXL JSON response."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Any, Sequence

CONTRACT_SCHEMA = "fs2-serve.nebius.ai/semantic-contract/v1"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class SemanticError(ValueError):
    """A fixture or response failed the mixed SDXL contract."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SemanticError(f"{label} cannot be read") from exc
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise SemanticError(f"{label} size is invalid")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SemanticError(f"{label} is not a JSON object") from exc
    if not isinstance(value, dict):
        raise SemanticError(f"{label} is not a JSON object")
    return value


def load_contract(path: Path) -> dict[str, Any]:
    value = load_json(path, "fixture")
    if (
        value.get("schema") != CONTRACT_SCHEMA
        or value.get("kind") != "png-and-b64-json"
        or value.get("contract") != "direct-png-and-b64-json-512x512/v1"
    ):
        raise SemanticError("fixture schema, kind, or contract differs")
    requests = value.get("requests")
    if not isinstance(requests, list) or len(requests) != 2:
        raise SemanticError("fixture must contain exactly two requests")
    formats = [item.get("request", {}).get("response_format") for item in requests]
    if formats != ["image/png", "b64_json"]:
        raise SemanticError("fixture must cover direct PNG then b64_json")
    return value


def read_bounded(path: Path, label: str) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SemanticError(f"{label} cannot be read") from exc
    if not data or len(data) > MAX_RESPONSE_BYTES:
        raise SemanticError(f"{label} size is invalid")
    return data


def png_identity(data: bytes) -> tuple[int, int, int]:
    if not data.startswith(PNG_SIGNATURE):
        raise SemanticError("response is not a PNG")
    position = len(PNG_SIGNATURE)
    width = height = 0
    compressed = bytearray()
    saw_end = False
    while position + 12 <= len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        chunk_type = data[position + 4 : position + 8]
        chunk_end = position + 12 + length
        if chunk_end > len(data):
            raise SemanticError("PNG chunk is truncated")
        chunk = data[position + 8 : position + 8 + length]
        expected_crc = struct.unpack(">I", data[position + 8 + length : chunk_end])[0]
        if zlib.crc32(chunk_type + chunk) & 0xFFFFFFFF != expected_crc:
            raise SemanticError("PNG chunk CRC differs")
        if chunk_type == b"IHDR":
            if length != 13:
                raise SemanticError("PNG header length differs")
            width, height = struct.unpack(">II", chunk[:8])
        elif chunk_type == b"IDAT":
            compressed.extend(chunk)
        elif chunk_type == b"IEND":
            saw_end = True
            break
        position = chunk_end
    if not saw_end or width <= 0 or height <= 0 or not compressed:
        raise SemanticError("PNG structure is incomplete")
    try:
        raster = zlib.decompress(bytes(compressed))
    except zlib.error as exc:
        raise SemanticError("PNG raster cannot be inflated") from exc
    unique = len(set(raster))
    if unique < 4:
        raise SemanticError("PNG raster is constant or trivial")
    return width, height, unique


def validate_png(data: bytes, oracle: dict[str, Any], label: str) -> dict[str, Any]:
    width, height, unique = png_identity(data)
    digest = hashlib.sha256(data).hexdigest()
    if width != oracle.get("width") or height != oracle.get("height"):
        raise SemanticError(f"{label} dimensions differ")
    if oracle.get("nonconstant") is not True:
        raise SemanticError(f"{label} oracle must require a nonconstant image")
    if (
        oracle.get("expected_bytes") is not None
        and len(data) != oracle["expected_bytes"]
    ):
        raise SemanticError(f"{label} byte length differs")
    if (
        oracle.get("expected_sha256") is not None
        and digest != oracle["expected_sha256"]
    ):
        raise SemanticError(f"{label} PNG identity differs")
    return {
        "png_sha256": digest,
        "png_bytes": len(data),
        "width": width,
        "height": height,
        "inflated_unique_byte_values": unique,
    }


def validate(contract: dict[str, Any], responses: Sequence[Path]) -> dict[str, Any]:
    if len(responses) != 2:
        raise SemanticError("exactly two response files are required")
    requests = contract["requests"]
    direct = read_bounded(responses[0], requests[0]["id"])
    first = validate_png(direct, requests[0]["oracle"], requests[0]["id"])

    encoded = read_bounded(responses[1], requests[1]["id"])
    try:
        envelope = json.loads(encoded)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SemanticError("b64_json response is not JSON") from exc
    model = contract["model"]
    if (
        not isinstance(envelope, dict)
        or envelope.get("model") != model["id"]
        or envelope.get("repository") != model["repository"]
        or envelope.get("revision") != model["revision"]
        or envelope.get("mime_type") != "image/png"
        or not isinstance(envelope.get("request_id"), str)
        or not envelope["request_id"]
    ):
        raise SemanticError("b64_json model or correlation envelope differs")
    data = envelope.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise SemanticError("b64_json data envelope differs")
    try:
        decoded = base64.b64decode(data[0]["b64_json"], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise SemanticError("b64_json image is invalid") from exc
    second = validate_png(decoded, requests[1]["oracle"], requests[1]["id"])
    if (
        envelope.get("png_sha256") != second["png_sha256"]
        or envelope.get("png_bytes") != second["png_bytes"]
    ):
        raise SemanticError("b64_json declared PNG identity differs")

    request_hashes = [
        hashlib.sha256(canonical_json(item["request"])).hexdigest() for item in requests
    ]
    if len(set(request_hashes)) != 2 or first["png_sha256"] == second["png_sha256"]:
        raise SemanticError("requests and decoded PNG responses must be distinct")
    return {
        "schema": "fs2-serve.nebius.ai/semantic-validation-result/v1",
        "status": "PASS",
        "contract": contract["contract"],
        "request_sha256": request_hashes,
        "response_sha256": [
            hashlib.sha256(direct).hexdigest(),
            hashlib.sha256(encoded).hexdigest(),
        ],
        "decoded_png": [first, second],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--response", required=True, type=Path, action="append")
    args = parser.parse_args(argv)
    try:
        result = validate(load_contract(args.contract), args.response)
    except SemanticError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Validate two bounded Cosmos 3 Nano JSON/base64 MP4 responses."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence

CONTRACT_SCHEMA = "fs2-serve.nebius.ai/semantic-contract/v1"
MODEL_REPOSITORY = "nvidia/Cosmos3-Nano"
MODEL_REVISION = "7a312c868bcce8e40b3eb40861300a9d0ba3fde1"
MAX_JSON_BYTES = 96 * 1024 * 1024


class SemanticError(ValueError):
    """A fixture or response failed the bounded media contract."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SemanticError(f"{label} cannot be read") from exc
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise SemanticError(f"{label} size is invalid")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SemanticError(f"{label} is not JSON") from exc
    if not isinstance(value, dict):
        raise SemanticError(f"{label} is not a JSON object")
    return value


def load_contract(path: Path) -> dict[str, Any]:
    value = load_json(path, "fixture")
    model = value.get("model")
    if (
        value.get("schema") != CONTRACT_SCHEMA
        or value.get("kind") != "base64-mp4-json"
        or value.get("contract") != "bounded-json-base64-media/v1"
        or model
        != {
            "id": "cosmos3-nano",
            "repository": MODEL_REPOSITORY,
            "revision": MODEL_REVISION,
        }
    ):
        raise SemanticError("fixture identity differs")
    requests = value.get("requests")
    if not isinstance(requests, list) or len(requests) != 2:
        raise SemanticError("fixture must contain exactly two requests")
    if any(item.get("request", {}).get("mode") != "text-to-video" for item in requests):
        raise SemanticError("initial acceptance must contain two text-to-video requests")
    if any(
        item.get("request", {}).get("size") != "448x256"
        or item.get("request", {}).get("num_frames") != 25
        for item in requests
    ):
        raise SemanticError("initial acceptance must remain bounded to 256p and 25 frames")
    return value


def mp4_identity(data: bytes) -> str:
    if len(data) < 16 or b"ftyp" not in data[:32]:
        raise SemanticError("decoded response is not an ISO BMFF MP4")
    return hashlib.sha256(data).hexdigest()


def validate_response(
    response: dict[str, Any], request: dict[str, Any], label: str
) -> dict[str, Any]:
    oracle = request["oracle"]
    if (
        response.get("model") != MODEL_REPOSITORY
        or response.get("revision") != MODEL_REVISION
        or response.get("mode") != "text-to-video"
        or response.get("mime_type") != oracle["mime_type"]
        or response.get("width") != oracle["width"]
        or response.get("height") != oracle["height"]
        or response.get("frames") != oracle["frames"]
        or response.get("fps") != oracle["fps"]
    ):
        raise SemanticError(f"{label} model or media envelope differs")
    encoded = response.get("data_base64")
    if not isinstance(encoded, str) or not encoded:
        raise SemanticError(f"{label} has no base64 media")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise SemanticError(f"{label} base64 media is invalid") from exc
    if not decoded or len(decoded) > oracle["maximum_decoded_bytes"]:
        raise SemanticError(f"{label} decoded media size is invalid")
    digest = mp4_identity(decoded)
    if response.get("bytes") != len(decoded) or response.get("sha256") != digest:
        raise SemanticError(f"{label} declared media identity differs")
    timings = response.get("timings_ms")
    if (
        not isinstance(timings, dict)
        or set(timings) != {"queue", "upstream", "total"}
        or any(not isinstance(value, (int, float)) or value < 0 for value in timings.values())
        or timings["total"] < timings["queue"] + timings["upstream"]
    ):
        raise SemanticError(f"{label} timings are invalid")
    return {"sha256": digest, "bytes": len(decoded)}


def validate(contract: dict[str, Any], responses: Sequence[Path]) -> dict[str, Any]:
    if len(responses) != 2:
        raise SemanticError("exactly two response files are required")
    requests = contract["requests"]
    response_values = [
        load_json(path, requests[index]["id"]) for index, path in enumerate(responses)
    ]
    media = [
        validate_response(response_values[index], requests[index], requests[index]["id"])
        for index in range(2)
    ]
    request_hashes = [
        hashlib.sha256(canonical_json(item["request"])).hexdigest()
        for item in requests
    ]
    if len(set(request_hashes)) != 2 or media[0]["sha256"] == media[1]["sha256"]:
        raise SemanticError("requests and decoded media responses must be distinct")
    return {
        "schema": "fs2-serve.nebius.ai/semantic-validation-result/v1",
        "status": "PASS",
        "contract": contract["contract"],
        "request_sha256": request_hashes,
        "response_sha256": [
            hashlib.sha256(canonical_json(value)).hexdigest()
            for value in response_values
        ],
        "decoded_media": media,
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

#!/usr/bin/env python3
"""Validate two saved semantic responses against one pinned fs2-serve contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
import zlib
from pathlib import Path
from typing import Any, Sequence


CONTRACT_SCHEMA = "fs2-serve.nebius.ai/semantic-contract/v1"


class SemanticError(ValueError):
    pass


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticError(f"invalid JSON response: {path}") from exc


def _content(value: Any) -> str:
    try:
        content = value["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SemanticError("response is not an OpenAI chat response") from exc
    if not isinstance(content, str) or not content.strip():
        raise SemanticError("response content is empty")
    return content.strip()


def _validate_text(contract: dict[str, Any], paths: Sequence[Path]) -> list[str]:
    outputs = [_content(_json(path)) for path in paths]
    for output, request in zip(outputs, contract["requests"], strict=True):
        oracle = request["oracle"]
        if oracle["type"] == "exact-content" and output != oracle["expected"]:
            raise SemanticError(f"{request['id']} did not match the exact oracle")
        if oracle["type"] == "content-contains" and oracle["expected"] not in output:
            raise SemanticError(f"{request['id']} did not contain the required oracle")
    return outputs


def _png_identity(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SemanticError("image response is not a PNG")
    position = 8
    width = height = 0
    compressed = bytearray()
    saw_end = False
    while position + 12 <= len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        chunk_type = data[position + 4 : position + 8]
        content = data[position + 8 : position + 8 + length]
        expected_crc = struct.unpack(">I", data[position + 8 + length : position + 12 + length])[0]
        if zlib.crc32(chunk_type + content) & 0xFFFFFFFF != expected_crc:
            raise SemanticError("PNG chunk CRC is invalid")
        if chunk_type == b"IHDR":
            width, height = struct.unpack(">II", content[:8])
        elif chunk_type == b"IDAT":
            compressed.extend(content)
        elif chunk_type == b"IEND":
            saw_end = True
            break
        position += 12 + length
    if not saw_end or width <= 0 or height <= 0 or not compressed:
        raise SemanticError("PNG structure is incomplete")
    try:
        raster = zlib.decompress(bytes(compressed))
    except zlib.error as exc:
        raise SemanticError("PNG raster stream is invalid") from exc
    if len(set(raster)) < 4:
        raise SemanticError("PNG raster is constant/trivial")
    return width, height, data


def _validate_png(contract: dict[str, Any], paths: Sequence[Path]) -> list[bytes]:
    outputs: list[bytes] = []
    for path, request in zip(paths, contract["requests"], strict=True):
        width, height, data = _png_identity(path)
        oracle = request["oracle"]
        if width != oracle["width"] or height != oracle["height"]:
            raise SemanticError(f"{request['id']} PNG dimensions differ")
        if oracle["expected_bytes"] is not None and len(data) != oracle["expected_bytes"]:
            raise SemanticError(f"{request['id']} PNG byte size differs")
        if oracle["expected_sha256"] is not None and hashlib.sha256(data).hexdigest() != oracle["expected_sha256"]:
            raise SemanticError(f"{request['id']} PNG identity differs")
        outputs.append(data)
    return outputs


def _validate_cxr(contract: dict[str, Any], paths: Sequence[Path]) -> list[str]:
    outputs = [_content(_json(path)) for path in paths]
    oracle = contract["oracle"]
    for output, request in zip(outputs, contract["requests"], strict=True):
        lowered = output.lower()
        if len(output) < oracle["minimum_output_characters"]:
            raise SemanticError(f"{request['id']} medical response is too short")
        if any(phrase in lowered for phrase in oracle["forbidden_phrases"]):
            raise SemanticError(f"{request['id']} contains a refusal/non-image response")
        tags = "|".join(re.escape(tag) for tag in oracle["reasoning_tags"])
        sections = re.fullmatch(
            rf"\s*<(?P<tag>{tags})>(?P<thinking>.*?)</(?P=tag)>"
            r"\s*<answer>(?P<answer>.*?)</answer>\s*",
            output,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if sections is None:
            raise SemanticError(f"{request['id']} lacks complete reasoning/answer sections")
        thinking = sections.group("thinking").strip()
        answer = sections.group("answer").strip()
        if len(thinking) < oracle["minimum_thinking_characters"]:
            raise SemanticError(f"{request['id']} reasoning section is too short")
        if len(answer) < oracle["minimum_answer_characters"]:
            raise SemanticError(f"{request['id']} answer section is too short")
        if any(not any(term in lowered for term in group) for group in request["expected_any_groups"]):
            raise SemanticError(f"{request['id']} does not satisfy the pinned medical term groups")
        terms = oracle["medical_terms"]
        matched_terms = {
            term
            for term in terms
            if re.search(rf"(?<!\w){re.escape(term)}(?:s)?(?!\w)", lowered)
        }
        if len(matched_terms) < oracle["minimum_medical_term_count"]:
            raise SemanticError(f"{request['id']} has too few pinned medical terms")
    return outputs


def validate(contract: dict[str, Any], response_paths: Sequence[Path]) -> dict[str, Any]:
    if contract.get("schema") != CONTRACT_SCHEMA or len(contract.get("requests", [])) != 2:
        raise SemanticError("semantic contract must contain exactly two requests")
    if len(response_paths) != 2:
        raise SemanticError("semantic validation requires exactly two response files")
    kind = contract.get("kind")
    if kind == "openai-text":
        outputs: Sequence[str | bytes] = _validate_text(contract, response_paths)
    elif kind == "png":
        outputs = _validate_png(contract, response_paths)
    elif kind == "medical-nonclinical":
        if contract.get("non_clinical") is not True or contract.get("commercial_use") != "prohibited":
            raise SemanticError("CXR contract must retain nonclinical/noncommercial policy")
        outputs = _validate_cxr(contract, response_paths)
    else:
        raise SemanticError("semantic contract kind is unsupported")
    request_hashes = [
        hashlib.sha256(
            json.dumps(item["request"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for item in contract["requests"]
    ]
    response_hashes = [
        hashlib.sha256(item.encode() if isinstance(item, str) else item).hexdigest()
        for item in outputs
    ]
    if len(set(request_hashes)) != 2 or len(set(response_hashes)) != 2:
        raise SemanticError("requests and semantically valid responses must both be distinct")
    return {
        "schema": "fs2-serve.nebius.ai/semantic-validation-result/v1",
        "status": "PASS",
        "contract": contract["contract"],
        "request_sha256": request_hashes,
        "response_sha256": response_hashes,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--response", required=True, type=Path, action="append")
    args = parser.parse_args(argv)
    try:
        result = validate(_json(args.contract), args.response)
    except SemanticError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

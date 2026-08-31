#!/usr/bin/env python3
"""Run two deterministic, synthetic, non-patient NV-Segment-CT probes."""

from __future__ import annotations

import argparse
import array
import base64
import gzip
import hashlib
import json
import math
import struct
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

CONTRACT_SCHEMA = "fs2-serve.nebius.ai/semantic-contract/v1"
MAX_RESPONSE_BYTES = 48 * 1024 * 1024
EXPECTED_KEYS = {
    "id",
    "generator",
    "request",
    "payload_sha256",
    "oracle",
}
DATATYPES = {
    2: ("B", 1),
    4: ("h", 2),
    8: ("i", 4),
    16: ("f", 4),
    64: ("d", 8),
    256: ("b", 1),
    512: ("H", 2),
}


class SemanticError(ValueError):
    """The endpoint or fixture failed the semantic contract."""


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_: Any, **__: Any) -> None:  # type: ignore[override]
        return None


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def load_contract(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SemanticError("fixture is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise SemanticError("fixture must be a JSON object")
    if (
        value.get("schema") != CONTRACT_SCHEMA
        or value.get("kind") != "medical-segmentation-nonclinical"
    ):
        raise SemanticError("fixture schema or kind differs")
    if (
        value.get("non_clinical") is not True
        or value.get("commercial_use") != "license-dependent"
    ):
        raise SemanticError("fixture must retain its non-clinical license boundary")
    requests = value.get("requests")
    if not isinstance(requests, list) or len(requests) != 2:
        raise SemanticError("fixture must define exactly two probes")
    if any(
        not isinstance(item, dict) or set(item) != EXPECTED_KEYS for item in requests
    ):
        raise SemanticError("fixture request shape differs")
    if any(
        not isinstance(item["payload_sha256"], str)
        or len(item["payload_sha256"]) != 64
        or any(
            character not in "0123456789abcdef" for character in item["payload_sha256"]
        )
        for item in requests
    ):
        raise SemanticError("fixture payload identity is not an exact SHA-256")
    return value


def validate_base_url(raw: str) -> str:
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SemanticError("base URL must be an HTTP(S) origin")
    if (
        parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SemanticError(
            "base URL must not contain credentials, path, query, or fragment"
        )
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}" + (
        f":{parsed.port}" if parsed.port is not None else ""
    )


def nifti_bytes(generator: Mapping[str, Any]) -> bytes:
    if (
        generator.get("kind") != "synthetic-ellipsoid/v1"
        or generator.get("affine") != "identity"
    ):
        raise SemanticError("unsupported safe fixture generator")
    shape = generator.get("shape")
    center = generator.get("center")
    axes = generator.get("axes")
    if (
        shape != [96, 96, 96]
        or not isinstance(center, list)
        or not isinstance(axes, list)
    ):
        raise SemanticError("safe fixture geometry differs")
    if (
        len(center) != 3
        or len(axes) != 3
        or any(type(value) is not int for value in center + axes)
    ):
        raise SemanticError("safe fixture coordinates must be integers")
    if any(value <= 0 or value >= 96 for value in axes):
        raise SemanticError("safe fixture axes are out of range")
    background = float(generator.get("background_hu"))
    foreground = float(generator.get("foreground_hu"))
    if (
        not math.isfinite(background)
        or not math.isfinite(foreground)
        or background == foreground
    ):
        raise SemanticError("safe fixture intensities differ")
    if generator.get("gzip_mtime") != 0:
        raise SemanticError("safe fixture gzip timestamp must be deterministic")

    nx, ny, nz = shape
    voxels = array.array("f", [background]) * (nx * ny * nz)
    cx, cy, cz = center
    ax, ay, az = axes
    for z in range(nz):
        dz = ((z - cz) / az) ** 2
        for y in range(ny):
            dyz = ((y - cy) / ay) ** 2 + dz
            for x in range(nx):
                if ((x - cx) / ax) ** 2 + dyz <= 1.0:
                    voxels[x + nx * (y + ny * z)] = foreground

    header = bytearray(352)
    struct.pack_into("<i", header, 0, 348)
    struct.pack_into("<8h", header, 40, 3, nx, ny, nz, 1, 1, 1, 1)
    struct.pack_into("<h", header, 70, 16)
    struct.pack_into("<h", header, 72, 32)
    struct.pack_into("<8f", header, 76, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
    struct.pack_into("<f", header, 108, 352.0)
    struct.pack_into("<f", header, 112, 1.0)
    header[123] = 2
    struct.pack_into("<h", header, 254, 1)
    struct.pack_into("<4f", header, 280, 1.0, 0.0, 0.0, 0.0)
    struct.pack_into("<4f", header, 296, 0.0, 1.0, 0.0, 0.0)
    struct.pack_into("<4f", header, 312, 0.0, 0.0, 1.0, 0.0)
    header[344:348] = b"n+1\x00"
    return gzip.compress(bytes(header) + voxels.tobytes(), compresslevel=9, mtime=0)


def read_bounded(response: Any) -> bytes:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise SemanticError("response exceeds the bound")
    return body


def parse_mask(output: bytes, shape: list[int]) -> tuple[str, int]:
    try:
        raw = gzip.decompress(output)
    except (OSError, EOFError) as exc:
        raise SemanticError("output is not a valid gzip stream") from exc
    if (
        len(raw) < 352
        or struct.unpack_from("<i", raw, 0)[0] != 348
        or raw[344:348] != b"n+1\x00"
    ):
        raise SemanticError("output is not a NIfTI-1 single-file image")
    dimensions = list(struct.unpack_from("<8h", raw, 40))
    if dimensions[0] != 3 or dimensions[1:4] != shape:
        raise SemanticError("output NIfTI shape differs")
    datatype = struct.unpack_from("<h", raw, 70)[0]
    bitpix = struct.unpack_from("<h", raw, 72)[0]
    if datatype not in DATATYPES:
        raise SemanticError("output NIfTI datatype is unsupported")
    typecode, item_size = DATATYPES[datatype]
    if bitpix != item_size * 8:
        raise SemanticError("output NIfTI bit depth differs")
    offset = int(struct.unpack_from("<f", raw, 108)[0])
    count = math.prod(shape)
    payload = raw[offset : offset + count * item_size]
    if len(payload) != count * item_size:
        raise SemanticError("output NIfTI voxel payload is truncated")
    values = array.array(typecode)
    values.frombytes(payload)
    if sys.byteorder != "little" and item_size > 1:
        values.byteswap()
    nonzero = sum(value != 0 for value in values)
    return hashlib.sha256(payload).hexdigest(), nonzero


def validate_response(
    body: bytes, contract: Mapping[str, Any], probe: Mapping[str, Any]
) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SemanticError("endpoint response is not UTF-8 JSON") from exc
    model = contract["model"]
    if (
        value.get("model") != model["id"]
        or value.get("repository") != model["repository"]
        or value.get("revision") != model["revision"]
        or value.get("non_clinical") is not True
        or value.get("mime_type") != "application/gzip"
    ):
        raise SemanticError("endpoint identity or policy differs")
    oracle = probe["oracle"]
    if value.get("shape") != oracle["shape"]:
        raise SemanticError("response shape differs")
    labels = value.get("labels")
    label = str(oracle["foreground_label"])
    if not isinstance(labels, dict) or type(labels.get(label)) is not int:
        raise SemanticError("response foreground label is missing")
    if labels[label] < oracle["minimum_foreground_voxels"]:
        raise SemanticError("response contains too few foreground voxels")
    encoded = value.get("output_nifti_base64")
    if not isinstance(encoded, str):
        raise SemanticError("response output is missing")
    try:
        output = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise SemanticError("response output is not valid base64") from exc
    if value.get("output_bytes") != len(output):
        raise SemanticError("response output length differs")
    mask_sha256, nonzero = parse_mask(output, oracle["shape"])
    if nonzero < oracle["minimum_foreground_voxels"]:
        raise SemanticError("NIfTI output contains too few foreground voxels")
    return {
        "response_sha256": hashlib.sha256(body).hexdigest(),
        "output_sha256": hashlib.sha256(output).hexdigest(),
        "mask_sha256": mask_sha256,
        "output_bytes": len(output),
        "nonzero_voxels": nonzero,
        "labels": labels,
    }


def validate(base_url: str, contract: dict[str, Any]) -> dict[str, Any]:
    endpoint = validate_base_url(base_url) + "/segment"
    opener = build_opener(ProxyHandler({}), RejectRedirects())
    results: list[dict[str, Any]] = []
    for probe in contract["requests"]:
        input_bytes = nifti_bytes(probe["generator"])
        payload = dict(probe["request"])
        payload["input_nifti_base64"] = base64.b64encode(input_bytes).decode("ascii")
        request_body = canonical_json(payload)
        request_sha256 = hashlib.sha256(request_body).hexdigest()
        if request_sha256 != probe["payload_sha256"]:
            raise SemanticError(
                f"{probe['id']} generated payload differs from the pinned identity"
            )
        request = Request(
            endpoint,
            data=request_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(request, timeout=900) as response:
                if response.status != 200:
                    raise SemanticError(
                        f"{probe['id']} returned HTTP {response.status}"
                    )
                body = read_bounded(response)
        except HTTPError as exc:
            raise SemanticError(f"{probe['id']} returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise SemanticError(f"{probe['id']} transport failed") from exc
        result = validate_response(body, contract, probe)
        result.update(
            {
                "id": probe["id"],
                "input_bytes": len(input_bytes),
                "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
                "request_sha256": request_sha256,
            }
        )
        results.append(result)
    if len({item["request_sha256"] for item in results}) != 2:
        raise SemanticError("semantic requests are not distinct")
    if len({item["mask_sha256"] for item in results}) != 2:
        raise SemanticError("semantic masks are not distinct")
    return {
        "schema": "fs2-serve.nebius.ai/live-semantic-validation/v1",
        "status": "PASS",
        "contract": contract["contract"],
        "model": contract["model"],
        "non_clinical": True,
        "results": results,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--fixture", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = validate(args.base_url, load_contract(args.fixture))
    except SemanticError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

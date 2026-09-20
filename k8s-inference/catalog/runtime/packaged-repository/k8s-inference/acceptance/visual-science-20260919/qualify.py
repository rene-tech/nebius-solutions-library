"""Qualify two visual-science requests against a directly addressed worker."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import time
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def request(url: str, *, body: bytes | None = None, content_type: str | None = None) -> dict:
    if urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError("qualification URL must use HTTP or HTTPS")
    headers = {} if content_type is None else {"Content-Type": content_type}
    started = time.perf_counter()
    try:
        with urlopen(Request(url, data=body, headers=headers), timeout=900) as response:  # noqa: S310
            raw, status, response_type = response.read(), response.status, response.headers.get_content_type()
    except HTTPError as error:
        raw, status, response_type = error.read(), error.code, error.headers.get_content_type()
    return {
        "status": status,
        "elapsed_seconds": time.perf_counter() - started,
        "request_bytes": 0 if body is None else len(body),
        "request_sha256": None if body is None else hashlib.sha256(body).hexdigest(),
        "response_bytes": len(raw),
        "response_sha256": hashlib.sha256(raw).hexdigest(),
        "content_type": response_type,
        "body": raw,
    }


def multipart(path: Path, fields: dict[str, str]) -> tuple[bytes, str]:
    identity = hashlib.sha256(path.read_bytes() + json.dumps(fields, sort_keys=True).encode()).hexdigest()
    boundary = f"fs2-{identity[:32]}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
            f"Content-Type: {mimetypes.guess_type(path.name)[0] or 'application/x-hdf5'}\r\n\r\n".encode(),
            path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def cellpose_payload(path: Path, *, research_only: bool = True) -> bytes:
    return json.dumps(
        {
            "image_base64": base64.b64encode(path.read_bytes()).decode(),
            "media_type": "image/png",
            "diameter": None,
            "research_only": research_only,
        },
        separators=(",", ":"),
    ).encode()


def qualify_cellpose(base_url: str, fixtures: list[Path], output_dir: Path) -> list[dict]:
    results = []
    for path in fixtures:
        payload = cellpose_payload(path)
        result = request(f"{base_url}/v1/segment", body=payload, content_type="application/json")
        if result["status"] != 200:
            raise RuntimeError(result["body"].decode(errors="replace"))
        decoded = json.loads(result.pop("body"))
        if decoded["input_sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
            raise AssertionError("runtime input digest differs from fixture")
        if decoded["object_count"] < 1 or decoded["research_only"] is not True:
            raise AssertionError("cell segmentation result is not semantically valid")
        overlay = base64.b64decode(decoded["overlay_base64"], validate=True)
        if not overlay.startswith(b"\x89PNG\r\n\x1a\n"):
            raise AssertionError("overlay is not PNG")
        (output_dir / f"{path.stem}-overlay.png").write_bytes(overlay)
        result["semantic"] = {
            "input_sha256": decoded["input_sha256"],
            "model_sha256": decoded["model_sha256"],
            "object_count": decoded["object_count"],
            "overlay_sha256": hashlib.sha256(overlay).hexdigest(),
        }
        results.append(result)
    return results


def failure_replay_cellpose(base_url: str, fixture: Path, reference: dict) -> dict:
    replay = request(
        f"{base_url}/v1/segment",
        body=cellpose_payload(fixture),
        content_type="application/json",
    )
    replay_body = json.loads(replay.pop("body"))
    if replay["request_sha256"] != reference["request_sha256"]:
        raise AssertionError("Cellpose replay request differs")
    if replay["response_sha256"] != reference["response_sha256"]:
        raise AssertionError("Cellpose deterministic replay response differs")
    replay["semantic"] = {
        "input_sha256": replay_body["input_sha256"],
        "model_sha256": replay_body["model_sha256"],
        "object_count": replay_body["object_count"],
    }
    invalid = json.dumps(
        {"image_base64": "!!!", "media_type": "image/png", "research_only": True},
        separators=(",", ":"),
    ).encode()
    negative = request(f"{base_url}/v1/segment", body=invalid, content_type="application/json")
    negative_body = json.loads(negative.pop("body"))
    if negative["status"] != 422:
        raise AssertionError("Cellpose malformed-input request did not fail closed")
    negative["semantic"] = negative_body
    return {"replay": replay, "negative": negative}


def qualify_scvi(base_url: str, fixtures: list[Path], output_dir: Path) -> list[dict]:
    results = []
    for index, path in enumerate(fixtures):
        fields = {
            "method": "scvi" if index == 0 else "scanvi",
            "batch_key": "batch",
            "max_epochs": "2",
            "n_latent": "4",
            "seed": str(17 + index),
            "research_only": "true",
        }
        if index == 1:
            fields.update(labels_key="cell_type", unlabeled_category="Unknown")
        body, content_type = multipart(path, fields)
        result = request(f"{base_url}/v1/fit-transform", body=body, content_type=content_type)
        if result["status"] != 200:
            raise RuntimeError(result["body"].decode(errors="replace"))
        archive = result.pop("body")
        with zipfile.ZipFile(BytesIO(archive)) as bundle:
            names = set(bundle.namelist())
            required = {"manifest.json", "integrated.h5ad", "latent_embeddings.csv", "preview.png"}
            if not required <= names or bundle.testzip() is not None:
                raise AssertionError("scVI result bundle is incomplete")
            manifest = json.loads(bundle.read("manifest.json"))
            preview = bundle.read("preview.png")
        if manifest["method"] != fields["method"] or manifest["research_only"] is not True:
            raise AssertionError("scVI manifest differs from request")
        if not preview.startswith(b"\x89PNG\r\n\x1a\n"):
            raise AssertionError("scVI preview is not PNG")
        (output_dir / f"{path.stem}-{fields['method']}.zip").write_bytes(archive)
        (output_dir / f"{path.stem}-{fields['method']}-preview.png").write_bytes(preview)
        result["semantic"] = manifest
        results.append(result)
    return results


def failure_replay_scvi(base_url: str, fixture: Path, reference: dict) -> dict:
    fields = {
        "method": "scvi",
        "batch_key": "batch",
        "max_epochs": "2",
        "n_latent": "4",
        "seed": "17",
        "research_only": "true",
    }
    body, content_type = multipart(fixture, fields)
    replay = request(f"{base_url}/v1/fit-transform", body=body, content_type=content_type)
    archive = replay.pop("body")
    with zipfile.ZipFile(BytesIO(archive)) as bundle:
        replay_manifest = json.loads(bundle.read("manifest.json"))
    if replay["request_sha256"] != reference["request_sha256"]:
        raise AssertionError("scVI replay request differs")
    if replay_manifest != reference["semantic"]:
        raise AssertionError("scVI replay semantic manifest differs")
    replay["semantic"] = replay_manifest

    fields["research_only"] = "false"
    body, content_type = multipart(fixture, fields)
    negative = request(f"{base_url}/v1/fit-transform", body=body, content_type=content_type)
    negative_body = json.loads(negative.pop("body"))
    if negative["status"] != 403:
        raise AssertionError("scVI acknowledgement request did not fail closed")
    negative["semantic"] = negative_body
    return {"replay": replay, "negative": negative}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=("cellpose", "scvi"))
    parser.add_argument("base_url")
    parser.add_argument("fixtures", nargs=2, type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    health = request(f"{args.base_url}/v1/health/ready")
    health_body = json.loads(health.pop("body"))
    if health["status"] != 200:
        raise RuntimeError(health_body)
    results = (
        qualify_cellpose(args.base_url, args.fixtures, args.output_dir)
        if args.model == "cellpose"
        else qualify_scvi(args.base_url, args.fixtures, args.output_dir)
    )
    failure_replay = (
        failure_replay_cellpose(args.base_url, args.fixtures[0], results[0])
        if args.model == "cellpose"
        else failure_replay_scvi(args.base_url, args.fixtures[0], results[0])
    )
    report = {
        "schema": "fs2-visual-science-native-qualification/v1",
        "started_at": datetime.now(UTC).isoformat(),
        "model": args.model,
        "boundary": "direct worker HTTP; excludes public App admission and MCP",
        "health": {**health, "semantic": health_body},
        "results": results,
        "replay": failure_replay["replay"],
        "negative": failure_replay["negative"],
        "completed_at": datetime.now(UTC).isoformat(),
        "outcome": "passed",
    }
    if len({item["request_sha256"] for item in report["results"]}) != 2:
        raise AssertionError("qualification requests are not distinct")
    if len({item["response_sha256"] for item in report["results"]}) != 2:
        raise AssertionError("qualification responses are not distinct")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

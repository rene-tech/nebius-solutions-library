#!/usr/bin/env python3
"""Download and verify one immutable Hugging Face snapshot before serving."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import snapshot_download


LOCK_PATH = Path(os.environ.get("MODEL_LOCK_PATH", "/contract/model.lock.json"))
MODEL_DIR = Path(os.environ.get("MODEL_DIR", "/models/qwen3-8b"))
RECEIPT_PATH = Path(
    os.environ.get("LOCALIZATION_RECEIPT_PATH", "/models/localization-receipt.json")
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(files: list[dict[str, object]]) -> tuple[list[dict[str, object]], int]:
    expected_paths = {str(entry["path"]) for entry in files}
    actual_paths = {
        path.relative_to(MODEL_DIR).as_posix()
        for path in MODEL_DIR.rglob("*")
        if path.is_file() and ".cache" not in path.relative_to(MODEL_DIR).parts
    }
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        unexpected = sorted(actual_paths - expected_paths)
        raise RuntimeError(f"snapshot inventory mismatch: missing={missing}, unexpected={unexpected}")

    verified: list[dict[str, object]] = []
    total = 0
    for entry in files:
        path = MODEL_DIR / str(entry["path"])
        size = path.stat().st_size
        digest = sha256(path)
        if size != entry["size"]:
            raise RuntimeError(f"size mismatch for {entry['path']}: {size} != {entry['size']}")
        if digest != entry["sha256"]:
            raise RuntimeError(
                f"sha256 mismatch for {entry['path']}: {digest} != {entry['sha256']}"
            )
        verified.append({"path": entry["path"], "size": size, "sha256": digest})
        total += size
    return verified, total


def main() -> int:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    if lock.get("schema_version") != 1:
        raise RuntimeError("unsupported model lock schema")
    model = lock["model"]
    files = model["files"]
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    download_started = datetime.now(timezone.utc)
    snapshot_download(
        repo_id=model["repo_id"],
        revision=model["revision"],
        local_dir=MODEL_DIR,
        allow_patterns=[entry["path"] for entry in files],
    )
    download_completed = datetime.now(timezone.utc)
    verified, total = verify(files)
    verification_completed = datetime.now(timezone.utc)
    if total != model["total_size_bytes"]:
        raise RuntimeError(f"total size mismatch: {total} != {model['total_size_bytes']}")

    receipt = {
        "schema_version": 1,
        "repo_id": model["repo_id"],
        "revision": model["revision"],
        "download_started_at": download_started.isoformat().replace("+00:00", "Z"),
        "download_completed_at": download_completed.isoformat().replace("+00:00", "Z"),
        "verification_completed_at": verification_completed.isoformat().replace("+00:00", "Z"),
        "download_duration_seconds": (
            download_completed - download_started
        ).total_seconds(),
        "verification_duration_seconds": (
            verification_completed - download_completed
        ).total_seconds(),
        "total_size_bytes": total,
        "files": verified,
    }
    temporary = RECEIPT_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, RECEIPT_PATH)
    print(json.dumps(receipt, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # preserve a concise terminal reason in init logs
        print(f"localization failed: {error}", file=sys.stderr, flush=True)
        raise

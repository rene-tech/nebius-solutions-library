#!/usr/bin/env python3
"""Materialize the exact Evo2-40B checkpoint on its persistent cache volume."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from huggingface_hub import hf_hub_download

REPOSITORY = "arcinstitute/evo2_40b"
REVISION = "d529aa57c30771814217ad89baaeaf6e2315c7d7"
PARTS = (
    (
        "evo2_40b.pt.part0",
        41_126_745_847,
        "3b74fa4e6158d49265e3e270ba8869390d064358f8bf3d2af0b3e1772728f485",
    ),
    (
        "evo2_40b.pt.part1",
        41_126_745_847,
        "bdc4a76e0f23f8295e7061c2f0deff24f723bd916dc4cdc4d9216cac9c2d49d5",
    ),
)
TARGET_BYTES = 82_253_491_694
TARGET_SHA256 = "dd299612b1c1cdded0dfdcaf4d16f98fc97458261d80f4d662429f0ccb316bc3"
CACHE_ROOT = Path("/model-cache")
TARGET = CACHE_ROOT / "evo2_40b.pt"
RECEIPT = CACHE_ROOT / "evo2_40b.materialization.json"


def emit_startup_phase(name: str) -> None:
    print(
        json.dumps({"event": "fs2-startup-phase", "name": name}, sort_keys=True),
        flush=True,
    )


def expected_receipt() -> dict[str, object]:
    return {
        "schema": "fs2-serve.nebius.ai/evo2-materialization/v1",
        "repository": REPOSITORY,
        "revision": REVISION,
        "bytes": TARGET_BYTES,
        "sha256": TARGET_SHA256,
        "verification": "huggingface-lfs-oid-and-size",
        "parts": [
            {"filename": filename, "bytes": size, "sha256": digest}
            for filename, size, digest in PARTS
        ],
    }


def verify_target() -> None:
    try:
        receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("existing Evo2 checkpoint lacks its exact receipt") from exc
    if TARGET.stat().st_size != TARGET_BYTES or receipt != expected_receipt():
        raise RuntimeError("existing Evo2 checkpoint differs from the exact target")


def exact_lfs_blob(path: Path, filename: str, size: int, digest: str) -> Path:
    blob = path.resolve(strict=True)
    if (
        not blob.is_file()
        or blob.is_symlink()
        or blob.name != digest
        or blob.stat().st_size != size
    ):
        raise RuntimeError(f"downloaded {filename} differs from its exact LFS identity")
    return blob


def main() -> None:
    emit_startup_phase("artifact-localization-start")
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    if TARGET.exists():
        verify_target()
        emit_startup_phase("artifact-localization-verified")
        print(
            json.dumps(
                {
                    "event": "checkpoint-verified",
                    "bytes": TARGET_BYTES,
                    "sha256": TARGET_SHA256,
                }
            ),
            flush=True,
        )
        return

    download_cache = CACHE_ROOT / "hf-download"
    temporary = CACHE_ROOT / "evo2_40b.pt.partial"
    temporary.unlink(missing_ok=True)
    downloaded: list[Path] = []
    for filename, _, _ in PARTS:
        downloaded.append(
            Path(
                hf_hub_download(
                    repo_id=REPOSITORY,
                    filename=filename,
                    revision=REVISION,
                    cache_dir=download_cache,
                )
            )
        )

    # Hugging Face stores LFS payloads by their SHA-256 OID. Validate both blob
    # identities and sizes without rereading 82 GB, then move the first shard in
    # place so only the second shard must be copied. A retained restart trusts
    # the immutable, read-only target plus this exact materialization receipt.
    first_path = downloaded[0]
    first_name, first_expected_bytes, first_expected_sha256 = PARTS[0]
    first_blob = exact_lfs_blob(
        first_path, first_name, first_expected_bytes, first_expected_sha256
    )
    os.replace(first_blob, temporary)
    merged_bytes = first_expected_bytes
    try:
        with temporary.open("ab") as output:
            for path, (filename, expected_bytes, expected_sha256) in zip(
                downloaded[1:], PARTS[1:], strict=True
            ):
                exact_lfs_blob(path, filename, expected_bytes, expected_sha256)
                part_bytes = 0
                with path.open("rb") as stream:
                    while chunk := stream.read(16 * 1024 * 1024):
                        output.write(chunk)
                        part_bytes += len(chunk)
                        merged_bytes += len(chunk)
                if part_bytes != expected_bytes:
                    raise RuntimeError(
                        f"downloaded {filename} changed during materialization"
                    )
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    if merged_bytes != TARGET_BYTES or temporary.stat().st_size != TARGET_BYTES:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("merged Evo2 checkpoint size differs from the exact target")
    os.chmod(temporary, 0o444)
    os.replace(temporary, TARGET)
    shutil.rmtree(download_cache, ignore_errors=True)
    receipt = expected_receipt()
    RECEIPT.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(RECEIPT, 0o444)
    emit_startup_phase("artifact-localization-verified")
    print(
        json.dumps(
            {
                "event": "checkpoint-materialized",
                "bytes": TARGET_BYTES,
                "sha256": TARGET_SHA256,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()

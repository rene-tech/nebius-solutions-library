#!/usr/bin/env python3
"""Pinned-reader preparation/validation for the public local-directory client.

Run with the worker's locked Python environment. Never changes the input tree.
API credentials are neither needed nor accepted by this process.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "models/general-media/lerobot-augmentation"
sys.path.insert(0, str(MODEL / "runtime/src"))
sys.path.insert(0, str(MODEL / "fixtures"))

from fs2_lerobot_augmentation.contracts import AugmentationRequest  # noqa: E402
from fs2_lerobot_augmentation.dataset import (  # noqa: E402
    BUNDLE_MANIFEST_SCHEMA,
    MAX_FILES,
    extract_uploaded_bundle,
    open_and_validate,
    sha256_file,
)


def check(condition, code):
    if not condition:
        raise ValueError(code)


def write_json(path: Path, value):
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def identity(path: Path):
    return {"sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def pack_directory(
    source: Path,
    archive: Path,
    *,
    max_bytes: int,
    max_expanded_bytes: int | None = None,
):
    """Inventory a local directory and stream a self-contained zstd bundle.

    The client, not the server, interprets the local path. A freshly generated
    manifest replaces any old bundle manifest in the archive, never on disk.
    """
    import zstandard

    check(not source.is_symlink() and source.is_dir(), "dataset_directory_required")
    source = source.resolve()
    check(not archive.resolve().is_relative_to(source), "output_inside_dataset_refused")
    entries = []
    for path in sorted(source.rglob("*")):
        check(not path.is_symlink(), "dataset_symlink_refused")
        mode = path.stat().st_mode
        check(stat.S_ISDIR(mode) or stat.S_ISREG(mode), "dataset_special_file_refused")
        if not path.is_file() or path == source / "fs2-bundle-manifest.json":
            continue
        entries.append({"path": path.relative_to(source).as_posix(), **identity(path)})
        check(len(entries) <= MAX_FILES, "dataset_file_limit_exceeded")
    check(
        entries
        and sum(row["size_bytes"] for row in entries)
        <= (max_expanded_bytes or max_bytes),
        "dataset_size_limit_exceeded",
    )
    manifest = (
        json.dumps(
            {"schema": BUNDLE_MANIFEST_SCHEMA, "files": entries},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    descriptor = os.open(
        archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with (
        os.fdopen(descriptor, "wb") as raw,
        zstandard.ZstdCompressor(level=3).stream_writer(raw) as encoded,
    ):
        with tarfile.open(fileobj=encoded, mode="w|") as bundle:
            for row in entries:
                path = source / row["path"]
                check(
                    not path.is_symlink()
                    and identity(path) == {k: row[k] for k in ("sha256", "size_bytes")},
                    "dataset_changed_during_pack",
                )
                with path.open("rb") as content:
                    info = tarfile.TarInfo(row["path"])
                    info.size, info.mode, info.mtime = row["size_bytes"], 0o600, 0
                    bundle.addfile(info, content)
                check(
                    identity(path) == {k: row[k] for k in ("sha256", "size_bytes")},
                    "dataset_changed_during_pack",
                )
            info = tarfile.TarInfo("fs2-bundle-manifest.json")
            info.size, info.mode, info.mtime = len(manifest), 0o600, 0
            bundle.addfile(info, io.BytesIO(manifest))
    check(archive.stat().st_size <= max_bytes, "compressed_bundle_size_limit_exceeded")
    return identity(archive)


def parameters(template, artifact):
    if template.get("schema") == "fs2-serve.nebius.ai/scientific-run-request/v1":
        check(
            template.get("operation") == "augment-lerobot-dataset",
            "request_operation_invalid",
        )
        value = template.get("parameters")
    else:
        value = template
    check(isinstance(value, dict), "augmentation_parameters_required")
    value = json.loads(json.dumps(value))
    value["source"] = {"kind": "uploaded-bundle", **artifact}
    AugmentationRequest.parse(value)
    return value


def extract_bounded(archive: Path, destination: Path, *, sha256: str, max_bytes: int):
    """Preflight expanded size before the worker's inventory/hash-safe extractor."""
    import zstandard

    total, seen = 0, set()
    with (
        archive.open("rb") as raw,
        zstandard.ZstdDecompressor().stream_reader(raw) as decoded,
    ):
        with tarfile.open(fileobj=decoded, mode="r|") as bundle:
            for member in bundle:
                path = PurePosixPath(member.name)
                check(
                    (member.isfile() or member.isdir())
                    and not path.is_absolute()
                    and ".." not in path.parts
                    and member.name.rstrip("/") == path.as_posix()
                    and member.name not in seen,
                    "bundle_member_invalid",
                )
                seen.add(member.name)
                total += member.size
                check(
                    len(seen) <= MAX_FILES + 1 and total <= max_bytes,
                    "expanded_bundle_limit_exceeded",
                )
    extract_uploaded_bundle(archive, destination, expected_sha256=sha256)


def require_visual_changes(checked):
    selected = [row for row in checked["visual_comparison"] if row["selected"]]
    check(
        selected and all(row["changed_frames"] > 0 for row in selected),
        "selected_video_unchanged",
    )


def prepare(
    source: Path,
    template: Path,
    output: Path,
    *,
    max_bytes: int,
    max_expanded_bytes: int = 8 * 1024**3,
):
    archive = output / "dataset.tar.zst"
    measured = pack_directory(
        source, archive, max_bytes=max_bytes, max_expanded_bytes=max_expanded_bytes
    )
    placeholder = {
        "artifact_id": "00000000-0000-4000-8000-000000000001",
        **measured,
        "media_type": "application/x-tar",
        "compression": "zstd",
    }
    parsed_parameters = parameters(json.loads(template.read_text()), placeholder)
    request = AugmentationRequest.parse(parsed_parameters)
    localized = output / "source"
    extract_bounded(
        archive, localized, sha256=measured["sha256"], max_bytes=max_expanded_bytes
    )
    inspection = open_and_validate(
        localized, repo_id="fs2/client-source", selection=request.selection
    )
    result = {
        "bundle": measured,
        "parameters": parsed_parameters,
        "source": {
            "tree_sha256": inspection.tree_sha256,
            "frames": inspection.frames,
            "episodes": len(inspection.episodes),
            "cameras": list(inspection.cameras),
            "fps": inspection.fps,
            "decoded_video_frames": inspection.decoded_video_frames,
        },
        "reader": "lerobot==0.6.1",
        "input_directory_modified": False,
    }
    write_json(output / "prepared.json", result)
    return result


def validate(
    source: Path,
    archive: Path,
    destination: Path,
    parameters_path: Path,
    variant_path: Path,
    receipt_path: Path,
    *,
    max_bytes: int = 5 * 1024**3,
    max_expanded_bytes: int = 8 * 1024**3,
):
    from validate_variant import compare_variant

    request = AugmentationRequest.parse(json.loads(parameters_path.read_text()))
    variant = json.loads(variant_path.read_text())
    artifact = variant["artifact"]
    check(archive.stat().st_size <= max_bytes, "compressed_bundle_size_limit_exceeded")
    check(
        identity(archive) == {k: artifact[k] for k in ("sha256", "size_bytes")},
        "variant_identity_mismatch",
    )
    extract_bounded(
        archive, destination, sha256=artifact["sha256"], max_bytes=max_expanded_bytes
    )
    original = open_and_validate(
        source, repo_id="fs2/client-source", selection=request.selection
    )
    # The worker's output validation deliberately does not impose generation
    # bounds on untouched episodes/cameras; compare_variant validates every row.
    produced = open_and_validate(
        destination,
        repo_id="fs2/client-output",
        selection=request.selection,
        generation_bounds=False,
    )
    provenance_path = destination / "meta/fs2-augmentation-provenance.json"
    check(
        sha256_file(provenance_path) == variant["provenance_sha256"],
        "provenance_digest_mismatch",
    )
    provenance = json.loads(provenance_path.read_text())
    replacements = {
        (episode, camera)
        for episode in original.selected_episodes
        for camera in original.selected_cameras
    }
    checked = compare_variant(
        original, produced, replacements=replacements, provenance=provenance
    )
    require_visual_changes(checked)
    result = {
        "reader": "lerobot==0.6.1",
        "source_tree_sha256": original.tree_sha256,
        "output_tree_sha256": produced.tree_sha256,
        "validation": checked,
        "provenance_sha256": variant["provenance_sha256"],
        "physical_alignment_verified": False,
    }
    write_json(receipt_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("prepare")
    pack.add_argument("--dataset", type=Path, required=True)
    pack.add_argument("--request", type=Path, required=True)
    pack.add_argument("--output", type=Path, required=True)
    pack.add_argument("--max-bytes", type=int, default=5 * 1024**3)
    pack.add_argument("--max-expanded-bytes", type=int, default=8 * 1024**3)
    verify = commands.add_parser("validate")
    for name in (
        "source",
        "archive",
        "destination",
        "parameters",
        "variant",
        "receipt",
    ):
        verify.add_argument("--" + name, type=Path, required=True)
    verify.add_argument("--max-bytes", type=int, default=5 * 1024**3)
    verify.add_argument("--max-expanded-bytes", type=int, default=8 * 1024**3)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        check(
            0 < args.max_bytes <= 5 * 1024**3
            and 0 < args.max_expanded_bytes <= 8 * 1024**3,
            "bounded_size_required",
        )
        if args.command == "prepare":
            result = prepare(
                args.dataset,
                args.request,
                args.output,
                max_bytes=args.max_bytes,
                max_expanded_bytes=args.max_expanded_bytes,
            )
        else:
            result = validate(
                args.source,
                args.archive,
                args.destination,
                args.parameters,
                args.variant,
                args.receipt,
                max_bytes=args.max_bytes,
                max_expanded_bytes=args.max_expanded_bytes,
            )
        print(json.dumps({"outcome": "passed", "reader": result["reader"]}))
    except Exception as error:
        # No arbitrary exception text: it may contain caller paths or data.
        code = (
            str(error)
            if re.fullmatch(r"[a-z0-9_]+", str(error))
            else type(error).__name__
        )
        print(
            json.dumps(
                {"outcome": "failed", "error_type": type(error).__name__, "code": code}
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

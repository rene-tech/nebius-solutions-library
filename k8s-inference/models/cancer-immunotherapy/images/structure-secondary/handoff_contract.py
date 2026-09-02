#!/usr/bin/env python3
"""Relocatable, content-bound two-file stage handoffs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile


HANDOFF_SCHEMA = "fs2-serve.nebius.ai/relocatable-stage-handoff/v1"
ARTIFACT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _artifact_id(value: str) -> str:
    if ARTIFACT_ID_PATTERN.fullmatch(value) is None:
        raise SystemExit("output/input artifact ID is not a valid bounded logical ID")
    return value


def _absolute(path: str | Path, label: str, *, existing: bool) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or (existing and not candidate.is_file()):
        suffix = " existing file" if existing else " path"
        raise SystemExit(f"{label} must be an absolute{suffix}: {candidate}")
    return candidate


def write_handoff(
    source: Path,
    processed_json: Path,
    provenance_marker: Path,
    handoff_tar: Path,
    artifact_id: str,
) -> dict[str, str]:
    """Write canonical files plus a deterministic zstd-compressed tar."""
    source = _absolute(source, "processed source", existing=True)
    processed_json = _absolute(processed_json, "processed-json", existing=False)
    provenance_marker = _absolute(
        provenance_marker, "provenance-marker", existing=False
    )
    handoff_tar = _absolute(handoff_tar, "handoff-tar", existing=False)
    artifact_id = _artifact_id(artifact_id)
    if len({processed_json, provenance_marker, handoff_tar}) != 3:
        raise SystemExit("processed-json, provenance-marker, and handoff-tar must differ")
    processed_json.parent.mkdir(parents=True, exist_ok=True)
    provenance_marker.parent.mkdir(parents=True, exist_ok=True)
    handoff_tar.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != processed_json.resolve():
        processed_json.write_bytes(source.read_bytes())
    marker = {
        "schema": HANDOFF_SCHEMA,
        "artifact_id": artifact_id,
        "member": "processed.json",
        "sha256": sha256_file(processed_json),
    }
    provenance_marker.write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    write_archive(
        handoff_tar,
        {"processed.json": processed_json, "provenance.json": provenance_marker},
    )
    return marker


def write_archive(handoff_tar: Path, members: dict[str, Path]) -> None:
    """Write an exact, deterministic zstd tar from canonical member names."""
    handoff_tar = _absolute(handoff_tar, "handoff-tar", existing=False)
    if not members or any(
        not name or Path(name).is_absolute() or ".." in Path(name).parts
        for name in members
    ):
        raise SystemExit("handoff archive member names must be safe and relative")
    if any(not path.is_file() for path in members.values()):
        raise SystemExit("handoff archive members must be existing files")
    handoff_tar.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=".fs2-handoff-", suffix=".tar", dir=handoff_tar.parent, delete=False
    ) as temporary:
        raw_tar = Path(temporary.name)
    try:
        with tarfile.open(raw_tar, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for member_name, path in members.items():
                info = tarfile.TarInfo(member_name)
                info.size = path.stat().st_size
                info.mode = 0o444
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
        completed = subprocess.run(
            ["zstd", "-q", "-f", "--threads=1", "-o", str(handoff_tar), str(raw_tar)],
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit(completed.returncode)
    finally:
        raw_tar.unlink(missing_ok=True)


def validate_handoff(
    processed_json: Path, provenance_marker: Path, artifact_id: str
) -> dict[str, str]:
    processed_json = _absolute(processed_json, "processed-json", existing=True)
    provenance_marker = _absolute(
        provenance_marker, "provenance-marker", existing=True
    )
    artifact_id = _artifact_id(artifact_id)
    try:
        marker = json.loads(provenance_marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"provenance-marker is invalid JSON: {exc}") from exc
    expected = {
        "schema": HANDOFF_SCHEMA,
        "artifact_id": artifact_id,
        "member": "processed.json",
        "sha256": sha256_file(processed_json),
    }
    if marker != expected:
        raise SystemExit("provenance-marker does not bind the relocated processed.json")
    return expected

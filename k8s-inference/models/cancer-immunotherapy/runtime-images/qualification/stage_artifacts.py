#!/usr/bin/env python3
"""Stage exact, external model artifacts for H100 qualification."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(os.environ.get("FS2_ARTIFACT_ROOT", "/artifacts"))
ARTIFACTS = [
    ("nvidia/NV-Proteina-Complexa-Protein-Target-160M-v1", "ffed199e32612b98ffa04f4640d34d37b137fca5", "complexa.ckpt", "model", ROOT / "proteina-complexa"),
    ("nvidia/NV-Proteina-Complexa-Protein-Target-160M-v1", "ffed199e32612b98ffa04f4640d34d37b137fca5", "complexa_ae.ckpt", "model", ROOT / "proteina-complexa"),
    ("boltzgen/boltzgen-1", "c1be29e1f82ffcc72264f64b993c43fb4e0d17f0", "boltzgen1_diverse.ckpt", "model", ROOT / "boltzgen"),
    ("boltzgen/inference-data", "c3d36fd276e9caf098c75d4113c6d5eb320b1a4c", "mols.zip", "dataset", ROOT / "boltzgen"),
]
EXPECTED = {
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(repository: str, revision: str, filename: str, repository_type: str, directory: Path) -> Path:
    prefix = "datasets/" if repository_type == "dataset" else ""
    url = f"https://huggingface.co/{prefix}{repository}/resolve/{revision}/{filename}?download=true"
    target = directory / filename
    if target.is_file():
        return target
    partial = target.with_suffix(target.suffix + ".partial")
    request = urllib.request.Request(url, headers={"User-Agent": "fs2-runtime-qualification/1"})
    with urllib.request.urlopen(request, timeout=300) as response, partial.open("wb") as stream:
        while chunk := response.read(8 * 1024 * 1024):
            stream.write(chunk)
    os.replace(partial, target)
    return target


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    records = []
    for repository, revision, filename, repository_type, directory in ARTIFACTS:
        directory.mkdir(parents=True, exist_ok=True)
        path = download(repository, revision, filename, repository_type, directory)
        relative = path.relative_to(ROOT).as_posix()
        digest = sha256(path)
        if relative in EXPECTED and digest != EXPECTED[relative]:
            raise SystemExit(f"artifact digest mismatch: {relative}")
        records.append({
            "repository": repository,
            "revision": revision,
            "path": relative,
            "bytes": path.stat().st_size,
            "sha256": digest,
        })

    boltzgen_dir = ROOT / "boltzgen"
    if not (boltzgen_dir / "mols").is_dir():
        with zipfile.ZipFile(boltzgen_dir / "mols.zip") as archive:
            archive.extractall(boltzgen_dir)
    receipt = {
        "schema": "fs2.nebius.ai/scientific-runtime-external-artifact-receipt/v1",
        "weight_policy": "external-pvc-not-container-image",
        "artifacts": sorted(records, key=lambda item: item["path"]),
    }
    output = ROOT / "qualification" / "artifact-receipt.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()

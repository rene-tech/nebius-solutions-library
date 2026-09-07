#!/usr/bin/env python3
"""Materialize the pinned public PDB70 FASTA as an MMseqs2 database."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path


SOURCE_URL = "https://wwwuser.gwdg.de/~compbiol/colabfold/pdb70_220313.fasta.gz"
SOURCE_BYTES = 21_231_545
SOURCE_SHA256 = "3e075127dd90ee4e44635eb1596cc74f17c46bfdca470d4e9ffcc79df5567ca9"
FASTA_BYTES = 38_115_370
FASTA_SHA256 = "ee828baf233c8737406f15447acb4fd45fa23b96b9750e460cb137149319f2bd"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def materialize(destination: Path, *, source_url: str = SOURCE_URL) -> dict[str, object]:
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "pdb70_220313.fasta.gz"
    fasta = destination / "pdb70_220313.fasta"
    database = destination / "pdb70_220313"

    with urllib.request.urlopen(source_url, timeout=120) as response, archive.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)
    if archive.stat().st_size != SOURCE_BYTES or _sha256(archive) != SOURCE_SHA256:
        raise RuntimeError("PDB70 compressed source identity mismatch")

    with gzip.open(archive, "rb") as source, fasta.open("wb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)
    if fasta.stat().st_size != FASTA_BYTES or _sha256(fasta) != FASTA_SHA256:
        raise RuntimeError("PDB70 expanded FASTA identity mismatch")

    subprocess.run(
        ["mmseqs", "createdb", str(fasta), str(database)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    fasta.unlink()
    archive.unlink()
    manifest: dict[str, object] = {
        "schema": "fs2-pdb70-mmseqs2-database/v1",
        "database": "pdb70_220313",
        "source_url": source_url,
        "source_bytes": SOURCE_BYTES,
        "source_sha256": SOURCE_SHA256,
        "expanded_fasta_bytes": FASTA_BYTES,
        "expanded_fasta_sha256": FASTA_SHA256,
        "sequence_count": 92_110,
        "license": "CC-BY-4.0",
        "mmseqs_version": subprocess.run(
            ["mmseqs", "version"], check=True, capture_output=True, text=True
        ).stdout.strip(),
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(materialize(args.destination), sort_keys=True))


if __name__ == "__main__":
    main()

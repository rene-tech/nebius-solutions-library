"""Local, revision-pinned MMseqs2 search service for public PDB70 220313.

This is an open implementation of the canonical PDB70 operation and database,
not the NVIDIA NIM package.  Its identity and runtime parity stay explicit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator


LOGGER = logging.getLogger("fs2.msa_search_pdb70")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

DATABASE_NAME = "pdb70_220313"
DATABASE_ROOT = Path(os.getenv("PDB70_DATABASE_ROOT", "/opt/pdb70"))
DATABASE = DATABASE_ROOT / DATABASE_NAME
DATABASE_MANIFEST = DATABASE_ROOT / "manifest.json"
CACHE = Path(os.getenv("MSA_CACHE", "/cache"))
MMSEQS = os.getenv("MMSEQS", "mmseqs")
MMSEQS_THREADS = max(1, int(os.getenv("MMSEQS_THREADS", "8")))
MAX_RECORDS = 128
AMINO_ACIDS = re.compile(r"^[ACDEFGHIKLMNPQRSTVWY]+$")


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: str = Field(min_length=6, max_length=4096)
    databases: list[str]
    max_msa_sequences: int = Field(default=500, ge=1, le=5000)
    output_alignment_formats: list[str]

    @field_validator("sequence")
    @classmethod
    def normalize_sequence(cls, value: str) -> str:
        sequence = "".join(value.split()).upper()
        if not AMINO_ACIDS.fullmatch(sequence):
            raise ValueError("sequence must contain canonical protein residues")
        return sequence

    @field_validator("databases")
    @classmethod
    def validate_databases(cls, value: list[str]) -> list[str]:
        if value != [DATABASE_NAME]:
            raise ValueError(f"only {DATABASE_NAME} is supported")
        return value

    @field_validator("output_alignment_formats")
    @classmethod
    def validate_formats(cls, value: list[str]) -> list[str]:
        if value != ["a3m"]:
            raise ValueError("only A3M output is supported")
        return value


class Runtime:
    requests = 0
    failures = 0
    cache_hits = 0
    lock = asyncio.Lock()


RUNTIME = Runtime()
app = FastAPI(title="FS2 local PDB70 MMseqs2", version="220313")


def _manifest() -> dict[str, Any]:
    value = json.loads(DATABASE_MANIFEST.read_text(encoding="utf-8"))
    if value.get("source_sha256") != "3e075127dd90ee4e44635eb1596cc74f17c46bfdca470d4e9ffcc79df5567ca9":
        raise RuntimeError("PDB70 manifest identity mismatch")
    return value


def _check_database() -> None:
    _manifest()
    for suffix in ("", ".dbtype", ".index", "_h", "_h.dbtype", "_h.index"):
        if not Path(f"{DATABASE}{suffix}").is_file():
            raise RuntimeError(f"PDB70 database component missing: {suffix or 'data'}")


def _parse_records(alignment: str) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
    header: str | None = None
    sequence: list[str] = []
    for raw_line in alignment.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(">"):
            if header is not None and sequence:
                records.append((header, "".join(sequence)))
            header = line[1:].strip() or "unnamed"
            sequence = []
        elif header is not None:
            sequence.append(line)
    if header is not None and sequence:
        records.append((header, "".join(sequence)))
    return records


def _search_local(sequence: str, max_sequences: int) -> str:
    with tempfile.TemporaryDirectory(prefix="fs2-pdb70-") as temporary:
        root = Path(temporary)
        (root / "query.fasta").write_text(f">query\n{sequence}\n", encoding="ascii")
        commands = (
            [MMSEQS, "createdb", "query.fasta", "querydb"],
            [
                MMSEQS,
                "search",
                "querydb",
                str(DATABASE),
                "resultdb",
                "tmp",
                "--max-seqs",
                str(max(max_sequences, MAX_RECORDS)),
                "--threads",
                str(MMSEQS_THREADS),
                "-s",
                "7.5",
                "-e",
                "10",
            ],
            [
                MMSEQS,
                "result2msa",
                "querydb",
                str(DATABASE),
                "resultdb",
                "msadb",
                "--msa-format-mode",
                "5",
                "--threads",
                str(MMSEQS_THREADS),
            ],
            [
                MMSEQS,
                "unpackdb",
                "msadb",
                "unpack",
                "--unpack-name-mode",
                "0",
                "--unpack-suffix",
                ".a3m",
            ],
        )
        (root / "unpack").mkdir()
        for command in commands:
            subprocess.run(
                command,
                cwd=root,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=900,
            )
        records = _parse_records((root / "unpack" / "0.a3m").read_text(encoding="utf-8"))
    if not records or records[0][1].replace("-", "").upper() != sequence:
        raise RuntimeError("MMseqs2 PDB70 result did not echo the query")
    usable = [records[0], *[record for record in records[1:] if record[1].replace("-", "")]]
    if len(usable) < MAX_RECORDS:
        raise RuntimeError(f"PDB70 returned only {len(usable)} usable records")
    return "".join(f">{header}\n{aligned}\n" for header, aligned in usable[:MAX_RECORDS])


def _cached_search(sequence: str, max_sequences: int) -> str:
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(
        f"{DATABASE_NAME}\0{max_sequences}\0{sequence}".encode("ascii")
    ).hexdigest()
    path = CACHE / f"{cache_key}.a3m"
    if path.is_file():
        RUNTIME.cache_hits += 1
        return path.read_text(encoding="utf-8")
    alignment = _search_local(sequence, max_sequences)
    temporary = CACHE / f".{cache_key}.{os.getpid()}.tmp"
    temporary.write_text(alignment, encoding="utf-8")
    temporary.replace(path)
    return alignment


@app.on_event("startup")
def startup() -> None:
    started = time.monotonic()
    _check_database()
    LOGGER.info(
        "local PDB70 runtime ready database_sha256=%s mmseqs=%s startup_seconds=%.3f",
        _manifest()["source_sha256"],
        _manifest()["mmseqs_version"],
        time.monotonic() - started,
    )


@app.get("/healthz")
@app.get("/v1/health/live")
def live() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/readyz")
@app.get("/v1/health/ready")
def ready() -> dict[str, Any]:
    manifest = _manifest()
    return {
        "status": "ready",
        "model": "msa-search-pdb70",
        "runtime": "mmseqs2-local-open-runtime",
        "relationship": "exact-public-database-independent-runtime",
        "nim_package_parity": "unverified",
        "database": DATABASE_NAME,
        "database_sha256": manifest["source_sha256"],
        "sequence_count": manifest["sequence_count"],
    }


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    return "\n".join(
        (
            "# TYPE fs2_model_requests_total counter",
            f'fs2_model_requests_total{{model="msa-search-pdb70"}} {RUNTIME.requests}',
            "# TYPE fs2_model_failures_total counter",
            f'fs2_model_failures_total{{model="msa-search-pdb70"}} {RUNTIME.failures}',
            "# TYPE fs2_model_cache_hits_total counter",
            f'fs2_model_cache_hits_total{{model="msa-search-pdb70"}} {RUNTIME.cache_hits}',
            "",
        )
    )


@app.post("/biology/colabfold/msa-search/predict")
async def search(request: SearchRequest) -> dict[str, Any]:
    RUNTIME.requests += 1
    try:
        async with RUNTIME.lock:
            alignment = await asyncio.to_thread(
                _cached_search, request.sequence, request.max_msa_sequences
            )
        return {
            # `colabfold` is the frozen native protocol value. Readiness and
            # image provenance identify this as the local MMseqs2 runtime.
            "metrics": {"search_type": "colabfold"},
            "alignments": {
                DATABASE_NAME: {"a3m": {"alignment": alignment, "format": "a3m"}}
            },
        }
    except Exception as exc:
        RUNTIME.failures += 1
        LOGGER.exception("local PDB70 search failed")
        raise HTTPException(status_code=500, detail=type(exc).__name__) from exc

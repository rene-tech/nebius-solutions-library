"""Cached HTTP adapter for the public ColabFold MMseqs2 service.

This is a capability-equivalent fallback, not the NVIDIA PDB70 database artifact.
The response retains the frozen NIM shape so existing clients can call it while
readiness and logs identify the actual upstream database family.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import random
import re
import tarfile
import time
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator


LOGGER = logging.getLogger("fs2.msa_search")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

SOURCE_REVISION = os.environ["COLABFOLD_SOURCE_REVISION"]
UPSTREAM = os.getenv("COLABFOLD_API", "https://api.colabfold.com").rstrip("/")
CACHE = Path(os.getenv("MSA_CACHE", "/cache"))
DATABASE = "pdb70_220313"
ACTUAL_DATABASES = "UniRef30 plus ColabFold environmental databases"
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
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
        if value != [DATABASE]:
            raise ValueError(f"only the compatibility name {DATABASE} is supported")
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
    upstream_calls = 0
    lock = asyncio.Lock()


RUNTIME = Runtime()
app = FastAPI(title="FS2 ColabFold MSA fallback", version=SOURCE_REVISION[:12])


def _session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = "fs2-serve/0.1 (+https://github.com/nebius)"
    session.trust_env = False
    return session


def _status_value(response: requests.Response) -> dict[str, Any]:
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        raise RuntimeError("invalid ColabFold status response")
    return value


def _download(session: requests.Session, ticket_id: str) -> bytes:
    with session.get(
        f"{UPSTREAM}/result/download/{ticket_id}", timeout=(10, 120), stream=True
    ) as response:
        response.raise_for_status()
        payload = bytearray()
        for chunk in response.iter_content(1024 * 1024):
            payload.extend(chunk)
            if len(payload) > MAX_DOWNLOAD_BYTES:
                raise RuntimeError("ColabFold result exceeded 64 MiB")
        return bytes(payload)


def _tar_members(payload: bytes) -> dict[str, str]:
    wanted = {
        "uniref.a3m",
        "bfd.mgnify30.metaeuk30.smag30.a3m",
    }
    found: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            name = Path(member.name).name
            if name not in wanted or not member.isfile() or member.size > 32 * 1024 * 1024:
                continue
            source = archive.extractfile(member)
            if source is None:
                continue
            found[name] = source.read().decode("utf-8", errors="strict")
    if "uniref.a3m" not in found:
        raise RuntimeError("ColabFold result omitted uniref.a3m")
    return found


def _parse_records(alignment: str) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
    header: str | None = None
    sequence: list[str] = []
    for raw_line in alignment.replace("\x00", "\n").splitlines():
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


def _uppercase(sequence: str) -> str:
    return "".join(character for character in sequence if character.isupper())


def _combine(sequence: str, members: dict[str, str]) -> str:
    records: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for name in ("uniref.a3m", "bfd.mgnify30.metaeuk30.smag30.a3m"):
        for header, aligned in _parse_records(members.get(name, "")):
            key = (header, aligned)
            if key not in seen:
                seen.add(key)
                records.append(key)
    query_index = next(
        (index for index, (_, aligned) in enumerate(records) if _uppercase(aligned) == sequence),
        None,
    )
    if query_index is None:
        raise RuntimeError("ColabFold result did not echo the query")
    query = records.pop(query_index)
    records.insert(0, ("query", query[1]))
    nonempty = [records[0]] + [record for record in records[1:] if _uppercase(record[1])]
    if len(nonempty) < MAX_RECORDS:
        raise RuntimeError(f"ColabFold returned only {len(nonempty)} usable records")
    return "\n".join(
        line
        for header, aligned in nonempty[:MAX_RECORDS]
        for line in (f">{header}", aligned)
    ) + "\n"


def _search_upstream(sequence: str) -> str:
    RUNTIME.upstream_calls += 1
    query = f">101\n{sequence}\n"
    with _session() as session:
        deadline = time.monotonic() + 900
        ticket: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            response = session.post(
                f"{UPSTREAM}/ticket/msa",
                data={"q": query, "mode": "env"},
                timeout=(10, 30),
            )
            ticket = _status_value(response)
            if ticket["status"] not in {"UNKNOWN", "RATELIMIT"}:
                break
            time.sleep(5 + random.randint(0, 3))
        if ticket is None or ticket["status"] in {"ERROR", "MAINTENANCE"}:
            raise RuntimeError(f"ColabFold submission failed: {ticket}")
        ticket_id = ticket.get("id")
        if not isinstance(ticket_id, str) or not ticket_id:
            raise RuntimeError("ColabFold submission omitted ticket ID")
        while ticket["status"] in {"UNKNOWN", "RUNNING", "PENDING"}:
            if time.monotonic() >= deadline:
                raise TimeoutError("ColabFold search exceeded 900 seconds")
            time.sleep(5 + random.randint(0, 3))
            ticket = _status_value(
                session.get(f"{UPSTREAM}/ticket/{ticket_id}", timeout=(10, 30))
            )
        if ticket["status"] != "COMPLETE":
            raise RuntimeError(f"ColabFold search ended in {ticket['status']}")
        return _combine(sequence, _tar_members(_download(session, ticket_id)))


def _cached_search(sequence: str) -> str:
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(sequence.encode("ascii")).hexdigest()
    path = CACHE / f"{key}.a3m"
    if path.is_file():
        RUNTIME.cache_hits += 1
        return path.read_text(encoding="utf-8")
    alignment = _search_upstream(sequence)
    temporary = CACHE / f".{key}.{os.getpid()}.tmp"
    temporary.write_text(alignment, encoding="utf-8")
    temporary.replace(path)
    return alignment


@app.get("/v1/health/ready")
def ready() -> dict[str, Any]:
    return {
        "status": "ready",
        "model": "msa-search-pdb70",
        "runtime": "colabfold-mmseqs2-federated-fallback",
        "source_revision": SOURCE_REVISION,
        "identity_relationship": "capability-equivalent-non-alias",
        "actual_databases": ACTUAL_DATABASES,
        "upstream": UPSTREAM,
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
            "# TYPE fs2_model_upstream_calls_total counter",
            f'fs2_model_upstream_calls_total{{model="msa-search-pdb70"}} {RUNTIME.upstream_calls}',
            "",
        )
    )


@app.post("/biology/colabfold/msa-search/predict")
async def search(request: SearchRequest) -> dict[str, Any]:
    RUNTIME.requests += 1
    try:
        async with RUNTIME.lock:
            alignment = await asyncio.to_thread(_cached_search, request.sequence)
        return {
            "metrics": {"search_type": "colabfold"},
            "alignments": {
                DATABASE: {"a3m": {"alignment": alignment, "format": "a3m"}}
            },
        }
    except Exception as exc:
        RUNTIME.failures += 1
        LOGGER.exception("ColabFold MSA search failed")
        raise HTTPException(status_code=502, detail=type(exc).__name__) from exc

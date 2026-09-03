#!/usr/bin/env python3
"""Resumable, checksum-first ingestion of pinned public scientific checkpoints.

The unit of work is one contracted file. A file is only ever published under its
contracted name once its full byte stream has hashed to the contracted SHA-256,
so a consumer that sees the name sees verified bytes and nothing else. Partial
downloads live beside it under a ``.part`` suffix and are resumed, never
restarted, because the smallest artifact here is 1.7 GiB and the claim this
stages into has no room for a second copy of anything.

Standard library only: this runs in a stock ``python:3.11-slim`` with no
package installation step, which keeps the staging job free of a wheel-resolution
failure mode on a path whose whole job is to be dependable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

SCHEMA = "fs2-serve.nebius.ai/scientific-file-ingestion/v1"
RECEIPT_SCHEMA = "fs2-serve.nebius.ai/scientific-ingestion-receipt/v1"
USER_AGENT = "fs2-serve-scientific-ingestion/1 (+https://github.com/rene-tech/nebius-solutions-library)"
PART_SUFFIX = ".part"
CHUNK = 8 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
HF_HOST = "https://huggingface.co"


class IngestionError(RuntimeError):
    """A contract, transport, or identity failure that must stop publication."""


@dataclass
class FileOutcome:
    path: str
    bytes: int
    sha256: str
    state: str
    downloaded_bytes: int = 0
    resumed_from: int = 0
    attempts: int = 1
    seconds: float = 0.0


@dataclass
class ArtifactOutcome:
    artifact_id: str
    staged_at: str
    files: list[FileOutcome] = field(default_factory=list)


def load_contract(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text())
    if document.get("schema") != SCHEMA:
        raise IngestionError(f"{path}: unexpected schema {document.get('schema')!r}")
    seen: set[str] = set()
    for artifact in document.get("artifacts", []):
        artifact_id = artifact.get("artifact_id")
        if not artifact_id or artifact_id in seen:
            raise IngestionError(f"{path}: missing or duplicate artifact_id {artifact_id!r}")
        seen.add(artifact_id)
        if artifact.get("transform") != "fetch-files":
            raise IngestionError(f"{artifact_id}: transform must be fetch-files")
        files = artifact.get("files") or []
        if not files:
            raise IngestionError(f"{artifact_id}: declares no files")
        pattern = re.compile(artifact["tree"]["entry_path_pattern"])
        total = 0
        for entry in files:
            name = entry.get("path", "")
            # A contracted name is one path segment. Anything else would let a
            # contract edit write outside the artifact's own staging directory.
            if not name or "/" in name or name in (".", "..") or name.endswith(PART_SUFFIX):
                raise IngestionError(f"{artifact_id}: illegal file path {name!r}")
            if not pattern.fullmatch(name):
                raise IngestionError(f"{artifact_id}: {name!r} is outside entry_path_pattern")
            if not SHA256.fullmatch(entry.get("sha256", "")):
                raise IngestionError(f"{artifact_id}/{name}: sha256 must be 64 lowercase hex characters")
            if not isinstance(entry.get("bytes"), int) or entry["bytes"] <= 0:
                raise IngestionError(f"{artifact_id}/{name}: bytes must be a positive integer")
            total += entry["bytes"]
        tree = artifact["tree"]
        if tree.get("entry_count") != len(files):
            raise IngestionError(f"{artifact_id}: entry_count disagrees with the declared file list")
        if tree.get("total_bytes") != total:
            raise IngestionError(f"{artifact_id}: total_bytes disagrees with the declared file sizes")
    return document


def selected(document: dict[str, Any], artifact_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    artifacts = document["artifacts"]
    if not artifact_ids:
        return list(artifacts)
    by_id = {artifact["artifact_id"]: artifact for artifact in artifacts}
    missing = [artifact_id for artifact_id in artifact_ids if artifact_id not in by_id]
    if missing:
        raise IngestionError(f"contract declares no artifact {', '.join(missing)}")
    return [by_id[artifact_id] for artifact_id in artifact_ids]


def source_url(artifact: dict[str, Any], filename: str) -> str:
    source = artifact["source"]
    resolver = source["resolver"]
    if resolver == "huggingface-resolve":
        repo = source["uri"].removeprefix("hf://")
        return f"{HF_HOST}/{repo}/resolve/{source['revision']}/{filename}"
    if resolver == "direct-https":
        base = source["uri"]
        if not base.startswith("https://"):
            raise IngestionError(f"{artifact['artifact_id']}: direct-https source must be https")
        return f"{base.rstrip('/')}/{filename}"
    raise IngestionError(f"{artifact['artifact_id']}: unknown resolver {resolver!r}")


def hash_prefix(path: Path, length: int) -> hashlib._Hash:
    """Rehash bytes already on disk so a resumed stream keeps one running digest."""

    digest = hashlib.sha256()
    remaining = length
    with path.open("rb") as handle:
        while remaining:
            block = handle.read(min(CHUNK, remaining))
            if not block:
                raise IngestionError(f"{path}: shorter than the {length} bytes reported by stat")
            digest.update(block)
            remaining -= len(block)
    return digest


def verified(path: Path, expected_bytes: int, expected_sha256: str) -> bool:
    if not path.is_file() or path.stat().st_size != expected_bytes:
        return False
    return hash_prefix(path, expected_bytes).hexdigest() == expected_sha256


def _open(url: str, offset: int, timeout: float) -> Any:
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def _validate_range(response: Any, offset: int, expected_bytes: int, url: str) -> None:
    status = response.status
    if offset == 0:
        if status != 200:
            raise IngestionError(f"{url}: expected 200 for a fresh download, got {status}")
        declared = response.headers.get("Content-Length")
        if declared is not None and int(declared) != expected_bytes:
            raise IngestionError(f"{url}: upstream is {declared} bytes, contract says {expected_bytes}")
        return
    if status != 206:
        # Silently restarting here would be the expensive mistake: it throws away
        # gigabytes and, worse, would append a whole second copy to the part file.
        raise IngestionError(f"{url}: resume from {offset} was answered {status}, not 206")
    content_range = response.headers.get("Content-Range", "")
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range.strip())
    if not match:
        raise IngestionError(f"{url}: unparseable Content-Range {content_range!r}")
    start, _, total = (int(value) for value in match.groups())
    if start != offset:
        raise IngestionError(f"{url}: asked to resume at {offset}, server started at {start}")
    if total != expected_bytes:
        raise IngestionError(f"{url}: upstream is {total} bytes, contract says {expected_bytes}")


def fetch_file(
    url: str,
    destination: Path,
    expected_bytes: int,
    expected_sha256: str,
    *,
    retries: int,
    timeout: float,
    log: Any,
) -> FileOutcome:
    started = time.monotonic()
    if verified(destination, expected_bytes, expected_sha256):
        log(f"  {destination.name}: already published and verified, skipping")
        return FileOutcome(destination.name, expected_bytes, expected_sha256, "already-verified",
                           seconds=round(time.monotonic() - started, 3))

    part = destination.with_name(destination.name + PART_SUFFIX)
    resumed_from = 0
    downloaded = 0
    attempt = 0
    last_error: Exception | None = None

    while attempt < retries:
        attempt += 1
        offset = part.stat().st_size if part.is_file() else 0
        if offset > expected_bytes:
            log(f"  {destination.name}: partial file is longer than the contract, discarding")
            part.unlink()
            offset = 0
        digest = hash_prefix(part, offset) if offset else hashlib.sha256()
        if attempt == 1:
            resumed_from = offset
        if offset == expected_bytes:
            pass  # Nothing left to transfer; fall through to verification.
        else:
            try:
                with _open(url, offset, timeout) as response:
                    _validate_range(response, offset, expected_bytes, url)
                    mode = "r+b" if offset else "wb"
                    with part.open(mode) as handle:
                        handle.seek(offset)
                        while True:
                            block = response.read(CHUNK)
                            if not block:
                                break
                            handle.write(block)
                            digest.update(block)
                            offset += len(block)
                            downloaded += len(block)
                        handle.flush()
                        os.fsync(handle.fileno())
            except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError, OSError) as error:
                last_error = error
                # ENOSPC is not transient and retrying it just burns the claim.
                if isinstance(error, OSError) and error.errno == 28:
                    raise IngestionError(f"{destination}: no space left on the staging claim") from error
                backoff = min(30.0, 2.0 ** attempt)
                log(f"  {destination.name}: attempt {attempt} failed at {offset}/{expected_bytes} ({error}); retrying in {backoff:.0f}s")
                time.sleep(backoff)
                continue

        if offset != expected_bytes:
            last_error = IngestionError(f"stream ended at {offset}, contract says {expected_bytes}")
            log(f"  {destination.name}: attempt {attempt} short by {expected_bytes - offset} bytes; resuming")
            continue

        actual = digest.hexdigest()
        if actual != expected_sha256:
            # Identity failure is never resumable: the bytes on disk are wrong.
            part.unlink(missing_ok=True)
            raise IngestionError(
                f"{destination}: sha256 {actual} does not match contracted {expected_sha256}; discarded"
            )
        os.replace(part, destination)
        seconds = time.monotonic() - started
        rate = (downloaded / seconds / 1e6) if seconds > 0 and downloaded else 0.0
        log(f"  {destination.name}: verified {expected_bytes} bytes in {seconds:.1f}s ({rate:.0f} MB/s)")
        return FileOutcome(destination.name, expected_bytes, expected_sha256, "downloaded",
                           downloaded_bytes=downloaded, resumed_from=resumed_from, attempts=attempt,
                           seconds=round(seconds, 3))

    raise IngestionError(f"{destination}: exhausted {retries} attempts; last error: {last_error}")


def stage_artifact(
    artifact: dict[str, Any],
    staging_root: Path,
    *,
    retries: int,
    timeout: float,
    log: Any,
) -> ArtifactOutcome:
    artifact_id = artifact["artifact_id"]
    directory = staging_root / artifact_id
    directory.mkdir(parents=True, exist_ok=True)
    log(f"{artifact_id}: staging {artifact['tree']['entry_count']} file(s), "
        f"{artifact['tree']['total_bytes']} bytes into {directory}")
    outcome = ArtifactOutcome(artifact_id=artifact_id, staged_at=_now())
    for entry in artifact["files"]:
        outcome.files.append(
            fetch_file(
                source_url(artifact, entry["path"]),
                directory / entry["path"],
                entry["bytes"],
                entry["sha256"],
                retries=retries,
                timeout=timeout,
                log=log,
            )
        )
    return outcome


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def receipt_document(document: dict[str, Any], outcomes: list[ArtifactOutcome], staging_root: Path,
                     namespace: str, claim: str, sub_path: str) -> dict[str, Any]:
    by_id = {artifact["artifact_id"]: artifact for artifact in document["artifacts"]}
    return {
        "schema": RECEIPT_SCHEMA,
        "generated_at": _now(),
        "staging": {
            "namespace": namespace,
            "claim": claim,
            "sub_path": sub_path,
            "root": str(staging_root),
            "visibility": "task-private",
        },
        "artifacts": [
            {
                "artifact_id": outcome.artifact_id,
                "staged_at": outcome.staged_at,
                "source": by_id[outcome.artifact_id]["source"],
                "file_count": len(outcome.files),
                "total_bytes": sum(entry.bytes for entry in outcome.files),
                "files": [
                    {
                        "path": entry.path,
                        "bytes": entry.bytes,
                        "sha256": entry.sha256,
                        "state": entry.state,
                        "downloaded_bytes": entry.downloaded_bytes,
                        "resumed_from": entry.resumed_from,
                        "attempts": entry.attempts,
                        "seconds": entry.seconds,
                    }
                    for entry in outcome.files
                ],
            }
            for outcome in outcomes
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--artifact-id", action="append", default=[], dest="artifact_ids")
    parser.add_argument("--staging-root", type=Path, required=True,
                        help="task-owned private directory the verified files are published into")
    parser.add_argument("--receipt", type=Path, help="where the machine-readable staging receipt is written")
    parser.add_argument("--namespace", default="", help="recorded in the receipt, not used to reach the cluster")
    parser.add_argument("--claim", default="", help="recorded in the receipt")
    parser.add_argument("--sub-path", default="", help="the claim sub-path the staging root corresponds to")
    parser.add_argument("--retries", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--continue-on-artifact-error", action="store_true",
                        help="keep staging later artifacts when one fails; the receipt records the failure")
    options = parser.parse_args(argv)

    def log(message: str) -> None:
        print(message, flush=True)

    document = load_contract(options.contract)
    artifacts = selected(document, tuple(options.artifact_ids))
    options.staging_root.mkdir(parents=True, exist_ok=True)

    outcomes: list[ArtifactOutcome] = []
    failures: list[tuple[str, str]] = []
    for artifact in artifacts:
        try:
            outcomes.append(stage_artifact(artifact, options.staging_root,
                                           retries=options.retries, timeout=options.timeout, log=log))
        except IngestionError as error:
            failures.append((artifact["artifact_id"], str(error)))
            log(f"{artifact['artifact_id']}: FAILED {error}")
            if not options.continue_on_artifact_error:
                break

    if options.receipt:
        receipt = receipt_document(document, outcomes, options.staging_root,
                                   options.namespace, options.claim, options.sub_path)
        if failures:
            receipt["failures"] = [{"artifact_id": artifact_id, "error": error} for artifact_id, error in failures]
        options.receipt.parent.mkdir(parents=True, exist_ok=True)
        options.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        log(f"receipt written to {options.receipt}")

    staged = sum(len(outcome.files) for outcome in outcomes)
    log(f"staged {staged} verified file(s) across {len(outcomes)} artifact(s); {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except IngestionError as error:
        print(f"error: {error}", file=sys.stderr, flush=True)
        sys.exit(2)

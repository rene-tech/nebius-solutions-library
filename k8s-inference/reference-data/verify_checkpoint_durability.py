#!/usr/bin/env python3
"""Write or remount-read a challenge-bound checkpoint durability marker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path


class DurabilityError(ValueError):
    pass


SHA256 = re.compile(r"^[a-f0-9]{64}$")


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def marker_path(root: Path, generation: str) -> Path:
    return root / f".fs2-durability-{generation[:24]}"


def validate_root(root: Path) -> Path:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir() or resolved != Path("/checkpoints"):
        raise DurabilityError("checkpoint root is not the exact mounted directory")
    return resolved


def read_exact(path: Path, expected: bytes) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != len(expected):
            raise DurabilityError("durability marker is not the exact regular file")
        observed = bytearray()
        while len(observed) < len(expected) + 1:
            chunk = os.read(descriptor, len(expected) + 1 - len(observed))
            if not chunk:
                break
            observed.extend(chunk)
        if bytes(observed) != expected:
            raise DurabilityError("existing durability marker differs")
    finally:
        os.close(descriptor)


def write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise DurabilityError("durability candidate write made no progress")
        offset += written


def fsync_directory(root: Path) -> None:
    """Persist a verified directory entry, including an entry from a prior try."""

    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def publish_marker(root: Path, target: Path, generation: str, payload: bytes) -> None:
    """Publish complete bytes without overwriting or deleting any filesystem entry.

    A crash can leave only a uniquely named candidate.  A later retry writes a
    different candidate and atomically hard-links it to the absent final name.
    If another writer won the link race, its final bytes must be identical.
    Candidates are intentionally retained; the signed generation/Job bounds
    cap them at three per generation without requiring unlink cleanup.
    """

    payload_sha256 = hashlib.sha256(payload).hexdigest()
    candidate = root / (
        f".fs2-durability-candidate-{generation[:12]}-"
        f"{payload_sha256[:12]}-{secrets.token_hex(12)}"
    )
    descriptor = os.open(
        candidate,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    try:
        os.link(candidate, target, follow_symlinks=False)
    except FileExistsError:
        read_exact(target, payload)
    else:
        # Verify through the published name rather than trusting only the
        # candidate descriptor.  This also rejects an unexpected filesystem
        # implementation that did not preserve the complete inode contents.
        read_exact(target, payload)

    fsync_directory(root)


def proof(args: argparse.Namespace) -> dict[str, object]:
    root = validate_root(args.root)
    if not SHA256.fullmatch(args.generation) or args.attempt < 1:
        raise DurabilityError("proof generation or attempt is malformed")
    identity = {
        "uid": args.pvc_uid,
        "resource_version": args.pvc_resource_version,
        "volume_name": args.volume_name,
    }
    marker = {
        "schema": "fs2-serve.nebius.ai/checkpoint-durability-marker/v2",
        "pvc": identity,
        "challenge": args.challenge,
        "generation": args.generation,
        "attempt": args.attempt,
    }
    payload = canonical(marker) + b"\n"
    target = marker_path(root, args.generation)
    if args.mode == "write":
        if target.exists():
            read_exact(target, payload)
            # The prior process may have crashed after link(2) but before its
            # directory fsync. A retry must make the already verified final
            # name durable rather than treating byte equality as completion.
            fsync_directory(root)
        else:
            publish_marker(root, target, args.generation, payload)
    else:
        read_exact(target, payload)
    result: dict[str, object] = {
        "schema": "fs2-serve.nebius.ai/checkpoint-durability-proof/v2",
        "mode": args.mode,
        "pvc": identity,
        "challenge": args.challenge,
        "generation": args.generation,
        "attempt": args.attempt,
        "marker_sha256": hashlib.sha256(payload).hexdigest(),
    }
    result["proof_sha256"] = hashlib.sha256(canonical(result)).hexdigest()
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("mode", choices=("write", "read"))
    result.add_argument("--root", required=True, type=Path)
    result.add_argument("--pvc-uid", required=True)
    result.add_argument("--pvc-resource-version", required=True)
    result.add_argument("--volume-name", required=True)
    result.add_argument("--challenge", required=True)
    result.add_argument("--generation", required=True)
    result.add_argument("--attempt", required=True, type=int)
    result.add_argument("--proof-output", required=True, type=Path)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        if args.proof_output != Path("/dev/termination-log"):
            raise DurabilityError("proof output must be the Kubernetes termination log")
        payload = json.dumps(proof(args), sort_keys=True, separators=(",", ":"))
        args.proof_output.write_text(payload + "\n", encoding="utf-8")
    except (OSError, DurabilityError) as error:
        print(f"checkpoint durability proof refused: {error}", file=sys.stderr)
        return 1
    sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

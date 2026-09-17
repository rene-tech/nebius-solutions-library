#!/usr/bin/env python3
"""Write or remount-read a challenge-bound checkpoint durability marker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            descriptor = os.open(target, flags, 0o600)
        except FileExistsError:
            descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                if os.read(descriptor, len(payload) + 1) != payload:
                    raise DurabilityError("existing durability marker differs")
            finally:
                os.close(descriptor)
        else:
            try:
                if os.write(descriptor, payload) != len(payload):
                    raise DurabilityError("durability marker write was incomplete")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    else:
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            observed = os.read(descriptor, len(payload) + 1)
            if observed != payload:
                raise DurabilityError("remounted durability marker content differs")
        finally:
            os.close(descriptor)
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

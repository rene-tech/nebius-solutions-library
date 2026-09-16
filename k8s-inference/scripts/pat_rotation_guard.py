#!/usr/bin/env python3
"""Create value-free PAT maps and prove overlap or revocation against a live API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import stat
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PAT_PATTERN = re.compile(r"^fs2_pat_([0-9a-f]{32})_([A-Za-z0-9_-]{32,})$")
AUDIENCES = frozenset({"general", "scientific"})


class PatGuardError(RuntimeError):
    pass


def now_text() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def private_document(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise PatGuardError("private input must be a regular file")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise PatGuardError("private input must be owner-owned mode 0600")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise PatGuardError("private input is not valid JSON") from error


def private_json(path: Path, document: Any) -> None:
    path = path.absolute()
    if path.exists() or path.is_symlink():
        raise PatGuardError("receipt is write-once and already exists")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise PatGuardError("receipt parent must be a real directory")
    metadata = parent.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PatGuardError("receipt parent must be owner-owned and owner-only")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if path.exists():
            path.chmod(0o600)


def parse_tokens(document: Any) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    if not isinstance(document, dict) or not document or not set(document) <= AUDIENCES:
        raise PatGuardError(
            "PAT input must contain only general and scientific audiences"
        )
    tokens: dict[str, dict[str, str]] = {}
    entries: dict[str, Any] = {}
    identifiers: list[str] = []
    fingerprints: list[str] = []
    for audience, raw_generations in sorted(document.items()):
        if not isinstance(raw_generations, dict) or not raw_generations:
            raise PatGuardError(
                f"{audience} PAT generations must be a non-empty object"
            )
        try:
            generations = sorted(int(value) for value in raw_generations)
        except (TypeError, ValueError) as error:
            raise PatGuardError(
                f"{audience} PAT generation keys must be integers"
            ) from error
        if generations != list(range(1, max(generations) + 1)):
            raise PatGuardError(f"{audience} PAT generations must be contiguous from 1")
        tokens[audience] = {}
        entries[audience] = {}
        for generation in generations:
            key = str(generation)
            token = raw_generations.get(key)
            match = PAT_PATTERN.fullmatch(token) if isinstance(token, str) else None
            if match is None:
                raise PatGuardError(f"{audience} generation {generation} is not a PAT")
            token_id = match.group(1)
            fingerprint = hashlib.sha256(token.encode()).hexdigest()
            identifiers.append(token_id)
            fingerprints.append(fingerprint)
            tokens[audience][key] = token
            entries[audience][key] = {
                "token_id": token_id,
                "token_sha256": fingerprint,
            }
    if len(identifiers) != len(set(identifiers)):
        raise PatGuardError(
            "PAT IDs must be distinct across all generations and audiences"
        )
    if len(fingerprints) != len(set(fingerprints)):
        raise PatGuardError(
            "PAT fingerprints must be distinct across all generations and audiences"
        )
    return tokens, entries


def mapping_receipt(tokens_path: Path, receipt_path: Path) -> dict[str, Any]:
    _, entries = parse_tokens(private_document(tokens_path))
    receipt = {
        "schema": "fs2-serve.nebius.ai/pat-generation-map/v1",
        "captured_at": now_text(),
        "audiences": entries,
    }
    private_json(receipt_path, receipt)
    return receipt


def load_mapping(path: Path, entries: dict[str, Any]) -> dict[str, Any]:
    receipt = private_document(path)
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != "fs2-serve.nebius.ai/pat-generation-map/v1"
        or receipt.get("audiences") != entries
    ):
        raise PatGuardError("PAT mapping receipt differs from the private token map")
    return receipt


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_endpoint(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    if parsed.scheme == "https" and parsed.hostname:
        return
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}:
        return
    raise PatGuardError("PAT proof endpoint must be HTTPS or loopback HTTP")


def request_token(endpoint: str, token: str, *, timeout_seconds: int) -> dict[str, Any]:
    validate_endpoint(endpoint)
    request = urllib.request.Request(
        endpoint,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    opener = urllib.request.build_opener(
        NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = response.status
            body = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        status = error.code
        body = error.read(1024 * 1024 + 1)
    except urllib.error.URLError as error:
        raise PatGuardError("PAT proof request failed") from error
    if len(body) > 1024 * 1024:
        raise PatGuardError("PAT proof response exceeds one MiB")
    semantic = False
    if 200 <= status < 300:
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as error:
            raise PatGuardError("successful PAT proof response is not JSON") from error
        semantic = isinstance(decoded, (dict, list)) and bool(decoded)
        if not semantic:
            raise PatGuardError("successful PAT proof response is semantically empty")
    return {
        "status": status,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "semantic": semantic,
    }


def selected_tokens(
    args: argparse.Namespace,
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    tokens, entries = parse_tokens(private_document(args.tokens_file))
    load_mapping(args.mapping_receipt, entries)
    audience = tokens.get(args.audience)
    if audience is None:
        raise PatGuardError("requested PAT audience is absent")
    old_key, new_key = str(args.old_generation), str(args.new_generation)
    if (
        args.old_generation >= args.new_generation
        or old_key not in audience
        or new_key not in audience
    ):
        raise PatGuardError(
            "PAT proof requires existing ordered old and new generations"
        )
    return (
        audience[old_key],
        audience[new_key],
        entries[args.audience][old_key],
        entries[args.audience][new_key],
    )


def prove(args: argparse.Namespace, *, revoked: bool) -> dict[str, Any]:
    old_token, new_token, old_entry, new_entry = selected_tokens(args)
    old_result = request_token(
        args.endpoint, old_token, timeout_seconds=args.timeout_seconds
    )
    new_result = request_token(
        args.endpoint, new_token, timeout_seconds=args.timeout_seconds
    )
    if revoked:
        if old_result["status"] not in {401, 403} or not (
            200 <= new_result["status"] < 300
        ):
            raise PatGuardError(
                "PAT revocation proof did not reject old and accept new"
            )
    elif not (
        200 <= old_result["status"] < 300
        and 200 <= new_result["status"] < 300
        and old_result["semantic"]
        and new_result["semantic"]
    ):
        raise PatGuardError(
            "PAT overlap proof requires semantic success from old and new"
        )
    receipt = {
        "schema": (
            "fs2-serve.nebius.ai/pat-revocation-proof/v1"
            if revoked
            else "fs2-serve.nebius.ai/pat-overlap-proof/v1"
        ),
        "verified_at": now_text(),
        "audience": args.audience,
        "endpoint_sha256": hashlib.sha256(args.endpoint.encode()).hexdigest(),
        "old": {"generation": args.old_generation, **old_entry, **old_result},
        "new": {"generation": args.new_generation, **new_entry, **new_result},
    }
    private_json(args.receipt, receipt)
    return receipt


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    mapping = subparsers.add_parser("map")
    mapping.add_argument("--tokens-file", type=Path, required=True)
    mapping.add_argument("--receipt", type=Path, required=True)
    for command in ("prove-overlap", "prove-revocation"):
        proof = subparsers.add_parser(command)
        proof.add_argument("--tokens-file", type=Path, required=True)
        proof.add_argument("--mapping-receipt", type=Path, required=True)
        proof.add_argument("--audience", choices=sorted(AUDIENCES), required=True)
        proof.add_argument("--old-generation", type=int, required=True)
        proof.add_argument("--new-generation", type=int, required=True)
        proof.add_argument("--endpoint", required=True)
        proof.add_argument("--timeout-seconds", type=int, default=10)
        proof.add_argument("--receipt", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    args = parse_args(argv)
    if args.command == "map":
        receipt = mapping_receipt(args.tokens_file, args.receipt)
    else:
        receipt = prove(args, revoked=args.command == "prove-revocation")
    print(
        json.dumps(
            {
                "status": "pass",
                "schema": receipt["schema"],
                "receipt": str(args.receipt.absolute()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, PatGuardError) as error:
        print(f"error: {error}", file=os.sys.stderr)
        raise SystemExit(2) from error

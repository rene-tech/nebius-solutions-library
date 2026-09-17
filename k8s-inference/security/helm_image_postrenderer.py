#!/usr/bin/env python3
"""Rewrite every rendered Helm image to its reviewed digest or fail closed.

Helm invokes post-renderers with the complete rendered release on stdin. This
tool deliberately does not trust an inventory-complete flag: every scalar
``image:`` value in the actual render is either already digest-bound or must
have an exact tag-to-digest mapping with registry-resolution provenance.
Unknown, tag-only, variable, or malformed references abort the release.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

try:
    from .image_security_evidence import (
        DIGEST_REFERENCE,
        EvidenceError,
        validate_first_party_inventory,
        validate_inventory,
    )
except ImportError:
    from image_security_evidence import (
        DIGEST_REFERENCE,
        EvidenceError,
        validate_first_party_inventory,
        validate_inventory,
    )
try:
    from .yaml_image_references import (
        YamlImageError,
        image_scalars,
        rewrite_image_scalars,
    )
except ImportError:
    from yaml_image_references import (
        YamlImageError,
        image_scalars,
        rewrite_image_scalars,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _protected_path(argument: Path, variable: str) -> Path:
    """Use a hash-bound out-of-tree release artifact when one is supplied."""

    override = os.environ.get(variable)
    expected = os.environ.get(f"{variable}_SHA256")
    if override is None and expected is None:
        return argument.resolve()
    if not override or not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise EvidenceError(f"{variable} and {variable}_SHA256 must be supplied together")
    path = Path(override).resolve()
    if _sha256(path) != expected:
        raise EvidenceError(f"{variable} hash differs from the protected release binding")
    return path


def rewrite(
    rendered: str,
    lock_path: Path,
    first_party_lock_path: Path,
    trust_path: Path,
) -> tuple[str, set[str]]:
    images = validate_inventory(lock_path, trust_path)
    first_party_images = validate_first_party_inventory(
        first_party_lock_path,
        trust_path,
        source_root=Path(__file__).resolve().parent.parent,
    )
    mappings = {
        image["source_reference"]: image["digest_reference"]
        for image in images
    }
    accepted_digests = {
        image["digest_reference"] for image in images + first_party_images
    }
    subjects: set[str] = set()
    replacements: dict[tuple[int, int], str] = {}
    try:
        scalars = image_scalars(rendered)
    except YamlImageError as exc:
        raise EvidenceError(str(exc)) from exc
    for scalar in scalars:
        reference = scalar.reference
        if DIGEST_REFERENCE.fullmatch(reference):
            digest_reference = reference
        else:
            digest_reference = mappings.get(reference)
            if digest_reference is None:
                raise EvidenceError(
                    f"render line {scalar.line}: image {reference!r} has no reviewed "
                    "tag-to-digest mapping"
                )
        if digest_reference not in accepted_digests:
            raise EvidenceError(
                f"render line {scalar.line}: digest {digest_reference!r} is absent "
                "from the reviewed scan inventory"
            )
        subjects.add(digest_reference)
        replacements[(scalar.start, scalar.end)] = digest_reference
    if not scalars:
        raise EvidenceError("render contains no image fields; refusing empty closure")
    return rewrite_image_scalars(rendered, replacements), subjects


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--first-party-lock", required=True, type=Path)
    parser.add_argument("--trust", required=True, type=Path)
    args = parser.parse_args()
    try:
        lock = _protected_path(args.lock, "FS2_THIRD_PARTY_IMAGE_LOCK")
        first_party_lock = _protected_path(
            args.first_party_lock, "FS2_FIRST_PARTY_IMAGE_LOCK"
        )
        trust = args.trust.resolve()
        rendered = sys.stdin.read()
        rewritten, _ = rewrite(
            rendered, lock, first_party_lock, trust
        )
    except EvidenceError as exc:
        print(f"image post-render gate: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(rewritten)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

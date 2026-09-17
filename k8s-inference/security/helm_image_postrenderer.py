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
import sys
from pathlib import Path

try:
    from .image_security_evidence import (
        DIGEST_REFERENCE,
        EvidenceError,
        validate_first_party_inventory,
        validate_image_gate_authorization,
        validate_inventory,
    )
except ImportError:
    from image_security_evidence import (
        DIGEST_REFERENCE,
        EvidenceError,
        validate_first_party_inventory,
        validate_image_gate_authorization,
        validate_inventory,
    )
try:
    from .semantic_yaml_images import (
        SemanticYamlError,
        independently_validated_image_scalars,
    )
    from .yaml_image_references import rewrite_image_scalars
except ImportError:
    from semantic_yaml_images import (
        SemanticYamlError,
        independently_validated_image_scalars,
    )
    from yaml_image_references import rewrite_image_scalars


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
        scalars = independently_validated_image_scalars(rendered)
    except SemanticYamlError as exc:
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--first-party-lock", required=True, type=Path)
    parser.add_argument("--trust", required=True, type=Path)
    parser.add_argument("--authorization", required=True, type=Path)
    args = parser.parse_args()
    try:
        lock = Path(os.environ.get("FS2_THIRD_PARTY_IMAGE_LOCK", args.lock)).resolve()
        first_party_lock = Path(
            os.environ.get("FS2_FIRST_PARTY_IMAGE_LOCK", args.first_party_lock)
        ).resolve()
        trust = args.trust.resolve()
        authorization = Path(
            os.environ.get("FS2_IMAGE_GATE_AUTHORIZATION", args.authorization)
        ).resolve()
        authorized = validate_image_gate_authorization(
            authorization,
            trust,
            Path(__file__).resolve().parent.parent,
        )
        if _sha256(lock) != authorized["inventory_sha256"]:
            raise EvidenceError(
                "third-party inventory differs from signed image-gate authority"
            )
        if (
            _sha256(first_party_lock)
            != authorized["first_party_inventory_sha256"]
        ):
            raise EvidenceError(
                "first-party inventory differs from signed image-gate authority"
            )
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

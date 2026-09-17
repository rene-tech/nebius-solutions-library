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
import re
import sys
from pathlib import Path

try:
    from .image_security_evidence import (
        DIGEST_REFERENCE,
        EvidenceError,
        validate_inventory,
    )
except ImportError:
    from image_security_evidence import (
        DIGEST_REFERENCE,
        EvidenceError,
        validate_inventory,
    )


IMAGE_LINE = re.compile(
    r"^(?P<prefix>[ \t]*(?:-[ \t]*)?image:[ \t]*)"
    r"(?P<quote>['\"]?)(?P<ref>[^'\"# \t]+)(?P=quote)"
    r"(?P<suffix>[ \t]*(?:#.*)?)(?P<newline>\n?)$"
)


def rewrite(rendered: str, lock_path: Path) -> tuple[str, set[str]]:
    images = validate_inventory(lock_path)
    mappings = {
        image["source_reference"]: image["digest_reference"]
        for image in images
    }
    accepted_digests = {image["digest_reference"] for image in images}
    output: list[str] = []
    subjects: set[str] = set()
    found = 0
    for line_number, line in enumerate(rendered.splitlines(keepends=True), start=1):
        match = IMAGE_LINE.match(line)
        if match is None:
            output.append(line)
            continue
        found += 1
        reference = match.group("ref")
        if DIGEST_REFERENCE.fullmatch(reference):
            digest_reference = reference
        else:
            digest_reference = mappings.get(reference)
            if digest_reference is None:
                raise EvidenceError(
                    f"render line {line_number}: image {reference!r} has no reviewed "
                    "tag-to-digest mapping"
                )
        if digest_reference not in accepted_digests:
            raise EvidenceError(
                f"render line {line_number}: digest {digest_reference!r} is absent "
                "from the reviewed scan inventory"
            )
        subjects.add(digest_reference)
        quote = match.group("quote")
        output.append(
            f"{match.group('prefix')}{quote}{digest_reference}{quote}"
            f"{match.group('suffix')}{match.group('newline')}"
        )
    if found == 0:
        raise EvidenceError("render contains no image fields; refusing empty closure")
    return "".join(output), subjects


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True, type=Path)
    args = parser.parse_args()
    try:
        rendered = sys.stdin.read()
        rewritten, _ = rewrite(rendered, args.lock)
    except EvidenceError as exc:
        print(f"image post-render gate: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(rewritten)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

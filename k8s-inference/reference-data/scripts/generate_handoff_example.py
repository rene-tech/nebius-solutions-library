#!/usr/bin/env python3
"""Generate the consumer-facing AlphaFold 3 terminal handoff example.

The example is produced by the publisher's own builder, so a consumer
integration test written against it cannot drift from what the publisher
writes. Digests are fixed placeholders; the real receipt is published to the
shared filesystem, never to this repository.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

REFERENCE_DATA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REFERENCE_DATA))

import reference_data  # noqa: E402


EXAMPLE_PATH = REFERENCE_DATA / "examples" / "af3-terminal-handoff.example.json"


def build() -> dict[str, object]:
    return reference_data.build_terminal_receipt(
        bundle_id="alphafold3-public-databases-v3.0",
        revision="v3.0-paper-snapshot-2022-09-28",
        tree_sha256="c" * 64,
        manifest_sha256="b" * 64,
        inventory_sha256="e" * 64,
        file_count=214_017,
        expanded_bytes=630_000_000_000,
        created_at="2026-09-03T00:00:00Z",
    )


def main() -> int:
    document = build()
    EXAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXAMPLE_PATH.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"written": str(EXAMPLE_PATH.relative_to(REFERENCE_DATA.parent))}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

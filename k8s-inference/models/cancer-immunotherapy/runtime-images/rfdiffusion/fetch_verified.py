#!/usr/bin/env python3
"""Download a URL to a path and fail unless it matches an expected sha256.

Used at image build time so every externally fetched byte is pinned. The base
image ships no curl/wget, so this deliberately depends on the standard library
only.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

_CHUNK = 1024 * 1024


def fetch(url: str, destination: Path, expected_sha256: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "fs2-rfdiffusion-build/1"})
    with urllib.request.urlopen(request, timeout=600) as response:  # noqa: S310 - pinned https/http source
        with destination.open("wb") as handle:
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                handle.write(chunk)

    actual = digest.hexdigest()
    if actual != expected_sha256:
        destination.unlink(missing_ok=True)
        raise SystemExit(
            f"sha256 mismatch for {url}\n  expected {expected_sha256}\n  actual   {actual}"
        )
    print(f"verified {destination} sha256={actual} bytes={destination.stat().st_size}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args(argv)
    fetch(args.url, args.output, args.sha256.lower())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

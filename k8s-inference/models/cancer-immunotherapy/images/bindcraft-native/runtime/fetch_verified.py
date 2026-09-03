#!/usr/bin/env python3
"""Download one public immutable build material and verify its SHA-256."""

from __future__ import annotations

import hashlib
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: fetch_verified.py HTTPS_URL SHA256 OUTPUT")
    url, expected, output_value = sys.argv[1:]
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or len(expected) != 64:
        raise SystemExit("only public HTTPS materials with SHA-256 are accepted")
    output = Path(output_value)
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "fs2-reproducible-image-builder/1"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, output.open("xb") as stream:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                stream.write(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("download digest mismatch")
    except Exception:
        output.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    main()

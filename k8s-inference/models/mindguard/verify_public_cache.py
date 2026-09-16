#!/usr/bin/env python3
"""Read a pinned file manifest from stdin and verify an already downloaded model directory."""

import hashlib
import json
import sys
import time
from pathlib import Path

manifest = json.load(sys.stdin)
root = Path(sys.argv[1]).resolve(strict=True)
started = time.perf_counter()
for item in manifest["files"]:
    target = (root / item["path"]).resolve(strict=True)
    if not target.is_relative_to(root) or not target.is_file():
        raise SystemExit("artifact path is outside model directory")
    if target.stat().st_size != item["size_bytes"]:
        raise SystemExit("artifact size mismatch: " + item["path"])
    with target.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != item["sha256"]:
        raise SystemExit("artifact digest mismatch: " + item["path"])
print(json.dumps({"model_id": manifest["model_id"], "revision": manifest["revision"],
                  "status": "all_files_sha256_verified", "file_count": len(manifest["files"]),
                  "total_bytes": sum(item["size_bytes"] for item in manifest["files"]),
                  "verification_seconds": time.perf_counter() - started}))

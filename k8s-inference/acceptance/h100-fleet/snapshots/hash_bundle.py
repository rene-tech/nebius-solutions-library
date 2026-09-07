#!/usr/bin/env python3
"""Describe immutable snapshot bytes without modifying the bundle.

Run in a CPU-only holder with the exact task-owned bundle mounted read-only.
The canonical file manifest binds content and ownership; stdout is the small
receipt. Large checkpoint pages remain on shared storage, never in Git.
"""

import argparse
import hashlib
import json
from pathlib import Path
import stat


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory.resolve(strict=True)
    if not (root / "images/compatibility.json").is_file():
        parser.error("directory is not a captured bundle")
    files = []
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(
                f"bundle contains an unsupported non-regular entry: {path.relative_to(root)}"
            )
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files.append(
            {
                "path": str(path.relative_to(root)),
                "bytes": metadata.st_size,
                "sha256": digest,
                "mode": stat.S_IMODE(metadata.st_mode),
                "uid": metadata.st_uid,
                "gid": metadata.st_gid,
            }
        )
    manifest = {
        "schema": "fs2-serve.nebius.ai/immutable-snapshot-bundle/v1",
        "files": files,
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    print(
        json.dumps(
            {
                "bundle_sha256": hashlib.sha256(canonical).hexdigest(),
                "bytes": sum(item["bytes"] for item in files),
                "file_count": len(files),
                "manifest": manifest,
                "compatibility": json.loads(
                    (root / "images/compatibility.json").read_bytes()
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

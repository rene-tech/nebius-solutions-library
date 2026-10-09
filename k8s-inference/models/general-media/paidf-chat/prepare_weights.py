"""Acquire or verify precisely the measured source files on a model-owned PVC."""

import argparse
import hashlib
import json
import os
from pathlib import Path

from huggingface_hub import snapshot_download


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    contract = json.loads(Path("/contract/weights.json").read_text())
    root = Path(snapshot_download(
        repo_id=contract["model"], revision=contract["revision"],
        allow_patterns=[item["path"] for item in contract["files"]],
        cache_dir=os.environ["HF_HOME"] + "/hub", local_files_only=args.verify_only,
        max_workers=4,
    ))
    for item in contract["files"]:
        path = root / item["path"]
        if not path.is_file() or path.stat().st_size != item["bytes"]:
            raise ValueError("Missing or differently sized model file: " + item["path"])
        with path.open("rb") as source:
            checksum = hashlib.file_digest(source, "sha256").hexdigest()
        if checksum != item["sha256"]:
            raise ValueError("Model file checksum mismatch: " + item["path"])
    print(json.dumps({"model": contract["model"], "revision": contract["revision"],
                      "verified_files": len(contract["files"]),
                      "verified_bytes": sum(item["bytes"] for item in contract["files"]),
                      "mode": "verify-only" if args.verify_only else "acquire-and-verify"}), flush=True)


if __name__ == "__main__":
    main()

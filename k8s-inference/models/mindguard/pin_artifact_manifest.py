#!/usr/bin/env python3
"""Record exact public file hashes; fetch only metadata and small config/tokenizer files."""

import argparse
import hashlib
import json
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("model", choices=["mindguard-4b", "mindguard-8b"])
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
model = json.loads((root / "public-models.lock.json").read_text())["models"][args.model]
info = HfApi().model_info(model["repo_id"], revision=model["revision"], files_metadata=True)
if info.sha != model["revision"]:
    raise SystemExit("source revision mismatch")
files = []
for item in info.siblings:
    if not item.rfilename.endswith((".json", ".jinja", ".txt", ".safetensors")):
        continue
    if item.rfilename.endswith(".safetensors"):
        if not item.lfs:
            raise SystemExit("missing immutable weight digest")
        digest = item.lfs.sha256
    else:
        file_path = Path(hf_hub_download(model["repo_id"], item.rfilename, revision=model["revision"]))
        with file_path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
    files.append({"path": item.rfilename, "size_bytes": item.size, "sha256": digest})
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps({"model_id": args.model, **model, "files": files}, indent=2) + "\n")
print(f"Pinned {args.model}: {len(files)} exact files")

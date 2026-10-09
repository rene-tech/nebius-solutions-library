"""Inspect the immutable vendor image without starting inference or downloads."""

import hashlib
import json
from pathlib import Path

import yaml

paths = [
    Path("/opt/nim/etc/model_manifest.yaml"),
    Path("/opt/nim/etc/default/model_manifest.yaml"),
]
manifest_path = next((path for path in paths if path.is_file()), None)
if manifest_path is None:
    raise SystemExit("No NIM model manifest at the documented paths")
payload = manifest_path.read_bytes()
document = yaml.safe_load(payload)
print(
    json.dumps(
        {
            "manifest_path": str(manifest_path),
            "manifest_sha256": hashlib.sha256(payload).hexdigest(),
            "manifest": document,
        },
        sort_keys=True,
    ),
    flush=True,
)

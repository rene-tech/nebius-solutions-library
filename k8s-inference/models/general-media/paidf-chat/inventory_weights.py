"""Read-only inventory of the exact files actually loaded by a private probe."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

model, revision = sys.argv[1:]
root = (
    Path(os.environ["HF_HOME"])
    / "hub"
    / ("models--" + model.replace("/", "--"))
    / "snapshots"
    / revision
)
if not root.is_dir():
    raise SystemExit("Pinned source snapshot is absent")
files = []
for path in sorted(root.rglob("*")):
    if path.is_file():
        with path.open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
        files.append(
            {
                "path": str(path.relative_to(root)),
                "bytes": path.stat().st_size,
                "sha256": checksum,
            }
        )
if not any(value["path"].endswith(".safetensors") for value in files):
    raise SystemExit("No loaded weights found")
hardware = subprocess.check_output(
    [
        "nvidia-smi",
        "--query-gpu=name,uuid,driver_version,memory.total",
        "--format=csv,noheader",
    ],
    text=True,
).strip()
print(
    json.dumps(
        {
            "model": model,
            "revision": revision,
            "files": files,
            "expanded_bytes": sum(value["bytes"] for value in files),
            "hardware": hardware,
        },
        indent=2,
    )
)

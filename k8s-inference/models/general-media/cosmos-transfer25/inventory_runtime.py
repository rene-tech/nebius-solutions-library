"""Read-only SHA-256 inventory of every file in the pinned NIM workspace."""

import hashlib
import json
import time
from pathlib import Path

import yaml

PROFILE_ID = "e74ebba119c8a196dca12cac66aa1b5323291048a855fe02ceb6b664f334c672"
MANIFEST_SHA256 = "67e0b910a86b7cdd1d91d9dae2058a12e88bc3f4b154073f176df2377bdbca17"


def main():
    raw = Path("/opt/nim/etc/default/model_manifest.yaml").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == MANIFEST_SHA256
    manifest = yaml.safe_load(raw)
    profile = next(item for item in manifest["profiles"] if item["id"] == PROFILE_ID)
    started = time.monotonic()
    files = []
    for name in sorted(profile["workspace"]["files"]):
        relative = Path(name)
        assert not relative.is_absolute() and ".." not in relative.parts
        path = Path("/opt/nim/workspace") / relative
        assert path.is_file()
        value = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024**2), b""):
                size += len(chunk)
                value.update(chunk)
        files.append({"path": name, "bytes": size, "sha256": value.hexdigest()})
    print(
        json.dumps(
            {
                "profile_id": PROFILE_ID,
                "manifest_sha256": MANIFEST_SHA256,
                "files": files,
                "expanded_bytes": sum(item["bytes"] for item in files),
                "elapsed_seconds": time.monotonic() - started,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()

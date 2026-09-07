"""Build-time acquisition of the distinct, public OpenFold3 Preview2 weights."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

ARTIFACTS = (
    ("of3-p2-155k.pt", "https://openfold3-data.s3.amazonaws.com/openfold3-parameters/of3-p2-155k.pt",
     2287928196, '"7826b2e9cb1d387e7b641c39b80f67a5-273"', "af09eac4f29cef856633af07558cb143226fe95ebbef2c20921769d4a5f4bee4"),
    ("components.bcif", "https://openfold3-data.s3.us-west-2.amazonaws.com/components.bcif",
     63393643, None, "473d845c8b250b188dbed9bf505ae206692a178a2a7c4869bf8f9de707ffcc0c"),
)


def main():
    target = Path("/opt/fs2/openfold3-preview2-artifacts")
    target.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, url, expected_bytes, etag, expected_sha in ARTIFACTS:
        digest, count = hashlib.sha256(), 0
        request = Request(url, headers={"If-Match": etag} if etag else {})
        with urlopen(request, timeout=120) as response, (target / name).open("xb") as output:
            actual_etag = response.headers.get("ETag")
            if etag and actual_etag != etag:
                raise ValueError("Checkpoint object identity changed")
            while block := response.read(8 * 1024 * 1024):
                output.write(block)
                digest.update(block)
                count += len(block)
        actual_sha = digest.hexdigest()
        if count != expected_bytes or (expected_sha and actual_sha != expected_sha):
            raise ValueError("Artifact content failed exact size/digest check")
        row = {"path": name, "url": url, "bytes": count, "sha256": actual_sha, "etag": actual_etag}
        print(json.dumps(row), flush=True)
        rows.append(row)
    (target / "manifest.json").write_text(json.dumps({"model": "OpenFold3 Preview2", "files": rows}, indent=2) + "\n")


if __name__ == "__main__":
    main()

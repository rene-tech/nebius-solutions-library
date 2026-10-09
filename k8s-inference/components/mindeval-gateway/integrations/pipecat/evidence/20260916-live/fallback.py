#!/usr/bin/env python3
"""Build/verify a deterministic, credential-free prerecorded offline ZIP."""

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

FILES = (
    "001-clinician-Jason.wav",
    "002-patient-Sofia.wav",
    "README.md",
    "report.json",
    "fallback.html",
    "fallback.README.md",
    "fallback.py",
)
RECORDED_AT = "2026-09-16T17:18:58.676072487Z"


def bundle(output):
    source = Path(__file__).resolve().parent
    report = json.loads((source / "report.json").read_text())
    assert report["passed"] and report["run_id"] == "fs2-pipecat-live-20260916"
    contents = {name: (source / name).read_bytes() for name in FILES}
    for audio in report["audio_files"]:
        assert hashlib.sha256(contents[audio["path"]]).hexdigest() == audio["sha256"], "recorded audio changed"
    manifest = {
        "schema": "fs2-prerecorded-fallback/v1",
        "mode": "PRERECORDED_OFFLINE",
        "recorded_report_at": RECORDED_AT,
        "run_id": report["run_id"],
        "profile_id": report["profile"]["id"],
        "synthetic_profile": True,
        "patient_model": report["registration"]["patient_model"],
        "clinician_models": report["registration"]["clinician_models"],
        "judge_model": report["canonical_judgment"]["model"],
        "files": {
            name: {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in sorted(contents.items())
        },
    }
    contents["fallback.manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    target = Path(output).resolve()
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing bundle: {target}")
    with ZipFile(target, "x", compression=ZIP_STORED) as archive:
        for name, content in sorted(contents.items()):
            info = ZipInfo(name, date_time=(2026, 9, 16, 17, 18, 58))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = ZIP_STORED
            archive.writestr(info, content)
    with ZipFile(target) as archive:
        assert archive.testzip() is None
        for name, metadata in manifest["files"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == metadata["sha256"]
    return {
        "path": str(target),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "bytes": target.stat().st_size,
        "files": len(contents),
        "mode": manifest["mode"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="New ZIP path outside the source tree")
    args = parser.parse_args()
    print(json.dumps(bundle(args.output)))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Decode each retained Cosmos output with pinned PyAV in a client-only venv.

Install av==15.1.0 in a disposable environment; this does not touch the serving
image. Standard output is a non-secret JSON validation receipt.
"""
import argparse
import hashlib
import json
from pathlib import Path

import av


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media", type=Path, nargs="+")
    args = parser.parse_args()
    reports = []
    for path in args.media:
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            frames = list(container.decode(stream))
            frame_shapes = sorted({(frame.width, frame.height) for frame in frames})
            report = {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "bytes": path.stat().st_size, "codec": stream.codec_context.name,
                      "decoded_frames": len(frames), "shapes": frame_shapes,
                      "fps": str(stream.average_rate),
                      "distinct_luma_frames": len({hashlib.sha256(bytes(f.planes[0])).hexdigest() for f in frames})}
            report["passed"] = len(frames) == 25 and frame_shapes == [(448, 256)] and stream.average_rate == 24
            reports.append(report)
    print(json.dumps({"pyav_version": av.__version__, "ffmpeg_libraries": av.library_versions,
                      "result": "PASS" if all(r["passed"] for r in reports) else "FAIL",
                      "outputs": reports}, indent=2, sort_keys=True))
    return 0 if all(r["passed"] for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())

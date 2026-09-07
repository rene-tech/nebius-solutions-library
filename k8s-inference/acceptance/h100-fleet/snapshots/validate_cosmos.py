#!/usr/bin/env python3
"""Validate the unchanged two Cosmos media fixtures on a task-owned Pod."""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", default="k8s-inference-h100")
    parser.add_argument("--pod", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--seed-offset", type=int, default=0, help="new seeds not captured in the donor"
    )
    args = parser.parse_args()
    import av

    solution = Path(__file__).resolve().parents[3]
    validator_path = solution / "catalog/runtime/validators/validate_cosmos3_nano.py"
    spec = importlib.util.spec_from_file_location("cosmos_validator", validator_path)
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    fixture = validator.load_contract(
        validator_path.parent / "assets/cosmos3-nano.json"
    )
    args.output.mkdir(parents=True, exist_ok=False)
    records = []
    for case in fixture["requests"]:
        case["request"]["seed"] += args.seed_offset
        started = time.monotonic()
        command = [
            "kubectl",
            "--kubeconfig",
            args.kubeconfig,
            "--context",
            args.context,
            "-n",
            "fs2-models",
            "exec",
            "-i",
            args.pod,
            "-c",
            "bounded-json-adapter",
            "--",
            "python3",
            "-c",
            "import json,sys,urllib.request;"
            "r=urllib.request.Request('http://127.0.0.1:8080/generate',"
            "data=sys.stdin.buffer.read(),headers={'Content-Type':'application/json'});"
            "print(urllib.request.urlopen(r,timeout=600).read().decode())",
        ]
        response = subprocess.run(
            command,
            input=json.dumps(case["request"]),
            capture_output=True,
            text=True,
            check=True,
            timeout=660,
        )
        result = json.loads(response.stdout)
        identity = validator.validate_response(result, case, case["id"])
        raw = base64.b64decode(result["data_base64"], validate=True)
        with av.open(io.BytesIO(raw)) as container:
            stream = container.streams.video[0]
            frames = list(container.decode(stream))
            shapes = sorted({(frame.width, frame.height) for frame in frames})
            assert (
                len(frames) == 25
                and shapes == [(448, 256)]
                and stream.average_rate == 24
            )
            decoded = {
                "frames": len(frames),
                "shapes": shapes,
                "fps": str(stream.average_rate),
            }
        (args.output / (case["id"] + ".mp4")).write_bytes(raw)
        records.append(
            {
                "case": case["id"],
                "input_sha256": hashlib.sha256(
                    validator.canonical_json(case["request"])
                ).hexdigest(),
                "media": identity,
                "decoded": decoded,
                "seconds": time.monotonic() - started,
                "runtime_timings_ms": result["timings_ms"],
                "passed": True,
            }
        )
    assert len({record["media"]["sha256"] for record in records}) == 2
    receipt = {
        "at": datetime.now(timezone.utc).isoformat(),
        "model": fixture["model"],
        "pod": args.pod,
        "requests": records,
        "passed": True,
    }
    (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()

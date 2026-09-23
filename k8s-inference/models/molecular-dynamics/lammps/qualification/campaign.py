"""Sequential repetitions on exactly one task-owned GPU Pod, retaining failures."""

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from cluster import run
from make_fixture import fixture, write_fixture
from validate_case import validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", default="lj,eam,tersoff,snap,reaxff,rhodo")
    parser.add_argument("--steps", type=int, default=2000)
    parser.add_argument("--step-map", default="", help="Optional case:steps comma-separated overrides, fixed before repetitions.")
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--segment-seconds", type=int, default=60)
    parser.add_argument("--trajectory-every", type=int, default=1000)
    parser.add_argument("--resume", action="store_true", help="Continue only missing repetitions; never overwrite or rerun an existing attempt")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=args.resume)
    receipts = []
    step_map = {key: int(value) for key, value in (item.split(":", 1) for item in args.step_map.split(",") if item)}
    for case in args.cases.split(","):
        body, files = fixture(case, args.assets, step_map.get(case, args.steps), warmup=args.warmup, segment_seconds=args.segment_seconds, trajectory_every=args.trajectory_every)
        source = args.output / (case + "-fixture")
        if source.exists() and args.resume:
            expected = {name: {"size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()} for name, content in files.items()}
            if json.loads((source / "request.json").read_text()) != body or json.loads((source / "fixture-manifest.json").read_text()) != expected:
                raise ValueError("existing fixture does not match the requested scientific protocol")
        else:
            write_fixture(source, body, files)
        for repeat in range(1, args.repetitions + 1):
            destination = args.output / f"{args.output.name}-{case}-r{repeat}"
            if destination.exists() and args.resume:
                if not (destination / "qualification.json").is_file() or not (destination / "workspace/result.json").is_file():
                    raise ValueError(f"recover incomplete evidence collection first: {destination}")
            else:
                run(SimpleNamespace(pod=args.pod, output=destination, input=source, job=case))
            try:
                receipt = validate(destination / "workspace")
            except Exception as exc:
                receipt = {"status": "failed", "error": str(exc)}
            receipt |= {"case": case, "repetition": repeat, "directory": str(destination)}
            (destination / "validation.json").write_text(json.dumps(receipt, indent=2) + "\n")
            receipts.append(receipt)
            (args.output / "campaign.json").write_text(json.dumps(receipts, indent=2) + "\n")
            print(json.dumps(receipt), flush=True)
    raise SystemExit(0 if all(r["status"] == "passed" for r in receipts) else 1)


if __name__ == "__main__":
    main()

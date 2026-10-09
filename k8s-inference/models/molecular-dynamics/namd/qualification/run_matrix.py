"""Run output-enabled cases serially on one GPU, retaining failures and validators."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--campaign-suffix", required=True)
    parser.add_argument("--job-id", action="append")
    parser.add_argument("--append", action="store_true")
    parser.add_argument("fixtures", nargs="+")
    args = parser.parse_args()
    receipts = []
    for fixture in args.fixtures:
        output = args.root / ("campaign-" + fixture.removesuffix("-r2") + "-" + args.campaign_suffix)
        started = time.monotonic()
        extra = [part for job in (args.job_id or []) for part in ("--job-id", job)] + (["--append"] if args.append else [])
        run = subprocess.run([sys.executable, str(Path(__file__).with_name("run_campaign.py")),
                              "--fixture", str(args.root / fixture), "--output", str(output),
                              "--operation", output.name] + extra)
        validation = subprocess.run([sys.executable, str(Path(__file__).with_name("validate_campaign.py")),
                                     "--campaign", str(output), "--output", str(output / "validation.json")])
        receipt = {"fixture": fixture, "campaign": str(output), "run_exit": run.returncode,
                   "validation_exit": validation.returncode, "elapsed_seconds": time.monotonic() - started}
        receipts.append(receipt)
        print(json.dumps({"matrix_case": receipt}), flush=True)
        (args.root / ("matrix-" + args.campaign_suffix + ".json")).write_text(json.dumps(receipts, indent=2) + "\n")
    raise SystemExit(0 if all(not r["run_exit"] and not r["validation_exit"] for r in receipts) else 1)


if __name__ == "__main__":
    main()

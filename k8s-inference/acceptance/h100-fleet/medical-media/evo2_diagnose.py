#!/usr/bin/env python3
"""Retain both unchanged Evo2 oracle outcomes, even after the first fails."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2/evo2-native"))
import validate_evo2 as validator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:29384")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profile", choices=("archived-b300", "historical-hopper"), default="archived-b300")
    args = parser.parse_args()
    origin = validator.validate_base_url(args.base_url)
    directory = validator.create_receipt_dir(args.output)
    opener = validator.direct_opener()
    readiness = validator.wait_until_ready(opener, origin, 30)
    report = {"readiness": readiness, "status": "PASS", "attempts": []}
    build_probes = validator.build_probes
    if args.profile == "historical-hopper":
        from evo2_hopper_validate import build_probes
    report["oracle_profile"] = args.profile
    for repetition in (1, 2):
        trial = validator.create_receipt_dir(directory / f"repeat-{repetition}")
        for probe in build_probes((f"fs2-mm-evo-diagnostic-{repetition}-a", f"fs2-mm-evo-diagnostic-{repetition}-b")):
            try:
                result = validator.run_probe(opener, origin, probe, 900, trial)
            except (validator.SemanticFailure, validator.TransportFailure) as exc:
                report["status"] = "FAIL"
                response = json.loads((trial / f"response-{probe.index}.body").read_bytes())
                result = {"status": "FAIL", "error": str(exc), "case": probe.index,
                    "expected_sequence": probe.expected_sequence, "actual_sequence": response.get("sequence")}
            report["attempts"].append({"repetition": repetition, **result})
    validator.write_private(directory / "report.json", validator.json_bytes(report))
    print(json.dumps(report), flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

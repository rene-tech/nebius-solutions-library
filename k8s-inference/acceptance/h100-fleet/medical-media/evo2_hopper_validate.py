#!/usr/bin/env python3
"""Exact historical Hopper/NIM oracle, retaining all original protocol checks."""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2/evo2-native"))
import validate_evo2 as base

ORACLE_PATH = Path(__file__).with_name("evo2-hopper-oracle.json")
validate_response = base.validate_response


def build_probes(run_ids):
    oracle = json.loads(ORACLE_PATH.read_text())
    probes = base.build_probes(run_ids)
    for probe, case in zip(probes, oracle["cases"], strict=True):
        if probe.payload != case["payload"]:
            raise base.SetupFailure("original payload differs from independently pinned Hopper fixture")
    return tuple(dataclasses.replace(probe, expected_sequence=case["expected_sequence"])
        for probe, case in zip(probes, oracle["cases"], strict=True))


def main():
    args = base.parse_args()
    origin = base.validate_base_url(args.base_url)
    probes = build_probes(args.run_id)
    receipt = base.create_receipt_dir(args.receipt_dir)
    opener = base.direct_opener()
    ready = base.wait_until_ready(opener, origin, args.ready_timeout)
    cases = [base.run_probe(opener, origin, probe, args.timeout, receipt) for probe in probes]
    summary = {"status": "PASS", "profile": "historical-h200-nim-exact-dna20",
        "oracle_sha256": base.sha256(ORACLE_PATH.read_bytes()), "readiness": ready, "cases": cases}
    base.write_private(receipt / "summary.json", base.json_bytes(summary))
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Promote only completed ESM paired proofs into explicit optional bundles."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from report_protenix_pairs import project  # noqa: E402


def esm_report(receipt, manifest, config, cases):
    adapted = copy.deepcopy(receipt)
    for run in adapted["runs"]:
        if run["mode"] == "normal":
            run["ready"]["model_load_seconds"] = run["ready"]["load_seconds"]
    result = project(adapted, manifest)
    result.update(model_id=config["model_id"], stage_id="fold")
    for key in ("model_revision", "profile_model_revision"):
        if key in config:
            result[key] = config[key]
    for projected, original in zip(result["runs"], receipt["runs"], strict=True):
        if "node" in original:
            projected["node"] = original["node"]
    result["parameters"] = {"esmc_precision": "bf16", "attention": "flash_attention_2",
                            "num_loops": 20, "num_sampling_steps": 200, "mode": "single-sequence"}
    result["inputs"] = cases
    result["bundle"].update(id=config["bundle_id"], subpath=config["bundle_path"],
                           captured_runtime_path="/checkpoints/" + config["bundle_path"])
    result["qualification_scope"] = (
        "Original immutable model image plus versioned optional canonical wrapper overlay; "
        "request-independent GPU state, original controller-issued short and ubiquitin "
        "arguments/localization/handoffs, full normal CIF/confidence outputs; production rollout separate"
    )
    result["source_overlay_sha256"] = config["cli_sha256"]
    result["clock_notes"][-1] = (
        "Trials do not deliberately evict shared data or node caches; shared files do not guarantee node page-cache residency. "
        "Existing-pool autoscaling can introduce a new node and actual image pulls. Pod-create clocks include observed "
        "scheduler/init/image waits; container-start clocks exclude those waits. Acquisition events and node identity "
        "must be read separately from the model-ready clock; this is not a controlled image-cold benchmark."
    )
    result["failed_init_attempts"] = {
        "count": len(receipt.get("failed_attempts", [])),
        "cause": "shared-filesystem ACL metadata read returned ENODATA to cp -a before runtime startup",
        "resolution": "copy small cache files with numeric ownership/modes/timestamps via tar; GPU pages remain read-only",
        "statistics": "failed pre-runtime attempts retained in private receipt and excluded from the three completed pairs",
    }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("receipt", "manifest", "config", "report", "bundle"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--case", action="append", type=Path, required=True)
    args = parser.parse_args()
    root = HERE.parents[3]
    config = json.loads(args.config.read_bytes())
    manifest = json.loads(args.manifest.read_bytes())
    assert config["model_id"] in {"esmfold2", "esmfold2-fast"}
    assert len(args.case) == 2
    cases = []
    for directory in args.case:
        command = json.loads((directory / "original-command.json").read_bytes())
        assert command[command.index("--variant") + 1] == config["model_id"]
        assert command[command.index("--esmc-precision") + 1] == "bf16"
        assert command[command.index("--num-loops") + 1] == "20"
        assert command[command.index("--num-sampling-steps") + 1] == "200"
        cases.append({"raw_sha256": command[command.index("--expected-raw-input-sha256") + 1],
                      "seed": int(command[command.index("--seed") + 1])})
    assert len({case["raw_sha256"] for case in cases}) == 2
    for name, expected in config["source_sha256"].items():
        source = (root / "models/cancer-immunotherapy/images/structure-secondary/run_esmfold2.py"
                  if name == "run_esmfold2.py" else root / "models/scientific-snapshot" / name)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == expected
    receipt = json.loads(args.receipt.read_bytes())
    report = esm_report(receipt, manifest, config, cases)
    report["private_receipt_sha256"] = hashlib.sha256(args.receipt.read_bytes()).hexdigest()
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    bundle = {key: value for key, value in config.items() if key != "node"}
    bundle.update(qualified=True, qualification_receipt_sha256=hashlib.sha256(args.report.read_bytes()).hexdigest())
    from fs2_serve.scientific_batch.startup import validate_bundle

    validate_bundle(bundle, bundle["bundle_id"])
    args.bundle.write_text(json.dumps(bundle, indent=2) + "\n")
    print(json.dumps({"model_id": config["model_id"], "statistics": report["statistics"]}))


if __name__ == "__main__":
    main()

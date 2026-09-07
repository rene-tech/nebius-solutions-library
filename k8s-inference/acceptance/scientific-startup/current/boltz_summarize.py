"""Summarize completed exact-stage BoltzGen startup receipts without private Pod specs."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics


def elapsed(start: str, end: str) -> float:
    value = (datetime.fromisoformat(end.replace("Z", "+00:00")) -
             datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
    if value < 0:
        raise ValueError("startup timestamps are out of order")
    return value


def summarize(paths: list[Path], source: Path) -> dict:
    records = [json.loads(path.read_text()) for path in paths]
    if len(records) < 3 or len({row["job"] for row in records}) != len(records):
        raise ValueError("at least three distinct completed probe Jobs are required")
    identities = set()
    for index, row in enumerate(records, 1):
        if row["status"] != "passed" or row["model_id"] != "boltzgen":
            raise ValueError("a startup trial did not pass")
        ready, semantic = row["ready"], row["semantic_output"]
        if "_PredictionLoop._on_predict_start" not in ready["boundary"]:
            raise ValueError("not a post-restore prediction-start boundary")
        if semantic["unchanged_runtime_exit_code"] != 0 or len(semantic["structures"]) != 20:
            raise ValueError("full 20-candidate stage output was not verified")
        identities.add((row["image_id"], row["original_command_sha256"],
                        json.dumps(row["inputs"], sort_keys=True),
                        ready["native_hook_source_sha256"]))
        row["campaign_repetition"] = index
        row["timings_seconds"].update({
            "container_start_to_valid_stage_output": elapsed(row["container_started_at"], semantic["utc"]),
            "pod_create_to_valid_stage_output": elapsed(row["pod_created_at"], semantic["utc"]),
        })
    if len(identities) != 1:
        raise ValueError("runtime/input/native hook identity differs across trials")
    template = json.loads(source.read_text())
    stage = next(item for item in template["spec"]["template"]["spec"]["containers"]
                 if item["name"] == "scientific-stage")
    here = Path(__file__).resolve().parent
    files = ["boltz_sitecustomize.py", "boltz_supervise.py", "primary_isolated_runs.py",
             "primary_local_materialize.py", "boltz_recover_template.py"]
    result = {
        "schema": "fs2-serve.nebius.ai/boltz-current-startup-report/v1",
        "date": "2026-09-07", "model_id": "boltzgen", "result": "PASS",
        "scope": "exact current design stage, 20 candidates, fresh processes and current cached weights/images",
        "node_condition": "shared retained H100 nodes; trials overlap on separately allocated GPUs",
        "source_pod_uid": template["metadata"]["annotations"]["benchmark.fs2.nebius/source-pod-uid"],
        "source_pod_spec_sha256": template["metadata"]["annotations"]["benchmark.fs2.nebius/source-pod-spec-sha256"],
        "resource_envelope_unchanged": stage["resources"],
        "immutable_model_mounts": [mount for mount in stage["volumeMounts"] if mount.get("readOnly")],
        "report_source_sha256": {name: hashlib.sha256((here / name).read_bytes()).hexdigest() for name in files},
        "source_hash_note": "Published harness files at reporting time; native hook source is also hashed inside each measured process. Boltz hook and supervisor were unchanged across all three trials.",
        "raw_receipt_sha256": {path.parent.name + "/" + path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
        "trials": records,
        "statistics": {},
        "limitations": [
            "No global cache flush; not disk-cold or checkpoint-restore timing",
            "The model-ready marker follows native setup/restore and CUDA synchronization; lazy compilation can follow",
            "Pod creation is the isolated start clock, not a public request",
            "The complete design stage is validated, not all later folding and filtering stages",
            "Three trials support median/range, not tail latency claims",
            "Kubernetes container timestamps have one-second granularity",
        ],
    }
    for key in records[0]["timings_seconds"]:
        values = [row["timings_seconds"][key] for row in records]
        result["statistics"][key] = {"n": len(values), "median": statistics.median(values),
                                     "minimum": min(values), "maximum": max(values)}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, action="append", required=True)
    parser.add_argument("--source-template", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(args.receipt, args.source_template), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Project retained real one-shot proof, including independently checked cleanup."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

from fs2_serve.scientific_batch.adapters.rfdiffusion import _validate_cache_evidence
from report_rfdiffusion_pairs import original_result, restore_measurement


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("restore", "fallback", "qualification", "report", "kubeconfig"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    qualified = json.loads(args.qualification.read_bytes())
    expected = next(row for row in qualified["inputs"] if row["residues"] == 76)
    report = {
        "schema": "fs2-serve.nebius.ai/scientific-snapshot-production-transform-acceptance/v1",
        "date": "2026-09-07",
        "model_id": "rfdiffusion",
        "status": "passed",
        "scope": "Exact production transform in isolated task Pods; original stage argv, completion writer and output validation retained. Public API/admission/collector rollout is separate.",
        "qualification_sha256": hashlib.sha256(args.qualification.read_bytes()).hexdigest(),
        "runs": [],
    }
    for directory, backend in ((args.restore, "cuda-criu"), (args.fallback, "normal-load")):
        receipt = json.loads((directory / "receipt.json").read_bytes())
        pod = json.loads((directory / "pod-final-private.json").read_bytes())
        if receipt["status"] != "passed" or receipt["actual_startup"]["backend"] != backend:
            raise ValueError("original one-shot proof did not pass expected actual path")
        with tarfile.open(directory / "outputs.tar") as archive:
            stream = archive.extractfile("./result.json")
            if stream is None:
                raise ValueError("original result missing")
            cache = json.load(stream)["cache_level"]
        _validate_cache_evidence(cache)
        result = original_result(directory / "outputs.tar")
        if result["pdb_sha256"] != expected["pdb_sha256"]:
            raise ValueError("one-shot output differs from accepted original coordinates")
        name = pod["metadata"]["name"]
        for kind, resource in (("pod", name), ("configmap", name + "-inputs")):
            check = subprocess.run(
                ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", "k8s-inference-h100", "-n", "fs2-models", "get", kind, resource, "--ignore-not-found", "-o", "name"],
                capture_output=True, text=True, check=True, timeout=30,
            )
            if check.stdout.strip():
                raise ValueError("test resource still exists: " + resource)
        row = {
            "actual_startup": receipt["actual_startup"],
            "pod_uid": pod["metadata"]["uid"],
            "result": result,
            "cache_metadata": cache,
            "original_receipt_sha256": hashlib.sha256((directory / "receipt.json").read_bytes()).hexdigest(),
            "log_sha256": hashlib.sha256((directory / "scientific-stage.log").read_bytes()).hexdigest(),
            "original_cleanup_wait_completed": receipt["test_pod_and_inputs_deleted"],
            "test_pod_and_inputs_absent_verified": True,
        }
        if backend == "cuda-criu":
            row["restore_components"] = restore_measurement(directory / "scientific-stage.log")
        else:
            row["test_condition"] = "Only this task Pod's source-directory argument points to a nonexistent path; shared bundle and original normal command are unchanged."
        report["runs"].append(row)
    report["cleanup_verified_at"] = datetime.now(timezone.utc).isoformat()
    report["cleanup_note"] = "The first harness wait matched the inherited 90s termination grace and timed out before final Pod removal. Original receipts retain false; this later independent check confirms both Pods and input ConfigMaps absent. Retained entrypoint/source ConfigMaps and immutable shared bundle are for Terraform adoption."
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": "passed", "runs": len(report["runs"])}))


if __name__ == "__main__":
    main()

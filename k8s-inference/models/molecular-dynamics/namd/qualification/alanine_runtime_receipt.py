"""Bind an audited canonical alanine campaign to its captured exact native worker."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from runtime_receipt import runtime_identity, sha


def receipt(fixture, campaign, validation_path, runtime, image, source_revision):
    identity = runtime_identity(runtime, image)
    validation = json.loads(validation_path.read_text())
    request = json.loads((fixture / "request.json").read_text())
    if (
        validation["status"] != "passed"
        or validation["input_sha256"] != sha(fixture / "input.tar.gz")
        or validation["request_sha256"] != sha(fixture / "request.json")
        or [r["job_id"] for r in validation["results"]] != [j["id"] for j in request["jobs"]]
    ):
        raise ValueError("canonical validation does not bind the exact completed native request")
    tests = []
    for report in validation["results"]:
        result_path = campaign / report["job_id"] / "result.json"
        result = json.loads(result_path.read_text())
        if result["status"] != "succeeded" or report["status"] != "passed" or sha(result_path) != report["result_sha256"]:
            raise ValueError("canonical result differs from successful scientific validation")
        tests.append({
            "case": "canonical-ff14sb-tip3p-alanine/" + validation["mode"] + "/" + report["job_id"],
            **identity, "status": "passed", "input_sha256": validation["input_sha256"],
            "request_sha256": validation["request_sha256"], "result_sha256": report["result_sha256"],
            "validation_sha256": sha(validation_path), "result_path": str(result_path),
            "validation_path": str(validation_path), "raw_evidence_path": str(campaign),
            "scientific_validation": report,
        })
    if not tests:
        raise ValueError("canonical receipt requires actual native results")
    return {"model_id": "namd", "runtime_image": image, "source_revision": source_revision,
            "recorded_at": datetime.now(timezone.utc).isoformat(), "status": "passed",
            "customer_ready": False, "tests": tests, "single_trajectory": True, "MPS": False,
            "gpu_snapshot_qualified": False, "cross_engine_equivalence_proven": False,
            "scientific_convergence_claimed": False,
            "limitations": ["Native qualification is separate from ordinary hosted-client acceptance",
                            "Native checkpoint files do not serialize GPU memory or stochastic RNG state",
                            "Native single-trajectory timings exclude cloud queue, image pull and artifact transfer"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("fixture", "campaign", "validation", "runtime", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args()
    value = receipt(args.fixture, args.campaign, args.validation, args.runtime, args.image, args.source_revision)
    args.output.write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps({"status": value["status"], "receipt": str(args.output), "sha256": sha(args.output),
                      "tests": len(value["tests"]), "customer_ready": False}))


if __name__ == "__main__":
    main()

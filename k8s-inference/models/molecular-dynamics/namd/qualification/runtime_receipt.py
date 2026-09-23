"""Bind finite scientific campaign evidence to one exact worker image and GPU."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", nargs=2, action="append", metavar=("FIXTURE", "CAMPAIGN"), required=True)
    args = parser.parse_args()
    pod = json.loads((args.runtime / "pod.json").read_text())
    container = next(c for c in pod["spec"]["containers"] if c["name"] == "runtime")
    actual = next(c for c in pod["status"]["containerStatuses"] if c["name"] == "runtime")
    if container["image"] != args.image or args.image.split("@")[-1] not in actual["imageID"]:
        raise ValueError("requested receipt image does not match the captured running Pod")
    with (args.runtime / "native-identity.txt").open() as stream:
        gpu = next(csv.DictReader([next(stream), next(stream)], skipinitialspace=True))
    tests = []
    for fixture_arg, campaign_arg in args.case:
        fixture, campaign = Path(fixture_arg), Path(campaign_arg)
        provenance = json.loads((fixture / "provenance.json").read_text())
        validation_path = campaign / "validation.json"
        validation = json.loads(validation_path.read_text())
        results = []
        for path in sorted(campaign.glob("rep-*/result.json")):
            result = json.loads(path.read_text())
            results.append({"job_id": result["job_id"], "status": result["status"], "error": result["error"],
                            "result_sha256": sha(path), "result_path": str(path),
                            "completed_steps": result["completed_steps"], "recipe_sha256": result["recipe_sha256"],
                            "file_count": len(result["files"]),
                            "artifact_bytes": sum(f["size_bytes"] for f in result["files"]),
                            "seeds_by_process": [{k: c.get(k) for k in ("step_id", "segment", "random_seed", "configured_first_step", "checkpoint_step")} for c in result["commands"]]})
        tests.append({"case": f"{provenance['system']}-{provenance['ensemble']}-{provenance['gpu_mode']}" + ("-prepare" if provenance.get("preparation") else ""),
                      "pool": pod["spec"]["nodeSelector"]["accelerator.fs2.nebius/pool-id"],
                      "gpu": gpu, "node": pod["spec"]["nodeName"], "pod_uid": pod["metadata"]["uid"],
                      "status": "passed" if len(results) == provenance["repetitions"] == validation["successful_repetitions"] and all(r["status"] == "succeeded" for r in results) else "incomplete",
                      "input_sha256": sha(fixture / "input.tar.gz"), "request_sha256": sha(fixture / "request.json"),
                      "validation_sha256": sha(validation_path), "validation_path": str(validation_path),
                      "fixture_path": str(fixture), "raw_evidence_path": str(campaign),
                      "production_steps": provenance["production_steps"], "results": results,
                      "median_ns_per_day": validation["median_ns_per_day"],
                      "min_ns_per_day": validation["min_ns_per_day"], "max_ns_per_day": validation["max_ns_per_day"]})
    receipt = {"model_id": "namd", "runtime_image": args.image, "source_revision": args.source_revision,
               "recorded_at": datetime.now(timezone.utc).isoformat(), "customer_ready": False,
               "tests": tests, "single_trajectory": True, "MPS": False, "gpu_snapshot_qualified": False,
               "runtime_evidence_path": str(args.runtime), "pod_record_sha256": sha(args.runtime / "pod.json"),
               "native_identity_sha256": sha(args.runtime / "native-identity.txt"),
               "limitations": ["Native scientific worker tests do not qualify hosted REST/MCP or tenant artifacts",
                               "Native checkpoints do not serialize GPU process memory or stochastic RNG state",
                               "No scientific ensemble or free-energy convergence claim",
                               "GBIS and spinAngle affected by fixes absent from this NVIDIA 3.0.2 image remain unavailable",
                               "Pool eligibility is limited to the exact tested GPU/pool; no L40S inference",
                               "Startup events were measured on an existing node with cached base layers, not an uncached node cold start"]}
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"receipt": str(args.output), "sha256": sha(args.output), "cases": len(tests), "customer_ready": False}))


if __name__ == "__main__":
    main()

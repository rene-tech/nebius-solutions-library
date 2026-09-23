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


def runtime_identity(runtime, image):
    pod = json.loads((runtime / "pod.json").read_text())
    container = next(c for c in pod["spec"]["containers"] if c["name"] == "runtime")
    actual = next(c for c in pod["status"]["containerStatuses"] if c["name"] == "runtime")
    if container["image"] != image or image.split("@")[-1] not in actual["imageID"]:
        raise ValueError("requested receipt image does not match the captured running Pod")
    with (runtime / "native-identity.txt").open() as stream:
        gpu = next(csv.DictReader([next(stream), next(stream)], skipinitialspace=True))
    return {"pool": pod["spec"]["nodeSelector"]["accelerator.fs2.nebius/pool-id"],
            "gpu_name": gpu["name"], "gpu_uuid": gpu["uuid"], "driver": gpu["driver_version"],
            "node": pod["spec"]["nodeName"], "pod_uid": pod["metadata"]["uid"],
            "runtime_evidence_path": str(runtime), "pod_record_sha256": sha(runtime / "pod.json"),
            "native_identity_sha256": sha(runtime / "native-identity.txt")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", nargs=2, action="append", metavar=("FIXTURE", "CAMPAIGN"), required=True)
    parser.add_argument("--chosen-job-id", action="append", help="Explicit cohort subset for an initial hosted-acceptance receipt; does not imply all planned performance repetitions")
    parser.add_argument("--recovery-case", nargs=3, action="append", metavar=("SNAPSHOT", "CAMPAIGN", "FRESH_RUNTIME"))
    args = parser.parse_args()
    identity = runtime_identity(args.runtime, args.image)
    tests = []
    for fixture_arg, campaign_arg in args.case:
        fixture, campaign = Path(fixture_arg), Path(campaign_arg)
        provenance = json.loads((fixture / "provenance.json").read_text())
        validation_path = campaign / "validation.json"
        validation = json.loads(validation_path.read_text())
        request = json.loads((fixture / "request.json").read_text())
        chosen = args.chosen_job_id or [job["id"] for job in request["jobs"]]
        case = f"{provenance['system']}-{provenance['ensemble']}-{provenance['gpu_mode']}" + ("-prepare" if provenance.get("preparation") else "")
        if provenance.get("colvars_variant") == "grid-keep-hills":
            case += "-grid-keep-hills"
        for job_id in chosen:
            path = campaign / job_id / "result.json"
            row = {"case": case + "/" + job_id, "group_case": case, "job_id": job_id, **identity,
                   "status": "incomplete", "input_sha256": sha(fixture / "input.tar.gz"),
                   "request_sha256": sha(fixture / "request.json"), "validation_sha256": sha(validation_path),
                   "validation_path": str(validation_path), "fixture_path": str(fixture), "raw_evidence_path": str(campaign),
                   "production_steps": provenance["production_steps"], "planned_repetitions": provenance["repetitions"],
                   "completed_repetitions": validation["successful_repetitions"],
                   "benchmark_cohort_complete": validation["successful_repetitions"] == provenance["repetitions"],
                   "median_ns_per_day": validation["median_ns_per_day"],
                   "min_ns_per_day": validation["min_ns_per_day"], "max_ns_per_day": validation["max_ns_per_day"]}
            tests.append(row)
            if not path.is_file():
                row["error"] = "chosen repetition has not completed"
                continue
            result = json.loads(path.read_text())
            scientific = next(r for r in validation["repetitions"] if r["job"] == job_id)
            row.update(status="passed" if result["status"] == scientific["status"] == "succeeded" else "failed",
                       error=scientific["error"] or result["error"], result_sha256=sha(path), result_path=str(path),
                       completed_steps=result["completed_steps"], recipe_sha256=result["recipe_sha256"],
                       file_count=len(result["files"]), artifact_bytes=sum(f["size_bytes"] for f in result["files"]),
                       scientific_validation=scientific,
                       seeds_by_process=[{k: c.get(k) for k in ("step_id", "segment", "random_seed", "configured_first_step", "checkpoint_step")} for c in result["commands"]])
    for snapshot_arg, campaign_arg, runtime_arg in args.recovery_case or []:
        snapshot, campaign, runtime = Path(snapshot_arg), Path(campaign_arg), Path(runtime_arg)
        fresh = runtime_identity(runtime, args.image)
        staged = json.loads((snapshot / "staged.json").read_text())
        work = campaign / staged["job_id"]
        restored = json.loads((work / "recovery-receipt.json").read_text())
        validation_path = campaign / "validation.json"
        validation = json.loads(validation_path.read_text())
        passed = fresh["pod_uid"] != identity["pod_uid"] and restored["status"] == "succeeded" and validation["successful_repetitions"] == 1
        tests.append({"case": "colvars-fresh-pod-native-checkpoint/" + staged["job_id"], **fresh,
                      "status": "passed" if passed else "failed", "job_id": staged["job_id"],
                      "input_sha256": sha(snapshot / "checkpoint.tar.gz"), "result_sha256": sha(work / "result.json"),
                      "validation_sha256": sha(validation_path), "validation_path": str(validation_path),
                      "result_path": str(work / "result.json"), "raw_evidence_path": str(campaign),
                      "source_pod_uid": identity["pod_uid"], "native_recovery": restored,
                      "checkpoint_manifest_sha256": sha(snapshot / "manifest.json"),
                      "checkpoint_stage_receipt_sha256": sha(snapshot / "staged.json"),
                      "recovery_receipt_sha256": sha(work / "recovery-receipt.json"), "gpu_snapshot_used": False})
    receipt = {"model_id": "namd", "runtime_image": args.image, "source_revision": args.source_revision,
               "recorded_at": datetime.now(timezone.utc).isoformat(), "customer_ready": False,
               "status": "passed" if tests and all(test["status"] == "passed" for test in tests) else "incomplete",
               "chosen_job_ids": args.chosen_job_id or "all planned repetitions",
               "tests": tests, "single_trajectory": True, "MPS": False, "gpu_snapshot_qualified": False,
               "runtime_evidence_path": str(args.runtime), "pod_record_sha256": sha(args.runtime / "pod.json"),
               "native_identity_sha256": sha(args.runtime / "native-identity.txt"),
               "limitations": ["Native scientific worker tests do not qualify hosted REST/MCP or tenant artifacts",
                               "Native checkpoints do not serialize GPU process memory or stochastic RNG state",
                               "No scientific ensemble or free-energy convergence claim",
                               "GBIS and spinAngle affected by fixes absent from this NVIDIA 3.0.2 image remain unavailable",
                               "Pool eligibility is limited to the exact tested GPU/pool; no untested-GPU inference",
                               "Startup events were measured on an existing node; base-layer cache state was not independently measured, so this is not an uncached-node cold start"]}
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"receipt": str(args.output), "sha256": sha(args.output), "cases": len(tests), "customer_ready": False}))


if __name__ == "__main__":
    main()

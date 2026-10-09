#!/usr/bin/env python3
"""Independently verify retained RF recovery records; makes no network calls."""
import argparse
import hashlib
import json
import math
from pathlib import Path


def verify(directory: Path) -> dict:
    def read(name):
        return json.loads((directory / name).read_bytes())

    receipt, status = read("receipt.json"), read("status.json")
    assert receipt["state"] == "verified" and receipt["recovery_verified"]
    assert receipt["same_identity_replay_verified"]
    assert status["operation"]["id"] == receipt["operation_id"]
    assert status["operation"]["status"] == "succeeded" and status["batch"]["result_published"]
    attempts = next(row for row in status["batch"]["stages"] if row["stage_id"] == "inference")["attempts"]
    assert len(attempts) == 2 and [row["attempt_number"] for row in attempts] == [1, 2]
    assert attempts[0]["attempt_id"] == receipt["eviction_intent"]["attempt_id"]
    assert attempts[0]["failure_kind"] == "infrastructure"
    assert attempts[0]["failure_code"] == "JobDisruptionTarget"
    assert attempts[1]["failure_kind"] is None
    assert all(row["resource_released"] for stage in status["batch"]["stages"] for row in stage["attempts"])
    assert read("operator-accounting.json")["data"]["retry"]["max_attempts_per_stage"] == 2
    replay = read("idempotent-replay.json")
    assert replay["operation"]["id"] == receipt["operation_id"]
    assert replay["batch"]["workload_id"] == receipt["workload_id"]

    final_jobs = []
    for line in (directory / "job-watch.json-stream").read_text().splitlines():
        event = json.loads(line)
        job = event["object"]
        for condition in job.get("status", {}).get("conditions", []):
            if condition.get("type") == "Failed" and condition.get("status") == "True":
                assert condition["reason"] == "PodFailurePolicy"
                assert condition["message"] == (
                    f"Pod {receipt['namespace']}/{receipt['eviction_intent']['pod_name']} "
                    "has condition DisruptionTarget matching FailJob rule at index 1"
                )
                final_jobs.append(job["metadata"]["uid"])
    assert set(final_jobs) == {attempts[0]["workload_uid"]}

    outputs = read("verified-artifacts.json")["outputs"]
    result = next(row for row in outputs if row.get("schema", "").endswith("/rfdiffusion-design-result/v1"))
    structure = next(row for row in outputs if row.get("semantic_type") == "protein-structure-pdb/v1")
    pdb = structure["structure"]
    digest = hashlib.sha256(pdb.encode()).hexdigest()
    assert digest == structure["verified_sha256"] == result["design"]["pdb"]["sha256"]
    atoms = [line for line in pdb.splitlines() if line.startswith("ATOM  ")]
    coordinates = [tuple(float(line[start:start + 8]) for start in (30, 38, 46)) for line in atoms]
    assert coordinates and all(math.isfinite(value) for row in coordinates for value in row)
    ca = [xyz for line, xyz in zip(atoms, coordinates, strict=True) if line[12:16].strip() == "CA"]
    assert len(ca) == result["design"]["residue_count"] == 48
    distances = [math.dist(left, right) for left, right in zip(ca[:-1], ca[1:], strict=True)]
    assert all(3.4 < value < 4.2 for value in distances)
    return {"operation_id": receipt["operation_id"], "passed": True, "gpu_attempts": 2,
            "retry_budget": 2, "retained_job_reason_verified": True, "same_identity_replay": True,
            "all_resources_released": True, "atoms": len(atoms), "ca_residues": len(ca),
            "ca_distance_range_angstrom": [min(distances), max(distances)], "pdb_sha256": digest,
            "scope": "API worker eviction and coarse structure integrity; not cloud preemption or biological efficacy"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.directory)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()

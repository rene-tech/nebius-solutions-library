"""Read-only live check of superseded scratch cleanup and retained job history."""

import argparse
import hashlib
import json
import time
from pathlib import Path

import httpx
import pilot


def resource(context, kind, name):
    text = pilot.command(
        "kubectl",
        "--context",
        context,
        "-n",
        pilot.NAMESPACE,
        "get",
        kind,
        name,
        "--ignore-not-found",
        "-o",
        "json",
    )
    return json.loads(text) if text.strip() else None


def completed(name, directory, client):
    fault = json.loads((directory / (name + "-automatic-fault.json")).read_bytes())
    op, root = fault["public_operation_id"], fault["old_s2_id"]
    response = client.get("/v1/operations/" + op)
    response.raise_for_status()
    state = response.json()
    assert state["status"] == "SUCCEEDED" and state["root_operation_id"] == root
    response = client.get("/v1/operations/" + op + "/result")
    response.raise_for_status()
    result = response.json()["native_result"]
    job = resource(pilot.CONTEXT, "job", root)
    assert job and job["metadata"]["annotations"]["serverless2.nebius/successor"]
    return {
        "name": name,
        "operation_id": op,
        "root": root,
        "manager_uid": job["metadata"]["uid"],
        "attempts": [a["attempt_id"] for a in state["attempts"]],
        "result_sha256": hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("before", "after"))
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--name", action="append", required=True)
    parser.add_argument("--retained-name", required=True)
    parser.add_argument("--prefix", required=True)
    args = parser.parse_args()
    pilot.configure(args.deployment)
    directory, context = args.private_dir, pilot.CONTEXTS[args.worker]
    baseline_path = directory / (args.prefix + "-scratch-before.json")
    with httpx.Client(
        base_url=pilot.PUBLIC,
        headers={"Authorization": "Bearer " + pilot.keys()["worker"]},
        timeout=120,
    ) as client:
        retained = json.loads((directory / (args.retained_name + ".json")).read_bytes())
        retained_op = retained["response"]["operation_id"]
        response = client.get("/v1/operations/" + retained_op)
        response.raise_for_status()
        state = response.json()
        assert state["status"] == "FAILED", "fixture must retain incomplete delivery"
        kept_claim = state["root_operation_id"] + "-work"
        kept = resource(context, "pvc", kept_claim)
        assert kept and kept["status"]["phase"] == "Bound"
        if args.action == "before":
            assert not baseline_path.exists(), "preserve earlier baseline"
            records = [completed(name, directory, client) for name in args.name]
            for record in records:
                claim = resource(context, "pvc", record["root"] + "-work")
                assert claim, "pre-fix fixture must reproduce retained scratch"
                record["scratch_uid"] = claim["metadata"]["uid"]
            pilot.write(
                baseline_path,
                {
                    "at": pilot.now(),
                    "worker": args.worker,
                    "records": records,
                    "retained_claim": kept_claim,
                    "retained_uid": kept["metadata"]["uid"],
                },
            )
            print(json.dumps({"baseline": "captured", "closed_chains": len(records)}))
            return
        before = json.loads(baseline_path.read_bytes())
        assert before["worker"] == args.worker and before["retained_claim"] == kept_claim
        end = time.monotonic() + 300
        absent_since = None
        while time.monotonic() < end:
            absent = all(resource(context, "pvc", r["root"] + "-work") is None for r in before["records"])
            kept = resource(context, "pvc", kept_claim)
            assert kept and kept["metadata"]["uid"] == before["retained_uid"]
            absent_since = (absent_since or time.monotonic()) if absent else None
            if absent_since and time.monotonic() - absent_since >= 60:
                break
            time.sleep(10)
        else:
            raise TimeoutError("closed scratch not removed or was recreated")
        for old in before["records"]:
            now = completed(old["name"], directory, client)
            assert all(now[key] == old[key] for key in now), "cleanup changed history/result/handle"
        receipt = {
            "at": pilot.now(),
            "closed_chains": len(before["records"]),
            "old_scratch_absent_for_at_least_seconds": 60,
            "manager_handles_attempts_results_unchanged": True,
            "failed_delivery_claim_uid_unchanged": True,
            "mutations_by_this_checker": 0,
        }
        pilot.write(directory / (args.prefix + "-scratch-after.json"), receipt)
        print(json.dumps(receipt))


if __name__ == "__main__":
    main()

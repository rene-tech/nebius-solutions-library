#!/usr/bin/env python3
"""Read-only bounded reconciliation, with an explicit disposable-canary teardown.

Run only after the two-cohort driver exits. No customer token, scientific row,
artifact, workload, or local evidence is deleted. --revoke-canary is the sole
mutation, and requires both same-policy canary validation and zero active work.
"""

import argparse
import json
import logging
import os
from pathlib import Path
from uuid import UUID

import httpx

from collect_live import check, collect, digest, kube, now, private_json, release_facts, require_release, write_private
from run_live import TERMINAL, validate_canary


def expected_operations(receipt):
    check(receipt.get("outcome") == "partial_scope_passed" and len(receipt.get("cohorts", [])) == 2,
          "two_completed_bounded_cohorts_required")
    expected, pods, revisions = set(), set(), {}
    for cohort in receipt["cohorts"]:
        rows = cohort["calls"] + cohort["mixed_calls"]
        check(len(rows) == 13 and cohort["observed_peak_outstanding"] == 5, "cohort_coverage_incomplete")
        for row in rows:
            check(row["state"] == "passed" and row["semantic_validated"], "case_not_passed")
            operation = row["terminal_operation"]
            identifier = str(UUID(operation["id"]))
            check(identifier not in expected and operation["status"] == "succeeded", "duplicate_or_failed_operation")
            expected.add(identifier)
            pods.add(operation["runtime"]["pod_uid"])
            revisions.setdefault(operation["model_id"], set()).add(operation["model_revision"])
        batch = cohort["batch"]
        check(batch["terminal_state"] == {"operation": "succeeded", "batch": "succeeded",
              "result": "succeeded", "semantic_validation": "passed"}, "batch_not_successful")
        attempts = [attempt for stage in batch["queue"]["observed_stages"] for attempt in stage["attempts"]]
        check(bool(attempts) and all(attempt["resource_released"] for attempt in attempts),
              "batch_resources_not_released")
        identifier = str(UUID(batch["operation_identity"]["operation_id"]))
        check(identifier not in expected and cohort["verified_batch_downloads"], "batch_evidence_incomplete")
        expected.add(identifier)
        pods.update(uid for attempt in batch["attempts"] for uid in attempt["pod_uids"])
    check(all(len(values) == 1 for values in revisions.values()), "serving_model_revision_changed")
    check(receipt["cohorts"][0]["batch"]["execution_identity"]
          == receipt["cohorts"][1]["batch"]["execution_identity"], "batch_runtime_identity_changed")
    return expected, pods, {model: next(iter(values)) for model, values in revisions.items()}


def settled(rows, expected):
    check(bool(rows) and len(rows) <= 1000, "canary_operation_bound_invalid")
    check(all(row["status"] in TERMINAL for row in rows), "canary_operations_still_active")
    check(expected <= {row["id"] for row in rows}, "expected_canary_operations_missing")


def pod_summary(pod):
    return {"name": pod["metadata"]["name"], "namespace": pod["metadata"]["namespace"],
            "uid": pod["metadata"]["uid"], "node": pod["spec"].get("nodeName"),
            "images": {row["name"]: row["image"] for row in pod["spec"]["containers"]},
            "containers": [{key: row.get(key) for key in ("name", "imageID", "ready", "restartCount")}
                           for row in pod.get("status", {}).get("containerStatuses", [])],
            "gpu_requested": any(float(row.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0)) > 0
                                 for row in pod["spec"]["containers"]),
            "phase": pod.get("status", {}).get("phase")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--expected-cp-image", required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--cohort-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revoke-canary", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    args.output.mkdir(mode=0o700)
    receipt = private_json(args.cohort_directory / "partial-receipt.json")
    expected, pod_ids, revisions = expected_operations(receipt)
    before = collect(args.kubeconfig, args.context)
    require_release(before, args.expected_cp_image)
    key = private_json(args.key_file)
    token = validate_canary(key, before)
    token_id = str(UUID(key["token_id"]))
    run = private_json(args.cohort_directory / "run.json")
    check(digest({"release": release_facts(before), "token_id": token_id}) == run["run_identity"],
          "release_changed_before_finalization")

    def in_pod(*command, script=None):
        return kube(args.kubeconfig, args.context, "-n", "fs2-system", "exec", "-i",
                    "deploy/fs2-serve-control-plane", "-c", "control-plane", "--", *command, script=script)

    usage = in_pod("python", "-m", "fs2_serve.usage_reconciliation", "--tenant", "stockholm",
                   "--from", receipt["started_at"], "--to", receipt["completed_at"],
                   "--principal", key["principal_id"], "--key", token_id,
                   "--limit", "1000", "--dsn-env", "FS2_DATABASE_URL")
    check({row["id"] for row in usage["operations"]} == expected, "usage_operation_set_mismatch")
    write_private(args.output / "usage.json", usage, (token,))
    check(all(row["admission_budget_reserved_gpu_seconds"] == 0 for row in usage["admission_snapshots"]),
          "canary_admission_reservations_remain")
    # Only scoped SELECTs; credentials remain inside the existing CP pod.
    sql_script = "token_id = " + repr(token_id) + '''
import asyncio, asyncpg, json, os
from uuid import UUID
async def run():
    connection = await asyncpg.connect(os.environ['FS2_DATABASE_URL'], command_timeout=20)
    try:
        async with connection.transaction(isolation='repeatable_read', readonly=True):
            rows = await connection.fetch("SELECT id,model_id,protocol,status,accepted_at,completed_at FROM fs2_operations WHERE token_id=$1 ORDER BY accepted_at,id LIMIT 1001", UUID(token_id))
        print(json.dumps({'operations':[dict(row) for row in rows]}, default=str))
    finally:
        await connection.close()
asyncio.run(run())
'''
    state = in_pod("python", "-", script=sql_script)
    settled(state["operations"], expected)
    write_private(args.output / "all-canary-operations.json", state, (token,))

    all_pods = kube(args.kubeconfig, args.context, "get", "pods", "-A", "-o", "json")["items"]
    used = [pod_summary(pod) for pod in all_pods if pod["metadata"]["uid"] in pod_ids]
    observers = [pod_summary(pod) for pod in all_pods if "gpu-observer" in pod["metadata"]["name"]]
    nodes = {row["node"] for row in used if row["gpu_requested"]}
    runtime = {"collected_at": now(), "scope": "operation-bound-runtime-and-current-observer-readback",
               "model_revisions": revisions, "pods": used,
               "missing_pod_uids": sorted(pod_ids - {row["uid"] for row in used}),
               "observers_on_used_gpu_nodes": [row for row in observers if row["node"] in nodes],
               "gaps": ["Observer readiness is a readback snapshot, not continuous interval coverage.",
                        "Shared serving usage is not additive GPU occupancy."]}
    write_private(args.output / "runtime-observers.json", runtime, (token,))
    after = collect(args.kubeconfig, args.context)
    require_release(after, args.expected_cp_image)
    check(release_facts(before) == release_facts(after), "release_changed_during_finalization")
    write_private(args.output / "release-after.json", after, (token,))

    result = {"completed_at": now(), "expected_inference_operations": len(expected),
              "all_canary_operations_terminal": len(state["operations"]), "canary_token_id": token_id,
              "same_release_identity": True, "customer_ready": False, "revoked": False}
    write_private(args.output / "pre-revocation.json", result, (token,))
    if args.revoke_canary:
        # Repeat the no-active-work read immediately before the sole mutation.
        settled(in_pod("python", "-", script=sql_script)["operations"], expected)
        revoke_script = "token_id = " + repr(token_id) + "\nprincipal_id = " + repr(key["principal_id"]) + '''
import json, os, pathlib, urllib.request
from urllib.parse import urlsplit
assert principal_id.startswith('stockholm-canary-')
secret = pathlib.Path(os.environ['FS2_ADMIN_TOKEN_FILE']).read_text().strip()
headers = {'Authorization':'Bearer '+secret,'Host':urlsplit(os.environ['FS2_PUBLIC_BASE_URL']).netloc}
request = urllib.request.Request('http://127.0.0.1:8080/admin/v1/tokens/'+token_id, method='DELETE', headers=headers)
with urllib.request.urlopen(request, timeout=15) as response:
    row = json.load(response)
assert row['id'] == token_id and row['principal_id'] == principal_id and row['revoked_at']
print(json.dumps({key:row[key] for key in ('id','principal_id','revoked_at')}))
'''
        result["revocation"] = in_pod("python", "-", script=revoke_script)
        result["revoked"] = True
        # Persist the durable response before any supplemental verification.
        write_private(args.output / "revocation.json", result, (token,))
        final = collect(args.kubeconfig, args.context)
        old_others = [row for row in before["token_metadata"] if row["id"] != token_id]
        new_others = [row for row in final["token_metadata"] if row["id"] != token_id]
        check(digest(sorted(old_others, key=lambda row: row["id"]))
              == digest(sorted(new_others, key=lambda row: row["id"])), "other_key_metadata_changed")
        with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
            response = client.get(final["public_endpoint"].rstrip("/") + "/v1/models",
                                  headers={"Authorization": "Bearer " + token})
        result["revoked_key_http_status"] = response.status_code
        check(response.status_code == 401, "revoked_canary_still_authorized")
        result["other_key_metadata_unchanged"] = True
    write_private(args.output / "completed.json", result, (token,))
    print(json.dumps(result))


if __name__ == "__main__":
    main()

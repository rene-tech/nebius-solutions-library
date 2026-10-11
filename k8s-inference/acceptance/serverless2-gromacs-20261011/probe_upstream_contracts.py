"""Read-only, offline integration probes against the actual S2 source tree.

Uses S2's own fake cluster fixtures: no cluster, key, bucket or customer data.
Run with the existing control-plane test environment (pytest/FastAPI/Kubernetes
dependencies) and --source pointing at the Serverless PR's k8s-inference root.
Exit 1 means the agreed compatibility/recovery gate is not met, not that the
probe itself crashed. Keep the JSON output as an acceptance receipt.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    sys.path[:0] = [str(source / "services/api/tests"), str(source / "services/api")]
    # Deliberately disallow any configured database connection in this probe.
    os.environ["DATABASE_URL"] = ""
    import pytest
    import test_api as upstream
    import status

    checks = []
    with pytest.MonkeyPatch.context() as patch:
        fake = upstream.cluster.__wrapped__(patch)
        client, fake = upstream.client.__wrapped__(patch, fake)
        submitted = client.post(
            "/v1/models/batch-example:invoke",
            json={"input": {"input_prefix": "s3://synthetic/in", "nsteps": 7}},
            headers=upstream.H,
        )
        submitted.raise_for_status()
        operation = submitted.json()["id"]
        job = fake.jobs[("tenant-demo", operation)]
        job["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
        headers = {**upstream.H, "Idempotency-Key": "same-resume-intent-20261011"}
        first = client.post(f"/v1/operations/{operation}:resume", headers=headers)
        second = client.post(f"/v1/operations/{operation}:resume", headers=headers)
        checks.append({
            "case": "resume_same_idempotency_key",
            "expected": "the same operation ID",
            "first": first.json().get("id"), "second": second.json().get("id"),
            "passed": first.is_success and second.is_success
            and first.json().get("id") == second.json().get("id"),
        })
        job["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        job["metadata"]["annotations"][f"{upstream.L}/execution"] = '{"io_mode":"application"}'
        observed = status.normalise(job, None, [])
        checks.append({
            "case": "complete_job_missing_durable_finalization", "expected": "not SUCCEEDED",
            "observed": observed["status"], "passed": observed["status"] != "SUCCEEDED",
        })
        queued = client.post(
            "/v1/models/batch-example:invoke",
            json={"input": {"input_prefix": "s3://synthetic/in", "nsteps": 7}},
            headers={**upstream.H, "Idempotency-Key": "fallback-intent-20261011"},
        )
        queued.raise_for_status()
        original_id = queued.json()["id"]
        model = upstream.appmod._model("batch-example")
        replacement, _ = upstream.jobs.switch_class(
            "tenant-demo", original_id, "eu-north1", model,
            upstream.catalog.region_params(model, "eu-north1"), "l40s", ["l40s"],
        )
        polled = client.get(f"/v1/operations/{original_id}", headers=upstream.H)
        checks.append({
            "case": "original_handle_survives_capacity_fallback",
            "expected": "original handle remains readable and identifies its replacement",
            "observed_http_status": polled.status_code,
            "replacement": replacement["metadata"]["name"],
            "passed": polled.is_success,
        })
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    print(json.dumps({"source_commit": revision, "live_resources_used": False, "checks": checks}, indent=2))
    return 0 if all(check["passed"] for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())

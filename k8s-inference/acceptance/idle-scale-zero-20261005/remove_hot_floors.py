"""Remove idle model hot floors through the supported, ETag-bound admin API.

This is an explicit operator migration, not an autoscaler. It never writes a
generated Deployment, changes model publication, uses a customer inference key,
or touches models with nonterminal operations. Full before/after policy evidence
is written to an operator-selected private output directory.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx


def kubectl(context: str, *args: str) -> str:
    return subprocess.check_output(
        ["kubectl", "--context", context, *args], text=True
    )


def active_counts(context: str) -> dict[str, int]:
    query = """SELECT model_id,count(*) FROM fs2_operations
        WHERE status IN ('queued','activating','running') GROUP BY model_id;"""
    raw = kubectl(
        context, "-n", "fs2-data", "exec", "fs2-control-db-1", "-c", "postgres",
        "--", "psql", "-U", "postgres", "-d", "fs2serve", "-X", "-A", "-t",
        "-c", query,
    )
    return {line.split("|")[0]: int(line.split("|")[1]) for line in raw.splitlines()}


def proposal_for(current: dict) -> dict:
    spec = copy.deepcopy(current["spec"])
    spec["availability"]["minReplicas"] = 0
    return {
        "name": current["name"], "namespace": current["namespace"],
        "base_etag": current["etag"], "spec": spec,
    }


def main(args: argparse.Namespace) -> None:
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret = json.loads(kubectl(
        args.context, "-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json"
    ))
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    receipt = {"observed_at": datetime.now(UTC).isoformat(), "apply": args.apply,
               "context": args.context, "origin": args.origin, "models": []}
    with httpx.Client(base_url=args.origin, headers={"origin": args.origin},
                      verify=False, timeout=120, trust_env=False) as client:
        client.post("/admin/api/v1/session",
                    headers={"authorization": "Bearer " + token}).raise_for_status()
        try:
            for name in args.models:
                response = client.get("/admin/api/v1/model-deployments/" + name)
                response.raise_for_status()
                current = response.json()["data"]
                row = {"model": name, "before": current}
                receipt["models"].append(row)
                if current["spec"]["availability"]["minReplicas"] == 0:
                    row["outcome"] = "already_zero"
                    continue
                active = active_counts(args.context)
                row["active_operation_counts"] = active
                if active.get(current["spec"]["modelRef"], 0):
                    row["outcome"] = "deferred_active_work"
                    print(json.dumps({"model": name, "outcome": row["outcome"]}), flush=True)
                    continue
                proposal = proposal_for(current)
                preview_response = client.post(
                    "/admin/api/v1/model-deployments:plan-preview", json=proposal
                )
                preview_response.raise_for_status()
                preview = preview_response.json()["data"]
                row["preview"] = preview
                if preview["decision"]["disposition"] != "accepted":
                    row["outcome"] = "preview_rejected"
                    print(json.dumps({"model": name, "outcome": row["outcome"],
                                      "decision": preview["decision"]}), flush=True)
                    continue
                if args.apply:
                    # Recheck immediately before the actual mutation. The model
                    # controller retains in-flight work across hot-floor changes.
                    if active_counts(args.context).get(current["spec"]["modelRef"], 0):
                        row["outcome"] = "deferred_new_active_work"
                        continue
                    result = client.post("/admin/api/v1/model-deployments:apply", json={
                        "preview_id": preview["preview_id"],
                        "proposed_etag": preview["proposed_etag"], "proposal": proposal,
                        "idempotency_key": "idle-zero-20261005-" + name + "-" + current["etag"].strip('"'),
                    })
                    row["apply_status"] = result.status_code
                    row["apply_result"] = result.json()
                    result.raise_for_status()
                    row["outcome"] = "applied"
                else:
                    row["outcome"] = "preview_accepted"
                print(json.dumps({"model": name, "outcome": row["outcome"]}), flush=True)
        finally:
            client.delete("/admin/api/v1/session")
            (args.output / ("apply.json" if args.apply else "preview.json")).write_text(
                json.dumps(receipt, indent=2) + "\n"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("models", nargs="+")
    main(parser.parse_args())

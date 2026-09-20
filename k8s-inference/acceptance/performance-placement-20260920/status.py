"""Print bounded, credential-free campaign and task-owned operation status."""

import argparse
import json
from contextlib import closing

from inventory import admin_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("kubeconfig", "context", "campaign-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    args = parser.parse_args()
    with closing(admin_client(args.kubeconfig, args.context, args.origin)) as admin:
        response = admin.get("/admin/api/v1/performance/campaigns/" + args.campaign_id)
        response.raise_for_status()
        campaign = response.json()["data"]
        counts = {}
        rows = []
        for trial in campaign["trials"]:
            counts[trial["status"]] = counts.get(trial["status"], 0) + 1
            if trial["status"] not in {"queued", "unsupported"}:
                result = trial.get("result") or {}
                rows.append({"model": trial["model_id"], "repetition": trial["repetition"],
                             "status": trial["status"], "worker": trial["worker"],
                             "seconds": result.get("elapsed_seconds"), "error": result.get("error_code"),
                             "operation_id": result.get("operation_id")})
        print(json.dumps({"campaign": campaign["name"], "counts": counts, "trials": rows}, indent=2))


if __name__ == "__main__":
    main()

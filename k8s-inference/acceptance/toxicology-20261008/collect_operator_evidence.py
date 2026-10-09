"""Read-only correlation of the two qualification Apps with operator surfaces."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from manage_release import MODELS, admin, get, write


def main(args):
    result = {
        "observed_at": datetime.now(UTC).isoformat(),
        "origin": args.origin,
        "models": {},
    }
    with admin(args) as client:
        response = client.get("/admin/api/v1/apps")
        response.raise_for_status()
        apps = response.json()["data"]["items"]
        for model in MODELS:
            app = next(item for item in apps if item["public_model_id"] == model)
            record = {"app": app}
            for surface in (
                "settings",
                "usage",
                "runs",
                "containers",
                "metrics",
                "logs",
            ):
                response = client.get(f"/admin/api/v1/apps/{app['app_id']}/{surface}")
                response.raise_for_status()
                record[surface] = response.json()
            runs = record["runs"]["data"]["items"]
            assert runs and all(
                row["operation"]["tenant_id"] == "system" for row in runs
            )
            assert record["usage"]["data"]["logical_runs"] == len(runs)
            for suffix, params in (
                ("telemetry/workloads", {"model_id": model, "tenant_id": "system"}),
                (f"apps/{app['app_id']}/requests", {}),
            ):
                response = client.get("/admin/api/v1/" + suffix, params=params)
                response.raise_for_status()
                record[suffix] = response.json()
            record["modeldeployment"] = get(
                args, "modeldeployment", model, "fs2-models"
            )
            result["models"][model] = record
            print(
                json.dumps(
                    {
                        "model": model,
                        "app": app["app_id"],
                        "runs": len(runs),
                        "usage": record["usage"]["data"]["logical_runs"],
                    }
                ),
                flush=True,
            )
    write(args.directory, "operator-evidence", result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument(
        "--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"
    )
    parser.add_argument("--directory", type=Path, required=True)
    main(parser.parse_args())

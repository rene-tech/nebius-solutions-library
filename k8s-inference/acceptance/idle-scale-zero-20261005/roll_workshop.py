"""Image-only update of the existing, idle workshop worker; never creates a UI."""

import argparse
import copy
import json
from pathlib import Path

import httpx

from remove_hot_floors import kubectl


def main(args):
    expected = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-mindeval-workshop@sha256:69c3c3798016a100df8048bbc2d7fdc636d90715e808fee77d2859c34f1406be"
    desired = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-mindeval-workshop@sha256:ee6eea4a54749a75510b4c9362b271c3261c08793dbf0c8f11a813ddd5f09553"
    active = kubectl(args.context, "-n", "fs2-data", "exec", "fs2-control-db-1", "-c", "postgres", "--",
                     "psql", "-U", "postgres", "-d", "fs2serve", "-X", "-A", "-t", "-c",
                     "SELECT count(*) FROM fs2_workshop.runs WHERE status NOT IN ('completed','aborted');")
    if int(active.strip()) != 0:
        raise RuntimeError("workshop has active/retained unfinished runs; postpone rollout")
    env = dict(line.split("=", 1) for line in args.env_file.read_text().splitlines()
               if "=" in line and not line.startswith("#"))
    token = env["SCIENTIFIC_MODELS_API_KEY"].strip().strip('"').strip("'")
    before = json.loads(kubectl(args.context, "-n", "fs2-system", "get", "deployment",
                                "fs2-mindeval-workshop", "-o", "json"))
    if before["spec"]["template"]["spec"]["containers"][0]["image"] != expected:
        raise RuntimeError("live workshop changed; inspect it before rebuilding")
    with httpx.Client(base_url="https://89.169.99.188", headers={"authorization": "Bearer " + token},
                      trust_env=False, timeout=60) as client:
        identity = client.get("/v1/me").json()
        assert (identity["tenant_id"], identity["principal_id"]) == ("system", "qa")
        check = client.get("/v1/workshop/catalog")
        check.raise_for_status()
        args.directory.mkdir(parents=True, exist_ok=True)
        (args.directory / "workshop-before.json").write_text(json.dumps(before, indent=2) + "\n")
        patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": before["metadata"]["resourceVersion"]},
                 {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": desired}]
        expected_template = copy.deepcopy(before["spec"]["template"])
        expected_template["spec"]["containers"][0]["image"] = desired
        kubectl(args.context, "-n", "fs2-system", "patch", "deployment", "fs2-mindeval-workshop",
                "--type=json", "-p", json.dumps(patch))
        after = json.loads(kubectl(args.context, "-n", "fs2-system", "get", "deployment",
                                  "fs2-mindeval-workshop", "-o", "json"))
        assert after["spec"]["template"] == expected_template
        receipt = {"source_commit": "ab4927d6a", "previous": expected, "desired": desired,
                   "patch": patch, "only_image_changed": True, "active_runs_before": 0,
                   "public_catalog_before_status": check.status_code}
        (args.directory / "workshop-rollout.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    main(parser.parse_args())

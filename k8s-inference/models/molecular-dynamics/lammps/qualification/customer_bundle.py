"""Bundle all six sustained native scientific cases into one multi-job request."""

import argparse
import hashlib
import json
from pathlib import Path

from make_fixture import fixture, write_fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bundle, jobs, request = {}, [], None
    for case, steps in (("lj", 300000), ("eam", 150000), ("tersoff", 300000), ("snap", 150000), ("reaxff", 20000), ("rhodo", 50000)):
        body, files = fixture(case, args.assets, steps, warmup=2000, segment_seconds=60, trajectory_every=10000)
        if request is None:
            request = body
        job = body["jobs"][0]
        for stage in job["steps"]:
            stage["directory"] = case
        jobs.append(job)
        bundle.update({case + "/" + name: content for name, content in files.items()})
    request["jobs"] = jobs
    request["output_destination"] = "customer-bucket"
    request["output_prefix"] = "runs/lammps/sustained-six-case"
    write_fixture(args.output, request, bundle)
    print(json.dumps({"input": str(args.output / "input.tar.gz"), "request": str(args.output / "request.json"), "input_sha256": hashlib.sha256((args.output / "input.tar.gz").read_bytes()).hexdigest(), "request_sha256": hashlib.sha256((args.output / "request.json").read_bytes()).hexdigest(), "jobs": [j["id"] for j in jobs], "customer_ready": False}))


if __name__ == "__main__":
    main()

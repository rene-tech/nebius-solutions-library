"""Two fixed-release REST/MCP cohorts plus isolated recovery/delivery faults.

Only an explicit internal fleet and fresh receipt prefix are accepted. Jobs
share existing system/qa capacity; this driver never changes pool sizes/quotas.
"""

import argparse
import json
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import delivery_retry
import pilot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--benchmem-tpr", type=Path, required=True)
    parser.add_argument("--engine-image", required=True)
    args = parser.parse_args()
    if not args.prefix.replace("-", "").isalnum():
        raise ValueError("a simple owned receipt prefix is required")
    directory, prefix = args.private_dir, args.prefix
    if list(directory.glob(prefix + "-*.json")):
        raise ValueError("keep previous evidence; select a fresh qualification prefix")
    pilot.configure(args.deployment)
    pilot.keys()  # Resolve internal identity once, before parallel callers.

    def cohorts():
        for cohort in (1, 2):

            def execute(path, cohort=cohort):
                name = f"{prefix}-c{cohort}-{path}"
                pilot.submit(
                    directory,
                    name,
                    50000 if path == "rest" else 100000,
                    path,
                    benchmem_tpr=args.benchmem_tpr if path == "rest" else None,
                )
                pilot.wait_for(directory, name, path)
                if path == "mcp":
                    pilot.trajectories(directory, name, args.engine_image)
                pilot.boundaries(directory, name)

            with ThreadPoolExecutor(max_workers=2) as workers:
                list(workers.map(execute, ("rest", "mcp")))
        name = prefix + "-cancel"
        pilot.submit(directory, name, 250000, "rest", benchmem_tpr=args.benchmem_tpr)
        pilot.cancellation_cycle(directory, name)

    def recovery():
        name = prefix + "-recovery-mcp"
        pilot.submit(directory, name, 2000000, "mcp")
        pilot.automatic_recovery_cycle(directory, name)
        pilot.wait_for(directory, name, "mcp")
        pilot.trajectories(directory, name, args.engine_image)
        pilot.storage_denial(directory, name)

    def delivery():
        name = prefix + "-delivery-mcp"
        pilot.submit(directory, name, 100000, "mcp", benchmem_tpr=args.benchmem_tpr)
        delivery_retry.run(directory, name)
        pilot.storage_denial(directory, name)

    results = {"started_at": pilot.now(), "prefix": prefix, "gates": {}}
    with ThreadPoolExecutor(max_workers=3) as workers:
        futures = {
            workers.submit(fn): name
            for name, fn in (
                ("cohorts_and_cancellation", cohorts),
                ("automatic_recovery", recovery),
                ("cpu_delivery", delivery),
            )
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                future.result()
                results["gates"][name] = "passed"
            except Exception as error:
                # Details remain in each operation's private evidence files.
                results["gates"][name] = {"failed": type(error).__name__}
                pilot.write(
                    directory / (prefix + "-" + name + "-error.json"),
                    {"type": type(error).__name__, "traceback": traceback.format_exc()},
                    private=True,
                )
            pilot.write(directory / (prefix + "-qualification.json"), results)
            print(json.dumps({"gate": name, "result": results["gates"][name]}), flush=True)
    results["finished_at"] = pilot.now()
    pilot.write(directory / (prefix + "-qualification.json"), results)
    if any(value != "passed" for value in results["gates"].values()):
        raise SystemExit("qualification failed; inspect retained operation evidence")


if __name__ == "__main__":
    main()

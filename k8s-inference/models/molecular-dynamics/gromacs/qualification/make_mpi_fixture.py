"""Make bounded two-node fixtures from already verified public-system inputs."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=100000)
    parser.add_argument(
        "--gpu-pme",
        action="store_true",
        help="Explicit one-rank GPU PME split; compare rather than assume it is faster.",
    )
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, exist_ok=False)
    # The source is a task-generated fixture with its own input digest/receipt,
    # not an unverified download or customer directory.
    source = json.loads((args.source / "request.json").read_text())
    original = source["jobs"][0]
    steps = original["steps"]
    if original["id"] == "stmv":
        steps = [
            steps[0],
            steps[1],
            {"id": "join-energies", "command": "eneconv", "args": ["-f", {"files": "md-1.part*.edr"}, "-o", "md.edr"]},
            {"id": "energies", "command": "energy", "args": ["-f", "md.edr", "-o", "energies.xvg"], "stdin": "Potential\nTotal-Energy\nTemperature\n0\n"},
            {"id": "check", "command": "check", "args": ["-e", "md.edr"]},
        ]
        steps[0]["args"][-1] = str(args.steps)
        if args.gpu_pme:
            steps[1]["args"] += [
                "-npme",
                "1",
                "-pme",
                "gpu",
                "-nb",
                "gpu",
                "-update",
                "gpu",
            ]
    else:
        raise ValueError("Use the pinned public STMV fixture for this two-node probe")
    request = {
        **source,
        "schema": "fs2-serve.nebius.ai/gromacs-mpi-workflow-request/v1",
        "nodes": 2,
        "jobs": [{"id": "gang", "steps": steps}],
        "segment_minutes": 0.5,
        "checkpoint_minutes": 0.5,
    }
    shutil.copyfile(args.source / "input.tar.gz", args.output / "input.tar.gz")
    (args.output / "request.json").write_text(json.dumps(request, indent=2) + "\n")
    original_fixture = json.loads((args.source / "fixture.json").read_text())
    (args.output / "fixture.json").write_text(
        json.dumps(
            {
                **original_fixture,
                "steps": args.steps,
                "repetitions": 1,
                "nodes": 2,
                "gpu_pme": args.gpu_pme,
                "input_bundle_sha256": hashlib.file_digest(
                    (args.output / "input.tar.gz").open("rb"), "sha256"
                ).hexdigest(),
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps({"output": str(args.output), "steps": args.steps, "nodes": 2}))


if __name__ == "__main__":
    main()

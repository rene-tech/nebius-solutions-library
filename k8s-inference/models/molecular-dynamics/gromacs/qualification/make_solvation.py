"""Adapt the official CC-BY-4.0 ethanol solvation tutorial for runtime testing.

This does not establish a converged free energy or a production FEP protocol.
The upstream tutorial deliberately uses coupled Coulomb/VDW lambda states.
Copy only from the pinned upstream checkout; retain attribution and hashes.
"""

import argparse
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

REPO = "https://gitlab.com/gromacs/online-tutorials/free-energy-of-solvation"
REVISION = "4528c50cffbfe9efad826ef589a63c6ad2a8105b"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (
        subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=args.checkout, text=True
        ).strip()
        != REVISION
    ):
        raise ValueError("tutorial checkout differs from the pinned revision")
    args.output.mkdir(parents=True, exist_ok=False)
    data = args.output / "data"
    data.mkdir()
    (data / "UPSTREAM-LICENSE.txt").write_bytes(
        (args.checkout / "LICENSE").read_bytes()
    )
    attribution = {
        "source": REPO,
        "revision": REVISION,
        "license": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "authors": "GROMACS online tutorials contributors; see the pinned repository and tutorial credits",
        "changes": "Independent fixed stochastic seed per lambda; explicit trajectory output; ordered native commands.",
        "purpose": "Native free-energy workflow execution qualification, not a converged scientific result.",
        "source_files": {},
    }
    steps = []
    for index in range(7):
        upstream = args.checkout / f"reference/lambda_{index:02d}"
        directory = f"lambda-{index:02d}"
        target = data / directory
        target.mkdir()
        for name in ("conf.gro", "topol.top", "grompp.mdp"):
            raw = (upstream / name).read_bytes()
            attribution["source_files"][f"reference/lambda_{index:02d}/{name}"] = (
                hashlib.sha256(raw).hexdigest()
            )
            if name.endswith(".mdp"):
                raw += f"\nld-seed = {20260923 + index}\nnstxout-compressed = 1000\n".encode()
            (target / name).write_bytes(raw)
        steps.extend(
            [
                {
                    "id": f"prepare-{index}",
                    "directory": directory,
                    "command": "grompp",
                    "args": [
                        "-f",
                        "grompp.mdp",
                        "-c",
                        "conf.gro",
                        "-p",
                        "topol.top",
                        "-o",
                        "run.tpr",
                    ],
                },
                {
                    "id": f"simulate-{index}",
                    "directory": directory,
                    "command": "mdrun",
                    "args": ["-s", "run.tpr", "-deffnm", "run", "-dhdl", "dhdl.xvg"],
                },
            ]
        )
    steps.append(
        {
            "id": "bar-analysis",
            "command": "bar",
            "args": [
                "-f",
                {"files": "lambda-*/dhdl.part*.xvg"},
                "-o",
                "bar.xvg",
                "-oi",
                "bar-integral.xvg",
                "-oh",
                "bar-histogram.xvg",
                "-b",
                "20",
            ],
            "expected_outputs": ["bar.xvg", "bar-integral.xvg", "bar-histogram.xvg"],
        }
    )
    request = {
        "schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1",
        "threads": 8,
        "segment_minutes": 5,
        "checkpoint_minutes": 5,
        "max_wall_seconds": 3600,
        "jobs": [{"id": "solvation", "steps": steps}],
    }
    (data / "ATTRIBUTION.json").write_text(json.dumps(attribution, indent=2) + "\n")
    (args.output / "request.json").write_text(json.dumps(request, indent=2) + "\n")
    with tarfile.open(args.output / "input.tar.gz", "w:gz") as archive:
        for file in sorted(data.rglob("*")):
            if file.is_file():
                archive.add(file, arcname=file.relative_to(data))
    print(args.output)


if __name__ == "__main__":
    main()

"""Compose a single GROMACS+MPI release, preserving the captured live baseline.

Prepare the enhanced single-GPU successor first, so the MPI addition freezes
that successor in its qualification baseline. This edits source only; deployment
still compares the original captured live release in release_backend.py.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--single-image", required=True)
    parser.add_argument("--mpi-image", required=True)
    parser.add_argument("--single-result", type=Path, action="append", required=True)
    parser.add_argument("--mpi-result", type=Path, action="append", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    prepare = str(Path(__file__).with_name("prepare.py"))
    single = args.output / "single"
    subprocess.run(
        [
            sys.executable,
            prepare,
            "--baseline",
            str(args.baseline),
            "--model",
            "gromacs",
            "--runtime-image",
            args.single_image,
            "--output",
            str(single),
            "--replace-existing",
            "--publish-catalog",
            *[
                item
                for directory in args.single_result
                for item in ("--gpu-result", str(directory))
            ],
        ],
        check=True,
    )
    baseline = copy.deepcopy(json.loads((args.baseline / "values.json").read_text()))
    overlay = json.loads((single / "activation.values.json").read_text())
    for key, value in overlay.items():
        baseline[key].update(value)
    composed = args.output / "composed-baseline"
    composed.mkdir(mode=0o700)
    (composed / "values.json").write_text(json.dumps(baseline))
    cm = json.loads((single / "scheduling.configmap.json").read_text())
    (composed / "scheduling.json").write_text(
        cm["data"][baseline["scientificBatch"]["schedulingContractKey"]]
    )
    subprocess.run(
        [
            sys.executable,
            prepare,
            "--baseline",
            str(composed),
            "--model",
            "gromacs-mpi",
            "--runtime-image",
            args.mpi_image,
            "--output",
            str(args.output / "combined"),
            "--publish-catalog",
            *[
                item
                for directory in args.mpi_result
                for item in ("--gpu-result", str(directory))
            ],
        ],
        check=True,
    )
    print(
        json.dumps(
            {
                "activation": str(args.output / "combined"),
                "source_updated": True,
                "deployed": False,
            }
        )
    )


if __name__ == "__main__":
    main()

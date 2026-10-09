"""Create or verify the complete delivered case's portable SHA-256 inventory.

This is an artifact-completeness/integrity check, not a scientific equivalence,
convergence or whole-platform readiness decision. Scientific receipts are kept
in the same inventory and must be read with their stated scope.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ENGINES = ("gromacs", "namd", "amber", "lammps")
MANIFEST = "delivery-manifest.json"
REQUIRED = (
    "README.md",
    "REPRODUCE.md",
    "PREPARATION.md",
    "case.json",
    "run_case.py",
    "master/master-manifest.json",
    "master/system.prmtop",
    "master/system.rst7",
    "analysis-inputs/spec.json",
    "analysis-inputs/regenerate.py",
    "analysis/comparison.csv",
    "analysis/comparison.md",
    "analysis/phi-psi-timeseries.png",
    "analysis/phi-psi-distributions.png",
    "analysis/receipt.json",
    "videos/four-engine-2x2.mp4",
    "videos/receipt.json",
) + tuple(
    name
    for engine in ENGINES
    for name in (
        f"inputs/{engine}/input.tar.gz",
        f"inputs/{engine}/request.json",
        f"runs/{engine}/result.json",
        f"runs/{engine}/request.json",
        f"videos/{engine}.mp4",
    )
)


def inventory(root: Path) -> list[dict]:
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(
                f"Deliver actual files, not environment-dependent links: {path.relative_to(root)}"
            )
        if path.is_dir() or path == root / MANIFEST:
            continue
        if not path.is_file():
            raise ValueError(f"Unsupported non-file artifact: {path.relative_to(root)}")
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        result.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": checksum,
            }
        )
    return result


def check_required(root: Path) -> None:
    missing = [name for name in REQUIRED if not (root / name).is_file()]
    if missing:
        raise ValueError("Required deliverables are missing: " + ", ".join(missing))
    analysis = json.loads((root / "analysis/receipt.json").read_text())
    video = json.loads((root / "videos/receipt.json").read_text())
    if analysis.get("status") != "analysis-passed":
        raise ValueError("The four-engine analysis is not complete.")
    if video.get("status") != "real-trajectories-rendered-and-encoding-validated":
        raise ValueError("The four-engine video is not complete.")


def run(root: Path, create: bool = False) -> dict:
    root = root.resolve(strict=True)
    check_required(root)
    current = inventory(root)
    path = root / MANIFEST
    if create:
        document = {
            "schema": "fs2-md-complete-delivery-inventory/v1",
            "scope": "Complete file inventory and integrity, not a replacement for scientific acceptance receipts.",
            "file_count": len(current),
            "total_bytes": sum(row["bytes"] for row in current),
            "files": current,
        }
        with path.open("x") as stream:
            json.dump(document, stream, indent=2)
            stream.write("\n")
    else:
        document = json.loads(path.read_text())
        if (
            document.get("schema") != "fs2-md-complete-delivery-inventory/v1"
            or document.get("files") != current
            or document.get("file_count") != len(current)
            or document.get("total_bytes") != sum(row["bytes"] for row in current)
        ):
            raise ValueError(
                "Delivery contents differ: missing, changed or extra files; preserve the original evidence."
            )
    return {
        "status": "inventory-created" if create else "inventory-verified",
        "file_count": len(current),
        "total_bytes": document["total_bytes"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--create",
        action="store_true",
        help="Create a new manifest once; never overwrite an existing one.",
    )
    args = parser.parse_args()
    print(json.dumps(run(args.directory, args.create)))

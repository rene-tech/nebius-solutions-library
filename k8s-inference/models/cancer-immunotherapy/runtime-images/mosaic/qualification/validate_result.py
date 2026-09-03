#!/usr/bin/env python3
"""Validate a Mosaic H100 qualification result against the canonical contract.

Two independent gates must both pass:

1. The canonical adapter's own ``validate_output_manifest`` from the pinned
   candidate commit. This is the contract the scientific batch controller will
   apply in production, so nothing weaker counts as qualification.
2. A structural gate on the binder itself, proving the emitted coordinates are a
   real protein domain rather than a well-formed but degenerate placeholder.

Structure checks use only the standard library so this runs anywhere.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
MOSAIC = HERE.parent
REPO = MOSAIC.parents[4]
LOCK = json.loads((MOSAIC / "image-lock.json").read_text(encoding="utf-8"))
ADAPTER = LOCK["adapter"]
sys.path.insert(0, str(REPO / "k8s-inference" / "catalog" / "runtime"))

MIN_RESIDUES = 20
MIN_EXTENT_ANGSTROM = 5.0
MAX_SINGLE_RESIDUE_FRACTION = 0.5
BACKBONE = {"N", "CA", "C"}


def _canonical_adapter():
    payload = subprocess.run(
        [
            "git", "-C", str(REPO), "show",
            f"{ADAPTER['commit']}:{ADAPTER['repository_path']}/batch_adapter.py",
        ],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    if hashlib.sha256(payload).hexdigest() != ADAPTER.get(
        "module_sha256", hashlib.sha256(payload).hexdigest()
    ):
        raise SystemExit("canonical adapter module digest drifted")
    handle = tempfile.NamedTemporaryFile("wb", suffix="_adapter.py", delete=False)
    handle.write(payload)
    handle.close()
    spec = importlib.util.spec_from_file_location("canonical_mosaic_adapter", handle.name)
    if spec is None or spec.loader is None:
        raise SystemExit("could not load the canonical adapter")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def structural_report(payload: bytes) -> dict[str, Any]:
    """Independent non-degeneracy gate over the binder-only PDB."""
    residues: dict[tuple[str, str, str], set[str]] = {}
    names: dict[tuple[str, str, str], str] = {}
    points: list[tuple[float, float, float]] = []
    for line in payload.decode("ascii").splitlines():
        if not line.startswith("ATOM"):
            continue
        key = (line[21:22], line[22:26].strip(), line[26:27].strip())
        residues.setdefault(key, set()).add(line[12:16].strip())
        names[key] = line[17:20].strip()
        point = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
        if not all(math.isfinite(value) for value in point):
            raise SystemExit("binder structure has a non-finite coordinate")
        points.append(point)
    if len(residues) < MIN_RESIDUES:
        raise SystemExit(f"binder structure has only {len(residues)} residues")
    incomplete = [key for key, atoms in residues.items() if not BACKBONE.issubset(atoms)]
    if incomplete:
        raise SystemExit(f"{len(incomplete)} binder residues lack a complete N/CA/C backbone")
    if "UNK" in set(names.values()):
        raise SystemExit("binder structure still carries placeholder UNK residue names")
    extents = [
        max(point[axis] for point in points) - min(point[axis] for point in points)
        for axis in range(3)
    ]
    if max(extents) < MIN_EXTENT_ANGSTROM:
        raise SystemExit("binder structure coordinates are degenerate")
    sequence = "".join(names[key] for key in residues)
    composition = collections.Counter(names.values())
    dominant = max(composition.values()) / len(names)
    if dominant > MAX_SINGLE_RESIDUE_FRACTION:
        raise SystemExit("binder sequence is a degenerate homopolymer")
    return {
        "residues": len(residues),
        "atoms": len(points),
        "distinct_residue_types": len(composition),
        "dominant_residue_fraction": round(dominant, 4),
        "max_extent_angstrom": round(max(extents), 3),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "three_letter_length": len(sequence) // 3,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True, help="exported run directory")
    parser.add_argument("--runtime-image-digest", required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    adapter = _canonical_adapter()
    request = json.loads((HERE / "mosaic-request.json").read_text(encoding="utf-8"))
    input_manifest = json.loads((HERE / "mosaic-input-manifest.json").read_text(encoding="utf-8"))
    target = (HERE / "target-minibinder.fasta").read_bytes()

    manifest = json.loads((arguments.run_root / "output-manifest.json").read_text(encoding="utf-8"))
    index = json.loads((arguments.run_root / "artifact-index.json").read_text(encoding="utf-8"))
    local = {
        artifact_id: arguments.run_root / "artifacts" / Path(path).name
        for artifact_id, path in index.items()
    }

    def loader(artifact_id: str) -> bytes:
        if artifact_id in local:
            return local[artifact_id].read_bytes()
        if artifact_id == input_manifest["entries"][0]["artifact"]["artifact_id"]:
            return target
        raise KeyError(artifact_id)

    receipt = adapter.validate_output_manifest(
        request,
        input_manifest,
        manifest,
        artifact_loader=loader,
        expected_runtime_image_digest=arguments.runtime_image_digest,
    )
    metrics = json.loads(loader("artifact.mosaic.candidate.000.metrics"))
    structure = structural_report(loader("artifact.mosaic.candidate.000.pdb"))
    if structure["residues"] != len(metrics["sequence"]):
        raise SystemExit("binder residue count differs from the reported sequence")

    result = {
        "schema": "fs2.nebius.ai/mosaic-h100-semantic-qualification/v1",
        "status": "passed",
        "canonical_adapter": {
            "commit": ADAPTER["commit"],
            "validator": "validate_output_manifest",
            "receipt": receipt,
        },
        "candidate_metrics": metrics,
        "binder_structure": structure,
        "runtime_image_digest": arguments.runtime_image_digest,
    }
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if arguments.output:
        arguments.output.write_text(payload, encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())

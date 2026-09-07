"""Run the unchanged BoltzGen design stage and validate all generated structures."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess


def validate_structure(path: Path) -> dict:
    import gemmi
    import numpy as np

    structure = gemmi.read_structure(str(path))
    if len(structure) != 1 or len(structure[0]) != 2:
        raise ValueError(f"expected one two-chain generated complex: {path.name}")
    atoms = 0
    for chain in structure[0]:
        for residue in chain:
            for atom in residue:
                if not all(math.isfinite(v) for v in (atom.pos.x, atom.pos.y, atom.pos.z)):
                    raise ValueError(f"nonfinite generated coordinates: {path.name}")
                atoms += 1
    if atoms < 500:
        raise ValueError(f"incomplete generated structure: {path.name}")
    metadata = path.with_suffix(".npz")
    with np.load(metadata, allow_pickle=False) as data:
        if not {"design_mask", "mol_type"}.issubset(data.files):
            raise ValueError("generated metadata misses required fields")
        designed = int(np.asarray(data["design_mask"], dtype=bool).sum())
        if not 60 <= designed <= 80:
            raise ValueError("generated binder length differs from accepted fixture")
    return {
        "filename": path.name,
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "metadata_sha256": hashlib.sha256(metadata.read_bytes()).hexdigest(),
        "atoms": atoms,
        "designed_residues": designed,
    }


def main() -> int:
    command = json.loads(Path("/benchmark/command.json").read_text())
    code = subprocess.run(command, check=False).returncode
    if code:
        return code
    root = Path(os.environ["FS2_RUN_ROOT"])
    paths = sorted(path for path in root.rglob("intermediate_designs/*.cif")
                   if not path.name.endswith("_native.cif"))
    # This campaign freezes the currently accepted 20-candidate PD-L1 fixture.
    if len(paths) != 20:
        raise ValueError(f"expected all 20 unchanged design candidates, found {len(paths)}")
    structures = [validate_structure(path) for path in paths]
    print("FS2_STARTUP " + json.dumps({
        "schema": "fs2-startup-benchmark/v1", "model_id": "boltzgen",
        "phase": "semantic_output_verified", "utc": datetime.now(UTC).isoformat(),
        "unchanged_runtime_exit_code": 0, "structures": structures,
        "scope": "complete design stage, not later folding/filtering stages or scientific affinity qualification",
    }, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

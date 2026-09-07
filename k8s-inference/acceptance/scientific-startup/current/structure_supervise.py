"""Run unchanged scientific argv and independently inspect its full outputs."""

import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone


def emit(phase, **extra):
    print(
        "FS2_STARTUP "
        + json.dumps(
            {
                "phase": phase,
                "model_id": os.environ["FS2_STARTUP_BENCHMARK_MODEL"],
                "monotonic_seconds": time.monotonic(),
                "utc": datetime.now(timezone.utc).isoformat(),
                **extra,
            },
            sort_keys=True,
        ),
        flush=True,
    )


def finite_cif_atoms(path):
    """Read the atom-site loop emitted by AF3 without adding runtime packages."""
    headers = []
    count = 0
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("_atom_site."):
            headers.append(line.split()[0])
            continue
        if not headers or not line:
            continue
        if line == "#" or line == "loop_" or line.startswith("_"):
            if count:
                break
            continue
        fields = shlex.split(line)
        if len(fields) != len(headers):
            raise ValueError("unexpected atom-site output row")
        values = [
            float(fields[headers.index("_atom_site.Cartn_" + axis)]) for axis in "xyz"
        ]
        assert all(math.isfinite(value) for value in values)
        count += 1
    assert count > 0, "no finite structural atoms"
    return count


command = json.loads(Path("/benchmark/command.json").read_bytes())
emit(
    "original_command_start",
    argv_sha256=hashlib.sha256(
        json.dumps(command, separators=(",", ":")).encode()
    ).hexdigest(),
)
code = subprocess.run(command, check=False).returncode
if code:
    emit("original_command_failed", exit_code=code)
    raise SystemExit(code)

root = Path(os.environ["FS2_RUN_ROOT"])
structures = sorted(root.rglob("*.cif")) + sorted(root.rglob("*.pdb"))
assert structures, "unchanged runtime returned no scientific structure"
verified = []
for path in structures:
    # The unchanged model wrapper performs its production confidence checks.
    # Independently require actual finite-coordinate structural atoms as well.
    try:
        import gemmi

        structure = gemmi.read_structure(str(path))
        atoms = [
            atom
            for model in structure
            for chain in model
            for residue in chain
            for atom in residue
        ]
        assert atoms and all(
            math.isfinite(v)
            for atom in atoms
            for v in (atom.pos.x, atom.pos.y, atom.pos.z)
        )
        count = len(atoms)
    except ImportError:
        try:
            import numpy as np
            from biotite.structure.io import load_structure
        except ImportError:
            count = finite_cif_atoms(path)
        else:
            structure = load_structure(str(path))
            assert structure.array_length() > 0 and np.isfinite(structure.coord).all()
            count = structure.array_length()
    verified.append(
        {
            "filename": str(path.relative_to(root)),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "atoms": count,
        }
    )
emit("semantic_output_verified", structures=verified, unchanged_runtime_exit_code=0)
collector = Path("/benchmark/collect_environment.py")
if collector.exists():
    # Inventory runs only after both measured boundaries and output validation.
    # Its imports/tool probes must not warm or perturb the measured model load.
    output = root / "benchmark-environment.json"
    environment = dict(os.environ)
    environment.pop("FS2_STARTUP_BENCHMARK_MODEL", None)
    collected = subprocess.run(
        [sys.executable, str(collector), "--output", str(output)],
        check=False,
        env=environment,
    )
    if collected.returncode != 0:
        raise SystemExit("read-only environment collection failed")
    print("FS2_ENVIRONMENT " + output.read_text().replace("\n", " "), flush=True)

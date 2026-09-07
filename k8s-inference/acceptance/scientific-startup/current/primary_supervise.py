"""Run the unchanged primary-model argv and independently inspect its outputs."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone
from typing import Any


def emit(phase: str, **extra: Any) -> None:
    print(
        "FS2_STARTUP "
        + json.dumps(
            {
                "schema": "fs2-startup-benchmark/v1",
                "model_id": os.environ["FS2_STARTUP_BENCHMARK_MODEL"],
                "phase": phase,
                "monotonic_seconds": time.monotonic(),
                "utc": datetime.now(timezone.utc).isoformat(),
                **extra,
            },
            sort_keys=True,
        ),
        flush=True,
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finite_atoms(path: Path) -> int:
    count = 0
    for line in path.read_text(encoding="ascii", errors="strict").splitlines():
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        if len(line) < 54:
            raise AssertionError(f"truncated coordinate record in {path}")
        coordinates = tuple(float(line[index:index + 8]) for index in (30, 38, 46))
        if not all(math.isfinite(value) for value in coordinates):
            raise AssertionError(f"non-finite coordinate in {path}")
        count += 1
    if count == 0:
        raise AssertionError(f"no structural atoms in {path}")
    return count


def finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AssertionError(f"{label} is not numeric")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise AssertionError(f"{label} is not finite")
    return numeric


command = json.loads(Path("/benchmark/command.json").read_text(encoding="utf-8"))
command_hash = hashlib.sha256(
    json.dumps(command, separators=(",", ":")).encode()
).hexdigest()
emit("original_command_start", argv_sha256=command_hash)
code = subprocess.run(command, check=False).returncode
if code:
    emit("original_command_failed", exit_code=code)
    raise SystemExit(code)

root = Path(os.environ["FS2_RUN_ROOT"])
model = os.environ["FS2_STARTUP_BENCHMARK_MODEL"]
if model == "mosaic":
    structures = sorted(root.rglob("candidate.pdb"))
    metrics_paths = sorted(root.rglob("candidate-metrics.json"))
    required_metrics = ("iptm", "mean_plddt", "objective")
elif model == "bindcraft":
    structures = sorted(root.rglob("artifacts/candidate-*.pdb"))
    structures = [path for path in structures if "relaxed-complex" not in path.name]
    metrics_paths = sorted(root.rglob("artifacts/candidate-*-metrics.json"))
    required_metrics = (
        "iptm",
        "mean_plddt",
        "interface_dg",
        "shape_complementarity",
        "buried_interface_area",
        "binder_energy_score",
    )
    shard_outputs = sorted(root.rglob("shard-output.json"))
    if not shard_outputs:
        raise AssertionError("BindCraft runtime returned no shard-output contract")
else:
    raise AssertionError(f"unsupported primary isolated model: {model}")

if not structures or not metrics_paths:
    raise AssertionError("unchanged runtime returned no validated structure/metrics pair")
verified_structures = [
    {
        "filename": str(path.relative_to(root)),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "atoms": finite_atoms(path),
    }
    for path in structures
]
verified_metrics = []
for path in metrics_paths:
    value = json.loads(path.read_text(encoding="utf-8"))
    for key in required_metrics:
        finite_number(value.get(key), f"{path.name}.{key}")
    sequence = value.get("sequence")
    if not isinstance(sequence, str) or not sequence.isalpha():
        raise AssertionError(f"{path.name}.sequence is not a protein sequence")
    verified_metrics.append(
        {
            "filename": str(path.relative_to(root)),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
            "sequence_length": len(sequence),
        }
    )

emit(
    "semantic_output_verified",
    unchanged_runtime_exit_code=0,
    structures=verified_structures,
    metrics=verified_metrics,
)

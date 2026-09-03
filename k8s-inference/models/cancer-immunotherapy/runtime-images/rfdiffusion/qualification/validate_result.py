#!/usr/bin/env python3
"""Independent acceptance gate over an exported RFdiffusion run directory.

This deliberately does not trust the runtime's own result envelope. It re-derives
every claim from the exported bytes and only then checks that the envelope agrees.
A run passes when both hold: the structures are real, and the runtime described
them honestly.

Structural criteria follow the committed semantic validator spec in
models/cancer-immunotherapy/model-source-qualification.json:

* every residue carries a complete N/CA/C backbone
* consecutive CA-CA spacing is physical (~3.8 A)
* the chain is not a degenerate point cloud
* the diffused region is emitted as glycine, which is what upstream writes for
  positions it designed rather than copied from a motif
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pickle
import sys
from pathlib import Path
from typing import Any

MIN_CA_CA = 3.4
MAX_CA_CA = 4.2
MIN_EXTENT_ANGSTROM = 5.0
BACKBONE = {"N", "CA", "C"}


class ValidationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_atoms(payload: str, name: str) -> list[dict[str, Any]]:
    atoms: list[dict[str, Any]] = []
    for line in payload.splitlines():
        if not line.startswith("ATOM"):
            continue
        if len(line) < 54:
            raise ValidationError(f"{name}: truncated ATOM record")
        try:
            coords = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
        except ValueError as exc:
            raise ValidationError(f"{name}: unparseable coordinates") from exc
        for value in coords:
            if not math.isfinite(value):
                raise ValidationError(f"{name}: non-finite coordinate")
        atoms.append(
            {
                "atom": line[12:16].strip(),
                "residue": line[17:20].strip(),
                "chain": line[21:22].strip() or " ",
                "seq": int(line[22:26]),
                "coords": coords,
            }
        )
    if not atoms:
        raise ValidationError(f"{name}: no ATOM records")
    return atoms


def structural_report(path: Path) -> dict[str, Any]:
    payload = path.read_text(encoding="utf-8")
    atoms = parse_atoms(payload, path.name)

    residues: dict[tuple[str, int], dict[str, Any]] = {}
    order: list[tuple[str, int]] = []
    for atom in atoms:
        key = (atom["chain"], atom["seq"])
        if key not in residues:
            residues[key] = {"name": atom["residue"], "atoms": {}}
            order.append(key)
        residues[key]["atoms"][atom["atom"]] = atom["coords"]

    incomplete = [k for k in order if not BACKBONE.issubset(residues[k]["atoms"])]
    if incomplete:
        raise ValidationError(
            f"{path.name}: {len(incomplete)} residues lack a complete N/CA/C backbone"
        )

    ca = [residues[k]["atoms"]["CA"] for k in order]
    spans = []
    for axis in range(3):
        values = [c[axis] for c in ca]
        spans.append(max(values) - min(values))
    extent = max(spans)
    if extent < MIN_EXTENT_ANGSTROM:
        raise ValidationError(
            f"{path.name}: CA cloud spans only {extent:.2f} A; this is not a structure"
        )

    breaks = []
    worst = 0.0
    for index in range(1, len(order)):
        if order[index][0] != order[index - 1][0]:
            continue  # chain break, not a bond
        a, b = ca[index - 1], ca[index]
        distance = math.dist(a, b)
        worst = max(worst, abs(distance - 3.8))
        if not (MIN_CA_CA <= distance <= MAX_CA_CA):
            breaks.append({"after_residue": order[index - 1][1], "distance": round(distance, 3)})
    if breaks:
        raise ValidationError(
            f"{path.name}: {len(breaks)} non-physical CA-CA distances, first {breaks[0]}"
        )

    counts: dict[str, int] = {}
    for key in order:
        counts[residues[key]["name"]] = counts.get(residues[key]["name"], 0) + 1

    return {
        "residues": len(order),
        "atoms": len(atoms),
        "chains": sorted({k[0] for k in order}),
        "residue_counts": counts,
        "glycine_fraction": round(counts.get("GLY", 0) / len(order), 4),
        "max_ca_ca_deviation_angstrom": round(worst, 3),
        "max_extent_angstrom": round(extent, 3),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def run_metadata(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        payload = pickle.load(handle)
    device = payload.get("device")
    if not isinstance(device, str) or device.strip().upper() == "CPU":
        raise ValidationError(f"{path.name}: run metadata does not record a CUDA device")
    return {
        "device": device,
        "seconds": round(float(payload.get("time") or 0.0), 3),
        "sha256": sha256_file(path),
        "motif_positions": len(payload.get("con_ref_pdb_idx") or []),
    }


def validate(run_root: Path, expected_digest: str) -> dict[str, Any]:
    envelope_path = run_root / "result.json"
    if not envelope_path.is_file():
        raise ValidationError(f"no result envelope at {envelope_path}")
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))

    if envelope.get("status") != "succeeded":
        raise ValidationError(
            f"runtime reported status {envelope.get('status')!r}: "
            f"{(envelope.get('error') or {}).get('message')}"
        )
    if not envelope.get("designs"):
        raise ValidationError("envelope declares no designs")

    checkpoint = envelope.get("checkpoint") or {}
    if not checkpoint.get("digest_verified"):
        raise ValidationError("the runtime did not verify the checkpoint digest")

    accelerator = envelope.get("accelerator") or {}
    if not accelerator.get("cuda_execution_confirmed"):
        raise ValidationError("the envelope does not confirm CUDA execution")

    cache = envelope.get("cache_level") or {}
    if cache.get("gpu_snapshot_used") is not False:
        raise ValidationError("cache level must state explicitly that no GPU snapshot was used")

    designs: list[dict[str, Any]] = []
    for declared in envelope["designs"]:
        pdb_path = run_root / declared["pdb"]["path"]
        trb_path = run_root / declared["run_metadata"]["path"]
        for path in (pdb_path, trb_path):
            if not path.is_file():
                raise ValidationError(f"declared artifact is absent: {path}")

        structure = structural_report(pdb_path)
        metadata = run_metadata(trb_path)

        if structure["sha256"] != declared["pdb"]["sha256"]:
            raise ValidationError(
                f"{pdb_path.name}: envelope declares sha256 {declared['pdb']['sha256']}, "
                f"exported bytes hash to {structure['sha256']}"
            )
        if metadata["sha256"] != declared["run_metadata"]["sha256"]:
            raise ValidationError(f"{trb_path.name}: run metadata digest disagrees with the envelope")
        if structure["residues"] != declared["residue_count"]:
            raise ValidationError(
                f"{pdb_path.name}: envelope declares {declared['residue_count']} residues, "
                f"structure has {structure['residues']}"
            )
        if metadata["device"] != declared["device"]:
            raise ValidationError(f"{trb_path.name}: device disagrees with the envelope")

        requested = envelope["request"]["requested_residues"]
        if not (requested["minimum"] <= structure["residues"] <= requested["maximum"]):
            raise ValidationError(
                f"{pdb_path.name}: {structure['residues']} residues is outside the "
                f"requested {requested['minimum']}..{requested['maximum']}"
            )

        if envelope["operation"] == "scaffold-motif":
            if declared.get("motif_positions_preserved") != metadata["motif_positions"]:
                raise ValidationError(
                    f"{trb_path.name}: motif position count disagrees with the envelope"
                )
            if declared.get("motif_ca_rmsd_angstrom") is None:
                raise ValidationError("a scaffold-motif design must report a motif CA RMSD")
        else:
            if structure["glycine_fraction"] < 1.0:
                raise ValidationError(
                    f"{pdb_path.name}: an unconditional design must be all glycine, got "
                    f"{structure['glycine_fraction']:.3f}"
                )
            if declared.get("motif_ca_rmsd_angstrom") is not None:
                raise ValidationError("an unconditional design must not report a motif RMSD")

        designs.append(
            {
                "design_index": declared["design_index"],
                "seed": declared["seed"],
                "structure": structure,
                "run_metadata": metadata,
                "trajectory_files": declared.get("trajectory_files") or [],
            }
        )

    return {
        "schema": "fs2.nebius.ai/rfdiffusion-h100-semantic-qualification/v1",
        "status": "passed",
        "runtime_image_digest": expected_digest,
        "operation": envelope["operation"],
        "request": envelope["request"],
        "checkpoint": checkpoint,
        "accelerator": accelerator,
        "cache_level": cache,
        "phases_seconds": envelope.get("phases_seconds"),
        "total_seconds": envelope.get("total_seconds"),
        "upstream": envelope.get("upstream"),
        "upstream_argv": envelope.get("upstream_argv"),
        "designs": designs,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--runtime-image-digest", required=True)
    parser.add_argument("--output", default="-")
    args = parser.parse_args(argv)

    try:
        report = validate(args.run_root, args.runtime_image_digest)
    except ValidationError as exc:
        print(f"QUALIFICATION FAILED: {exc}", file=sys.stderr)
        return 1

    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        sys.stdout.write(payload)
    else:
        Path(args.output).write_text(payload, encoding="utf-8")
        print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

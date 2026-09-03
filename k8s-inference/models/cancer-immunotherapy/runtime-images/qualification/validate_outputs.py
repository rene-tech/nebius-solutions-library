#!/usr/bin/env python3
"""Validate that a qualification run produced nondegenerate model-domain output."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import gemmi


AA = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}
REQUIRED = {
    "proteina-complexa": {"proteina-complexa/complexa.ckpt", "proteina-complexa/complexa_ae.ckpt"},
    "boltzgen": {"boltzgen/boltzgen1_diverse.ckpt", "boltzgen/mols.zip"},
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _external_artifacts(model: str, root: Path, receipt_path: Path) -> list[dict[str, Any]]:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    records = {record["path"]: record for record in receipt["artifacts"]}
    missing = REQUIRED[model] - records.keys()
    if missing:
        raise SystemExit(f"external artifact receipt is incomplete: {sorted(missing)}")
    selected = []
    for relative in sorted(REQUIRED[model]):
        path = root / relative
        record = records[relative]
        if path.stat().st_size != record["bytes"] or _sha256(path) != record["sha256"]:
            raise SystemExit(f"external artifact differs from receipt: {relative}")
        selected.append(record)
    return selected


def _structure(path: Path) -> dict[str, Any]:
    structure = gemmi.read_structure(str(path))
    sequences: list[str] = []
    chain_extents: list[float] = []
    atom_count = 0
    for chain in structure[0]:
        sequence = []
        positions: list[tuple[float, float, float]] = []
        for residue in chain:
            if residue.name not in AA:
                continue
            sequence.append(AA[residue.name])
            for atom in residue:
                xyz = (float(atom.pos.x), float(atom.pos.y), float(atom.pos.z))
                if not all(math.isfinite(value) for value in xyz):
                    raise SystemExit(f"non-finite atom coordinate: {path}")
                positions.append(xyz)
        if sequence:
            chain_sequence = "".join(sequence)
            if len(chain_sequence) < 20 or len(positions) < len(chain_sequence) * 3:
                raise SystemExit(f"structure lacks a complete protein domain: {path} chain={chain.name}")
            extents = [
                max(point[i] for point in positions) - min(point[i] for point in positions)
                for i in range(3)
            ]
            chain_extent = max(extents)
            if not 5.0 <= chain_extent <= 1_000.0:
                raise SystemExit(
                    f"structure coordinates are degenerate or retain diffusion sentinels: "
                    f"{path} chain={chain.name} extent={chain_extent}"
                )
            if max(collections.Counter(chain_sequence).values()) / len(chain_sequence) > 0.5:
                raise SystemExit(f"structure sequence is a degenerate homopolymer: {path}")
            sequences.append(chain_sequence)
            chain_extents.append(chain_extent)
            atom_count += len(positions)
    residues = sum(map(len, sequences))
    if not sequences:
        raise SystemExit(f"structure lacks a protein domain: {path}")
    sequence = "".join(sequences)
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "chains": len(sequences),
        "residues": residues,
        "atoms": atom_count,
        "sequence_diversity": len(set(sequence)),
        "max_chain_extent_angstrom": max(chain_extents),
    }


def _standard(model: str, output: Path) -> dict[str, Any]:
    search_root = output
    if model == "boltzgen" and (output / "intermediate_designs").is_dir():
        search_root = output / "intermediate_designs"
    candidates = sorted(search_root.rglob("*.pdb")) + sorted(search_root.rglob("*.cif"))
    errors = []
    for candidate in candidates:
        try:
            result = _structure(candidate)
            result["candidate_files_examined"] = len(candidates)
            return result
        except (IndexError, RuntimeError, SystemExit, ValueError) as exc:
            errors.append(f"{candidate}: {exc}")
    raise SystemExit(f"{model} produced no semantically valid structure; examined={len(candidates)} errors={errors[-3:]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("model", choices=sorted(REQUIRED))
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--artifact-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-image-digest")
    args = parser.parse_args()
    artifacts = _external_artifacts(args.model, args.artifact_root, args.artifact_receipt)
    semantic = _standard(args.model, args.output)
    print(json.dumps({
        "schema": "fs2.nebius.ai/scientific-model-h100-qualification/v1",
        "model": args.model,
        "status": "passed",
        "qualification": "external-artifact-model-domain-forward",
        "external_artifacts": artifacts,
        "semantic_output": semantic,
    }, sort_keys=True))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Independent semantic gate for a BoltzGen PD-L1 design-stage output.

The validator imports neither the runtime entrypoint nor the scientific adapter.
It re-parses the emitted mmCIF and NPZ bytes with independent libraries and
requires a physical 60..80-residue designed chain, an intact PD-L1 target, and
a real interface at the requested PD-1-contact face.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import gemmi
import numpy as np

BINDING_SITE = (54, 56, 58, 60, 61, 63, 66, 113, 115, 117, 121, 123, 124, 125)
STANDARD_AA = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
}
BACKBONE = ("N", "CA", "C")


class ValidationError(RuntimeError):
    """The output is not a physical, target-bound BoltzGen design."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def protein_residues(chain: gemmi.Chain) -> list[gemmi.Residue]:
    return [residue for residue in chain if residue.name in STANDARD_AA]


def sequence(residues: list[gemmi.Residue]) -> str:
    return "".join(
        gemmi.find_tabulated_residue(residue.name).one_letter_code for residue in residues
    )


def atom_position(residue: gemmi.Residue, name: str) -> tuple[float, float, float]:
    atom = residue.find_atom(name, "*")
    if atom is None:
        raise ValidationError(
            f"chain {residue.subchain or '?'} residue {residue.seqid} lacks {name}"
        )
    values = (atom.pos.x, atom.pos.y, atom.pos.z)
    if not all(math.isfinite(value) for value in values):
        raise ValidationError("output contains non-finite backbone coordinates")
    return values


def chain_report(chain: gemmi.Chain, *, minimum: int, maximum: int) -> dict[str, Any]:
    residues = protein_residues(chain)
    if not minimum <= len(residues) <= maximum:
        raise ValidationError(
            f"chain {chain.name} has {len(residues)} protein residues, expected {minimum}..{maximum}"
        )
    ca = [atom_position(residue, "CA") for residue in residues]
    for residue in residues:
        for name in BACKBONE:
            atom_position(residue, name)
    distances = [math.dist(ca[index - 1], ca[index]) for index in range(1, len(ca))]
    broken = [distance for distance in distances if not 2.8 <= distance <= 4.8]
    if len(broken) > max(1, len(distances) // 20):
        raise ValidationError(f"chain {chain.name} has {len(broken)} non-physical CA steps")
    extent = max(
        max(point[axis] for point in ca) - min(point[axis] for point in ca)
        for axis in range(3)
    )
    if extent < 10.0:
        raise ValidationError(f"chain {chain.name} is a degenerate point cloud")
    return {
        "chain": chain.name,
        "protein_residues": len(residues),
        "atoms": sum(len(residue) for residue in residues),
        "backbone_extent_angstrom": round(extent, 3),
        "ca_step_min_angstrom": round(min(distances), 3),
        "ca_step_max_angstrom": round(max(distances), 3),
        "sequence": sequence(residues),
    }


def metadata_report(path: Path, binder_length: int) -> dict[str, Any]:
    with np.load(path, allow_pickle=False) as payload:
        required = {"design_mask", "mol_type"}
        if not required.issubset(payload.files):
            raise ValidationError(f"{path.name} lacks {sorted(required - set(payload.files))}")
        design_mask = np.asarray(payload["design_mask"], dtype=bool)
        if int(design_mask.sum()) != binder_length:
            raise ValidationError(
                f"metadata identifies {int(design_mask.sum())} designed residues; structure has {binder_length}"
            )
        return {
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
            "designed_residues": int(design_mask.sum()),
            "tokens": int(design_mask.size),
        }


def validate(workspace: Path, target: Path) -> dict[str, Any]:
    output_root = workspace / "intermediate_designs"
    structures = sorted(
        path for path in output_root.glob("*.cif") if not path.name.endswith("_native.cif")
    )
    if len(structures) != 1:
        raise ValidationError(f"expected one generated mmCIF, found {len(structures)}")
    structure_path = structures[0]
    metadata_path = structure_path.with_suffix(".npz")
    if not metadata_path.is_file():
        raise ValidationError(f"missing output metadata {metadata_path.name}")
    structure = gemmi.read_structure(str(structure_path))
    if len(structure) != 1:
        raise ValidationError("generated mmCIF must contain exactly one model")
    model = structure[0]
    chain_names = [chain.name for chain in model]
    if len(chain_names) != 2 or len(set(chain_names)) != 2:
        raise ValidationError(f"expected exactly two uniquely named protein chains, found {chain_names}")

    target_structure = gemmi.read_structure(str(target))
    source_residues = protein_residues(target_structure[0]["A"])
    source_sequence = sequence(source_residues)
    target_candidates: list[tuple[gemmi.Chain, int]] = []
    for chain in model:
        output_sequence = sequence(protein_residues(chain))
        if len(output_sequence) < 100:
            continue
        offsets = [
            offset
            for offset in range(len(source_sequence) - len(output_sequence) + 1)
            if source_sequence[offset : offset + len(output_sequence)] == output_sequence
        ]
        if len(offsets) == 1:
            target_candidates.append((chain, offsets[0]))
    if len(target_candidates) != 1:
        lengths = {chain.name: len(protein_residues(chain)) for chain in model}
        raise ValidationError(
            f"could not identify one exact PD-L1 sequence slice; chains={lengths}"
        )
    target_chain, source_offset = target_candidates[0]
    binder_chain = next(chain for chain in model if chain.name != target_chain.name)
    target_report = chain_report(target_chain, minimum=100, maximum=160)
    binder_report = chain_report(binder_chain, minimum=60, maximum=80)

    output_target_residues = protein_residues(target_chain)
    if target_report["sequence"] != source_sequence[
        source_offset : source_offset + len(output_target_residues)
    ]:
        raise ValidationError("generated complex does not preserve an exact PD-L1 sequence slice")

    site_ca = []
    for sequence_id in BINDING_SITE:
        matching_source_indices = [
            index for index, residue in enumerate(source_residues) if residue.seqid.num == sequence_id
        ]
        if len(matching_source_indices) != 1:
            raise ValidationError(f"source PD-L1 binding residue A:{sequence_id} is absent or ambiguous")
        output_index = matching_source_indices[0] - source_offset
        if not 0 <= output_index < len(output_target_residues):
            raise ValidationError(f"PD-L1 binding residue A:{sequence_id} was trimmed from the output")
        site_ca.append(atom_position(output_target_residues[output_index], "CA"))
    binder_ca = [atom_position(residue, "CA") for residue in protein_residues(binder_chain)]
    distances = [math.dist(binder, site) for binder in binder_ca for site in site_ca]
    closest = min(distances)
    contacting_binder_residues = sum(
        min(math.dist(binder, site) for site in site_ca) <= 12.0 for binder in binder_ca
    )
    if not 2.0 <= closest <= 8.0 or contacting_binder_residues < 3:
        raise ValidationError(
            f"designed chain misses the requested PD-L1 face: closest={closest:.3f}, "
            f"contacting_residues={contacting_binder_residues}"
        )
    if sha256_file(structure_path) == sha256_file(target):
        raise ValidationError("generated structure is byte-identical to the target input")
    metadata = metadata_report(metadata_path, binder_report["protein_residues"])
    return {
        "schema": "fs2-serve.nebius.ai/boltzgen-h100-semantic-qualification/v1",
        "status": "passed",
        "validator": "independent-gemmi-numpy-pdl1-design-stage-v1",
        "structure": {
            "path": str(structure_path.relative_to(workspace)),
            "sha256": sha256_file(structure_path),
            "bytes": structure_path.stat().st_size,
            "chains": chain_names,
        },
        "metadata": metadata,
        "target": target_report,
        "binder": binder_report,
        "target_projection": {
            "source_chain": "A",
            "output_chain": target_chain.name,
            "leading_residues_trimmed": source_offset,
            "trailing_residues_trimmed": len(source_residues)
            - source_offset
            - len(output_target_residues),
        },
        "interface": {
            "binding_site_auth_residues": list(BINDING_SITE),
            "closest_ca_angstrom": round(closest, 3),
            "binder_residues_within_12_angstrom": contacting_binder_residues,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        report = validate(arguments.workspace, arguments.target)
    except (OSError, ValueError, ValidationError) as error:
        print(json.dumps({"status": "failed", "reason": str(error)}, sort_keys=True))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

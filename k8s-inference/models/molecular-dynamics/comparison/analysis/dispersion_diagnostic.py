#!/usr/bin/env python3
"""Source-formula tail diagnostics from unchanged native AMBER coefficients."""
import argparse
import json
from pathlib import Path

import numpy as np

from compare import file_receipt, source_identity, write_json
from geometry import ValidationError, finite


def tails(types, c6, c12, exclusions, volume_A3, cutoff_A=10.):
    types = np.asarray(types, dtype=int)
    counts = np.bincount(types, minlength=len(c6))
    c6, c12 = finite(c6, "C6"), finite(c12, "C12")
    if c6.shape != c12.shape or c6.shape != (len(counts), len(counts)) or volume_A3 <= 0 or cutoff_A <= 0:
        raise ValidationError("invalid coefficient matrix or geometry")
    if not np.array_equal(c6, c6.T) or not np.array_equal(c12, c12.T):
        raise ValidationError("asymmetric pair coefficients")
    populations = counts[:, None] * counts[None, :]
    bulk6, bulk12 = float(np.sum(populations * c6)), float(np.sum(populations * c12))
    distinct6 = (bulk6 - np.sum(counts * np.diag(c6))) / 2
    pairs = set()
    for i, j in exclusions:
        pair = tuple(sorted((int(i), int(j))))
        if i == j or pair in pairs or min(pair) < 0 or max(pair) >= len(types):
            raise ValidationError("invalid or duplicate excluded pair")
        pairs.add(pair)
        distinct6 -= c6[types[i], types[j]]
    remaining = len(types) * (len(types) - 1) / 2 - len(pairs)
    if remaining <= 0:
        raise ValidationError("no nonexcluded distinct pairs")
    pref6 = -2 * np.pi / (3 * cutoff_A**3 * volume_A3)
    pref12 = 2 * np.pi / (9 * cutoff_A**9 * volume_A3)
    return {"atoms": len(types), "exclusions": len(pairs), "volume_A3": float(volume_A3), "cutoff_A": cutoff_A,
            "units": "kcal/mol", "bulk_C6_only": pref6 * bulk6, "bulk_C12_contribution": pref12 * bulk12,
            "bulk_C6_plus_C12": pref6 * bulk6 + pref12 * bulk12,
            "distinct_nonexcluded_pairmean_C6_only": pref6 * len(types)**2 * distinct6 / remaining}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--static-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be new")
    import parmed as pmd
    files = [args.master / name for name in ("system.prmtop", "system.rst7", "master-manifest.json")]
    originals = [file_receipt(path) for path in files]
    manifest = json.loads(files[2].read_text())
    for entry in manifest["files"]:
        if file_receipt(args.master / entry["path"])["sha256"] != entry["sha256"]:
            raise ValidationError("master manifest hash mismatch")
    topology = pmd.load_file(str(files[0]), str(files[1]))
    data, ntypes = topology.parm_data, topology.ptr("ntypes")
    index = np.array(data["NONBONDED_PARM_INDEX"]).reshape(ntypes, ntypes) - 1
    if np.any(index < 0):
        raise ValidationError("unexpected non-LJ pair coefficient")
    c6 = np.array(data["LENNARD_JONES_BCOEF"])[index]
    c12 = np.array(data["LENNARD_JONES_ACOEF"])[index]
    if not np.array_equal(topology.box[3:], [90., 90., 90.]):
        raise ValidationError("diagnostic requires canonical orthogonal cell")
    offset, exclusions = 0, []
    for atom, count in enumerate(data["NUMBER_EXCLUDED_ATOMS"]):
        exclusions.extend((atom, other - 1) for other in data["EXCLUDED_ATOMS_LIST"][offset:offset + count] if other)
        offset += count
    if offset != len(data["EXCLUDED_ATOMS_LIST"]):
        raise ValidationError("excluded-list extent differs")
    prediction = tails(np.array(data["ATOM_TYPE_INDEX"]) - 1, c6, c12, exclusions, np.prod(topology.box[:3]))
    measured = json.loads(args.static_receipt.read_text())["tail_on_minus_off"]
    comparisons = {}
    for engine, convention in (("amber", "bulk_C6_only"), ("lammps", "bulk_C6_plus_C12"), ("gromacs", "distinct_nonexcluded_pairmean_C6_only")):
        actual = measured[engine]["vdw_including_14_and_tail"]
        comparisons[engine] = {"source_formula": convention, "predicted": prediction[convention], "native_on_minus_off": actual, "native_minus_formula": actual - prediction[convention]}
    if [file_receipt(path) for path in files] != originals:
        raise ValidationError("source changed during diagnostic")
    write_json(args.output, {"status": "source-formula-diagnostic-completed", "source": source_identity(), "inputs": originals + [file_receipt(args.static_receipt)], "prediction": prediction, "comparisons": comparisons,
                            "scope": "independent arithmetic from unchanged native coefficient matrices, not modified force field or native rerun; GROMACS upstream-source to exact NGC binary binding remains unproven; NAMD formula not independently traced"})


if __name__ == "__main__":
    main()

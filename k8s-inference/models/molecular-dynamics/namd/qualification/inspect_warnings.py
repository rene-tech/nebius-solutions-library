"""Inventory retained native warnings and fixture PSFs without changing protocols.

This is an offline review aid, not a warning suppressor, runtime policy, Tcl
feature detector, or automatic customer-release approval.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re


KNOWN = {
    'Warning: Option "1-4scaling" has been deprecated. Instead use "oneFourScaling".': "deprecated-alias",
    'Warning: Option "CUDASOAintegrate" has been deprecated. Instead use "GPUresident".': "deprecated-alias",
    'Warning: Option "CUDAForceTable" has been deprecated. Instead use "GPUForceTable".': "deprecated-alias",
    'Warning: Option "DeviceMigration" has been deprecated. Instead use "GPUAtomMigration".': "deprecated-alias",
    'Warning: Setting "GPUForceTable off" is considered experimental.': "explicit-experimental-optimization",
    'Warning: Setting "GPUAtomMigration on" is considered experimental.': "explicit-experimental-optimization",
    'Warning: GPUAtomMigration is experimental': "explicit-experimental-optimization",
    'Warning: The Langevin gamma parameters differ over the particles,': "heterogeneous-langevin-coupling",
    'Warning: requiring extra work per step to constrain rigid bonds.': "heterogeneous-langevin-coupling",
    'Warning: Disabling lonepair support due to incompatability with GPU-resident.': "resident-lonepair-capability",
}


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def psf_inventory(path):
    count, minimum, light, remaining, atoms = 0, math.inf, 0, 0, None
    sections = []
    with path.open() as stream:
        for line in stream:
            if re.search(r"!NATOM\b", line):
                if atoms is not None:
                    raise ValueError("duplicate PSF atom section")
                atoms = remaining = int(line.split()[0])
                continue
            if remaining:
                fields = line.split()
                mass = float(fields[7])
                if int(fields[0]) != count + 1 or not math.isfinite(mass) or mass < 0:
                    raise ValueError("invalid PSF atom or mass")
                minimum = min(minimum, mass)
                light += mass < 0.1
                count += 1
                remaining -= 1
            elif re.search(r"!NUMLP\b", line):
                sections.append(line.strip())
    if not atoms or remaining or count != atoms:
        raise ValueError("incomplete PSF atom section")
    return {"path": str(path), "sha256": sha(path), "atoms": atoms,
            "minimum_mass_amu": minimum, "atoms_below_0_1_amu": light,
            "explicit_lonepair_section_headers": sections}


def warning_lines(text):
    return [line.strip() for line in text.splitlines() if re.search(r"\bwarning\b", line, re.IGNORECASE)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, action="append", required=True)
    parser.add_argument("--psf", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    warnings, logs = Counter(), []
    for campaign in args.campaign:
        for path in sorted(campaign.glob("rep-*/data/**/*.log")):
            lines = warning_lines(path.read_text(errors="replace"))
            warnings.update(lines)
            logs.append({"path": str(path), "sha256": sha(path), "warning_lines": len(lines)})
    if not logs:
        raise ValueError("no native logs found")
    rows = [{"text": line, "occurrences": count, "review_class": KNOWN.get(line, "unclassified")}
            for line, count in sorted(warnings.items())]
    result = {"recorded_at": datetime.now(timezone.utc).isoformat(), "status": "inventoried",
              "customer_release_approved": False, "warnings_suppressed": False,
              "logs": logs, "warnings": rows, "psfs": [psf_inventory(path) for path in args.psf],
              "unclassified_warning_lines": [row["text"] for row in rows if row["review_class"] == "unclassified"],
              "limitations": ["Known warning text is classified, not automatically approved",
                              "PSF inspection covers only the listed files, not arbitrary Tcl or every possible feature",
                              "Experimental optimization warnings remain visible and require protocol-specific interpretation"]}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"receipt": str(args.output), "sha256": sha(args.output), "logs": len(logs),
                      "unique_warning_lines": len(rows), "unclassified": len(result["unclassified_warning_lines"])}))


if __name__ == "__main__":
    main()

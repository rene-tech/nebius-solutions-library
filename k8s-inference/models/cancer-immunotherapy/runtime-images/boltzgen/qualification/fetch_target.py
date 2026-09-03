#!/usr/bin/env python3
"""Fetch and deterministically project the exact public PD-L1 target.

The generated runtime Job has no network access.  This preparation helper runs
outside that Job, verifies the complete RCSB object, keeps only chain A's
polymer residues, and refuses to emit bytes other than the identity pinned in
``image-lock.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path
from typing import Any

import gemmi

HERE = Path(__file__).resolve().parent
LOCK_PATH = HERE.parent / "image-lock.json"


class TargetError(RuntimeError):
    """The source or projected target did not satisfy the immutable contract."""


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def project(source: bytes) -> bytes:
    document = gemmi.cif.read_string(source.decode("utf-8"))
    structure = gemmi.make_structure_from_block(document.sole_block())
    # make_structure_from_block retains atom-site fragments as separate Chain
    # objects; read_structure normally performs this normalization for us.
    structure.merge_chain_parts()
    if len(structure) != 1:
        raise TargetError(f"5J89 must contain exactly one model, found {len(structure)}")
    model = structure[0]
    for index in range(len(model) - 1, -1, -1):
        if model[index].name != "A":
            del model[index]
    if len(model) != 1 or model[0].name != "A":
        raise TargetError("5J89 chain A was not selected uniquely")
    chain = model[0]
    for index in range(len(chain) - 1, -1, -1):
        if chain[index].het_flag != "A":
            del chain[index]
    if len(chain) < 100:
        raise TargetError("projected PD-L1 chain is unexpectedly short")
    # BoltzGen resolves the mmCIF ``label_asym_id`` rather than Gemmi's chain
    # name (the latter is the mmCIF ``auth_asym_id``).  RCSB 5J89 calls the
    # same chain B/A in those two namespaces.  A projection that only retains
    # chain A but does not normalize its residue subchain therefore looks like
    # chain B to the runtime.  Make both identifiers A deterministically.
    for residue in chain:
        residue.subchain = "A"
    return structure.make_mmcif_document().as_string().encode("utf-8")


def fetch(lock: dict[str, Any], output: Path) -> dict[str, object]:
    contract = lock["input"]
    request = urllib.request.Request(
        contract["source_uri"], headers={"User-Agent": "fs2-boltzgen-qualification/1"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        source = response.read(contract["source_bytes"] + 1)
    if len(source) != contract["source_bytes"] or sha256(source) != contract["source_sha256"]:
        raise TargetError("RCSB source bytes do not match image-lock.json")
    projected = project(source)
    if (
        len(projected) != contract["projected_bytes"]
        or sha256(projected) != contract["projected_sha256"]
    ):
        raise TargetError("projected chain A bytes do not match image-lock.json")
    output.write_bytes(projected)
    structure = gemmi.read_structure(str(output))
    chain = structure[0]["A"]
    polymer = list(chain.get_polymer())
    atom_site = gemmi.cif.read_file(str(output)).sole_block().find(
        ["_atom_site.label_asym_id", "_atom_site.auth_asym_id"]
    )
    chain_identifiers = sorted({(row[0], row[1]) for row in atom_site})
    if chain_identifiers != [("A", "A")]:
        raise TargetError(f"projected mmCIF chain identifiers are {chain_identifiers!r}")
    return {
        "schema": "fs2-serve.nebius.ai/boltzgen-target-preparation/v1",
        "source_uri": contract["source_uri"],
        "source_sha256": contract["source_sha256"],
        "source_bytes": contract["source_bytes"],
        "projection": contract["projection"],
        "projected_path": output.name,
        "projected_sha256": sha256(projected),
        "projected_bytes": len(projected),
        "chain": "A",
        "mmcif_chain_identifiers": [list(value) for value in chain_identifiers],
        "polymer_residues": len(polymer),
        "gemmi_version": gemmi.__version__,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=LOCK_PATH)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        lock = json.loads(arguments.lock.read_text(encoding="utf-8"))
        receipt = fetch(lock, arguments.output)
    except (OSError, ValueError, TargetError) as error:
        print(json.dumps({"state": "failed", "reason": str(error)}, sort_keys=True))
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

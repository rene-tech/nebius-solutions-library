#!/usr/bin/env python3
"""Independently gate a Proteina-Complexa qualification run.

This validator deliberately does not import the runtime entrypoint.  The
entrypoint decides its own terminal state; this re-derives the verdict from the
artifacts alone -- the result envelope, the upstream log and the produced
structures -- so a bug that made the entrypoint too lenient cannot also make
the gate too lenient.

A variant passes only when all of the following hold:

* the upstream process exited zero and the envelope says PASS
* the upstream log shows Lightning actually using CUDA
* the exact pinned checkpoint pair for that variant, and no other variant's
  checkpoint, appears in the recorded argv
* LoRA re-application matches the variant (required for ligand and AME,
  forbidden for protein)
* at least one non-degenerate protein structure was produced, and for the two
  ligand-bearing variants the expected ligand residue is present
* a compute phase was measured
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LOCK = json.loads((HERE.parent / "image-lock.json").read_text(encoding="utf-8"))

EXPECTED = {
    "protein": {"lora": False, "ligand": False},
    "ligand": {"lora": True, "ligand": True},
    "ame": {"lora": True, "ligand": True},
}
CA_MIN_A, CA_MAX_A = 2.5, 4.6
MIN_CHAIN_FOR_DIVERSITY = 20
MIN_DISTINCT_RESIDUES = 5
STANDARD = frozenset(
    """ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP
    TYR VAL""".split()
)


def _files(catalogue: dict[str, Any], artifact_id: str) -> dict[str, dict[str, Any]]:
    entry = next(
        item for item in catalogue["external_artifacts"] if item["artifact_id"] == artifact_id
    )
    return {item["path"]: item for item in entry["files"]}


def _structures(root: Path) -> list[Path]:
    return sorted(root.rglob("*.pdb"))


def _summarise(path: Path) -> dict[str, Any]:
    residues: dict[tuple[str, str], str] = {}
    trace: dict[str, list[tuple[float, float, float]]] = {}
    names: set[str] = set()
    chain_standard: dict[str, int] = {}
    chain_kinds: dict[str, set[str]] = {}
    for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
        if not line.startswith(("ATOM", "HETATM")) or len(line) < 54:
            continue
        name = line[17:20].strip()
        chain = line[21:22]
        key = (chain, line[22:27])
        if key not in residues:
            chain_standard.setdefault(chain, 0)
            if name in STANDARD:
                chain_standard[chain] += 1
                chain_kinds.setdefault(chain, set()).add(name)
        residues[key] = name
        names.add(name)
        point = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
        if not all(math.isfinite(value) for value in point):
            raise SystemExit(f"non-finite coordinate in {path}")
        if line[12:16].strip() == "CA":
            trace.setdefault(chain, []).append(point)
    steps = {}
    for chain, points in trace.items():
        if len(points) > 1:
            distances = [math.dist(points[i], points[i + 1]) for i in range(len(points) - 1)]
            steps[chain] = sum(distances) / len(distances)
    return {
        "residues": len(residues),
        "standard": sum(1 for value in residues.values() if value in STANDARD),
        "chain_standard": chain_standard,
        "chain_distinct": {key: len(value) for key, value in chain_kinds.items()},
        "residue_names": names,
        "mean_ca_step": steps,
        "chains": sorted(trace),
    }


def validate(variant: str, root: Path) -> dict[str, Any]:
    failures: list[str] = []
    checks: dict[str, Any] = {"variant": variant, "output_root": str(root)}

    envelope_path = root / "result.json"
    if not envelope_path.is_file():
        return {**checks, "passed": False, "failures": [f"no result envelope at {envelope_path}"]}
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))

    if envelope.get("upstream_exit_code") != 0:
        failures.append(f"upstream exit code was {envelope.get('upstream_exit_code')}")
    if envelope.get("terminal_state") != "PASS":
        failures.append(f"envelope terminal state is {envelope.get('terminal_state')}")
    if envelope.get("variant") != variant:
        failures.append(f"envelope reports variant {envelope.get('variant')}")

    log_path = root / "upstream.log"
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    checks["cuda_marker_in_log"] = "GPU available: True (cuda), used: True" in log
    if not checks["cuda_marker_in_log"]:
        failures.append("the upstream log does not show Lightning using CUDA")

    # Exact checkpoint-pair selection, re-derived from the recorded argv.
    argv = " ".join(envelope.get("argv") or [])
    artifact_id = f"complexa-{variant}"
    pinned = _files(LOCK, artifact_id)
    for name in pinned:
        if name not in argv:
            failures.append(f"argv does not reference the pinned file {name}")
    checks["checkpoint_pair"] = sorted(pinned)
    for other in ("protein", "ligand", "ame"):
        if other == variant:
            continue
        for name in _files(LOCK, f"complexa-{other}"):
            if name in pinned:
                continue
            if f"++ckpt_name={name}" in argv or f"/{name}" in argv:
                failures.append(f"argv leaks the {other} checkpoint {name}")

    verification = envelope.get("artifact_verification") or {}
    markers = verification.get("markers") or []
    checks["markers_verified"] = len(markers)
    checks["content_digests_verified"] = verification.get("content_digests_verified")
    if len(markers) != 2:
        failures.append(f"expected two verified checkpoint markers, got {len(markers)}")
    for marker in markers:
        if marker.get("observed_bytes") != marker.get("expected_bytes"):
            failures.append(f"{marker.get('label')} byte count did not match")
        if verification.get("content_digests_verified") and not marker.get("digest_verified"):
            failures.append(f"{marker.get('label')} content digest was not verified")

    rf3 = verification.get("rosettafold3") or {}
    checks["rosettafold3_bound"] = rf3.get("bound")
    checks["rosettafold3_exercised"] = rf3.get("exercised")

    phases = envelope.get("phases") or {}
    checks["phases"] = {
        key: phases.get(key)
        for key in (
            "interpreter_and_import_seconds",
            "model_load_seconds",
            "sampling_seconds",
            "compute_seconds",
            "upstream_reported_generation_seconds",
            "upstream_process_seconds",
            "lora_reapplied",
        )
    }
    if not phases.get("compute_seconds"):
        failures.append("no compute phase was measured")
    expected_lora = EXPECTED[variant]["lora"]
    if bool(phases.get("lora_reapplied")) != expected_lora:
        failures.append(
            f"LoRA re-application was {phases.get('lora_reapplied')}, expected {expected_lora}"
        )

    structures = _structures(root)
    checks["structure_count"] = len(structures)
    if not structures:
        failures.append("no PDB structure was produced")
    protein_like = 0
    observed_ligands: set[str] = set()
    chain_lengths: dict[str, dict[str, int]] = {}
    chain_diversity: dict[str, dict[str, int]] = {}
    for path in structures:
        summary = _summarise(path)
        observed_ligands.update(summary["residue_names"] - STANDARD)
        chain_lengths[path.name] = summary["chain_standard"]
        for chain, count in summary["chain_standard"].items():
            if count >= MIN_CHAIN_FOR_DIVERSITY:
                distinct = summary["chain_distinct"].get(chain, 0)
                if distinct < MIN_DISTINCT_RESIDUES:
                    failures.append(
                        f"{path.name} chain {chain} has {count} residues but only "
                        f"{distinct} distinct amino-acid type(s)"
                    )
        chain_diversity[path.name] = summary["chain_distinct"]
        if summary["standard"] < 1:
            continue
        bad = {
            chain: round(step, 3)
            for chain, step in summary["mean_ca_step"].items()
            if not (CA_MIN_A <= step <= CA_MAX_A)
        }
        if bad:
            failures.append(f"{path.name} has non-protein C-alpha spacing {bad}")
        else:
            protein_like += 1
    checks["protein_like_structures"] = protein_like
    checks["chain_lengths"] = chain_lengths
    checks["chain_distinct_residues"] = chain_diversity
    if protein_like < 1:
        failures.append("no produced structure has a protein-like backbone")

    # The designed binder must actually be in the target's declared length
    # envelope, measured per chain: the binder pipelines write the supplied
    # target and the designed binder into one file.
    declared = (envelope.get("target") or {}).get("binder_length") or []
    if isinstance(declared, (int, float)):
        declared = [declared]
    declared = [int(value) for value in declared if value is not None]
    checks["binder_length_envelope"] = declared
    if len(declared) > 1 and chain_lengths:
        low, high = min(declared), max(declared)
        for name, chains in chain_lengths.items():
            if not any(low <= count <= high for count in chains.values()):
                failures.append(
                    f"{name} has no chain within the binder envelope {low}-{high}: {chains}"
                )

    if EXPECTED[variant]["ligand"]:
        expected_ligands = {
            name.upper() for name in (envelope.get("target") or {}).get("ligand_residues") or []
        }
        checks["expected_ligands"] = sorted(expected_ligands)
        checks["observed_non_standard_residues"] = sorted(observed_ligands)
        if not expected_ligands:
            failures.append("the target declared no ligand for a ligand-bearing variant")
        elif not expected_ligands & observed_ligands:
            failures.append(
                f"none of the expected ligand residues {sorted(expected_ligands)} "
                f"appear in the produced structures"
            )

    return {**checks, "passed": not failures, "failures": failures}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="directory holding the per-variant outputs")
    parser.add_argument("--variant", action="append", default=None)
    parser.add_argument("--output", default=None)
    arguments = parser.parse_args()

    root = Path(arguments.root)
    variants = arguments.variant or ["protein", "ligand", "ame"]
    reports = [validate(variant, root / variant) for variant in variants]
    verdict = {
        "schema": "fs2.nebius.ai/proteina-complexa-semantic-qualification/v1",
        "owner_task": LOCK["owner_task"],
        "model_id": LOCK["model_id"],
        "source_revision": LOCK["source"]["revision"],
        "image_digest": LOCK["image"]["published_digest"],
        "variants": reports,
        "all_passed": all(item["passed"] for item in reports),
    }
    payload = json.dumps(verdict, indent=2, default=sorted) + "\n"
    if arguments.output:
        Path(arguments.output).write_text(payload, encoding="utf-8")
    print(payload)
    for report in reports:
        state = "PASS" if report["passed"] else "FAIL"
        print(f"{report['variant']}: {state}", file=sys.stderr)
        for failure in report["failures"]:
            print(f"    - {failure}", file=sys.stderr)
    return 0 if verdict["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

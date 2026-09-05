#!/usr/bin/env python3
"""Execute and aggregate deterministic native BindCraft batch shards."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import math
import os
import re
import runpy
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable


BACKEND_ID = "bindcraft-v1-5-3-pyrosetta-academic"
SOURCE_REVISION = "7cd4ace1b7407adf66a50dfefa47de2270f5e4a9"
ACADEMIC_ASSET_ID = "pyrosetta-bindcraft"
ACADEMIC_ARTIFACT_SHA256 = "4383d8d1a14fd3aff52983de936908791cc77bc6ac418e3bc53bb963a42c5242"
PYROSETTA_EXPECTED_VERSION = "2026.29+releasequarterly.80a0635615"
PYROSETTA_TREE_MANIFEST_SHA256 = "a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d"
ARTIFACT_MANIFEST_SCHEMA = "fs2-serve.nebius.ai/scientific-artifact-manifest/v1"

# Every tree this runtime needs arrives from outside the image, so the roots are
# declared per run instead of compiled in: the shared filesystem that carries
# them is re-published under new paths as its handoff evolves, and an image that
# hard-coded one layout would have to be rebuilt for a move that changes no
# bytes.  What the image does own is the licensed tree's identity - see
# _admit_external_trees.
EXTERNAL_TREE_ADMISSION_SCHEMA = "fs2.nebius.ai/bindcraft-external-tree-admission/v1"
DEFAULT_EXTERNAL_TREE_ADMISSION = Path("/var/run/fs2/external-trees.json")
# The shared controller writes a runtime localization marker into every stage
# working directory and names it in the argv, so a stage always carries a record
# of the localization generation it was scheduled against. This wrapper reads it
# instead of ignoring it: recording the scheduler's chosen generation beside the
# tree identities the run actually verified is what lets the two be audited
# together, and disagreement between them is a rejection rather than a note.
RUNTIME_LOCALIZATION_MARKER_ENV = "FS2_RUNTIME_LOCALIZATION_MARKER"
LOCALIZATION_GENERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PYROSETTA_ROLE = "pyrosetta-site-packages"
AF2_PARAMS_ROLE = "alphafold2-params"
MPNN_VANILLA_ROLE = "colabdesign-mpnn-weights-vanilla"
MPNN_SOLUBLE_ROLE = "colabdesign-mpnn-weights-soluble"
NESTED_TREE_ROLES = {PYROSETTA_ROLE}
FLAT_TREE_ROLES = {AF2_PARAMS_ROLE, MPNN_VANILLA_ROLE, MPNN_SOLUBLE_ROLE}
REQUIRED_TREE_ROLES = NESTED_TREE_ROLES | FLAT_TREE_ROLES
# colabdesign.mpnn.model resolves its checkpoints through these two package
# submodules, so a declared root that is not the directory the model imports
# would verify bytes nobody reads.
MPNN_PACKAGE_BY_ROLE = {
    MPNN_VANILLA_ROLE: "colabdesign.mpnn.weights",
    MPNN_SOLUBLE_ROLE: "colabdesign.mpnn.weights_soluble",
}

# Upstream column names in final_design_stats.csv.  These are the averaged
# per-design statistics that settings_filters/default_filters.json thresholds,
# so reading exactly these names is what makes a filtered design's evidence real.
FINAL_STAT_COLUMNS = {
    "iptm": "Average_i_pTM",
    "mean_plddt": "Average_pLDDT",
    "interface_dg": "Average_dG",
    "shape_complementarity": "Average_ShapeComplementarity",
    "interface_residue_count": "Average_n_InterfaceResidues",
    "buried_interface_area": "Average_dSASA",
    "binder_energy_score": "Average_Binder_Energy_Score",
    "binder_rmsd": "Average_Binder_RMSD",
}
TARGET_CHAIN = "A"
BINDER_CHAIN = "B"
INTERFACE_RESIDUE = re.compile(rf"{BINDER_CHAIN}[1-9][0-9]*")
MAX_INTERFACE_RESIDUES = 10_000
# Upstream biopython_utils.hotspot_residues calls an interface contact at 4.0 A
# between any pair of atoms; the same criterion is used here so the hotspot
# geometry this runtime reports means what BindCraft means by contact.
HOTSPOT_CONTACT_ANGSTROM = 4.0
AA3 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}


class ContractError(RuntimeError):
    """The immutable wrapper contract was not satisfied."""


def _runtime_helper(name: str) -> Any:
    """Import a runtime helper that sits next to this file, by exact path.

    The batch wrapper reaches this file through runpy, so neither /opt/fs2 nor
    its bindcraft subdirectory is guaranteed to be on sys.path, and PYTHONPATH
    belongs to the caller and has to keep the licensed tree first.  Loading by
    path depends on neither.
    """

    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"fs2_{name}", path)
    if spec is None or spec.loader is None:
        raise ContractError(f"runtime helper {name!r} is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_object(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ContractError(f"{path} must contain one JSON object")
    return value


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode() + b"\n"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_file(path: str, expected: str, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink() or _sha256(candidate) != expected:
        raise ContractError(f"{label} differs from its immutable SHA-256")
    return candidate


def _localization_marker(value: str) -> dict[str, Any]:
    """Read the controller-written runtime localization marker named in the argv.

    The controller owns this file's schema, so only what it must be is required:
    an absolute path to a readable JSON object. A ``generation`` string, when the
    marker carries one, is cross-checked against the trees actually mounted.
    """

    path = Path(value)
    if not path.is_absolute():
        raise ContractError("runtime localization marker must be an absolute path")
    if not path.is_file() or path.is_symlink():
        raise ContractError("runtime localization marker is unavailable")
    raw = path.read_bytes()
    try:
        marker = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("runtime localization marker is unreadable") from exc
    if not isinstance(marker, dict):
        raise ContractError("runtime localization marker must contain one JSON object")
    configured = os.environ.get(RUNTIME_LOCALIZATION_MARKER_ENV, "")
    if configured and Path(configured) != path:
        raise ContractError("runtime localization marker argv and environment disagree")
    generation = marker.get("generation")
    if generation is not None and (
        not isinstance(generation, str) or LOCALIZATION_GENERATION.fullmatch(generation) is None
    ):
        raise ContractError("runtime localization marker generation is not a bounded token")
    return {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "generation": generation or "",
    }


def _admit_external_trees(marker: dict[str, Any]) -> dict[str, Any]:
    """Verify every mounted tree's full content before any model code runs.

    The declaration says where each tree is and which immutable identity it must
    have; this reads the bytes and refuses to continue on any mismatch.  Identity
    authority is split deliberately.  The licensed PyRosetta tree is what this
    image is licensed around, so its identity is pinned here and a declaration
    naming a different one is rejected outright.  The three public trees are
    published by the platform's artifact plane, which is their authority, so
    their identities are taken from the declaration and then proven against the
    bytes actually mounted.
    """

    tree_identity = _runtime_helper("tree_identity")
    configured = os.environ.get("FS2_BINDCRAFT_EXTERNAL_TREES")
    path = Path(configured) if configured else DEFAULT_EXTERNAL_TREE_ADMISSION
    if not path.is_file() or path.is_symlink():
        raise ContractError("external tree admission declaration is unavailable")
    declaration = _load_object(str(path))
    if declaration.get("schema") != EXTERNAL_TREE_ADMISSION_SCHEMA:
        raise ContractError("external tree admission schema is unsupported")
    generation = declaration.get("generation", "")
    if not isinstance(generation, str) or (
        generation and LOCALIZATION_GENERATION.fullmatch(generation) is None
    ):
        raise ContractError("external tree admission generation is not a bounded token")
    if generation and marker["generation"] and generation != marker["generation"]:
        raise ContractError(
            "mounted localization generation is not the generation this run was scheduled against"
        )
    declared = declaration.get("trees")
    if not isinstance(declared, list) or not declared:
        raise ContractError("external tree admission declares no trees")

    by_role: dict[str, dict[str, Any]] = {}
    for entry in declared:
        if not isinstance(entry, dict):
            raise ContractError("external tree admission entry is malformed")
        role = entry.get("role")
        if role not in REQUIRED_TREE_ROLES:
            raise ContractError(f"external tree admission declares unsupported role {role!r}")
        if role in by_role:
            raise ContractError(f"external tree admission declares role {role!r} twice")
        by_role[role] = entry
    missing = sorted(REQUIRED_TREE_ROLES - set(by_role))
    if missing:
        raise ContractError("external tree admission is missing roles: " + ", ".join(missing))

    receipts: dict[str, Any] = {}
    started = time.monotonic()
    for role in sorted(REQUIRED_TREE_ROLES):
        entry = by_role[role]
        artifact_id = entry.get("artifact_id")
        root = entry.get("root")
        expected = entry.get("sha256")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ContractError(f"{role}: external tree admission has no artifact ID")
        if not isinstance(root, str) or not root.startswith("/"):
            raise ContractError(f"{role}: external tree root must be an absolute path")
        if not isinstance(expected, str):
            raise ContractError(f"{role}: external tree admission has no identity digest")
        if role == PYROSETTA_ROLE and expected != PYROSETTA_TREE_MANIFEST_SHA256:
            raise ContractError(
                "declared PyRosetta tree identity is not the licensed tree this image is built for"
            )
        try:
            if role in NESTED_TREE_ROLES:
                receipt = tree_identity.verify_tree(
                    Path(root), artifact_id=artifact_id, expected_tree_manifest_sha256=expected,
                )
                receipt.pop("entries", None)
            else:
                receipt = tree_identity.verify_flat_tree(
                    Path(root), artifact_id=artifact_id, expected_inventory_sha256=expected,
                )
        except tree_identity.TreeIdentityError as exc:
            raise ContractError(f"{role}: {exc}") from None
        receipt["root"] = root
        receipts[role] = receipt

    for role, module_name in MPNN_PACKAGE_BY_ROLE.items():
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - any import failure is a rejection
            raise ContractError(f"{role}: {module_name} is not importable from the mounted tree") from None
        origin = Path(str(module.__file__)).resolve().parent
        if origin != Path(receipts[role]["root"]).resolve():
            raise ContractError(f"{role}: {module_name} does not resolve to the verified tree")
        receipts[role]["package"] = module_name

    return {
        "schema": EXTERNAL_TREE_ADMISSION_SCHEMA,
        "localization_generation": generation,
        "admission_seconds": round(time.monotonic() - started, 3),
        "verified_bytes": sum(int(receipt["total_bytes"]) for receipt in receipts.values()),
        "trees": receipts,
    }


def _bind_pyrosetta(tree_root: Path) -> dict[str, str]:
    if not tree_root.is_dir() or tree_root.is_symlink() or not os.access(tree_root, os.R_OK | os.X_OK):
        raise ContractError("tenant-private preinstalled PyRosetta tree is absent or unreadable")
    configured = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    if not configured or configured[0] != str(tree_root):
        raise ContractError("PYTHONPATH must begin with the canonical tenant-private PyRosetta tree")
    if not sys.path or sys.path[0] != str(tree_root):
        sys.path.insert(0, str(tree_root))
    importlib.invalidate_caches()
    pyrosetta = importlib.import_module("pyrosetta")
    origin = Path(str(pyrosetta.__file__)).resolve()
    if tree_root.resolve() not in origin.parents:
        raise ContractError("PyRosetta resolved outside the tenant-private mount")
    try:
        distribution = importlib.metadata.distribution("pyrosetta")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ContractError("PyRosetta installed dist-info is missing from the tenant-private mount") from exc
    dist_path = Path(str(getattr(distribution, "_path", ""))).resolve()
    if tree_root.resolve() not in dist_path.parents or not dist_path.name.endswith(".dist-info"):
        raise ContractError("PyRosetta dist-info resolved outside the tenant-private mount")
    if distribution.version != PYROSETTA_EXPECTED_VERSION:
        raise ContractError(
            f"PyRosetta metadata version {distribution.version!r} is not the expected {PYROSETTA_EXPECTED_VERSION!r}"
        )
    version_api_status = "unsupported"
    version_api_value = ""
    version_api = getattr(pyrosetta, "version", None)
    if callable(version_api):
        try:
            version_api_value = str(version_api())
            version_api_status = "supported"
        except Exception as exc:  # Some exact releases do not expose this API.
            version_api_status = f"unsupported:{type(exc).__name__}"
    return {
        "origin": str(origin),
        "version": distribution.version,
        "dist_info": dist_path.name,
        "tree_manifest_sha256": PYROSETTA_TREE_MANIFEST_SHA256,
        "version_api_status": version_api_status,
        "version_api_value": version_api_value,
    }


def _target_artifact(request: dict[str, Any], manifest: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    if request.get("input_manifest", {}).get("artifact_id") != manifest.get("manifest_id"):
        raise ContractError("request and input manifest identities differ")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or len(entries) != 1 or entries[0].get("name") != "target_structure":
        raise ContractError("native BindCraft requires exactly one target_structure")
    artifact = entries[0].get("artifact", {})
    configured = os.environ.get("FS2_BINDCRAFT_TARGET_PDB")
    candidates = [
        Path(configured) if configured else None,
        Path("/workspace/inputs/target_structure.pdb"),
        Path("/workspace/artifacts") / str(artifact.get("artifact_id", "")),
    ]
    path = next((item for item in candidates if item is not None and item.is_file()), None)
    if path is None or path.is_symlink():
        raise ContractError("materialized target_structure PDB was not found")
    if path.stat().st_size != artifact.get("size_bytes") or _sha256(path) != artifact.get("sha256"):
        raise ContractError("materialized target_structure differs from its content-addressed pointer")
    return path, artifact


def _pinned(advanced: dict[str, Any], key: str) -> Any:
    if key not in advanced:
        raise ContractError(f"pinned advanced settings template has no {key!r}")
    return advanced[key]


def _overridable(advanced: dict[str, Any], key: str, variable: str, cast: Callable[[str], Any]) -> Any:
    """Take the pinned template's value unless the caller overrides it.

    The template is admitted by SHA-256, which makes it the production
    definition of design depth and MPNN breadth.  Earlier revisions defaulted
    these to literals that were weaker than the template - one recycle instead
    of three, one sampled sequence instead of twenty - so a run that looked
    production-equivalent silently was not.
    """

    raw = os.environ.get(variable)
    return _pinned(advanced, key) if raw is None else cast(raw)


def _hotspot_specification(parameters: dict[str, Any]) -> str:
    return ",".join(str(item["residue"]) for item in parameters["hotspots"])


def _settings(
    request: dict[str, Any], target: Path, output: Path, template: Path, af2_params_dir: str,
) -> tuple[Path, Path, dict[str, Any]]:
    parameters = request["parameters"]
    target_settings = {
        "design_path": str(output / "upstream") + "/",
        "binder_name": f"fs2_s{parameters['_shard_index']:03d}",
        "starting_pdb": str(target),
        "chains": ",".join(parameters["target_chains"]),
        "target_hotspot_residues": _hotspot_specification(parameters),
        "lengths": [parameters["binder_length"]["minimum"], parameters["binder_length"]["maximum"]],
        "number_of_final_designs": parameters["accepted_designs_per_shard"],
    }
    advanced = json.loads(template.read_text(encoding="utf-8"))
    advanced.update({
        # Typed request bounds and artifact retention are the runtime's to own.
        "max_trajectories": parameters["max_trajectories_per_shard"],
        "save_mpnn_fasta": True,
        "save_design_animations": False,
        "save_design_trajectory_plots": False,
        "save_trajectory_pickle": False,
        "zip_animations": False,
        "zip_plots": False,
        "remove_unrelaxed_trajectory": False,
        "remove_unrelaxed_complex": False,
        "remove_binder_monomer": False,
        "enable_rejection_check": False,
        "optimise_beta": False,
        # Design depth and MPNN breadth come from the admitted template.
        "num_recycles_design": _overridable(
            advanced, "num_recycles_design", "FS2_BINDCRAFT_DESIGN_RECYCLES", int),
        "num_recycles_validation": _overridable(
            advanced, "num_recycles_validation", "FS2_BINDCRAFT_VALIDATION_RECYCLES", int),
        "num_seqs": _overridable(advanced, "num_seqs", "FS2_BINDCRAFT_MPNN_SEQUENCES", int),
        "max_mpnn_sequences": _overridable(
            advanced, "max_mpnn_sequences", "FS2_BINDCRAFT_MPNN_KEPT_SEQUENCES", int),
        "model_path": _overridable(advanced, "model_path", "FS2_BINDCRAFT_AF2_MODEL", str),
        "mpnn_weights": _overridable(advanced, "mpnn_weights", "FS2_BINDCRAFT_MPNN_WEIGHTS", str),
        # The template intentionally leaves this empty; it is a mount, not source.
        "af_params_dir": af2_params_dir,
    })
    settings_path = output / "target-settings.json"
    advanced_path = output / "advanced-settings.json"
    settings_path.write_text(json.dumps(target_settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    advanced_path.write_text(json.dumps(advanced, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return settings_path, advanced_path, advanced


def _run_upstream(settings: Path, filters: Path, advanced: Path, seed: int) -> None:
    import numpy as np

    original_randint = np.random.randint
    first = True

    def deterministic_first_randint(*args: Any, **kwargs: Any) -> Any:
        nonlocal first
        if first:
            first = False
            return np.array([seed], dtype=int)
        return original_randint(*args, **kwargs)

    np.random.randint = deterministic_first_randint
    old_argv = sys.argv
    try:
        sys.argv = [
            "bindcraft.py", "--settings", str(settings), "--filters", str(filters),
            "--advanced", str(advanced),
        ]
        runpy.run_path("/opt/bindcraft/bindcraft.py", run_name="__main__")
    finally:
        np.random.randint = original_randint
        sys.argv = old_argv


def _atoms(source: Path) -> tuple[list[dict[str, Any]], list[str]]:
    atoms: list[dict[str, Any]] = []
    binder_lines: list[str] = []
    for line in source.read_text(encoding="ascii").splitlines():
        if not line.startswith("ATOM"):
            continue
        chain = line[21:22].strip()
        try:
            coordinates = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
        except ValueError as exc:
            raise ContractError("accepted structure has an unreadable ATOM record") from exc
        atoms.append({
            "chain": chain,
            "residue_number": line[22:26].strip(),
            "insertion": line[26:27],
            "residue_name": line[17:20].strip(),
            "coordinates": coordinates,
        })
        if chain == BINDER_CHAIN:
            binder_lines.append(line)
    return atoms, binder_lines


def _binder_only(atoms: list[dict[str, Any]], binder_lines: list[str], destination: Path) -> str:
    residues: dict[tuple[str, str], str] = {}
    for atom in atoms:
        if atom["chain"] != BINDER_CHAIN:
            continue
        if atom["residue_name"] not in AA3:
            raise ContractError("accepted binder structure contains an unsupported residue")
        residues.setdefault((atom["residue_number"], atom["insertion"]), AA3[atom["residue_name"]])
    if not binder_lines:
        raise ContractError("accepted design has no binder chain B atoms")
    destination.write_text("\n".join(binder_lines + ["TER", "END"]) + "\n", encoding="ascii")
    return "".join(residues.values())


def _hotspot_geometry(atoms: list[dict[str, Any]], parameters: dict[str, Any]) -> dict[str, Any]:
    """Measure whether the accepted binder actually landed on the requested site.

    Upstream treats target_hotspot_residues as a loss preference, not a
    guarantee, and the statistics it writes describe the binder side of the
    interface only.  So the requested target residues are measured here against
    the accepted complex: every requested hotspot's closest approach to the
    binder chain is recorded, and at least one has to be a real contact.
    """

    binder = [atom["coordinates"] for atom in atoms if atom["chain"] == BINDER_CHAIN]
    if not binder:
        raise ContractError("accepted complex has no binder chain to measure against")
    contacts: list[dict[str, Any]] = []
    for hotspot in parameters["hotspots"]:
        chain = str(hotspot["chain"])
        residue = str(hotspot["residue"])
        target = [
            atom["coordinates"]
            for atom in atoms
            if atom["chain"] == chain and atom["residue_number"] == residue
        ]
        if not target:
            raise ContractError(
                f"requested hotspot {chain}{residue} is absent from the accepted complex"
            )
        closest = min(
            math.dist(target_atom, binder_atom)
            for target_atom in target
            for binder_atom in binder
        )
        contacts.append({
            "chain": chain,
            "residue": int(hotspot["residue"]),
            "closest_binder_atom_angstrom": round(closest, 3),
            "in_contact": closest <= HOTSPOT_CONTACT_ANGSTROM,
        })
    if not any(contact["in_contact"] for contact in contacts):
        raise ContractError(
            "no requested hotspot is in atomic contact with the accepted binder"
        )
    return {
        "contact_cutoff_angstrom": HOTSPOT_CONTACT_ANGSTROM,
        "requested": contacts,
        "contacted": sum(1 for contact in contacts if contact["in_contact"]),
    }


def _statistic(row: dict[str, Any], key: str) -> float:
    """Read one averaged upstream statistic, refusing to invent a missing one.

    An earlier revision read two columns that upstream does not emit and
    defaulted them to zero, so filtered designs reported no interface residues
    and no buried area while still being called validated.
    """

    column = FINAL_STAT_COLUMNS[key]
    if column not in row:
        raise ContractError(f"upstream design statistics have no {column!r} column")
    raw = row[column]
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ContractError(f"upstream statistic {column!r} is not a number") from exc
    if not math.isfinite(value):
        raise ContractError(f"upstream statistic {column!r} is not finite")
    return value


def _interface_residue_evidence(row: dict[str, Any], average_count: float) -> tuple[str, int | float]:
    """Normalize the model-specific residue list independently of its average.

    Pinned upstream writes ``InterfaceResidues`` from the last AF2 model it
    scores, while ``Average_n_InterfaceResidues`` is calculated across all
    available AF2 models.  Their cardinalities can legitimately differ.  The
    former must still be a canonical, unique list of binder residue IDs, and
    the latter remains the production-filtered aggregate statistic.
    """

    raw = row.get("InterfaceResidues")
    if not isinstance(raw, str) or not raw.strip():
        raise ContractError("accepted design records no interface residues")
    residues = tuple(part.strip() for part in raw.split(","))
    if (
        not residues
        or len(residues) > MAX_INTERFACE_RESIDUES
        or any(INTERFACE_RESIDUE.fullmatch(residue) is None for residue in residues)
    ):
        raise ContractError("accepted design interface residue list is malformed")
    if len(set(residues)) != len(residues):
        raise ContractError("accepted design interface residue list contains duplicates")
    if not 0 < average_count <= MAX_INTERFACE_RESIDUES:
        raise ContractError("accepted design has no bounded average interface residue count")
    normalized_average: int | float = int(average_count) if average_count.is_integer() else average_count
    return ",".join(residues), normalized_average


def _artifact(artifact_id: str, path: Path, media_type: str) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
        "media_type": media_type,
        "compression": "none",
    }


def _exact_ranked_rows(path: Path, quota: int) -> list[dict[str, str]]:
    """Return exactly the requested top-ranked upstream designs.

    BindCraft checks ``number_of_final_designs`` only between trajectories.  A
    single trajectory may therefore append as many accepted rows as
    ``max_mpnn_sequences`` and overshoot the remaining quota.  Its terminal
    check reranks ``final_design_stats.csv`` by ``Average_i_pTM`` before
    returning, so the leading rows are the deterministic winners to export.
    """

    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) < quota:
        raise ContractError(
            f"upstream BindCraft published {len(rows)} accepted designs for an exact quota of {quota}"
        )
    return rows[:quota]


def run_trajectory(args: argparse.Namespace) -> None:
    if args.backend_id != BACKEND_ID or not args.pyrosetta_required:
        raise ContractError("native academic backend identity and PyRosetta requirement are mandatory")
    request = _load_object(args.request)
    manifest = _load_object(args.input_manifest)
    template = _verify_file(args.settings_template, args.settings_sha256, "advanced settings")
    filters = _verify_file(args.filters, args.filters_sha256, "filter settings")
    marker = _localization_marker(args.runtime_localization_marker)
    output = Path(args.output)
    # The controller creates the stage working directory before it starts the
    # container, so an already-present output directory is normal; an already
    # -populated one is a re-entered attempt and must never be merged into.
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ContractError("shard output directory is not empty")
    request["parameters"]["_shard_index"] = args.shard_index
    target, _ = _target_artifact(request, manifest)
    external = _admit_external_trees(marker)
    bind_started = time.monotonic()
    pyrosetta = _bind_pyrosetta(Path(external["trees"][PYROSETTA_ROLE]["root"]))
    pyrosetta_bind_seconds = round(time.monotonic() - bind_started, 3)
    settings, advanced, resolved = _settings(
        request, target, output, template, external["trees"][AF2_PARAMS_ROLE]["root"],
    )
    upstream_started = time.monotonic()
    _run_upstream(settings, filters, advanced, args.seed)
    upstream_seconds = round(time.monotonic() - upstream_started, 3)

    post_started = time.monotonic()
    upstream = output / "upstream"
    trajectory_csv = upstream / "trajectory_stats.csv"
    final_csv = upstream / "final_design_stats.csv"
    if not final_csv.is_file():
        raise ContractError("upstream BindCraft did not create final design statistics")
    if not trajectory_csv.is_file():
        raise ContractError("upstream BindCraft did not create trajectory statistics")
    with trajectory_csv.open(encoding="utf-8", newline="") as handle:
        trajectory_rows = list(csv.DictReader(handle))
    if not trajectory_rows:
        raise ContractError("upstream BindCraft recorded no trajectory")
    rows = _exact_ranked_rows(
        final_csv,
        request["parameters"]["accepted_designs_per_shard"],
    )

    hotspot_specification = _hotspot_specification(request["parameters"])
    artifacts_dir = output / "artifacts"
    artifacts_dir.mkdir()
    entries: list[dict[str, Any]] = []
    index: dict[str, str] = {}
    for candidate_index, row in enumerate(rows):
        candidate_id = f"native-s{args.shard_index:03d}-c{candidate_index:03d}"
        design = str(row["Design"])
        source_matches = sorted((upstream / "Accepted").glob(f"{design}_model*.pdb"))
        if len(source_matches) != 1:
            raise ContractError("accepted design does not resolve to exactly one ranked AF2 model")
        source_pdb = source_matches[0]
        candidate_pdb = artifacts_dir / f"candidate-{candidate_index:03d}.pdb"
        relaxed_pdb = artifacts_dir / f"candidate-{candidate_index:03d}-relaxed-complex.pdb"
        shutil.copyfile(source_pdb, relaxed_pdb)
        atoms, binder_lines = _atoms(source_pdb)
        sequence = _binder_only(atoms, binder_lines, candidate_pdb)
        if sequence != row["Sequence"]:
            raise ContractError("accepted binder sequence differs from its binder-only PDB")
        if str(row.get("Target_Hotspot", "")).strip() != hotspot_specification:
            raise ContractError("accepted design was not designed against the requested hotspots")
        statistics = {key: _statistic(row, key) for key in FINAL_STAT_COLUMNS}
        interface_residues, interface_residue_count = _interface_residue_evidence(
            row, statistics["interface_residue_count"]
        )
        if statistics["buried_interface_area"] <= 0.0:
            raise ContractError("accepted design buries no interface area")
        if statistics["binder_energy_score"] == 0.0:
            raise ContractError("PyRosetta scored the accepted binder as exactly zero")
        metrics = {
            "candidate_id": candidate_id,
            "shard_index": args.shard_index,
            "seed": args.seed,
            "sequence": sequence,
            "scoring_engine": "pyrosetta",
            "filter_set_sha256": args.filters_sha256,
            "iptm": statistics["iptm"],
            "mean_plddt": statistics["mean_plddt"],
            "interface_dg": statistics["interface_dg"],
            "shape_complementarity": statistics["shape_complementarity"],
            "interface_residue_count": interface_residue_count,
            "binder_interface_residues": interface_residues,
            "buried_interface_area": statistics["buried_interface_area"],
            "binder_energy_score": statistics["binder_energy_score"],
            "binder_rmsd": statistics["binder_rmsd"],
            "target_hotspot_specification": hotspot_specification,
            "hotspot_geometry": _hotspot_geometry(atoms, request["parameters"]),
        }
        metrics_path = artifacts_dir / f"candidate-{candidate_index:03d}-metrics.json"
        metrics_path.write_text(json.dumps(metrics, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        metrics_id = f"artifact.bindcraft.native.s{args.shard_index:03d}.c{candidate_index:03d}.metrics"
        structure_id = f"artifact.bindcraft.native.s{args.shard_index:03d}.c{candidate_index:03d}.pdb"
        relaxed_id = f"artifact.bindcraft.native.s{args.shard_index:03d}.c{candidate_index:03d}.relaxed-complex"
        entries.extend([
            {"name": f"candidate-{candidate_index:03d}-metrics", "semantic_type": "bindcraft-native-design-metrics-json/v1", "artifact": _artifact(metrics_id, metrics_path, "application/json")},
            {"name": f"candidate-{candidate_index:03d}-structure", "semantic_type": "protein-structure-pdb/v1", "artifact": _artifact(structure_id, candidate_pdb, "chemical/x-pdb")},
            {"name": f"candidate-{candidate_index:03d}-relaxed-complex", "semantic_type": "bindcraft-native-relaxed-complex-pdb/v1", "artifact": _artifact(relaxed_id, relaxed_pdb, "chemical/x-pdb")},
        ])
        index[metrics_id] = str(metrics_path.relative_to(output))
        index[structure_id] = str(candidate_pdb.relative_to(output))
        index[relaxed_id] = str(relaxed_pdb.relative_to(output))

    shard = {
        "backend_id": BACKEND_ID,
        "source_revision": SOURCE_REVISION,
        "index": args.shard_index,
        "seed": args.seed,
        "status": "succeeded",
    }
    shard_path = artifacts_dir / "shard.json"
    shard_path.write_text(json.dumps(shard, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    shard_id = f"artifact.bindcraft.native.shard.{args.shard_index:03d}"
    (output / "shard-output.json").write_text(json.dumps({
        "schema": "fs2-serve.nebius.ai/bindcraft-native-shard-output/v1",
        "shard": {"name": f"shard-{args.shard_index:03d}", "semantic_type": "bindcraft-native-shard-result-json/v1", "artifact": _artifact(shard_id, shard_path, "application/json")},
        "candidates": entries,
        "artifact_paths": {shard_id: str(shard_path.relative_to(output)), **index},
        "pyrosetta": pyrosetta,
        "external_trees": external,
        "runtime_localization_marker": marker,
        "resolved_settings": {
            key: resolved[key]
            for key in (
                "num_recycles_design", "num_recycles_validation", "num_seqs",
                "max_mpnn_sequences", "max_trajectories", "model_path", "mpnn_weights",
                "af_params_dir",
            )
        },
        "trajectories_recorded": len(trajectory_rows),
        "timings": {
            "external_tree_admission_seconds": external["admission_seconds"],
            "pyrosetta_bind_seconds": pyrosetta_bind_seconds,
            "upstream_seconds": upstream_seconds,
            "post_processing_seconds": round(time.monotonic() - post_started, 3),
        },
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _verify_handoff(root: Path, shard_output: dict[str, Any]) -> None:
    """Hold a shard's published output to the digests that shard declared."""

    declared: dict[str, dict[str, Any]] = {}
    for entry in [shard_output["shard"], *shard_output["candidates"]]:
        artifact = entry.get("artifact")
        if not isinstance(artifact, dict) or not isinstance(artifact.get("artifact_id"), str):
            raise ContractError("shard output declares a malformed artifact")
        declared[artifact["artifact_id"]] = artifact
    relatives = shard_output.get("artifact_paths")
    if not isinstance(relatives, dict) or set(relatives) != set(declared):
        raise ContractError("shard output artifact paths and declarations disagree")
    for artifact_id, artifact in sorted(declared.items()):
        relative = relatives[artifact_id]
        if not isinstance(relative, str) or relative.startswith("/") or ".." in relative.split("/"):
            raise ContractError("shard output artifact path is unsafe")
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise ContractError(f"handed-off artifact {artifact_id!r} is missing")
        if path.stat().st_size != artifact.get("size_bytes") or _sha256(path) != artifact.get("sha256"):
            raise ContractError(
                f"handed-off artifact {artifact_id!r} does not match the digest its shard published"
            )


def aggregate(args: argparse.Namespace) -> None:
    if args.backend_id != BACKEND_ID or not args.atomic_rename:
        raise ContractError("aggregate requires the exact backend and atomic rename")
    marker = _localization_marker(args.runtime_localization_marker)
    request = _load_object(args.request)
    _load_object(args.input_manifest)
    shard_root = Path(args.shards)
    entries: list[dict[str, Any]] = []
    paths: dict[str, str] = {}
    candidate_index = 0
    for shard_index in range(args.expected_shards):
        root = shard_root / f"{shard_index:03d}"
        value = _load_object(str(root / "shard-output.json"))
        # Shards run in separate Pods, so this is the only place their trees can
        # be compared. Aggregating designs that came from different localization
        # generations would produce one result set with two provenances.
        shard_generation = value.get("external_trees", {}).get("localization_generation", "")
        if marker["generation"] and shard_generation and shard_generation != marker["generation"]:
            raise ContractError(
                f"shard {shard_index:03d} ran against a different localization generation"
            )
        entries.append(value["shard"])
        for entry in value["candidates"]:
            kind = entry["name"].rsplit("-", 1)[1]
            entries.append({**entry, "name": f"candidate-{candidate_index:03d}-{kind}"})
            if kind == "structure":
                candidate_index += 1
        for artifact_id, relative in value["artifact_paths"].items():
            paths[artifact_id] = str((root / relative).resolve())
        # The design stage runs in a different Pod, so its output crosses a
        # durable volume to get here. Re-read every artifact and hold it to the
        # digest the producing shard published, rather than trusting that a path
        # under a shared volume still holds the bytes that shard wrote.
        _verify_handoff(root, value)
    if candidate_index == 0:
        raise ContractError("aggregate has no accepted candidates")
    runtime_digest = os.environ.get("FS2_RUNTIME_IMAGE_DIGEST", "")
    if not runtime_digest.startswith("sha256:") or len(runtime_digest) != 71:
        raise ContractError("FS2_RUNTIME_IMAGE_DIGEST must identify the admitted immutable image")
    request_bytes = _canonical_bytes(request)
    aggregate_value = {
        "backend_id": BACKEND_ID,
        "source_revision": SOURCE_REVISION,
        "access_profile": "academic",
        "academic_asset_id": ACADEMIC_ASSET_ID,
        "academic_artifact_sha256": ACADEMIC_ARTIFACT_SHA256,
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "runtime_image_digest": runtime_digest,
        "runtime_localization_marker_sha256": marker["sha256"],
        "localization_generation": marker["generation"],
        "expected_shards": args.expected_shards,
        "succeeded_shards": args.expected_shards,
        "atomic_commit": True,
    }
    aggregate_path = shard_root.parent / "aggregate.json"
    aggregate_path.write_text(json.dumps(aggregate_value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    aggregate_id = "artifact.bindcraft.native.aggregate"
    entries.append({"name": "aggregate", "semantic_type": "bindcraft-native-aggregate-json/v1", "artifact": _artifact(aggregate_id, aggregate_path, "application/json")})
    paths[aggregate_id] = str(aggregate_path.resolve())
    staging = Path(args.staging_manifest)
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(json.dumps({
        "schema": ARTIFACT_MANIFEST_SCHEMA,
        "manifest_id": "manifest.bindcraft.native.output",
        "entries": entries,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    Path(str(staging) + ".artifact-paths.json").write_text(json.dumps(paths, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    destination = Path(args.output_manifest)
    staging.replace(destination)
    Path(str(staging) + ".artifact-paths.json").replace(Path(str(destination) + ".artifact-paths.json"))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="mode", required=True)
    run = commands.add_parser("run-trajectory")
    for flag in ("backend-id", "request", "input-manifest", "settings-template", "settings-sha256", "filters", "filters-sha256", "output"):
        run.add_argument("--" + flag, required=True)
    run.add_argument("--shard-index", type=int, required=True)
    run.add_argument("--seed", type=int, required=True)
    run.add_argument("--pyrosetta-required", action="store_true")
    combine = commands.add_parser("aggregate")
    for flag in ("backend-id", "request", "input-manifest", "shards", "staging-manifest", "output-manifest"):
        combine.add_argument("--" + flag, required=True)
    combine.add_argument("--expected-shards", type=int, required=True)
    combine.add_argument("--atomic-rename", action="store_true")
    # The shared controller rejects a runtime-artifact stage whose argv omits the
    # canonical marker path, so both subcommands take it and neither runs blind.
    for command in (run, combine):
        command.add_argument("--runtime-localization-marker", required=True)
    return root


def main() -> None:
    args = parser().parse_args()
    if args.mode == "run-trajectory":
        run_trajectory(args)
    else:
        aggregate(args)


if __name__ == "__main__":
    try:
        main()
    except (ContractError, KeyError, OSError, ValueError) as exc:
        print(json.dumps({"event": "bindcraft_batch_rejected", "reason": str(exc)}), file=sys.stderr)
        raise SystemExit(78) from None

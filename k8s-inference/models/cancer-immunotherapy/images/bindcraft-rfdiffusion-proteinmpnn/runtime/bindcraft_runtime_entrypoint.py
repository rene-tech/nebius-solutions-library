#!/usr/bin/env python3
"""Execute and aggregate deterministic native BindCraft batch shards."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import importlib.metadata
import json
import os
import runpy
import sys
from pathlib import Path
from typing import Any


BACKEND_ID = "bindcraft-v1-5-3-pyrosetta-academic"
SOURCE_REVISION = "7cd4ace1b7407adf66a50dfefa47de2270f5e4a9"
ACADEMIC_ASSET_ID = "pyrosetta-bindcraft"
ACADEMIC_ARTIFACT_SHA256 = "4383d8d1a14fd3aff52983de936908791cc77bc6ac418e3bc53bb963a42c5242"
PYROSETTA_EXPECTED_VERSION = "2026.29+releasequarterly.80a0635615"
PYROSETTA_TREE_MANIFEST_SHA256 = "a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d"
PYROSETTA_SITE_PACKAGES = Path("/opt/fs2/academic/pyrosetta-bindcraft/site-packages")
ARTIFACT_MANIFEST_SCHEMA = "fs2-serve.nebius.ai/scientific-artifact-manifest/v1"
AA3 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}


class ContractError(RuntimeError):
    """The immutable wrapper contract was not satisfied."""


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


def _bind_pyrosetta() -> dict[str, str]:
    target = PYROSETTA_SITE_PACKAGES
    if not target.is_dir() or target.is_symlink() or not os.access(target, os.R_OK | os.X_OK):
        raise ContractError("tenant-private preinstalled PyRosetta tree is absent or unreadable")
    configured = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    if not configured or configured[0] != str(target):
        raise ContractError("PYTHONPATH must begin with the canonical tenant-private PyRosetta tree")
    if not sys.path or sys.path[0] != str(target):
        sys.path.insert(0, str(target))
    importlib.invalidate_caches()
    pyrosetta = importlib.import_module("pyrosetta")
    origin = Path(str(pyrosetta.__file__)).resolve()
    if target.resolve() not in origin.parents:
        raise ContractError("PyRosetta resolved outside the tenant-private mount")
    try:
        distribution = importlib.metadata.distribution("pyrosetta")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ContractError("PyRosetta installed dist-info is missing from the tenant-private mount") from exc
    dist_path = Path(str(getattr(distribution, "_path", ""))).resolve()
    if target.resolve() not in dist_path.parents or not dist_path.name.endswith(".dist-info"):
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


def _settings(request: dict[str, Any], target: Path, output: Path, template: Path) -> tuple[Path, Path]:
    parameters = request["parameters"]
    target_settings = {
        "design_path": str(output / "upstream") + "/",
        "binder_name": f"fs2_s{parameters['_shard_index']:03d}",
        "starting_pdb": str(target),
        "chains": ",".join(parameters["target_chains"]),
        "target_hotspot_residues": ",".join(str(item["residue"]) for item in parameters["hotspots"]),
        "lengths": [parameters["binder_length"]["minimum"], parameters["binder_length"]["maximum"]],
        "number_of_final_designs": parameters["accepted_designs_per_shard"],
    }
    advanced = json.loads(template.read_text(encoding="utf-8"))
    advanced.update({
        "max_trajectories": parameters["max_trajectories_per_shard"],
        "num_seqs": parameters["accepted_designs_per_shard"],
        "max_mpnn_sequences": parameters["accepted_designs_per_shard"],
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
        "num_recycles_design": int(os.environ.get("FS2_BINDCRAFT_DESIGN_RECYCLES", "1")),
        "num_recycles_validation": int(os.environ.get("FS2_BINDCRAFT_VALIDATION_RECYCLES", "1")),
        "optimise_beta": False,
        "af_params_dir": os.environ.get("FS2_BINDCRAFT_AF2_PARAMS", "/models/alphafold2"),
    })
    settings_path = output / "target-settings.json"
    advanced_path = output / "advanced-settings.json"
    settings_path.write_text(json.dumps(target_settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    advanced_path.write_text(json.dumps(advanced, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return settings_path, advanced_path


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


def _binder_only(source: Path, destination: Path) -> str:
    residues: dict[tuple[str, str, str], str] = {}
    lines: list[str] = []
    for line in source.read_text(encoding="ascii").splitlines():
        if not line.startswith("ATOM") or line[21:22].strip() != "B":
            continue
        residue = line[17:20].strip()
        if residue not in AA3:
            raise ContractError("accepted binder structure contains an unsupported residue")
        residues.setdefault((line[21:22], line[22:26], line[26:27]), AA3[residue])
        lines.append(line)
    if not lines:
        raise ContractError("accepted design has no binder chain B atoms")
    destination.write_text("\n".join(lines + ["TER", "END"]) + "\n", encoding="ascii")
    return "".join(residues.values())


def _artifact(artifact_id: str, path: Path, media_type: str) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
        "media_type": media_type,
        "compression": "none",
    }


def run_trajectory(args: argparse.Namespace) -> None:
    if args.backend_id != BACKEND_ID or not args.pyrosetta_required:
        raise ContractError("native academic backend identity and PyRosetta requirement are mandatory")
    request = _load_object(args.request)
    manifest = _load_object(args.input_manifest)
    template = _verify_file(args.settings_template, args.settings_sha256, "advanced settings")
    filters = _verify_file(args.filters, args.filters_sha256, "filter settings")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    request["parameters"]["_shard_index"] = args.shard_index
    target, _ = _target_artifact(request, manifest)
    pyrosetta = _bind_pyrosetta()
    settings, advanced = _settings(request, target, output, template)
    _run_upstream(settings, filters, advanced, args.seed)

    upstream = output / "upstream"
    final_csv = upstream / "final_design_stats.csv"
    if not final_csv.is_file():
        raise ContractError("upstream BindCraft did not create final design statistics")
    with final_csv.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ContractError("bounded upstream trajectory produced no accepted design")

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
        sequence = _binder_only(source_pdb, candidate_pdb)
        if sequence != row["Sequence"]:
            raise ContractError("accepted binder sequence differs from its binder-only PDB")
        metrics = {
            "candidate_id": candidate_id,
            "shard_index": args.shard_index,
            "seed": args.seed,
            "sequence": sequence,
            "scoring_engine": "pyrosetta",
            "iptm": float(row["Average_i_pTM"]),
            "mean_plddt": float(row["Average_pLDDT"]),
            "interface_dg": float(row["Average_dG"]),
            "shape_complementarity": float(row["Average_ShapeComplementarity"]),
        }
        metrics_path = artifacts_dir / f"candidate-{candidate_index:03d}-metrics.json"
        metrics_path.write_text(json.dumps(metrics, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        metrics_id = f"artifact.bindcraft.native.s{args.shard_index:03d}.c{candidate_index:03d}.metrics"
        structure_id = f"artifact.bindcraft.native.s{args.shard_index:03d}.c{candidate_index:03d}.pdb"
        entries.extend([
            {"name": f"candidate-{candidate_index:03d}-metrics", "semantic_type": "bindcraft-native-design-metrics-json/v1", "artifact": _artifact(metrics_id, metrics_path, "application/json")},
            {"name": f"candidate-{candidate_index:03d}-structure", "semantic_type": "protein-structure-pdb/v1", "artifact": _artifact(structure_id, candidate_pdb, "chemical/x-pdb")},
        ])
        index[metrics_id] = str(metrics_path.relative_to(output))
        index[structure_id] = str(candidate_pdb.relative_to(output))

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
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def aggregate(args: argparse.Namespace) -> None:
    if args.backend_id != BACKEND_ID or not args.atomic_rename:
        raise ContractError("aggregate requires the exact backend and atomic rename")
    request = _load_object(args.request)
    _load_object(args.input_manifest)
    shard_root = Path(args.shards)
    entries: list[dict[str, Any]] = []
    paths: dict[str, str] = {}
    candidate_index = 0
    for shard_index in range(args.expected_shards):
        root = shard_root / f"{shard_index:03d}"
        value = _load_object(str(root / "shard-output.json"))
        entries.append(value["shard"])
        for entry in value["candidates"]:
            kind = entry["name"].rsplit("-", 1)[1]
            entries.append({**entry, "name": f"candidate-{candidate_index:03d}-{kind}"})
            if kind == "structure":
                candidate_index += 1
        for artifact_id, relative in value["artifact_paths"].items():
            paths[artifact_id] = str((root / relative).resolve())
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

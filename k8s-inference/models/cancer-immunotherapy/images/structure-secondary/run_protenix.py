#!/usr/bin/env python3
"""Fail-closed Protenix v2 CPU-prep and H100 inference boundaries."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess


CHECKPOINT = Path("/models/protenix-v2/checkpoint/protenix-v2.pt")
CHECKPOINT_MARKER = Path(f"{CHECKPOINT}.fs2.json")
CHECKPOINT_BYTES = 1_859_785_497
CHECKPOINT_SHA256 = "8f931f9774a396b67033d0e58628e1834f4a1448165e04254b40a780b0c0d599"
CHECKPOINT_REVISION = "TMF001/protenix-v2-weights@653edab28103133512575365130916e3fd23ecc3"
DATABASE_ROOT = Path("/databases/protenix")
REFERENCE_BUNDLE_ID = "protenix-v2-inference-data-2026-01-29"
REFERENCE_REVISION = "v2.0.0-inference-plus-mmcif-20260129"
REFERENCE_SOURCE_SHA256 = "27da2585d0ea1d820f4693099653aab1fdff7d4e18c21e6d90a0dc18f718dd89"
REFERENCE_REQUIRED_PATHS = (
    Path("common/components.cif"),
    Path("common/components.cif.rdkit_mol.pkl"),
    Path("common/clusters-by-entity-40.txt"),
    Path("common/obsolete_release_date.csv"),
)


def _file(path: str | Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_file():
        raise SystemExit(f"{label} must be an existing absolute file: {candidate}")
    return candidate


def _directory(path: str | Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_dir():
        raise SystemExit(f"{label} must be an existing absolute directory: {candidate}")
    return candidate


def _load_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{label} is not valid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{label} must contain a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_seeds(value: str) -> str:
    try:
        seeds = [int(item) for item in value.split(",")]
    except ValueError as exc:
        raise SystemExit("seeds must be comma-separated integers") from exc
    if (
        not 1 <= len(seeds) <= 16
        or len(set(seeds)) != len(seeds)
        or any(seed < 0 or seed > 2**31 - 1 for seed in seeds)
    ):
        raise SystemExit("seeds must contain 1..16 unique integers in [0, 2^31-1]")
    return ",".join(str(seed) for seed in seeds)


def _validate_installed_runtime() -> None:
    expected_prefix = "/opt/protenix-venv/"
    for module in ("protenix", "runner", "configs"):
        spec = importlib.util.find_spec(module)
        if spec is None or not (spec.origin or "").startswith(expected_prefix):
            raise SystemExit(f"{module} does not resolve from the installed runtime: {spec}")


def _validate_checkpoint() -> None:
    checkpoint = _file(CHECKPOINT, "canonical Protenix v2 checkpoint")
    marker = _load_object(
        _file(CHECKPOINT_MARKER, "checkpoint localization marker"),
        "checkpoint localization marker",
    )
    expected = {
        "schema": "fs2.nebius.ai/localized-artifact/v1",
        "artifact_id": "protenix-v2-checkpoint",
        "path": str(CHECKPOINT),
        "bytes": CHECKPOINT_BYTES,
        "sha256": CHECKPOINT_SHA256,
        "revision": CHECKPOINT_REVISION,
        "verified": True,
    }
    if marker != expected:
        raise SystemExit("checkpoint localization marker does not bind the canonical Protenix v2 object")
    # Localization/cache promotion hashes the 1.86 GB object once. Runtime
    # admission checks the fixed path and byte count but never rehashes it.
    if checkpoint.stat().st_size != CHECKPOINT_BYTES:
        raise SystemExit("canonical Protenix v2 checkpoint byte count changed after localization")


def _validate_reference_data(manifest_path: Path) -> str:
    _directory(DATABASE_ROOT, "Protenix reference-data mount")
    missing = [
        str(path)
        for path in REFERENCE_REQUIRED_PATHS
        if not (DATABASE_ROOT / path).is_file()
    ]
    if missing:
        raise SystemExit(f"Protenix reference-data bundle is incomplete: {', '.join(missing)}")
    manifest = _load_object(
        _file(manifest_path, "reference-data manifest"), "reference-data manifest"
    )
    if (
        manifest.get("bundle_id") != REFERENCE_BUNDLE_ID
        or manifest.get("revision") != REFERENCE_REVISION
    ):
        raise SystemExit("reference-data manifest does not identify the exact Protenix v2 bundle")
    upstream = manifest.get("upstream")
    if (
        not isinstance(upstream, dict)
        or upstream.get("source_sha256") != REFERENCE_SOURCE_SHA256
    ):
        raise SystemExit("reference-data manifest does not bind the reviewed Protenix downloader")
    manifest_sha256 = _canonical_json_sha256(manifest)
    ready = _file(
        DATABASE_ROOT / ".fs2-manifest-sha256", "reference-data ready marker"
    )
    if ready.read_text(encoding="utf-8").strip() != manifest_sha256:
        raise SystemExit("reference-data ready marker does not match the exact manifest")
    return manifest_sha256


def _validate_preprocessed_input(input_path: Path, marker_path: Path) -> None:
    marker = _load_object(
        _file(marker_path, "preprocessing marker"), "preprocessing marker"
    )
    expected_keys = {
        "schema",
        "processed_json",
        "processed_json_sha256",
        "reference_bundle_id",
        "reference_revision",
        "reference_manifest_sha256",
        "msa_mode",
    }
    if set(marker) != expected_keys:
        raise SystemExit("preprocessing marker has an unexpected shape")
    if (
        marker.get("schema")
        != "fs2.nebius.ai/protenix-preprocess-result/v1"
        or marker.get("processed_json") != str(input_path)
        or marker.get("processed_json_sha256") != _sha256(input_path)
        or marker.get("reference_bundle_id") != REFERENCE_BUNDLE_ID
        or marker.get("reference_revision") != REFERENCE_REVISION
        or marker.get("msa_mode") not in {"none", "precomputed"}
        or not isinstance(marker.get("reference_manifest_sha256"), str)
        or len(str(marker["reference_manifest_sha256"])) != 64
    ):
        raise SystemExit("preprocessing marker does not bind the enriched Protenix input")


def build_prep_command(input_path: Path, output_dir: Path) -> list[str]:
    return [
        "/opt/protenix-venv/bin/protenix",
        "prep",
        "--input",
        str(input_path),
        "--out_dir",
        str(output_dir),
        "--msa_server_mode",
        "protenix",
    ]


def build_pred_command(
    input_path: Path,
    output_dir: Path,
    *,
    seeds: str,
    cycle: int,
    step: int,
    sample: int,
    msa_mode: str,
) -> list[str]:
    use_precomputed = str(msa_mode == "precomputed").lower()
    return [
        "/opt/protenix-venv/bin/protenix",
        "pred",
        "--input",
        str(input_path),
        "--out_dir",
        str(output_dir),
        "--seeds",
        seeds,
        "--cycle",
        str(cycle),
        "--step",
        str(step),
        "--sample",
        str(sample),
        "--model_name",
        "protenix-v2",
        "--use_default_params",
        "true",
        "--use_msa",
        use_precomputed,
        "--use_template",
        use_precomputed,
        "--use_rna_msa",
        use_precomputed,
    ]


def _prep(args: argparse.Namespace) -> None:
    input_path = _file(args.input, "input")
    output_dir = Path(args.output_dir)
    processed_json = Path(args.processed_json)
    if not output_dir.is_absolute() or not processed_json.is_absolute():
        raise SystemExit("output-dir and processed-json must be absolute")
    if processed_json.parent != output_dir and output_dir not in processed_json.parents:
        raise SystemExit("processed-json must be located below output-dir")
    output_dir.mkdir(parents=True, exist_ok=True)
    processed_json.parent.mkdir(parents=True, exist_ok=True)
    manifest_sha256 = _validate_reference_data(Path(args.reference_manifest))
    command = build_prep_command(input_path, output_dir)
    if args.print_command:
        print(
            json.dumps(
                {"argv": command, "network_policy": "offline-private-reference-data"},
                sort_keys=True,
            )
        )
        return
    environment = dict(os.environ)
    environment.update(
        {
            "PROTENIX_ROOT_DIR": str(DATABASE_ROOT),
            "FS2_MSA_MODE": args.msa_mode,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    completed = subprocess.run(command, check=False, env=environment)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    candidates = sorted(
        path
        for path in output_dir.rglob("*.json")
        if not path.name.endswith(".fs2.json") and path != processed_json
    )
    preferred = [
        path
        for path in candidates
        if path.name.endswith("-final-updated.json")
        or path.name.endswith("-update-msa.json")
    ]
    if len(preferred) == 1:
        source_json = preferred[0]
    elif len(candidates) == 1:
        source_json = candidates[0]
    elif not candidates and args.msa_mode in {"none", "precomputed"}:
        source_json = input_path
    else:
        raise SystemExit(
            f"Protenix prep produced an ambiguous JSON handoff: {[str(path) for path in candidates]}"
        )
    if source_json != processed_json:
        shutil.copyfile(source_json, processed_json)
    _file(processed_json, "processed-json produced by Protenix prep")
    marker = {
        "schema": "fs2.nebius.ai/protenix-preprocess-result/v1",
        "processed_json": str(processed_json),
        "processed_json_sha256": _sha256(processed_json),
        "reference_bundle_id": REFERENCE_BUNDLE_ID,
        "reference_revision": REFERENCE_REVISION,
        "reference_manifest_sha256": manifest_sha256,
        "msa_mode": args.msa_mode,
    }
    Path(f"{processed_json}.fs2.json").write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _pred(args: argparse.Namespace) -> None:
    input_path = _file(args.input, "enriched input")
    marker_path = Path(args.input_marker or f"{input_path}.fs2.json")
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        raise SystemExit("output-dir must be absolute")
    output_dir.mkdir(parents=True, exist_ok=True)
    _validate_preprocessed_input(input_path, marker_path)
    marker = _load_object(marker_path, "preprocessing marker")
    if marker.get("msa_mode") != args.msa_mode:
        raise SystemExit("requested msa-mode does not match the immutable preprocessing handoff")
    _validate_checkpoint()
    _validate_installed_runtime()
    command = build_pred_command(
        input_path,
        output_dir,
        seeds=_canonical_seeds(args.seeds),
        cycle=args.cycle,
        step=args.step,
        sample=args.sample,
        msa_mode=args.msa_mode,
    )
    if args.print_command:
        print(
            json.dumps(
                {"argv": command, "checkpoint_sha256": CHECKPOINT_SHA256},
                sort_keys=True,
            )
        )
        return
    import torch

    if not torch.cuda.is_available() or tuple(torch.cuda.get_device_capability(0)) != (
        9,
        0,
    ):
        raise SystemExit(
            "the qualified Protenix v2 semantic boundary requires an H100 (SM90)"
        )
    environment = dict(os.environ)
    environment.update(
        {
            "PROTENIX_ROOT_DIR": "/models/protenix-v2",
            "FS2_MSA_MODE": args.msa_mode,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    os.execve(command[0], command, environment)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    prep = subparsers.add_parser("prep")
    prep.add_argument("--input", required=True)
    prep.add_argument("--output-dir", required=True)
    prep.add_argument("--processed-json", required=True)
    prep.add_argument("--msa-mode", choices=("none", "precomputed"), required=True)
    prep.add_argument(
        "--reference-manifest", default="/databases/protenix/manifest.json"
    )
    prep.add_argument("--print-command", action="store_true")
    prep.set_defaults(handler=_prep)

    pred = subparsers.add_parser("pred")
    pred.add_argument("--input", required=True)
    pred.add_argument("--input-marker")
    pred.add_argument("--output-dir", required=True)
    pred.add_argument("--msa-mode", choices=("none", "precomputed"), required=True)
    pred.add_argument("--seeds", default="101")
    pred.add_argument("--cycle", type=int, default=10)
    pred.add_argument("--step", type=int, default=200)
    pred.add_argument("--sample", type=int, default=5)
    pred.add_argument("--print-command", action="store_true")
    pred.set_defaults(handler=_pred)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()

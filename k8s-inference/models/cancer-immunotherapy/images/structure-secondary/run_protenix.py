#!/usr/bin/env python3
"""Fail-closed Protenix v2 CPU-prep and H100 inference boundaries."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from result_contract import load_bounded_metrics, write_confidence_envelope


PROTENIX_ROOT = Path("/models/protenix-v2")
ARTIFACT_ID = "protenix-v2"
ARTIFACT_REVISION = (
    "code-2475421477ab414b571149ad4a875c390ff8a35d_"
    "checkpoint-653edab28103133512575365130916e3fd23ecc3_"
    "common-2026-01-29"
)
ARTIFACT_MANIFEST = PROTENIX_ROOT / "manifest.json"
ARTIFACT_READY = PROTENIX_ROOT / ".fs2-manifest-sha256"
CHECKPOINT = PROTENIX_ROOT / "checkpoint/protenix-v2.pt"
CHECKPOINT_BYTES = 1_859_785_497
CHECKPOINT_SHA256 = "8f931f9774a396b67033d0e58628e1834f4a1448165e04254b40a780b0c0d599"
CHECKPOINT_MD5 = "49016ebf4775bf6b629bc4dc77b6673e"
CHECKPOINT_PARAMETER_COUNT = 464_442_431
CHECKPOINT_REVISION = "TMF001/protenix-v2-weights@653edab28103133512575365130916e3fd23ecc3"
CODE_REVISION = "2475421477ab414b571149ad4a875c390ff8a35d"
COMMON_DATA_REVISION = "protenix-v2-inference-data-2026-01-29"
COMMON_DATA_SOURCE_SHA256 = "27da2585d0ea1d820f4693099653aab1fdff7d4e18c21e6d90a0dc18f718dd89"
COMMON_REQUIRED_PATHS = (
    Path("common/components.cif"),
    Path("common/components.cif.rdkit_mol.pkl"),
    Path("common/clusters-by-entity-40.txt"),
    Path("common/obsolete_release_date.csv"),
)
ARTIFACT_REQUIRED_PATHS = (Path("checkpoint/protenix-v2.pt"), *COMMON_REQUIRED_PATHS)
PROTENIX_CLI = "/opt/protenix-venv/bin/protenix"
TRITON_CACHE = Path("/cache/protenix/triton")
CUEQ_TRITON_CACHE = Path("/cache/protenix/cueq-triton")


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


def _validate_artifact() -> str:
    """Validate one localized composite artifact without rehashing 1.86 GB/run."""
    _directory(PROTENIX_ROOT, "canonical Protenix v2 artifact root")
    manifest = _load_object(
        _file(ARTIFACT_MANIFEST, "Protenix v2 composite manifest"),
        "Protenix v2 composite manifest",
    )
    if set(manifest) != {"schema", "artifact_id", "revision", "sources", "files"}:
        raise SystemExit("Protenix v2 composite manifest has an unexpected shape")
    expected_sources = {
        "code": {"revision": CODE_REVISION},
        "checkpoint": {
            "revision": CHECKPOINT_REVISION,
            "bytes": CHECKPOINT_BYTES,
            "sha256": CHECKPOINT_SHA256,
            "md5": CHECKPOINT_MD5,
            "parameter_count": CHECKPOINT_PARAMETER_COUNT,
            "verification": "third-party-mirror-verified-not-publisher-byte-compared",
        },
        "common": {
            "revision": COMMON_DATA_REVISION,
            "source_sha256": COMMON_DATA_SOURCE_SHA256,
        },
    }
    if (
        manifest.get("schema") != "fs2.nebius.ai/protenix-v2-composite-artifact/v1"
        or manifest.get("artifact_id") != ARTIFACT_ID
        or manifest.get("revision") != ARTIFACT_REVISION
        or manifest.get("sources") != expected_sources
    ):
        raise SystemExit("composite manifest does not identify the exact Protenix v2 artifact")

    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise SystemExit("Protenix v2 composite manifest files must be a list")
    files: dict[str, dict[str, object]] = {}
    for entry in raw_files:
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}:
            raise SystemExit("Protenix v2 composite manifest has an invalid file entry")
        relative = entry.get("path")
        byte_count = entry.get("bytes")
        sha256 = entry.get("sha256")
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or byte_count < 1
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
            or relative in files
        ):
            raise SystemExit("Protenix v2 composite manifest has an invalid file identity")
        files[relative] = entry
    expected_paths = {path.as_posix() for path in ARTIFACT_REQUIRED_PATHS}
    if set(files) != expected_paths:
        raise SystemExit("Protenix v2 composite manifest does not bind the complete artifact tree")
    checkpoint_entry = files[ARTIFACT_REQUIRED_PATHS[0].as_posix()]
    if (
        checkpoint_entry["bytes"] != CHECKPOINT_BYTES
        or checkpoint_entry["sha256"] != CHECKPOINT_SHA256
    ):
        raise SystemExit("composite manifest does not bind the exact Protenix v2 checkpoint")
    for relative, entry in files.items():
        localized = _file(PROTENIX_ROOT / relative, f"localized Protenix v2 file {relative}")
        if localized.stat().st_size != entry["bytes"]:
            raise SystemExit(f"localized Protenix v2 file size changed after promotion: {relative}")

    # The localizer hashes every file and writes this marker only after an
    # atomic immutable promotion. Runtime admission checks that one manifest
    # digest and cheap file sizes; it never rehashes the 1.86 GB checkpoint.
    manifest_sha256 = _canonical_json_sha256(manifest)
    ready = _file(ARTIFACT_READY, "Protenix v2 composite ready marker")
    if ready.read_text(encoding="utf-8").strip() != manifest_sha256:
        raise SystemExit("Protenix v2 ready marker does not match the composite manifest")
    return manifest_sha256


def _validate_preprocessed_input(
    input_path: Path, marker_path: Path, artifact_manifest_sha256: str
) -> dict[str, object]:
    marker = _load_object(
        _file(marker_path, "preprocessing marker"), "preprocessing marker"
    )
    expected_keys = {
        "schema",
        "processed_json_sha256",
        "artifact_id",
        "artifact_manifest_sha256",
        "msa_mode",
    }
    if set(marker) != expected_keys:
        raise SystemExit("preprocessing marker has an unexpected shape")
    if (
        marker.get("schema")
        != "fs2.nebius.ai/protenix-preprocess-result/v1"
        or marker.get("processed_json_sha256") != _sha256(input_path)
        or marker.get("artifact_id") != ARTIFACT_ID
        or marker.get("artifact_manifest_sha256") != artifact_manifest_sha256
        or marker.get("msa_mode") not in {"none", "precomputed"}
    ):
        raise SystemExit("preprocessing marker does not bind the enriched Protenix input")
    return marker


def build_prep_command(input_path: Path, output_dir: Path) -> list[str]:
    return [
        PROTENIX_CLI,
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
        PROTENIX_CLI,
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
        "false",
        "--use_rna_msa",
        "false",
    ]


def _write_confidence(
    output_dir: Path, *, seeds: list[int], samples_per_seed: int
) -> dict[str, object]:
    candidates = sorted(
        output_dir.rglob("*_seed_*_summary_confidence_sample_*.json"),
        key=lambda path: path.relative_to(output_dir).as_posix(),
    )
    if not candidates:
        raise SystemExit("Protenix produced no summary confidence artifacts")
    results: list[dict[str, object]] = []
    pattern = re.compile(
        r"^(?P<prefix>.+)_seed_(?P<seed>[0-9]+)_summary_confidence_"
        r"sample_(?P<sample>[0-9]+)\.json$"
    )
    for path in candidates:
        matched = pattern.fullmatch(path.name)
        if matched is None:
            raise SystemExit(f"Protenix confidence filename is not canonical: {path}")
        seed = int(matched.group("seed"))
        sample_index = int(matched.group("sample"))
        structure = path.with_name(
            f"{matched.group('prefix')}_seed_{seed}_sample_{sample_index}.cif"
        )
        metrics = load_bounded_metrics(
            path,
            {
                "plddt": (0.0, 100.0),
                "ptm": (0.0, 1.0),
                "iptm": (0.0, 1.0),
                "ranking_score": (-100.0, 2.0),
            },
            required={"plddt", "ptm", "iptm", "ranking_score"},
        )
        results.append(
            {
                "seed": seed,
                "sample_index": sample_index,
                "structure": structure,
                "summary": path,
                "metrics": metrics,
            }
        )
    confidence = write_confidence_envelope(
        output_dir,
        runtime_id="protenix-v2",
        model_revision=CHECKPOINT_REVISION,
        seeds=seeds,
        samples_per_seed=samples_per_seed,
        results=results,
    )
    return confidence


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
    manifest_sha256 = _validate_artifact()
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
            "PROTENIX_ROOT_DIR": str(PROTENIX_ROOT),
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
        "processed_json_sha256": _sha256(processed_json),
        "artifact_id": ARTIFACT_ID,
        "artifact_manifest_sha256": manifest_sha256,
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
    manifest_sha256 = _validate_artifact()
    marker = _validate_preprocessed_input(input_path, marker_path, manifest_sha256)
    if marker.get("msa_mode") != args.msa_mode:
        raise SystemExit("requested msa-mode does not match the immutable preprocessing handoff")
    _validate_installed_runtime()
    canonical_seeds = _canonical_seeds(args.seeds)
    if not 1 <= args.sample <= 16:
        raise SystemExit("sample must be in [1, 16]")
    command = build_pred_command(
        input_path,
        output_dir,
        seeds=canonical_seeds,
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
    for cache in (TRITON_CACHE, CUEQ_TRITON_CACHE):
        if not cache.is_dir() or not os.access(cache, os.W_OK):
            raise SystemExit(f"required writable Protenix Triton cache is unavailable: {cache}")
    environment = dict(os.environ)
    environment.update(
        {
            "PROTENIX_ROOT_DIR": "/models/protenix-v2",
            "FS2_MSA_MODE": args.msa_mode,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "TRITON_CACHE_DIR": str(TRITON_CACHE),
            "CUEQ_TRITON_CACHE_DIR": str(CUEQ_TRITON_CACHE),
        }
    )
    completed = subprocess.run(command, check=False, env=environment)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    confidence = _write_confidence(
        output_dir,
        seeds=[int(seed) for seed in canonical_seeds.split(",")],
        samples_per_seed=args.sample,
    )
    print(json.dumps(confidence, sort_keys=True, separators=(",", ":")))


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    prep = subparsers.add_parser("prep")
    prep.add_argument("--input", required=True)
    prep.add_argument("--output-dir", required=True)
    prep.add_argument("--processed-json", required=True)
    prep.add_argument("--msa-mode", choices=("none", "precomputed"), required=True)
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

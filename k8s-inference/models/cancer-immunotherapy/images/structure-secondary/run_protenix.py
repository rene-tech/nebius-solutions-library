#!/usr/bin/env python3
"""Fail-closed Protenix v2 relocatable CPU-prep and H100 prediction boundary."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from handoff_contract import sha256_file, write_archive
from result_contract import load_bounded_metrics, write_confidence_envelope
from runtime_localization import (
    RuntimeArtifactExpectation,
    validate_runtime_localization,
)


PROTENIX_ROOT = Path("/models/protenix-v2")
ARTIFACT_ID = "protenix-v2"
ARTIFACT_REVISION = (
    "code-2475421477ab414b571149ad4a875c390ff8a35d_"
    "checkpoint-653edab28103133512575365130916e3fd23ecc3_"
    "common-2026-01-29"
)
ARTIFACT_MANIFEST = PROTENIX_ROOT / "manifest.json"
ARTIFACT_READY = PROTENIX_ROOT / ".fs2-manifest-sha256"
LOCALIZED_CONTENT_DIGEST_SHA256 = "5e1c3b548af40752bb15f9f2ba06590e20e2b165e3fe9ab3fa99af9977574d48"
LOCALIZATION_MANIFEST_SHA256 = "a093d28ecfc8374f143cc32ff713b0e6ad1124c095dbbca5af6e51b4f7dcc6b7"
CHECKPOINT = PROTENIX_ROOT / "checkpoint/protenix-v2.pt"
COMMON_DIR = PROTENIX_ROOT / "common"
CHECKPOINT_BYTES = 1_859_785_497
CHECKPOINT_SHA256 = "8f931f9774a396b67033d0e58628e1834f4a1448165e04254b40a780b0c0d599"
CHECKPOINT_MD5 = "49016ebf4775bf6b629bc4dc77b6673e"
CHECKPOINT_PARAMETER_COUNT = 464_442_431
CHECKPOINT_REVISION = "TMF001/protenix-v2-weights@653edab28103133512575365130916e3fd23ecc3"
CODE_REVISION = "2475421477ab414b571149ad4a875c390ff8a35d"
COMMON_DATA_REVISION = "tos-common-2026-01-29"
COMMON_DATA_ARCHIVE_URL = "https://protenix.tos-cn-beijing.volces.com/common.tar.gz"
COMMON_DATA_ARCHIVE_BYTES = 475_085_654
COMMON_DATA_ARCHIVE_SHA256 = "08ea594f429df35494c062e3dfcacaf48fa761e4ea4a8bcb6d5107d211e64dbd"
COMMON_REQUIRED_PATHS = (
    Path("common/components.cif"),
    Path("common/components.cif.rdkit_mol.pkl"),
    Path("common/clusters-by-entity-40.txt"),
    Path("common/obsolete_release_date.csv"),
)
COMMON_FILE_IDENTITIES = {
    "common/clusters-by-entity-40.txt": (
        21_699_572,
        "1ab4af905e75b382eda8dec59917dc3608bee0729e36b9e71baf860bbe86850c",
    ),
    "common/components.cif": (
        490_777_362,
        "bb31ae5cf6c8bc669924313077cb4231ee5ffefd3a20118cd14f3ec89f8bb6a5",
    ),
    "common/components.cif.rdkit_mol.pkl": (
        142_498_117,
        "d1cfb71f5993a3ebea7c47877022d7f597bbfbaf86e28a4770e957da6c50cd35",
    ),
    "common/obsolete_release_date.csv": (
        134_716,
        "a4f3f63ac5d7eebd78b07995cc669b9eccd6f5d8813c9492c9df02868893cf33",
    ),
}
ARTIFACT_REQUIRED_PATHS = (Path("checkpoint/protenix-v2.pt"), *COMMON_REQUIRED_PATHS)
PROTENIX_CLI = "/opt/protenix-venv/bin/protenix"
PROTENIX_HANDOFF_SCHEMA = "fs2.nebius.ai/protenix-v2-prepared-handoff/v1"
VARIANT_ID = "upstream-v2-0-0"


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


def _output(path: str | Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise SystemExit(f"{label} must be an absolute path: {candidate}")
    return candidate


def _load_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{label} is not valid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{label} must contain a JSON object: {path}")
    return value


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8") + b"\n"
    return hashlib.sha256(encoded).hexdigest()


def _logical_id(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value) is None:
        raise SystemExit("stage artifact ID must be a valid bounded logical ID")
    return value


def _validate_runtime_localization_args(
    command: str, args: argparse.Namespace
) -> dict[str, object]:
    stages = {"prep": "prepare-data", "pred": "sample-structure"}
    if command not in stages:
        raise SystemExit(f"unsupported Protenix runtime artifact stage: {command}")
    return validate_runtime_localization(
        args.runtime_localization_marker,
        model_id="protenix-v2",
        variant_id=VARIANT_ID,
        stage_id=stages[command],
        artifacts=(
            RuntimeArtifactExpectation(
                ARTIFACT_ID,
                str(PROTENIX_ROOT),
                LOCALIZED_CONTENT_DIGEST_SHA256,
                expected_manifest_sha256=LOCALIZATION_MANIFEST_SHA256,
            ),
        ),
    )


def _parse_seeds(value: str) -> list[int]:
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
    return seeds


def _load_marker(path: Path) -> dict[str, object]:
    marker = _load_object(path, "Protenix prep provenance marker")
    return marker


def _validate_installed_runtime() -> None:
    if importlib.metadata.version("protenix") != "2.0.0":
        raise SystemExit("installed Protenix package must be exactly 2.0.0")
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
            "archive_url": COMMON_DATA_ARCHIVE_URL,
            "archive_bytes": COMMON_DATA_ARCHIVE_BYTES,
            "archive_sha256": COMMON_DATA_ARCHIVE_SHA256,
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
    checkpoint_entry = files["checkpoint/protenix-v2.pt"]
    if (
        checkpoint_entry["bytes"] != CHECKPOINT_BYTES
        or checkpoint_entry["sha256"] != CHECKPOINT_SHA256
    ):
        raise SystemExit("composite manifest does not bind the exact Protenix v2 checkpoint")
    for relative, (byte_count, digest) in COMMON_FILE_IDENTITIES.items():
        if files.get(relative) != {
            "path": relative,
            "bytes": byte_count,
            "sha256": digest,
        }:
            raise SystemExit(f"composite manifest does not bind exact common file: {relative}")
    for relative, entry in files.items():
        localized = _file(PROTENIX_ROOT / relative, f"localized Protenix v2 file {relative}")
        if localized.stat().st_size != entry["bytes"]:
            raise SystemExit(f"localized Protenix v2 file size changed after promotion: {relative}")
    manifest_sha256 = _canonical_json_sha256(manifest)
    if manifest_sha256 != LOCALIZATION_MANIFEST_SHA256:
        raise SystemExit("Protenix v2 composite manifest digest is not the qualified localization identity")
    ready = _file(ARTIFACT_READY, "Protenix v2 composite ready marker")
    if ready.read_text(encoding="utf-8").strip() != LOCALIZATION_MANIFEST_SHA256:
        raise SystemExit("Protenix v2 ready marker does not match the composite manifest")
    return LOCALIZATION_MANIFEST_SHA256


def _require_canonical_artifact_args(args: argparse.Namespace) -> None:
    if hasattr(args, "reference_root") and Path(args.reference_root) != PROTENIX_ROOT:
        raise SystemExit("reference-root must be the canonical /models/protenix-v2 tree")
    if hasattr(args, "reference_manifest") and Path(args.reference_manifest) != ARTIFACT_MANIFEST:
        raise SystemExit("reference-manifest must be the canonical composite manifest")
    if hasattr(args, "checkpoint") and Path(args.checkpoint) != CHECKPOINT:
        raise SystemExit("checkpoint must be /models/protenix-v2/checkpoint/protenix-v2.pt")
    if hasattr(args, "common_dir") and Path(args.common_dir) != COMMON_DIR:
        raise SystemExit("common-dir must be /models/protenix-v2/common")


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
    seeds: list[int],
    sample_count: int,
) -> list[str]:
    return [
        PROTENIX_CLI,
        "pred",
        "--input",
        str(input_path),
        "--out_dir",
        str(output_dir),
        "--seeds",
        ",".join(str(seed) for seed in seeds),
        "--cycle",
        "10",
        "--step",
        "200",
        "--sample",
        str(sample_count),
        "--model_name",
        "protenix-v2",
        "--use_default_params",
        "true",
        "--use_msa",
        "false",
        "--use_template",
        "false",
        "--use_rna_msa",
        "false",
    ]


def _write_confidence(
    output_dir: Path, *, seeds: list[int], samples_per_seed: int
) -> dict[str, object]:
    candidates = sorted(
        output_dir.rglob("*_summary_confidence_sample_*.json"),
        key=lambda path: path.relative_to(output_dir).as_posix(),
    )
    results: list[dict[str, object]] = []
    seed_pattern = re.compile(r"^seed_(?P<seed>[0-9]+)$")
    file_pattern = re.compile(
        r"^(?P<prefix>.+)_summary_confidence_sample_(?P<sample>[0-9]+)\.json$"
    )
    for summary in candidates:
        matched = file_pattern.fullmatch(summary.name)
        seed_parent = summary.parent.parent
        seed_match = seed_pattern.fullmatch(seed_parent.name)
        if summary.parent.name != "predictions" or matched is None or seed_match is None:
            raise SystemExit(f"Protenix confidence path is not canonical: {summary}")
        seed = int(seed_match.group("seed"))
        sample_index = int(matched.group("sample"))
        structure = summary.with_name(
            f"{matched.group('prefix')}_sample_{sample_index}.cif"
        )
        metrics = load_bounded_metrics(
            summary,
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
                "summary": summary,
                "metrics": metrics,
            }
        )
    return write_confidence_envelope(
        output_dir,
        runtime_id="protenix-v2",
        model_revision=CHECKPOINT_REVISION,
        seeds=seeds,
        samples_per_seed=samples_per_seed,
        results=results,
    )


def _prep(args: argparse.Namespace) -> None:
    _validate_runtime_localization_args("prep", args)
    _require_canonical_artifact_args(args)
    artifact_manifest_sha256 = _validate_artifact()
    _logical_id(args.output_artifact_id)
    input_path = _file(args.input, "input")
    output_dir = _output(args.output_dir, "output-dir")
    processed_json = _output(args.processed_json, "processed-json")
    provenance_marker = _output(args.provenance_marker, "provenance-marker")
    handoff_tar = _output(args.handoff_tar, "handoff-tar")
    output_dir.mkdir(parents=True, exist_ok=True)
    command = build_prep_command(input_path, output_dir)
    environment = dict(os.environ)
    environment.update(
        {
            "PROTENIX_ROOT_DIR": str(PROTENIX_ROOT),
            "FS2_MSA_MODE": "none",
            "FS2_NETWORK_MODE": "offline",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    completed = subprocess.run(command, check=False, env=environment)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    candidates = sorted(
        path for path in output_dir.rglob("*.json") if path != processed_json
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
    elif not candidates:
        source_json = input_path
    else:
        raise SystemExit(
            f"Protenix prep produced an ambiguous JSON handoff: {[str(path) for path in candidates]}"
        )
    processed_json.parent.mkdir(parents=True, exist_ok=True)
    if source_json.resolve() != processed_json.resolve():
        shutil.copyfile(source_json, processed_json)
    marker = {
        "schema": PROTENIX_HANDOFF_SCHEMA,
        "artifact_id": args.output_artifact_id,
        "member": "processed.json",
        "sha256": sha256_file(processed_json),
        "raw_input_sha256": sha256_file(input_path),
        "msa_mode": args.msa_mode,
        "composite_artifact_id": ARTIFACT_ID,
        "composite_artifact_revision": ARTIFACT_REVISION,
        "localized_content_digest_sha256": LOCALIZED_CONTENT_DIGEST_SHA256,
        "composite_manifest_sha256": artifact_manifest_sha256,
        "source_revision": CODE_REVISION,
    }
    provenance_marker.parent.mkdir(parents=True, exist_ok=True)
    provenance_marker.write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    write_archive(
        handoff_tar,
        {"processed.json": processed_json, "provenance.json": provenance_marker},
    )
    print(json.dumps(marker, sort_keys=True, separators=(",", ":")))


def _pred(args: argparse.Namespace) -> None:
    _validate_runtime_localization_args("pred", args)
    _require_canonical_artifact_args(args)
    artifact_manifest_sha256 = _validate_artifact()
    input_path = _file(args.input, "enriched input")
    input_marker = _file(args.input_marker, "input-marker")
    _logical_id(args.input_artifact_id)
    marker = _load_marker(input_marker)
    expected_marker = {
        "schema": PROTENIX_HANDOFF_SCHEMA,
        "artifact_id": args.input_artifact_id,
        "member": "processed.json",
        "sha256": sha256_file(input_path),
        "raw_input_sha256": marker.get("raw_input_sha256"),
        "msa_mode": args.msa_mode,
        "composite_artifact_id": ARTIFACT_ID,
        "composite_artifact_revision": ARTIFACT_REVISION,
        "localized_content_digest_sha256": LOCALIZED_CONTENT_DIGEST_SHA256,
        "composite_manifest_sha256": artifact_manifest_sha256,
        "source_revision": CODE_REVISION,
    }
    if (
        not isinstance(marker.get("raw_input_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", str(marker.get("raw_input_sha256"))) is None
        or marker != expected_marker
    ):
        raise SystemExit(
            "input-marker does not bind the Protenix input, msa_mode, and composite artifact manifest"
        )
    output_dir = _output(args.output_dir, "output-dir")
    output_dir.mkdir(parents=True, exist_ok=True)
    seeds = _parse_seeds(args.seeds)
    if not 1 <= args.sample_count <= 16:
        raise SystemExit("sample-count must be in [1, 16]")
    _validate_installed_runtime()
    command = build_pred_command(
        input_path,
        output_dir,
        seeds=seeds,
        sample_count=args.sample_count,
    )
    import torch

    if not torch.cuda.is_available() or tuple(torch.cuda.get_device_capability(0)) != (9, 0):
        raise SystemExit("the qualified Protenix v2 semantic boundary requires an H100 (SM90)")
    cache_paths = {
        "TRITON_CACHE_DIR": Path(os.environ.get("TRITON_CACHE_DIR", "")),
        "CUEQ_TRITON_CACHE_DIR": Path(os.environ.get("CUEQ_TRITON_CACHE_DIR", "")),
    }
    for name, cache in cache_paths.items():
        if not cache.is_absolute():
            raise SystemExit(f"{name} must be an absolute persistent cache path")
        cache.mkdir(parents=True, exist_ok=True)
        if not os.access(cache, os.W_OK):
            raise SystemExit(f"required writable Protenix Triton cache is unavailable: {cache}")
    environment = dict(os.environ)
    environment.update(
        {
            "PROTENIX_ROOT_DIR": str(PROTENIX_ROOT),
            "FS2_MSA_MODE": "none",
            "FS2_NETWORK_MODE": "offline",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    completed = subprocess.run(command, check=False, env=environment)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    confidence = _write_confidence(
        output_dir, seeds=seeds, samples_per_seed=args.sample_count
    )
    print(json.dumps(confidence, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    prep = subparsers.add_parser("prep")
    prep.add_argument("--input", required=True)
    prep.add_argument("--output-dir", required=True)
    prep.add_argument("--processed-json", required=True)
    prep.add_argument("--provenance-marker", required=True)
    prep.add_argument("--handoff-tar", required=True)
    prep.add_argument("--output-artifact-id", required=True)
    prep.add_argument("--msa-mode", choices=("none",), required=True)
    prep.add_argument("--reference-root", required=True)
    prep.add_argument("--reference-manifest", required=True)
    prep.add_argument("--runtime-localization-marker", required=True)
    prep.set_defaults(handler=_prep)

    pred = subparsers.add_parser("pred")
    pred.add_argument("--input", required=True)
    pred.add_argument("--input-marker", required=True)
    pred.add_argument("--input-artifact-id", required=True)
    pred.add_argument("--output-dir", required=True)
    pred.add_argument("--checkpoint", required=True)
    pred.add_argument("--common-dir", required=True)
    pred.add_argument("--msa-mode", choices=("none",), required=True)
    pred.add_argument("--seeds", required=True)
    pred.add_argument("--sample-count", type=int, required=True)
    pred.add_argument("--disable-templates", action="store_true", required=True)
    pred.add_argument("--disable-rna-msa", action="store_true", required=True)
    pred.add_argument("--runtime-localization-marker", required=True)
    pred.set_defaults(handler=_pred)

    args = parser.parse_args(argv)
    args.handler(args)


if __name__ == "__main__":
    main(sys.argv[1:])

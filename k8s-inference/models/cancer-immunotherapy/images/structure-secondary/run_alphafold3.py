#!/usr/bin/env python3
"""Official AlphaFold3 relocatable CPU-data and H100-inference boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from handoff_contract import validate_handoff, write_handoff
from result_contract import load_bounded_metrics, write_confidence_envelope


UPSTREAM = [
    "/opt/alphafold3-venv/bin/python",
    "/opt/alphafold3/run_alphafold.py",
]
PARAMETER_BYTES = 1_020_545_840
CANONICAL_DATABASE_DIR = Path("/databases")
CANONICAL_MODEL_DIR = Path("/models")
DATABASE_ARTIFACT_ID = "alphafold3-public-databases-v3.0"
DATABASE_MANIFEST = CANONICAL_DATABASE_DIR / "manifest.json"
DATABASE_READY = CANONICAL_DATABASE_DIR / ".fs2-manifest-sha256"


def _absolute(path: str | Path, label: str, *, directory: bool) -> Path:
    candidate = Path(path)
    exists = candidate.is_dir() if directory else candidate.is_file()
    if not candidate.is_absolute() or not exists:
        kind = "directory" if directory else "file"
        raise SystemExit(f"{label} must be an existing absolute {kind}: {candidate}")
    return candidate


def _output_path(path: str | Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise SystemExit(f"{label} must be an absolute path: {candidate}")
    return candidate


def _load_document(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"AlphaFold3 input is not valid JSON: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise SystemExit("AlphaFold3 wrapper accepts exactly one JSON job object")
    return document


def _canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _validate_database_contract(args: argparse.Namespace) -> None:
    if (
        Path(args.db_dir) != CANONICAL_DATABASE_DIR
        or Path(args.db_manifest) != DATABASE_MANIFEST
        or Path(args.db_ready_marker) != DATABASE_READY
        or args.reference_artifact_id != DATABASE_ARTIFACT_ID
    ):
        raise SystemExit("AlphaFold3 data stage requires the canonical /databases artifact")
    manifest = _load_document(_absolute(DATABASE_MANIFEST, "db-manifest", directory=False))
    ready = _absolute(DATABASE_READY, "db-ready-marker", directory=False)
    manifest_sha256 = _canonical_json_sha256(manifest)
    if (
        manifest.get("bundle_id") != DATABASE_ARTIFACT_ID
        or ready.read_text(encoding="utf-8").strip() != manifest_sha256
    ):
        raise SystemExit("AlphaFold3 reference manifest/ready marker identity does not match")


def _parse_seeds(value: str) -> list[int]:
    try:
        seeds = [int(item) for item in value.split(",")]
    except ValueError as exc:
        raise SystemExit("model-seeds must be comma-separated integers") from exc
    if (
        not 1 <= len(seeds) <= 16
        or len(set(seeds)) != len(seeds)
        or any(seed < 0 or seed > 2**31 - 1 for seed in seeds)
    ):
        raise SystemExit("model-seeds must contain 1..16 unique integers in [0, 2^31-1]")
    return seeds


def _bind_raw_input(raw: Path, staged: Path, seeds: list[int]) -> None:
    document = _load_document(raw)
    document["modelSeeds"] = seeds
    staged.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _assert_processed_seeds(processed: Path, seeds: list[int]) -> None:
    if _load_document(processed).get("modelSeeds") != seeds:
        raise SystemExit("processed AlphaFold3 JSON does not bind the requested exact seeds")


def build_command(
    *,
    stage: str,
    json_path: Path,
    output_dir: Path,
    model_dir: Path | None,
    num_diffusion_samples: int,
) -> list[str]:
    command = [
        *UPSTREAM,
        f"--json_path={json_path}",
        f"--output_dir={output_dir}",
        "--force_output_dir",
        "--run_data_pipeline" if stage == "data" else "--norun_data_pipeline",
        "--norun_inference" if stage == "data" else "--run_inference",
    ]
    if stage == "data":
        command.append(f"--db_dir={CANONICAL_DATABASE_DIR}")
    else:
        if model_dir is None:
            raise SystemExit("model-dir is required for inference")
        command.extend(
            [
                f"--model_dir={model_dir}",
                f"--num_diffusion_samples={num_diffusion_samples}",
            ]
        )
    return command


def _write_confidence(
    output_dir: Path, *, seeds: list[int], samples_per_seed: int
) -> dict[str, object]:
    summaries = sorted(output_dir.rglob("*_summary_confidences.json"))
    results: list[dict[str, object]] = []
    parent_pattern = re.compile(r"^seed-(?P<seed>[0-9]+)_sample-(?P<sample>[0-9]+)$")
    for summary in summaries:
        matched = parent_pattern.fullmatch(summary.parent.name)
        # AF3 also writes a top-level best copy. It is intentionally excluded.
        if matched is None:
            continue
        structures = sorted(summary.parent.glob("*.cif"))
        if len(structures) != 1:
            raise SystemExit(
                f"AlphaFold3 summary must pair with exactly one sample CIF: {summary}"
            )
        metrics = load_bounded_metrics(
            summary,
            {
                "ptm": (0.0, 1.0),
                "iptm": (0.0, 1.0),
                "fraction_disordered": (0.0, 1.0),
                "ranking_score": (-100.0, 2.0),
            },
            required={"ptm", "ranking_score"},
        )
        results.append(
            {
                "seed": int(matched.group("seed")),
                "sample_index": int(matched.group("sample")),
                "structure": structures[0],
                "summary": summary,
                "metrics": metrics,
            }
        )
    return write_confidence_envelope(
        output_dir,
        runtime_id="alphafold3",
        model_revision="85c4d20505fd5cef05eac22b534d4e793971ae69",
        seeds=seeds,
        samples_per_seed=samples_per_seed,
        results=results,
    )


def _run_upstream(command: list[str], *, h100: bool) -> None:
    if h100:
        import jax

        devices = [device for device in jax.devices() if device.platform == "gpu"]
        if not devices or "H100" not in devices[0].device_kind:
            raise SystemExit(
                f"exact AlphaFold3 readiness requires an H100; devices={jax.devices()}"
            )
    environment = dict(os.environ)
    environment.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    completed = subprocess.run(command, check=False, env=environment)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def _data(args: argparse.Namespace) -> None:
    input_json = _absolute(args.input_json, "input-json", directory=False)
    if re.fullmatch(r"[0-9a-f]{64}", args.raw_input_sha256) is None:
        raise SystemExit("raw-input-sha256 must be a lowercase SHA-256")
    from handoff_contract import sha256_file

    if sha256_file(input_json) != args.raw_input_sha256:
        raise SystemExit("raw-input-sha256 does not bind input-json")
    if not args.print_command:
        _validate_database_contract(args)
    output_dir = _output_path(args.output_dir, "output-dir")
    processed_json = _output_path(args.processed_json, "processed-json")
    provenance_marker = _output_path(args.provenance_marker, "provenance-marker")
    handoff_tar = _output_path(args.handoff_tar, "handoff-tar")
    seeds = _parse_seeds(args.model_seeds)
    output_dir.mkdir(parents=True, exist_ok=True)
    staged_input = output_dir / ".fs2-seeded-input.json"
    _bind_raw_input(input_json, staged_input, seeds)
    command = build_command(
        stage="data",
        json_path=staged_input,
        output_dir=output_dir,
        model_dir=None,
        num_diffusion_samples=1,
    )
    if args.print_command:
        print(json.dumps({"argv": command, "model_seeds": seeds}, sort_keys=True))
        return
    _run_upstream(command, h100=False)
    candidates = sorted(
        path
        for path in output_dir.rglob("*_data.json")
        if path not in {processed_json, staged_input}
    )
    if len(candidates) != 1:
        raise SystemExit(
            f"AlphaFold3 data stage produced {len(candidates)} processed JSON files; expected one"
        )
    processed_json.parent.mkdir(parents=True, exist_ok=True)
    if candidates[0].resolve() != processed_json.resolve():
        shutil.copyfile(candidates[0], processed_json)
    _assert_processed_seeds(processed_json, seeds)
    marker = write_handoff(
        processed_json,
        processed_json,
        provenance_marker,
        handoff_tar,
        args.output_artifact_id,
    )
    print(json.dumps(marker, sort_keys=True, separators=(",", ":")))


def _inference(args: argparse.Namespace) -> None:
    processed_json = _absolute(args.processed_json, "processed-json", directory=False)
    provenance_marker = _absolute(
        args.provenance_marker, "provenance-marker", directory=False
    )
    validate_handoff(processed_json, provenance_marker, args.input_artifact_id)
    seeds = _parse_seeds(args.model_seeds)
    if args.expected_reference_artifact_id != DATABASE_ARTIFACT_ID:
        raise SystemExit("expected-reference-artifact-id does not match AlphaFold3 databases")
    if _parse_seeds(args.expected_model_seeds) != seeds:
        raise SystemExit("expected-model-seeds does not match model-seeds")
    if args.expected_raw_input_sha256 is not None and re.fullmatch(
        r"[0-9a-f]{64}", args.expected_raw_input_sha256
    ) is None:
        raise SystemExit("expected-raw-input-sha256 must be a lowercase SHA-256")
    _assert_processed_seeds(processed_json, seeds)
    if not 1 <= args.num_diffusion_samples <= 16:
        raise SystemExit("num-diffusion-samples must be in [1, 16]")
    output_dir = _output_path(args.output_dir, "output-dir")
    output_dir.mkdir(parents=True, exist_ok=True)
    if Path(args.model_dir) != CANONICAL_MODEL_DIR:
        raise SystemExit("model-dir must be the canonical /models mount")
    model_dir = CANONICAL_MODEL_DIR
    command = build_command(
        stage="inference",
        json_path=processed_json,
        output_dir=output_dir,
        model_dir=model_dir,
        num_diffusion_samples=args.num_diffusion_samples,
    )
    if args.print_command:
        print(json.dumps({"argv": command, "model_seeds": seeds}, sort_keys=True))
        return
    _absolute(model_dir, "model-dir", directory=True)
    parameter_path = model_dir / "af3.bin.zst"
    if not parameter_path.is_file() or parameter_path.stat().st_size != PARAMETER_BYTES:
        raise SystemExit("official AlphaFold3 parameters do not match /models/af3.bin.zst")
    _run_upstream(command, h100=True)
    confidence = _write_confidence(
        output_dir,
        seeds=seeds,
        samples_per_seed=args.num_diffusion_samples,
    )
    print(json.dumps(confidence, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="stage", required=True)

    data = subparsers.add_parser("data")
    data.add_argument("--input-json", required=True)
    data.add_argument("--output-dir", required=True)
    data.add_argument("--processed-json", required=True)
    data.add_argument("--provenance-marker", required=True)
    data.add_argument("--handoff-tar", required=True)
    data.add_argument("--output-artifact-id", required=True)
    data.add_argument("--db-dir", required=True)
    data.add_argument("--db-manifest", required=True)
    data.add_argument("--db-ready-marker", required=True)
    data.add_argument("--reference-artifact-id", required=True)
    data.add_argument("--raw-input-sha256", required=True)
    data.add_argument("--model-seeds", required=True)
    data.add_argument("--print-command", action="store_true")
    data.set_defaults(handler=_data)

    inference = subparsers.add_parser("inference")
    inference.add_argument("--processed-json", required=True)
    inference.add_argument("--provenance-marker", required=True)
    inference.add_argument("--input-artifact-id", required=True)
    inference.add_argument("--expected-reference-artifact-id", required=True)
    inference.add_argument("--expected-model-seeds", required=True)
    inference.add_argument("--expected-raw-input-sha256")
    inference.add_argument("--output-dir", required=True)
    inference.add_argument("--model-dir", default="/models")
    inference.add_argument("--num-diffusion-samples", type=int, default=5)
    inference.add_argument("--model-seeds", required=True)
    inference.add_argument("--print-command", action="store_true")
    inference.set_defaults(handler=_inference)

    args = parser.parse_args(argv)
    args.handler(args)


if __name__ == "__main__":
    main(sys.argv[1:])

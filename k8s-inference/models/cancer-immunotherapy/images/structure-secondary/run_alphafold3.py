#!/usr/bin/env python3
"""Official AlphaFold3 CPU-data and H100-inference stage boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


UPSTREAM = [
    "/opt/alphafold3-venv/bin/python",
    "/opt/alphafold3/run_alphafold.py",
]
PARAMETER_BYTES = 1_020_545_840


def _absolute(path: str, label: str, *, directory: bool) -> Path:
    candidate = Path(path)
    exists = candidate.is_dir() if directory else candidate.is_file()
    if not candidate.is_absolute() or not exists:
        kind = "directory" if directory else "file"
        raise SystemExit(f"{label} must be an existing absolute {kind}: {candidate}")
    return candidate


def _load_document(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"AlphaFold3 input is not valid JSON: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise SystemExit("AlphaFold3 wrapper accepts exactly one JSON job object")
    return document


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def _validate_processed_handoff(
    processed: Path, marker_path: Path, seeds: list[int], db_dir: Path
) -> None:
    marker = _load_document(
        _absolute(str(marker_path), "processed-json marker", directory=False)
    )
    expected_keys = {
        "schema",
        "input_json_sha256",
        "processed_json",
        "processed_json_sha256",
        "model_seeds",
        "database_root",
    }
    if set(marker) != expected_keys:
        raise SystemExit("processed-json marker has an unexpected shape")
    if (
        marker.get("schema") != "fs2.nebius.ai/alphafold3-processed-input/v1"
        or marker.get("processed_json") != str(processed)
        or marker.get("processed_json_sha256") != _sha256(processed)
        or marker.get("model_seeds") != seeds
        or marker.get("database_root") != str(db_dir)
        or not isinstance(marker.get("input_json_sha256"), str)
        or len(str(marker["input_json_sha256"])) != 64
    ):
        raise SystemExit("processed-json marker does not bind the deterministic AF3 handoff")


def build_command(
    *,
    stage: str,
    json_path: Path,
    output_dir: Path,
    model_dir: Path,
    db_dir: Path,
    num_diffusion_samples: int,
) -> list[str]:
    command = [
        *UPSTREAM,
        f"--json_path={json_path}",
        f"--output_dir={output_dir}",
        f"--model_dir={model_dir}",
        f"--db_dir={db_dir}",
        "--force_output_dir",
        "--run_data_pipeline" if stage == "data" else "--norun_data_pipeline",
        "--norun_inference" if stage == "data" else "--run_inference",
    ]
    if stage == "inference":
        command.append(f"--num_diffusion_samples={num_diffusion_samples}")
    return command


def _run(args: argparse.Namespace) -> None:
    input_name = "input-json" if args.stage == "data" else "processed-json"
    json_path = _absolute(args.json_path, input_name, directory=False)
    model_dir = _absolute(args.model_dir, "model-dir", directory=True)
    db_dir = _absolute(args.db_dir, "db-dir", directory=True)
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        raise SystemExit("output-dir must be absolute")
    output_dir.mkdir(parents=True, exist_ok=True)
    seeds = _parse_seeds(args.seeds)
    if args.num_diffusion_samples < 1 or args.num_diffusion_samples > 16:
        raise SystemExit("num-diffusion-samples must be in [1, 16]")

    processed_output: Path | None = None
    if args.stage == "data":
        processed_output = Path(args.processed_json_output)
        if not processed_output.is_absolute():
            raise SystemExit("processed-json-output must be absolute")
        processed_output.parent.mkdir(parents=True, exist_ok=True)
        staged_input = output_dir / ".fs2-seeded-input.json"
        _bind_raw_input(json_path, staged_input, seeds)
        command_input = staged_input
    else:
        parameter_path = model_dir / "af3.bin.zst"
        if not parameter_path.is_file():
            raise SystemExit(
                f"official AlphaFold3 parameters are missing: {parameter_path}"
            )
        if parameter_path.stat().st_size != PARAMETER_BYTES:
            raise SystemExit("official AlphaFold3 parameter byte count does not match the lock")
        _assert_processed_seeds(json_path, seeds)
        marker_path = Path(
            args.processed_json_marker or f"{json_path}.fs2.json"
        )
        _validate_processed_handoff(json_path, marker_path, seeds, db_dir)
        command_input = json_path

    command = build_command(
        stage=args.stage,
        json_path=command_input,
        output_dir=output_dir,
        model_dir=model_dir,
        db_dir=db_dir,
        num_diffusion_samples=args.num_diffusion_samples,
    )
    if args.print_command:
        print(
            json.dumps(
                {
                    "argv": command,
                    "stage": args.stage,
                    "seeds": seeds,
                    "processed_json_output": (
                        str(processed_output) if processed_output is not None else None
                    ),
                    "network_policy": "mounted-model-and-databases-only",
                },
                sort_keys=True,
            )
        )
        return

    if args.stage == "inference":
        import jax

        devices = [device for device in jax.devices() if device.platform == "gpu"]
        if not devices or "H100" not in devices[0].device_kind:
            raise SystemExit(
                f"exact AlphaFold3 readiness requires an H100; devices={jax.devices()}"
            )
        environment = dict(os.environ)
        environment.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
        os.execve(command[0], command, environment)

    environment = dict(os.environ)
    environment.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    completed = subprocess.run(command, check=False, env=environment)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    candidates = sorted(
        path
        for path in output_dir.rglob("*_data.json")
        if processed_output is None or path != processed_output
    )
    if len(candidates) != 1 or processed_output is None:
        raise SystemExit(
            f"AlphaFold3 data stage produced {len(candidates)} processed JSON files; expected one"
        )
    shutil.copyfile(candidates[0], processed_output)
    _assert_processed_seeds(processed_output, seeds)
    marker = {
        "schema": "fs2.nebius.ai/alphafold3-processed-input/v1",
        "input_json_sha256": _sha256(json_path),
        "processed_json": str(processed_output),
        "processed_json_sha256": _sha256(processed_output),
        "model_seeds": seeds,
        "database_root": str(db_dir),
    }
    Path(f"{processed_output}.fs2.json").write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(marker, sort_keys=True, separators=(",", ":")))


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="stage", required=True)
    for stage in ("data", "inference"):
        command = subparsers.add_parser(stage)
        input_flag = "--input-json" if stage == "data" else "--processed-json"
        command.add_argument(input_flag, dest="json_path", required=True)
        command.add_argument("--output-dir", required=True)
        if stage == "data":
            command.add_argument("--processed-json-output", required=True)
        else:
            command.add_argument("--processed-json-marker")
        command.add_argument("--seeds", required=True)
        command.add_argument("--num-diffusion-samples", type=int, default=5)
        command.add_argument(
            "--model-dir", default=os.environ.get("FS2_MODEL_DIR", "/models")
        )
        command.add_argument(
            "--db-dir", default=os.environ.get("FS2_DATABASE_DIR", "/databases")
        )
        command.add_argument("--print-command", action="store_true")
        command.set_defaults(handler=_run)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()

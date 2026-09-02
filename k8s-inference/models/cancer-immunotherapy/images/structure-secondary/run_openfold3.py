#!/usr/bin/env python3
"""Offline OpenFold3 boundary with explicit checkpoint and CCD artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import yaml


CCD_BYTES = 63_393_643
CCD_SHA256 = "473d845c8b250b188dbed9bf505ae206692a178a2a7c4869bf8f9de707ffcc0c"


def _file(path: str, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_file():
        raise SystemExit(f"{label} must be an existing absolute file: {candidate}")
    return candidate


def _parse_seeds(value: str) -> list[int]:
    try:
        seeds = [int(item) for item in value.split(",")]
    except ValueError as exc:
        raise SystemExit("seeds must be comma-separated integers") from exc
    if (
        not 1 <= len(seeds) <= 16
        or len(set(seeds)) != len(seeds)
        or any(seed < 0 or seed > 2**32 - 1 for seed in seeds)
    ):
        raise SystemExit("seeds must contain 1..16 unique uint32 values")
    return seeds


def _validate_runner_seeds(path: Path, seeds: list[int]) -> None:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SystemExit(f"runner-yaml is invalid: {exc}") from exc
    settings = document.get("experiment_settings") if isinstance(document, dict) else None
    if not isinstance(settings, dict) or settings.get("seeds") != seeds:
        raise SystemExit("runner-yaml does not bind the requested exact model seeds")


def _validate_prepared_handoff(
    marker_path: Path,
    query: Path,
    runner_yaml: Path,
    seeds: list[int],
    ccd_path: Path,
) -> None:
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"prepared-marker is invalid: {exc}") from exc
    expected_keys = {
        "schema",
        "query_json",
        "query_sha256",
        "msa_mode",
        "model_seeds",
        "runner_yaml",
        "ccd_path",
        "ccd_sha256",
        "network_policy",
    }
    if not isinstance(marker, dict) or set(marker) != expected_keys:
        raise SystemExit("prepared-marker has an unexpected shape")
    if (
        marker.get("schema") != "fs2.nebius.ai/openfold3-prepared-query/v1"
        or marker.get("query_json") != str(query)
        or marker.get("query_sha256") != hashlib.sha256(query.read_bytes()).hexdigest()
        or marker.get("msa_mode") not in {"none", "precomputed"}
        or marker.get("model_seeds") != seeds
        or marker.get("runner_yaml") != str(runner_yaml)
        or marker.get("ccd_path") != str(ccd_path)
        or marker.get("ccd_sha256") != CCD_SHA256
        or marker.get("network_policy") != "offline"
    ):
        raise SystemExit("prepared-marker does not bind the exact offline OpenFold3 handoff")


def build_command(
    *,
    query: Path,
    output: Path,
    checkpoint: Path,
    runner_yaml: Path,
    use_precomputed_templates: bool,
    num_diffusion_samples: int,
) -> list[str]:
    command = [
        "run_openfold",
        "predict",
        "--query-json",
        str(query),
        "--output-dir",
        str(output),
        "--inference-ckpt-path",
        str(checkpoint),
        "--use-msa-server",
        "false",
        "--use-templates",
        str(use_precomputed_templates).lower(),
        "--num-diffusion-samples",
        str(num_diffusion_samples),
        "--runner-yaml",
        str(runner_yaml),
    ]
    return command


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query-json", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--checkpoint",
        default=os.path.join(os.environ.get("FS2_MODEL_DIR", ""), "of3-ob-2025-06-30-174k.pt"),
    )
    parser.add_argument("--ccd-path", default=os.environ.get("FS2_OPENFOLD3_CCD_PATH", ""))
    parser.add_argument("--runner-yaml", required=True)
    parser.add_argument("--prepared-marker", required=True)
    parser.add_argument("--seeds", required=True)
    parser.add_argument("--use-precomputed-templates", action="store_true")
    parser.add_argument("--num-diffusion-samples", type=int, default=1)
    parser.add_argument("--print-command", action="store_true")
    args = parser.parse_args()

    query = _file(args.query_json, "query-json")
    checkpoint = _file(args.checkpoint, "checkpoint")
    ccd_path = _file(args.ccd_path, "ccd-path")
    output = Path(args.output_dir)
    if not output.is_absolute():
        raise SystemExit("output-dir must be absolute")
    output.mkdir(parents=True, exist_ok=True)

    if checkpoint.stat().st_size != 2_287_872_989:
        raise SystemExit("OpenFold3 checkpoint byte count does not match OpenBind-0")
    if ccd_path.stat().st_size != CCD_BYTES:
        raise SystemExit("OpenFold3 components.bcif byte count does not match the lock")
    runner_yaml = _file(args.runner_yaml, "runner-yaml")
    seeds = _parse_seeds(args.seeds)
    _validate_runner_seeds(runner_yaml, seeds)
    prepared_marker = _file(args.prepared_marker, "prepared-marker")
    _validate_prepared_handoff(prepared_marker, query, runner_yaml, seeds, ccd_path)
    command = build_command(
        query=query,
        output=output,
        checkpoint=checkpoint,
        runner_yaml=runner_yaml,
        use_precomputed_templates=args.use_precomputed_templates,
        num_diffusion_samples=args.num_diffusion_samples,
    )
    if args.print_command:
        print(
            json.dumps(
                {"argv": command, "network_policy": "offline", "seeds": seeds},
                sort_keys=True,
            )
        )
        return

    import torch

    if not torch.cuda.is_available() or tuple(torch.cuda.get_device_capability(0)) != (9, 0):
        raise SystemExit("exact OpenFold3 readiness requires an H100 (SM90)")
    os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    # The public API replaces the CCD and clears all Biotite CCD-dependent
    # caches. Assigning Biotite's private _CCD_FILE does not clear those caches.
    from biotite.structure.info import ccd

    ccd.set_ccd_path(ccd_path)
    from openfold3.run_openfold import cli

    cli.main(args=command[1:], prog_name="run_openfold", standalone_mode=True)


if __name__ == "__main__":
    main()

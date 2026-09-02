#!/usr/bin/env python3
"""Offline OpenFold3 boundary with explicit checkpoint and CCD artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

import yaml

from result_contract import load_bounded_metrics, write_confidence_envelope


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
) -> None:
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"prepared-marker is invalid: {exc}") from exc
    expected_keys = {
        "schema",
        "query_sha256",
        "msa_mode",
        "model_seeds",
        "runner_yaml_sha256",
        "ccd_sha256",
        "network_policy",
    }
    if not isinstance(marker, dict) or set(marker) != expected_keys:
        raise SystemExit("prepared-marker has an unexpected shape")
    if (
        marker.get("schema") != "fs2.nebius.ai/openfold3-prepared-query/v1"
        or marker.get("query_sha256") != hashlib.sha256(query.read_bytes()).hexdigest()
        or marker.get("msa_mode") not in {"none", "precomputed"}
        or marker.get("model_seeds") != seeds
        or marker.get("runner_yaml_sha256")
        != hashlib.sha256(runner_yaml.read_bytes()).hexdigest()
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


def _write_confidence(
    output_dir: Path, *, seeds: list[int], samples_per_seed: int
) -> dict[str, object]:
    summaries = sorted(output_dir.rglob("*_confidences_aggregated.json"))
    pattern = re.compile(
        r"^(?P<prefix>.+)_seed_(?P<seed>[0-9]+)_sample_"
        r"(?P<sample>[1-9][0-9]*)_confidences_aggregated\.json$"
    )
    results: list[dict[str, object]] = []
    for summary in summaries:
        matched = pattern.fullmatch(summary.name)
        if matched is None:
            raise SystemExit(f"OpenFold3 confidence filename is not canonical: {summary}")
        prefix = (
            f"{matched.group('prefix')}_seed_{matched.group('seed')}_"
            f"sample_{matched.group('sample')}_model"
        )
        structures = sorted(
            path
            for path in summary.parent.glob(f"{prefix}.*")
            if path.suffix.lower() in {".cif", ".mmcif", ".pdb"}
        )
        if len(structures) != 1:
            raise SystemExit(
                f"OpenFold3 summary must pair with exactly one sample structure: {summary}"
            )
        metrics = load_bounded_metrics(
            summary,
            {
                "avg_plddt": (0.0, 100.0),
                "gpde": (0.0, 64.0),
                "ptm": (0.0, 1.0),
                "iptm": (0.0, 1.0),
                "disorder": (0.0, 1.0),
                "sample_ranking_score": (-100.0, 2.0),
            },
            required={"avg_plddt", "gpde"},
        )
        results.append(
            {
                "seed": int(matched.group("seed")),
                "sample_index": int(matched.group("sample")) - 1,
                "structure": structures[0],
                "summary": summary,
                "metrics": metrics,
            }
        )
    return write_confidence_envelope(
        output_dir,
        runtime_id="openfold3",
        model_revision="c4771653c5d0a3ebb0b3af71b05efd64bc44ee86",
        seeds=seeds,
        samples_per_seed=samples_per_seed,
        results=results,
    )


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

    if not 1 <= args.num_diffusion_samples <= 16:
        raise SystemExit("num-diffusion-samples must be in [1, 16]")

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
    _validate_prepared_handoff(prepared_marker, query, runner_yaml, seeds)
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

    cli.main(args=command[1:], prog_name="run_openfold", standalone_mode=False)
    confidence = _write_confidence(
        output,
        seeds=seeds,
        samples_per_seed=args.num_diffusion_samples,
    )
    print(json.dumps(confidence, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()

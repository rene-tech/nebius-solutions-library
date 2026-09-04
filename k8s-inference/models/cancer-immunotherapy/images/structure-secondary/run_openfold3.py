#!/usr/bin/env python3
"""One offline OpenFold3 prepare/predict surface with exact seeded handoff."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import yaml

from handoff_contract import write_archive
from result_contract import load_bounded_metrics, write_confidence_envelope
from runtime_localization import (
    RuntimeArtifactExpectation,
    validate_runtime_localization,
)


CCD_BYTES = 63_393_643
CCD_SHA256 = "473d845c8b250b188dbed9bf505ae206692a178a2a7c4869bf8f9de707ffcc0c"
CHECKPOINT_BYTES = 2_287_872_989
CHECKPOINT_CONTENT_SHA256 = "f954e2f2e3d0bdba297ac8009f6d590b3e2c28ca2985742c9bbd8167f276f6b5"
CCD_CONTENT_SHA256 = "ff75f66793c11d7cb63531c758b210fa6fe33d5a39378bb0ab89094278e95e3b"
CANONICAL_CHECKPOINT = Path("/models/openfold3/of3-ob-2025-06-30-174k.pt")
CANONICAL_CCD = Path("/databases/openfold3/components.bcif")
CANONICAL_BASE_RUNNER = Path("/opt/fs2/runtime/openfold3/runner-base.yaml")
BASE_RUNNER_SHA256 = "c42271cdfc4c9dd01ceca7a9e0c2d0a207c2d8106a2bb03146d491d54b601469"
OPENFOLD_HANDOFF_SCHEMA = "fs2.nebius.ai/openfold3-query-handoff/v1"
PUBLIC_MODEL_ID = "openfold3-openbind"
SOURCE_REVISION = "c4771653c5d0a3ebb0b3af71b05efd64bc44ee86"
LANE_ID = "openfold3-openbind-0-none"
VARIANT_ID = "upstream-openbind-v0-5-0"
EXTERNAL_CHAIN_FIELDS = (
    "paired_msa_file_paths",
    "main_msa_file_paths",
    "template_alignment_file_path",
    "template_entry_chain_ids",
    "template_cif_paths",
    "template_cif_chain_ids",
    "sdf_file_path",
)


def _file(path: str | Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_file():
        raise SystemExit(f"{label} must be an existing absolute file: {candidate}")
    return candidate


def _output(path: str | Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise SystemExit(f"{label} must be an absolute path: {candidate}")
    return candidate


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _logical_id(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value) is None:
        raise SystemExit("stage artifact ID must be a valid bounded logical ID")
    return value


def _validate_runtime_localization_args(
    command: str, args: argparse.Namespace
) -> dict[str, object]:
    if command != "predict":
        raise SystemExit(f"{command} does not consume OpenFold3 runtime artifacts")
    return validate_runtime_localization(
        args.runtime_localization_marker,
        model_id=PUBLIC_MODEL_ID,
        variant_id=VARIANT_ID,
        stage_id="inference",
        artifacts=(
            RuntimeArtifactExpectation(
                "openfold3-openbind-0",
                "/models/openfold3",
                CHECKPOINT_CONTENT_SHA256,
            ),
            RuntimeArtifactExpectation(
                "openfold3-components-bcif",
                "/databases/openfold3",
                CCD_CONTENT_SHA256,
            ),
        ),
    )


def _parse_seeds(value: str) -> list[int]:
    try:
        seeds = [int(item) for item in value.split(",")]
    except ValueError as exc:
        raise SystemExit("model-seeds must be comma-separated integers") from exc
    if (
        not 1 <= len(seeds) <= 16
        or len(set(seeds)) != len(seeds)
        or any(seed < 0 or seed > 2**32 - 1 for seed in seeds)
    ):
        raise SystemExit("model-seeds must contain 1..16 unique uint32 values")
    return seeds


def _load_yaml(path: Path, label: str) -> dict[str, object]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SystemExit(f"{label} is invalid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise SystemExit(f"{label} must be a YAML mapping")
    return document


def _validate_runner_seeds(path: Path, seeds: list[int]) -> None:
    document = _load_yaml(path, "runner-yaml")
    settings = document.get("experiment_settings")
    if not isinstance(settings, dict) or settings.get("seeds") != seeds:
        raise SystemExit("runner-yaml does not bind the requested exact model seeds")


def _write_seeded_runner(base: Path, destination: Path, seeds: list[int]) -> None:
    runner_document = _load_yaml(base, "base-runner-yaml")
    settings = runner_document.setdefault("experiment_settings", {})
    if not isinstance(settings, dict):
        raise SystemExit("base-runner-yaml experiment_settings must be a mapping")
    settings.update(
        {
            "mode": "predict",
            "seeds": seeds,
            "use_msa_server": False,
            "use_templates": False,
        }
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        yaml.safe_dump(runner_document, sort_keys=True), encoding="utf-8"
    )
    _validate_runner_seeds(destination, seeds)


def _bind_msa_mode(document: dict[str, object], mode: str) -> None:
    queries = document.get("queries")
    if not isinstance(queries, dict) or len(queries) != 1:
        raise SystemExit("OpenFold3 input must contain exactly one query object")
    for query_name, query in queries.items():
        if not isinstance(query, dict) or not isinstance(query.get("chains"), list):
            raise SystemExit(f"OpenFold3 query {query_name!r} must contain a chains array")
        if mode != "none":
            raise SystemExit("only the fail-closed OpenFold3 msa_mode=none lane is supported")
        for chain in query["chains"]:
            if not isinstance(chain, dict):
                raise SystemExit(f"OpenFold3 query {query_name!r} contains an invalid chain")
            populated = [
                field
                for field in EXTERNAL_CHAIN_FIELDS
                if field in chain and chain[field] not in (None, "", [], {})
            ]
            if populated:
                raise SystemExit(
                    "OpenFold3 msa_mode=none rejects external Chain fields: "
                    + ", ".join(populated)
                )
        # These are Query controls in OpenFold3 v0.5.0, not Chain controls.
        query["use_msas"] = False
        query["use_main_msas"] = False
        query["use_paired_msas"] = False


def build_command(
    *,
    query: Path,
    output: Path,
    checkpoint: Path,
    runner_yaml: Path,
    num_diffusion_samples: int,
) -> list[str]:
    return [
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
        "false",
        "--num-diffusion-samples",
        str(num_diffusion_samples),
        "--runner-yaml",
        str(runner_yaml),
    ]


def _prepare(args: argparse.Namespace) -> None:
    input_manifest = _file(args.input_manifest, "input-manifest")
    _logical_id(args.output_artifact_id)
    raw_sha256 = _sha256(input_manifest)
    if raw_sha256 != args.raw_input_sha256:
        raise SystemExit("raw-input-sha256 does not bind input-manifest")
    try:
        document = json.loads(input_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"input-manifest is invalid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise SystemExit("input-manifest must be a JSON object")
    seeds = _parse_seeds(args.model_seeds)
    _bind_msa_mode(document, args.msa_mode)
    document["seeds"] = seeds
    query_json = _output(args.query_json, "query-json")
    query_json.parent.mkdir(parents=True, exist_ok=True)
    query_json.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    base_runner = _file(args.base_runner_yaml, "base-runner-yaml")
    if Path(args.base_runner_yaml) != CANONICAL_BASE_RUNNER:
        raise SystemExit("base-runner-yaml must be the image-baked canonical runner")
    if _sha256(base_runner) != BASE_RUNNER_SHA256:
        raise SystemExit("image-baked OpenFold3 runner identity changed")
    runner_yaml = _output(args.runner_yaml, "runner-yaml")
    _write_seeded_runner(base_runner, runner_yaml, seeds)
    provenance_marker = _output(args.provenance_marker, "provenance-marker")
    provenance_marker.parent.mkdir(parents=True, exist_ok=True)
    marker = {
        "schema": OPENFOLD_HANDOFF_SCHEMA,
        "artifact_id": args.output_artifact_id,
        "member": "query.json",
        "sha256": _sha256(query_json),
        "raw_input_sha256": raw_sha256,
        "model_seeds": seeds,
        "msa_mode": args.msa_mode,
        "runner_base_sha256": _sha256(base_runner),
        "lane_id": LANE_ID,
        "source_revision": SOURCE_REVISION,
    }
    provenance_marker.write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    write_archive(
        _output(args.handoff_tar, "handoff-tar"),
        {"query.json": query_json, "provenance.json": provenance_marker},
    )
    print(
        json.dumps(
            {
                "schema": "fs2.nebius.ai/openfold3-prepared-query/v2",
                "query_json": query_json.name,
                "query_sha256": _sha256(query_json),
                "runner_yaml": runner_yaml.name,
                "runner_yaml_sha256": _sha256(runner_yaml),
                "model_seeds": seeds,
                "msa_mode": args.msa_mode,
                "network_policy": "offline",
                "handoff": marker,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _write_confidence(
    output_dir: Path, *, seeds: list[int], samples_per_seed: int
) -> dict[str, object]:
    summaries = sorted(output_dir.rglob("*_confidences_aggregated.json"))
    results: list[dict[str, object]] = []
    seed_pattern = re.compile(r"^seed_(?P<seed>[0-9]+)$")
    file_pattern = re.compile(
        r"^(?P<prefix>.+)_seed_(?P<file_seed>[0-9]+)_sample_"
        r"(?P<sample>[1-9][0-9]*)_confidences_aggregated\.json$"
    )
    for summary in summaries:
        seed_match = seed_pattern.fullmatch(summary.parent.name)
        file_match = file_pattern.fullmatch(summary.name)
        if seed_match is None or file_match is None:
            raise SystemExit(f"OpenFold3 confidence path is not canonical: {summary}")
        seed = int(seed_match.group("seed"))
        if seed != int(file_match.group("file_seed")):
            raise SystemExit(f"OpenFold3 confidence filename seed disagrees with its parent: {summary}")
        one_based_sample = int(file_match.group("sample"))
        sample_index = one_based_sample - 1
        prefix = (
            f"{file_match.group('prefix')}_seed_{seed}_sample_{one_based_sample}_model"
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
                "seed": seed,
                "sample_index": sample_index,
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


def _predict(args: argparse.Namespace) -> None:
    _validate_runtime_localization_args("predict", args)
    _logical_id(args.input_artifact_id)
    if re.fullmatch(r"[0-9a-f]{64}", args.expected_raw_input_sha256) is None:
        raise SystemExit("expected-raw-input-sha256 must be a lowercase SHA-256")
    if Path(args.checkpoint) != CANONICAL_CHECKPOINT:
        raise SystemExit("checkpoint must be the canonical mounted OpenBind-0 object")
    if Path(args.ccd_path) != CANONICAL_CCD:
        raise SystemExit("ccd-path must be the canonical mounted components.bcif object")
    query = _file(args.query_json, "query-json")
    provenance_marker = _file(args.provenance_marker, "provenance-marker")
    checkpoint = _file(args.checkpoint, "checkpoint")
    ccd_path = _file(args.ccd_path, "ccd-path")
    base_runner = _file(args.base_runner_yaml, "base-runner-yaml")
    if Path(args.base_runner_yaml) != CANONICAL_BASE_RUNNER:
        raise SystemExit("base-runner-yaml must be the image-baked canonical runner")
    if _sha256(base_runner) != BASE_RUNNER_SHA256:
        raise SystemExit("image-baked OpenFold3 runner identity changed")
    runner_yaml = _output(args.runner_yaml, "runner-yaml")
    output = _output(args.output_dir, "output-dir")
    output.mkdir(parents=True, exist_ok=True)
    if checkpoint.stat().st_size != CHECKPOINT_BYTES:
        raise SystemExit("OpenFold3 checkpoint byte count does not match OpenBind-0")
    if ccd_path.stat().st_size != CCD_BYTES or _sha256(ccd_path) != CCD_SHA256:
        raise SystemExit("OpenFold3 components.bcif does not match the pinned object")
    seeds = _parse_seeds(args.model_seeds)
    if args.num_model_seeds != len(seeds):
        raise SystemExit("num-model-seeds must equal the exact model-seeds cardinality")
    if args.num_diffusion_samples != 1:
        raise SystemExit("the required OpenFold3 acceptance boundary requires num-diffusion-samples=1")
    try:
        marker = json.loads(provenance_marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"provenance-marker is invalid: {exc}") from exc
    expected_marker = {
        "schema": OPENFOLD_HANDOFF_SCHEMA,
        "artifact_id": args.input_artifact_id,
        "member": "query.json",
        "sha256": _sha256(query),
        "raw_input_sha256": args.expected_raw_input_sha256,
        "model_seeds": seeds,
        "msa_mode": args.msa_mode,
        "runner_base_sha256": _sha256(base_runner),
        "lane_id": LANE_ID,
        "source_revision": SOURCE_REVISION,
    }
    if marker != expected_marker:
        raise SystemExit("provenance-marker does not bind the relocated OpenFold3 query")
    prepared_document = json.loads(query.read_text(encoding="utf-8"))
    canonical_document = json.loads(query.read_text(encoding="utf-8"))
    if not isinstance(canonical_document, dict):
        raise SystemExit("query-json must contain an object")
    _bind_msa_mode(canonical_document, args.msa_mode)
    if canonical_document.get("seeds") != seeds:
        raise SystemExit("query-json does not bind the exact ordered model seeds")
    if canonical_document != prepared_document:
        raise SystemExit("query-json is not in the canonical offline MSA mode")
    _write_seeded_runner(base_runner, runner_yaml, seeds)
    command = build_command(
        query=query,
        output=output,
        checkpoint=checkpoint,
        runner_yaml=runner_yaml,
        num_diffusion_samples=args.num_diffusion_samples,
    )
    if args.print_command:
        print(json.dumps({"argv": command, "model_seeds": seeds}, sort_keys=True))
        return

    import torch

    if not torch.cuda.is_available() or tuple(torch.cuda.get_device_capability(0)) != (9, 0):
        raise SystemExit("exact OpenFold3 readiness requires an H100 (SM90)")
    os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    from biotite.structure.info import ccd

    ccd.set_ccd_path(ccd_path)
    from openfold3.run_openfold import cli

    cli.main(args=command[1:], prog_name="run_openfold", standalone_mode=False)
    confidence = _write_confidence(output, seeds=seeds, samples_per_seed=1)
    print(json.dumps(confidence, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--input-manifest", required=True)
    prepare.add_argument("--query-json", required=True)
    prepare.add_argument("--base-runner-yaml", required=True)
    prepare.add_argument("--runner-yaml", required=True)
    prepare.add_argument("--provenance-marker", required=True)
    prepare.add_argument("--handoff-tar", required=True)
    prepare.add_argument("--output-artifact-id", required=True)
    prepare.add_argument("--raw-input-sha256", required=True)
    prepare.add_argument("--msa-mode", choices=("none",), required=True)
    prepare.add_argument("--model-seeds", required=True)
    prepare.add_argument("--offline", action="store_true", required=True)
    prepare.set_defaults(handler=_prepare)

    predict = commands.add_parser("predict")
    predict.add_argument("--query-json", required=True)
    predict.add_argument("--provenance-marker", required=True)
    predict.add_argument("--input-artifact-id", required=True)
    predict.add_argument("--expected-raw-input-sha256", required=True)
    predict.add_argument("--output-dir", required=True)
    predict.add_argument("--checkpoint", required=True)
    predict.add_argument("--ccd-path", required=True)
    predict.add_argument("--runner-yaml", required=True)
    predict.add_argument("--base-runner-yaml", required=True)
    predict.add_argument("--num-diffusion-samples", type=int, required=True)
    predict.add_argument("--num-model-seeds", type=int, required=True)
    predict.add_argument("--model-seeds", required=True)
    predict.add_argument("--msa-mode", choices=("none",), required=True)
    predict.add_argument("--use-templates", choices=("false",), required=True)
    predict.add_argument("--runtime-localization-marker", required=True)
    predict.add_argument("--print-command", action="store_true")
    predict.set_defaults(handler=_predict)

    args = parser.parse_args(argv)
    args.handler(args)


if __name__ == "__main__":
    main(sys.argv[1:])

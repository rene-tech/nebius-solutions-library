#!/usr/bin/env python3
"""Prepare a validated offline OpenFold3 query for the GPU prediction stage."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _file(path: str, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_file():
        raise SystemExit(f"{label} must be an existing absolute file: {candidate}")
    return candidate


def _directory(path: str, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_dir():
        raise SystemExit(f"{label} must be an existing absolute directory: {candidate}")
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bind_msa_mode(document: dict[str, object], mode: str) -> None:
    queries = document.get("queries")
    if not isinstance(queries, dict) or len(queries) != 1:
        raise SystemExit("OpenFold3 input must contain exactly one query object")
    for query_name, query in queries.items():
        if not isinstance(query, dict) or not isinstance(query.get("chains"), list):
            raise SystemExit(f"OpenFold3 query {query_name!r} must contain a chains array")
        for chain in query["chains"]:
            if not isinstance(chain, dict):
                raise SystemExit(f"OpenFold3 query {query_name!r} contains an invalid chain")
            molecule_type = chain.get("molecule_type")
            if molecule_type not in {"protein", "rna"}:
                continue
            if mode == "none":
                chain["use_msas"] = False
                chain["use_main_msas"] = False
                chain["use_paired_msas"] = False
                continue
            paths = chain.get("main_msa_file_paths")
            path_values = [paths] if isinstance(paths, str) else paths
            if not isinstance(path_values, list) or not path_values:
                raise SystemExit(
                    f"precomputed mode requires main_msa_file_paths for {query_name!r}"
                )
            for path_value in path_values:
                if not isinstance(path_value, str):
                    raise SystemExit("precomputed MSA paths must be strings")
                _file(path_value, "precomputed MSA")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--msa-mode", choices=("none", "precomputed"), required=True)
    parser.add_argument("--database-dir", default="/databases/openfold3")
    parser.add_argument("--seeds", required=True)
    parser.add_argument("--offline", action="store_true", required=True)
    args = parser.parse_args()

    input_path = _file(args.input, "input")
    database_dir = _directory(args.database_dir, "database-dir")
    ccd = _file(str(database_dir / "components.bcif"), "OpenFold3 CCD")
    if ccd.stat().st_size != 63_393_643:
        raise SystemExit("OpenFold3 components.bcif byte count is not the pinned object")
    ccd_sha256 = _sha256(ccd)
    if ccd_sha256 != "473d845c8b250b188dbed9bf505ae206692a178a2a7c4869bf8f9de707ffcc0c":
        raise SystemExit("OpenFold3 components.bcif SHA-256 is not the pinned object")
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        raise SystemExit("output-dir must be absolute")
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        document = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"input is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise SystemExit("OpenFold3 input must be a JSON object")
    _bind_msa_mode(document, args.msa_mode)
    try:
        seeds = [int(value) for value in args.seeds.split(",")]
    except ValueError as exc:
        raise SystemExit("seeds must be comma-separated integers") from exc
    if (
        not 1 <= len(seeds) <= 16
        or len(set(seeds)) != len(seeds)
        or any(seed < 0 or seed > 2**32 - 1 for seed in seeds)
    ):
        raise SystemExit("seeds must contain 1..16 unique uint32 values")

    query = output_dir / "query.json"
    query.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    runner = output_dir / "runner.yaml"
    runner.write_text(
        json.dumps({"experiment_settings": {"seeds": seeds}}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    marker = {
        "schema": "fs2.nebius.ai/openfold3-prepared-query/v1",
        "query_sha256": _sha256(query),
        "msa_mode": args.msa_mode,
        "model_seeds": seeds,
        "runner_yaml_sha256": _sha256(runner),
        "ccd_sha256": ccd_sha256,
        "network_policy": "offline",
    }
    (output_dir / "prepared-query.fs2.json").write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(marker, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()

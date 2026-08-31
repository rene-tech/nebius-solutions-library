#!/usr/bin/env python3
"""Offline command line interface for catalog consumers and CI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .acquisition import acquire_huggingface_artifact
from .artifacts import load_artifact_manifest
from .loader import CatalogError, load_catalog
from .staging import LOCALIZER_OWNER, stage_artifact


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--catalog-root", required=True, type=Path)
    stage = subparsers.add_parser("stage")
    stage.add_argument("--manifest", required=True, type=Path)
    stage.add_argument("--source-root", required=True, type=Path)
    stage.add_argument("--cache-root", required=True, type=Path)
    stage.add_argument("--controller-owner", required=True, choices=[LOCALIZER_OWNER])
    stage.add_argument("--serving-node-name", required=True)
    stage.add_argument("--serving-node-uid", required=True)
    stage.add_argument("--serving-node-provider-id-sha256", required=True)
    stage.add_argument("--reserve-bytes", type=int, default=8 * 1024**3)
    stage.add_argument("--concurrency", type=int, default=4)
    acquire = subparsers.add_parser("acquire-hf")
    acquire.add_argument("--catalog-root", required=True, type=Path)
    acquire.add_argument("--model-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "stage":
            receipt = stage_artifact(
                load_artifact_manifest(args.manifest),
                args.source_root,
                args.cache_root,
                controller_owner=args.controller_owner,
                serving_node_name=args.serving_node_name,
                serving_node_uid=args.serving_node_uid,
                serving_node_provider_id_sha256=args.serving_node_provider_id_sha256,
                reserve_bytes=args.reserve_bytes,
                max_concurrent_files=args.concurrency,
            )
            print(json.dumps(receipt, sort_keys=True))
            return 0
        catalog = load_catalog(args.catalog_root)
        if args.command == "acquire-hf":
            record = catalog.model(args.model_id)
            plan = catalog.acquisition_plan(args.model_id)
            receipt = acquire_huggingface_artifact(
                record,
                plan,
                plan.to_dict()["destination_prefix"],
            )
            print(json.dumps(receipt, sort_keys=True))
            return 0
    except CatalogError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "status": "PASS",
                "schema": "fs2-serve.nebius.ai/catalog-validation/v1",
                "catalog_version": catalog.version,
                "catalog_digest": catalog.digest,
                "models": len(catalog.records),
                "tested_models": len(catalog.tested_model_ids),
                "blocked_candidates": list(catalog.blocked_candidate_ids),
                "routable_models": list(catalog.routable_model_ids()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

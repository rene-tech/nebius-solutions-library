"""Shared public parameters; profiles describe resources, not scientific caps."""

from __future__ import annotations

import copy
import json
from typing import Any

from jsonschema import Draft202012Validator

from . import PARAMETER_SCHEMA

MAX_INPUT_BYTES = 25 * 1024**3


def request_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema"],
        "description": "Queued scVI integration/scANVI annotation on immutable raw-count AnnData. "
        "No fixed cell-count cap: preflight checks the expanded matrix against the worker memory budget. "
        "Not a clinical interpretation or a claim of biological convergence.",
        "properties": {
            "schema": {"const": PARAMETER_SCHEMA},
            "resource_profile": {
                "enum": ["routine", "atlas"],
                "default": "routine",
                "description": "Operator-defined 128/256 GiB host-memory execution shape; both use one GPU. These are resource envelopes, not measured cell-count guarantees.",
            },
            "method": {"enum": ["scvi", "scanvi"], "default": "scvi"},
            "mode": {"enum": ["train", "map-query"], "default": "train"},
            "counts_source": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "default": "X",
                "description": "X, raw.X, or layers/<name>. Must contain unnormalized integer counts.",
            },
            "batch_key": {"type": ["string", "null"], "minLength": 1, "default": None},
            "labels_key": {"type": ["string", "null"], "minLength": 1, "default": None},
            "unlabeled_category": {
                "type": "string",
                "minLength": 1,
                "default": "Unknown",
            },
            "gene_selection": {
                "enum": ["hvg", "provided", "all"],
                "default": "hvg",
                "description": "hvg uses batch-aware Seurat v3; provided uses var.highly_variable; all retains all genes. Query mapping uses reference genes instead.",
            },
            "n_top_genes": {"type": "integer", "minimum": 2, "default": 3000},
            "hvg_span": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 1,
                "default": 0.3,
                "description": "Seurat-v3 LOESS smoothing span for HVG selection. Increase toward 1 for numerically singular batches, or provide your own var.highly_variable mask. No automatic method change is made.",
            },
            "max_epochs": {
                "type": ["integer", "null"],
                "minimum": 1,
                "default": None,
                "description": "scVI epoch budget; null uses the upstream cell-count heuristic. Separate from scANVI fine-tuning.",
            },
            "scanvi_max_epochs": {"type": "integer", "minimum": 1, "default": 20},
            "query_max_epochs": {"type": "integer", "minimum": 1, "default": 100},
            "early_stopping": {"type": "boolean", "default": True},
            "early_stopping_patience": {"type": "integer", "minimum": 1, "default": 30},
            "batch_size": {
                "type": "integer",
                "minimum": 16,
                "maximum": 8192,
                "default": 256,
            },
            "n_latent": {
                "type": "integer",
                "minimum": 2,
                "maximum": 256,
                "default": 10,
            },
            "n_hidden": {
                "type": "integer",
                "minimum": 16,
                "maximum": 1024,
                "default": 128,
            },
            "n_layers": {"type": "integer", "minimum": 1, "maximum": 8, "default": 2},
            "gene_likelihood": {"enum": ["nb", "zinb", "poisson"], "default": "nb"},
            "train_size": {
                "type": "number",
                "minimum": 0.5,
                "maximum": 0.99,
                "default": 0.9,
            },
            "seed": {
                "type": "integer",
                "minimum": 0,
                "maximum": 2147483647,
                "default": 0,
            },
            "checkpoint_every_n_epochs": {
                "type": "integer",
                "minimum": 1,
                "default": 5,
            },
            "visualization": {"enum": ["none", "sample", "full"], "default": "sample"},
            "visualization_cells": {"type": "integer", "minimum": 10, "default": 20000},
            "write_integrated_h5ad": {
                "type": "boolean",
                "default": True,
                "description": "Exports selected raw-count genes plus embeddings/labels. Original full input remains an immutable input artifact, not copied into every checkpoint.",
            },
            "max_wall_seconds": {
                "type": "integer",
                "minimum": 60,
                "maximum": 1209600,
                "default": 86400,
            },
            "max_output_bytes": {
                "type": "integer",
                "minimum": 1048576,
                "maximum": 100 * 1024**3,
                "default": 25 * 1024**3,
            },
            "output_destination": {
                "enum": ["customer-bucket", "platform-artifacts"],
                "default": "customer-bucket",
            },
            "output_prefix": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "default": "runs/scvi-scanvi",
            },
        },
    }


def normalize(value: object) -> dict[str, Any]:
    schema = request_schema()
    Draft202012Validator(schema).validate(value)
    result = copy.deepcopy(value)
    for name, spec in schema["properties"].items():
        if "default" in spec:
            result.setdefault(name, spec["default"])
    if (
        result["method"] == "scanvi"
        and result["mode"] == "train"
        and not result["labels_key"]
    ):
        raise ValueError("labels_key is required to train scANVI")
    source = result["counts_source"]
    if source not in {"X", "raw.X"} and not (
        source.startswith("layers/")
        and len(source.split("/")) == 2
        and source.split("/")[1] not in {"", ".", ".."}
    ):
        raise ValueError("counts_source must be X, raw.X or layers/<name>")
    prefix = result["output_prefix"]
    if (
        "\\" in prefix
        or any(part in {"", ".", ".."} for part in prefix.split("/"))
        or any(ord(c) < 32 for c in prefix)
    ):
        raise ValueError("output_prefix must be a nonempty relative object prefix")
    return result


def canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()

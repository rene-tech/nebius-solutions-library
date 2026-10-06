"""Load only the requested count matrix, never all unrelated AnnData layers."""

from __future__ import annotations

import os
from pathlib import Path

import anndata as ad
import h5py
import numpy as np
from scipy import sparse


def memory_budget() -> int:
    """Use the actual cgroup envelope; the operator can reserve a smaller budget."""
    limits = []
    if value := os.environ.get("FS2_SCVI_MEMORY_BYTES"):
        limits.append(int(value))
    for path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        try:
            limits.append(int(Path(path).read_text().strip()))
        except (OSError, ValueError):
            pass
    # Host fallback is for a local CLI, not a promise of schedulable capacity.
    limits.append(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    return min(limits)


def inspect_counts(path: Path, source: str) -> dict:
    with h5py.File(path, "r") as handle:
        key = "raw/X" if source == "raw.X" else source
        if key not in handle:
            raise ValueError(f"counts_source {source!r} is missing from the input")
        node = handle[key]
        if isinstance(node, h5py.Group):
            encoding = node.attrs.get("encoding-type")
            if encoding not in {"csr_matrix", "csc_matrix"}:
                raise ValueError(
                    "counts must be dense, CSR or CSC, not an arbitrary HDF5 group"
                )
            shape = tuple(int(n) for n in node.attrs["shape"])
            stored = sum(
                node[k].size * node[k].dtype.itemsize
                for k in ("data", "indices", "indptr")
            )
            nnz = int(node["data"].size)
        else:
            shape, stored, nnz = node.shape, node.size * node.dtype.itemsize, node.size
            encoding = "dense"
        if len(shape) != 2 or min(shape) < 2:
            raise ValueError("counts must contain at least two cells and two genes")
        # Includes sparse conversion/HVG copies, metadata, Python/CUDA and export.
        # Conservative estimate is observable and configurable, not a cell cap.
        estimated = int(stored * 4 + shape[0] * 4096 + 8 * 1024**3)
        return {
            "cells": shape[0],
            "genes": shape[1],
            "stored_entries": int(nnz),
            "matrix_bytes": int(stored),
            "estimated_host_bytes": estimated,
            "encoding": encoding,
            "counts_source": source,
        }


def validate_values(matrix) -> None:
    values = matrix.data if sparse.issparse(matrix) else matrix.reshape(-1)
    for start in range(0, len(values), 1024 * 1024):
        chunk = values[start : start + 1024 * 1024]
        if (
            not np.isfinite(chunk).all()
            or (chunk < 0).any()
            or not np.allclose(chunk, np.rint(chunk), rtol=0, atol=1e-6)
        ):
            raise ValueError(
                "selected counts source must contain finite nonnegative integer raw counts"
            )


def load_counts(path: Path, parameters: dict, *, budget_bytes: int | None = None):
    description = inspect_counts(path, parameters["counts_source"])
    budget = memory_budget() if budget_bytes is None else budget_bytes
    if description["estimated_host_bytes"] > budget * 0.85:
        raise ValueError(
            f"Input estimates {description['estimated_host_bytes']} host bytes but this worker has "
            f"{budget} bytes; select a larger-memory profile. No training was started."
        )
    with h5py.File(path, "r") as handle:
        matrix = ad.io.read_elem(
            handle[
                "raw/X"
                if parameters["counts_source"] == "raw.X"
                else parameters["counts_source"]
            ]
        )
        obs = ad.io.read_elem(handle["obs"])
        var = ad.io.read_elem(
            handle["raw/var" if parameters["counts_source"] == "raw.X" else "var"]
        )
    validate_values(matrix)
    if not obs.index.is_unique or not var.index.is_unique:
        raise ValueError(
            "Cell and gene identifiers must be unique; duplicates are not silently renamed"
        )
    for key in (parameters["batch_key"], parameters["labels_key"]):
        if key and key not in obs:
            raise ValueError(f"AnnData obs column not found: {key}")
        if key and obs[key].isna().any():
            raise ValueError(
                f"AnnData obs column {key!r} contains missing values; use an explicit unlabeled category"
            )
    matrix = matrix.astype(np.float32, copy=False)
    if sparse.issparse(matrix):
        matrix = matrix.tocsr()
    adata = ad.AnnData(matrix, obs=obs, var=var)
    description["input_genes"] = int(adata.n_vars)
    if parameters["mode"] == "train":
        selection = parameters["gene_selection"]
        if selection == "provided":
            if (
                "highly_variable" not in adata.var
                or adata.var["highly_variable"].dtype.kind != "b"
            ):
                raise ValueError(
                    "gene_selection=provided requires boolean var.highly_variable"
                )
            adata = adata[:, adata.var["highly_variable"].to_numpy()].copy()
        elif selection == "hvg" and adata.n_vars > parameters["n_top_genes"]:
            import scanpy as sc

            sc.pp.highly_variable_genes(
                adata,
                flavor="seurat_v3",
                n_top_genes=parameters["n_top_genes"],
                batch_key=parameters["batch_key"],
                subset=True,
            )
        if adata.n_vars < 2:
            raise ValueError("Gene selection retained fewer than two genes")
    description["selected_genes"] = int(adata.n_vars)
    description["host_memory_budget_bytes"] = budget
    return adata, description
